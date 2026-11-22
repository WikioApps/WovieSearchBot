"""Mongo-backed file catalog, search and bot state."""

import asyncio
import hashlib
import re
import secrets
import unicodedata
from datetime import UTC, datetime, timedelta
from difflib import SequenceMatcher
from uuid import uuid4

from pymongo import AsyncMongoClient
from pymongo.write_concern import WriteConcern

from info import DEFAULTS, LANGUAGES, PERSONAL


def now():
    return datetime.now(UTC)


def words(text):
    # Keep combining vowel signs with their base letters in Indic release names.
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return "".join(
        c if c.isalnum() or unicodedata.category(c).startswith("M") else " "
        for c in normalized
    ).split()


def norm(text):
    return " ".join(words(text))


LANG_ALIASES = {
    "eng": "english",
    "hin": "hindi",
    "tam": "tamil",
    "tel": "telugu",
    "mal": "malayalam",
    "kan": "kannada",
    "ben": "bengali",
    "kor": "korean",
    "jpn": "japanese",
}


def tags(text):
    normalized = norm(text)
    lang = [
        LANG_ALIASES.get(w, w)
        for w in normalized.split()
        if w in LANGUAGES or w in LANG_ALIASES
    ]
    if "dual audio" in normalized or "multi audio" in normalized:
        lang.append("multi")
    q = re.search(r"\b(2160p|1080p|720p|480p|360p|4k)\b", normalized)
    years = re.findall(r"\b(19\d{2}|20\d{2})\b", normalized)
    season = re.search(r"\b(?:s|season\s*)0*(\d{1,2})(?:\b|e)", normalized)
    ep = re.search(r"(?:\be(?:p(?:isode)?)?\s*|\bs\d{1,2}e)0*(\d{1,3})\b", normalized)
    return {
        "language": list(dict.fromkeys(lang)),
        "quality": ("2160p" if q[1] == "4k" else q[1]) if q else "",
        "year": years[-1] if years else "",
        "season": str(int(season[1])) if season else "",
        "episode": str(int(ep[1])) if ep else "",
    }


def file_doc(m):
    if getattr(m, "has_protected_content", False) or getattr(m, "empty", False):
        return None
    media = next((v for v in (m.document, m.video, m.audio, m.animation) if v), None)
    if not media:
        return None
    caption = str(m.caption or "").splitlines()
    name = media.file_name or (caption[0][:180] if caption else None) or f"Media {m.id}"
    content = " ".join([name, *caption])[:2000]
    tokens = list(dict.fromkeys(words(content)))[:160]
    d = tags(content)
    d.update(
        {
            "_id": f"{abs(m.chat.id):x}_{m.id:x}",
            "source": m.chat.id,
            "mid": m.id,
            "name": name[:230],
            "bytes": media.file_size or 0,
            "file": media.file_id,
            "unique": media.file_unique_id,
            "time": m.date or now(),
            "tokens": tokens,
            "prefix": [t[:3] for t in tokens[:45]],
        }
    )
    return d


def parse_query(query):
    facets = {k: v for k, v in tags(query).items() if v}
    if "language" in facets:
        facets["language"] = facets["language"][0]
    remainder = re.sub(
        r"\b(?:s(?:eason\s*)?\d{1,2}(?:e\d{1,3})?|e(?:p(?:isode)?)?\s*\d{1,3}|2160p|1080p|720p|480p|360p|4k|19\d{2}|20\d{2})\b",
        " ",
        norm(query),
    )
    tokens = [
        w for w in words(remainder) if w not in LANGUAGES and w not in LANG_ALIASES
    ]
    first = next(iter(words(query)), "")
    if re.fullmatch(r"(19|20)\d{2}", first):
        tokens.insert(0, first)
        if facets.get("year") == first:
            facets.pop("year")
    return tokens[:14], facets


def preferred_filters(query, prefs):
    _, explicit = parse_query(query)
    return {
        k: ("2160p" if prefs[k] == "4k" else prefs[k])
        for k in ("language", "quality")
        if prefs[k] != "any" and k not in explicit
    }


class Store:
    def __init__(self, uris):
        self.clients = [
            AsyncMongoClient(
                u, maxPoolSize=8, serverSelectionTimeoutMS=8000, tz_aware=True
            )
            for u in uris
        ]
        self.dbs = [
            c.get_default_database(
                default="wovie_eva", write_concern=WriteConcern(w="majority")
            )
            for c in self.clients
        ]
        self.db = self.dbs[0]
        self.shards = {}
        self.state = self.db.eva_settings
        self.jobs = self.db.eva_jobs
        self.users = self.db.eva_users
        self.sessions = self.db.eva_search
        self.prompts = self.db.eva_prompts
        self.requests = self.db.eva_requests
        self.saved = self.db.eva_saved
        self.peers = self.db.eva_peers
        self.deletions = self.db.eva_deletions

    async def open(self):
        for db in self.dbs:
            ident = await db.eva_meta.find_one_and_update(
                {"_id": "catalog"},
                {"$setOnInsert": {"uuid": uuid4().hex}},
                upsert=True,
                return_document=True,
            )
            sid = ident["uuid"]
            if sid in self.shards:
                raise ValueError("DATABASE_URI contains the same database twice.")
            self.shards[sid] = db.eva_files
            await db.eva_files.create_index(
                [("source", 1), ("tokens", 1), ("time", -1)]
            )
            await db.eva_files.create_index([("source", 1), ("prefix", 1)])
        registered = await self.db.eva_meta.find_one({"_id": "shards"})
        if registered and not set(registered["ids"]).issubset(self.shards):
            raise ValueError(
                "A registered catalog database is missing. Restore the original DATABASE_URI list."
            )
        for db in self.dbs[1:]:
            if await db.eva_meta.find_one({"_id": "shards"}):
                raise ValueError(
                    "The first Mongo URI must remain the primary database."
                )
        primary_id = next(iter(self.shards))
        for db in self.dbs:
            role = await db.eva_meta.find_one({"_id": "primary"})
            if role and role["uuid"] != primary_id:
                raise ValueError(
                    "Catalog belongs to a different primary database. Restore the original first URI."
                )
        for db in self.dbs:
            await db.eva_meta.update_one(
                {"_id": "primary"}, {"$setOnInsert": {"uuid": primary_id}}, upsert=True
            )
        await self.db.eva_meta.update_one(
            {"_id": "shards"},
            {"$addToSet": {"ids": {"$each": list(self.shards)}}},
            upsert=True,
        )
        for coll in (
            self.sessions,
            self.prompts,
            self.db.eva_grants,
            self.db.eva_metadata,
            self.db.eva_trending,
        ):
            await coll.create_index("expires", expireAfterSeconds=0)
        await self.deletions.create_index("due")
        await self.jobs.create_index([("status", 1), ("created", 1)])
        await self.saved.create_index([("user", 1), ("time", -1)])
        await self.requests.create_index([("status", 1), ("time", -1)])

    async def close(self):
        await asyncio.gather(*(c.close() for c in self.clients))

    async def config(self, uid=None):
        d = await self.state.find_one({"_id": uid if uid is not None else "bot"}) or {}
        return (PERSONAL if uid is not None else DEFAULTS) | d.get("values", {})

    async def setting(self, key, value, uid=None, reset=False):
        await self.state.update_one(
            {"_id": uid if uid is not None else "bot"},
            {"$unset" if reset else "$set": {f"values.{key}": "" if reset else value}},
            upsert=True,
        )

    async def add_source(self, chat):
        # Atomically start with the editable defaults when no override exists.
        await self.state.update_one(
            {"_id": "bot"},
            [
                {
                    "$set": {
                        "values.sources": {
                            "$setUnion": [
                                {"$ifNull": ["$values.sources", DEFAULTS["sources"]]},
                                [chat],
                            ]
                        }
                    }
                }
            ],
            upsert=True,
        )

    async def collection(self, source):
        preferred = max(
            self.shards,
            key=lambda sid: hashlib.sha256(f"{source}:{sid}".encode()).digest(),
        )
        row = await self.db.eva_routes.find_one_and_update(
            {"_id": source},
            {"$setOnInsert": {"shard": preferred}},
            upsert=True,
            return_document=True,
        )
        return self.shards[row["shard"]]

    async def file(self, key):
        if not re.fullmatch(r"[0-9a-f]{1,15}_[0-9a-f]{1,8}", key):
            return None
        source = -int(key.split("_")[0], 16)
        route = await self.db.eva_routes.find_one({"_id": source})
        if route:
            return await self.shards[route["shard"]].find_one({"_id": key})

    async def search(
        self, query, filters, sources, sort="newest", suggest=False, limit=101
    ):
        tokens, facets = parse_query(query)
        facets.update(filters)
        criteria = {
            "source": {"$in": sources},
            **{k: v for k, v in facets.items() if v and v != "any"},
        }
        if not sources or not (tokens or facets):
            return [], []
        if tokens:
            criteria["tokens"] = {"$all": tokens}
        direction = 1 if sort == "oldest" else -1
        projection = {
            "name": 1,
            "time": 1,
            "language": 1,
            "quality": 1,
            "year": 1,
            "season": 1,
            "episode": 1,
            "bytes": 1,
            "source": 1,
        }

        async def find(c):
            return (
                await c.find(criteria, projection)
                .sort([("time", direction), ("_id", direction)])
                .limit(limit)
                .to_list(limit)
            )

        groups = await asyncio.gather(*(find(c) for c in self.shards.values()))
        hits = sorted(
            (r for group in groups for r in group),
            key=lambda r: (r["time"], r["_id"]),
            reverse=direction < 0,
        )[:limit]
        if hits or not suggest or not tokens:
            return hits, []
        prefix = [t[:3] for t in tokens if len(t) >= 3]
        if not prefix:
            return [], []
        candidates = set()
        for coll in self.shards.values():
            async for d in coll.find(
                {"source": {"$in": sources}, "prefix": {"$in": prefix}}, {"name": 1}
            ).limit(45):
                title = re.split(
                    r"\b(?:19\d\d|20\d\d|s\d+e?\d*|1080p|720p|2160p|480p)\b",
                    norm(d["name"]),
                )[0].strip()
                if title:
                    candidates.add(title[:72])
        ranked = sorted(
            (
                (SequenceMatcher(None, " ".join(tokens), t).ratio(), t)
                for t in candidates
            ),
            reverse=True,
        )
        return [], [t for score, t in ranked if score >= 0.42][:5]

    async def prompt(self, uid, kind, **fields):
        await self.prompts.replace_one(
            {"_id": uid},
            {
                "_id": uid,
                "kind": kind,
                "expires": now() + timedelta(minutes=10),
                **fields,
            },
            upsert=True,
        )

    async def user_prompt(self, uid):
        return await self.prompts.find_one({"_id": uid, "expires": {"$gt": now()}})

    async def session(self, uid, query, prefs, chat=None):
        key = secrets.token_hex(6)
        filters = preferred_filters(query, prefs)
        await self.sessions.insert_one(
            {
                "_id": key,
                "owner": uid,
                "chat": chat or uid,
                "query": query,
                "filters": filters,
                "expires": now() + timedelta(minutes=45),
            }
        )
        return key
