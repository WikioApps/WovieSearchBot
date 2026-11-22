"""Private/group auto-filter, release facets, Telegram delivery and watchlist."""

from datetime import timedelta
from html import escape
from math import ceil

from pyrogram import enums, types
from pyrogram.errors import RPCError, UserNotParticipant

from database.ia_filterdb import now, preferred_filters
from info import LANGUAGES, QUALITIES
from Script import button, caps, head, keys, size

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
        return await m.reply_text("Search is disabled in this chat.")
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
    if refresh or "hits" not in s:
        rows, suggestions = await app.db.search(
            s["query"], s["filters"], cfg["sources"], pref["sort"], cfg["spell_check"]
        )
        s.update(hits=rows[:100], more=len(rows) > 100, suggestions=suggestions)
        await app.db.sessions.update_one(
            {"_id": key}, {"$set": {k: s[k] for k in ("hits", "more", "suggestions")}}
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
        label = (f"[{size(r['bytes'])}] " if pref["show_size"] else "") + r["name"]
        url = link(app, "f_" + r["_id"])
        if layout == "buttons":
            keyboard.append([button(label[:72], url=url)])
        else:
            title += f'\n{i}. <a href="{url}">{escape(label[:100])}</a>'
    navigation = []
    if page:
        navigation.append(button("‹ ᴘʀᴇᴠ", f"s:{key}:page:{page - 1}"))
    navigation.append(button(f"{page + 1} / {pages}", f"s:{key}:page:{page}"))
    if page + 1 < pages:
        navigation.append(button("ɴᴇxᴛ ›", f"s:{key}:page:{page + 1}"))
    keyboard.extend(
        [
            navigation,
            [
                button("🎚 ꜰɪʟᴛᴇʀꜱ", f"s:{key}:menu"),
                button("📦 ɢᴇᴛ ᴘᴀɢᴇ", url=link(app, f"p_{key}_{page}")),
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
        + escape(d["name"])
        + "</b>\n"
        + escape(
            " · ".join(
                filter(
                    None, (size(d["bytes"]), d["quality"], *d["language"], d["year"])
                )
            )
        )
    )
    if meta:
        text += "\n\n" + meta["text"]
    markup = keys(
        [
            button("📥 ɢᴇᴛ ꜰɪʟᴇ", f"f:get:{key}", style="success"),
            button("⭐ ꜱᴀᴠᴇ", f"f:save:{key}"),
        ],
        rows,
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


async def original(app, d):
    # Recheck the original post. Deleted or newly protected media may still have a valid cached ID.
    source = await app.get_chat(d["source"])
    if source.has_protected_content:
        raise ValueError("This source now protects its content.")
    post = await app.get_messages(source.id, d["mid"], replies=0)
    if (
        not post
        or post.empty
        or not post.chat
        or post.chat.id != source.id
        or post.has_protected_content
    ):
        raise ValueError("The original file is unavailable.")
    media = next(
        (v for v in (post.document, post.video, post.audio, post.animation) if v), None
    )
    if not media:
        raise ValueError("The original post no longer contains a file.")
    return post, media


async def deliver(app, uid, d, cfg):
    _, media = await original(app, d)
    caption = cfg["caption"].format(
        filename=d["name"],
        title=d["name"],
        size=size(d["bytes"]),
        quality=d["quality"] or "—",
        language=", ".join(d["language"]) or "—",
    )[:1000]
    sent = await app.send_cached_media(
        uid,
        media.file_id,
        caption=escape(caption),
        protect_content=cfg["protect_files"],
    )
    if cfg["auto_delete"]:
        await app.db.deletions.insert_one(
            {
                "uid": uid,
                "mid": sent.id,
                "due": now() + timedelta(seconds=cfg["auto_delete"]),
            }
        )


async def file_action(app, q, verb, key, cfg):
    uid = q.from_user.id
    if q.message.chat.id != uid:
        raise ValueError("Open the file in a private chat.")
    bookmark = {"_id": f"{uid}:{key}"}
    if verb == "remove":
        await app.db.saved.delete_one(bookmark)
        return await show_saved(app, q.message, uid)
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
        return await q.message.reply_text(head("⭐", "Saved to watchlist"))
    if verb == "get":
        await deliver(app, uid, d, cfg)


async def show_saved(app, m, uid, page=0):
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
            button(r["name"][:64], url=link(app, "f_" + r["key"])),
            button("✕", f"f:remove:{r['key']}"),
        ]
        for r in rows[:8]
    ]
    nav = []
    if page:
        nav.append(button("‹ ᴘʀᴇᴠ", f"nav:saved:{page - 1}"))
    if len(rows) > 8:
        nav.append(button("ɴᴇxᴛ ›", f"nav:saved:{page + 1}"))
    await m.reply_text(
        head("⭐", "My watchlist")
        + (
            "\n\nTap a title to open its file card."
            if rows
            else "\n\nSave a file from its details card."
        ),
        reply_markup=keys(*items, nav, [button("✕ ᴄʟᴏꜱᴇ", "nav:close")]),
    )


async def send_page(app, m, key, page, cfg):
    uid = m.from_user.id
    s = await get_session(app, key, uid)
    from plugins.settings import group_config

    cfg = await group_config(app, s.get("chat", uid), cfg)
    if not allowed(s.get("chat", uid), cfg):
        raise ValueError("Search is disabled in the original chat.")
    if block := await gate(app, uid, cfg):
        return await m.reply_text(block[0], reply_markup=block[1])
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
    await m.reply_text(f"📦 Delivered {sent} of {len(rows)} files.")


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
                    title=d["name"],
                    description=size(d["bytes"]),
                    input_message_content=types.InputTextMessageContent(
                        head("🎞", "File details") + "\n" + escape(d["name"])
                    ),
                    reply_markup=keys(
                        [button("📥 ᴏᴘᴇɴ ꜰɪʟᴇ", url=link(app, "f_" + d["_id"]))]
                    ),
                )
            )
    await q.answer(results, cache_time=0, is_personal=True)
