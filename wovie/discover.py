"""Scoped series navigation, explicit season batches, and metadata discovery."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import secrets

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message
from pymongo import ReturnDocument

from .delivery import DeliveryError
from .admission import BatchQueueFull, enqueue_batch
from .parser import normalize, parse_query
from .ui import button, keyboard, safe, size_label


def build_router(settings, store, delivery, metadata):
    router = Router(name="discovery")

    async def scope(user_id, chat_id):
        ok, reason = await delivery.allowed(
            user_id, group_id=chat_id if chat_id < 0 else None
        )
        if not ok:
            raise DeliveryError(reason)
        requested = None
        if chat_id < 0:
            cfg = await store.setting(chat_id)
            if not cfg.get("enabled"):
                raise DeliveryError("Discovery is disabled in this group.")
            if user_id not in settings.admins:
                if not await delivery._member(user_id, chat_id):
                    raise DeliveryError("Join this group first.")
                for channel in cfg.get("required_channels") or []:
                    if not await delivery._member(user_id, channel):
                        raise DeliveryError(
                            "Join this group’s required channels first."
                        )
                if cfg.get("premium_only"):
                    user = await store.db.users.find_one({"_id": user_id}) or {}
                    if not user.get("premium_until") or user[
                        "premium_until"
                    ] <= datetime.now(UTC):
                        raise DeliveryError("This group requires an active plan.")
            requested = cfg.get("source_ids") or None
        sources = await delivery.visible_sources(user_id, requested)
        if chat_id < 0:
            sources = [
                s["_id"]
                async for s in store.db.sources.find(
                    {
                        "_id": {"$in": sources},
                        "$or": [{"public": True}, {"_id": chat_id}],
                    }
                )
            ]
        return sources

    async def rate(user_id):
        if user_id in settings.admins:
            return
        now = datetime.now(UTC)
        row = await store.db.rate_limits.find_one_and_update(
            {"_id": f"{user_id}:{now:%Y%m%d%H%M}"},
            {
                "$inc": {"used": 1},
                "$setOnInsert": {"expires_at": now + timedelta(minutes=5)},
            },
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        if row["used"] > settings.rate_limit:
            raise DeliveryError(
                "Please wait until next minute before another discovery request."
            )

    async def collect(query, filters, sources, limit=100, exact_title=True):
        found = []
        page, more = 0, True
        while len(found) < limit and more and page < 25:
            rows, more = await store.search(query, page, filters, source_ids=sources)
            found.extend(
                r
                for r in rows
                if not exact_title or r.get("title_norm") == normalize(query)
            )
            page += 1
            if not rows:
                break
        return found[:limit], more or len(found) > limit

    async def new_session(user_id, chat_id, query, filters, sources, **extra):
        session = {
            "_id": secrets.token_urlsafe(12),
            "owner_id": user_id,
            "chat_id": chat_id,
            "query": query,
            "filters": filters,
            "sources": sources,
            "expires_at": datetime.now(UTC) + timedelta(minutes=30),
            **extra,
        }
        await store.db.discovery_sessions.insert_one(session)
        return session

    async def owned(callback, token):
        session = await store.db.discovery_sessions.find_one(
            {"_id": token, "expires_at": {"$gt": datetime.now(UTC)}}
        )
        if not session or session["owner_id"] != callback.from_user.id:
            raise DeliveryError(
                "This selection expired or belongs to another user. Start a new search."
            )
        current = await scope(callback.from_user.id, session["chat_id"])
        session["sources"] = [s for s in session["sources"] if s in current]
        return session

    async def links(rows, session):
        username = (await delivery.bot.me()).username
        result = []
        for row in rows[:20]:
            token = await store.create_link(
                [row["_id"]],
                owner_id=session["owner_id"],
                expires_at=datetime.now(UTC) + timedelta(days=1),
            )
            await store.db.links.update_one(
                {"_id": token}, {"$set": {"chat_id": session["chat_id"]}}
            )
            label = f"{row.get('quality') or 'File'} · {size_label(row.get('file_size'))} · {row.get('filename', 'Media')}"
            result.append(
                [button(label[:60], url=f"https://t.me/{username}?start=link_{token}")]
            )
        return result

    async def variant_view(session, season=None, episode=None):
        filters = dict(session["filters"])
        if season is not None:
            filters["season"] = season
        if episode is not None:
            filters["episode"] = episode
        rows, more = await collect(
            session["query"], filters, session["sources"], limit=20
        )
        if not rows:
            return "No accessible variants match this selection.", None
        controls = await links(rows, session)
        header = f"🎞 <b>{safe(session['query'])}</b>"
        if season is not None:
            header += f" · Season {season}"
        if episode is not None:
            header += f" · Episode {episode}"
        return header + "\n\nChoose a file." + (
            "\nShowing 20 matches. Add language/quality filters to narrow them."
            if more
            else ""
        ), keyboard(controls)

    async def season_view(session, season):
        filters = {**session["filters"], "season": season}
        rows, more = await collect(
            session["query"], filters, session["sources"], limit=100
        )
        episodes = sorted({r["episode"] for r in rows if r.get("episode") is not None})
        controls = []
        for offset in range(0, min(len(episodes), 60), 4):
            controls.append(
                [
                    button(f"E{ep:02d}", data=f"disc:{session['_id']}:ep:{season}:{ep}")
                    for ep in episodes[offset : offset + 4]
                ]
            )
        controls.append(
            [
                button(
                    "📦 Choose season batch",
                    data=f"disc:{session['_id']}:batch:{season}",
                )
            ]
        )
        controls.append(
            [button("All variants", data=f"disc:{session['_id']}:variants:{season}")]
        )
        body = f"📺 <b>{safe(session['query'])}</b> · Season {season}\n\nChoose an episode or prepare a season batch."
        if more or len(episodes) > 60:
            body += "\nThis view is bounded. Use /search with season/episode filters for additional files."
        return body, keyboard(controls)

    async def prepare_batch(session, season):
        rows, more = await collect(
            session["query"],
            {**session["filters"], "season": season},
            session["sources"],
            limit=101,
        )
        if more or len(rows) > 100:
            raise DeliveryError(
                "More than 100 variants match. Add quality:1080p or language:english before preparing this batch."
            )
        if not rows:
            raise DeliveryError("No accessible files match this season.")
        # Keep one variant per episode. Explicit filters let the user control language/quality.
        ranks = {"1080p": 0, "720p": 1, "2160p": 2, "480p": 3, "360p": 4}
        chosen = {}
        for row in sorted(
            rows, key=lambda r: (ranks.get(r.get("quality"), 5), str(r["_id"]))
        ):
            identity = (row.get("title_norm"), row.get("season"), row.get("episode"))
            chosen.setdefault(identity, row)
        ordered = sorted(
            chosen.values(),
            key=lambda r: (
                r.get("title_norm") or "",
                r.get("episode") is None,
                r.get("episode") or 0,
            ),
        )
        confirm = await new_session(
            session["owner_id"],
            session["chat_id"],
            session["query"],
            {**session["filters"], "season": season},
            session["sources"],
            file_ids=[r["_id"] for r in ordered],
            kind="batch",
        )
        body = (
            f"📦 <b>Season {season} batch</b>\n{len(ordered)} files · {safe(session['query'])}\n\n"
            "One variant per episode; available 1080p is preferred unless you selected a quality. "
            "This is the indexed selection, not a promise that every released episode exists.\n\n"
            "Confirm to send privately. Access rules and limits apply to each file."
        )
        return body, keyboard(
            [
                [
                    button(
                        "Send selected files", data=f"disc:{confirm['_id']}:confirm"
                    ),
                    button("Cancel", data=f"disc:{confirm['_id']}:cancel"),
                ]
            ]
        )

    @router.message(Command("series", "variants", "season"))
    async def catalog(message: Message, command: CommandObject):
        if not message.from_user:
            return
        if not command.args:
            await message.answer(
                "Use <code>/series title</code>, <code>/variants title</code>, or "
                "<code>/season title season:1 language:english</code>.",
                parse_mode="HTML",
            )
            return
        try:
            await rate(message.from_user.id)
            sources = await scope(message.from_user.id, message.chat.id)
            query, filters = parse_query(command.args[:240])
            if not query:
                raise DeliveryError("Include a title to keep this selection precise.")
            rows, more = await collect(query, filters, sources, exact_title=False)
            if not rows:
                raise DeliveryError(
                    "No accessible matches. Search privately for private-channel files or use /request title."
                )
            # Freeze the selected canonical title instead of mixing similarly named series.
            canonical = rows[0].get("title") or query
            session = await new_session(
                message.from_user.id, message.chat.id, canonical, filters, sources
            )
            if command.command == "season":
                if "season" not in filters:
                    raise DeliveryError(
                        "Specify season:N, for example /season Dark season:2."
                    )
                body, markup = await prepare_batch(session, filters["season"])
            elif command.command == "variants":
                body, markup = await variant_view(session)
            else:
                canonical_norm = normalize(canonical)
                seasons = sorted(
                    {
                        r["season"]
                        for r in rows
                        if r.get("season") is not None
                        and r.get("title_norm") == canonical_norm
                    }
                )
                if not seasons:
                    body, markup = await variant_view(session)
                else:
                    controls = [
                        [
                            button(
                                f"Season {season}",
                                data=f"disc:{session['_id']}:season:{season}",
                            )
                        ]
                        for season in seasons[:40]
                    ]
                    body = f"📺 <b>{safe(canonical)}</b>\n\nChoose a season. Use a more precise title if this is not the series you meant."
                    if more:
                        body += "\nThis summary samples 100 matches; /season title season:N selects another season directly."
                    markup = keyboard(controls)
            await message.answer(body, reply_markup=markup, parse_mode="HTML")
        except (DeliveryError, ValueError) as exc:
            await message.answer(safe(exc), parse_mode="HTML")

    @router.callback_query(F.data.startswith("disc:"))
    async def selection(callback: CallbackQuery):
        try:
            parts = callback.data.split(":")
            if len(parts) < 3:
                raise DeliveryError("Invalid selection.")
            session = await owned(callback, parts[1])
            action = parts[2]
            if action == "cancel":
                await store.db.discovery_sessions.delete_one({"_id": session["_id"]})
                await callback.answer("Batch cancelled.")
                return
            if action == "confirm":
                if session.get("kind") != "batch":
                    raise DeliveryError("Invalid batch confirmation.")
                await enqueue_batch(
                    store,
                    {
                        "actor_id": callback.from_user.id,
                        "chat_id": callback.from_user.id,
                        "file_ids": session["file_ids"],
                        "source_chat": session["chat_id"],
                    },
                    key=f"season:{session['_id']}",
                )
                await callback.answer(
                    "Batch queued. Open the bot privately to receive it.",
                    show_alert=True,
                )
                return
            await rate(callback.from_user.id)
            season = int(parts[3])
            if not 0 <= season <= 999:
                raise DeliveryError("Invalid season.")
            if action == "season":
                body, markup = await season_view(session, season)
            elif action == "variants":
                body, markup = await variant_view(session, season)
            elif action == "ep":
                episode = int(parts[4])
                if not 0 <= episode <= 9999:
                    raise DeliveryError("Invalid episode.")
                body, markup = await variant_view(session, season, episode)
            elif action == "batch":
                body, markup = await prepare_batch(session, season)
            else:
                raise DeliveryError("Unknown selection.")
            await callback.answer()
            if isinstance(callback.message, Message):
                await callback.message.edit_text(
                    body, reply_markup=markup, parse_mode="HTML"
                )
        except (DeliveryError, BatchQueueFull, ValueError, IndexError) as exc:
            await callback.answer(str(exc)[:190], show_alert=True)
        except TelegramBadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                raise

    async def card(message, item):
        try:
            enriched = await metadata.detail(item["id"], item.get("kind", "movie"))
            item = {**item, **enriched}
        except Exception:
            pass
        text = f"🎬 <b>{safe(item.get('title', 'Untitled'))}</b> ({safe(item.get('year', ''))})\n⭐ {safe(item.get('rating', '—'))}"
        if item.get("genres"):
            text += "\n" + safe(", ".join(item["genres"][:5]))
        if item.get("cast"):
            text += "\nCast: " + safe(", ".join(item["cast"][:5]))
        text += "\n\n" + safe(
            str(item.get("overview") or "No synopsis available.")[:500]
        )
        text += "\n\n<i>Metadata supplied by TMDB. File availability is separate.</i>"
        controls = []
        if str(item.get("url", "")).startswith("https://www.themoviedb.org/"):
            controls.append(button("Details ↗", url=item["url"]))
        if str(item.get("trailer", "")).startswith("https://www.youtube.com/watch?v="):
            controls.append(button("Trailer ↗", url=item["trailer"]))
        markup = keyboard([controls]) if controls else None
        if str(item.get("poster", "")).startswith("https://image.tmdb.org/"):
            try:
                await message.answer_photo(
                    item["poster"], caption=text, reply_markup=markup, parse_mode="HTML"
                )
                return
            except TelegramBadRequest:
                pass
        await message.answer(text, reply_markup=markup, parse_mode="HTML")

    @router.message(Command("trending", "pick"))
    async def discovery(message: Message, command: CommandObject):
        if not message.from_user:
            return
        try:
            await scope(message.from_user.id, message.chat.id)
            await rate(message.from_user.id)
            items = (
                await metadata.trending()
                if not command.args
                else await metadata.search(command.args[:160])
            )
            if command.command == "pick":
                seen = {
                    row["title_norm"]
                    async for row in store.db.watched.find(
                        {"user_id": message.from_user.id}
                    ).limit(1000)
                }
                items = [r for r in items if normalize(r.get("title", "")) not in seen]
                items = [secrets.choice(items)] if items else []
            if not items:
                await message.answer(
                    "No discovery results. The administrator may need to configure TMDB."
                )
                return
            for item in items[:3]:
                await card(message, item)
        except DeliveryError as exc:
            await message.answer(safe(exc), parse_mode="HTML")
        except Exception:
            await message.answer(
                "Discovery is temporarily unavailable. Catalog search still works."
            )

    @router.message(Command("watched"))
    async def watched(message: Message, command: CommandObject):
        if not message.from_user or message.chat.type != "private":
            await message.answer("Manage /watched privately.")
            return
        ok, reason = await delivery.allowed(message.from_user.id)
        if not ok:
            await message.answer(safe(reason), parse_mode="HTML")
            return
        title = (command.args or "").strip()[:160]
        if not title:
            rows = [
                row
                async for row in store.db.watched.find(
                    {"user_id": message.from_user.id}
                )
                .sort("created_at", -1)
                .limit(20)
            ]
            await message.answer(
                "✅ <b>Watched</b>\n\n"
                + (
                    "\n".join("• " + safe(r["title"]) for r in rows)
                    or "No watched titles yet."
                )
                + "\n\nUse /watched TITLE or /watched remove TITLE.",
                parse_mode="HTML",
            )
            return
        removing = title.lower().startswith("remove ")
        title = title[7:].strip() if removing else title
        identifier = hashlib.sha256(
            f"{message.from_user.id}:{normalize(title)}".encode()
        ).hexdigest()[:32]
        if removing:
            await store.db.watched.delete_one(
                {"_id": identifier, "user_id": message.from_user.id}
            )
            await message.answer("Removed from your watched list.")
        else:
            await store.db.watched.update_one(
                {"_id": identifier},
                {
                    "$setOnInsert": {
                        "user_id": message.from_user.id,
                        "title": title,
                        "title_norm": normalize(title),
                        "created_at": datetime.now(UTC),
                    }
                },
                upsert=True,
            )
            await message.answer(
                f"✅ Marked watched: <b>{safe(title)}</b>", parse_mode="HTML"
            )

    @router.message(Command("browse"))
    async def browse(message: Message, command: CommandObject):
        if not message.from_user:
            return
        try:
            await rate(message.from_user.id)
            sources = await scope(message.from_user.id, message.chat.id)
            raw = (command.args or "").strip()[:160]
            if raw in ("language", "quality", "year"):
                raise DeliveryError(
                    "Choose a value: /browse language:english, /browse quality:1080p, or /browse year:2024."
                )
            parts = raw.split(maxsplit=1)
            if (
                len(parts) == 2
                and parts[0] in ("language", "quality", "year")
                and ":" not in parts[1]
            ):
                raw = ":".join(parts)
            title, filters = parse_query(raw)
            if title or set(filters) - {"language", "quality", "year"}:
                raise DeliveryError(
                    "Browse accepts language, quality and year filters. Example: /browse language:english quality:1080p year:2024."
                )
            rows, more = await collect(
                "", filters, sources, limit=80, exact_title=False
            )
            distinct = {}
            for row in rows:
                distinct.setdefault((row.get("title_norm"), row.get("year")), row)
                if len(distinct) >= 12:
                    break
            selected = list(distinct.values())
            if not selected:
                raise DeliveryError(
                    "No accessible titles match those filters. Private source browsing is available in private chat."
                )
            session = await new_session(
                message.from_user.id, message.chat.id, "", filters, sources
            )
            controls = await links(selected, session)
            await message.answer(
                "🗂 <b>Browse your catalog</b>\nDistinct titles from up to 80 recent matches. "
                "Each button opens one available file. Use /variants TITLE for other releases.",
                reply_markup=keyboard(controls),
                parse_mode="HTML",
            )
        except (DeliveryError, ValueError) as exc:
            await message.answer(safe(exc), parse_mode="HTML")

    @router.message(Command("suggest"))
    async def suggest(message: Message, command: CommandObject):
        if not message.from_user:
            return
        try:
            await rate(message.from_user.id)
            sources = await scope(message.from_user.id, message.chat.id)
            query = (command.args or "").strip()[:160]
            if not query:
                raise DeliveryError(
                    "Use /suggest TITLE for close catalog-title suggestions."
                )
            titles = await store.suggest_titles(query, source_ids=sources, limit=5)
            body = (
                "\n".join(f"• <code>{safe(title)}</code>" for title in titles)
                if titles
                else "No close accessible titles found. Try an alternate spelling or /request TITLE."
            )
            await message.answer(
                "💡 <b>Title suggestions</b>\n\n"
                + body
                + ("\n\nCopy a title into /search to see its files." if titles else ""),
                parse_mode="HTML",
            )
        except (DeliveryError, ValueError) as exc:
            await message.answer(safe(exc), parse_mode="HTML")

    @router.message(Command("popular"))
    async def popular(message: Message):
        if not message.from_user:
            return
        try:
            await rate(message.from_user.id)
            visible = await scope(message.from_user.id, message.chat.id)
            config = await store.setting(0)
            if not config.get("popularity_enabled", False):
                raise DeliveryError(
                    "Popular-file discovery is disabled. The owner can opt in with /set 0 popularity_enabled true."
                )
            sources = [
                s["_id"]
                async for s in store.db.sources.find(
                    {"_id": {"$in": visible}, "public": True, "active": True}
                )
            ]
            pipeline = [
                {
                    "$match": {
                        "state": "sent",
                        "count_for_popularity": True,
                        "source_chat": {"$in": sources},
                        "sent_at": {"$gte": datetime.now(UTC) - timedelta(days=30)},
                    }
                },
                {"$sort": {"sent_at": -1}},
                {"$limit": 10000},
                {"$group": {"_id": "$selected_file_id", "deliveries": {"$sum": 1}}},
                {"$match": {"deliveries": {"$gte": 3}}},
                {"$sort": {"deliveries": -1, "_id": 1}},
                {"$limit": 40},
            ]
            cursor = await store.db.delivery_receipts.aggregate(
                pipeline, maxTimeMS=5000, allowDiskUse=False
            )
            selected, distinct = [], set()
            try:
                async for row in cursor:
                    record = await store.get_file(row["_id"])
                    if not record or record["source_chat"] not in sources:
                        continue
                    title_key = (record.get("title_norm"), record.get("year"))
                    if title_key in distinct:
                        continue
                    distinct.add(title_key)
                    selected.append(record)
                    if len(selected) == 12:
                        break
            finally:
                await cursor.close()
            if not selected:
                raise DeliveryError(
                    "No eligible public titles yet. Popularity needs at least three opted-in successful deliveries per file."
                )
            session = await new_session(
                message.from_user.id, message.chat.id, "", {}, sources
            )
            controls = await links(selected, session)
            await message.answer(
                "🔥 <b>Popular public titles</b>\nRanked from opted-in public deliveries in the last 30 days "
                "(latest 10,000 receipts). Private catalog activity and query history are excluded.",
                reply_markup=keyboard(controls),
                parse_mode="HTML",
            )
        except DeliveryError as exc:
            await message.answer(safe(exc), parse_mode="HTML")

    @router.message(Command("resultschannel"))
    async def results_channel(message: Message, command: CommandObject):
        if not message.from_user or message.from_user.id not in settings.admins:
            return
        parts = (command.args or "").split()
        if len(parts) != 2:
            await message.answer(
                "Use <code>/resultschannel GROUP_ID CHANNEL_ID</code> or <code>/resultschannel GROUP_ID off</code>.",
                parse_mode="HTML",
            )
            return
        try:
            group_id = int(parts[0])
            if group_id >= 0:
                raise ValueError
            channel_id = None if parts[1].lower() == "off" else int(parts[1])
            if channel_id:
                chat = await delivery.bot.get_chat(channel_id)
                member = await delivery.bot.get_chat_member(channel_id, delivery.bot.id)
                if chat.type != "channel" or member.status not in (
                    "administrator",
                    "creator",
                ):
                    raise DeliveryError(
                        "Choose a channel where the bot is an administrator."
                    )
                if member.status == "administrator" and not getattr(
                    member, "can_post_messages", False
                ):
                    raise DeliveryError(
                        "The bot needs permission to post messages in the results channel."
                    )
            await store.set_setting(group_id, "results_channel", channel_id)
            await message.answer(
                "Results destination updated. Only published-source results are routed there, and the requester must be a member."
            )
        except (
            ValueError,
            TelegramBadRequest,
            TelegramForbiddenError,
            DeliveryError,
        ) as exc:
            await message.answer(
                safe(
                    str(exc)
                    if isinstance(exc, DeliveryError)
                    else "Check numeric IDs and the bot’s channel permissions."
                ),
                parse_mode="HTML",
            )

    return router
