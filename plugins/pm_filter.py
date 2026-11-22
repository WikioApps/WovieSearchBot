"""Private/group auto-filter, release facets, Telegram delivery and watchlist."""

import asyncio
import secrets
from datetime import timedelta
from html import escape
from math import ceil

from pyrogram import enums, types
from pyrogram.errors import (
    RPCError,
    UserNotParticipant,
)

from database.ia_filterdb import now, preferred_filters
from info import DELETE_AFTER, LANGUAGES, QUALITIES
from plugins.file_store import INVALID_HANDLE, MissingPost, handles
from Script import button, caps, edit_panel, head, keys, release, size

FACETS = {
    "language": "🌐",
    "quality": "📺",
    "year": "📅",
    "season": "🍿",
    "episode": "🎞",
}


def link(app, payload):
    return f"https://t.me/{app.me.username}?start={payload}"


def allowed(chat, cfg):
    return (
        cfg["pm_search"]
        if chat > 0
        else cfg["group_search"]
        and (not cfg["search_groups"] or chat in cfg["search_groups"])
    )


async def missing_membership(app, uid, cfg):
    for channel in cfg["force_sub"]:
        try:
            member = await app.get_chat_member(channel, uid)
        except UserNotParticipant:
            return channel
        if member.status in {
            enums.ChatMemberStatus.LEFT,
            enums.ChatMemberStatus.BANNED,
        } or (
            member.status == enums.ChatMemberStatus.RESTRICTED and not member.is_member
        ):
            return channel
    return None


async def gate(app, uid, cfg):
    missing = await missing_membership(app, uid, cfg)
    if missing:
        try:
            chat = await app.get_chat(missing)
            invite = (
                f"https://t.me/{chat.username}" if chat.username else chat.invite_link
            )
        except RPCError:
            invite = None
        return (
            head("🔐", "Join to continue")
            + "\n\nJoin the required channel, then repeat your search or open the file again.",
            keys(
                [button("📢 ᴊᴏɪɴ", url=invite or cfg["updates_link"])]
                if invite or cfg["updates_link"]
                else [button("‹ ʜᴇʟᴘ", "nav:help")]
            ),
        )
    return None


async def start_search(app, m, query, cfg):
    uid = m.from_user.id
    if not allowed(m.chat.id, cfg):
        return await m.reply_text(
            "Search is disabled in this chat.", reply_markup=keys()
        )
    if block := await gate(app, uid, cfg):
        return await m.reply_text(block[0], reply_markup=block[1])
    query = query.strip()
    if not 1 <= len(query) <= 120:
        raise ValueError("Send a title between 1 and 120 characters.")
    pref = await app.db.config(uid)
    key = await app.db.session(uid, query, pref, m.chat.id)
    s = await app.db.sessions.find_one({"_id": key})
    text, markup = await render(app, s, cfg, pref, 0, refresh=True)
    if s.get("hits"):
        first = s["hits"][0]
        await app.db.db.eva_trending.update_one(
            {"_id": first["_id"]},
            {
                "$inc": {"count": 1},
                "$set": {
                    "name": first["name"],
                    "source": first["source"],
                    "expires": now() + timedelta(days=7),
                },
            },
            upsert=True,
        )
    await m.reply_text(
        text,
        reply_markup=markup,
        link_preview_options=types.LinkPreviewOptions(is_disabled=True),
    )


async def render(app, s, cfg, pref, page, refresh=False):
    key = s["_id"]
    if refresh or s.get("version") != 2 or "hits" not in s:
        rows, suggestions = await app.db.search(
            s["query"], s["filters"], cfg["sources"], pref["sort"], cfg["spell_check"]
        )
        s.update(
            version=2, hits=rows[:100], more=len(rows) > 100, suggestions=suggestions
        )
        await app.db.sessions.update_one(
            {"_id": key},
            {"$set": {k: s[k] for k in ("hits", "more", "suggestions", "version")}},
        )
    hits = [r for r in s["hits"] if r["source"] in cfg["sources"]]
    title = head("🎬", "Auto filter") + "\n\n🔎 <b>" + escape(s["query"]) + "</b>"
    if s["filters"]:
        title += "\n" + " · ".join(escape(str(v)) for v in s["filters"].values())
    if not hits:
        rows = [
            [button("🔍 " + name, f"s:{key}:try:{i}")]
            for i, name in enumerate(s.get("suggestions", []))
        ]
        if s["filters"]:
            rows.append([button("↺ ᴄʟᴇᴀʀ ꜰɪʟᴛᴇʀꜱ", f"s:{key}:clear")])
        return title + "\n\n" + (
            "Did you mean…"
            if rows and s.get("suggestions")
            else "No editions found. Try a shorter title or request it."
        ), keys(*rows, [button("📮 ʀᴇǫᴜᴇꜱᴛ", f"s:{key}:request")])
    n = cfg["page_size"]
    pages = ceil(len(hits) / n)
    page = max(0, min(page, pages - 1))
    title += (
        f"\n{len(hits)}{'+' if s['more'] else ''} editions · Page {page + 1}/{pages}"
    )
    keyboard = []
    layout = cfg["layout"] if pref["layout"] == "default" else pref["layout"]
    for i, r in enumerate(hits[page * n : (page + 1) * n], page * n + 1):
        label = release(r["name"])[:53] + (
            f"  ·  {size(r['bytes'])}" if pref["show_size"] else ""
        )
        url = link(app, "f_" + r["_id"])
        if layout == "buttons":
            keyboard.append([button(label[:72], url=url, style="success")])
        else:
            title += f'\n{i}. <a href="{url}">{escape(label[:100])}</a>'
    navigation = []
    if page:
        navigation.append(button("‹ ᴘʀᴇᴠ", f"s:{key}:page:{page - 1}"))
    navigation.append(button(f"{page + 1} / {pages}", f"s:{key}:page:{page}"))
    if page + 1 < pages:
        navigation.append(button("ɴᴇxᴛ ›", f"s:{key}:page:{page + 1}", style="success"))
    keyboard.extend(
        [
            navigation,
            [
                button("🎚 ꜰɪʟᴛᴇʀꜱ", f"s:{key}:menu"),
                button(
                    "📦 ɢᴇᴛ ᴘᴀɢᴇ", url=link(app, f"p_{key}_{page}"), style="success"
                ),
            ],
        ]
    )
    return title, keys(*keyboard)


async def get_session(app, key, uid):
    s = await app.db.sessions.find_one(
        {"_id": key, "owner": uid, "expires": {"$gt": now()}}
    )
    if not s:
        raise ValueError(
            "This search expired or belongs to another user. Send your own title."
        )
    return s


async def search_action(app, q, parts, cfg):
    key, verb, *args = parts
    uid = q.from_user.id
    s = await get_session(app, key, uid)
    if not allowed(q.message.chat.id, cfg):
        raise ValueError("Search is disabled in this chat.")
    if block := await gate(app, uid, cfg):
        return await q.message.reply_text(block[0], reply_markup=block[1])
    pref = await app.db.config(uid)
    page = 0
    refresh = False
    if verb == "menu":
        options = [
            button(f"{icon} {caps(k)}", f"s:{key}:facet:{k}")
            for k, icon in FACETS.items()
        ]
        return await q.message.edit_text(
            head("🎚", "Release filters") + "\n\n" + escape(s["query"]),
            reply_markup=keys(
                options[:2],
                options[2:],
                [
                    button("↺ ᴄʟᴇᴀʀ", f"s:{key}:clear"),
                    button("‹ ʀᴇꜱᴜʟᴛꜱ", f"s:{key}:page:0"),
                ],
            ),
        )
    if verb == "facet":
        field = args[0]
        if field not in FACETS:
            raise ValueError("Unknown filter.")
        if field == "language":
            choices = LANGUAGES
        elif field == "quality":
            choices = QUALITIES[:-1]
        else:
            choices = sorted(
                {
                    r[field]
                    for r in s.get("hits", [])
                    if r["source"] in cfg["sources"] and r[field]
                },
                key=int,
            )[:75]
        options = [button(caps(v), f"s:{key}:set:{field}:{v}") for v in choices]
        return await q.message.edit_text(
            head(FACETS[field], field)
            + "\n\nSelect a value, or type it in a new search.",
            reply_markup=keys(
                *[options[i : i + 4] for i in range(0, len(options), 4)],
                [
                    button("✕ ᴀɴʏ", f"s:{key}:set:{field}:any"),
                    button("‹ ʙᴀᴄᴋ", f"s:{key}:menu"),
                ],
            ),
        )
    if verb == "request":
        from plugins.commands import request_title

        return await request_title(app, q.message, uid, s["query"], cfg)
    if verb == "page":
        page = int(args[0])
    elif verb in {"set", "clear", "try"}:
        if verb == "set":
            field, v = args
            if field not in FACETS:
                raise ValueError("Unknown filter.")
            if v == "any":
                s["filters"].pop(field, None)
            else:
                s["filters"][field] = v
        if verb == "clear":
            s["filters"] = {}
        if verb == "try":
            s["query"] = s["suggestions"][int(args[0])]
        await app.db.sessions.update_one(
            {"_id": key}, {"$set": {"filters": s["filters"], "query": s["query"]}}
        )
        refresh = True
    text, markup = await render(app, s, cfg, pref, page, refresh)
    await q.message.edit_text(
        text,
        reply_markup=markup,
        link_preview_options=types.LinkPreviewOptions(is_disabled=True),
    )


async def entry(app, key, cfg):
    d = await app.db.file(key)
    if not d or d["source"] not in cfg["sources"]:
        raise ValueError("This file is no longer in the active catalog.")
    return d


async def file_card(app, m, key, cfg):
    uid = m.from_user.id
    if block := await gate(app, uid, cfg):
        return await m.reply_text(block[0], reply_markup=block[1])
    d = await entry(app, key, cfg)
    from plugins.media import details, stream_buttons

    meta = await details(app, d, cfg)
    rows = await stream_buttons(app, uid, key, cfg)
    text = (
        head("🎞", "File details")
        + "\n\n<b>"
        + escape(release(d["name"]))
        + "</b>\n"
        + escape(
            " · ".join(
                filter(
                    None, (size(d["bytes"]), d["quality"], *d["language"], d["year"])
                )
            )
        )
    )
    text += "\n\n" + expiry_note(cfg)
    if meta:
        text += "\n\n" + meta["text"]
    markup = keys(
        [
            button("📥 ɢᴇᴛ ꜰɪʟᴇ", f"f:get:{key}", style="success"),
            button("⭐ ꜱᴀᴠᴇ", f"f:save:{key}", style="success"),
        ],
        rows,
        file_links(cfg),
    )
    if meta and meta["poster"]:
        try:
            return await m.reply_photo(
                meta["poster"], caption=text, reply_markup=markup
            )
        except RPCError:
            pass
    await m.reply_text(
        text,
        reply_markup=markup,
        link_preview_options=types.LinkPreviewOptions(is_disabled=True),
    )


def file_links(cfg):
    return [
        button(label, url=cfg[key])
        for key, label in (
            ("updates_link", "📢 ᴜᴘᴅᴀᴛᴇꜱ"),
            ("support_link", "💬 ꜱᴜᴘᴘᴏʀᴛ"),
        )
        if cfg[key]
    ]


def expiry_note(cfg):
    return (
        "⏱ This copy expires in 5 minutes. Reopen its file card if needed."
        if cfg["protect_files"]
        else "⏱ Forward to Saved Messages within 5 minutes to keep this file."
    )


async def deliver(app, uid, d, cfg):
    caption = cfg["caption"].format(
        filename=release(d["name"]),
        title=release(d["name"]),
        size=size(d["bytes"]),
        quality=d["quality"] or "—",
        language=", ".join(d["language"]) or "—",
    )[:840]
    first, sep, rest = caption.partition("\n")
    options = {
        "caption": "<b>"
        + escape(first)
        + "</b>"
        + sep
        + escape(rest)
        + "\n\n"
        + expiry_note(cfg),
        "reply_markup": keys(file_links(cfg)),
        "protect_content": cfg["protect_files"],
    }
    sent = None
    async for handle in handles(app, d, cfg):
        try:
            sent = await app.send_cached_media(uid, handle, **options)
        except INVALID_HANDLE:
            continue
        break
    if sent is None:
        raise ValueError(
            "Telegram did not confirm delivery. Check your chat before trying again."
        )
    try:
        await app.db.deletions.insert_one(
            {
                "_id": f"{uid}:{sent.id}",
                "uid": uid,
                "mid": sent.id,
                "due": now() + timedelta(seconds=DELETE_AFTER),
            }
        )
    except Exception:
        # If scheduling fails, retract the temporary copy while we still know its ID.
        await app.delete_messages(uid, sent.id)
        raise


async def file_action(app, q, verb, key, cfg):
    uid = q.from_user.id
    if q.message.chat.id != uid:
        raise ValueError("Open the file in a private chat.")
    bookmark = {"_id": f"{uid}:{key}"}
    if verb == "remove":
        await app.db.saved.delete_one(bookmark)
        return await show_saved(app, q.message, uid, edit=True)
    if block := await gate(app, uid, cfg):
        return await q.message.reply_text(block[0], reply_markup=block[1])
    d = await entry(app, key, cfg)
    if verb == "save":
        await app.db.saved.update_one(
            bookmark,
            {
                "$set": {
                    "user": uid,
                    "key": key,
                    "name": d["name"],
                    "source": d["source"],
                    "time": now(),
                }
            },
            upsert=True,
        )
        return await q.message.edit_reply_markup(
            reply_markup=keys(
                [
                    button("📥 ɢᴇᴛ ꜰɪʟᴇ", f"f:get:{key}", style="success"),
                    button("★ ꜱᴀᴠᴇᴅ", "nav:saved", style="success"),
                ],
                *[
                    row
                    for row in q.message.reply_markup.inline_keyboard[1:]
                    if row and row[0].url
                ],
            )
        )
    if verb == "get":
        await deliver(app, uid, d, cfg)


async def show_saved(app, m, uid, page=0, *, edit=False):
    if m.chat.id != uid:
        raise ValueError("Open /saved in private.")
    cfg = await app.db.config()
    if block := await gate(app, uid, cfg):
        return await m.reply_text(block[0], reply_markup=block[1])
    rows = (
        await app.db.saved.find({"user": uid, "source": {"$in": cfg["sources"]}})
        .sort("time", -1)
        .skip(page * 8)
        .limit(9)
        .to_list(9)
    )
    items = [
        [
            button(
                release(r["name"])[:64], url=link(app, "f_" + r["key"]), style="success"
            ),
            button("✕", f"f:remove:{r['key']}", style="danger"),
        ]
        for r in rows[:8]
    ]
    nav = []
    if page:
        nav.append(button("‹ ᴘʀᴇᴠ", f"nav:saved:{page - 1}"))
    if len(rows) > 8:
        nav.append(button("ɴᴇxᴛ ›", f"nav:saved:{page + 1}", style="success"))
    text = (
        head("⭐", "My watchlist")
        + "\n\n"
        + (
            "Tap a title to open its file card."
            if rows
            else "Save a file from its details card."
        )
    )
    markup = keys(*items, nav, [button("‹ ʜᴏᴍᴇ", "nav:home")])
    if edit:
        return await edit_panel(m, text, markup)
    await m.reply_text(text, reply_markup=markup)


async def send_page(app, m, key, page, cfg):
    uid = m.from_user.id
    s = await get_session(app, key, uid)
    if not allowed(s.get("chat", uid), cfg):
        raise ValueError("Search is disabled in the original chat.")
    if block := await gate(app, uid, cfg):
        return await m.reply_text(block[0], reply_markup=block[1])
    if s.get("version") != 2:
        await render(app, s, cfg, await app.db.config(uid), page, refresh=True)
    n = cfg["page_size"]
    hits = [r for r in s.get("hits", []) if r["source"] in cfg["sources"]]
    rows = hits[page * n : (page + 1) * n]
    if not rows:
        raise ValueError("This page is empty.")
    sent = 0
    for r in rows:
        try:
            await deliver(app, uid, await entry(app, r["_id"], cfg), cfg)
        except ValueError:
            continue
        sent += 1
    await m.reply_text(
        f"📦 Delivered {sent} of {len(rows)} files.", reply_markup=keys()
    )


async def share(app, m, cmd, arg, cfg):
    from plugins.index import boundary

    async def resolve(ref):
        source, mid, _ = boundary(ref)
        if isinstance(source, str):
            source = (await app.get_chat(source)).id
        if source not in cfg["sources"] or not 0 < mid < 2**31:
            raise ValueError("Choose a post from an enabled catalog source.")
        return source, mid

    if cmd == "link":
        post = m.reply_to_message
        if arg:
            source, mid = await resolve(arg)
        elif (
            post
            and getattr(post.forward_origin, "chat", None)
            and getattr(post.forward_origin, "message_id", None)
        ):
            source, mid = post.forward_origin.chat.id, post.forward_origin.message_id
        elif post and m.chat.id < 0:
            source, mid = m.chat.id, post.id
        else:
            raise ValueError(
                "Use /link POST_LINK, /link CHAT_ID POST_ID, or reply to an indexed source post."
            )
        key = f"{abs(source):x}_{mid:x}"
        await entry(app, key, cfg)
        url = link(app, "f_" + key)
    else:
        parts = arg.split()
        if len(parts) == 3 and all(p.isdecimal() for p in parts[1:]):
            source, first = await resolve(" ".join(parts[:2]))
            last = int(parts[2])
        elif len(parts) == 2 and all(p.startswith("https://t.me/") for p in parts):
            source, first = await resolve(parts[0])
            other, last = await resolve(parts[1])
            if source != other:
                raise ValueError("Both posts must belong to the same source.")
        else:
            raise ValueError(
                "Use /batch CHAT_ID FIRST_ID LAST_ID, or /batch FIRST_POST_LINK LAST_POST_LINK."
            )
        if not first <= last < min(first + 100, 2**31):
            raise ValueError("Choose an ascending range of up to 100 source posts.")
        keys_in_range = [f"{abs(source):x}_{mid:x}" for mid in range(first, last + 1)]
        indexed = {}
        for coll in app.db.shards.values():
            async for row in coll.database.eva_posts.find(
                {"_id": {"$in": keys_in_range}}, {"media": 1}
            ):
                indexed[row["_id"]] = row["media"]
        items, seen = [], set()
        for key in keys_in_range:
            if key in indexed and indexed[key] not in seen:
                items.append({"key": key, "media": indexed[key]})
                seen.add(indexed[key])
        if not items:
            raise ValueError("This range has no indexed files.")
        token = secrets.token_hex(12)
        await app.db.db.eva_batches.insert_one({"_id": token, "items": items})
        url = link(app, "b_" + token)
    await m.reply_text(
        url,
        reply_markup=keys([button("📦 ᴏᴘᴇɴ", url=url, style="success")]),
        link_preview_options=types.LinkPreviewOptions(is_disabled=True),
    )


async def shared_batch(app, m, uid, token, page, cfg, *, edit=False, send=False):
    if m.chat.id != uid:
        raise ValueError("Open the batch in your private chat.")
    if block := await gate(app, uid, cfg):
        return await m.reply_text(block[0], reply_markup=block[1])
    batch = await app.db.db.eva_batches.find_one({"_id": token})
    n = cfg["page_size"]
    if not batch or not 0 <= page < ceil(len(batch["items"]) / n):
        raise ValueError("This batch page is unavailable.")
    items = batch["items"][page * n : (page + 1) * n]
    docs = await asyncio.gather(*(app.db.file(item["key"]) for item in items))
    rows = [
        d
        for d, item in zip(docs, items)
        if d and d["source"] in cfg["sources"] and d["media"] == item["media"]
    ]
    if send:
        count = 0
        for row in rows:
            try:
                await deliver(app, uid, row, cfg)
                count += 1
            except MissingPost:
                continue
        return await m.reply_text(
            f"📦 Delivered {count} of {len(items)} batch files.", reply_markup=keys()
        )
    pages = ceil(len(batch["items"]) / n)
    nav = []
    if page:
        nav.append(button("‹ ᴘʀᴇᴠ", f"b:view:{token}:{page - 1}"))
    if page + 1 < pages:
        nav.append(button("ɴᴇxᴛ ›", f"b:view:{token}:{page + 1}", style="success"))
    markup = keys(
        *[
            [button(release(d["name"])[:64], url=link(app, "f_" + d["_id"]))]
            for d in rows
        ],
        [button("📥 ɢᴇᴛ ᴘᴀɢᴇ", f"b:get:{token}:{page}", style="success")]
        if rows
        else [],
        nav,
    )
    text = (
        head("📦", "File batch")
        + f"\nPage {page + 1} of {pages} · {len(rows)} available on this page."
    )
    if edit:
        return await edit_panel(m, text, markup)
    await m.reply_text(text, reply_markup=markup)


async def inline(app, q, cfg):
    results = []
    if (
        q.query.strip()
        and cfg["pm_search"]
        and not cfg["search_groups"]
        and not await missing_membership(app, q.from_user.id, cfg)
    ):
        prefs = await app.db.config(q.from_user.id)
        filters = preferred_filters(q.query[:120], prefs)
        hits, _ = await app.db.search(
            q.query[:120], filters, cfg["sources"], prefs["sort"], limit=20
        )
        for d in hits:
            results.append(
                types.InlineQueryResultArticle(
                    id=d["_id"],
                    title=release(d["name"]),
                    description=size(d["bytes"]),
                    input_message_content=types.InputTextMessageContent(
                        head("🎞", "File details") + "\n" + escape(release(d["name"]))
                    ),
                    reply_markup=keys(
                        [
                            button(
                                "📥 ᴏᴘᴇɴ ꜰɪʟᴇ",
                                url=link(app, "f_" + d["_id"]),
                                style="success",
                            )
                        ],
                        close=False,
                    ),
                )
            )
    await q.answer(results, cache_time=0, is_personal=True)
