"""Access-checked Telegram delivery without downloading media onto the host."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import secrets

from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from pymongo import ReturnDocument

from wovie.ui import caption


class DeliveryError(Exception):
    """An actionable failure safe to display to the requester."""

    def __init__(self, message: str, retry_after: int | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class Delivery:
    def __init__(self, bot, store, settings):
        self.bot, self.store, self.settings = bot, store, settings

    async def _member(
        self, user_id: int, chat_id: int | str, refresh: bool = False
    ) -> bool:
        key = f"{chat_id}:{user_id}"
        now = datetime.now(UTC)
        if not refresh:
            cached = await self.store.db.membership_cache.find_one(
                {"_id": key, "expires_at": {"$gt": now}}
            )
            if cached is not None:
                return cached["member"]
        try:
            member = await self.bot.get_chat_member(chat_id, user_id)
            present = member.status in ("creator", "administrator", "member") or (
                member.status == "restricted" and getattr(member, "is_member", False)
            )
        except (TelegramBadRequest, TelegramForbiddenError):
            return False
        except TelegramRetryAfter as exc:
            raise DeliveryError(
                "Telegram membership checks are rate limited. Please try again shortly.",
                exc.retry_after,
            ) from exc
        except (TelegramNetworkError, asyncio.TimeoutError) as exc:
            raise DeliveryError(
                "Telegram membership checks are temporarily unavailable.", 30
            ) from exc
        await self.store.db.membership_cache.update_one(
            {"_id": key},
            {
                "$set": {
                    "member": bool(present),
                    "expires_at": now + timedelta(seconds=30),
                }
            },
            upsert=True,
        )
        return bool(present)

    async def allowed(
        self, user_id: int, group_id: int | None = None
    ) -> tuple[bool, str]:
        user = await self.store.db.users.find_one({"_id": user_id}) or {}
        if user.get("banned"):
            return False, "Your access is disabled. Contact the bot administrator."
        if user_id in self.settings.admins:
            return True, ""
        if not self.settings.public_search:
            approved = bool(user.get("approved"))
            candidates = ([group_id] if group_id else []) + list(
                user.get("authorized_groups", [])
            )[:16]
            for chat in dict.fromkeys(candidates):
                cfg = await self.store.setting(chat)
                if cfg.get("enabled") and await self._member(user_id, chat):
                    approved = True
                    if group_id == chat:
                        await self.store.db.users.update_one(
                            {"_id": user_id},
                            {"$addToSet": {"authorized_groups": chat}},
                            upsert=True,
                        )
                    break
            if not approved:
                return (
                    False,
                    "This bot is private. Use an authorized group or contact its administrator.",
                )
        config = await self.store.setting(0)
        channels = list(self.settings.required_channels) + list(
            config.get("required_channels") or []
        )
        for channel in dict.fromkeys(channels):
            if not await self._member(user_id, channel):
                label = (
                    f"@{channel.lstrip('@')} "
                    if isinstance(channel, str) and not channel.lstrip("-").isdigit()
                    else ""
                )
                return (
                    False,
                    f"Join the required channel {label}and try again. Membership could not be verified.",
                )
        return True, ""

    async def can_access_source(
        self, user_id: int, source: int | dict, refresh: bool = False
    ) -> bool:
        if isinstance(source, int):
            source = await self.store.db.sources.find_one({"_id": source})
        if not source or not source.get("active", True):
            return False
        if source.get("protected") or source.get("has_protected_content"):
            return False
        if user_id in self.settings.admins or source.get("public", False):
            return True
        return await self._member(user_id, source["_id"], refresh=refresh)

    async def visible_sources(
        self, user_id: int, source_ids: list[int] | None = None
    ) -> list[int]:
        query = {"active": {"$ne": False}}
        if source_ids is not None:
            query["_id"] = {"$in": source_ids}
        visible = []
        async for source in self.store.db.sources.find(query).limit(1000):
            if await self.can_access_source(user_id, source):
                visible.append(source["_id"])
        return visible

    async def check_file(self, user_id: int, file_id: str) -> dict:
        allowed, reason = await self.allowed(user_id)
        if not allowed:
            raise DeliveryError(reason)
        from .operations import verification_allowed

        if not await verification_allowed(self.settings, self.store, user_id):
            raise DeliveryError("Complete /verify to unlock file delivery.")
        record = await self.store.get_file(file_id)
        if not record:
            raise DeliveryError(
                "This file is unavailable or was removed from the catalog."
            )
        source = await self.store.db.sources.find_one({"_id": record["source_chat"]})
        if not await self.can_access_source(user_id, source, refresh=True):
            raise DeliveryError(
                "This source is private, disabled, or inaccessible. Join it or contact the administrator."
            )
        if record.get("source_protected"):
            raise DeliveryError(
                "This source protects its content; file forwarding is unavailable."
            )
        try:
            live_source = await self.bot.get_chat(record["source_chat"])
        except (TelegramBadRequest, TelegramForbiddenError) as exc:
            raise DeliveryError(
                "Source access could not be verified. Ask the administrator to check the bot’s source permissions."
            ) from exc
        except TelegramRetryAfter as exc:
            raise DeliveryError(
                "Telegram source checks are rate limited. Try again shortly.",
                exc.retry_after,
            ) from exc
        except (TelegramNetworkError, asyncio.TimeoutError) as exc:
            raise DeliveryError(
                "Telegram source access checks are temporarily unavailable.", 30
            ) from exc
        if live_source.has_protected_content:
            raise DeliveryError(
                "This source now protects its content; forwarding is unavailable."
            )
        config = await self.store.setting(0)
        user = await self.store.db.users.find_one({"_id": user_id}) or {}
        premium = user.get("premium_until")
        if user_id not in self.settings.admins and (
            source.get("premium_only") or config.get("premium_only")
        ):
            if not premium or premium <= datetime.now(UTC):
                raise DeliveryError(
                    "This file requires an active plan. Open /premium for details."
                )
        return record

    async def _reserve_quota(self, user_id: int) -> str | None:
        config = await self.store.setting(0)
        limit = int(config.get("daily_limit", self.settings.daily_limit))
        if limit <= 0 or user_id in self.settings.admins:
            return None
        user = await self.store.db.users.find_one({"_id": user_id}) or {}
        if user.get("premium_until") and user["premium_until"] > datetime.now(UTC):
            return None
        now = datetime.now(UTC)
        key = f"{now:%Y-%m-%d}:{user_id}"
        await self.store.db.delivery_quota.update_one(
            {"_id": key},
            {"$setOnInsert": {"used": 0, "expires_at": now + timedelta(days=2)}},
            upsert=True,
        )
        reserved = await self.store.db.delivery_quota.find_one_and_update(
            {"_id": key, "used": {"$lt": limit}},
            {"$inc": {"used": 1}},
            return_document=ReturnDocument.AFTER,
        )
        if not reserved:
            raise DeliveryError(
                "Your daily delivery limit has been reached. It resets at 00:00 UTC."
            )
        return key

    async def check_group(
        self, user_id: int, group_id: int | None, file_id: str
    ) -> dict:
        if group_id is None or group_id >= 0:
            return {}
        ok, reason = await self.allowed(user_id, group_id=group_id)
        if not ok:
            raise DeliveryError(reason)
        config = await self.store.setting(group_id)
        if not config.get("enabled", False):
            raise DeliveryError(
                "Search and delivery have been disabled in the originating group."
            )
        record = await self.store.get_file(file_id)
        if not record:
            raise DeliveryError("This file is unavailable.")
        scoped = config.get("source_ids")
        if scoped and record["source_chat"] not in scoped:
            raise DeliveryError(
                "This file is outside the originating group’s allowed sources."
            )
        if user_id not in self.settings.admins:
            if not await self._member(user_id, group_id, refresh=True):
                raise DeliveryError(
                    "You must remain a member of the originating group."
                )
            for channel in config.get("required_channels") or []:
                if not await self._member(user_id, channel, refresh=True):
                    raise DeliveryError(
                        "Join the originating group’s required channels and try again."
                    )
            user = await self.store.db.users.find_one({"_id": user_id}) or {}
            if config.get("premium_only") and (
                not user.get("premium_until")
                or user["premium_until"] <= datetime.now(UTC)
            ):
                raise DeliveryError("The originating group requires an active plan.")
        return config

    async def _original_with_alternates(self, file_id: str) -> list[str]:
        original = None
        # Internal lookup only: never return unavailable-source metadata to a user.
        # Every candidate is independently checked before sending any content.
        for catalog in self.store.catalogs:
            original = await catalog.files.find_one(
                {"_id": file_id, "deleted_at": None}, {"alternate_ids": 1}
            )
            if original:
                break
        return list(
            dict.fromkeys([file_id, *((original or {}).get("alternate_ids") or [])[:5]])
        )

    async def _transmit(self, user_id: int, record: dict, config: dict):
        from .buttons import render_buttons

        kwargs = {
            "chat_id": user_id,
            "caption": caption(record, config.get("caption_template")),
            "parse_mode": "HTML",
            "protect_content": bool(
                config.get("protect_content", self.settings.protect_content)
            ),
            "reply_markup": render_buttons(config.get("delivery_buttons"), record),
        }
        if record.get("file_id") and record.get("bot_id") == self.bot.id:
            kind = record.get("media_type", "document")
            if kind not in ("video", "audio", "document"):
                kind = "document"
            try:
                return await getattr(self.bot, f"send_{kind}")(
                    **kwargs, **{kind: record["file_id"]}
                )
            except TelegramBadRequest:
                pass
        return await self.bot.copy_message(
            **kwargs,
            from_chat_id=record["source_chat"],
            message_id=record["message_id"],
        )

    async def send(self, user_id: int, file_id: str, group_id: int | None = None):
        candidates = await self._original_with_alternates(file_id)
        quota_key = None
        receipt_id = None
        last_error = "This file and its alternate locations are unavailable."
        sent = None
        config = {}
        for candidate in candidates:
            try:
                group_config = await self.check_group(user_id, group_id, candidate)
                record = await self.check_file(user_id, candidate)
            except DeliveryError as exc:
                if exc.retry_after:
                    raise
                last_error = str(exc)
                continue
            config = dict(await self.store.setting(0))
            for key in ("caption_template", "delete_after", "delivery_buttons"):
                if key in group_config:
                    config[key] = group_config[key]
            config["protect_content"] = bool(
                config.get("protect_content", self.settings.protect_content)
                or group_config.get("protect_content")
            )
            if receipt_id is None:
                quota_key = await self._reserve_quota(user_id)
                receipt_id = secrets.token_hex(16)
                await self.store.db.delivery_receipts.insert_one(
                    {
                        "_id": receipt_id,
                        "user_id": user_id,
                        "file_id": file_id,
                        "state": "sending",
                        "created_at": datetime.now(UTC),
                        "expires_at": datetime.now(UTC) + timedelta(days=30),
                    }
                )
            publication = (
                await self.store.db.sources.find_one(
                    {"_id": record["source_chat"]}, {"public": 1}
                )
                or {}
            )
            await self.store.db.delivery_receipts.update_one(
                {"_id": receipt_id},
                {
                    "$set": {
                        "selected_file_id": candidate,
                        "source_chat": record["source_chat"],
                        "count_for_popularity": bool(
                            config.get("popularity_enabled", False)
                            and publication.get("public", False)
                        ),
                    }
                },
            )
            try:
                sent = await self._transmit(user_id, record, config)
                break
            except (TelegramBadRequest, TelegramForbiddenError) as exc:
                last_error = "Unable to deliver this file. Start the bot privately; its administrator may need to restore source access."
                await self.store.db.delivery_receipts.update_one(
                    {"_id": receipt_id}, {"$set": {"error": type(exc).__name__}}
                )
                continue
            except TelegramRetryAfter as exc:
                if quota_key:
                    await self.store.db.delivery_quota.update_one(
                        {"_id": quota_key, "used": {"$gt": 0}}, {"$inc": {"used": -1}}
                    )
                await self.store.db.delivery_receipts.update_one(
                    {"_id": receipt_id}, {"$set": {"state": "rate_limited"}}
                )
                raise DeliveryError(
                    f"Telegram asks us to wait {exc.retry_after} seconds. Please try again afterward.",
                    exc.retry_after,
                ) from exc
            except (TelegramNetworkError, asyncio.TimeoutError) as exc:
                await self.store.db.delivery_receipts.update_one(
                    {"_id": receipt_id}, {"$set": {"state": "uncertain"}}
                )
                raise DeliveryError(
                    "Telegram did not confirm delivery. Check your messages before retrying to avoid duplicates."
                ) from exc
        if sent is None:
            if quota_key:
                await self.store.db.delivery_quota.update_one(
                    {"_id": quota_key, "used": {"$gt": 0}}, {"$inc": {"used": -1}}
                )
            if receipt_id:
                await self.store.db.delivery_receipts.update_one(
                    {"_id": receipt_id}, {"$set": {"state": "failed"}}
                )
            raise DeliveryError(last_error)
        await self.store.db.delivery_receipts.update_one(
            {"_id": receipt_id},
            {
                "$set": {
                    "state": "sent",
                    "message_id": sent.message_id,
                    "sent_at": datetime.now(UTC),
                }
            },
        )
        delete_after = int(config.get("delete_after", self.settings.delete_after))
        if delete_after > 0:
            await self.store.enqueue(
                "delete",
                {"chat_id": user_id, "message_id": sent.message_id},
                run_at=datetime.now(UTC)
                + timedelta(seconds=min(delete_after, 47 * 3600)),
                key=f"delete:{user_id}:{sent.message_id}",
            )
        return sent
