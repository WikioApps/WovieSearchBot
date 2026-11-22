"""Owner-only metadata corrections, title assignments and explicit alternate files."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
from html import escape
import re

from aiogram import Router
from aiogram.enums import ChatType
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.filters import Command
from aiogram.types import Message
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from .delivery import DeliveryError
from .parser import (
    ascii_normalize,
    canonical_language,
    canonical_quality,
    normalize,
    parse_query,
    parse_release,
    tokenize,
)

UTC = timezone.utc
FIELDS = {"title", "year", "season", "episode", "quality", "languages", "aliases"}


class CurationBusy(RuntimeError):
    retry_after = 30


def now():
    return datetime.now(UTC)


def identifier(value: str) -> str:
    if not re.fullmatch(r"[a-f0-9]{32}", value):
        raise ValueError("Use a 32-character catalog file/title ID.")
    return value


async def locate(store, file_id):
    identifier(file_id)
    for catalog in store.catalogs:
        row = await catalog.files.find_one({"_id": file_id, "deleted_at": None})
        if row:
            return catalog, row
    raise ValueError("Catalog file not found or soft-deleted.")


def corrected(row, overrides):
    result = parse_release(row["filename"], row.get("caption") or "")
    result.update(overrides)
    result["aliases"] = overrides.get("aliases", row.get("source_aliases", []))
    result["title_norm"] = normalize(result["title"])
    result["title_ascii"] = ascii_normalize(result["title"])
    result["tokens"] = tokenize(
        " ".join(
            [
                result["title"],
                row["filename"],
                row.get("caption") or "",
                *result["aliases"],
            ]
        )
    )
    return result


async def update_file(catalog, row, actor, action, overrides=None, extra=None):
    previous = row.get("corrections") or {}
    overrides = dict(previous) if overrides is None else overrides
    fields = corrected(row, overrides)
    fields.update(extra or {})
    fields.update({"corrections": overrides, "updated_at": now()})
    version = row.get("curation_version", 0)
    selector = {
        "_id": row["_id"],
        "deleted_at": None,
        "curation_version": version
        if "curation_version" in row
        else {"$exists": False},
    }
    before = {
        "corrections": previous,
        "title_id": row.get("title_id"),
        "alternate_ids": row.get("alternate_ids", []),
    }
    after = {
        "corrections": overrides,
        "title_id": fields.get("title_id", row.get("title_id")),
        "alternate_ids": fields.get("alternate_ids", row.get("alternate_ids", [])),
    }
    if before == after:
        return
    result = await catalog.files.update_one(
        selector,
        {
            "$set": fields,
            "$inc": {"curation_version": 1},
            "$push": {
                "curation_audit": {
                    "$each": [
                        {
                            "at": now(),
                            "actor_id": actor,
                            "action": action,
                            "before": before,
                            "after": after,
                        }
                    ],
                    "$slice": -10,
                }
            },
        },
    )
    if not result.matched_count:
        raise ValueError("This record changed concurrently. Read it again and retry.")


def parse_value(key, value):
    if key not in FIELDS:
        raise ValueError(
            "Field: title, year, season, episode, quality, languages or aliases."
        )
    if key in {"year", "season", "episode"}:
        try:
            result = int(value)
        except ValueError:
            raise ValueError("Use an integer for this metadata field.") from None
        low, high = (1800, 2200) if key == "year" else (0, 10000)
        if not low <= result <= high:
            raise ValueError(f"{key} must be between {low} and {high}.")
        return result
    if key == "title":
        value = value.strip()
        if not value or len(value) > 256:
            raise ValueError("Title must contain 1–256 characters.")
        return value
    if key == "quality":
        value = canonical_quality(value)
        if value not in {"2160p", "1080p", "720p", "480p", "360p"}:
            raise ValueError("Quality: 2160p, 1080p, 720p, 480p or 360p.")
        return value
    values = [v.strip() for v in value.split("|") if v.strip()]
    if not values or len(values) > 10 or any(len(v) > 100 for v in values):
        raise ValueError(
            "Provide 1–10 values separated by |, at most 100 characters each."
        )
    return list(
        dict.fromkeys(
            canonical_language(v) if key == "languages" else v for v in values
        )
    )


def build_router(settings, store, delivery):
    router = Router(name="curation")

    async def owner(message):
        if (
            message.chat.type != ChatType.PRIVATE
            or not message.from_user
            or message.from_user.id not in settings.admins
        ):
            await message.answer(
                "Use an owner's private chat for catalog curation.", parse_mode=None
            )
            return False
        return True

    async def respond(message, text):
        await message.answer(text, parse_mode="HTML")

    def args(message):
        return (message.text or "").partition(" ")[2].strip()

    @router.message(Command("correct", "uncorrect"))
    async def correction(message: Message):
        if not await owner(message):
            return
        try:
            parts = args(message).split(maxsplit=2)
            reset = message.text.split()[0].split("@")[0] == "/uncorrect"
            if len(parts) != (2 if reset else 3):
                raise ValueError(
                    "Use /correct FILE_ID FIELD VALUE or /uncorrect FILE_ID FIELD."
                )
            file_id, key = parts[:2]
            if key not in FIELDS:
                raise ValueError("Unsupported correction field.")
            await delivery.check_file(message.from_user.id, identifier(file_id))
            catalog, row = await locate(store, file_id)
            overrides = dict(row.get("corrections") or {})
            if reset:
                overrides.pop(key, None)
            else:
                overrides[key] = parse_value(key, parts[2])
            await update_file(
                catalog,
                row,
                message.from_user.id,
                "uncorrect" if reset else "correct",
                overrides,
            )
            await respond(
                message, "✅ Correction saved. Reindexing preserves active overrides."
            )
        except (ValueError, DeliveryError) as exc:
            await respond(message, escape(str(exc)))

    @router.message(Command("catalog"))
    async def inspect_file(message: Message):
        if not await owner(message):
            return
        try:
            await delivery.check_file(message.from_user.id, identifier(args(message)))
            _, row = await locate(store, args(message))
            lines = [
                f"<b>{escape(row['filename'][:200])}</b>",
                f"ID: <code>{row['_id']}</code>",
            ]
            for key in (
                "title",
                "title_id",
                "year",
                "season",
                "episode",
                "languages",
                "quality",
                "aliases",
                "alternate_ids",
                "curation_version",
            ):
                lines.append(f"{key}: {escape(str(row.get(key, '—')))[:350]}")
            lines.append(
                f"Corrections: {escape(str(row.get('corrections', {})))[:600]}"
            )
            await respond(message, "\n".join(lines))
        except (ValueError, DeliveryError) as exc:
            await respond(message, escape(str(exc)))

    @router.message(Command("title"))
    async def titles(message: Message):
        if not await owner(message):
            return
        try:
            action, _, text = args(message).partition(" ")
            if action == "create":
                title, filters = parse_query(text)
                if not title or set(filters) - {"year"}:
                    raise ValueError("Use /title create TITLE [year:YYYY].")
                key = hashlib.sha256(
                    f"title:{message.chat.id}:{message.message_id}".encode()
                ).hexdigest()[:32]
                await store.db.titles.update_one(
                    {"_id": key},
                    {
                        "$setOnInsert": {
                            "title": title,
                            "year": filters.get("year"),
                            "created_at": now(),
                            "created_by": message.from_user.id,
                        }
                    },
                    upsert=True,
                )
                return await respond(
                    message,
                    f"✅ Title created: <code>{key}</code>\n/title assign {key} FILE_ID[,FILE_ID]",
                )
            if action == "assign":
                title_id, _, files = text.partition(" ")
                title = await store.db.titles.find_one(
                    {"_id": identifier(title_id), "merged_into": {"$exists": False}}
                )
                if not title:
                    raise ValueError("Title not found or already merged.")
                file_ids = list(
                    dict.fromkeys(x.strip() for x in files.split(",") if x.strip())
                )
                if not file_ids or len(file_ids) > 20:
                    raise ValueError(
                        "Assign between one and twenty comma-separated file IDs."
                    )
                # Validate all input first. Each individual change is atomic and replay-safe;
                # MongoDB shards do not provide one global transaction for this command.
                for fid in file_ids:
                    await delivery.check_file(message.from_user.id, identifier(fid))
                changed = 0
                lock_owner = f"assign:{message.chat.id}:{message.message_id}"
                await acquire_lock(store, lock_owner)
                try:
                    if not await store.db.titles.find_one(
                        {"_id": title_id, "merged_into": {"$exists": False}}
                    ):
                        raise ValueError(
                            "Title was merged while this command was being validated."
                        )
                    for fid in file_ids:
                        renewed = await store.db.curation_locks.update_one(
                            {
                                "_id": "merge",
                                "owner": lock_owner,
                                "lease_until": {"$gt": now()},
                            },
                            {"$set": {"lease_until": now() + timedelta(seconds=120)}},
                        )
                        if not renewed.matched_count:
                            raise CurationBusy(
                                "Assignment lease expired; rerun the command to continue safely."
                            )
                        catalog, row = await locate(store, fid)
                        if row.get("title_id") == title_id:
                            continue
                        overrides = dict(row.get("corrections") or {})
                        overrides["title"] = title["title"]
                        if title.get("year"):
                            overrides["year"] = title["year"]
                        await update_file(
                            catalog,
                            row,
                            message.from_user.id,
                            "title_assign",
                            overrides,
                            {"title_id": title_id},
                        )
                        changed += 1
                finally:
                    await store.db.curation_locks.delete_one(
                        {"_id": "merge", "owner": lock_owner}
                    )
                return await respond(
                    message,
                    f"✅ Assigned {changed} files. All source locations and variants remain intact.",
                )
            if action == "merge":
                parts = text.split()
                if len(parts) != 3 or parts[2] != "confirm":
                    raise ValueError(
                        "Use /title merge FROM_TITLE_ID INTO_TITLE_ID confirm."
                    )
                source_id, target_id = map(identifier, parts[:2])
                if source_id == target_id:
                    raise ValueError("Choose two different titles.")
                for tid in (source_id, target_id):
                    if not await store.db.titles.find_one(
                        {"_id": tid, "merged_into": {"$exists": False}}
                    ):
                        raise ValueError("Both titles must exist and remain unmerged.")
                job = await store.enqueue(
                    "curation",
                    {
                        "source_title": source_id,
                        "target_title": target_id,
                        "actor_id": message.from_user.id,
                    },
                    key=f"title-merge:{message.chat.id}:{message.message_id}",
                )
                return await respond(
                    message,
                    f"✅ Merge queued: <code>{job}</code>. Monitor /jobs; pause/cancel controls apply.",
                )
            raise ValueError(
                "Use /title create, assign or merge. To split a title, create another and assign only the selected variants."
            )
        except (ValueError, DeliveryError, CurationBusy) as exc:
            await respond(message, escape(str(exc)))

    @router.message(Command("alternate", "unalternate"))
    async def alternate(message: Message):
        if not await owner(message):
            return
        try:
            remove = message.text.split()[0].split("@")[0] == "/unalternate"
            parts = args(message).split()
            if len(parts) != (2 if remove else 3) or (
                not remove and parts[2] != "confirm"
            ):
                raise ValueError(
                    "Use /alternate ORIGINAL_ID COPY_ID confirm or /unalternate ORIGINAL_ID COPY_ID."
                )
            original, alternative = map(identifier, parts[:2])
            if original == alternative:
                raise ValueError("Choose two distinct catalog locations.")
            catalog, row = await locate(store, original)
            existing = list(row.get("alternate_ids") or [])
            if remove:
                existing = [value for value in existing if value != alternative]
            else:
                await delivery.check_file(message.from_user.id, original)
                other = await delivery.check_file(message.from_user.id, alternative)
                if (
                    row.get("file_unique_id")
                    and other.get("file_unique_id")
                    and row["file_unique_id"] != other["file_unique_id"]
                ):
                    raise ValueError(
                        "These files have different Telegram unique IDs; keep them as separate variants."
                    )
                if (
                    row.get("file_size")
                    and other.get("file_size")
                    and row["file_size"] != other["file_size"]
                ):
                    raise ValueError(
                        "File sizes differ; alternate locations must represent the same file."
                    )
                if alternative not in existing:
                    existing.append(alternative)
                if len(existing) > 5:
                    raise ValueError(
                        "At most five alternate locations may be registered per file."
                    )
            await update_file(
                catalog,
                row,
                message.from_user.id,
                "alternate_remove" if remove else "alternate_add",
                extra={"alternate_ids": existing},
            )
            await respond(
                message,
                "✅ Alternate locations saved. Each destination's permissions are checked during delivery.",
            )
        except (ValueError, DeliveryError) as exc:
            await respond(message, escape(str(exc)))

    @router.message(Command("revoke_link"))
    async def revoke_link(message: Message):
        if not await owner(message):
            return
        token = args(message)
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,40}", token):
            return await respond(
                message, "Use /revoke_link TOKEN (the value after start=)."
            )
        result = await store.db.links.update_one(
            {"_id": token, "revoked": {"$ne": True}},
            {
                "$set": {
                    "revoked": True,
                    "revoked_at": now(),
                    "revoked_by": message.from_user.id,
                }
            },
        )
        await respond(
            message,
            "✅ Link revoked."
            if result.modified_count
            else "Link not found or already revoked.",
        )

    @router.message(Command("duplicates"))
    async def duplicates(message: Message):
        if not await owner(message):
            return
        try:
            original = await delivery.check_file(
                message.from_user.id, identifier(args(message))
            )
            unique = original.get("file_unique_id")
            if not unique:
                raise ValueError(
                    "No Telegram unique ID is stored. Use /repair_file FILE_ID to acquire one from an accessible source."
                )
            found = []
            locations = {(original["source_chat"], original["message_id"])}
            for catalog in store.catalogs:
                rows = (
                    await catalog.files.find(
                        {
                            "file_unique_id": unique,
                            "deleted_at": None,
                            "_id": {"$ne": original["_id"]},
                        }
                    )
                    .limit(11)
                    .to_list(11)
                )
                for row in rows:
                    location = (row["source_chat"], row["message_id"])
                    if location in locations:
                        continue
                    try:
                        await delivery.check_file(message.from_user.id, row["_id"])
                    except DeliveryError:
                        continue
                    locations.add(location)
                    found.append(row)
                    if len(found) == 10:
                        break
                if len(found) == 10:
                    break
            if not found:
                return await respond(
                    message,
                    "No accessible alternate locations with this exact Telegram unique ID were found.",
                )
            lines = ["<b>Matching Telegram file identities</b>"]
            for row in found:
                lines.append(
                    f"<code>{row['_id']}</code>\nSource <code>{row['source_chat']}</code> · message {row['message_id']}"
                )
            lines.append(
                f"\nReview each candidate, then use:\n<code>/alternate {original['_id']} COPY_ID confirm</code>\nNo records were merged or deleted."
            )
            await respond(message, "\n\n".join(lines))
        except (ValueError, DeliveryError) as exc:
            await respond(message, escape(str(exc)))

    @router.message(Command("repair_file"))
    async def repair_file(message: Message):
        if not await owner(message):
            return
        try:
            file_id = identifier(args(message))
            record = await delivery.check_file(message.from_user.id, file_id)
            operation = f"{message.chat.id}:{message.message_id}"
            receipts = store.db.repair_receipts
            existing = await receipts.find_one_and_update(
                {"_id": operation},
                {
                    "$setOnInsert": {
                        "state": "pending",
                        "file_id": file_id,
                        "actor_id": message.from_user.id,
                        "source_chat": record["source_chat"],
                        "source_message": record["message_id"],
                        "expected_unique_id": record.get("file_unique_id"),
                        "created_at": now(),
                    }
                },
                upsert=True,
                return_document=ReturnDocument.BEFORE,
            )
            if existing and existing["file_id"] != file_id:
                raise ValueError(
                    "This command message already belongs to another repair. Send a new command."
                )
            if existing and existing["state"] in {"pending", "uncertain"}:
                raise ValueError(
                    "A previous send has an uncertain outcome. Check your private chat; this command will not resend automatically. Send a new command to retry deliberately."
                )
            if existing and existing["state"] == "failed":
                raise ValueError(
                    "This repair attempt failed. Check source access and send a new command to retry."
                )
            if existing and existing["state"] == "applied":
                await cleanup_repair(message.bot, store, receipts, operation, existing)
                return await respond(
                    message,
                    "✅ This repair was already applied; no file was forwarded again.",
                )
            forwarded = None
            receipt = existing
            try:
                if existing is None:
                    try:
                        forwarded = await message.bot.forward_message(
                            chat_id=message.from_user.id,
                            from_chat_id=record["source_chat"],
                            message_id=record["message_id"],
                            disable_notification=True,
                            protect_content=True,
                        )
                    except (TelegramNetworkError, TimeoutError):
                        await receipts.update_one(
                            {"_id": operation, "state": "pending"},
                            {
                                "$set": {
                                    "state": "uncertain",
                                    "updated_at": now(),
                                }
                            },
                        )
                        raise ValueError(
                            "Telegram's send outcome is uncertain. Check your private chat; no automatic resend will occur."
                        ) from None
                    except TelegramRetryAfter as exc:
                        await receipts.update_one(
                            {"_id": operation},
                            {
                                "$set": {
                                    "state": "failed",
                                    "retry_after": exc.retry_after,
                                    "updated_at": now(),
                                }
                            },
                        )
                        raise ValueError(
                            f"Telegram requires a {exc.retry_after}-second wait. Send a new repair command afterward."
                        ) from None
                    except TelegramAPIError:
                        await receipts.update_one(
                            {"_id": operation},
                            {"$set": {"state": "failed", "updated_at": now()}},
                        )
                        raise ValueError(
                            "The source could not be forwarded. Check message existence, bot access and content protection."
                        ) from None
                    media = forwarded.document or forwarded.video or forwarded.audio
                    kind = (
                        "video"
                        if forwarded.video
                        else "audio"
                        if forwarded.audio
                        else "document"
                    )
                    invalid = (
                        media is None
                        or kind != record.get("media_type", "document")
                        or (
                            record.get("file_unique_id")
                            and media.file_unique_id != record["file_unique_id"]
                        )
                        or (
                            record.get("file_size")
                            and media.file_size
                            and media.file_size != record["file_size"]
                        )
                    )
                    if invalid:
                        await receipts.update_one(
                            {"_id": operation},
                            {
                                "$set": {
                                    "state": "failed",
                                    "forwarded_message": forwarded.message_id,
                                    "reason": "source_media_identity_changed",
                                    "updated_at": now(),
                                }
                            },
                        )
                        raise ValueError(
                            "The source media type/identity/size changed. Reindex and inspect it before attempting repair."
                        )
                    receipt = {
                        "state": "forwarded",
                        "actor_id": message.from_user.id,
                        "source_chat": record["source_chat"],
                        "source_message": record["message_id"],
                        "expected_unique_id": record.get("file_unique_id"),
                        "forwarded_message": forwarded.message_id,
                        "handle": media.file_id,
                        "unique_id": media.file_unique_id,
                        "bot_id": message.bot.id,
                        "updated_at": now(),
                    }
                    await receipts.update_one({"_id": operation}, {"$set": receipt})
                catalog, _ = await locate(store, file_id)
                permitted_identities = list(
                    {receipt.get("expected_unique_id"), receipt["unique_id"]}
                )
                result = await catalog.files.update_one(
                    {
                        "_id": file_id,
                        "deleted_at": None,
                        "source_chat": receipt["source_chat"],
                        "message_id": receipt["source_message"],
                        "file_unique_id": {"$in": permitted_identities},
                    },
                    {
                        "$set": {
                            "file_id": receipt["handle"],
                            "file_unique_id": receipt["unique_id"],
                            "bot_id": receipt["bot_id"],
                            "handle_repaired_at": now(),
                            "updated_at": now(),
                        }
                    },
                )
                if not result.matched_count:
                    raise ValueError(
                        "Catalog identity changed during repair. No handle was overwritten; inspect the source and retry."
                    )
                await receipts.update_one(
                    {"_id": operation},
                    {"$set": {"state": "applied", "applied_at": now()}},
                )
                await respond(
                    message,
                    "✅ Bot-specific file handle refreshed from the authorized source. Other file records and variants are unchanged.",
                )
            finally:
                if forwarded is not None:
                    cleanup = {
                        "actor_id": message.from_user.id,
                        "forwarded_message": forwarded.message_id,
                    }
                    await cleanup_repair(
                        message.bot, store, receipts, operation, cleanup
                    )
                elif receipt and receipt.get("forwarded_message"):
                    await cleanup_repair(
                        message.bot, store, receipts, operation, receipt
                    )
        except (ValueError, DeliveryError) as exc:
            await respond(message, escape(str(exc)))

    return router


async def cleanup_repair(bot, store, receipts, operation, receipt):
    message_id = receipt.get("forwarded_message")
    if not message_id or receipt.get("cleanup") == "removed":
        return
    try:
        await bot.delete_message(receipt["actor_id"], message_id)
        cleanup = "removed"
    except (TelegramAPIError, TimeoutError):
        await store.enqueue(
            "delete",
            {"chat_id": receipt["actor_id"], "message_id": message_id},
            key=f"repair-cleanup:{operation}",
        )
        cleanup = "queued"
    await receipts.update_one({"_id": operation}, {"$set": {"cleanup": cleanup}})


async def acquire_lock(store, owner):
    lock = store.db.curation_locks
    stamp = now()
    try:
        await lock.find_one_and_update(
            {
                "_id": "merge",
                "$or": [
                    {"lease_until": {"$lte": stamp}},
                    {"owner": owner},
                ],
            },
            {"$set": {"owner": owner, "lease_until": stamp + timedelta(seconds=120)}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
    except DuplicateKeyError:
        raise CurationBusy(
            "Another title assignment/merge is active. Retry shortly."
        ) from None


async def run_job(job, store):
    """Merge title assignments; each file update and its audit are one atomic write."""
    payload = job["payload"]
    source_id, target_id = payload["source_title"], payload["target_title"]
    lock = store.db.curation_locks
    lock_owner = f"{job['_id']}:{job['worker_id']}"
    await acquire_lock(store, lock_owner)
    try:
        source = await store.db.titles.find_one({"_id": source_id})
        target = await store.db.titles.find_one(
            {"_id": target_id, "merged_into": {"$exists": False}}
        )
        if (
            not source
            or not target
            or (source.get("merged_into") and source["merged_into"] != target_id)
        ):
            raise ValueError(
                "Merge titles changed; inspect the title assignments before retrying."
            )
        for catalog in store.catalogs:
            while True:
                stamp = now()
                fence = {
                    "_id": job["_id"],
                    "worker_id": job["worker_id"],
                    "status": "running",
                    "lease_until": {"$gt": stamp},
                }
                if not await store.db.jobs.find_one(fence, {"_id": 1}):
                    return
                renewed = await lock.update_one(
                    {
                        "_id": "merge",
                        "owner": lock_owner,
                        "lease_until": {"$gt": stamp},
                    },
                    {"$set": {"lease_until": stamp + timedelta(seconds=120)}},
                )
                if not renewed.matched_count:
                    raise CurationBusy(
                        "Merge lease expired; resume from the remaining assignments."
                    )
                rows = (
                    await catalog.files.find(
                        {"title_id": source_id, "deleted_at": None}
                    )
                    .sort("_id", 1)
                    .limit(20)
                    .to_list(20)
                )
                if not rows:
                    break
                for row in rows:
                    if not await store.db.jobs.find_one(
                        {**fence, "lease_until": {"$gt": now()}}, {"_id": 1}
                    ):
                        return
                    overrides = dict(row.get("corrections") or {})
                    overrides["title"] = target["title"]
                    if target.get("year"):
                        overrides["year"] = target["year"]
                    await update_file(
                        catalog,
                        row,
                        payload["actor_id"],
                        "title_merge",
                        overrides,
                        {"title_id": target_id},
                    )
                    await store.db.jobs.update_one(
                        {**fence, "lease_until": {"$gt": now()}},
                        {"$inc": {"payload.moved": 1}},
                    )
        if not await store.db.jobs.find_one(
            {
                "_id": job["_id"],
                "worker_id": job["worker_id"],
                "status": "running",
                "lease_until": {"$gt": now()},
            },
            {"_id": 1},
        ):
            return
        await store.db.titles.update_one(
            {"_id": source_id},
            {
                "$set": {
                    "merged_into": target_id,
                    "merged_at": now(),
                    "merged_by": payload["actor_id"],
                }
            },
        )
    finally:
        await lock.delete_one({"_id": "merge", "owner": lock_owner})
