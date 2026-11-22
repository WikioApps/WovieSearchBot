"""Forward-to-index workflow adapted from Nova-Filter-Bot (GPL-3.0).
Index with the bot's own membership and checkpoint whole message-ID batches.
"""

import asyncio
import logging
import re
import secrets
from datetime import timedelta
from html import escape

from pymongo import ReplaceOne
from pyrogram import enums
from pyrogram.errors import FloodWait, RPCError

from database.ia_filterdb import file_doc, now
from Script import button, head, keys

log = logging.getLogger(__name__)
ACTIVE = {"$in": ["queued", "running"]}
INDEX_HELP = (
    "Forward the <b>last post</b> from your source here, or send its Telegram message link. "
    "For a group, reply to the last post there with /index. The bot must be a source admin.\n\n"
    "<code>/index -1001234567890 2400 1000</code> scans IDs 1001–2400. Use /cancel to leave."
)


def boundary(text):
    link = re.fullmatch(
        r"https?://(?:www\.)?t\.me/(c/\d+|[A-Za-z][A-Za-z0-9_]{3,31})/(?:\d+/)?(\d+)(?:\?[^\s]*)?",
        text,
    )
    if link:
        source, mid = link.groups()
        return (
            (int("-100" + source[2:]) if source.startswith("c/") else source),
            int(mid),
            0,
        )
    raw = re.fullmatch(r"(-\d+|@[A-Za-z][A-Za-z0-9_]+)\s+(\d+)(?:\s+(\d+))?", text)
    if raw:
        source, mid, skip = raw.groups()
        return (
            (int(source) if source.startswith("-") else source),
            int(mid),
            int(skip or 0),
        )
    raise ValueError(
        "Send a link to a specific post, or use /index CHAT_ID LAST_ID [SKIP]."
    )


async def remember(app, peer):
    p = await app.resolve_peer(peer.id)
    kind = "user" if peer.type == enums.ChatType.PRIVATE else peer.type.value
    await app.db.peers.update_one(
        {"_id": peer.id},
        {"$set": {"hash": getattr(p, "access_hash", 0) or 0, "type": kind}},
        upsert=True,
    )


async def source_chat(app, source):
    chat = await app.get_chat(source)
    if chat.type not in {
        enums.ChatType.CHANNEL,
        enums.ChatType.SUPERGROUP,
        enums.ChatType.FORUM,
        enums.ChatType.GROUP,
    }:
        raise ValueError("Choose a source channel or group.")
    member = await app.get_chat_member(chat.id, app.me.id)
    if member.status not in {
        enums.ChatMemberStatus.ADMINISTRATOR,
        enums.ChatMemberStatus.OWNER,
    }:
        raise ValueError(
            "Promote this bot to administrator in the source and try again."
        )
    if chat.has_protected_content:
        raise ValueError("This source protects its content.")
    return chat


async def propose(app, m, text="", session=False):
    if session and not app.reader:
        raise ValueError("No optional indexing account is configured.")
    uid = m.from_user.id
    origin = m.forward_origin
    if origin and getattr(origin, "message_id", None) and getattr(origin, "chat", None):
        source, last, skip = origin.chat.id, origin.message_id, 0
    elif text:
        source, last, skip = boundary(text)
    elif m.chat.id < 0 and m.reply_to_message:
        source, last, skip = m.chat.id, m.reply_to_message.id, 0
    else:
        if m.chat.id > 0:
            await app.db.prompt(uid, "index", session=session)
        return await m.reply_text(head("🗂", "Index a source") + "\n\n" + INDEX_HELP)
    if not 0 <= skip < last < 2**31:
        raise ValueError("The last message ID must exceed the skipped ID.")
    try:
        chat = await source_chat(app, source)
        msg = await app.get_messages(chat.id, last, replies=0)
    except FloodWait:
        raise
    except RPCError as e:
        raise ValueError(
            "Telegram could not open that source. Add the bot as admin, send a fresh post there and forward it or its link here."
        ) from e
    if session and chat.type == enums.ChatType.GROUP:
        raise ValueError(
            "Basic groups use account-specific message IDs. Use /index with the bot reader."
        )
    if not msg or msg.empty or not msg.chat or msg.chat.id != chat.id:
        raise ValueError(
            "Telegram could not read that last post. Choose an existing message visible to the bot."
        )
    await remember(app, chat)
    key = secrets.token_hex(6)
    await app.db.jobs.insert_one(
        {
            "_id": key,
            "actor": uid,
            "source": chat.id,
            "title": chat.title,
            "last": last,
            "reader": "session" if session else "bot",
            "cursor": skip,
            "skip": skip,
            "indexed": 0,
            "status": "draft",
            "created": now(),
            "notify": m.chat.id,
        }
    )
    await app.db.prompts.delete_one({"_id": uid, "kind": "index"})
    return await m.reply_text(
        head("🗂", "Confirm index")
        + "\n\n"
        + f"<b>{escape(chat.title or str(chat.id))}</b> · <code>{chat.id}</code>\n"
        f"Scan IDs <b>{skip + 1:,}–{last:,}</b>. Confirming makes this source searchable and its files deliverable to your bot’s users.",
        reply_markup=keys(
            [
                button("✓ ɪɴᴅᴇx", f"idx:go:{key}", style="success"),
                button("✕ ᴄᴀɴᴄᴇʟ", f"idx:stop:{key}", style="danger"),
            ]
        ),
    )


def progress(j):
    pct = 100 * (j["cursor"] - j["skip"]) / max(1, j["last"] - j["skip"])
    return (
        head("🗂", "Index manager")
        + f"\n\n<b>{escape(j['title'] or str(j['source']))}</b>\n{escape(j['status'])} · {pct:.0f}%\n"
    )


def card(j):
    text = (
        progress(j)
        + f"Cursor <code>{j['cursor']:,}/{j['last']:,}</code> · Media <b>{j['indexed']:,}</b>"
    )
    if j.get("error"):
        text += "\n" + escape(j["error"])
    actions = []
    if j["status"] in {"queued", "running", "draft"}:
        actions.append(button("⏹ ꜱᴛᴏᴘ", f"idx:stop:{j['_id']}"))
    if j["status"] in {"paused", "failed"}:
        actions.append(button("▶ ʀᴇꜱᴜᴍᴇ", f"idx:resume:{j['_id']}"))
    return text, keys(actions, [button("↻ ʀᴇꜰʀᴇꜱʜ", f"idx:view:{j['_id']}")])


async def action(app, q, verb, key, cfg):
    uid = q.from_user.id
    if uid != app.owner and uid not in cfg["admins"]:
        raise ValueError("Catalog admin access required.")
    j = await app.db.jobs.find_one({"_id": key})
    if not j or (uid != app.owner and uid != j["actor"]):
        raise ValueError("This index job belongs to another operator.")
    if verb in {"go", "resume"}:
        allowed = ["draft"] if verb == "go" else ["failed", "paused"]
        if j["status"] in allowed:
            await source_chat(app, j["source"])
            await app.db.add_source(j["source"])
            await app.db.jobs.update_one(
                {"_id": key, "status": {"$in": allowed}},
                {
                    "$set": {
                        "status": "queued",
                        "error": "",
                        "notify": q.message.chat.id,
                        "panel": q.message.id,
                        "after": now(),
                    }
                },
            )
    elif verb == "stop":
        await app.db.jobs.update_one(
            {"_id": key, "status": {"$in": ["draft", "queued", "running"]}},
            {"$set": {"status": "paused" if j["status"] != "draft" else "cancelled"}},
        )
    j = await app.db.jobs.find_one({"_id": key})
    text, markup = card(j)
    await q.message.edit_text(text, reply_markup=markup)


async def status(app, m):
    criteria = {} if m.from_user.id == app.owner else {"actor": m.from_user.id}
    jobs = await app.db.jobs.find(criteria).sort("created", -1).limit(12).to_list(12)
    await m.reply_text(
        head("🗂", "Recent index jobs")
        + ("\n\nSelect a job." if jobs else "\n\nForward a source post to start."),
        reply_markup=keys(
            *[
                [button(f"{j['status']} · {j['title']}"[:65], f"idx:view:{j['_id']}")]
                for j in jobs
            ]
        )
        if jobs
        else None,
    )


async def notify(app, j, cfg):
    text, markup = card(j)
    try:
        if j.get("panel"):
            await app.edit_message_text(
                j["notify"], j["panel"], text, reply_markup=markup
            )
        if cfg["log_chat"]:
            await app.send_message(cfg["log_chat"], text)
    except RPCError:
        log.info("Index status notification unavailable", exc_info=True)


async def batch(app, j, cfg):
    if j["source"] not in cfg["sources"]:
        raise ValueError("Source removed from bot settings.")
    if j["actor"] != app.owner and j["actor"] not in cfg["admins"]:
        raise ValueError("Index operator access was removed.")
    chat = await source_chat(app, j["source"])
    end = min(j["last"], j["cursor"] + cfg["batch_size"])
    reader = app
    if j.get("reader") == "session":
        reader = app.reader
        if reader is None:
            raise ValueError("Restore the optional indexing session and resume.")
        try:
            await reader.resolve_peer(chat.id)
        except (ValueError, RPCError):
            async for dialog in reader.get_dialogs():
                if dialog.chat.id == chat.id:
                    break
            else:
                raise ValueError(
                    "The indexing account must already be a member of this source."
                )
    messages = await reader.get_messages(
        chat.id, list(range(j["cursor"] + 1, end + 1)), replies=0
    )
    docs = [
        d
        for m in messages
        if m and not m.empty and m.chat and m.chat.id == chat.id and (d := file_doc(m))
    ]
    if docs:
        coll = await app.db.collection(chat.id)
        await coll.bulk_write(
            [ReplaceOne({"_id": d["_id"]}, d, upsert=True) for d in docs], ordered=False
        )
    state = "complete" if end == j["last"] else "running"
    updated = await app.db.jobs.find_one_and_update(
        {"_id": j["_id"], "status": "running", "cursor": j["cursor"]},
        {
            "$set": {"cursor": end, "status": state, "error": "", "attempts": 0},
            "$inc": {"indexed": len(docs)},
        },
        return_document=True,
    )
    if updated and (
        state == "complete"
        or (end - j["skip"]) // 1000 != (j["cursor"] - j["skip"]) // 1000
    ):
        await notify(app, updated, cfg)


async def worker(app):
    while True:
        try:
            j = await app.db.jobs.find_one_and_update(
                {"status": ACTIVE, "after": {"$lte": now()}},
                {"$set": {"status": "running"}},
                sort=[("created", 1)],
                return_document=True,
            )
            if not j:
                await asyncio.sleep(2)
                continue
            cfg = await app.db.config()
            try:
                await batch(app, j, cfg)
            except FloodWait as e:
                await app.db.jobs.update_one(
                    {"_id": j["_id"], "status": "running"},
                    {"$set": {"after": now() + timedelta(seconds=e.value + 1)}},
                )
            except (ValueError, RPCError) as e:
                transient = isinstance(e, RPCError) and getattr(e, "CODE", 0) >= 500
                if transient and j.get("attempts", 0) < 4:
                    await app.db.jobs.update_one(
                        {"_id": j["_id"], "status": "running"},
                        {
                            "$inc": {"attempts": 1},
                            "$set": {"after": now() + timedelta(seconds=15)},
                        },
                    )
                else:
                    failed = await app.db.jobs.find_one_and_update(
                        {"_id": j["_id"], "status": "running"},
                        {"$set": {"status": "failed", "error": str(e)[:180]}},
                        return_document=True,
                    )
                    if failed:
                        await notify(app, failed, cfg)
            await asyncio.sleep(0.6)
        except Exception:
            log.exception("Index batch deferred")
            await asyncio.sleep(5)


async def live(app, m, cfg, *, edited=False):
    if m.chat.id not in cfg["sources"] or m.chat.has_protected_content:
        return
    d = file_doc(m)
    if not d:
        if edited:
            await remove_posts(app, [m], cfg)
        return
    await remember(app, m.chat)
    coll = await app.db.collection(m.chat.id)
    result = await coll.replace_one({"_id": d["_id"]}, d, upsert=True)
    if result.upserted_id and cfg["updates_chat"] and cfg["updates_chat"] != m.chat.id:
        await app.send_message(
            cfg["updates_chat"],
            head("🎬", "Just added") + "\n" + escape(d["name"]),
            reply_markup=keys(
                [
                    button(
                        "📥 ᴠɪᴇᴡ ꜰɪʟᴇ",
                        url=f"https://t.me/{app.me.username}?start=f_{d['_id']}",
                    )
                ]
            ),
        )


async def remove_posts(app, messages, cfg):
    sources = {}
    for m in messages:
        if m.chat and m.chat.id in cfg["sources"]:
            sources.setdefault(m.chat.id, []).append(f"{abs(m.chat.id):x}_{m.id:x}")
    for source, ids in sources.items():
        coll = await app.db.collection(source)
        await coll.delete_many({"_id": {"$in": ids}})
