"""Community commands, moderation and paced delivery of announcements."""

import asyncio
import logging
import secrets
from datetime import timedelta
from html import escape

from pyrogram.errors import (
    FloodWait,
    MessageIdInvalid,
    RPCError,
)

from database.ia_filterdb import norm, now
from Script import button, head, keys

log = logging.getLogger(__name__)


async def request_title(app, m, uid, title, cfg):
    title = title.strip()
    if not 2 <= len(title) <= 120:
        raise ValueError("Use /request followed by a title (2–120 characters).")
    recent = await app.db.requests.find_one(
        {"user": uid, "status": "open", "title": title}
    )
    if recent:
        return await m.reply_text("📮 Your request is in the queue.")
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
    await m.reply_text(head("📮", "Request saved") + "\n" + escape(title))


def request_keys(key):
    return keys(
        [
            button("✓ ᴀᴠᴀɪʟᴀʙʟᴇ", f"req:ready:{key}", style="success"),
            button("✕ ᴄʟᴏꜱᴇ", f"req:closed:{key}"),
        ]
    )


async def moderate(app, q, verb, key):
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
    await q.message.edit_text(text)
    try:
        await app.send_message(r["user"], text)
    except RPCError:
        log.info("Requester is unavailable")


async def stats(app, m, cfg):
    total = sum(
        await asyncio.gather(
            *(c.estimated_document_count() for c in app.db.shards.values())
        )
    )
    users, groups, pending = await asyncio.gather(
        app.db.users.count_documents({"_id": {"$gt": 0}}),
        app.db.users.count_documents({"_id": {"$lt": 0}}),
        app.db.requests.count_documents({"status": "open"}),
    )
    await m.reply_text(
        head("📊", "Catalog stats")
        + f"\n\nFiles · <b>{total:,}</b>\nSources · {len(cfg['sources'])}\nCatalog databases · {len(app.db.shards)}\nPrivate chats · {users:,}\nGroups · {groups:,}\nOpen requests · {pending:,}"
    )


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
            )
        )
    if cmd == "requests":
        rows = (
            await app.db.requests.find({"status": "open"})
            .sort("time", 1)
            .limit(10)
            .to_list(10)
        )
        if not rows:
            return await m.reply_text("📮 No open requests.")
        for r in rows:
            await m.reply_text(escape(r["title"]), reply_markup=request_keys(r["_id"]))
        return
    if cmd in {"filter", "unfilter", "filters"}:
        coll = app.db.db.eva_filters
        if cmd == "filters":
            rows = await coll.find({}, {"_id": 1}).sort("_id", 1).limit(60).to_list(60)
            return await m.reply_text(
                head("💬", "Manual filters")
                + "\n"
                + ("\n".join(escape(r["_id"]) for r in rows) or "No keywords yet.")
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
        return await m.reply_text("✓ Manual filter updated.")
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
        return await m.reply_text("✓ Access updated.")
    if cmd == "delete":
        key = arg.removeprefix("f_")
        d = await app.db.file(key)
        if not d:
            raise ValueError(
                "Use /delete FILE_KEY. The key follows f_ in the file link."
            )
        coll = await app.db.collection(d["source"])
        await coll.delete_one({"_id": key})
        return await m.reply_text(
            "✓ File removed from the catalog. The Telegram post is intact."
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
                    button("📣 ꜱᴇɴᴅ", f"bc:send:{key}"),
                    button("✕ ᴄᴀɴᴄᴇʟ", f"bc:cancel:{key}"),
                ]
            ),
        )


async def broadcast_action(app, q, verb, key):
    query = {"_id": key, "owner": q.from_user.id, "state": "draft"}
    job = await app.db.db.eva_broadcast.find_one_and_update(
        query,
        {"$set": {"state": "queued" if verb == "send" else "cancelled"}},
        return_document=True,
    )
    if not job:
        raise ValueError("This broadcast is no longer awaiting your confirmation.")
    await q.message.edit_text(head("📣", "Broadcast " + job["state"]))


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
    user = await app.db.users.find_one(
        {"_id": criteria, "seen": {"$exists": True}, "banned": {"$ne": True}},
        sort=[("_id", 1)],
    )
    if not user:
        await coll.update_one({"_id": job["_id"]}, {"$set": {"state": "complete"}})
        await app.send_message(
            job["owner"],
            head("📣", "Broadcast complete")
            + f"\nSent · {job['sent']}\nSkipped · {job['failed']}",
        )
    else:
        uid = user["_id"]
        try:
            await app.copy_message(uid, job["owner"], job["mid"])
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
            {"$set": {"cursor": uid, "after": now()}, "$inc": {counter: 1}},
        )
    return True


async def housekeeping(app):
    while True:
        try:
            for _ in range(6):
                row = await app.db.deletions.find_one({"due": {"$lte": now()}})
                if not row:
                    break
                try:
                    await app.delete_messages(row["uid"], row["mid"])
                except FloodWait as e:
                    await app.db.deletions.update_one(
                        {"_id": row["_id"]},
                        {"$set": {"due": now() + timedelta(seconds=e.value + 1)}},
                    )
                    continue
                except RPCError as e:
                    if getattr(e, "CODE", 0) >= 500:
                        raise
                    log.info("Telegram declined an expired message deletion")
                await app.db.deletions.delete_one({"_id": row["_id"]})
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
    await m.reply_text(head("💎", "My membership") + "\n\n" + text)


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
    await m.reply_text("✓ Membership updated.")


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
            "\n".join(f"{i}. {escape(r['name'][:100])}" for i, r in enumerate(rows, 1))
            or "Search for an indexed title to get started."
        )
    )
