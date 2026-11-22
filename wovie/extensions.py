"""Optional discovery providers, owner forwarding and companion interfaces."""

from __future__ import annotations

from html import escape
import asyncio
from datetime import UTC, datetime
import hashlib
import time
import os
import re
from urllib.parse import urlsplit

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)
import httpx
from pymongo.errors import DuplicateKeyError


def _url(value):
    parsed = urlsplit(value or "")
    return (
        value
        if parsed.scheme == "https"
        and parsed.hostname
        and not parsed.username
        and not parsed.password
        else None
    )


def build_router(settings, store, delivery, metadata):
    router = Router(name="discovery")
    drive_lock = asyncio.Lock()
    drive_bearer = None
    drive_expiry = 0.0

    async def authorize(m, owner=False):
        if m.chat.type != "private" or not m.from_user:
            await m.answer("Open this command in a private chat with me.")
            return False
        if owner and m.from_user.id not in settings.admins:
            await m.answer("This command is for bot owners.")
            return False
        allowed, reason = await delivery.allowed(m.from_user.id)
        if not allowed:
            await m.answer(escape(reason))
        return allowed

    async def request(m, method, url, *, report_unauthorized=False, **kwargs):
        try:
            async with httpx.AsyncClient(timeout=8, follow_redirects=False) as client:
                async with client.stream(method, url, **kwargs) as response:
                    if response.status_code == 401 and report_unauthorized:
                        raise PermissionError("Provider token expired")
                    response.raise_for_status()
                    buf = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(buf) + len(chunk) > 2_000_000:
                            raise ValueError("Response too large")
                        buf.extend(chunk)
                    import json

                    return json.loads(buf)
        except (ValueError, httpx.HTTPError):
            await m.answer(
                "The provider could not complete this request. Try again later."
            )
            return None

    async def drive_token(m, *, rejected=None):
        nonlocal drive_bearer, drive_expiry
        credentials = {
            k: os.getenv("DRIVE_" + k.upper())
            for k in ("client_id", "client_secret", "refresh_token")
        }
        if not any(credentials.values()):
            return os.getenv("DRIVE_ACCESS_TOKEN")
        if not all(credentials.values()):
            await m.answer(
                "Drive OAuth requires a client ID, client secret and refresh token."
            )
            return None
        async with drive_lock:
            if (
                drive_bearer
                and drive_bearer != rejected
                and time.monotonic() < drive_expiry
            ):
                return drive_bearer
            token = await request(
                m,
                "POST",
                "https://oauth2.googleapis.com/token",
                data={**credentials, "grant_type": "refresh_token"},
            )
            if token is None:
                return None
            if (
                not isinstance(token, dict)
                or not isinstance(token.get("access_token"), str)
                or not token["access_token"]
            ):
                await m.answer(
                    "Google did not return a usable access token. Check the configured OAuth grant."
                )
                return None
            drive_bearer = token["access_token"]
            drive_expiry = time.monotonic() + max(
                0, min(int(token.get("expires_in", 3600)), 86400) - 60
            )
            return drive_bearer

    @router.message(Command("ingest"))
    async def manual(m: Message, command: CommandObject):
        if not await authorize(m, owner=True):
            return
        try:
            target = int((command.args or "").strip())
        except ValueError:
            return await m.answer(
                "Reply to a document, video or audio with /ingest SOURCE_ID."
            )
        original = m.reply_to_message
        media = (
            (original.document or original.video or original.audio)
            if original
            else None
        )
        if not media or original.has_protected_content:
            return await m.answer("Reply to an unprotected media message in this chat.")
        if settings.max_file_size and (media.file_size or 0) > settings.max_file_size:
            return await m.answer("The file exceeds the configured indexing limit.")
        if not await store.db.sources.find_one({"_id": target, "active": True}):
            return await m.answer("Choose an active registered source.")
        target_chat = await m.bot.get_chat(target)
        if target_chat.has_protected_content:
            return await m.answer("This source protects its content.")
        key = f"{m.chat.id}:{m.message_id}"
        previous = await store.db.manual_ingest.find_one({"_id": key})
        if previous:
            mid = previous["message_id"]
        else:
            sent = await m.bot.copy_message(target, m.chat.id, original.message_id)
            mid = sent.message_id
            await store.db.manual_ingest.update_one(
                {"_id": key}, {"$setOnInsert": {"message_id": mid}}, upsert=True
            )
        kind = "video" if original.video else "audio" if original.audio else "document"
        fid = await store.put_file(
            {
                "source_chat": target,
                "message_id": mid,
                "filename": media.file_name or f"{kind}-{mid}",
                "caption": original.caption or "",
                "file_id": media.file_id,
                "file_unique_id": media.file_unique_id,
                "bot_id": m.bot.id,
                "file_size": media.file_size or 0,
                "media_type": kind,
            }
        )
        await m.answer(f"Indexed <code>{fid}</code> in <code>{target}</code>.")

    @router.message(Command("translate"))
    async def translate(m: Message, command: CommandObject):
        if not await authorize(m, owner=True):
            return
        url = _url(os.getenv("LIBRETRANSLATE_URL"))
        if not url:
            return await m.answer("Translation is disabled.")
        target, _, text = (command.args or "").partition(" ")
        if not text or not target.isalpha() or len(target) > 5:
            return await m.answer("Usage: /translate en TEXT")
        data = await request(
            m,
            "POST",
            url.rstrip("/") + "/translate",
            json={
                "q": text[:1000],
                "source": "auto",
                "target": target.lower(),
                "format": "text",
                "api_key": os.getenv("LIBRETRANSLATE_API_KEY", ""),
            },
        )
        if data:
            await m.answer(
                str(data.get("translatedText") or "No translation returned.")[:3500],
                parse_mode=None,
            )

    @router.message(Command("app"))
    async def web(m: Message):
        if not await authorize(m):
            return
        url = _url(os.getenv("WEB_PUBLIC_URL"))
        if not url:
            return await m.answer("The web catalog is disabled. Use /search TITLE.")
        await m.answer(
            "✦ Open the catalog",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="🎬 Browse files", web_app=WebAppInfo(url=url)
                        )
                    ]
                ]
            ),
        )

    @router.message(Command("announce"))
    async def announce(m: Message, command: CommandObject):
        if not await authorize(m, owner=True):
            return
        arg = (command.args or "").strip().lower()
        if arg == "off":
            await store.set_setting(0, "announcement_chat", None)
            return await m.answer("Automatic catalog posts disabled.")
        try:
            cid = int(arg)
            if cid >= 0:
                raise ValueError
        except ValueError:
            return await m.answer(
                "Usage: /announce TARGET_CHANNEL_ID or /announce off."
            )
        from aiogram.exceptions import TelegramAPIError

        try:
            target = await m.bot.get_chat(cid)
            rights = await m.bot.get_chat_member(cid, m.bot.id)
            if target.type != "channel" or not (
                rights.status == "creator"
                or getattr(rights, "can_post_messages", False)
            ):
                return await m.answer("Choose a channel where the bot can post.")
        except TelegramAPIError:
            return await m.answer("I cannot post in that channel.")
        await store.set_setting(0, "announcement_chat", cid)
        await m.answer(
            "New additions from public, unrestricted sources will be announced here."
        )

    @router.message(Command("digest"))
    async def digest(m: Message, command: CommandObject):
        if not await authorize(m):
            return
        choice = (command.args or "").strip().lower()
        if choice not in {"on", "off"}:
            return await m.answer(
                "Use /digest on or /digest off for weekly movie picks."
            )
        if choice == "on" and not metadata.key:
            return await m.answer("Digest needs TMDB_API_KEY.")
        await store.db.digests.update_one(
            {"_id": m.from_user.id}, {"$set": {"enabled": choice == "on"}}, upsert=True
        )
        await m.answer(
            "Weekly picks " + ("enabled." if choice == "on" else "disabled.")
        )

    @router.message(Command("weekly"))
    async def weekly(m: Message):
        if not await authorize(m):
            return
        if not metadata.key:
            return await m.answer("Weekly picks need a TMDB API key.")
        try:
            rows = await metadata.trending()
        except (httpx.HTTPError, RuntimeError):
            return await m.answer("TMDB is unavailable. Try again later.")
        lines = [
            f'• <a href="{escape(x["url"], quote=True)}">{escape(x["title"])}</a> {x["year"]}'
            for x in rows
        ]
        await m.answer(
            "<b>✦ Weekly picks</b>\n"
            + "\n".join(lines or ["No titles available."])
            + "\n\nSource: TMDB"
        )

    @router.message(Command("imdb", "omdb"))
    async def imdb(m: Message, command: CommandObject):
        if not await authorize(m):
            return
        key, title = os.getenv("OMDB_API_KEY"), (command.args or "").strip()[:180]
        if not key or not title:
            return await m.answer(
                "Use /imdb TITLE." if key else "OMDb discovery is disabled."
            )
        data = await request(
            m, "GET", "https://www.omdbapi.com/", params={"apikey": key, "t": title}
        )
        if data is None:
            return
        if data.get("Response") != "True":
            return await m.answer("No matching title on OMDb.")
        await m.answer(
            f"<b>{escape(data['Title'])}</b> ({escape(data.get('Year', ''))})\n"
            f"★ {escape(data.get('imdbRating', ''))} · {escape(data.get('Genre', ''))}\n"
            f"{escape(data.get('Plot', '')[:1200])}"
        )

    @router.message(Command("kino"))
    async def kino(m: Message, command: CommandObject):
        if not await authorize(m):
            return
        key, title = os.getenv("KINO_API_KEY"), (command.args or "").strip()[:180]
        if not key or not title:
            return await m.answer(
                "Use /kino TITLE." if key else "Kinopoisk discovery is disabled."
            )
        params = {"page": 1}
        words = []
        for word in title.split():
            name, sep, value = word.partition(":")
            if sep and name in {"year", "genre", "country", "rating", "page"}:
                if not re.fullmatch(r"[0-9]{1,4}", value):
                    return await m.answer(
                        "Use numeric year, genre ID, country ID, minimum rating or page."
                    )
                n = int(value)
                if name == "year" and 1888 <= n <= 2100:
                    params.update(yearFrom=n, yearTo=n)
                elif name == "rating" and n <= 10:
                    params.update(ratingFrom=n)
                elif name in {"genre", "country", "page"} and n > 0:
                    params[
                        {"genre": "genres", "country": "countries", "page": "page"}[
                            name
                        ]
                    ] = n
                else:
                    return await m.answer(
                        "Invalid filter value. Example: /kino year:2024 rating:7"
                    )
            else:
                words.append(word)
        if words:
            params["keyword"] = " ".join(words)
        data = await request(
            m,
            "GET",
            "https://kinopoiskapiunofficial.tech/api/v2.2/films",
            headers={"X-API-KEY": key},
            params=params,
        )
        if data is None:
            return
        lines = [
            f"• {escape(x.get('nameRu') or x.get('nameOriginal') or 'Untitled')} ({x.get('year') or '?'}) · #{int(x['kinopoiskId'])}"
            for x in data.get("items", [])[:8]
        ]
        await m.answer("\n".join(lines or ["No matching titles."]))

    @router.message(Command("kinobudget"))
    async def budget(m: Message, command: CommandObject):
        if not await authorize(m):
            return
        key = os.getenv("KINO_API_KEY")
        ident = (command.args or "").strip()
        if not key or not re.fullmatch(r"[0-9]{1,10}", ident):
            return await m.answer("Use /kinobudget FILM_ID with Kinopoisk enabled.")
        data = await request(
            m,
            "GET",
            f"https://kinopoiskapiunofficial.tech/api/v2.2/films/{ident}/box_office",
            headers={"X-API-KEY": key},
        )
        if data is not None:
            rows = [
                f"{row.get('type', '')}: {row.get('amount', '?')} {row.get('currencyCode', '')}"
                for row in data.get("items", [])[:10]
            ]
            await m.answer(
                "\n".join(rows or ["No budget or box-office figures are available."]),
                parse_mode=None,
            )

    @router.message(Command("serverrequest"))
    async def seerr(m: Message, command: CommandObject):
        if not await authorize(m, owner=True):
            return
        url, key = _url(os.getenv("SEERR_URL")), os.getenv("SEERR_API_KEY")
        parts = (command.args or "").split()
        if not (url and key):
            return await m.answer("The media server bridge is disabled.")
        if (
            len(parts) != 2
            or parts[0] not in {"tv", "movie"}
            or not parts[1].isdecimal()
            or len(parts[1]) > 10
        ):
            return await m.answer("Usage: /serverrequest movie|tv TMDB_ID")
        payload = {"mediaType": parts[0], "mediaId": int(parts[1])}
        if parts[0] == "tv":
            payload["seasons"] = "all"
        instance = hashlib.sha256(url.rstrip("/").encode()).hexdigest()
        receipt = f"{instance}:{m.chat.id}:{m.message_id}"
        try:
            await store.db.seerr_requests.insert_one(
                {
                    "_id": receipt,
                    "owner": m.from_user.id,
                    "instance": instance,
                    "submitted_at": datetime.now(UTC),
                    "state": "submitting",
                }
            )
        except DuplicateKeyError:
            previous = await store.db.seerr_requests.find_one({"_id": receipt})
            if previous and previous.get("request_id"):
                return await m.answer(f"Use /serverstatus {previous['request_id']}.")
            return await m.answer(
                "Submission already recorded. Check the Seerr request history before retrying."
            )
        data = await request(
            m,
            "POST",
            url.rstrip("/") + "/api/v1/request",
            headers={"X-Api-Key": key},
            json=payload,
        )
        if data:
            rid = int(data["id"])
            await store.db.seerr_requests.update_one(
                {"_id": receipt}, {"$set": {"state": "accepted", "request_id": rid}}
            )
            await m.answer(f"Media request #{rid} recorded. Use /serverstatus {rid}.")

    @router.message(Command("serverstatus"))
    async def serverstatus(m: Message, command: CommandObject):
        if not await authorize(m, owner=True):
            return
        url, key = _url(os.getenv("SEERR_URL")), os.getenv("SEERR_API_KEY")
        text = (command.args or "").strip()
        if (
            not url
            or not key
            or not text.isdecimal()
            or len(text) > 10
            or not int(text)
        ):
            return await m.answer(
                "Configure the media bridge and use /serverstatus REQUEST_ID."
            )
        rid = int(text)
        instance = hashlib.sha256(url.rstrip("/").encode()).hexdigest()
        known = await store.db.seerr_requests.find_one(
            {"owner": m.from_user.id, "instance": instance, "request_id": rid},
            {"_id": 1},
        )
        if not known:
            return await m.answer(
                "No matching request submitted by you through this bot."
            )
        data = await request(
            m,
            "GET",
            url.rstrip("/") + f"/api/v1/request/{rid}",
            headers={"X-Api-Key": key},
        )
        if data:
            request_state = {
                1: "pending",
                2: "approved",
                3: "declined",
                4: "failed",
                5: "completed",
            }.get(data.get("status"), "unknown")
            media_state = {
                1: "unknown",
                2: "pending",
                3: "processing",
                4: "partially available",
                5: "available",
                6: "deleted",
            }.get((data.get("media") or {}).get("status"), "unknown")
            await m.answer(
                f"Request #{rid} · {request_state}\nMedia · {media_state}",
                parse_mode=None,
            )

    @router.message(Command("forward"))
    async def forward(m: Message, command: CommandObject):
        if not await authorize(m, owner=True):
            return
        try:
            src, dest, first, last = map(int, (command.args or "").split())
            if (
                src >= 0
                or dest >= 0
                or src == dest
                or not 0 < first <= last <= first + 4999
            ):
                raise ValueError
        except ValueError:
            return await m.answer(
                "Usage: /forward SOURCE TARGET FIRST_ID LAST_ID (up to 5,000 messages)."
            )
        if not await store.db.sources.find_one({"_id": src, "active": True}):
            return await m.answer("Register and enable the source first.")
        try:
            chat = await m.bot.get_chat(src)
            if chat.has_protected_content:
                return await m.answer("This source protects its content.")
            await m.bot.get_chat(dest)
        except Exception:
            return await m.answer("The bot needs access to both chats.")
        job_id = await store.enqueue(
            "forward",
            {
                "source_chat": src,
                "chat_id": dest,
                "start": first,
                "end": last,
                "actor_id": m.from_user.id,
            },
            key=f"forward:{m.chat.id}:{m.message_id}",
        )
        await m.answer(f"Forwarding queued · <code>{escape(job_id)}</code>")

    @router.message(Command("drive"))
    async def drive(m: Message, command: CommandObject):
        if not await authorize(m, owner=True):
            return
        title = (command.args or "").strip()[:120]
        if not title:
            return await m.answer("Use /drive NAME.")
        if not (os.getenv("DRIVE_ACCESS_TOKEN") or os.getenv("DRIVE_REFRESH_TOKEN")):
            return await m.answer("Drive search is disabled.")
        token = await drive_token(m)
        if not token:
            return
        term = title.replace("\\", "\\\\").replace("'", "\\'")
        params = {
            "q": f"trashed = false and name contains '{term}'",
            "pageSize": 10,
            "fields": "files(id,name,webViewLink)",
            "includeItemsFromAllDrives": "true",
            "supportsAllDrives": "true",
        }
        if drive_id := os.getenv("DRIVE_ID"):
            params.update(corpora="drive", driveId=drive_id)
        endpoint = "https://www.googleapis.com/drive/v3/files"
        try:
            data = await request(
                m,
                "GET",
                endpoint,
                headers={"Authorization": f"Bearer {token}"},
                params=params,
                report_unauthorized=bool(os.getenv("DRIVE_REFRESH_TOKEN")),
            )
        except PermissionError:
            token = await drive_token(m, rejected=token)
            if not token:
                return
            data = await request(
                m,
                "GET",
                endpoint,
                headers={"Authorization": f"Bearer {token}"},
                params=params,
            )
        if data is not None:
            lines = []
            for item in data.get("files", []):
                link = _url(item.get("webViewLink"))
                name = escape(item.get("name", "")[:120])
                lines.append(
                    f'<a href="{escape(link, quote=True)}">{name}</a>' if link else name
                )
            await m.answer("\n".join(lines or ["No accessible matches."]))

    return router
