"""Optional TMDB cards and bounded, expiring HTTP media streams."""

import logging
import re
import secrets
from datetime import timedelta
from html import escape
from urllib.parse import quote

import aiohttp
from aiohttp import web
from pyrogram.errors import RPCError

from database.ia_filterdb import norm, now
from Script import button

log = logging.getLogger(__name__)
CHUNK = 1024 * 1024


async def details(app, d, cfg):
    if not cfg["tmdb_key"]:
        return None
    title = re.split(
        r"\b(?:19\d\d|20\d\d|s\d+|s\d+e\d+|2160p|1080p|720p|480p)\b", norm(d["name"])
    )[0].strip()[:120]
    if not title:
        return None
    kind = "tv" if d["season"] else "movie"
    ident = f"{kind}:{title}:{d['year']}"
    coll = app.db.db.eva_metadata
    cached = await coll.find_one({"_id": ident, "expires": {"$gt": now()}})
    if cached:
        return cached.get("card")
    params = {"api_key": cfg["tmdb_key"], "query": title}
    if d["year"]:
        params["first_air_date_year" if kind == "tv" else "year"] = d["year"]
    try:
        async with app.http.get(
            f"https://api.themoviedb.org/3/search/{kind}",
            params=params,
            timeout=aiohttp.ClientTimeout(total=4),
        ) as res:
            if res.status != 200:
                return None
            rows = (await res.json()).get("results", [])
    except (aiohttp.ClientError, TimeoutError, ValueError):
        return None
    card = None
    if rows:
        row = rows[0]
        poster = row.get("poster_path") or ""
        card = {
            "text": f"<b>{escape((row.get('title') or row.get('name') or '')[:120])}</b> · ★ {row.get('vote_average', 0):.1f}\n{escape((row.get('overview') or '')[:350])}",
            "poster": "https://image.tmdb.org/t/p/w342" + poster
            if re.fullmatch(r"/[\w.-]+", poster)
            else "",
        }
    await coll.replace_one(
        {"_id": ident},
        {"_id": ident, "card": card, "expires": now() + timedelta(hours=12)},
        upsert=True,
    )
    return card


async def stream_buttons(app, uid, key, cfg):
    if not cfg["stream_url"] or cfg["protect_files"]:
        return []
    if cfg["premium_stream"] and not await app.db.users.find_one(
        {"_id": uid, "premium_until": {"$gt": now()}}
    ):
        return []
    token = secrets.token_urlsafe(24)
    await app.db.db.eva_grants.insert_one(
        {
            "_id": token,
            "uid": uid,
            "file": key,
            "expires": now() + timedelta(minutes=15),
        }
    )
    base = cfg["stream_url"].rstrip("/") + "/media/" + token
    return [
        button("▶ ᴡᴀᴛᴄʜ", url=base),
        button("⬇ ᴅᴏᴡɴʟᴏᴀᴅ", url=base + "?download=1", style="success"),
    ]


def byte_range(value, total):
    if value is None:
        return 0, total - 1
    m = re.fullmatch(r"bytes=(\d*)-(\d*)", value.strip())
    if not m or not (m[1] or m[2]):
        raise ValueError
    start = int(m[1]) if m[1] else max(0, total - int(m[2]))
    end = min(total - 1, int(m[2])) if m[1] and m[2] else total - 1
    if not 0 <= start <= end < total:
        raise ValueError
    return start, end


async def serve(app, req):
    from plugins.pm_filter import entry, missing_membership, original

    key = req.match_info["token"]
    grant = await app.db.db.eva_grants.find_one({"_id": key, "expires": {"$gt": now()}})
    if not grant:
        raise web.HTTPNotFound()
    cfg = await app.db.config()
    if (
        not cfg["stream_url"]
        or cfg["protect_files"]
        or await app.db.users.find_one({"_id": grant["uid"], "banned": True})
    ):
        raise web.HTTPForbidden()
    if cfg["premium_stream"] and not await app.db.users.find_one(
        {"_id": grant["uid"], "premium_until": {"$gt": now()}}
    ):
        raise web.HTTPForbidden()
    if app.stream_slots.locked():
        raise web.HTTPServiceUnavailable(headers={"Retry-After": "10"})
    async with app.stream_slots:
        try:
            if await missing_membership(app, grant["uid"], cfg):
                raise web.HTTPForbidden()
            d = await entry(app, grant["file"], cfg)
            post, media = await original(app, d, cfg)
        except (ValueError, RPCError):
            raise web.HTTPNotFound() from None
        total = media.file_size or 0
        try:
            first, last = byte_range(req.headers.get("Range"), total)
        except ValueError:
            raise web.HTTPRequestRangeNotSatisfiable(
                headers={"Content-Range": f"bytes */{total}"}
            ) from None
        partial = "Range" in req.headers
        mime = media.mime_type or ""
        safe_mime = (
            mime
            if re.fullmatch(r"(?:video|audio)/[a-zA-Z0-9.+-]+", mime)
            else "application/octet-stream"
        )
        disp = (
            "inline"
            if safe_mime != "application/octet-stream" and not req.query.get("download")
            else "attachment"
        )
        headers = {
            "Content-Type": safe_mime,
            "Content-Length": str(last - first + 1),
            "Accept-Ranges": "bytes",
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
            "Referrer-Policy": "no-referrer",
            "Content-Disposition": f"{disp}; filename*=UTF-8''{quote(d['name'], safe='')}",
        }
        if partial:
            headers["Content-Range"] = f"bytes {first}-{last}/{total}"
        response = web.StreamResponse(status=206 if partial else 200, headers=headers)
        await response.prepare(req)
        if req.method != "HEAD" and total:
            pos = first // CHUNK * CHUNK
            async for block in app.stream_media(
                post, offset=first // CHUNK, limit=last // CHUNK - first // CHUNK + 1
            ):
                await response.write(block[max(0, first - pos) : last - pos + 1])
                pos += len(block)
                if pos > last:
                    break
        await response.write_eof()
        return response
