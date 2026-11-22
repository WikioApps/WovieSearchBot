"""Authorized Telegram ingestion with durable, fenced history checkpoints."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from telethon import TelegramClient

log = logging.getLogger(__name__)


async def resolve_peer(client: TelegramClient, chat_id: int) -> Any:
    """Resolve an authorized numeric peer, refreshing a StringSession's entity cache."""
    try:
        return await client.get_input_entity(chat_id)
    except ValueError:
        async for dialog in client.iter_dialogs():
            if dialog.id == chat_id:
                return dialog.input_entity
    raise ValueError(
        "The session account cannot access this chat; add it to the source first."
    )


class IndexRateLimit(RuntimeError):
    """A Telegram wait that the durable worker must respect."""

    def __init__(self, seconds: int):
        self.retry_after = max(1, seconds)
        super().__init__(
            f"Telegram requires a {self.retry_after}-second indexing pause."
        )


class Indexer:
    def __init__(self, settings: Any, store: Any, bot: Any):
        self.settings = settings
        self.store = store
        self.bot = bot
        self.client: TelegramClient | None = None
        self.bot_id: int | None = None
        self._closing = False
        self._history_lock = asyncio.Lock()

    async def start(self) -> None:
        self.bot_id = self.bot.id
        if not self.settings.session_string:
            return
        from telethon import TelegramClient, events
        from telethon.sessions import StringSession

        self.client = TelegramClient(
            StringSession(self.settings.session_string),
            self.settings.api_id,
            self.settings.api_hash,
            connection_retries=3,
            request_retries=3,
            retry_delay=2,
            flood_sleep_threshold=0,
            sequential_updates=True,
            entity_cache_limit=256,
        )
        try:
            await self.client.connect()
            if not await self.client.is_user_authorized():
                raise RuntimeError(
                    "SESSION_STRING is not authorized; generate a new session."
                )
            account = await self.client.get_me()
            if account.bot:
                raise RuntimeError(
                    "SESSION_STRING must belong to an authorized user account."
                )
            self.client.add_event_handler(self._live, events.NewMessage())
            self.client.add_event_handler(self._edited, events.MessageEdited())
            self.client.add_event_handler(self._deleted, events.MessageDeleted())
            log.info("Authorized session importer connected.")
        except BaseException:
            await self.client.disconnect()
            self.client = None
            raise

    async def close(self) -> None:
        self._closing = True
        if self.client:
            await self.client.disconnect()
            self.client = None

    async def _source(self, chat_id: int) -> dict | None:
        return await self.store.db.sources.find_one({"_id": chat_id, "active": True})

    async def resolve_source(self, chat_id: int) -> Any:
        """Resolve an existing membership; this never joins a chat or imports an invite."""
        if not self.client:
            raise ValueError(
                "Historical indexing requires API_ID, API_HASH and SESSION_STRING."
            )
        # StringSession contains authentication, not a durable entity/access-hash cache.
        from telethon import utils
        from telethon.tl.types import Channel, Chat

        entity = await self.client.get_entity(await resolve_peer(self.client, chat_id))
        if not isinstance(entity, (Channel, Chat)):
            raise ValueError(
                "Only existing channels and groups can be registered as history sources."
            )
        if utils.get_peer_id(entity) != chat_id:
            raise ValueError(
                "Use the complete signed Telegram chat ID, including the -100 prefix for channels."
            )
        if getattr(entity, "left", False) or getattr(entity, "kicked", False):
            raise ValueError(
                "The session account is not an active member of this source."
            )
        return entity

    async def queue(
        self, chat_id: int, actor_id: int, start_id: int = 0, end_id: int = 0
    ) -> str:
        if not self.client:
            raise ValueError("Configure SESSION_STRING to import channel history.")
        from telethon import errors

        if actor_id not in self.settings.admins:
            raise PermissionError(
                "Only configured bot owners can start a history scan."
            )
        if start_id < 0 or end_id < 0 or (end_id and start_id > end_id):
            raise ValueError(
                "Message IDs must be nonnegative, with start no greater than end."
            )
        source = await self._source(chat_id)
        if not source:
            raise ValueError(
                "Register and enable this source with /source add before indexing."
            )
        try:
            entity = await self.resolve_source(chat_id)
            if getattr(entity, "noforwards", False):
                raise ValueError(
                    "This source protects its content; Telegram-side copying is unavailable."
                )
            if not end_id:
                newest = await self.client.get_messages(entity, limit=1)
                if not newest:
                    raise ValueError("The source has no accessible messages.")
                end_id = newest[0].id
        except errors.FloodWaitError as exc:
            raise IndexRateLimit(exc.seconds) from None
        if end_id < max(1, start_id):
            raise ValueError("No accessible messages exist in the requested range.")
        # A fixed upper bound makes completion meaningful while new messages keep arriving.
        payload = {
            "chat_id": chat_id,
            "actor_id": actor_id,
            "start_id": start_id,
            "end_id": end_id,
            "cursor": max(0, start_id - 1),
            "indexed": 0,
            "scanned": 0,
            "skipped": 0,
        }
        return await self.store.enqueue("index", payload)

    @staticmethod
    def _fence(job: dict) -> dict:
        return {
            "_id": job["_id"],
            "worker_id": job.get("worker_id"),
            "status": "running",
            "lease_until": {"$gt": datetime.now(timezone.utc)},
        }

    async def _continue(self, job: dict, chat_id: int) -> bool:
        if self._closing:
            return False
        if not await self.store.db.jobs.find_one(self._fence(job), {"_id": 1}):
            return False
        if not await self._source(chat_id):
            await self.store.db.jobs.update_one(
                self._fence(job),
                {
                    "$set": {
                        "status": "paused",
                        "error": "Source is disabled or no longer configured.",
                    }
                },
            )
            return False
        return True

    async def run_job(self, job: dict) -> None:
        if not self.client:
            raise ValueError(
                "Session importer is disabled; configure SESSION_STRING and restart."
            )
        from telethon import errors

        async with self._history_lock:
            payload = dict(job["payload"])
            chat_id = int(payload["chat_id"])
            if not await self._continue(job, chat_id):
                return
            try:
                entity = await self.resolve_source(chat_id)
                if getattr(entity, "noforwards", False):
                    raise ValueError(
                        "Source now protects its content; indexing was stopped."
                    )
                cursor = int(payload.get("cursor", 0))
                end_id = int(payload["end_id"])
                # min_id/max_id are exclusive; reverse walks oldest to newest.
                async for message in self.client.iter_messages(
                    entity,
                    min_id=cursor,
                    max_id=end_id + 1,
                    reverse=True,
                    wait_time=1,
                ):
                    if not await self._continue(job, chat_id):
                        return
                    file_id = await self._ingest_session(chat_id, message)
                    payload["cursor"] = message.id
                    payload["scanned"] = int(payload.get("scanned", 0)) + 1
                    label = "indexed" if file_id else "skipped"
                    payload[label] = int(payload.get(label, 0)) + 1
                    result = await self.store.db.jobs.update_one(
                        self._fence(job),
                        {"$set": {"payload": payload}},
                    )
                    if not result.matched_count:
                        return
                if not await self._continue(job, chat_id):
                    return
                # Missing/deleted IDs are legitimate; finishing the bounded scan is enough.
                payload["cursor"] = end_id
                payload["completed_at"] = datetime.now(timezone.utc)
                result = await self.store.db.jobs.update_one(
                    self._fence(job),
                    {"$set": {"payload": payload}},
                )
                if result.matched_count:
                    await self.store.enqueue(
                        "notify",
                        {
                            "chat_id": payload["actor_id"],
                            "text": (
                                f"Index complete\nSource: {chat_id}\n"
                                f"Scanned: {payload.get('scanned', 0):,}\n"
                                f"Saved/reconciled: {payload.get('indexed', 0):,}\n"
                                f"Skipped: {payload.get('skipped', 0):,}"
                            ),
                        },
                        key=f"index-complete:{job['_id']}",
                    )
            except errors.FloodWaitError as exc:
                raise IndexRateLimit(exc.seconds) from None

    async def _ingest_session(self, chat_id: int, message: Any) -> str | None:
        media = getattr(message, "document", None)
        if not media or getattr(message, "noforwards", False):
            return None
        size = int(getattr(media, "size", 0))
        if self.settings.max_file_size and size > self.settings.max_file_size:
            return None
        kind = "video" if message.video else "audio" if message.audio else "document"
        file = message.file
        caption = (message.message or "")[:4096]
        fallback = caption.splitlines()[0][:160] if caption else f"{kind}_{message.id}"
        filename = file.name or fallback + (file.ext or "")
        return await self.store.put_file(
            {
                "source_chat": chat_id,
                "message_id": message.id,
                "filename": filename[:512],
                "caption": caption,
                "file_size": size,
                "media_type": kind,
                "file_id": None,
                "file_unique_id": None,
                "bot_id": None,
                "source_protected": False,
                "source_date": message.date,
            }
        )

    async def ingest_bot_message(self, message: Any) -> str | None:
        chat_id = message.chat.id
        if not await self._source(chat_id):
            return None
        if message.has_protected_content or getattr(
            message.chat, "has_protected_content", False
        ):
            return None
        media = message.document or message.video or message.audio
        if media is None:
            if message.edit_date:
                await self._mark_deleted(chat_id, [message.message_id])
            return None
        size = int(media.file_size or 0)
        if self.settings.max_file_size and size > self.settings.max_file_size:
            return None
        kind = "video" if message.video else "audio" if message.audio else "document"
        caption = (message.caption or "")[:4096]
        suffix = ".mp4" if kind == "video" else ".mp3" if kind == "audio" else ""
        fallback = (
            caption.splitlines()[0][:160] if caption else f"{kind}_{message.message_id}"
        )
        return await self.store.put_file(
            {
                "source_chat": chat_id,
                "message_id": message.message_id,
                "filename": (media.file_name or fallback + suffix)[:512],
                "caption": caption,
                "file_size": size,
                "media_type": kind,
                "file_id": media.file_id,
                "file_unique_id": media.file_unique_id,
                "bot_id": self.bot_id,
                "source_protected": False,
                "source_date": message.date,
            }
        )

    async def _live(self, event: Any) -> None:
        if self._closing or event.chat_id is None:
            return
        try:
            source = await self._source(event.chat_id)
            if not source:
                return
            if getattr(await event.get_chat(), "noforwards", False):
                return
            await self._ingest_session(event.chat_id, event.message)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Do not log messages or sessions. A durable marker exposes gaps for admin backfill.
            log.exception("Session live ingestion failed for a configured source.")
            try:
                await self.store.db.sources.update_one(
                    {"_id": event.chat_id},
                    {
                        "$set": {
                            "reconcile_required": True,
                            "last_ingest_error_at": datetime.now(timezone.utc),
                        }
                    },
                )
            except Exception:
                log.error(
                    "Could not persist ingestion gap marker; check database availability."
                )

    async def _edited(self, event: Any) -> None:
        if event.chat_id is None or not await self._source(event.chat_id):
            return
        if not event.message.document:
            await self._mark_deleted(event.chat_id, [event.message.id])
            return
        await self._live(event)

    async def _deleted(self, event: Any) -> None:
        # Telegram does not identify the chat for every deletion event. Never guess using
        # message_id alone, since message IDs overlap across different chats.
        if event.chat_id is not None and await self._source(event.chat_id):
            await self._mark_deleted(event.chat_id, event.deleted_ids)

    async def _mark_deleted(self, chat_id: int, message_ids: list[int]) -> None:
        if not message_ids:
            return
        for catalog in self.store.catalogs:
            await catalog.files.update_many(
                {
                    "source_chat": chat_id,
                    "message_id": {"$in": message_ids},
                },
                {
                    "$set": {
                        "deleted_at": datetime.now(timezone.utc),
                        "deletion_reason": "source_message_removed",
                    }
                },
            )
