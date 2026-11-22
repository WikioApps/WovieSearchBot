"""Community commands, moderation and paced delivery of announcements."""

import asyncio
import hashlib
import logging
import secrets
from datetime import timedelta
from html import escape

from pymongo.errors import OperationFailure
from pyrogram import raw
from pyrogram.errors import (
    FloodWait,
    InputUserDeactivated,
    MessageIdInvalid,
    RandomIdDuplicate,
    RPCError,
    UserIsBlocked,
)

from database.ia_filterdb import norm, now
from Script import button, edit_panel, head, keys, release, size

log = logging.getLogger(__name__)


async def request_title(app, m, uid, title, cfg):
    title = title.strip()
    if not 2 <= len(title) <= 120:
        raise ValueError("Use /request followed by a title (2–120 characters).")
    recent = await app.db.requests.find_one(
        {"user": uid, "status": "open", "title": title}
    )
    if recent:
        return await m.reply_text(
            "📮 Your request is in the queue.", reply_markup=keys()
        )
    key = secrets.token_hex(5)
    await app.db.requests.insert_one(
        {"_id": key, "user": uid, "title": title, "status": "open", "time": now()}
    )
    try:
        await app.send_message(
            cfg["requests_chat"] or app.owner,
            head("📮", "Title request") + "\n" + escape(title),
            reply_markup=request_keys(key),
        )
    except RPCError:
        log.info("Use /requests to moderate an undelivered notification")
    await m.reply_text(
        head("📮", "Request saved") + "\n" + escape(title), reply_markup=keys()
    )


def request_keys(key):
    return keys(
        [
            button("✓ ᴀᴠᴀɪʟᴀʙʟᴇ", f"req:ready:{key}", style="success"),
            button("✕ ᴅᴇᴄʟɪɴᴇ", f"req:closed:{key}", style="danger"),
        ],
        [button("‹ ɪɴʙᴏx", "req:queue:0")],
    )


async def request_queue(app, m, *, edit=False):
    rows = (
        await app.db.requests.find({"status": "open"})
        .sort("time", 1)
        .limit(10)
        .to_list(10)
    )
    text = head("📮", "Request inbox") + (
        "\n\nSelect a title to review." if rows else "\n\nNo open requests."
    )
    markup = keys(*[[button(r["title"][:65], f"req:view:{r['_id']}")] for r in rows])
    if edit:
        return await edit_panel(m, text, markup)
    return await m.reply_text(text, reply_markup=markup)


async def moderate(app, q, verb, key):
    if verb == "queue":
        return await request_queue(app, q.message, edit=True)
    if verb == "view":
        row = await app.db.requests.find_one({"_id": key, "status": "open"})
        if not row:
            return await request_queue(app, q.message, edit=True)
        return await edit_panel(
            q.message,
            head("📮", "Title request") + "\n" + escape(row["title"]),
            request_keys(key),
        )
    if verb not in {"ready", "closed"}:
        raise ValueError("Unknown request action.")
    r = await app.db.requests.find_one_and_update(
        {"_id": key, "status": "open"},
        {"$set": {"status": verb, "moderator": q.from_user.id}},
        return_document=True,
    )
    if not r:
        raise ValueError("This request has already been handled.")
    text = head("📮", "Request " + verb) + "\n" + escape(r["title"])
    await q.message.edit_text(
        text, reply_markup=keys([button("‹ ɪɴʙᴏx", "req:queue:0")])
    )
    try:
        await app.send_message(r["user"], text)
    except RPCError:
        log.info("Requester is unavailable")


async def audit(app, text):
    await app.db.db.eva_audit.insert_one({"text": text, "due": now()})


async def send_audit(app):
    coll = app.db.db.eva_audit
    row = await coll.find_one({"due": {"$lte": now()}}, sort=[("_id", 1)])
    if row:
        try:
            await app.send_message(app.log_channel, row["text"])
        except FloodWait as e:
            await coll.update_one(
                {"_id": row["_id"]},
                {"$set": {"due": now() + timedelta(seconds=e.value + 1)}},
            )
        except RPCError:
            await coll.update_one(
                {"_id": row["_id"]}, {"$set": {"due": now() + timedelta(minutes=1)}}
            )
            log.warning("Log channel delivery deferred", exc_info=True)
        else:
            await coll.delete_one({"_id": row["_id"]})


async def stats(app, m, cfg, *, edit=False):
    async def catalog(coll):
        data = await (
            await coll.aggregate(
                [
                    {
                        "$group": {
                            "_id": None,
                            "files": {"$sum": 1},
                            "bytes": {"$sum": "$bytes"},
                        }
                    }
                ]
            )
        ).to_list(1)
        posts = await coll.database.eva_posts.count_documents({})
        active_rows = await (
            await coll.database.eva_posts.aggregate(
                [
                    {"$match": {"source": {"$in": cfg["sources"]}}},
                    {"$group": {"_id": "$media"}},
                    {"$count": "n"},
                ],
                allowDiskUse=True,
            )
        ).to_list(1)
        available = active_rows[0]["n"] if active_rows else 0
        try:
            dbs = await coll.database.command("dbStats", scale=1)
            disk = dbs["storageSize"] + dbs["indexSize"]
        except OperationFailure:
            disk = None
        return (
            (data[0]["files"], data[0]["bytes"], posts, disk, available)
            if data
            else (0, 0, posts, disk, available)
        )

    catalogs = await asyncio.gather(*(catalog(c) for c in app.db.shards.values()))
    files = sum(r[0] for r in catalogs)
    total_bytes = sum(r[1] for r in catalogs)
    posts = sum(r[2] for r in catalogs)
    available = sum(r[4] for r in catalogs)
    sizes = [r[3] for r in catalogs]
    disk = size(sum(sizes)) if None not in sizes else "Unavailable"
    users = app.db.users
    known = {"_id": {"$gt": 0}, "seen": {"$exists": True}}
    counts = await asyncio.gather(
        users.count_documents(known),
        users.count_documents(
            known | {"last_seen": {"$gte": now() - timedelta(days=30)}}
        ),
        users.count_documents(known | {"dm": True}),
        users.count_documents({"_id": {"$gt": 0}, "blocked": True}),
        users.count_documents({"_id": {"$gt": 0}, "banned": True}),
        users.count_documents({"_id": {"$lt": 0}, "seen": {"$exists": True}}),
        app.db.requests.count_documents({"status": "open"}),
        app.db.jobs.count_documents({"status": {"$in": ["queued", "running"]}}),
        app.db.db.eva_broadcast.count_documents({"state": "queued"}),
    )
    people, active, private, blocked, banned, groups, requests, jobs, casts = counts
    text = (
        head("🎬", "Library dashboard") + f"\n<i>{now():%d %b %Y · %H:%M UTC}</i>\n\n"
        f"<b>ᴄᴀᴛᴀʟᴏɢ</b>\nUnique files  <b>{files:,}</b>   ·   Media  <b>{size(total_bytes)}</b>\n"
        f"Indexed posts  {posts:,}   ·   Extra copies  {max(0, posts - files):,}\nSearchable files  {available:,}   ·   Active sources  {len(cfg['sources']):,}\n\n"
        f"<b>ᴀᴜᴅɪᴇɴᴄᴇ</b>\nTotal users  <b>{people:,}</b>   ·   Active in 30 days  <b>{active:,}</b>\n"
        f"Private chats  {private:,}   ·   Groups  {groups:,}\nBlocked the bot  {blocked:,}   ·   Admin bans  {banned:,}\n\n"
        f"<b>ꜱᴛᴏʀᴀɢᴇ & ᴏᴘᴇʀᴀᴛɪᴏɴꜱ</b>\nMongoDB data + indexes  {disk}   ·   Databases  {len(sizes)}\n"
        f"Index jobs  {jobs}   ·   Broadcasts  {casts}   ·   Open requests  {requests}"
    )
    markup = keys(
        [button("↻ ʀᴇꜰʀᴇꜱʜ", "stats:refresh"), button("▤ ʟɪᴠᴇ ꜱᴛᴀᴛᴜꜱ", "dash:open")],
        [button("‹ ʜᴏᴍᴇ", "nav:home")] if m.chat.id > 0 else [],
    )
    if edit:
        return await edit_panel(m, text, markup)
    return await m.reply_text(text, reply_markup=markup)


async def admin_command(app, m, cmd, arg, cfg):
    uid = m.from_user.id
    if cmd == "stats":
        return await stats(app, m, cfg)
    if cmd in {"sources", "index_channels"}:
        return await m.reply_text(
            head("🗂", "Active sources")
            + "\n\n"
            + (
                "\n".join(f"<code>{i}</code>" for i in cfg["sources"])
                or "Forward the last source post here to begin."
            ),
            reply_markup=keys(),
        )
    if cmd == "requests":
        return await request_queue(app, m)
    if cmd in {"filter", "unfilter", "filters"}:
        coll = app.db.db.eva_filters
        if cmd == "filters":
            rows = await coll.find({}, {"_id": 1}).sort("_id", 1).limit(60).to_list(60)
            return await m.reply_text(
                head("💬", "Manual filters")
                + "\n"
                + ("\n".join(escape(r["_id"]) for r in rows) or "No keywords yet."),
                reply_markup=keys(),
            )
        keyword, _, reply = arg.partition("|")
        keyword = norm(keyword.strip())
        if not 1 <= len(keyword) <= 80:
            raise ValueError("Use /filter KEYWORD | REPLY or /unfilter KEYWORD.")
        if cmd == "filter":
            if not 1 <= len(reply.strip()) <= 900:
                raise ValueError("Enter a reply of 1–900 characters after |.")
            await coll.update_one(
                {"_id": keyword}, {"$set": {"text": reply.strip()}}, upsert=True
            )
        else:
            await coll.delete_one({"_id": keyword})
        return await m.reply_text("✓ Manual filter updated.", reply_markup=keys())
    if cmd in {"ban", "unban"}:
        try:
            target = int(arg)
        except ValueError:
            raise ValueError("Supply a numeric user or group ID.") from None
        if target in {0, app.owner, uid, *cfg["admins"]}:
            raise ValueError("Choose a non-admin user or group.")
        await app.db.users.update_one(
            {"_id": target}, {"$set": {"banned": cmd == "ban"}}, upsert=True
        )
        return await m.reply_text("✓ Access updated.", reply_markup=keys())
    if cmd == "delete":
        key = arg.removeprefix("f_")
        d = await app.db.file(key)
        if not d:
            raise ValueError(
                "Use /delete FILE_KEY. The key follows f_ in the file link."
            )
        await app.db.catalog_write(removed=[key])
        return await m.reply_text(
            "✓ File removed from the catalog. The Telegram post is intact.",
            reply_markup=keys(),
        )
    if cmd in {"broadcast", "grp_broadcast"}:
        if m.chat.id != uid or not m.reply_to_message:
            raise ValueError("In private, reply to the announcement with this command.")
        key = secrets.token_hex(5)
        await app.db.db.eva_broadcast.insert_one(
            {
                "_id": key,
                "owner": uid,
                "mid": m.reply_to_message.id,
                "groups": cmd == "grp_broadcast",
                "state": "draft",
                "cursor": -(2**53),
                "sent": 0,
                "failed": 0,
                "after": now(),
            }
        )
        return await m.reply_text(
            head("📣", "Confirm broadcast")
            + "\n\nCopy this message to registered "
            + ("groups" if cmd == "grp_broadcast" else "private chats")
            + "?",
            reply_markup=keys(
                [
                    button("📣 ꜱᴇɴᴅ", f"bc:send:{key}", style="success"),
                    button("✕ ᴄᴀɴᴄᴇʟ", f"bc:cancel:{key}", style="danger"),
                ]
            ),
        )


async def broadcast_action(app, q, verb, key):
    coll = app.db.db.eva_broadcast
    query = {"_id": key, "owner": q.from_user.id}
    changed = None
    if verb in {"send", "cancel"}:
        allowed = ["draft"] if verb == "send" else ["draft", "queued"]
        changed = await coll.update_one(
            query | {"state": {"$in": allowed}},
            {
                "$set": {
                    "state": "queued" if verb == "send" else "cancelled",
                    "panel": q.message.id,
                }
            },
        )
    job = await coll.find_one(query)
    if not job:
        raise ValueError("This broadcast belongs to another operator.")
    text, markup = broadcast_card(job)
    if changed and changed.modified_count:
        await audit(app, text)
    await edit_panel(q.message, text, markup)


def broadcast_card(job):
    key = job["_id"]
    text = (
        head("📣", "Broadcast " + job["state"])
        + f"\n\nDelivered  {job['sent']:,}   ·   Failed  {job['failed']:,}\nNewly blocked  {job.get('blocked', 0):,}   ·   Deactivated  {job.get('deactivated', 0):,}"
    )
    actions = (
        [
            button("↻ ʀᴇꜰʀᴇꜱʜ", f"bc:view:{key}"),
            button("⏹ ꜱᴛᴏᴘ", f"bc:cancel:{key}", style="danger"),
        ]
        if job["state"] == "queued"
        else []
    )
    return text, keys(actions)


async def copy_announcement(app, job, uid):
    rid = int.from_bytes(
        hashlib.blake2b(f"{job['_id']}:{uid}".encode(), digest_size=8).digest(),
        "little",
        signed=True,
    )
    await app.invoke(
        raw.functions.messages.ForwardMessages(
            from_peer=await app.resolve_peer(job["owner"]),
            to_peer=await app.resolve_peer(uid),
            id=[job["mid"]],
            random_id=[rid],
            drop_author=True,
        )
    )


async def deliver_announcement(app):
    coll = app.db.db.eva_broadcast
    job = await coll.find_one_and_update(
        {"state": "queued", "after": {"$lte": now()}},
        {"$set": {"after": now() + timedelta(minutes=2)}},
        return_document=True,
    )
    if not job:
        return False
    cfg = await app.db.config()
    if job["owner"] not in {app.owner, *cfg["admins"]}:
        await coll.update_one({"_id": job["_id"]}, {"$set": {"state": "cancelled"}})
        return True
    criteria = {"$gt": max(job["cursor"], 0) if not job["groups"] else job["cursor"]}
    if job["groups"]:
        criteria["$lt"] = 0
    audience = {"_id": criteria, "seen": {"$exists": True}, "banned": {"$ne": True}}
    if not job["groups"]:
        audience.update(dm=True, blocked={"$ne": True}, deactivated={"$ne": True})
    user = await app.db.users.find_one(
        audience,
        sort=[("_id", 1)],
    )
    if not user:
        job = await coll.find_one_and_update(
            {"_id": job["_id"], "state": "queued"},
            {"$set": {"state": "complete"}},
            return_document=True,
        )
        if not job:
            return True
        text, markup = broadcast_card(job)
        await audit(app, text)
        try:
            if job.get("panel"):
                await app.edit_message_text(
                    job["owner"], job["panel"], text, reply_markup=markup
                )
            else:
                await app.send_message(job["owner"], text, reply_markup=markup)
        except RPCError:
            log.info("Broadcast summary is in the log channel")
    else:
        uid = user["_id"]
        extra = {}
        try:
            await copy_announcement(app, job, uid)
        except RandomIdDuplicate:
            counter = "sent"
        except (UserIsBlocked, InputUserDeactivated) as e:
            counter = "failed"
            if uid > 0:
                flag = "blocked" if isinstance(e, UserIsBlocked) else "deactivated"
                await app.db.users.update_one({"_id": uid}, {"$set": {flag: True}})
                extra[flag] = 1
        except FloodWait as e:
            await coll.update_one(
                {"_id": job["_id"]},
                {"$set": {"after": now() + timedelta(seconds=e.value + 1)}},
            )
            return True
        except MessageIdInvalid:
            await coll.update_one({"_id": job["_id"]}, {"$set": {"state": "failed"}})
            await app.send_message(
                job["owner"], "Broadcast stopped. The original post is unavailable."
            )
            return True
        except RPCError as e:
            if getattr(e, "CODE", 0) >= 500:
                raise
            counter = "failed"
        else:
            counter = "sent"
        await coll.update_one(
            {"_id": job["_id"], "cursor": job["cursor"]},
            {"$set": {"cursor": uid, "after": now()}, "$inc": {counter: 1, **extra}},
        )
    return True


async def expire_delivery(app):
    row = await app.db.deletions.find_one(
        {"due": {"$lte": now()}, "failed": {"$ne": True}}, sort=[("due", 1)]
    )
    if not row:
        return False
    try:
        await app.delete_messages(row["uid"], row["mid"])
    except FloodWait as e:
        await app.db.deletions.update_one(
            {"_id": row["_id"]},
            {"$set": {"due": now() + timedelta(seconds=e.value + 1)}},
        )
        return True
    except RPCError as e:
        if getattr(e, "CODE", 0) >= 500:
            raise
        if not isinstance(e, MessageIdInvalid):
            await app.db.deletions.update_one(
                {"_id": row["_id"]},
                {"$set": {"failed": True, "reason": type(e).__name__}},
            )
            log.warning("Telegram declined deletion for delivery %s", row["_id"])
            return True
    await app.db.deletions.delete_one({"_id": row["_id"]})
    return True


async def expiry_worker(app):
    while True:
        try:
            if not await expire_delivery(app):
                await asyncio.sleep(1)
        except Exception:
            log.exception("Delivery expiry will retry")
            await asyncio.sleep(3)


async def housekeeping(app):
    while True:
        try:
            await send_audit(app)
            busy = await deliver_announcement(app)
            await asyncio.sleep(0.4 if busy else 3)
        except Exception:
            log.exception("Maintenance will retry")
            await asyncio.sleep(6)


async def plan(app, m, uid):
    row = await app.db.users.find_one({"_id": uid}) or {}
    exp = row.get("premium_until")
    text = (
        "Premium until " + exp.strftime("%d %b %Y UTC")
        if exp and exp > now()
        else "Standard membership"
    )
    await m.reply_text(head("💎", "My membership") + "\n\n" + text, reply_markup=keys())


async def set_plan(app, m, cmd, arg):
    fields = arg.split()
    usage = "Use /add_premium USER_ID DAYS or /remove_premium USER_ID."
    try:
        uid = int(fields[0])
        days = int(fields[1]) if cmd == "add_premium" else 0
    except (ValueError, IndexError):
        raise ValueError(usage) from None
    if uid < 1 or (cmd == "add_premium" and not 1 <= days <= 3650):
        raise ValueError(usage)
    if days:
        await app.db.users.update_one(
            {"_id": uid},
            [
                {
                    "$set": {
                        "premium_until": {
                            "$dateAdd": {
                                "startDate": {
                                    "$max": [
                                        now(),
                                        {"$ifNull": ["$premium_until", now()]},
                                    ]
                                },
                                "unit": "day",
                                "amount": days,
                            }
                        }
                    }
                }
            ],
            upsert=True,
        )
    else:
        await app.db.users.update_one({"_id": uid}, {"$unset": {"premium_until": ""}})
    await m.reply_text("✓ Membership updated.", reply_markup=keys())


async def trending(app, m, cfg):
    from plugins.pm_filter import allowed, gate

    if not allowed(m.chat.id, cfg):
        raise ValueError("Search is disabled in this chat.")
    if block := await gate(app, m.from_user.id, cfg):
        return await m.reply_text(block[0], reply_markup=block[1])
    rows = (
        await app.db.db.eva_trending.find(
            {"source": {"$in": cfg["sources"]}, "expires": {"$gt": now()}}
        )
        .sort("count", -1)
        .limit(10)
        .to_list(10)
    )
    await m.reply_text(
        head("🔥", "Trending releases")
        + "\n\n"
        + (
            "\n".join(
                f"{i}. {escape(release(r['name'])[:100])}"
                for i, r in enumerate(rows, 1)
            )
            or "Search for an indexed title to get started."
        ),
        reply_markup=keys(),
    )
