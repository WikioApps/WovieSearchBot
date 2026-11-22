"""Poll Telegram after persisting incoming updates, then dispatch from the inbox."""

from __future__ import annotations

import argparse
import asyncio
from contextlib import AsyncExitStack, suppress
from datetime import UTC, datetime, timedelta
import logging
import signal
import secrets
import sys

from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.exceptions import (
    TelegramConflictError,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.types import BotCommand, Message
from pymongo import UpdateOne
from pymongo.errors import DuplicateKeyError

from .config import Settings

log = logging.getLogger(__name__)


class PollerLease:
    def __init__(self, db, bot_id):
        self.collection = db.poll_leases
        self.bot_id = bot_id
        self.owner = secrets.token_hex(16)

    async def acquire(self):
        now = datetime.now(UTC)
        try:
            await self.collection.update_one(
                {"_id": self.bot_id, "expires": {"$lte": now}},
                {"$set": {"owner": self.owner, "expires": now + timedelta(seconds=60)}},
                upsert=True,
            )
        except DuplicateKeyError:
            raise RuntimeError(
                "An active poller already holds this bot's lease"
            ) from None

    async def maintain(self):
        while True:
            await asyncio.sleep(15)
            now = datetime.now(UTC)
            result = await self.collection.update_one(
                {"_id": self.bot_id, "owner": self.owner, "expires": {"$gt": now}},
                {"$set": {"expires": now + timedelta(seconds=60)}},
            )
            if result.matched_count != 1:
                raise RuntimeError("Lost Telegram polling lease")

    async def release(self):
        await self.collection.delete_one({"_id": self.bot_id, "owner": self.owner})


async def receive(settings, bot, dp, db):
    allowed = dp.resolve_used_update_types()
    state = await db.poll_state.find_one({"_id": bot.id})
    updated_at = state.get("updated_at") if state else None
    offset = state["next_id"] if state else None
    backoff = 1
    while True:
        try:
            # After a week of inactivity Telegram can choose a new random update ID.
            if updated_at and datetime.now(UTC) - updated_at >= timedelta(days=6):
                offset = None
            updates = await bot.get_updates(
                offset=offset,
                limit=20,
                timeout=settings.poll_timeout,
                allowed_updates=allowed,
                request_timeout=settings.poll_timeout + 15,
            )
            if updates:
                now = datetime.now(UTC)
                ops = []
                for u in updates:
                    ops.append(
                        UpdateOne(
                            {"_id": f"{bot.id}:{u.update_id}"},
                            {
                                "$setOnInsert": {
                                    "bot_id": bot.id,
                                    "seq": u.update_id,
                                    "status": "pending",
                                    "next": now,
                                    "attempts": 0,
                                    "priority": "checkout"
                                    if u.pre_checkout_query
                                    else "normal",
                                    "update": u.model_dump(
                                        mode="json",
                                        exclude_none=True,
                                        exclude_defaults=True,
                                        by_alias=True,
                                    ),
                                }
                            },
                            upsert=True,
                        )
                    )
                await db.inbox.bulk_write(ops)
                next_id = updates[-1].update_id + 1
                await db.poll_state.update_one(
                    {"_id": bot.id},
                    {"$set": {"next_id": next_id, "updated_at": now}},
                    upsert=True,
                )
                offset = next_id
                updated_at = now
            backoff = 1
        except TelegramConflictError:
            raise RuntimeError(
                "Another worker is polling with this bot token"
            ) from None
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except Exception as exc:
            log.warning("Polling paused (%s)", type(exc).__name__)
            await asyncio.sleep(backoff)
            backoff = min(30, backoff * 2)


async def dispatch(bot, dp, db):
    # Telegram gives checkout queries ten seconds. Long-running commands may
    # continue in the normal lane while checkout validation answers immediately.
    async with asyncio.TaskGroup() as group:
        group.create_task(_dispatch_lane(bot, dp, db, checkout=True))
        group.create_task(_dispatch_lane(bot, dp, db, checkout=False))


async def _dispatch_lane(bot, dp, db, *, checkout):
    priority = "checkout" if checkout else {"$ne": "checkout"}
    while True:
        try:
            row = await db.inbox.find_one(
                {
                    "bot_id": bot.id,
                    "status": "pending",
                    "priority": priority,
                    "next": {"$lte": datetime.now(UTC)},
                },
                sort=[("seq", 1)],
            )
            if not row:
                await asyncio.sleep(0.15)
                continue
            try:
                await dp.feed_raw_update(bot, row["update"])
            except TelegramForbiddenError:
                pass
            except Exception as exc:
                attempts = row["attempts"] + 1
                delay = getattr(exc, "retry_after", None) or min(60, 2**attempts)
                failed = attempts >= 6
                change = {
                    "attempts": attempts,
                    "error": type(exc).__name__,
                    "status": "failed" if failed else "pending",
                    "next": datetime.now(UTC) + timedelta(seconds=delay),
                }
                if failed:
                    change["expires_at"] = datetime.now(UTC) + timedelta(days=7)
                await db.inbox.update_one({"_id": row["_id"]}, {"$set": change})
                log.warning(
                    "Update %s attempt %d (%s)",
                    row["seq"],
                    attempts,
                    type(exc).__name__,
                )
                continue
            await db.inbox.update_one(
                {"_id": row["_id"]},
                {
                    "$set": {
                        "status": "done",
                        "expires_at": datetime.now(UTC) + timedelta(days=2),
                    },
                    "$unset": {"update": ""},
                },
            )
        except Exception as exc:
            log.error("Inbox unavailable (%s)", type(exc).__name__)
            await asyncio.sleep(2)


async def run(settings, check=False):
    from . import billing, alerts, community, providers, operations, curation, discover
    from .delivery import Delivery
    from .handlers import admin, user
    from .indexer import Indexer
    from .jobs import Jobs
    from .metadata import Metadata
    from .store import Store
    from .extensions import build_router as extras

    async with AsyncExitStack() as stack:
        import os
        from urllib.parse import urlsplit

        base_url = os.environ.get("BOT_API_BASE_URL")
        session = None
        if base_url:
            parsed = urlsplit(base_url)
            if (
                not parsed.hostname
                or parsed.username
                or parsed.password
                or not (
                    parsed.scheme == "https"
                    or parsed.scheme == "http"
                    and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
                )
            ):
                raise ValueError(
                    "BOT_API_BASE_URL needs HTTPS or a loopback HTTP server"
                )
            session = AiohttpSession(
                api=TelegramAPIServer.from_base(base_url.rstrip("/"))
            )
        bot = Bot(
            settings.bot_token,
            session=session,
            default=DefaultBotProperties(parse_mode="HTML"),
        )
        stack.push_async_callback(bot.session.close)
        store = Store(settings)
        stack.push_async_callback(store.close)
        await store.connect()
        profile = await bot.me()
        db = store.db
        await db.inbox.create_index([("bot_id", 1), ("status", 1), ("seq", 1)])
        await db.inbox.create_index(
            [("bot_id", 1), ("status", 1), ("priority", 1), ("seq", 1)]
        )
        await db.inbox.create_index("expires_at", expireAfterSeconds=0)
        await db.invoices.create_index("valid_until")
        metadata = Metadata(settings)
        stack.push_async_callback(metadata.close)
        # Acquire and renew the poll lease before starting an MTProto listener.
        # This also protects a configured user session during --check.
        lease_task = None
        if not check or settings.session_string:
            lease = PollerLease(db, bot.id)
            await lease.acquire()
            stack.push_async_callback(lease.release)
            lease_task = asyncio.create_task(lease.maintain())

            async def stop_lease():
                lease_task.cancel()
                await asyncio.gather(lease_task, return_exceptions=True)

            stack.push_async_callback(stop_lease)
        indexer = Indexer(settings, store, bot)
        stack.push_async_callback(indexer.close)
        await indexer.start()
        delivery = Delivery(bot, store, settings)
        dp = Dispatcher(disable_fsm=True)
        ingest = Router(name="channel-ingestion")

        async def index_message(message: Message):
            await indexer.ingest_bot_message(message)

        for observer in (
            ingest.channel_post,
            ingest.edited_channel_post,
            ingest.edited_message,
        ):
            observer.register(index_message)

        # A group file may also carry a caption command. Save through middleware
        # so indexing never consumes normal messages before the command routers.
        @dp.message.outer_middleware()
        async def group_ingestion(handler, event, data):
            if event.chat.type in ("group", "supergroup") and (
                event.document or event.audio or event.video
            ):
                await indexer.ingest_bot_message(event)
            return await handler(event, data)

        dp.message.middleware(operations.AdminAuditMiddleware(settings, store))
        dp.include_routers(
            ingest,
            admin.build_router(settings, store, indexer),
            operations.build_router(settings, store, delivery),
            community.build_router(settings, store, delivery),
            providers.build_router(settings, store, delivery, metadata),
            curation.build_router(settings, store, delivery),
            discover.build_router(settings, store, delivery, metadata),
            billing.build_router(settings, store),
            extras(settings, store, delivery, metadata),
            user.build_router(settings, store, delivery, metadata),
        )
        if lease_task is not None and lease_task.done():
            await lease_task
        if check:
            print(
                f"MongoDB, routers and Telegram credentials ready for @{profile.username}."
            )
            return
        await bot.delete_webhook(drop_pending_updates=False)
        await bot.set_my_commands(
            [
                BotCommand(command=c, description=d)
                for c, d in [
                    ("start", "Open Wovie"),
                    ("search", "Search indexed files"),
                    ("help", "Commands and search tips"),
                    ("favorites", "Open your saved files"),
                    ("request", "Request a title"),
                    ("watchlist", "View your watchlist"),
                    ("premium", "Plan and expiry"),
                    ("settings", "Chat settings"),
                ]
            ]
        )
        jobs = Jobs(settings, store, bot, indexer, delivery)
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop.set)
        from .jobs import KINDS

        timed_jobs = ("delete", "notify", "announcement", "digest")
        general_jobs = tuple(
            kind for kind in KINDS if kind not in ("index", *timed_jobs)
        )
        workers = (
            receive(settings, bot, dp, db),
            dispatch(bot, dp, db),
            alerts.schedule(store, metadata),
            jobs.run(("index",)),
            jobs.run(timed_jobs),
            jobs.run(general_jobs),
            stop.wait(),
        )
        tasks = [asyncio.create_task(coro) for coro in workers]
        tasks.append(lease_task)
        try:
            log.info("Serving @%s", profile.username)
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                await task
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


def main():
    parser = argparse.ArgumentParser(description="Wovie Telegram search bot")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check MongoDB, Telegram identity and configured session",
    )
    args = parser.parse_args()
    try:
        settings = Settings.from_env()
        logging.basicConfig(
            level=settings.log_level,
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )
        for name in ("httpx", "httpcore", "pymongo", "telethon", "aiogram"):
            logging.getLogger(name).setLevel(logging.WARNING)
        asyncio.run(run(settings, args.check))
    except (KeyboardInterrupt, SystemExit):
        raise
    except ValueError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    except Exception as exc:
        print(
            f"Wovie stopped ({type(exc).__name__}). Check credentials, database access and service logs.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
