"""Private, group and inline catalog flows with durable owned search sessions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import secrets

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineQuery,
    InlineQueryResultArticle,
    InlineQueryResultCachedDocument,
    InputTextMessageContent,
    Message,
)
from pymongo import ReturnDocument

from wovie.delivery import DeliveryError
from wovie.admission import BatchQueueFull, enqueue_batch
from wovie.parser import normalize, parse_query
from wovie.ui import button, help_text, home, keyboard, safe, size_label


def _key(*parts: object) -> str:
    return hashlib.sha256(":".join(str(x) for x in parts).encode()).hexdigest()[:32]


def _title(text: str) -> str:
    return " ".join(text.casefold().split())[:160]


def build_router(settings, store, delivery, metadata) -> Router:
    router = Router(name="user")

    async def throttled(user_id: int) -> bool:
        if user_id in settings.admins or settings.rate_limit <= 0:
            return False
        now = datetime.now(UTC)
        key = f"{user_id}:{now:%Y%m%d%H%M}"
        counter = await store.db.rate_limits.find_one_and_update(
            {"_id": key},
            {
                "$inc": {"used": 1},
                "$setOnInsert": {"expires_at": now + timedelta(minutes=5)},
            },
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        return counter["used"] > settings.rate_limit

    async def context_access(user_id: int, chat_id: int):
        ok, reason = await delivery.allowed(
            user_id, group_id=chat_id if chat_id < 0 else None
        )
        if not ok:
            return False, [], reason
        source_ids = None
        if chat_id < 0:
            config = await store.setting(chat_id)
            if not config.get("enabled", False):
                return (
                    False,
                    [],
                    "Search is disabled in this group. Ask an administrator to enable it.",
                )
            source_ids = config.get("source_ids") or None
            if user_id not in settings.admins:
                if not await delivery._member(user_id, chat_id):
                    return False, [], "Join the originating group and try again."
                for channel in config.get("required_channels") or []:
                    if not await delivery._member(user_id, channel):
                        return (
                            False,
                            [],
                            "Join this group’s required channels and try again.",
                        )
                if config.get("premium_only"):
                    user = await store.db.users.find_one({"_id": user_id}) or {}
                    if not user.get("premium_until") or user[
                        "premium_until"
                    ] <= datetime.now(UTC):
                        return (
                            False,
                            [],
                            "This group requires an active plan. Open /premium privately.",
                        )
        visible = await delivery.visible_sources(user_id, source_ids)
        if chat_id < 0:
            visible = [
                s["_id"]
                async for s in store.db.sources.find(
                    {
                        "_id": {"$in": visible},
                        "$or": [{"public": True}, {"_id": chat_id}],
                    }
                )
            ]
        return True, visible, ""

    async def render(session: dict, page: int):
        ok, sources, reason = await context_access(
            session["owner_id"], session["chat_id"]
        )
        if not ok:
            raise DeliveryError(reason)
        sources = [s for s in sources if s in session["sources"]]
        if "snapshot_ids" not in session:
            raise DeliveryError("This search predates stable paging. Send a new query.")
        identifiers = session["snapshot_ids"]
        page_size = session["page_size"]
        page_count = max(1, (len(identifiers) + page_size - 1) // page_size)
        if not 0 <= page < page_count:
            raise DeliveryError("This page is outside the saved result set.")
        # Slice before checking current visibility: removed files leave a hole rather
        # than moving another file to a different page after an index/update/delete.
        selected = identifiers[page * page_size : (page + 1) * page_size]
        rows = []
        for identifier in selected:
            record = await store.get_file(identifier)
            if record and record["source_chat"] in sources:
                rows.append(record)
        more = page + 1 < page_count
        await store.db.search_sessions.update_one(
            {"_id": session["_id"]},
            {"$set": {"page": page, "rows": [r["_id"] for r in rows]}},
        )
        heading = f"🔎 <b>{safe(session['query'] or 'Latest files')}</b>\n"
        heading += (
            f"<i>Page {page + 1}/{page_count} · {len(rows)} available results</i>\n"
        )
        if session.get("snapshot_capped"):
            heading += "<i>First 120 matches saved. Refine the title or filters for more.</i>\n"
        heading += "\n"
        if not identifiers:
            return (
                heading + "No accessible matches. Try fewer words or remove a filter.\n"
                "Use /request title to request a missing release."
            ), None
        buttons = []
        for record in rows:
            token = await store.create_link(
                [record["_id"]],
                owner_id=session["owner_id"],
                expires_at=datetime.now(UTC) + timedelta(days=1),
            )
            await store.db.links.update_one(
                {"_id": token}, {"$set": {"chat_id": session["chat_id"]}}
            )
            username = (await delivery.bot.me()).username
            filename = str(record.get("filename") or record.get("title") or "Media")
            label = f"📁 {size_label(record.get('file_size'))} · {filename}"
            buttons.append(
                [
                    button(
                        label[:60], url=f"https://t.me/{username}?start=link_{token}"
                    ),
                    button("☆", data=f"fav:{session['_id']}:{record['_id']}"),
                ]
            )
        navigation = []
        if page:
            navigation.append(
                button("‹ Previous", data=f"page:{session['_id']}:{page - 1}")
            )
        if more:
            navigation.append(
                button("Next ›", data=f"page:{session['_id']}:{page + 1}")
            )
        if navigation:
            buttons.append(navigation)
        if rows:
            buttons.append(
                [
                    button("📦 Send this page", data=f"batch:{session['_id']}"),
                    button("🔔 Follow title", data=f"watch:{session['_id']}"),
                ]
            )
        detail = (
            "Choose a file to open private delivery. ☆ saves a favorite."
            if rows
            else "This page’s files are no longer accessible. Page positions are preserved; navigate or start a new search."
        )
        return heading + detail, keyboard(buttons) if buttons else None

    async def search(message: Message, query: str):
        if not message.from_user:
            return
        query = query.strip()[:240]
        ok, sources, reason = await context_access(
            message.from_user.id, message.chat.id
        )
        if not ok:
            if message.chat.type == "private" or (message.text or "").startswith("/"):
                await message.answer(safe(reason), parse_mode="HTML")
            return
        if await throttled(message.from_user.id):
            await message.answer(
                "Please slow down; your search limit resets next minute."
            )
            return
        await store.touch_user(message.from_user)
        try:
            text, filters = parse_query(query)
        except (ValueError, TypeError):
            await message.answer(
                "Invalid filters. Example: <code>Dune year:2024 quality:1080p</code>",
                parse_mode="HTML",
            )
            return
        snapshot_ids, snapshot_capped = await store.snapshot_ids(
            text, filters, source_ids=sources, limit=120
        )
        session = {
            "_id": secrets.token_urlsafe(12),
            "owner_id": message.from_user.id,
            "chat_id": message.chat.id,
            "query": text,
            "filters": filters,
            "sources": sources,
            "page": 0,
            "rows": [],
            "page_size": settings.page_size,
            "snapshot_ids": snapshot_ids,
            "snapshot_capped": snapshot_capped,
            "snapshot_at": datetime.now(UTC),
            "expires_at": datetime.now(UTC) + timedelta(hours=2),
        }
        await store.db.search_sessions.insert_one(session)
        if text:
            await store.db.search_history.update_one(
                {"_id": _key(message.from_user.id, query)},
                {
                    "$set": {
                        "user_id": message.from_user.id,
                        "query": query,
                        "created_at": datetime.now(UTC),
                        "expires_at": datetime.now(UTC) + timedelta(days=30),
                    }
                },
                upsert=True,
            )
        body, markup = await render(session, 0)
        config = await store.setting(message.chat.id) if message.chat.id < 0 else {}
        destination = config.get("results_channel")
        if (
            destination
            and markup
            and await delivery._member(message.from_user.id, destination)
        ):
            unpublished = await store.db.sources.count_documents(
                {"_id": {"$in": sources}, "public": {"$ne": True}}, limit=1
            )
            if not unpublished:
                try:
                    chat = await delivery.bot.get_chat(destination)
                    posted = await delivery.bot.send_message(
                        destination, body, reply_markup=markup, parse_mode="HTML"
                    )
                    target = (
                        f"https://t.me/{chat.username}/{posted.message_id}"
                        if chat.username
                        else f"https://t.me/c/{str(destination)[4:]}/{posted.message_id}"
                    )
                    await message.answer(
                        "🔎 Your results are ready.",
                        reply_markup=keyboard([[button("Open results", url=target)]]),
                    )
                    return
                except (TelegramBadRequest, TelegramForbiddenError):
                    pass
        await message.answer(body, reply_markup=markup, parse_mode="HTML")

    async def owned(callback: CallbackQuery, token: str):
        session = await store.db.search_sessions.find_one(
            {"_id": token, "expires_at": {"$gt": datetime.now(UTC)}}
        )
        if not session:
            await callback.answer("This search expired. Search again.", show_alert=True)
            return None
        if session["owner_id"] != callback.from_user.id:
            await callback.answer(
                "These controls belong to the person who searched. Send your own query.",
                show_alert=True,
            )
            return None
        ok, sources, reason = await context_access(
            callback.from_user.id, session["chat_id"]
        )
        if not ok:
            await callback.answer(reason[:190], show_alert=True)
            return None
        session["sources"] = [s for s in session["sources"] if s in sources]
        return session

    @router.message(CommandStart())
    async def start(message: Message, command: CommandObject):
        if not message.from_user:
            return
        await store.touch_user(message.from_user)
        if message.chat.type == "private":
            await store.db.users.update_one(
                {"_id": message.from_user.id, "blocked": True},
                {"$unset": {"blocked": ""}},
            )
        payload = (command.args or "").strip()
        if not payload:
            cfg = await store.setting(message.chat.id)
            global_cfg = await store.setting(0)
            welcome = cfg.get("welcome_text") or global_cfg.get("welcome_text")
            body = home(settings.bot_name, message.from_user.first_name)
            if welcome:
                body = safe(str(welcome)[:512]) + "\n\n" + body
            await message.answer(body, parse_mode="HTML")
            return
        if message.chat.type != "private":
            await message.answer("Open this link in a private chat with the bot.")
            return
        token = payload[5:] if payload.startswith("link_") else payload
        link = await store.get_link(token)
        if not link or link.get("owner_id") not in (None, message.from_user.id):
            await message.answer(
                "This link is expired, unavailable, or belongs to another user."
            )
            return
        origin = link.get("chat_id", message.chat.id)
        ok, sources, reason = await context_access(message.from_user.id, origin)
        if not ok:
            await message.answer(safe(reason), parse_mode="HTML")
            return
        identifiers = link.get("file_ids") or []
        if not identifiers:
            await message.answer("This link has no files.")
            return
        if len(identifiers) > 1:
            try:
                job = await enqueue_batch(
                    store,
                    {
                        "actor_id": message.from_user.id,
                        "chat_id": message.chat.id,
                        "file_ids": identifiers,
                        "source_chat": origin,
                    },
                    key=f"link:{message.chat.id}:{message.message_id}",
                )
            except BatchQueueFull as exc:
                await message.answer(safe(exc), parse_mode="HTML")
                return
            await message.answer(
                f"📦 Your batch is queued. Reference: <code>{safe(job)}</code>",
                parse_mode="HTML",
            )
            return
        try:
            await delivery.send(
                message.from_user.id,
                identifiers[0],
                group_id=origin if origin < 0 else None,
            )
        except DeliveryError as exc:
            await message.answer(safe(exc), parse_mode="HTML")

    @router.message(Command("help"))
    async def help_command(message: Message):
        await message.answer(help_text(), parse_mode="HTML")

    @router.message(Command("search", "find"))
    async def search_command(message: Message, command: CommandObject):
        if not command.args:
            await message.answer(
                "Use <code>/search title language:english quality:1080p</code>",
                parse_mode="HTML",
            )
            return
        await search(message, command.args)

    @router.message(Command("recent"))
    async def recent(message: Message):
        await search(message, "")

    @router.callback_query(F.data.startswith("page:"))
    async def page(callback: CallbackQuery):
        pieces = callback.data.split(":")
        if len(pieces) != 3 or not pieces[2].isdigit() or int(pieces[2]) > 200:
            await callback.answer("Invalid page.")
            return
        session = await owned(callback, pieces[1])
        if not session:
            return
        if await throttled(callback.from_user.id):
            await callback.answer("Please wait until next minute.", show_alert=True)
            return
        await callback.answer()
        if not callback.message or not isinstance(callback.message, Message):
            return
        try:
            body, markup = await render(session, int(pieces[2]))
            await callback.message.edit_text(
                body, reply_markup=markup, parse_mode="HTML"
            )
        except DeliveryError as exc:
            await callback.message.answer(safe(exc), parse_mode="HTML")
        except TelegramBadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                raise

    @router.callback_query(F.data.startswith("fav:"))
    async def favorite(callback: CallbackQuery):
        pieces = callback.data.split(":")
        if len(pieces) != 3:
            await callback.answer("Invalid selection.")
            return
        session = await owned(callback, pieces[1])
        if not session:
            return
        if pieces[2] not in session.get("rows", []):
            await callback.answer(
                "This page changed. Select a current result.", show_alert=True
            )
            return
        record = await store.get_file(pieces[2])
        if not record or record["source_chat"] not in session["sources"]:
            await callback.answer("This file is unavailable.", show_alert=True)
            return
        await store.db.favorites.update_one(
            {"_id": _key(callback.from_user.id, pieces[2])},
            {
                "$setOnInsert": {
                    "user_id": callback.from_user.id,
                    "file_id": pieces[2],
                    "created_at": datetime.now(UTC),
                }
            },
            upsert=True,
        )
        await callback.answer("Saved to /favorites.")

    @router.message(Command("favorites"))
    async def favorites(message: Message):
        if message.chat.type != "private" or not message.from_user:
            await message.answer("Open /favorites privately.")
            return
        ok, sources, reason = await context_access(
            message.from_user.id, message.chat.id
        )
        if not ok:
            await message.answer(safe(reason), parse_mode="HTML")
            return
        rows = []
        username = (await delivery.bot.me()).username
        async for favorite in (
            store.db.favorites.find({"user_id": message.from_user.id})
            .sort("created_at", -1)
            .limit(20)
        ):
            record = await store.get_file(favorite["file_id"])
            if not record or record["source_chat"] not in sources:
                continue
            token = await store.create_link(
                [record["_id"]],
                owner_id=message.from_user.id,
                expires_at=datetime.now(UTC) + timedelta(days=1),
            )
            rows.append(
                [
                    button(
                        str(record.get("filename", "Media"))[:55],
                        url=f"https://t.me/{username}?start=link_{token}",
                    ),
                    button("×", data=f"unfav:{record['_id']}"),
                ]
            )
        await message.answer(
            "⭐ <b>Your favorites</b>\nLatest 20 accessible saved files."
            if rows
            else "No accessible saved files yet.",
            reply_markup=keyboard(rows) if rows else None,
            parse_mode="HTML",
        )

    @router.callback_query(F.data.startswith("unfav:"))
    async def remove_favorite(callback: CallbackQuery):
        identifier = callback.data.split(":", 1)[1]
        await store.db.favorites.delete_one(
            {"_id": _key(callback.from_user.id, identifier)}
        )
        await callback.answer("Removed. Refresh /favorites.")

    @router.callback_query(F.data.startswith("batch:"))
    async def batch(callback: CallbackQuery):
        session = await owned(callback, callback.data.split(":", 1)[1])
        if not session:
            return
        if await throttled(callback.from_user.id):
            await callback.answer("Please wait until next minute.", show_alert=True)
            return
        ids = []
        for identifier in session.get("rows", []):
            record = await store.get_file(identifier)
            if record and record["source_chat"] in session["sources"]:
                ids.append(identifier)
        if not ids:
            await callback.answer("No available files on this page.", show_alert=True)
            return
        try:
            await enqueue_batch(
                store,
                {
                    "actor_id": callback.from_user.id,
                    "chat_id": callback.from_user.id,
                    "file_ids": ids,
                    "source_chat": session["chat_id"],
                },
                key=f"batch:{callback.id}",
            )
        except BatchQueueFull as exc:
            await callback.answer(str(exc)[:190], show_alert=True)
            return
        await callback.answer(
            "Batch queued. Start the bot privately to receive it.", show_alert=True
        )

    async def save_watch(user_id: int, title: str):
        normalized = normalize(title)[:160]
        if not normalized:
            return None
        identifier = _key(user_id, normalized)
        existing = await store.db.watchlist.find_one({"_id": identifier})
        if (
            not existing
            and await store.db.watchlist.count_documents(
                {"user_id": user_id}, limit=100
            )
            >= 100
        ):
            raise DeliveryError(
                "You already follow 100 titles. Remove one with /unwatch first."
            )
        await store.db.watchlist.update_one(
            {"_id": identifier},
            {
                "$setOnInsert": {
                    "user_id": user_id,
                    "title": title[:160],
                    "title_norm": normalized,
                    "created_at": datetime.now(UTC),
                }
            },
            upsert=True,
        )
        return identifier

    @router.callback_query(F.data.startswith("watch:"))
    async def watch_callback(callback: CallbackQuery):
        session = await owned(callback, callback.data.split(":", 1)[1])
        if not session:
            return
        if not session["query"]:
            await callback.answer("Search for a title first.")
            return
        try:
            await save_watch(callback.from_user.id, session["query"])
        except DeliveryError as exc:
            await callback.answer(str(exc), show_alert=True)
            return
        await callback.answer(
            "Following this title. Manage with /watchlist.", show_alert=True
        )

    @router.message(Command("watch", "subscribe"))
    async def watch(message: Message, command: CommandObject):
        if message.chat.type != "private" or not message.from_user:
            await message.answer("Manage subscriptions privately.")
            return
        ok, reason = await delivery.allowed(message.from_user.id)
        if not ok:
            await message.answer(safe(reason), parse_mode="HTML")
            return
        if not command.args:
            await message.answer(
                "Use <code>/watch title</code>. Alerts arrive when matching files are indexed.",
                parse_mode="HTML",
            )
            return
        if await throttled(message.from_user.id):
            await message.answer("Please wait until next minute.")
            return
        try:
            identifier = await save_watch(message.from_user.id, command.args)
        except DeliveryError as exc:
            await message.answer(safe(exc), parse_mode="HTML")
            return
        await message.answer(
            f"🔔 Following <b>{safe(command.args[:160])}</b>.\nID: <code>{identifier}</code>",
            parse_mode="HTML",
        )

    @router.message(Command("watchlist", "subscriptions"))
    async def watchlist(message: Message):
        if message.chat.type != "private" or not message.from_user:
            await message.answer("Open /watchlist privately.")
            return
        lines = []
        async for row in (
            store.db.watchlist.find({"user_id": message.from_user.id})
            .sort("created_at", -1)
            .limit(30)
        ):
            lines.append(f"• {safe(row['title'])}\n<code>{safe(row['_id'])}</code>")
        await message.answer(
            "🔔 <b>Your subscriptions</b>\n\n"
            + ("\n".join(lines) or "No subscriptions yet.")
            + "\n\nRemove with <code>/unwatch ID</code>.",
            parse_mode="HTML",
        )

    @router.message(Command("unwatch", "unsubscribe"))
    async def unwatch(message: Message, command: CommandObject):
        if message.chat.type != "private" or not message.from_user:
            return
        text = (command.args or "").strip()
        result = await store.db.watchlist.delete_one(
            {
                "user_id": message.from_user.id,
                "$or": [{"_id": text}, {"title_norm": normalize(text)[:160]}],
            }
        )
        await message.answer(
            "Subscription removed."
            if result.deleted_count
            else "Not found. Use /watchlist for IDs."
        )

    @router.message(Command("request"))
    async def request(message: Message, command: CommandObject):
        if not message.from_user:
            return
        text = (command.args or "").strip()[:160]
        if not text:
            await message.answer(
                "Use <code>/request movie or series title</code>.", parse_mode="HTML"
            )
            return
        ok, _, reason = await context_access(message.from_user.id, message.chat.id)
        if not ok:
            await message.answer(safe(reason), parse_mode="HTML")
            return
        if await throttled(message.from_user.id):
            await message.answer(
                "Please wait until next minute before sending another request."
            )
            return
        identifier = _key(message.from_user.id, _title(text))
        await store.db.requests.update_one(
            {"_id": identifier},
            {
                "$setOnInsert": {
                    "user_id": message.from_user.id,
                    "query": text,
                    "status": "pending",
                    "created_at": datetime.now(UTC),
                }
            },
            upsert=True,
        )
        await message.answer(
            f"📝 Request saved: <b>{safe(text)}</b>\nCheck /myrequests privately.",
            parse_mode="HTML",
        )

    @router.message(Command("myrequests"))
    async def requests(message: Message):
        if message.chat.type != "private" or not message.from_user:
            await message.answer("Open /myrequests privately.")
            return
        lines = []
        async for item in (
            store.db.requests.find({"user_id": message.from_user.id})
            .sort("created_at", -1)
            .limit(20)
        ):
            lines.append(f"• {safe(item['query'])} — {safe(item['status'])}")
        await message.answer(
            "📝 <b>Your requests</b>\n\n" + ("\n".join(lines) or "No requests yet."),
            parse_mode="HTML",
        )

    @router.message(Command("movie", "tmdb"))
    async def movie(message: Message, command: CommandObject):
        if not message.from_user:
            return
        ok, _, reason = await context_access(message.from_user.id, message.chat.id)
        if not ok:
            await message.answer(safe(reason), parse_mode="HTML")
            return
        if not command.args:
            await message.answer(
                "Use <code>/movie title</code> for movie and TV information.",
                parse_mode="HTML",
            )
            return
        if await throttled(message.from_user.id):
            await message.answer("Please wait until next minute.")
            return
        try:
            matches = await metadata.search(command.args[:160])
        except Exception:
            await message.answer(
                "Movie information is temporarily unavailable. Catalog search still works."
            )
            return
        if not matches:
            await message.answer(
                "No metadata found. Try the original title or a different spelling."
            )
            return
        for item in matches[:3]:
            text = (
                f"🎬 <b>{safe(item.get('title', 'Title'))}</b> ({safe(item.get('year', ''))})\n"
                f"⭐ {safe(item.get('rating', '—'))}\n\n{safe(str(item.get('overview') or 'No synopsis available.')[:800])}\n\n"
                "<i>Metadata supplied by TMDB.</i>"
            )
            url = item.get("url")
            markup = (
                keyboard([[button("Details ↗", url=url)]])
                if url and url.startswith("https://www.themoviedb.org/")
                else None
            )
            await message.answer(text, reply_markup=markup, parse_mode="HTML")

    @router.message(Command("history"))
    async def history(message: Message):
        if message.chat.type != "private" or not message.from_user:
            await message.answer("Open /history privately.")
            return
        lines = []
        async for row in (
            store.db.search_history.find({"user_id": message.from_user.id})
            .sort("created_at", -1)
            .limit(20)
        ):
            lines.append("• " + safe(row["query"]))
        await message.answer(
            "🔎 <b>Recent searches</b>\n\n"
            + ("\n".join(lines) or "No saved history.")
            + "\n\n/clearhistory deletes your saved history.",
            parse_mode="HTML",
        )

    @router.message(Command("clearhistory"))
    async def clear_history(message: Message):
        if message.from_user and message.chat.type == "private":
            await store.db.search_history.delete_many({"user_id": message.from_user.id})
            await message.answer("Your saved search history was deleted.")

    @router.inline_query()
    async def inline(query: InlineQuery):
        ok, reason = await delivery.allowed(query.from_user.id)
        if not ok or await throttled(query.from_user.id):
            await query.answer([], cache_time=0, is_personal=True)
            return
        text = query.query.strip()[:160]
        if not text:
            await query.answer([], cache_time=0, is_personal=True)
            return
        try:
            title, filters = parse_query(text)
        except (ValueError, TypeError):
            await query.answer([], cache_time=0, is_personal=True)
            return
        sources = await delivery.visible_sources(query.from_user.id)
        rows, more = await store.search(title, 0, filters, source_ids=sources)
        results = []
        username = (await delivery.bot.me()).username
        config = await store.setting(0)
        for record in rows[:8]:
            source = (
                await store.db.sources.find_one({"_id": record["source_chat"]}) or {}
            )
            # Cached media bypasses a later delivery callback. Permit only explicitly public,
            # ungated source files; private sources always produce an owner-bound start link.
            cached_ok = (
                source.get("public")
                and source.get("inline_public")
                and not source.get("premium_only")
                and not config.get("premium_only")
                and not settings.required_channels
                and not config.get("required_channels")
                and not settings.protect_content
                and not config.get("protect_content")
                and not settings.daily_limit
                and not config.get("daily_limit")
                and settings.public_search
                and not settings.verification_required
                and not record.get("source_protected")
                and record.get("bot_id") == delivery.bot.id
                and record.get("media_type") == "document"
                and record.get("file_id")
            )
            if cached_ok:
                try:
                    await delivery.check_file(query.from_user.id, record["_id"])
                except DeliveryError:
                    continue
                results.append(
                    InlineQueryResultCachedDocument(
                        id=record["_id"],
                        title=str(record.get("filename", "Media"))[:100],
                        document_file_id=record["file_id"],
                    )
                )
                continue
            token = await store.create_link(
                [record["_id"]],
                owner_id=query.from_user.id,
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
            results.append(
                InlineQueryResultArticle(
                    id=record["_id"],
                    title=str(record.get("filename", "Media"))[:100],
                    description=size_label(record.get("file_size"))
                    + " · Open private delivery",
                    input_message_content=InputTextMessageContent(
                        message_text="🎬 Open your private file delivery in the bot."
                    ),
                    reply_markup=keyboard(
                        [
                            [
                                button(
                                    "📁 Open file",
                                    url=f"https://t.me/{username}?start=link_{token}",
                                )
                            ]
                        ]
                    ),
                )
            )
        await query.answer(results, cache_time=0, is_personal=True)

    @router.message(F.text & ~F.text.startswith("/"))
    async def automatic(message: Message):
        if (
            not message.from_user
            or message.from_user.is_bot
            or message.chat.type not in ("private", "group", "supergroup")
        ):
            return
        if message.chat.type != "private":
            cfg = await store.setting(message.chat.id)
            if not cfg.get("enabled", False):
                return
        # Match exact normalized keys; manual filters take precedence over catalog searches.
        normalized = _title(message.text)
        manual = await store.db.filters.find_one(
            {"_id": f"{message.chat.id}:{normalized}"}
        )
        if not manual:
            manual = await store.db.filters.find_one({"_id": f"0:{normalized}"})
        if manual:
            ok, _, reason = await context_access(message.from_user.id, message.chat.id)
            if not ok:
                return
            if await throttled(message.from_user.id):
                return
            from wovie.buttons import render_buttons

            markup = render_buttons(manual.get("buttons"))
            if manual.get("source_chat") and manual.get("message_id"):
                try:
                    await message.bot.copy_message(
                        message.chat.id,
                        manual["source_chat"],
                        manual["message_id"],
                        reply_markup=markup,
                    )
                except (TelegramBadRequest, TelegramForbiddenError):
                    await message.answer(
                        "This saved reply is unavailable. Please notify the administrator."
                    )
            elif manual.get("text"):
                await message.answer(
                    safe(manual["text"][:3500]), parse_mode="HTML", reply_markup=markup
                )
            return
        if len(message.text.strip()) >= 2:
            await search(message, message.text)
        elif message.chat.type == "private":
            await message.answer(
                "Enter at least two characters of a title, or use /help."
            )

    @router.message(F.chat.type == "private")
    async def fallback(message: Message):
        await message.answer("Send a title to search, or use /help to see commands.")

    return router
