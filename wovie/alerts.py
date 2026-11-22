"""Turn new catalog entries into permission-checked durable notifications."""

import asyncio
from datetime import UTC, datetime, timedelta
import logging

from .delivery import DeliveryError

log = logging.getLogger(__name__)


async def schedule(store, metadata):
    db = store.db
    await db.watchlist.create_index([("title_norm", 1), ("created_at", 1)])
    last_referrals = 0
    while True:
        try:
            tick = asyncio.get_running_loop().time()
            if tick - last_referrals > 60:
                from .community import reconcile_referrals

                await reconcile_referrals(store)
                last_referrals = tick
                if metadata.key:
                    await weekly(store, metadata)
            policy = await store.setting(0)
            for uid, catalog in zip(store.catalog_ids, store.catalogs):
                now = datetime.now(UTC)
                cursor = await db.alert_cursors.find_one({"_id": uid})
                if not cursor:
                    # Only additions after first activation should trigger alerts.
                    await db.alert_cursors.update_one(
                        {"_id": uid},
                        {"$setOnInsert": {"at": now, "last": ""}},
                        upsert=True,
                    )
                    continue
                query = {
                    "created_at": {"$lte": now - timedelta(seconds=2)},
                    "deleted_at": None,
                    "$or": [
                        {"created_at": {"$gt": cursor["at"]}},
                        {"created_at": cursor["at"], "_id": {"$gt": cursor["last"]}},
                    ],
                }
                fields = {"title_norm": 1, "created_at": 1, "source_chat": 1}
                async for record in (
                    catalog.files.find(query, fields)
                    .sort([("created_at", 1), ("_id", 1)])
                    .limit(100)
                ):
                    target = policy.get("announcement_chat")
                    if target and not policy.get("premium_only"):
                        await store.enqueue(
                            "announcement",
                            {"chat_id": target, "file": record["_id"]},
                            key=f"announcement:{target}:{record['_id']}",
                        )
                    match = {
                        "title_norm": record["title_norm"],
                        "created_at": {"$lte": record["created_at"]},
                    }
                    async for sub in db.watchlist.find(match, {"user_id": 1}):
                        user = sub["user_id"]
                        await store.enqueue(
                            "release",
                            {
                                "chat_id": user,
                                "file": record["_id"],
                                "watch": sub["_id"],
                            },
                            key=f"release:{record['_id']}:{user}",
                        )
                    await db.alert_cursors.update_one(
                        {"_id": uid},
                        {"$set": {"at": record["created_at"], "last": record["_id"]}},
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Catalog notification scan paused (%s)", type(exc).__name__)
        await asyncio.sleep(4)


async def send_release(job, store, delivery, bot):
    from aiogram.exceptions import TelegramForbiddenError

    p = job["payload"]
    if not await store.db.watchlist.find_one(
        {"_id": p["watch"], "user_id": p["chat_id"]}
    ):
        return
    try:
        record = await delivery.check_file(p["chat_id"], p["file"])
    except DeliveryError as exc:
        if exc.retry_after:
            raise
        return
    try:
        await bot.send_message(
            p["chat_id"],
            "🎬 New on your watchlist\n"
            + record["filename"][:300]
            + "\nUse /search to see available editions.",
            parse_mode=None,
        )
    except TelegramForbiddenError:
        return


async def weekly(store, metadata):
    now = datetime.now(UTC)
    week = now.strftime("%G-W%V")
    rows = (
        await store.db.digests.find({"enabled": True, "last_week": {"$ne": week}})
        .limit(32)
        .to_list(32)
    )
    if not rows:
        return
    picks = await metadata.trending()
    if not picks:
        return
    message = "🎬 Weekly picks from TMDB\n" + "\n".join(
        "• " + x["title"][:80] for x in picks[:8]
    )
    for row in rows:
        await store.enqueue(
            "digest",
            {"chat_id": row["_id"], "text": message},
            key=f"digest:{week}:{row['_id']}",
        )
        await store.db.digests.update_one(
            {"_id": row["_id"]}, {"$set": {"last_week": week}}
        )


async def send_public(job, store, delivery, bot):
    from aiogram.exceptions import TelegramForbiddenError

    p = job["payload"]
    if job["kind"] == "digest":
        if not await store.db.digests.find_one({"_id": p["chat_id"], "enabled": True}):
            return
        ok, _ = await delivery.allowed(p["chat_id"])
        if not ok:
            return
        try:
            await bot.send_message(p["chat_id"], p["text"], parse_mode=None)
        except TelegramForbiddenError:
            await store.db.digests.update_one(
                {"_id": p["chat_id"]}, {"$set": {"enabled": False}}
            )
        return
    settings = await store.setting(0)
    if settings.get("announcement_chat") != p["chat_id"] or settings.get(
        "premium_only"
    ):
        return
    record = await store.get_file(p["file"])
    if not record or record.get("source_protected"):
        return
    src = await store.db.sources.find_one(
        {
            "_id": record["source_chat"],
            "active": True,
            "public": True,
            "premium_only": {"$ne": True},
            "protected": {"$ne": True},
        }
    )
    if not src or (await bot.get_chat(src["_id"])).has_protected_content:
        return
    await bot.send_message(
        p["chat_id"],
        "📽 New in the catalog\n" + record["filename"][:280],
        parse_mode=None,
    )
