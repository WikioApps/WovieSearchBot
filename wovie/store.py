"""MongoDB persistence, bounded catalog search and leased background work.

The primary database owns account/configuration/job state. Catalog shards own
file documents. Shard identities live in MongoDB, so credentials can rotate and
URI ordering can change without moving a source or silently losing its records.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
import hashlib
import heapq
import re
import secrets
from typing import Any

from pymongo import ASCENDING, DESCENDING, AsyncMongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

from .parser import (
    ascii_normalize,
    canonical_codec,
    canonical_language,
    canonical_quality,
    normalize,
    parse_query,
    parse_release,
    parse_size,
    tokenize,
)

UTC = timezone.utc
SCHEMA_VERSION = 1
LEASE_SECONDS = 120
MAX_ATTEMPTS = 8
MAX_PAGE = 50


def utcnow() -> datetime:
    return datetime.now(UTC)


def clean_error(error: object) -> str:
    text = str(error)
    text = re.sub(
        r"mongodb(?:\+srv)?://[^\s]+", "[database URI redacted]", text, flags=re.I
    )
    text = re.sub(r"\b\d{6,15}:[A-Za-z0-9_-]{20,}\b", "[bot token redacted]", text)
    return text[:1200]


class Store:
    def __init__(self, settings: Any):
        self.settings = settings
        self.clients: list[AsyncMongoClient] = []
        self.catalogs: list[Any] = []
        self.catalog_ids: list[str] = []
        self._size_samples: dict[int, tuple[float, int]] = {}
        self._by_catalog_id: dict[str, Any] = {}
        self.db: Any = None

    async def connect(self) -> None:
        if self.clients:
            raise RuntimeError("Store is already connected.")
        uris = [self.settings.mongo_uri, *self.settings.catalog_uris]
        if not uris[0] or len(uris) > 8:
            raise ValueError(
                "One primary MongoDB URI is required; at most eight catalog databases are supported."
            )
        try:
            for position, uri in enumerate(uris):
                client = AsyncMongoClient(
                    uri,
                    appname="WovieSearchBot",
                    tz_aware=True,
                    maxPoolSize=5,
                    minPoolSize=0,
                    maxIdleTimeMS=60000,
                    serverSelectionTimeoutMS=10000,
                    connectTimeoutMS=10000,
                    socketTimeoutMS=30000,
                    retryWrites=True,
                    w="majority",
                    journal=True,
                )
                self.clients.append(client)
                await client.admin.command("ping")
                database = (
                    client[self.settings.mongo_db]
                    if position == 0
                    else client.get_default_database(default=self.settings.mongo_db)
                )
                if database.name in {"admin", "config", "local"}:
                    raise ValueError(
                        "Use a dedicated application database, not admin, config or local."
                    )
                if "_wovie_restore_lock" in await database.list_collection_names():
                    raise RuntimeError(
                        "An incomplete restore lock exists. Finish recovery before starting the bot."
                    )
                identity = await database["_wovie_meta"].find_one_and_update(
                    {"_id": "catalog_identity"},
                    {
                        "$setOnInsert": {
                            "uuid": secrets.token_hex(16),
                            "created_at": utcnow(),
                        }
                    },
                    upsert=True,
                    return_document=ReturnDocument.AFTER,
                )
                catalog_id = identity["uuid"]
                if catalog_id in self._by_catalog_id:
                    raise RuntimeError(
                        "Two configured MongoDB URIs point to the same catalog database."
                    )
                self.catalogs.append(database)
                self.catalog_ids.append(catalog_id)
                self._by_catalog_id[catalog_id] = database
            self.db = self.catalogs[0]
            previous = await self.db["_wovie_meta"].find_one(
                {"_id": "catalog_registry"}
            )
            if previous and not set(previous["catalog_ids"]).issubset(self.catalog_ids):
                raise RuntimeError(
                    "A previously registered catalog is missing. Restore its URI or migrate its data before startup."
                )
            schema = await self.db["_wovie_meta"].find_one({"_id": "schema"})
            if schema and schema.get("version", 0) > SCHEMA_VERSION:
                raise RuntimeError(
                    "Database schema is newer than this application. Use a compatible release."
                )
            await self._create_indexes()
            await self.db["_wovie_meta"].update_one(
                {"_id": "catalog_registry"},
                {
                    "$set": {
                        "catalog_ids": sorted(self.catalog_ids),
                        "updated_at": utcnow(),
                    }
                },
                upsert=True,
            )
            await self.db["_wovie_meta"].update_one(
                {"_id": "schema"},
                {"$set": {"version": SCHEMA_VERSION, "updated_at": utcnow()}},
                upsert=True,
            )
        except BaseException:
            await self.close()
            raise

    async def _create_indexes(self) -> None:
        for catalog in self.catalogs:
            await catalog.files.create_index(
                [("source_chat", ASCENDING), ("message_id", ASCENDING)],
                unique=True,
                name="source_message",
            )
            await catalog.files.create_index(
                [("tokens", ASCENDING), ("source_chat", ASCENDING)], name="token_source"
            )
            await catalog.files.create_index(
                [("title_norm", ASCENDING), ("source_chat", ASCENDING)],
                name="title_source",
            )
            await catalog.files.create_index(
                [
                    ("source_chat", ASCENDING),
                    ("deleted_at", ASCENDING),
                    ("message_id", DESCENDING),
                ],
                name="source_live",
            )
            await catalog.files.create_index(
                [("file_unique_id", ASCENDING)], sparse=True, name="file_identity"
            )
            await catalog.files.create_index(
                [("deleted_at", ASCENDING)], name="recycle_bin"
            )
            await catalog.files.create_index(
                [("created_at", ASCENDING), ("_id", ASCENDING)], name="new_file_scan"
            )
            await catalog.files.create_index(
                [("source_chat", ASCENDING), ("codec", ASCENDING)], name="source_codec"
            )
            await catalog.files.create_index(
                [("source_chat", ASCENDING), ("extension", ASCENDING)],
                name="source_extension",
            )
            await catalog.files.create_index(
                [("source_chat", ASCENDING), ("file_size", ASCENDING)],
                name="source_size",
            )
            await catalog.files.create_index(
                [
                    ("title_id", ASCENDING),
                    ("deleted_at", ASCENDING),
                    ("_id", ASCENDING),
                ],
                name="title_variants",
            )
        await self.db.sources.create_index([("active", ASCENDING)])
        await self.db.jobs.create_index(
            [
                ("status", ASCENDING),
                ("kind", ASCENDING),
                ("run_at", ASCENDING),
                ("lease_until", ASCENDING),
            ],
            name="work_claim",
        )
        await self.db.jobs.create_index(
            [("expires_at", ASCENDING)], expireAfterSeconds=0
        )
        for name in (
            "links",
            "search_sessions",
            "rate_limits",
            "delivery_quota",
            "membership_cache",
            "inbox",
            "search_history",
            "delivery_receipts",
        ):
            await self.db[name].create_index(
                [("expires_at", ASCENDING)], expireAfterSeconds=0
            )
        await self.db.users.create_index([("banned", ASCENDING), ("_id", ASCENDING)])
        for name in ("favorites", "watchlist", "requests", "search_history"):
            await self.db[name].create_index(
                [("user_id", ASCENDING), ("created_at", DESCENDING)]
            )
        await self.db.requests.create_index(
            [("status", ASCENDING), ("created_at", DESCENDING)]
        )
        await self.db.filters.create_index(
            [("chat_id", ASCENDING), ("keyword", ASCENDING)], unique=True
        )
        await self.db.source_routes.create_index([("catalog_id", ASCENDING)])
        await self.db.referral_codes.create_index([("token", ASCENDING)], unique=True)
        await self.db.referral_claims.create_index([("status", ASCENDING)])
        await self.db.referral_claims.create_index(
            [("referrer", ASCENDING), ("status", ASCENDING)]
        )
        await self.db.join_events.create_index(
            [("expires_at", ASCENDING)], expireAfterSeconds=0
        )
        await self.db.verifications.create_index(
            [("expires_at", ASCENDING)], expireAfterSeconds=0
        )
        await self.db.verifications.create_index(
            [("user_id", ASCENDING), ("issued_at", DESCENDING), ("status", ASCENDING)]
        )
        await self.db.audit.create_index(
            [("created_at", DESCENDING)], expireAfterSeconds=180 * 86400
        )
        await self.db.discovery_sessions.create_index(
            [("expires_at", ASCENDING)], expireAfterSeconds=0
        )
        await self.db.watched.create_index(
            [("user_id", ASCENDING), ("created_at", DESCENDING)]
        )
        await self.db.delivery_receipts.create_index(
            [
                ("count_for_popularity", ASCENDING),
                ("source_chat", ASCENDING),
                ("sent_at", DESCENDING),
            ]
        )
        await self.db.jobs.create_index(
            [("payload.actor_id", ASCENDING), ("admission_slot", ASCENDING)],
            unique=True,
            name="user_batch_slots",
            partialFilterExpression={
                "kind": "batch",
                "admission_slot": {"$exists": True},
                "status": {"$in": ["queued", "running", "paused", "failed"]},
            },
        )

    async def close(self) -> None:
        for client in self.clients:
            await client.close()
        self.clients.clear()
        self.catalogs.clear()
        self.catalog_ids.clear()
        self._by_catalog_id.clear()
        self.db = None

    async def _catalog_for_source(self, source_chat: int) -> Any:
        route = await self.db.source_routes.find_one({"_id": source_chat})
        if route is None:
            # Assignment happens once; adding a shard never redistributes old sources.
            ordered = sorted(self.catalog_ids)
            position = int.from_bytes(
                hashlib.sha256(str(source_chat).encode()).digest()[:8], "big"
            ) % len(ordered)
            try:
                route = await self.db.source_routes.find_one_and_update(
                    {"_id": source_chat},
                    {
                        "$setOnInsert": {
                            "catalog_id": ordered[position],
                            "created_at": utcnow(),
                        }
                    },
                    upsert=True,
                    return_document=ReturnDocument.AFTER,
                )
            except DuplicateKeyError:
                route = await self.db.source_routes.find_one({"_id": source_chat})
        database = self._by_catalog_id.get(route["catalog_id"])
        if database is None:
            raise RuntimeError(
                "The source catalog is unavailable. Restore its configured database."
            )
        return database

    async def put_file(self, record: dict[str, Any]) -> str:
        chat_id, message_id = int(record["source_chat"]), int(record["message_id"])
        if not chat_id or message_id <= 0:
            raise ValueError(
                "A valid source chat and positive message ID are required."
            )
        file_id = hashlib.sha256(f"{chat_id}:{message_id}".encode()).hexdigest()[:32]
        now = utcnow()
        filename = str(record.get("filename") or f"Media {message_id}")[:512]
        caption = str(record.get("caption") or "")[:4096]
        base = parse_release(filename, caption)
        base.update(
            {
                "source_chat": chat_id,
                "message_id": message_id,
                "filename": filename,
                "caption": caption,
                "file_size": max(0, int(record.get("file_size") or 0)),
                "media_type": str(record.get("media_type") or "document")[:32],
                "updated_at": now,
            }
        )
        aliases = record.get("aliases")
        if aliases is not None:
            base["aliases"] = [str(a)[:128] for a in aliases[:20]]
            base["source_aliases"] = base["aliases"]
        # A user-session scan has no Bot API file ID. Never overwrite a good bot handle.
        for key in ("file_id", "file_unique_id", "bot_id"):
            if record.get(key):
                base[key] = record[key]
        for key in ("source_date", "source_protected"):
            if key in record:
                base[key] = record[key]
        database = await self._catalog_for_source(chat_id)
        for _ in range(5):
            existing = await database.files.find_one(
                {"_id": file_id},
                {
                    "corrections": 1,
                    "curation_version": 1,
                    "aliases": 1,
                    "source_aliases": 1,
                },
            )
            ceiling = getattr(self.settings, "catalog_max_mib", 0)
            if ceiling and not existing:
                tick = asyncio.get_running_loop().time()
                sample = self._size_samples.get(id(database))
                if not sample or tick - sample[0] >= 10:
                    usage = await database.command("dbStats", scale=1)
                    sample = (tick, int(usage["storageSize"] + usage["indexSize"]))
                    self._size_samples[id(database)] = sample
                if sample[1] >= ceiling * 1048576:
                    raise ValueError(
                        "Catalog size ceiling reached; expand the database or raise CATALOG_MAX_MIB"
                    )
            fields = dict(base)
            if aliases is None and existing and existing.get("aliases"):
                fields["aliases"] = existing.get(
                    "source_aliases",
                    existing["aliases"]
                    if "aliases" not in (existing.get("corrections") or {})
                    else [],
                )
            if existing:
                fields.update(existing.get("corrections") or {})
            fields["title_norm"] = normalize(fields["title"])
            fields["title_ascii"] = ascii_normalize(fields["title"])
            fields["tokens"] = tokenize(
                " ".join(
                    [fields["title"], filename, caption, *fields.get("aliases", [])]
                )
            )
            selector: dict[str, Any] = {"_id": file_id}
            selector["curation_version"] = (
                existing["curation_version"]
                if existing and "curation_version" in existing
                else {"$exists": False}
            )
            try:
                await database.files.update_one(
                    selector,
                    {
                        "$set": fields,
                        "$setOnInsert": {
                            "created_at": now,
                            "deleted_at": None,
                            "curation_version": 0,
                        },
                    },
                    upsert=True,
                )
                return file_id
            except DuplicateKeyError:
                # An owner corrected this file between read and write. Reapply that
                # new correction rather than replacing it with parsed metadata.
                continue
        raise RuntimeError(
            "Concurrent catalog corrections prevented ingestion; retry this message."
        )

    async def get_file(self, file_id: str) -> dict[str, Any] | None:
        if not re.fullmatch(r"[0-9a-f]{32}", str(file_id)):
            return None
        rows = await asyncio.gather(
            *(
                db.files.find_one({"_id": file_id, "deleted_at": None})
                for db in self.catalogs
            )
        )
        record = next((row for row in rows if row), None)
        if record is None:
            return None
        source = await self.db.sources.find_one(
            {"_id": record["source_chat"], "active": True}, {"_id": 1}
        )
        return record if source else None

    async def _active_source_ids(self, source_ids: list[int] | None) -> list[int]:
        query: dict[str, Any] = {"active": True}
        if source_ids is not None:
            if len(source_ids) > 5000:
                raise ValueError("Too many source scopes in one search.")
            query["_id"] = {"$in": list({int(value) for value in source_ids})}
        rows = (
            await self.db.sources.find(query, {"_id": 1})
            .limit(5001)
            .to_list(length=5001)
        )
        if len(rows) > 5000:
            raise ValueError(
                "This deployment supports at most 5,000 active sources per search."
            )
        return [row["_id"] for row in rows]

    async def search(
        self,
        query: str,
        page: int = 0,
        filters: dict[str, Any] | None = None,
        source_ids: list[int] | None = None,
    ) -> tuple[list[dict[str, Any]], bool]:
        if not isinstance(page, int) or not 0 <= page <= MAX_PAGE:
            raise ValueError(
                f"Choose a search page between 0 and {MAX_PAGE}. Refine broad queries."
            )
        rows, more = await self._search_window(
            query,
            filters,
            source_ids,
            page * self.settings.page_size,
            self.settings.page_size,
        )
        return rows, more and page < MAX_PAGE

    async def snapshot_ids(
        self,
        query: str,
        filters: dict[str, Any] | None = None,
        source_ids: list[int] | None = None,
        limit: int = 120,
    ) -> tuple[list[str], bool]:
        """Freeze a small ordered result set; consumers must recheck access on every use."""
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 200
        ):
            raise ValueError("Snapshot size must be between one and 200 records.")
        rows, capped = await self._search_window(
            query, filters, source_ids, 0, limit, ids_only=True
        )
        return [row["_id"] for row in rows], capped

    async def _search_window(
        self,
        query: str,
        filters: dict[str, Any] | None,
        source_ids: list[int] | None,
        offset: int,
        count: int,
        ids_only: bool = False,
    ) -> tuple[list[dict[str, Any]], bool]:
        text, parsed_filters = parse_query(query)
        parsed_filters.update(filters or {})
        active = await self._active_source_ids(source_ids)
        if not active:
            return [], False
        base: dict[str, Any] = {"source_chat": {"$in": active}, "deleted_at": None}
        for key, value in parsed_filters.items():
            if key in ("year", "season", "episode"):
                if (
                    isinstance(value, bool)
                    or not isinstance(value, int)
                    or not 0 <= value <= 9999
                ):
                    raise ValueError(f"Invalid {key} filter.")
                base[key] = value
            elif key == "language":
                if not isinstance(value, str):
                    raise ValueError("Invalid language filter.")
                base["languages"] = canonical_language(value)[:32]
            elif key == "quality":
                if not isinstance(value, str):
                    raise ValueError("Invalid quality filter.")
                base["quality"] = canonical_quality(value)[:16]
            elif key == "codec":
                if not isinstance(value, str):
                    raise ValueError("Invalid codec filter.")
                codec = canonical_codec(value)
                alternatives = {
                    "h264": r"(?:h[ ._-]?264|x[ ._-]?264|avc)",
                    "h265": r"(?:h[ ._-]?265|x[ ._-]?265|hevc)",
                    "mpeg4": r"mpeg[ ._-]?4",
                }
                pattern = r"\b" + alternatives.get(codec, re.escape(codec)) + r"\b"
                legacy = {
                    "codec": None,
                    "$or": [
                        {"filename": {"$regex": pattern, "$options": "i"}},
                        {"caption": {"$regex": pattern, "$options": "i"}},
                    ],
                }
                base.setdefault("$and", []).append({"$or": [{"codec": codec}, legacy]})
            elif key == "type":
                if not isinstance(value, str):
                    raise ValueError("Invalid file type filter.")
                file_type = value.casefold().lstrip(".")
                if file_type in ("video", "audio", "document"):
                    base["media_type"] = file_type
                elif file_type in (
                    "mkv",
                    "mp4",
                    "avi",
                    "mov",
                    "webm",
                    "m4v",
                    "ts",
                    "mp3",
                    "m4a",
                    "flac",
                    "wav",
                    "pdf",
                    "zip",
                    "rar",
                ):
                    base.setdefault("$and", []).append(
                        {
                            "$or": [
                                {"extension": file_type},
                                {
                                    "extension": None,
                                    "filename": {
                                        "$regex": r"\." + re.escape(file_type) + "$",
                                        "$options": "i",
                                    },
                                },
                            ]
                        }
                    )
                else:
                    raise ValueError("Unsupported file type.")
            elif key in ("minsize", "maxsize"):
                if isinstance(value, str):
                    value = parse_size(value)
                if (
                    isinstance(value, bool)
                    or not isinstance(value, int)
                    or not 0 <= value <= 2**63 - 1
                ):
                    raise ValueError("Invalid file size filter.")
                base.setdefault("file_size", {})[
                    "$gte" if key == "minsize" else "$lte"
                ] = value
            elif key in ("phrase", "phrase_rank"):
                if (
                    not isinstance(value, str)
                    or not normalize(value)
                    or len(value) > 120
                ):
                    raise ValueError("Invalid title phrase.")
                if key == "phrase":
                    phrase = normalize(value)
                    ascii_phrase = ascii_normalize(value)
                    base.setdefault("$and", []).append(
                        {
                            "$or": [
                                {
                                    "title_norm": {
                                        "$regex": r"(?:^| )"
                                        + re.escape(phrase)
                                        + r"(?: |$)"
                                    }
                                },
                                {
                                    "title_ascii": {
                                        "$regex": r"(?:^| )"
                                        + re.escape(ascii_phrase)
                                        + r"(?: |$)"
                                    }
                                },
                            ]
                        }
                    )
            else:
                raise ValueError(f"Unsupported search filter: {key}")
        size_filter = base.get("file_size", {})
        if size_filter.get("$gte", 0) > size_filter.get("$lte", 2**63 - 1):
            raise ValueError("minsize must not exceed maxsize.")
        strict_phrase = parsed_filters.get("phrase", "")
        words = tokenize(" ".join([text, strict_phrase]), transliterate=False)[:12]
        if words:
            base["tokens"] = {"$all": words}
        normalized = normalize(text or strict_phrase)
        ascii_text = ascii_normalize(text or strict_phrase)
        limit = offset + count + 1
        projection = {"_id": 1, "created_at": 1, "_rank": 1}
        if not ids_only:
            projection.update(
                {
                    key: 1
                    for key in (
                        "source_chat",
                        "message_id",
                        "filename",
                        "title",
                        "title_norm",
                        "title_ascii",
                        "year",
                        "season",
                        "episode",
                        "quality",
                        "languages",
                        "codec",
                        "extension",
                        "file_size",
                        "media_type",
                        "file_id",
                        "bot_id",
                        "title_id",
                        "source_protected",
                    )
                }
            )
        ranking = {
            "$switch": {
                "branches": [
                    {
                        "case": {"$eq": ["$title_norm", {"$literal": normalized}]},
                        "then": 100,
                    },
                    {
                        "case": {"$eq": ["$title_ascii", {"$literal": ascii_text}]},
                        "then": 95,
                    },
                    {
                        "case": {
                            "$eq": [
                                {
                                    "$indexOfCP": [
                                        {"$ifNull": ["$title_norm", ""]},
                                        {"$literal": normalized},
                                    ]
                                },
                                0,
                            ]
                        },
                        "then": 50,
                    },
                    {
                        "case": {
                            "$eq": [
                                {
                                    "$indexOfCP": [
                                        {"$ifNull": ["$title_ascii", ""]},
                                        {"$literal": ascii_text},
                                    ]
                                },
                                0,
                            ]
                        },
                        "then": 45,
                    },
                ],
                "default": 10,
            }
        }
        ranked_phrase = parsed_filters.get("phrase_rank") or strict_phrase
        if ranked_phrase:
            phrase_cases = []
            for field, phrase, rank in (
                ("title_norm", normalize(ranked_phrase), 80),
                ("title_ascii", ascii_normalize(ranked_phrase), 75),
            ):
                phrase_cases.append(
                    {
                        "case": {
                            "$regexMatch": {
                                "input": {"$ifNull": ["$" + field, ""]},
                                "regex": {
                                    "$literal": r"(?:^| )"
                                    + re.escape(phrase)
                                    + r"(?: |$)"
                                },
                            }
                        },
                        "then": rank,
                    }
                )
            ranking["$switch"]["branches"][2:2] = phrase_cases
        pipeline = [
            {"$match": base},
            {"$addFields": {"_rank": ranking}},
            {"$project": projection},
            {"$sort": {"_rank": -1, "created_at": -1, "_id": -1}},
            {"$limit": limit},
        ]

        # Merge sorted server cursors, keeping only one head and a tiny network
        # batch per shard. Deep pages never materialize every skipped document.
        cursors = []
        rows = []
        seen = 0

        async def open_cursor(database: Any):
            cursor = await database.files.aggregate(
                pipeline, maxTimeMS=10000, allowDiskUse=False, batchSize=8
            )
            cursors.append(cursor)
            return cursor

        async def next_row(cursor):
            try:
                return await cursor.__anext__()
            except StopAsyncIteration:
                return None

        def heap_key(row):
            created = row.get("created_at") or datetime(1970, 1, 1, tzinfo=UTC)
            return (-float(row["_rank"]), -created.timestamp(), -int(row["_id"], 16))

        try:
            opened = await asyncio.gather(
                *(open_cursor(database) for database in self.catalogs),
                return_exceptions=True,
            )
            for result in opened:
                if isinstance(result, BaseException):
                    raise result
            heads = await asyncio.gather(*(next_row(cursor) for cursor in opened))
            heap = [
                (heap_key(row), index, row)
                for index, row in enumerate(heads)
                if row is not None
            ]
            heapq.heapify(heap)
            while heap and seen < limit:
                _, index, row = heapq.heappop(heap)
                if seen >= offset:
                    rows.append(row)
                seen += 1
                if seen >= limit:
                    break
                following = await next_row(opened[index])
                if following is not None:
                    heapq.heappush(heap, (heap_key(following), index, following))
        finally:
            await asyncio.gather(
                *(cursor.close() for cursor in cursors), return_exceptions=True
            )
        if not seen and words:
            # Typo fallback only reads bounded indexed-prefix candidates, never the catalog.
            # Requiring a prefix means completely unrelated/misspelled beginnings return no match.
            fallback = {key: value for key, value in base.items() if key != "tokens"}
            candidates_words = list(dict.fromkeys([*words, *ascii_text.split()]))
            prefixes = [word[:3] for word in candidates_words if len(word) >= 3][:4]
            if not prefixes:
                return [], False
            fallback["$or"] = [
                {"tokens": {"$regex": "^" + re.escape(prefix)}} for prefix in prefixes
            ]

            async def candidates(database: Any) -> list[dict[str, Any]]:
                fields = {**projection, "title": 1, "title_norm": 1, "title_ascii": 1}
                return (
                    await database.files.find(fallback, fields)
                    .sort([("created_at", -1), ("_id", -1)])
                    .limit(80)
                    .max_time_ms(10000)
                    .to_list(length=80)
                )

            candidate_groups = await asyncio.gather(
                *(candidates(database) for database in self.catalogs)
            )
            for group in candidate_groups:
                for row in group:
                    score = max(
                        SequenceMatcher(
                            None,
                            normalized,
                            row.get("title_norm", "")[:256],
                            autojunk=False,
                        ).ratio(),
                        SequenceMatcher(
                            None,
                            ascii_text,
                            row.get("title_ascii")
                            or ascii_normalize(row.get("title", ""))[:256],
                            autojunk=False,
                        ).ratio(),
                    )
                    if score >= 0.55:
                        row["_rank"] = score
                        rows.append(row)
            rows.sort(
                key=lambda row: (
                    row["_rank"],
                    row.get("created_at", datetime.min.replace(tzinfo=UTC)),
                    row["_id"],
                ),
                reverse=True,
            )
            rows = rows[offset : offset + count + 1]
        return rows[:count], len(rows) > count

    async def suggest_titles(
        self, query: str, source_ids: list[int] | None = None, limit: int = 5
    ) -> list[str]:
        """Suggest existing visible titles from a bounded indexed candidate set."""
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 10
        ):
            raise ValueError("Choose between one and ten title suggestions.")
        text, filters = parse_query(query)
        text = text or filters.get("phrase", "")
        normalized = normalize(text)
        ascii_text = ascii_normalize(text)
        if not normalized:
            return []
        active = await self._active_source_ids(source_ids)
        if not active:
            return []
        words = list(dict.fromkeys([*normalized.split(), *ascii_text.split()]))[:8]
        prefixes = list(
            dict.fromkeys(word[:2] if len(word) > 2 else word for word in words)
        )[:6]
        selector = {
            "source_chat": {"$in": active},
            "deleted_at": None,
            "$or": [
                {"tokens": {"$regex": "^" + re.escape(prefix)}} for prefix in prefixes
            ],
        }
        projection = {"_id": 0, "title": 1, "title_norm": 1, "title_ascii": 1}

        async def candidates(database):
            return (
                await database.files.find(selector, projection)
                .sort([("created_at", -1), ("_id", -1)])
                .limit(80)
                .max_time_ms(5000)
                .to_list(length=80)
            )

        groups = await asyncio.gather(
            *(candidates(database) for database in self.catalogs)
        )
        titles = {}
        for group in groups:
            for row in group:
                title = str(row.get("title") or "").strip()[:256]
                key = normalize(title)
                if not key:
                    continue
                ascii_title = row.get("title_ascii") or ascii_normalize(title)
                score = max(
                    SequenceMatcher(None, normalized, key, autojunk=False).ratio(),
                    SequenceMatcher(
                        None, ascii_text, ascii_title, autojunk=False
                    ).ratio(),
                )
                if score >= 0.4 and (key not in titles or score > titles[key][0]):
                    titles[key] = (score, title)
        ranked = sorted(
            titles.values(), key=lambda item: (-item[0], normalize(item[1]))
        )
        return [title for _, title in ranked[:limit]]

    async def enqueue(
        self,
        kind: str,
        payload: dict[str, Any],
        run_at: datetime | None = None,
        key: str | None = None,
    ) -> str:
        if not re.fullmatch(r"[a-z_]{1,32}", kind):
            raise ValueError("Invalid job kind.")
        now = utcnow()
        if run_at is not None and run_at.tzinfo is None:
            raise ValueError("Job timestamps must include a timezone.")
        job_id = (
            hashlib.sha256(f"{kind}:{key}".encode()).hexdigest()[:32]
            if key
            else secrets.token_hex(16)
        )
        await self.db.jobs.update_one(
            {"_id": job_id},
            {
                "$setOnInsert": {
                    "kind": kind,
                    "payload": payload,
                    "status": "queued",
                    "run_at": run_at or now,
                    "lease_until": now,
                    "attempts": 0,
                    "created_at": now,
                    "updated_at": now,
                }
            },
            upsert=True,
        )
        return job_id

    async def claim_job(
        self, worker_id: str, kinds: tuple[str, ...] | None = None
    ) -> dict[str, Any] | None:
        now = utcnow()
        await self.db.jobs.update_many(
            {
                "status": "running",
                "lease_until": {"$lte": now},
                "attempts": {"$gte": MAX_ATTEMPTS},
            },
            {
                "$set": {
                    "status": "failed",
                    "error": "Worker stopped after the last retry; inspect outcome before retrying.",
                    "updated_at": now,
                }
            },
        )
        selector: dict[str, Any] = {
            "attempts": {"$lt": MAX_ATTEMPTS},
            "$or": [
                {"status": "queued", "run_at": {"$lte": now}},
                {"status": "running", "lease_until": {"$lte": now}},
            ],
        }
        if kinds is not None:
            selector["kind"] = {"$in": list(kinds)}
        return await self.db.jobs.find_one_and_update(
            selector,
            {
                "$set": {
                    "status": "running",
                    "worker_id": worker_id,
                    "lease_until": now + timedelta(seconds=LEASE_SECONDS),
                    "updated_at": now,
                },
                "$inc": {"attempts": 1},
            },
            sort=[("run_at", ASCENDING), ("created_at", ASCENDING)],
            return_document=ReturnDocument.AFTER,
        )

    @staticmethod
    def _lease(job_id: str, worker_id: str) -> dict[str, Any]:
        return {
            "_id": job_id,
            "status": "running",
            "worker_id": worker_id,
            "lease_until": {"$gt": utcnow()},
        }

    async def finish_job(self, job_id: str, worker_id: str) -> None:
        now = utcnow()
        await self.db.jobs.update_one(
            self._lease(job_id, worker_id),
            {
                "$set": {
                    "status": "done",
                    "finished_at": now,
                    "updated_at": now,
                    "expires_at": now + timedelta(days=30),
                },
                "$unset": {"worker_id": "", "error": ""},
            },
        )

    async def retry_job(
        self, job_id: str, worker_id: str, error: object, delay: float = 30
    ) -> None:
        lease = self._lease(job_id, worker_id)
        job = await self.db.jobs.find_one(lease, {"attempts": 1})
        if job is None:
            return
        now = utcnow()
        await self.db.jobs.update_one(
            lease,
            {
                "$set": {
                    "status": "failed" if job["attempts"] >= MAX_ATTEMPTS else "queued",
                    "error": clean_error(error),
                    "run_at": now + timedelta(seconds=max(1, min(delay, 86400))),
                    "updated_at": now,
                    "lease_until": now,
                },
                "$unset": {"worker_id": ""},
            },
        )

    async def heartbeat_job(self, job_id: str, worker_id: str) -> bool:
        result = await self.db.jobs.update_one(
            self._lease(job_id, worker_id),
            {
                "$set": {
                    "lease_until": utcnow() + timedelta(seconds=LEASE_SECONDS),
                    "updated_at": utcnow(),
                },
            },
        )
        return result.matched_count == 1

    async def setting(self, chat_id: int) -> dict[str, Any]:
        return await self.db.settings.find_one({"_id": int(chat_id)}) or {}

    async def set_setting(self, chat_id: int, key: str, value: Any) -> None:
        if not re.fullmatch(r"[a-z][a-z_]{0,39}", key):
            raise ValueError("Invalid setting name.")
        await self.db.settings.update_one(
            {"_id": int(chat_id)},
            {"$set": {key: value, "updated_at": utcnow()}},
            upsert=True,
        )

    async def touch_user(self, user: Any) -> None:
        if user is None or user.is_bot:
            return
        await self.db.users.update_one(
            {"_id": int(user.id)},
            {
                "$set": {
                    "first_name": str(user.first_name or "")[:128],
                    "username": user.username,
                    "language_code": user.language_code,
                    "last_seen": utcnow(),
                },
                "$setOnInsert": {"created_at": utcnow(), "banned": False},
            },
            upsert=True,
        )

    async def create_link(
        self,
        file_ids: list[str],
        owner_id: int | None = None,
        expires_at: datetime | None = None,
    ) -> str:
        if not file_ids or len(file_ids) > 100:
            raise ValueError("Links contain between one and 100 files.")
        unique = list(dict.fromkeys(file_ids))
        if any(not re.fullmatch(r"[0-9a-f]{32}", value) for value in unique):
            raise ValueError("Invalid file ID in link.")
        if expires_at is not None and expires_at.tzinfo is None:
            raise ValueError("Link expiry must include a timezone.")
        token = secrets.token_urlsafe(18)
        document: dict[str, Any] = {
            "_id": token,
            "file_ids": unique,
            "owner_id": owner_id,
            "created_at": utcnow(),
            "revoked": False,
        }
        if expires_at is not None:
            document["expires_at"] = expires_at
        await self.db.links.insert_one(document)
        return token

    async def get_link(self, token: str) -> dict[str, Any] | None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,40}", str(token)):
            return None
        return await self.db.links.find_one(
            {
                "_id": token,
                "revoked": {"$ne": True},
                "$or": [
                    {"expires_at": {"$exists": False}},
                    {"expires_at": {"$gt": utcnow()}},
                ],
            }
        )

    async def stats(self) -> dict[str, Any]:
        counts = await asyncio.gather(
            *(
                database.files.count_documents({"deleted_at": None}, maxTimeMS=10000)
                for database in self.catalogs
            )
        )
        deleted = await asyncio.gather(
            *(
                database.files.count_documents(
                    {"deleted_at": {"$ne": None}}, maxTimeMS=10000
                )
                for database in self.catalogs
            )
        )
        return {
            "files": sum(counts),
            "deleted_files": sum(deleted),
            "catalogs": len(self.catalogs),
            "users": await self.db.users.estimated_document_count(),
            "sources": await self.db.sources.count_documents({"active": True}),
            "queued_jobs": await self.db.jobs.count_documents({"status": "queued"}),
            "running_jobs": await self.db.jobs.count_documents({"status": "running"}),
            "failed_jobs": await self.db.jobs.count_documents({"status": "failed"}),
            "schema_version": SCHEMA_VERSION,
        }
