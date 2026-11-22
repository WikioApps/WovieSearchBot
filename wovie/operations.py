"""Owner diagnostics, safe audit receipts and optional timed verification links."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
from html import escape
import json
from pathlib import Path
import re
import secrets
import shutil
import time
from urllib.parse import unquote, urlsplit

from aiogram import BaseMiddleware, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BufferedInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
import httpx
from pymongo import ReturnDocument

STARTED = time.monotonic()
ADMIN_COMMANDS = frozenset(
    {
        "source",
        "index",
        "pause",
        "resume",
        "cancel",
        "group",
        "set",
        "filter",
        "delfilter",
        "broadcast",
        "batch",
        "delete",
        "restore",
        "purge",
        "ban",
        "unban",
        "grant",
        "revoke",
        "buttons",
        "filterbutton",
        "redeemcode",
        "requestdone",
        "backup",
        "export",
        "joinmode",
        "referralconfig",
        "sponsor",
        "modban",
        "modunban",
        "mute",
        "unmute",
        "pin",
        "unpin",
        "retryupdate",
        "forward",
        "operator",
        "groupbroadcast",
    }
)


class AdminAuditMiddleware(BaseMiddleware):
    """Record dispatch outcomes without persisting command arguments or message text."""

    def __init__(self, settings, store):
        self.settings, self.store = settings, store

    async def __call__(self, handler, event: Message, data):
        match = re.match(
            r"^/([a-z]+)(?:@\w+)?(?:\s|$)", event.text or event.caption or ""
        )
        if not match or match[1] not in ADMIN_COMMANDS:
            return await handler(event, data)
        identity = f"command:{event.bot.id}:{event.chat.id}:{event.message_id}"
        await self.store.db.audit.update_one(
            {"_id": identity},
            {
                "$setOnInsert": {
                    "action": match[1],
                    "actor_id": event.from_user.id if event.from_user else None,
                    "chat_id": event.chat.id,
                    "created_at": datetime.now(UTC),
                    "status": "started",
                }
            },
            upsert=True,
        )
        try:
            result = await handler(event, data)
        except Exception as exc:
            await self.store.db.audit.update_one(
                {"_id": identity},
                {
                    "$set": {
                        "status": "handler_failed",
                        "error": type(exc).__name__,
                        "updated_at": datetime.now(UTC),
                    }
                },
            )
            raise
        await self.store.db.audit.update_one(
            {"_id": identity},
            {"$set": {"status": "handler_returned", "updated_at": datetime.now(UTC)}},
        )
        return result


async def verification_allowed(settings, store, user_id: int) -> bool:
    if (
        not getattr(settings, "verification_required", False)
        or user_id in settings.admins
    ):
        return True
    user = await store.db.users.find_one({"_id": user_id}) or {}
    now = datetime.now(UTC)
    return any(
        isinstance(user.get(key), datetime) and user[key] > now
        for key in ("premium_until", "verified_until")
    )


async def shorten(settings, destination: str, secret_token: str) -> str:
    """Common shortener JSON protocol; never fall back to the original destination."""
    endpoint = getattr(settings, "shortener_api_url", None)
    api_key = getattr(settings, "shortener_api_key", None)
    if not endpoint or not api_key:
        raise ValueError("Verification provider is not configured.")
    async with httpx.AsyncClient(
        timeout=12, follow_redirects=False, trust_env=False
    ) as client:
        async with client.stream(
            "GET",
            endpoint,
            params={"api": api_key, "url": destination},
            headers={"Accept": "application/json"},
        ) as response:
            if response.status_code != 200:
                raise ValueError("Verification provider is temporarily unavailable.")
            body = bytearray()
            async for part in response.aiter_bytes():
                body.extend(part)
                if len(body) > 16384:
                    raise ValueError(
                        "Verification provider returned an invalid response."
                    )
    try:
        result = json.loads(body)
        link = next(
            (
                result.get(key)
                for key in ("shortenedUrl", "shortened_url", "short_url")
                if result.get(key)
            ),
            None,
        )
        parsed = urlsplit(link) if isinstance(link, str) else None
        expanded = unquote(unquote(link)) if isinstance(link, str) else ""
    except (ValueError, AttributeError, TypeError):
        raise ValueError(
            "Verification provider returned an invalid response."
        ) from None
    if (
        not parsed
        or parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or len(link) > 2048
        or secret_token in expanded
        or parsed.hostname.lower() in {"t.me", "telegram.me"}
    ):
        raise ValueError("Verification provider did not return a valid shortened link.")
    return link


def build_router(settings, store, delivery) -> Router:
    router = Router(name="operations")

    async def owner(message: Message) -> bool:
        if (
            message.chat.type != "private"
            or not message.from_user
            or message.from_user.id not in settings.admins
        ):
            await message.answer("Use an authorized owner account in a private chat.")
            return False
        return True

    @router.message(Command("health"))
    async def health(message: Message):
        if message.chat.type != "private" or not message.from_user:
            return await message.answer("Open /health in private chat.")
        if message.from_user.id not in settings.admins:
            operator = await store.db.users.find_one(
                {
                    "_id": message.from_user.id,
                    "role": "operator",
                    "banned": {"$ne": True},
                }
            )
            if not operator:
                return await message.answer(
                    "This diagnostic command requires an owner or assigned operator."
                )
        started = time.monotonic()
        await store.db.command("ping")
        latency = (time.monotonic() - started) * 1000
        memory = "unavailable on this platform"
        try:
            status = Path("/proc/self/status").read_text()
            match = re.search(r"^VmRSS:\s+(\d+)\s+kB", status, re.M)
            if match:
                memory = f"{int(match[1]) / 1024:.1f} MiB"
        except OSError:
            pass
        disk = shutil.disk_usage(Path.cwd())
        jobs = await store.db.jobs.aggregate(
            [{"$group": {"_id": "$status", "count": {"$sum": 1}}}]
        )
        counts = await jobs.to_list(length=20)
        backup = await store.db.jobs.find_one(
            {"kind": "backup", "status": "done"}, sort=[("updated_at", -1)]
        )
        age = "No successful backup job recorded"
        if backup:
            stamp = backup.get("updated_at") or backup.get("created_at")
            age = f"{max(0, int((datetime.now(UTC) - stamp).total_seconds() / 60))} minutes"
        lines = [
            "<b>🩺 Wovie · Health</b>",
            f"Process uptime: {int(time.monotonic() - STARTED)} seconds",
            f"Current process RAM: {memory}",
            f"Filesystem free: {disk.free / 1048576:.1f} / {disk.total / 1048576:.1f} MiB",
            f"Primary database ping: {latency:.1f} ms",
            f"Latest completed backup job: {age}",
            "Jobs: "
            + ", ".join(f"{escape(str(row['_id']))}={row['count']}" for row in counts),
        ]
        lines.append(
            "Backup completion is not proof of a successful restore. Keep independent recovery copies."
        )
        await message.answer("\n".join(lines), parse_mode="HTML")

    @router.message(Command("inbox"))
    async def inbox(message: Message):
        if not await owner(message):
            return
        raw = (message.text or "").partition(" ")[2].strip()
        if raw and (not raw.isdecimal() or not 1 <= int(raw) <= 100000):
            return await message.answer("Usage: /inbox [PAGE]")
        page = int(raw or 1)
        rows = (
            await store.db.inbox.find(
                {"bot_id": message.bot.id, "status": {"$in": ["failed", "paused"]}},
                {"seq": 1, "status": 1, "attempts": 1, "error": 1},
            )
            .sort("seq", -1)
            .skip((page - 1) * 15)
            .limit(16)
            .to_list(16)
        )
        lines = [f"<b>📮 Failed/paused updates · page {page}</b>"]
        for row in rows[:15]:
            lines.append(
                f"<code>{row['seq']}</code> · {escape(row['status'])} · attempts {row.get('attempts', 0)} · {escape(str(row.get('error', ''))[:80])}"
            )
        if len(rows) > 15:
            lines.append(f"Next: /inbox {page + 1}")
        lines.append(
            "Use /retryupdate SEQ confirm after correcting the cause. External effects may repeat."
        )
        await message.answer("\n".join(lines), parse_mode="HTML")

    @router.message(Command("retryupdate"))
    async def retryupdate(message: Message):
        if not await owner(message):
            return
        parts = (message.text or "").split()
        if len(parts) != 3 or not parts[1].isdecimal() or parts[2] != "confirm":
            return await message.answer(
                "Usage: /retryupdate SEQ confirm. Replays one retained failed/paused update; a prior external send may repeat."
            )
        changed = await store.db.inbox.update_one(
            {
                "bot_id": message.bot.id,
                "seq": int(parts[1]),
                "status": {"$in": ["failed", "paused"]},
                "update": {"$exists": True},
            },
            {
                "$set": {"status": "pending", "attempts": 0, "next": datetime.now(UTC)},
                "$unset": {"error": "", "expires_at": ""},
            },
        )
        await message.answer(
            "Update queued for replay."
            if changed.modified_count
            else "Retained failed/paused update not found."
        )

    @router.message(Command("audit"))
    async def audit(message: Message):
        if not await owner(message):
            return
        raw = (message.text or "").partition(" ")[2].strip()
        export = raw == "export"
        if not export and raw and (not raw.isdecimal() or not 1 <= int(raw) <= 100000):
            return await message.answer(
                "Usage: /audit [PAGE] or /audit export (latest 500 receipts)."
            )
        page = int(raw or 1) if not export else 1
        count = 500 if export else 15
        rows = (
            await store.db.audit.find(
                {},
                {
                    "action": 1,
                    "actor_id": 1,
                    "chat_id": 1,
                    "status": 1,
                    "created_at": 1,
                    "updated_at": 1,
                },
            )
            .sort("created_at", -1)
            .skip((page - 1) * count)
            .limit(count + 1)
            .to_list(count + 1)
        )
        if export:
            data = "\n".join(
                json.dumps(row, default=str, ensure_ascii=False) for row in rows[:500]
            ).encode()
            return await message.answer_document(
                BufferedInputFile(data, filename="wovie-audit.jsonl"),
                caption="Latest 500 audit receipts. Command arguments and credentials are omitted.",
            )
        lines = [f"<b>🧾 Audit · page {page}</b>"]
        for row in rows[:15]:
            lines.append(
                f"{escape(str(row.get('created_at', ''))[:19])} · {escape(str(row.get('action', '?')))} · "
                f"actor {row.get('actor_id', '?')} · {escape(str(row.get('status', 'recorded')))}"
            )
        if len(rows) > 15:
            lines.append(f"Next: /audit {page + 1}")
        lines.append(
            "handler_returned records dispatch completion, including a permission-denied response; it does not certify a mutation."
        )
        await message.answer("\n".join(lines), parse_mode="HTML")

    @router.message(Command("verify"))
    async def verify(message: Message):
        if not message.from_user or message.chat.type != "private":
            return await message.answer("Open /verify in a private chat with me.")
        allowed, reason = await delivery.allowed(message.from_user.id)
        if not allowed:
            return await message.answer(escape(reason))
        if await verification_allowed(settings, store, message.from_user.id):
            return await message.answer(
                "Verification is not currently required for your account."
            )
        uid = message.from_user.id
        now = datetime.now(UTC)
        recent = await store.db.verifications.find_one(
            {
                "user_id": uid,
                "issued_at": {"$gt": now - timedelta(seconds=30)},
                "status": {"$in": ["issuing", "ready"]},
            }
        )
        if recent:
            return await message.answer(
                "Use your recent verification link or wait 30 seconds before requesting another."
            )
        token = secrets.token_hex(24)
        identity = hashlib.sha256(token.encode()).hexdigest()
        await store.db.verifications.insert_one(
            {
                "_id": identity,
                "user_id": uid,
                "status": "issuing",
                "issued_at": now,
                "not_before": now
                + timedelta(seconds=getattr(settings, "verification_min_seconds", 10)),
                "expires_at": now + timedelta(minutes=15),
            }
        )
        try:
            me = await message.bot.get_me()
            short = await shorten(
                settings, f"https://t.me/{me.username}?start=verify_{token}", token
            )
        except (ValueError, httpx.HTTPError):
            await store.db.verifications.update_one(
                {"_id": identity}, {"$set": {"status": "failed"}}
            )
            return await message.answer(
                "Verification provider is unavailable. Please retry later or contact the administrator."
            )
        await store.db.verifications.update_one(
            {"_id": identity}, {"$set": {"status": "ready"}}
        )
        await message.answer(
            "<b>🔐 Verification</b>\nOpen the link, complete the provider flow, then return to this bot. "
            "This link belongs to your account and expires in 15 minutes.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="Continue verification ↗", url=short)]
                ]
            ),
        )

    @router.message(
        CommandStart(deep_link=True),
        F.text.regexp(r"^/start(?:@\w+)?\s+verify_[a-f0-9]{48}$"),
    )
    async def verified(message: Message):
        if not message.from_user or message.chat.type != "private":
            return
        allowed, reason = await delivery.allowed(message.from_user.id)
        if not allowed:
            return await message.answer(escape(reason))
        token = (message.text or "").split()[-1][7:]
        identity = hashlib.sha256(token.encode()).hexdigest()
        now = datetime.now(UTC)
        row = await store.db.verifications.find_one_and_update(
            {
                "_id": identity,
                "user_id": message.from_user.id,
                "status": "ready",
                "not_before": {"$lte": now},
                "expires_at": {"$gt": now},
            },
            {
                "$set": {
                    "status": "claimed",
                    "verified_until": now
                    + timedelta(seconds=getattr(settings, "verification_ttl", 43200)),
                }
            },
            return_document=ReturnDocument.AFTER,
        )
        if not row:
            row = await store.db.verifications.find_one(
                {
                    "_id": identity,
                    "user_id": message.from_user.id,
                    "status": {"$in": ["claimed", "redeemed"]},
                    "verified_until": {"$gt": now},
                }
            )
        if not row:
            return await message.answer(
                "This verification link is unavailable, expired, belongs to another account, or was opened too quickly. Wait briefly and try it again, or use /verify."
            )
        await store.db.users.update_one(
            {"_id": message.from_user.id},
            {"$max": {"verified_until": row["verified_until"]}},
            upsert=True,
        )
        await store.db.verifications.update_one(
            {"_id": identity}, {"$set": {"status": "redeemed"}}
        )
        await message.answer(
            "✅ Verification complete. File delivery is unlocked until "
            + row["verified_until"].strftime("%Y-%m-%d %H:%M UTC")
            + "."
        )

    return router
