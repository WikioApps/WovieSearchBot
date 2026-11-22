"""Optional Telegram Mini App and authenticated HTTP range streaming worker."""

from __future__ import annotations

import asyncio
import base64
from collections import OrderedDict
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import secrets
import shutil
import tempfile
import time
from urllib.parse import quote, urlsplit

from aiogram.types import FSInputFile
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.utils.web_app import safe_parse_webapp_init_data
from aiohttp import BodyPartReader, web
from cryptography.fernet import Fernet, InvalidToken
from pymongo.errors import DuplicateKeyError
from telethon import TelegramClient
from telethon.sessions import StringSession

from .config import Settings, number, values
from .delivery import Delivery, DeliveryError
from .indexer import Indexer
from .store import Store

LOG = logging.getLogger(__name__)
COOKIE = "__Host-wovie"
CHUNK = 128 * 1024
FILE_ID = re.compile(r"[a-f0-9]{32}\Z")
INLINE_TYPES = {
    "video/mp4",
    "video/webm",
    "audio/mpeg",
    "audio/mp4",
    "audio/ogg",
    "audio/wav",
}
HTML = Path(__file__).with_name("static") / "catalog.html"


class Rejected(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        super().__init__(message)


def byte_range(header: str | None, size: int) -> tuple[int, int]:
    if not header:
        return 0, size - 1
    match = re.fullmatch(r"bytes=(\d{0,20})-(\d{0,20})", header)
    if match:
        low, high = match.groups()
        if low:
            start, end = int(low), min(int(high), size - 1) if high else size - 1
        elif high:
            start, end = max(0, size - int(high)), size - 1
        else:
            start, end = 0, -1
        if 0 <= start <= end < size:
            return start, end
    raise web.HTTPRequestRangeNotSatisfiable(
        headers={"Content-Range": f"bytes */{size}"}
    )


@asynccontextmanager
async def capacity(semaphore):
    try:
        await asyncio.wait_for(semaphore.acquire(), timeout=0.15)
    except TimeoutError:
        raise Rejected(429, "All slots are busy. Try again shortly.") from None
    try:
        yield
    finally:
        semaphore.release()


class Catalog:
    def __init__(self, settings):
        self.settings = settings
        self.bot = Bot(settings.bot_token)
        self.store = Store(settings)
        self.delivery = Delivery(self.bot, self.store, settings)
        url = urlsplit(os.getenv("WEB_PUBLIC_URL", ""))
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.path not in ("", "/")
            or url.query
            or url.fragment
        ):
            raise ValueError("WEB_PUBLIC_URL must be a dedicated HTTPS origin")
        self.origin = f"https://{url.netloc}"
        self.sources = tuple(dict.fromkeys(int(x) for x in values("WEB_SOURCE_IDS")))
        if not 1 <= len(self.sources) <= 16 or any(x >= 0 for x in self.sources):
            raise ValueError(
                "WEB_SOURCE_IDS needs 1–16 authorized channel or group IDs"
            )
        self.auth = Fernet(
            base64.urlsafe_b64encode(
                hashlib.sha256(
                    b"wovie-mini-app-v1\x00" + settings.bot_token.encode()
                ).digest()
            )
        )
        self.api_slots = asyncio.Semaphore(8)
        self.upload_slot = asyncio.Semaphore(1)
        self.upload_limit = number("WEB_UPLOAD_MIB", 0, 0, 48) * 1024 * 1024
        self.stream_slots = asyncio.Semaphore(number("WEB_MAX_STREAMS", 1, 1, 16))
        self.speed = number(
            "WEB_BYTES_PER_SECOND", 2 * 1024 * 1024, CHUNK, 64 * 1024 * 1024
        )
        self.rate = OrderedDict()
        self.session = None
        self.peers = {}
        self.username = ""

    def token(self, data: list) -> str:
        return self.auth.encrypt(
            json.dumps(data, separators=(",", ":")).encode()
        ).decode()

    def open_token(self, token: str, purpose: str, ttl: int) -> list:
        try:
            data = json.loads(self.auth.decrypt(token.encode(), ttl=ttl))
            if not isinstance(data, list) or data[0] != purpose:
                raise ValueError
            return data
        except (ValueError, IndexError, TypeError, InvalidToken):
            raise Rejected(401, "Your session expired. Open /app again.") from None

    def user(self, request) -> tuple[int, str]:
        _, uid, nonce = self.open_token(
            request.cookies.get(COOKIE, ""), "session", 3600
        )
        return uid, nonce

    def throttle(self, uid: int):
        # Process-local limit on authenticated metadata and range requests.
        minute = int(time.monotonic() // 60)
        previous, count = self.rate.pop(uid, (-1, 0))
        count = count + 1 if previous == minute else 1
        self.rate[uid] = minute, count
        if len(self.rate) > 8192:
            self.rate.popitem(last=False)
        if count > max(30, self.settings.rate_limit * 3):
            raise Rejected(429, "Too many requests. Try again in a minute.")

    async def check(self, user):
        ok, reason = await self.delivery.allowed(user)
        if not ok:
            raise Rejected(403, reason or "Access is restricted.")

    async def member(self, uid: int, source: int) -> bool:
        try:
            member = await self.bot.get_chat_member(source, uid)
        except TelegramAPIError:
            return False
        return member.status in {"creator", "administrator", "member"} or bool(
            getattr(member, "is_member", False)
        )

    async def visible(self, uid: int) -> list[int]:
        rows = self.store.db.sources.find(
            {"_id": {"$in": list(self.sources)}, "active": True}
        )
        result = []
        async for source in rows:
            if await self.delivery.can_access_source(uid, source) and await self.member(
                uid, source["_id"]
            ):
                result.append(source["_id"])
        return result

    async def file(self, uid, file_id):
        if not isinstance(file_id, str) or not FILE_ID.fullmatch(file_id):
            raise Rejected(400, "Invalid file identifier.")
        try:
            row = await self.delivery.check_file(uid, file_id)
        except DeliveryError:
            raise Rejected(404, "File unavailable.") from None
        if row["source_chat"] not in self.sources or not await self.member(
            uid, row["source_chat"]
        ):
            raise Rejected(404, "File unavailable.")
        return row

    async def login(self, body):
        data = body.get("init_data")
        if not isinstance(data, str) or len(data) > 8192:
            raise Rejected(401, "Open the catalog through Telegram.")
        try:
            verified = safe_parse_webapp_init_data(self.settings.bot_token, data)
            if (
                not verified.user
                or not -30 <= time.time() - verified.auth_date.timestamp() <= 300
            ):
                raise ValueError
        except (ValueError, TypeError, UnicodeError):
            raise Rejected(
                401, "Telegram session validation failed. Reopen /app."
            ) from None
        uid = verified.user.id
        self.throttle(uid)
        await self.check(uid)
        session = self.token(["session", uid, secrets.token_urlsafe(18)])
        response = web.json_response(
            {
                "admin": uid in self.settings.admins,
                "brand": self.settings.bot_name,
                "stream": self.session is not None,
                "upload_limit": min(
                    self.upload_limit, self.settings.max_file_size or self.upload_limit
                )
                if uid in self.settings.admins
                else 0,
                "sources": await self.visible(uid)
                if uid in self.settings.admins
                else [],
            }
        )
        response.headers.add(
            "Set-Cookie",
            f"{COOKIE}={session}; Path=/; Max-Age=3600; HttpOnly; Secure; SameSite=None; Partitioned",
        )
        return response

    async def api(self, request):
        if (
            request.headers.get("Origin") != self.origin
            or request.content_type != "application/json"
        ):
            raise Rejected(403, "Open the catalog from its configured HTTPS address.")
        async with capacity(self.api_slots):
            try:
                body = await request.json()
                if not isinstance(body, dict):
                    raise ValueError
            except ValueError:
                raise Rejected(400, "Expected a JSON object.") from None
            action = request.match_info["action"]
            if action == "login":
                return await self.login(body)
            uid, nonce = self.user(request)
            self.throttle(uid)
            await self.check(uid)
            if action == "search":
                query, page = body.get("q"), body.get("page", 0)
                if (
                    not isinstance(query, str)
                    or not query.strip()
                    or type(page) is not int
                    or not 0 <= page < 100
                ):
                    raise Rejected(400, "Enter a title or release filter.")
                visible = await self.visible(uid)
                try:
                    rows, more = (
                        await self.store.search(query, page, source_ids=visible)
                        if visible
                        else ([], False)
                    )
                except ValueError as exc:
                    raise Rejected(400, str(exc)) from None
                result = [
                    {
                        "id": r["_id"],
                        "title": r["title"],
                        "filename": r["filename"],
                        "quality": r.get("quality"),
                        "size": r.get("file_size", 0),
                    }
                    for r in rows
                ]
                return web.json_response({"files": result, "more": more})
            if action == "stats" and uid in self.settings.admins:
                return web.json_response({"stats": await self.store.stats()})
            if action in {"hidden", "restore"} and uid in self.settings.admins:
                query = {
                    "source_chat": {"$in": await self.visible(uid)},
                    "deleted_at": {"$ne": None},
                    "deletion_reason": "web_hide",
                }
                if action == "restore":
                    fid = body.get("id")
                    if (
                        not isinstance(fid, str)
                        or not FILE_ID.fullmatch(fid)
                        or body.get("confirm") != fid
                    ):
                        raise Rejected(400, "Confirm a single catalog entry.")
                    for db in self.store.catalogs:
                        changed = await db.files.update_one(
                            {**query, "_id": fid},
                            {
                                "$unset": {
                                    "deleted_at": "",
                                    "deleted_by": "",
                                    "deletion_reason": "",
                                }
                            },
                        )
                        if changed.modified_count:
                            return web.json_response({"restored": True})
                    raise Rejected(404, "Entry is unavailable.")
                hidden = []
                for db in self.store.catalogs:
                    hidden.extend(
                        await db.files.find(query, {"filename": 1, "deleted_at": 1})
                        .sort("deleted_at", -1)
                        .limit(20)
                        .to_list(20)
                    )
                latest = sorted(hidden, key=lambda r: r["deleted_at"], reverse=True)[
                    :20
                ]
                return web.json_response(
                    {"files": [{"id": r["_id"], "name": r["filename"]} for r in latest]}
                )
            row = await self.file(uid, body.get("id"))
            if action == "link":
                link = await self.store.create_link(
                    [row["_id"]],
                    owner_id=uid,
                    expires_at=datetime.now(UTC) + timedelta(hours=1),
                )
                return web.json_response(
                    {"url": f"https://t.me/{self.username}?start=link_{link}"}
                )
            if action == "ticket" and self.session:
                await self.stream_allowed(uid, row)
                return web.json_response(
                    {"url": "/media/" + self.token(["file", uid, nonce, row["_id"]])}
                )
            if (
                action == "hide"
                and uid in self.settings.admins
                and body.get("confirm") == row["_id"]
            ):
                for db in self.store.catalogs:
                    changed = await db.files.update_one(
                        {"_id": row["_id"], "deleted_at": None},
                        {
                            "$set": {
                                "deleted_at": datetime.now(UTC),
                                "deleted_by": uid,
                                "deletion_reason": "web_hide",
                            }
                        },
                    )
                    if changed.modified_count:
                        return web.json_response({"hidden": True})
                raise Rejected(404, "File was already hidden.")
            raise Rejected(404, "Unavailable action.")

    async def upload(self, request):
        if (
            request.headers.get("Origin") != self.origin
            or request.content_type != "multipart/form-data"
        ):
            raise Rejected(403, "Use the catalog upload form.")
        uid, _ = self.user(request)
        if not self.upload_limit or uid not in self.settings.admins:
            raise Rejected(403, "Owner uploads are disabled.")
        self.throttle(uid)
        await self.check(uid)
        source = int(request.match_info["source"])
        if source not in self.sources:
            raise Rejected(403, "Source unavailable.")
        size = request.content_length
        limit = min(self.upload_limit, self.settings.max_file_size or self.upload_limit)
        if (
            size is None
            or size > limit + 16384
            or request.headers.get("Content-Encoding")
        ):
            raise Rejected(
                413, "Upload size exceeds the configured limit or is unknown."
            )
        nonce = request.headers.get("X-Upload-ID", "")
        if not re.fullmatch(r"[a-f0-9-]{36}", nonce):
            raise Rejected(400, "Invalid upload request ID.")
        key = f"{uid}:{source}:{nonce}"
        receipts = self.store.db.web_uploads
        async with capacity(self.upload_slot):
            old = await receipts.find_one({"_id": key})
            if old:
                if old.get("file"):
                    return web.json_response({"id": old["file"]})
                raise Rejected(
                    409,
                    "Upload previously submitted. Check the source chat before retrying.",
                )
            if source not in await self.visible(uid):
                raise Rejected(403, "Source unavailable.")
            with tempfile.TemporaryDirectory(prefix="wovie-upload-") as tmp:
                if shutil.disk_usage(tmp).free < size + 32 * 1024 * 1024:
                    raise Rejected(507, "Upload host has insufficient free disk space.")
                path = Path(tmp) / "payload"
                reader = await request.clone(client_max_size=limit + 16384).multipart()
                part = await reader.next()
                if (
                    not isinstance(part, BodyPartReader)
                    or part.name != "file"
                    or not part.filename
                ):
                    raise Rejected(400, "Choose one file.")
                filename = (
                    re.sub(r"[\x00-\x1f\x7f/\\]", "_", part.filename).strip()[:150]
                    or "upload"
                )
                async with asyncio.timeout(120):
                    length = 0
                    with path.open("xb") as out:
                        while chunk := await part.read_chunk(CHUNK):
                            length += len(chunk)
                            if length > limit:
                                raise Rejected(413, "File too large.")
                            out.write(chunk)
                    if not length or await reader.next() is not None:
                        raise Rejected(400, "Choose exactly one nonempty file.")
                active = await self.store.db.sources.find_one(
                    {"_id": source, "active": True}
                )
                global_policy, source_policy = (
                    await self.store.setting(0),
                    await self.store.setting(source),
                )
                if (
                    not await self.delivery.can_access_source(uid, active or {})
                    or not await self.member(uid, source)
                    or active.get("protect_content")
                    or global_policy.get(
                        "protect_content", self.settings.protect_content
                    )
                    or source_policy.get("protect_content")
                    or (await self.bot.get_chat(source)).has_protected_content
                ):
                    raise Rejected(
                        403, "Source access or protection prevents this upload."
                    )
                try:
                    await receipts.insert_one(
                        {
                            "_id": key,
                            "created_at": datetime.now(UTC),
                            "status": "submitting",
                        }
                    )
                except DuplicateKeyError:
                    raise Rejected(409, "Upload already submitted.") from None
                sent = await self.bot.send_document(
                    source, FSInputFile(path, filename=filename), request_timeout=180
                )
                await receipts.update_one(
                    {"_id": key},
                    {"$set": {"message_id": sent.message_id, "status": "sent"}},
                )
                indexer = Indexer(self.settings, self.store, self.bot)
                indexer.bot_id = self.bot.id
                fid = await indexer.ingest_bot_message(sent)
                if not fid:
                    raise Rejected(
                        409,
                        "Telegram received the file. Check source configuration before reindexing.",
                    )
                await receipts.update_one(
                    {"_id": key}, {"$set": {"file": fid, "status": "indexed"}}
                )
                return web.json_response({"id": fid})

    async def stream_allowed(self, uid, row):
        settings = await self.store.setting(row["source_chat"])
        global_settings = await self.store.setting(0)
        source = await self.store.db.sources.find_one(
            {"_id": row["source_chat"], "active": True}
        )
        if (
            not source
            or global_settings.get("protect_content", self.settings.protect_content)
            or settings.get("protect_content")
            or source.get("protect_content")
            or row.get("protected")
            or row.get("has_protected_content")
        ):
            raise Rejected(403, "Streaming is disabled for this source.")
        if (
            global_settings.get("daily_limit", self.settings.daily_limit)
            and uid not in self.settings.admins
        ):
            user = await self.store.db.users.find_one(
                {"_id": uid}, {"premium_until": 1}
            )
            premium = (user or {}).get("premium_until")
            if not premium or premium <= datetime.now(UTC):
                raise Rejected(403, "Use Telegram delivery with your daily allowance.")

    async def media(self, request):
        uid, nonce = self.user(request)
        _, bound_uid, bound_nonce, file_id = self.open_token(
            request.match_info["ticket"], "file", 900
        )
        if uid != bound_uid or nonce != bound_nonce or self.session is None:
            raise Rejected(403, "The media link belongs to another session.")
        self.throttle(uid)
        async with capacity(self.stream_slots):
            row = await self.file(uid, file_id)
            await self.stream_allowed(uid, row)
            source = row["source_chat"]
            if source not in self.peers:
                raise Rejected(404, "The streaming account cannot access this source.")
            chat = await self.bot.get_chat(source)
            if chat.has_protected_content:
                raise Rejected(403, "Content protection is enabled on this chat.")
            msg = await self.session.get_messages(
                self.peers[source], ids=row["message_id"]
            )
            if not msg or not msg.document or msg.noforwards:
                raise Rejected(404, "This media is unavailable.")
            size = msg.document.size
            start, end = byte_range(request.headers.get("Range"), size)
            total = end - start + 1
            mime = msg.document.mime_type
            inline = mime in INLINE_TYPES and request.query.get("download") != "1"
            headers = {
                "Content-Length": str(total),
                "Content-Type": mime if inline else "application/octet-stream",
                "Accept-Ranges": "bytes",
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "Content-Security-Policy": "sandbox; default-src 'none'",
                "Content-Disposition": ("inline" if inline else "attachment")
                + "; filename*=UTF-8''"
                + quote(row["filename"][:200], safe=""),
            }
            partial = request.headers.get("Range") is not None
            if partial:
                headers["Content-Range"] = f"bytes {start}-{end}/{size}"
            response = web.StreamResponse(
                status=206 if partial else 200, headers=headers
            )
            await response.prepare(request)
            if request.method == "HEAD" or not total:
                return response
            offset = start - start % CHUNK
            skip = start - offset
            chunks = self.session.iter_download(
                msg,
                offset=offset,
                request_size=CHUNK,
                limit=(skip + total + CHUNK - 1) // CHUNK,
            )
            remaining = total
            began = time.monotonic()
            try:
                async for chunk in chunks:
                    data = bytes(chunk[skip : skip + remaining])
                    skip = 0
                    await response.write(data)
                    remaining -= len(data)
                    if not remaining:
                        break
                    wait = (total - remaining) / self.speed - (time.monotonic() - began)
                    if wait > 0:
                        await asyncio.sleep(wait)
                if remaining:
                    raise ConnectionError("Telegram stream ended early")
                await response.write_eof()
            except Exception:
                if request.transport:
                    request.transport.close()
                raise
            finally:
                await chunks.close()
            return response

    async def lifecycle(self, app):
        try:
            await self.store.connect()
            self.username = (await self.bot.me()).username
            string = os.getenv("WEB_SESSION_STRING", "").strip()
            if string:
                if (
                    string
                    in {
                        self.settings.session_string,
                        os.getenv("ARCHIVE_SESSION_STRING"),
                    }
                    or not self.settings.api_id
                    or not self.settings.api_hash
                ):
                    raise ValueError(
                        "Use a separate WEB_SESSION_STRING with API_ID and API_HASH"
                    )
                self.session = TelegramClient(
                    StringSession(string),
                    self.settings.api_id,
                    self.settings.api_hash,
                    receive_updates=False,
                    flood_sleep_threshold=0,
                )
                await self.session.connect()
                account = await self.session.get_me()
                if not account or account.bot:
                    raise ValueError(
                        "WEB_SESSION_STRING needs an authorized user account"
                    )
                async for dialog in self.session.iter_dialogs():
                    if dialog.id in self.sources:
                        self.peers[dialog.id] = dialog.input_entity
                    if len(self.peers) == len(self.sources):
                        break
            yield
        finally:
            if self.session:
                await self.session.disconnect()
            await self.bot.session.close()
            await self.store.close()


@web.middleware
async def errors(request, handler):
    try:
        response = await handler(request)
    except Rejected as exc:
        response = web.json_response({"error": str(exc)}, status=exc.status)
    except web.HTTPException:
        raise
    except (ConnectionResetError, BrokenPipeError):
        raise
    except Exception as exc:
        LOG.warning("Web request failed (%s)", type(exc).__name__)
        response = web.json_response(
            {"error": "Service unavailable. Try again shortly."}, status=503
        )
    response.headers.setdefault("Cache-Control", "no-store")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    return response


def application():
    settings = Settings.from_env()
    service = Catalog(settings)
    app = web.Application(middlewares=[errors], client_max_size=12 * 1024)
    app.cleanup_ctx.append(service.lifecycle)

    async def page(_):
        nonce = secrets.token_urlsafe(16)
        headers = {
            "Content-Security-Policy": (
                "default-src 'none'; frame-ancestors 'self' https://web.telegram.org https://*.telegram.org; "
                f"script-src 'nonce-{nonce}' https://telegram.org; style-src 'nonce-{nonce}'; "
                "connect-src 'self'; media-src 'self'; base-uri 'none'; form-action 'none'"
            )
        }
        return web.Response(
            text=HTML.read_text().replace("__NONCE__", nonce),
            content_type="text/html",
            headers=headers,
        )

    app.router.add_get("/", page)
    app.router.add_post(
        "/api/{action:login|search|link|ticket|stats|hide|hidden|restore}", service.api
    )
    app.router.add_post(r"/admin/upload/{source:-[0-9]+}", service.upload)
    app.router.add_get("/media/{ticket}", service.media)
    return app


if __name__ == "__main__":
    site = application()
    web.run_app(
        site,
        host=os.getenv("WEB_HOST", "127.0.0.1"),
        port=number("WEB_PORT", 8080, 1, 65535),
        access_log=None,
        handler_cancellation=True,
        shutdown_timeout=15,
    )
