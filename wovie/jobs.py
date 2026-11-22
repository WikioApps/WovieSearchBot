"""Persistent work with lease renewal and incremental checkpoints."""

from __future__ import annotations

import asyncio
import csv
from datetime import UTC, datetime
import logging
import os
from pathlib import Path
import secrets
import shutil
from tempfile import TemporaryDirectory

from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramMigrateToChat,
    TelegramRetryAfter,
)
from aiogram.types import FSInputFile

log = logging.getLogger(__name__)
UPLOAD_LIMIT = 49_000_000
KINDS = (
    "delete",
    "broadcast",
    "batch",
    "notify",
    "export",
    "backup",
    "forward",
    "release",
    "publish",
    "curation",
    "announcement",
    "digest",
)


class LostLease(Exception):
    pass


class Jobs:
    def __init__(self, settings, store, bot, indexer, delivery):
        self.settings, self.store, self.bot = settings, store, bot
        self.indexer, self.delivery = indexer, delivery

    def fence(self, job):
        return {
            "_id": job["_id"],
            "status": "running",
            "worker_id": job["worker_id"],
            "lease_until": {"$gt": datetime.now(UTC)},
        }

    async def checkpoint(self, job, **fields):
        changed = await self.store.db.jobs.update_one(
            self.fence(job),
            {"$set": {f"payload.{key}": value for key, value in fields.items()}},
        )
        if not changed.matched_count:
            raise LostLease

    async def run(self, kinds=KINDS):
        worker = f"{os.getpid()}-{secrets.token_hex(6)}"
        while True:
            try:
                job = await self.store.claim_job(worker, kinds=kinds)
                if job:
                    await self.execute(job, worker)
                    continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("Job scheduler unavailable (%s)", type(exc).__name__)
            await asyncio.sleep(self.settings.job_interval)

    async def execute(self, job, worker):
        async def renew():
            while True:
                await asyncio.sleep(25)
                if not await self.store.heartbeat_job(job["_id"], worker):
                    raise LostLease

        job_task = asyncio.create_task(self.handle(job))
        heartbeat = asyncio.create_task(renew())
        try:
            done, _ = await asyncio.wait(
                [job_task, heartbeat], return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                await task
            await self.store.finish_job(job["_id"], worker)
        except LostLease:
            pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            delay = float(
                getattr(exc, "retry_after", 0)
                or min(600, 3 ** min(job.get("attempts", 1), 6))
            )
            await self.store.retry_job(
                job["_id"], worker, type(exc).__name__, delay + 1
            )
            log.warning(
                "Job %s %s encountered %s", job["_id"], job["kind"], type(exc).__name__
            )
        finally:
            for task in (job_task, heartbeat):
                task.cancel()
            await asyncio.gather(job_task, heartbeat, return_exceptions=True)

    async def handle(self, job):
        p, kind = job["payload"], job["kind"]
        if kind == "index":
            return await self.indexer.run_job(job)
        if kind == "delete":
            try:
                await self.bot.delete_message(p["chat_id"], p["message_id"])
            except (TelegramBadRequest, TelegramForbiddenError):
                await self.checkpoint(
                    job, outcome="message already gone or deletion forbidden"
                )
            return
        if kind == "curation":
            from .curation import run_job

            return await run_job(job, self.store)
        if kind == "publish":
            from .providers import publish

            return await publish(job, self.store)
        if kind in {"announcement", "digest"}:
            from .alerts import send_public

            return await send_public(job, self.store, self.delivery, self.bot)
        if kind == "release":
            from .alerts import send_release

            return await send_release(job, self.store, self.delivery, self.bot)
        if kind == "notify":
            try:
                await self.bot.send_message(
                    p["chat_id"], p["text"][:4000], parse_mode=None
                )
            except TelegramForbiddenError:
                await self.checkpoint(job, outcome="recipient unavailable")
            return
        if kind == "broadcast":
            return await self.broadcast(job)
        if kind == "batch":
            from .delivery import DeliveryError

            for pos, file_id in enumerate(
                p["file_ids"][p.get("cursor", 0) :], p.get("cursor", 0)
            ):
                await self.checkpoint(job, cursor=pos)
                try:
                    await self.delivery.send(
                        p["chat_id"], file_id, group_id=p.get("source_chat")
                    )
                except DeliveryError as exc:
                    if exc.retry_after:
                        raise
                    await self.checkpoint(job, skipped_reason=str(exc)[:160])
                await self.checkpoint(job, cursor=pos + 1)
                await asyncio.sleep(0.15)
            return
        if kind in {"export", "backup"}:
            return await self.export(job)
        if kind == "forward":
            return await self.forward(job)
        raise ValueError("Unknown job kind")

    async def broadcast(self, job):
        p = job["payload"]
        if p.get("actor_id") not in self.settings.admins:
            raise ValueError("Owner required")
        target = p.get("target", "users")
        if target not in {"users", "groups"}:
            raise ValueError("Unknown broadcast audience")
        groups = target == "groups"
        recipients = self.store.db.settings if groups else self.store.db.users
        eligible = (
            {"enabled": True, "broadcast_opt_in": True}
            if groups
            else {"banned": {"$ne": True}, "blocked": {"$ne": True}}
        )
        cursor = int(p.get("cursor", -(2**63) if groups else 0))

        # A dropped response can follow a successful send. Resume beyond the
        # ambiguous recipient and retain its ID for manual reconciliation.
        pending = p.get("in_flight")
        if pending is not None and pending > cursor:
            cursor = pending
            await self.checkpoint(
                job, cursor=cursor, in_flight=None, last_uncertain_recipient=cursor
            )

        while True:
            interval = {"$gt": cursor}
            if groups:
                interval["$lt"] = 0
            rows = (
                await recipients.find({**eligible, "_id": interval}, {"_id": 1})
                .sort("_id", 1)
                .limit(50)
                .to_list(50)
            )
            if not rows:
                return
            for row in rows:
                recipient = row["_id"]
                if await recipients.find_one(
                    {**eligible, "_id": recipient}, {"_id": 1}
                ):
                    await self.checkpoint(job, in_flight=recipient)
                    try:
                        await self.bot.copy_message(
                            recipient, p["source_chat"], p["message_id"]
                        )
                    except TelegramRetryAfter:
                        # Telegram explicitly rejected the send; retry this recipient after the wait.
                        await self.checkpoint(job, in_flight=None)
                        raise
                    except (
                        TelegramForbiddenError,
                        TelegramBadRequest,
                        TelegramMigrateToChat,
                    ) as exc:
                        if (
                            isinstance(exc, TelegramBadRequest)
                            and "chat not found" not in exc.message.lower()
                        ):
                            # A missing source message is a job failure, not a recipient failure.
                            await self.checkpoint(job, in_flight=None)
                            raise
                        field = (
                            {"broadcast_opt_in": False} if groups else {"blocked": True}
                        )
                        await recipients.update_one({"_id": recipient}, {"$set": field})
                cursor = recipient
                await self.checkpoint(job, cursor=cursor, in_flight=None)
                await asyncio.sleep(0.08)

    async def forward(self, job):
        p = job["payload"]
        if p["actor_id"] not in self.settings.admins:
            raise ValueError("Owner required")
        for mid in range(max(p["start"], p.get("cursor", 0) + 1), p["end"] + 1):
            src = await self.store.db.sources.find_one(
                {"_id": p["source_chat"], "active": True}
            )
            if not src:
                raise ValueError("Source disabled")
            await self.checkpoint(job, cursor=mid - 1)
            try:
                await self.bot.forward_message(p["chat_id"], p["source_chat"], mid)
            except TelegramBadRequest as exc:
                if "message to forward not found" not in exc.message.lower():
                    raise
            await self.checkpoint(job, cursor=mid)
            await asyncio.sleep(0.15)

    async def export(self, job):
        p = job["payload"]
        if p["actor_id"] not in self.settings.admins:
            raise ValueError("Owner required")
        target = p["actor_id"]
        with TemporaryDirectory(prefix="wovie-export-") as tmp:
            budget = min(UPLOAD_LIMIT, shutil.disk_usage(tmp).free - 16 * 1048576)
            if budget < 65536:
                raise ValueError("Insufficient temporary disk space for export")
            if job["kind"] == "backup":
                from .backup import export_archive

                if not self.settings.backup_key:
                    raise ValueError("BACKUP_KEY required")
                path = Path(tmp) / "wovie.encrypted"
                await export_archive(
                    self.store, path, self.settings.backup_key, max_bytes=budget
                )
                target = self.settings.backup_chat or target
            else:
                path = Path(tmp) / "wovie-catalog.csv"
                fields = [
                    "_id",
                    "source_chat",
                    "message_id",
                    "filename",
                    "file_size",
                    "title",
                    "year",
                    "quality",
                ]
                with path.open("w", newline="", encoding="utf-8") as fp:
                    out = csv.writer(fp)
                    out.writerow(fields)
                    for db in self.store.catalogs:
                        async for row in db.files.find(
                            {"deleted_at": None}, fields
                        ).batch_size(32):
                            values = [row.get(key, "") for key in fields]
                            out.writerow(
                                [
                                    "'" + v
                                    if isinstance(v, str)
                                    and v.startswith(("=", "+", "-", "@", "\t", "\r"))
                                    else v
                                    for v in values
                                ]
                            )
                            if fp.tell() > budget:
                                raise ValueError("Export exceeds Bot API upload budget")
            await self.checkpoint(job, bytes=path.stat().st_size)
            await self.bot.send_document(
                target,
                FSInputFile(path),
                caption=f"Wovie {job['kind']} • {datetime.now(UTC):%Y-%m-%d %H:%M UTC}",
            )
