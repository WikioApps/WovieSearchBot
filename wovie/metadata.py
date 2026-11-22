"""TMDB discovery, with a bounded short-lived cache."""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict

import httpx


class Metadata:
    def __init__(self, settings):
        self.key = settings.tmdb_api_key
        bearer = bool(self.key and self.key.count(".") == 2)
        self.client = httpx.AsyncClient(
            base_url="https://api.themoviedb.org/3/",
            headers={"Authorization": f"Bearer {self.key}"} if bearer else {},
            params={} if bearer or not self.key else {"api_key": self.key},
            timeout=httpx.Timeout(8, connect=3),
            limits=httpx.Limits(max_connections=3),
        )
        self.cache = OrderedDict()
        self.slots = asyncio.Semaphore(3)

    async def _fetch(self, path, **params):
        if not self.key:
            return {}
        key = (path, tuple(sorted(params.items())))
        cached = self.cache.get(key)
        if cached and cached[0] > time.monotonic():
            self.cache.move_to_end(key)
            return cached[1]
        async with self.slots:
            response = await self.client.get(path, params=params)
            if response.status_code == 404:
                return {}
            if response.is_error:
                raise RuntimeError(f"TMDB is unavailable (HTTP {response.status_code})")
            data = response.json()
        self.cache[key] = (time.monotonic() + 900, data)
        if len(self.cache) > 64:
            self.cache.popitem(last=False)
        return data

    @staticmethod
    def _card(row, kind):
        media = row.get("media_type", kind)
        return {
            "id": row["id"],
            "kind": media,
            "media_type": media,
            "title": row.get("title") or row.get("name") or "Untitled",
            "year": (row.get("release_date") or row.get("first_air_date") or "")[:4],
            "overview": row.get("overview") or "",
            "rating": round(row.get("vote_average") or 0, 1),
            "poster": "https://image.tmdb.org/t/p/w342" + row["poster_path"]
            if row.get("poster_path")
            else None,
            "url": f"https://www.themoviedb.org/{media}/{row['id']}",
        }

    async def search(self, query, kind="multi"):
        if kind not in {"multi", "movie", "tv"} or not query.strip():
            return []
        data = await self._fetch(
            f"search/{kind}", query=query.strip()[:160], include_adult="false"
        )
        return [
            self._card(x, kind)
            for x in data.get("results", [])
            if x.get("media_type", kind) in {"tv", "movie"}
        ][:10]

    async def detail(self, id: int, kind="movie"):
        if kind not in {"movie", "tv"}:
            return {}
        data = await self._fetch(
            f"{kind}/{int(id)}", append_to_response="credits,videos"
        )
        if not data.get("id"):
            return {}
        import re

        trailers = [
            v["key"]
            for v in data.get("videos", {}).get("results", [])
            if v.get("type") == "Trailer"
            and v.get("site") == "YouTube"
            and re.fullmatch(r"[A-Za-z0-9_-]{11}", v.get("key", ""))
        ]
        return dict(
            self._card(data, kind),
            genres=[g["name"] for g in data.get("genres", [])[:5]],
            cast=[p["name"] for p in data.get("credits", {}).get("cast", [])[:5]],
            runtime=data.get("runtime"),
            season_count=data.get("number_of_seasons"),
            trailer="https://www.youtube.com/watch?v=" + trailers[0]
            if trailers
            else None,
        )

    async def trending(self):
        data = await self._fetch("trending/all/week")
        return [
            self._card(x, x["media_type"])
            for x in data.get("results", [])
            if x.get("media_type") in {"tv", "movie"}
        ][:10]

    async def close(self):
        await self.client.aclose()
