"""Rebuild Wovie's derived parser/search metadata with resumable checkpoints."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import json
import secrets

from pymongo import AsyncMongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

from .config import Settings
from .parser import ascii_normalize, normalize, parse_release, tokenize
from .store import SCHEMA_VERSION, Store

UTC = timezone.utc
PARSER_VERSION = parse_release("metadata.mkv")["parser_version"]


def now():
    return datetime.now(UTC)


def pending():
    return {
        "$or": [{"parser_version": {"$lt": PARSER_VERSION}}, {"parser_version": None}]
    }


async def plan(settings):
    """Open read-only clients; no Store.connect, indexes or metadata writes."""
    clients = []
    result = {
        "schema_version": SCHEMA_VERSION,
        "parser_version": PARSER_VERSION,
        "catalogs": [],
    }
    try:
        if len(settings.catalog_uris) > 7:
            raise ValueError("At most eight catalog databases are supported.")
        for position, uri in enumerate((settings.mongo_uri, *settings.catalog_uris)):
            client = AsyncMongoClient(
                uri,
                tz_aware=True,
                maxPoolSize=2,
                serverSelectionTimeoutMS=10000,
                socketTimeoutMS=30000,
            )
            clients.append(client)
            db = client[settings.mongo_db]
            identity = await db["_wovie_meta"].find_one({"_id": "catalog_identity"})
            if identity is None:
                raise ValueError(
                    "An uninitialized catalog was found. Start Wovie once; use importer for legacy bot databases."
                )
            schema = await db["_wovie_meta"].find_one({"_id": "schema"})
            if schema and schema.get("version", 0) > SCHEMA_VERSION:
                raise ValueError(
                    "A newer schema is present. Downgrades are refused; use the matching release."
                )
            if position == 0:
                version = await db.data_migrations.find_one({"_id": "parser_version"})
                if version and version.get("version", 0) > PARSER_VERSION:
                    raise ValueError(
                        "A newer parser migration is recorded. This release cannot downgrade it."
                    )
            future = await db.files.count_documents(
                {"parser_version": {"$gt": PARSER_VERSION}}, maxTimeMS=30000
            )
            if future:
                raise ValueError(
                    "Catalog records use a newer parser. Use a newer release instead of overwriting them."
                )
            invalid = await db.files.count_documents(
                {
                    "parser_version": {
                        "$exists": True,
                        "$ne": None,
                        "$not": {"$type": "number"},
                    }
                },
                maxTimeMS=30000,
            )
            if invalid:
                raise ValueError(
                    "Invalid parser_version metadata was found. Inspect the catalog before migration."
                )
            invalid_ids = await db.files.count_documents(
                {"_id": {"$not": {"$regex": "^[0-9a-f]{32}$"}}}, maxTimeMS=30000
            )
            if invalid_ids:
                raise ValueError(
                    "Non-Wovie file identities were found. Use the legacy importer first."
                )
            result["catalogs"].append(
                {
                    "catalog_id": identity["uuid"],
                    "records": await db.files.estimated_document_count(),
                    "pending": await db.files.count_documents(
                        pending(), maxTimeMS=30000
                    ),
                }
            )
        if len({entry["catalog_id"] for entry in result["catalogs"]}) != len(clients):
            raise ValueError("Two configured URIs point to the same catalog database.")
        return result
    finally:
        for client in clients:
            await client.close()


def derived(row):
    overrides = row.get("corrections") or {}
    if not isinstance(overrides, dict) or set(overrides) - {
        "title",
        "year",
        "season",
        "episode",
        "quality",
        "languages",
        "aliases",
    }:
        raise ValueError(
            "Invalid correction metadata; inspect this record before retrying."
        )
    fields = parse_release(row.get("filename") or "", row.get("caption") or "")
    fields["aliases"] = row.get(
        "source_aliases", row.get("aliases", []) if "aliases" not in overrides else []
    )
    fields.update(overrides)
    if (
        not isinstance(fields["title"], str)
        or not isinstance(fields["aliases"], list)
        or not all(isinstance(v, str) for v in fields["aliases"])
    ):
        raise ValueError(
            "Invalid title/alias metadata; inspect this record before retrying."
        )
    fields["title_norm"] = normalize(fields["title"])
    fields["title_ascii"] = ascii_normalize(fields["title"])
    fields["tokens"] = tokenize(
        " ".join(
            [
                fields["title"],
                row.get("filename") or "",
                row.get("caption") or "",
                *fields["aliases"],
            ]
        )
    )
    return fields


async def refresh(catalog, row):
    for _ in range(5):
        if not isinstance(row.get("_id"), str):
            raise ValueError(
                "Non-Wovie record identity detected. Use the legacy importer first."
            )
        if row.get("parser_version", 0) and row["parser_version"] > PARSER_VERSION:
            raise ValueError(
                "A newer parser wrote this record during migration; stop and upgrade."
            )
        if row.get("parser_version") == PARSER_VERSION:
            return False
        fields = derived(row)
        selector = {
            "_id": row["_id"],
            "curation_version": row["curation_version"]
            if "curation_version" in row
            else {"$exists": False},
            "updated_at": row["updated_at"]
            if "updated_at" in row
            else {"$exists": False},
        }
        result = await catalog.files.update_one(
            selector, {"$set": {**fields, "updated_at": now()}}
        )
        if result.matched_count:
            return True
        row = await catalog.files.find_one({"_id": row["_id"]})
        if row is None:
            return False  # An explicit concurrent permanent purge won; do not resurrect it.
    raise RuntimeError(
        "A catalog record keeps changing. Pause bot writes and resume migration."
    )


async def apply(settings, preview, limit=0):
    store = Store(settings)
    await store.connect()
    owner = secrets.token_hex(16)
    count = 0
    summary = []
    try:
        if set(store.catalog_ids) != {
            entry["catalog_id"] for entry in preview["catalogs"]
        }:
            raise ValueError(
                "Catalog identities changed after planning. Stop writers and run the plan again."
            )
        for catalog_id, catalog in zip(store.catalog_ids, store.catalogs):
            if limit and count >= limit:
                break
            key = f"parser:{PARSER_VERSION}:{catalog_id}"
            existing = await store.db.data_migrations.find_one({"_id": key})
            if existing and existing.get("status") == "done":
                if not await catalog.files.count_documents(pending(), limit=1):
                    summary.append(
                        {"catalog_id": catalog_id, "status": "already_current"}
                    )
                    continue
                # A newly imported/added older record may sort before a completed
                # cursor. A completed pass with new work starts from the beginning.
                await store.db.data_migrations.update_one(
                    {"_id": key, "status": "done"},
                    {
                        "$set": {"cursor": "", "status": "paused"},
                    },
                )
            stamp = now()
            try:
                state = await store.db.data_migrations.find_one_and_update(
                    {
                        "_id": key,
                        "$or": [
                            {"lease_until": {"$lte": stamp}},
                            {"lease_until": {"$exists": False}},
                        ],
                    },
                    {
                        "$set": {
                            "owner": owner,
                            "status": "running",
                            "updated_at": stamp,
                            "lease_until": stamp + timedelta(seconds=120),
                        },
                        "$setOnInsert": {
                            "cursor": "",
                            "changed": 0,
                            "parser_version": PARSER_VERSION,
                            "catalog_id": catalog_id,
                            "created_at": stamp,
                        },
                    },
                    upsert=True,
                    return_document=ReturnDocument.AFTER,
                )
            except DuplicateKeyError:
                raise ValueError(
                    "Another migration owns this catalog lease. Wait for it to finish or expire."
                ) from None
            complete = False
            cursor = state.get("cursor", "")
            migrated = 0
            try:
                while not limit or count < limit:
                    size = min(100, limit - count) if limit else 100
                    rows = (
                        await catalog.files.find(
                            {"$and": [pending(), {"_id": {"$gt": cursor}}]}
                        )
                        .sort("_id", 1)
                        .limit(size)
                        .to_list(size)
                    )
                    if not rows:
                        complete = True
                        break
                    for row in rows:
                        stamp = now()
                        fence = {
                            "_id": key,
                            "owner": owner,
                            "status": "running",
                            "lease_until": {"$gt": stamp},
                        }
                        if not await store.db.data_migrations.find_one(
                            fence, {"_id": 1}
                        ):
                            raise RuntimeError(
                                "Migration lease lost. Rerun from the saved cursor."
                            )
                        changed = await refresh(catalog, row)
                        cursor = row["_id"]
                        count += 1
                        migrated += int(changed)
                        result = await store.db.data_migrations.update_one(
                            fence,
                            {
                                "$set": {
                                    "cursor": cursor,
                                    "updated_at": now(),
                                    "lease_until": now() + timedelta(seconds=120),
                                },
                                "$inc": {"changed": int(changed)},
                            },
                        )
                        if not result.matched_count:
                            raise RuntimeError(
                                "Migration lease lost after a write; replay is safe."
                            )
                if complete and await catalog.files.count_documents(pending(), limit=1):
                    # Old-version rows appeared behind the saved cursor. Persist a
                    # fresh pass instead of incorrectly marking the catalog complete.
                    complete = False
                    await store.db.data_migrations.update_one(
                        {"_id": key, "owner": owner}, {"$set": {"cursor": ""}}
                    )
                summary.append(
                    {
                        "catalog_id": catalog_id,
                        "changed_this_run": migrated,
                        "status": "done" if complete else "paused",
                    }
                )
            finally:
                await store.db.data_migrations.update_one(
                    {"_id": key, "owner": owner},
                    {
                        "$set": {
                            "status": "done" if complete else "paused",
                            "updated_at": now(),
                            "lease_until": now(),
                        },
                        "$unset": {"owner": ""},
                    },
                )
        remaining = sum(
            [
                await catalog.files.count_documents(pending(), maxTimeMS=30000)
                for catalog in store.catalogs
            ]
        )
        if remaining == 0:
            await store.db.data_migrations.update_one(
                {"_id": "parser_version"},
                {
                    "$max": {"version": PARSER_VERSION},
                    "$set": {"completed_at": now()},
                },
                upsert=True,
            )
        return {
            "mode": "apply",
            "parser_version": PARSER_VERSION,
            "processed_this_run": count,
            "remaining": remaining,
            "catalogs": summary,
        }
    finally:
        await store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--plan", action="store_true", help="Read-only counts; the default"
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="Rebuild derived metadata and save resumable cursors",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Maximum records this invocation; zero is unlimited",
    )
    args = parser.parse_args()
    if args.limit < 0:
        parser.error("--limit cannot be negative")

    async def run():
        settings = Settings.from_env(require_bot=False)
        preview = await plan(settings)
        return (
            await apply(settings, preview, args.limit)
            if args.apply
            else {"mode": "plan", **preview}
        )

    try:
        print(json.dumps(asyncio.run(run()), indent=2))
    except KeyboardInterrupt:
        raise SystemExit(
            "Interrupted. Rerun --apply to resume the saved catalog cursors."
        ) from None
    except Exception as exc:
        text = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        raise SystemExit("Migration stopped: " + text) from None


if __name__ == "__main__":
    main()
