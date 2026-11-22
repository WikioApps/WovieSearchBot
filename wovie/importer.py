"""Read an old MongoDB catalog and migrate validated records without modifying it."""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sys
from typing import Any

from bson import json_util
from pymongo import AsyncMongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

from .config import Settings
from .store import Store

UTC = timezone.utc
DEFAULT_MAPPING = {
    "source_chat": "source_chat",
    "message_id": "message_id",
    "filename": "filename",
    "caption": "caption",
    "file_size": "file_size",
    "media_type": "media_type",
    "file_id": "file_id",
    "file_unique_id": "file_unique_id",
    "bot_id": "bot_id",
    "aliases": "aliases",
}


def field(row: dict, path: str) -> Any:
    value: Any = row
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def convert(
    row: dict,
    mapping: dict,
    bot_id: int,
    source_chat: int | None,
    legacy_bot_id: int | None,
) -> tuple[dict, bool]:
    record = {name: field(row, path) for name, path in mapping.items()}
    if record.get("source_chat") is None:
        record["source_chat"] = source_chat
    try:
        record["source_chat"] = int(record["source_chat"])
        record["message_id"] = int(record["message_id"])
        record["file_size"] = int(record.get("file_size") or 0)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("missing_or_invalid_source_message_or_size") from None
    if (
        record["source_chat"] >= 0
        or record["message_id"] <= 0
        or record["file_size"] < 0
    ):
        raise ValueError("invalid_source_message_or_size")
    if not isinstance(record.get("filename"), str) or not record["filename"].strip():
        raise ValueError("missing_filename")
    if record.get("caption") is not None and not isinstance(record["caption"], str):
        raise ValueError("caption_must_be_text")
    record["caption"] = record.get("caption") or ""
    record["media_type"] = str(record.get("media_type") or "document").lower()
    if record["media_type"] not in {"document", "video", "audio"}:
        raise ValueError("unsupported_media_type")
    aliases = record.get("aliases")
    if aliases is not None and (
        not isinstance(aliases, list) or not all(isinstance(a, str) for a in aliases)
    ):
        raise ValueError("aliases_must_be_list_of_text")
    handle = record.get("file_id")
    had_handle = bool(handle)
    try:
        old_bot = int(record.get("bot_id") or legacy_bot_id or 0)
    except (TypeError, ValueError, OverflowError):
        old_bot = 0
    # A bot's token rotation does not change its numeric identity. Another bot's
    # handles must never be treated as deliverable by the current bot.
    if (
        old_bot != bot_id
        or not isinstance(handle, str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{10,1024}", handle)
    ):
        record["file_id"] = None
        record["file_unique_id"] = None
        record["bot_id"] = None
    else:
        record["bot_id"] = bot_id
        if not isinstance(record.get("file_unique_id"), str):
            record["file_unique_id"] = None
    return record, had_handle and record["file_id"] is None


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-db", required=True)
    parser.add_argument("--collection", required=True)
    parser.add_argument(
        "--mapping",
        type=Path,
        help="JSON mapping of Wovie fields to old dotted field paths",
    )
    parser.add_argument(
        "--source-chat",
        type=int,
        help="Default source ID only when the old row has no source field",
    )
    parser.add_argument(
        "--legacy-bot-id", type=int, help="Original bot identity, when absent from rows"
    )
    parser.add_argument(
        "--job",
        default="legacy-catalog",
        help="Persistent import name; reuse it to resume",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Maximum rows this run; dry-run default is 1,000",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write target records; otherwise read-only preview",
    )
    return parser.parse_args()


async def run(args: argparse.Namespace) -> dict:
    settings = Settings.from_env()
    uri = os.getenv("IMPORT_MONGO_URI", "").strip()
    if not uri.startswith(("mongodb://", "mongodb+srv://")):
        raise ValueError(
            "Set IMPORT_MONGO_URI to a read-only connection for the legacy database."
        )
    if not re.fullmatch(r"[\w.-]{1,64}", args.job):
        raise ValueError(
            "--job accepts 1–64 letters, digits, underscores, dots or hyphens."
        )
    if (
        not args.source_db
        or not args.collection
        or args.collection.startswith("system.")
    ):
        raise ValueError("Choose an ordinary source database and collection.")
    if args.limit < 0:
        raise ValueError("--limit cannot be negative.")
    if args.source_chat is not None and args.source_chat >= 0:
        raise ValueError("--source-chat must be a negative channel/group ID.")
    mapping = dict(DEFAULT_MAPPING)
    if args.mapping:
        changes = json.loads(args.mapping.read_text())
        if not isinstance(changes, dict) or set(changes) - set(DEFAULT_MAPPING):
            raise ValueError("Mapping contains unsupported fields.")
        if not all(
            isinstance(p, str) and p and len(p) <= 200 for p in changes.values()
        ):
            raise ValueError(
                "Every mapping value must be a nonempty dotted field path."
            )
        mapping.update(changes)
    bot_id = int(settings.bot_token.split(":", 1)[0])
    # Credentials may rotate without invalidating the import's source identity.
    source_location = uri.split("://", 1)[1].rsplit("@", 1)[-1]
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "source": source_location,
                "database": args.source_db,
                "collection": args.collection,
                "mapping": mapping,
                "source_chat": args.source_chat,
                "legacy_bot": args.legacy_bot_id,
                "current_bot": bot_id,
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    source_client = AsyncMongoClient(
        uri,
        appname="WovieReadOnlyImport",
        tz_aware=True,
        maxPoolSize=2,
        serverSelectionTimeoutMS=10000,
        socketTimeoutMS=30000,
    )
    target_client = None
    store = None
    state = None
    owner = secrets.token_hex(16)
    counters: Counter = Counter()
    samples: list[dict] = []
    source = source_client[args.source_db][args.collection]
    cursor_value = None
    complete = False
    try:
        await source_client.admin.command("ping")
        # MongoDB comparison predicates bracket BSON types. Reject mixed _id types
        # rather than silently skipping another type when resuming with $gt.
        types = await (
            await source.aggregate(
                [
                    {"$group": {"_id": {"$type": "$_id"}}},
                    {"$limit": 3},
                ],
                maxTimeMS=30000,
            )
        ).to_list(3)
        if len(types) > 1 or (
            types and types[0]["_id"] not in {"objectId", "string", "int", "long"}
        ):
            raise ValueError(
                "Source _id values must all use one supported BSON type: ObjectId, string, int or long."
            )
        if args.apply:
            store = Store(settings)
            await store.connect()
            target = store.db
            if args.collection == "files" and args.source_db == settings.mongo_db:
                identity = await source_client[args.source_db]["_wovie_meta"].find_one(
                    {"_id": "catalog_identity"}
                )
                if identity and identity.get("uuid") in store.catalog_ids:
                    raise ValueError(
                        "The selected source is a live Wovie target catalog. Use its backup/restore workflow instead."
                    )
            existing = await target.imports.find_one({"_id": args.job})
            if existing and existing["fingerprint"] != fingerprint:
                raise ValueError(
                    "This import name belongs to different inputs. Choose a new --job name."
                )
            if existing and existing.get("status") == "done":
                return {
                    "mode": "apply",
                    "job": args.job,
                    "status": "already_complete",
                    "totals": existing.get("totals", {}),
                }
            now = datetime.now(UTC)
            try:
                state = await target.imports.find_one_and_update(
                    {
                        "_id": args.job,
                        "fingerprint": fingerprint,
                        "$or": [
                            {"lease_until": {"$lte": now}},
                            {"lease_until": {"$exists": False}},
                        ],
                    },
                    {
                        "$set": {
                            "owner": owner,
                            "status": "running",
                            "updated_at": now,
                            "lease_until": now + timedelta(seconds=120),
                        },
                        "$setOnInsert": {
                            "fingerprint": fingerprint,
                            "created_at": now,
                            "cursor": None,
                            "totals": {},
                        },
                    },
                    upsert=True,
                    return_document=ReturnDocument.AFTER,
                )
            except DuplicateKeyError:
                raise ValueError(
                    "Another process holds this import lease. Wait for it or use its process to resume."
                ) from None
            if state is None:
                raise ValueError("Could not acquire the import lease.")
            cursor_value = state.get("cursor")
            counters.update(state.get("totals", {}))
        else:
            target_client = AsyncMongoClient(
                settings.mongo_uri,
                appname="WovieImportPreview",
                tz_aware=True,
                maxPoolSize=2,
                serverSelectionTimeoutMS=10000,
                socketTimeoutMS=30000,
            )
            target = target_client[settings.mongo_db]
        limit = args.limit or (0 if args.apply else 1000)
        processed = 0
        while not limit or processed < limit:
            selector = (
                {"_id": {"$gt": cursor_value}} if cursor_value is not None else {}
            )
            size = min(100, limit - processed) if limit else 100
            rows = await source.find(selector).sort("_id", 1).limit(size).to_list(size)
            if not rows:
                complete = True
                break
            for row in rows:
                reason = None
                try:
                    record, discarded_handle = convert(
                        row, mapping, bot_id, args.source_chat, args.legacy_bot_id
                    )
                    if not await target.sources.find_one(
                        {"_id": record["source_chat"], "active": True}, {"_id": 1}
                    ):
                        raise ValueError("source_not_registered_and_active")
                    if args.apply:
                        await store.put_file(record)
                    counters["valid"] += 1
                    counters["discarded_old_bot_handles"] += int(discarded_handle)
                except ValueError as exc:
                    reason = str(exc)
                    counters["rejected"] += 1
                    if len(samples) < 10:
                        samples.append(
                            {
                                "source_id": json_util.loads(
                                    json_util.dumps(row["_id"])
                                ),
                                "reason": reason,
                            }
                        )
                counters["scanned"] += 1
                processed += 1
                cursor_value = row["_id"]
                if args.apply:
                    now = datetime.now(UTC)
                    fence = {
                        "_id": args.job,
                        "owner": owner,
                        "status": "running",
                        "lease_until": {"$gt": now},
                    }
                    if reason:
                        reject_id = (
                            args.job
                            + ":"
                            + hashlib.sha256(
                                json_util.dumps(row["_id"]).encode()
                            ).hexdigest()
                        )
                        await target.import_rejections.update_one(
                            {"_id": reject_id},
                            {
                                "$set": {
                                    "job": args.job,
                                    "source_id": row["_id"],
                                    "reason": reason,
                                    "updated_at": now,
                                }
                            },
                            upsert=True,
                        )
                    updated = await target.imports.update_one(
                        fence,
                        {
                            "$set": {
                                "cursor": cursor_value,
                                "totals": dict(counters),
                                "updated_at": now,
                                "lease_until": now + timedelta(seconds=120),
                            }
                        },
                    )
                    if not updated.matched_count:
                        raise RuntimeError(
                            "Import lease lost. Stop this process and resume from the saved checkpoint."
                        )
        return {
            "mode": "apply" if args.apply else "preview",
            "job": args.job,
            "status": "complete" if complete else "limit_reached",
            "rows_this_run": processed,
            "totals": dict(counters),
            "rejected_samples": samples,
            "legacy_writes": 0,
            "target_catalog_writes": args.apply,
        }
    finally:
        if store is not None:
            try:
                if state is not None:
                    await store.db.imports.update_one(
                        {"_id": args.job, "owner": owner},
                        {
                            "$set": {
                                "status": "done" if complete else "paused",
                                "lease_until": datetime.now(UTC),
                                "updated_at": datetime.now(UTC),
                            },
                            "$unset": {"owner": ""},
                        },
                    )
            finally:
                await store.close()
        if target_client is not None:
            await target_client.close()
        await source_client.close()


def main() -> None:
    try:
        summary = asyncio.run(run(arguments()))
        print(json_util.dumps(summary, indent=2))
    except KeyboardInterrupt:
        raise SystemExit(
            "Interrupted. Rerun the same --job and arguments to resume."
        ) from None
    except Exception as exc:
        # Driver exception strings may contain server names or credential-bearing URIs.
        if isinstance(exc, ValueError):
            print(f"Import configuration/data error: {exc}", file=sys.stderr)
        else:
            print(
                f"Import stopped ({type(exc).__name__}); correct the connection or data issue and resume.",
                file=sys.stderr,
            )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
