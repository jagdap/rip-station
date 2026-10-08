"""Minimal async TMDB client: the only source of truth for names."""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx

API = "https://api.themoviedb.org/3"
IMG = "https://image.tmdb.org/t/p/w185"


@dataclass
class Candidate:
    tmdb_id: int
    kind: str  # movie | tv
    title: str
    year: int | None
    overview: str = ""
    poster: str | None = None
    runtime_min: int | None = None  # movie runtime, or typical episode runtime for tv
    seasons: list[int] = field(default_factory=list)

    def brief(self) -> dict:
        return {
            "tmdb_id": self.tmdb_id,
            "kind": self.kind,
            "title": self.title,
            "year": self.year,
            "runtime_min": self.runtime_min,
            "seasons": self.seasons or None,
            "overview": self.overview[:240],
        }


@dataclass
class Episode:
    number: int
    name: str
    runtime_min: int | None
    season: int = 0


def _year(date: str | None) -> int | None:
    return int(date[:4]) if date and date[:4].isdigit() else None


class TMDB:
    def __init__(self, key: str, language: str = "en-US", client: httpx.AsyncClient | None = None):
        self.key = key
        self.language = language
        headers, self.params = {}, {"language": language}
        if len(key) > 40:  # v4 read access token
            headers["Authorization"] = f"Bearer {key}"
        else:
            self.params["api_key"] = key
        self.client = client or httpx.AsyncClient(base_url=API, headers=headers, timeout=15)

    @property
    def configured(self) -> bool:
        return bool(self.key)

    async def _get(self, path: str, **params) -> dict:
        r = await self.client.get(path, params={**self.params, **params})
        r.raise_for_status()
        return r.json()

    async def search(self, kind: str, query: str, year: int | None = None, limit: int = 5) -> list[Candidate]:
        params = {"query": query}
        if year:
            params["year" if kind == "movie" else "first_air_date_year"] = year
        data = await self._get(f"/search/{kind}", **params)
        out = []
        for r in data.get("results", [])[:limit]:
            poster = f"{IMG}{r['poster_path']}" if r.get("poster_path") else None
            if kind == "movie":
                out.append(Candidate(r["id"], "movie", r.get("title", ""), _year(r.get("release_date")),
                                     r.get("overview", ""), poster))
            else:
                out.append(Candidate(r["id"], "tv", r.get("name", ""), _year(r.get("first_air_date")),
                                     r.get("overview", ""), poster))
        return out

    async def enrich(self, c: Candidate) -> Candidate:
        """Fill runtime (and seasons for tv), which search results don't include."""
        if c.kind == "movie":
            d = await self._get(f"/movie/{c.tmdb_id}")
            c.runtime_min = d.get("runtime") or None
        else:
            d = await self._get(f"/tv/{c.tmdb_id}")
            rts = d.get("episode_run_time") or []
            c.runtime_min = rts[0] if rts else None
            c.seasons = [s["season_number"] for s in d.get("seasons", []) if s.get("season_number", 0) > 0]
        return c

    async def details(self, kind: str, tmdb_id: int) -> Candidate:
        d = await self._get(f"/{kind}/{tmdb_id}")
        poster = f"{IMG}{d['poster_path']}" if d.get("poster_path") else None
        if kind == "movie":
            c = Candidate(tmdb_id, "movie", d.get("title", ""), _year(d.get("release_date")),
                          d.get("overview", ""), poster, d.get("runtime") or None)
        else:
            rts = d.get("episode_run_time") or []
            c = Candidate(tmdb_id, "tv", d.get("name", ""), _year(d.get("first_air_date")),
                          d.get("overview", ""), poster, rts[0] if rts else None,
                          [x["season_number"] for x in d.get("seasons", []) if x.get("season_number", 0) > 0])
        return c

    async def season(self, tv_id: int, season: int) -> list[Episode]:
        d = await self._get(f"/tv/{tv_id}/season/{season}")
        return [Episode(e["episode_number"], e.get("name", ""), e.get("runtime"), season)
                for e in d.get("episodes", [])]

    async def all_episodes(self, tv_id: int) -> list[Episode]:
        """Every regular episode of a show, in season/episode order (specials excluded)."""
        show = await self.details("tv", tv_id)
        out: list[Episode] = []
        for n in sorted(show.seasons):
            out += await self.season(tv_id, n)
        return out

    async def aclose(self) -> None:
        await self.client.aclose()
