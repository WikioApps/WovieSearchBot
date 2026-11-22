"""Forward-to-index workflow adapted from Nova-Filter-Bot (GPL-3.0).
Index with the bot's own membership and checkpoint whole message-ID batches.
"""

import asyncio
import logging
import re
import secrets
from datetime import timedelta
from html import escape

from pyrogram import enums
from pyrogram.errors import FloodWait, MessageIdInvalid, MessageNotModified, RPCError

from database.ia_filterdb import attachment, file_doc, now
from plugins.commands import audit
from plugins.file_store import archive
from Script import button, duration, head, keys, release

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
        return await m.reply_text(
            head("🗂", "Index a source") + "\n\n" + INDEX_HELP, reply_markup=keys()
        )
    if not 0 <= skip < last < 2**31:
        raise ValueError("The last message ID must exceed the skipped ID.")
    try:
        chat = await source_chat(app, source)
        if chat.id == (await app.db.config()).get("archive_chat"):
            raise ValueError(
                "The private storage channel cannot be indexed as a source."
            )
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
    done = j["cursor"] - j["skip"]
    total = j["last"] - j["skip"]
    pct = min(1, done / max(total, 1))
    filled = int(pct * 12)
    bar = "▰" * filled + "▱" * (12 - filled)
    state = j["status"].title()
    wait = (j.get("after") or now()) - now()
    rate = j.get("rate", 0)
    line = f"{bar}  {pct:.0%}\n{state} · {done:,} / {total:,} posts"
    if j["status"] == "running":
        if wait.total_seconds() > 1:
            line += f"\nWaiting {duration(wait.total_seconds())} for Telegram"
        elif rate:
            line += f"\n{rate:.1f} posts/s · ETA {duration((total - done) / rate)}"
    line += f"\nNew {j.get('added', 0):,} · Repeats {j.get('repeats', 0):,} · Media posts {j['indexed']:,}"
    if j.get("unavailable"):
        line += f"\nSkipped (unavailable to bot) {j['unavailable']:,}"
    return "<b>" + escape(j.get("title") or str(j["source"])) + "</b>\n" + line


def card(j):
    text = head("🎞", "Index status") + "\n\n" + progress(j)
    if j.get("error"):
        text += "\n" + escape(j["error"])
    actions = []
    if j["status"] in {"queued", "running", "draft"}:
        actions.append(button("⏹ ꜱᴛᴏᴘ", f"idx:stop:{j['_id']}", style="danger"))
    if j["status"] in {"paused", "failed"}:
        actions.append(button("▶ ʀᴇꜱᴜᴍᴇ", f"idx:resume:{j['_id']}", style="success"))
    return text, keys(actions, [button("↻ ʀᴇꜰʀᴇꜱʜ", f"idx:view:{j['_id']}")])


async def dashboard(app, uid):
    who = {} if uid == app.owner else {"actor": uid}
    coll = app.db.jobs
    query = who | {"status": ACTIVE}
    jobs = await coll.find(query).sort("created", 1).limit(4).to_list(4)
    total = await coll.count_documents(query)
    text = head("🎞", "Index dashboard") + f"\n{total:,} active jobs"
    if not jobs:
        latest = await coll.find_one(
            who | {"status": {"$in": ["complete", "failed", "paused", "cancelled"]}},
            sort=[("created", -1)],
        )
        text += "\n\n" + (
            progress(latest)
            if latest
            else "Forward the last source post to start indexing."
        )
    else:
        text += "\n\n" + "\n\n".join(progress(j) for j in jobs)
        if total > 4:
            text += f"\n+{total - 4} more in /jobs"
    return text, keys([button("↻ ʀᴇꜰʀᴇꜱʜ", "dash:refresh")]), total


async def watch(app, m, uid, *, edit=False):
    ident = f"{m.chat.id}:{uid}"
    coll = app.db.db.eva_status
    text, markup, _ = await dashboard(app, uid)
    row = await coll.find_one({"_id": ident})
    mid = m.id if edit else (row or {}).get("mid")
    if edit and m.photo:
        mid = (await m.reply_text(text, reply_markup=markup)).id
        await m.delete()
    elif mid:
        try:
            await app.edit_message_text(m.chat.id, mid, text, reply_markup=markup)
        except MessageNotModified:
            pass
        except MessageIdInvalid:
            mid = None
    if not mid:
        mid = (await m.reply_text(text, reply_markup=markup)).id
    await coll.replace_one(
        {"_id": ident},
        {
            "_id": ident,
            "chat": m.chat.id,
            "uid": uid,
            "mid": mid,
            "text": text,
            "next": now() + timedelta(seconds=10),
            "expires": now() + timedelta(days=1),
        },
        upsert=True,
    )


async def dashboards(app):
    coll = app.db.db.eva_status
    while True:
        try:
            async for row in coll.find({"next": {"$lte": now()}}).limit(20):
                key = {"_id": row["_id"], "mid": row["mid"]}
                cfg = await app.db.config()
                if row["uid"] not in {app.owner, *cfg["admins"]}:
                    await coll.delete_one(key)
                    continue
                text, markup, active = await dashboard(app, row["uid"])
                delay = 10 if active else 30
                if text != row["text"]:
                    try:
                        await app.edit_message_text(
                            row["chat"], row["mid"], text, reply_markup=markup
                        )
                    except MessageNotModified:
                        pass
                    except FloodWait as e:
                        await coll.update_one(
                            key,
                            {"$set": {"next": now() + timedelta(seconds=e.value + 1)}},
                        )
                        continue
                    except RPCError as e:
                        if e.CODE >= 500:
                            raise
                        await coll.delete_one(key)
                        continue
                patch = {"text": text, "next": now() + timedelta(seconds=delay)}
                if active:
                    patch["expires"] = now() + timedelta(days=1)
                await coll.update_one(key, {"$set": patch})
        except Exception:
            log.exception("Status refresh deferred")
        await asyncio.sleep(3)


async def action(app, q, verb, key, cfg):
    uid = q.from_user.id
    if uid != app.owner and uid not in cfg["admins"]:
        raise ValueError("Catalog admin access required.")
    j = await app.db.jobs.find_one({"_id": key})
    if not j or (uid != app.owner and uid != j["actor"]):
        raise ValueError("This index job belongs to another operator.")
    changed = None
    if verb in {"go", "resume"}:
        allowed = ["draft"] if verb == "go" else ["failed", "paused"]
        if j["status"] in allowed:
            await source_chat(app, j["source"])
            await app.db.add_source(j["source"])
            changed = await app.db.jobs.update_one(
                {"_id": key, "status": {"$in": allowed}},
                {
                    "$set": {
                        "status": "queued",
                        "error": "",
                        "notify": q.message.chat.id,
                        "panel": q.message.id,
                        "after": now(),
                        "rate": 0,
                    }
                },
            )
    elif verb == "stop":
        changed = await app.db.jobs.update_one(
            {"_id": key, "status": {"$in": ["draft", "queued", "running"]}},
            {"$set": {"status": "paused" if j["status"] != "draft" else "cancelled"}},
        )
    j = await app.db.jobs.find_one({"_id": key})
    text, markup = card(j)
    if changed and changed.modified_count:
        await audit(app, text)
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
        ),
    )


async def notify(app, j, cfg):
    text, markup = card(j)
    try:
        if j.get("panel"):
            await app.edit_message_text(
                j["notify"], j["panel"], text, reply_markup=markup
            )
    except MessageNotModified:
        pass
    except RPCError:
        log.info("Index status panel unavailable", exc_info=True)
    if j["status"] in {"complete", "failed"}:
        await audit(app, text)


async def batch(app, j, cfg):
    if j["source"] == cfg.get("archive_chat") or j["source"] not in cfg["sources"]:
        raise ValueError("Source removed from bot settings.")
    if j["actor"] != app.owner and j["actor"] not in cfg["admins"]:
        raise ValueError("Index operator access was removed.")
    tick = asyncio.get_running_loop().time()
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
    candidates = len(docs)
    if reader is not app and docs:
        # A user session and a bot receive different access hashes for the same media.
        # Session history selects posts. Only the bot's view supplies deliverable IDs.
        bot_posts = await app.get_messages(chat.id, [d["mid"] for d in docs], replies=0)
        fresh = {
            m.id: file_doc(m)
            for m in bot_posts
            if m and m.chat and m.chat.id == chat.id and not m.empty
        }
        docs = [
            fresh[d["mid"]]
            for d in docs
            if fresh.get(d["mid"]) and fresh[d["mid"]]["unique"] == d["unique"]
        ]
    added = await app.db.catalog_write(docs, stamp=f"{j['_id']}:{j['cursor']}")
    await archive(app, docs, cfg)
    state = "complete" if end == j["last"] else "running"
    rate = (end - j["cursor"]) / (
        max(0.001, asyncio.get_running_loop().time() - tick) + 0.6
    )
    previous = j.get("rate", 0)
    refresh = (
        state == "complete"
        or not j.get("panel_tick")
        or (now() - j["panel_tick"]).total_seconds() >= 10
    )
    fields = {
        "cursor": end,
        "status": state,
        "error": "",
        "attempts": 0,
        "rate": rate if not previous else 0.7 * previous + 0.3 * rate,
    }
    if refresh:
        fields["panel_tick"] = now()
    updated = await app.db.jobs.find_one_and_update(
        {"_id": j["_id"], "status": "running", "cursor": j["cursor"]},
        {
            "$set": fields,
            "$inc": {
                "indexed": len(docs),
                "unavailable": candidates - len(docs),
                "added": added,
                "repeats": len(docs) - added,
            },
        },
        return_document=True,
    )
    if updated and refresh:
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
    if (
        m.chat.id == cfg.get("archive_chat")
        or m.chat.id not in cfg["sources"]
        or m.chat.has_protected_content
    ):
        return
    d = file_doc(m)
    if not d:
        if edited:
            await remove_posts(app, [m], cfg)
        return
    await remember(app, m.chat)
    created = await app.db.catalog_write([d])
    try:
        await archive(app, [d], cfg)
    except (RPCError, ValueError) as exc:
        # Keep the live catalog entry. An operator can repeat this range to fill
        # the archive after restoring the channel or waiting out a rate limit.
        log.warning(
            "Archive deferred for %s/%s (%s)", m.chat.id, m.id, type(exc).__name__
        )
    if created and cfg["updates_chat"] and cfg["updates_chat"] != m.chat.id:
        await app.send_message(
            cfg["updates_chat"],
            head("🎬", "Just added") + "\n" + escape(release(d["name"])),
            reply_markup=keys(
                [
                    button(
                        "📥 ᴠɪᴇᴡ ꜰɪʟᴇ",
                        url=f"https://t.me/{app.me.username}?start=f_{d['_id']}",
                        style="success",
                    )
                ]
            ),
        )


async def remove_posts(app, messages, cfg):
    sources = {}
    for m in messages:
        if m.chat and m.chat.id in cfg["sources"]:
            sources.setdefault(m.chat.id, []).append(f"{abs(m.chat.id):x}_{m.id:x}")
    if sources:
        await app.db.catalog_write(
            removed=[key for ids in sources.values() for key in ids]
        )


async def repair(app, m, cfg, incoming=None):
    if m.chat.id != m.from_user.id:
        raise ValueError("Use /repair in your private chat with the bot.")
    msg = incoming or m.reply_to_message
    if not msg:
        panel = await m.reply_text(
            "Forward or upload the matching file here. Use /cancel to leave.",
            reply_markup=keys(),
        )
        await app.db.prompt(m.from_user.id, "repair", panel=panel.id)
        return
    if msg.has_protected_content or msg.chat.has_protected_content:
        raise ValueError("Choose a file with content sharing enabled.")
    media = attachment(msg)
    if not media or not media.file_unique_id:
        raise ValueError("Send the matching document, video, audio or animation.")
    coll = await app.db.collection_for(media.file_unique_id)
    post = (
        None
        if coll is None
        else await coll.database.eva_posts.find_one(
            {"media": media.file_unique_id, "source": {"$in": cfg["sources"]}},
            {"_id": 1},
        )
    )
    if not post or not await app.db.remember_handle(
        media.file_unique_id, media.file_id, media.mime_type
    ):
        raise ValueError("This file has no exact match in the active catalog.")
    await archive(
        app,
        [{"unique": media.file_unique_id, "file": media.file_id}],
        cfg,
        verify_existing=True,
    )
    await app.db.prompts.delete_one({"_id": m.from_user.id, "kind": "repair"})
    await m.reply_text(
        head("✓", "File restored") + "\nThe existing file card is ready.",
        reply_markup=keys(
            [
                button(
                    "📥 ᴠɪᴇᴡ ꜰɪʟᴇ",
                    url=f"https://t.me/{app.me.username}?start=f_{post['_id']}",
                    style="success",
                )
            ]
        ),
    )
