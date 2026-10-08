"""Local LLM (mlx_lm.server or any OpenAI-compatible endpoint) for disc identification.

The model never names anything itself: step 1 proposes TMDB searches, step 2 picks
one of the TMDB candidates (or none). Output is validated with pydantic; anything
invalid is retried once and then treated as "no opinion" (job goes to review).
"""

from __future__ import annotations

import json
import re
from typing import Literal

import httpx
from pydantic import BaseModel, Field, ValidationError


class QueryPlan(BaseModel):
    kind: Literal["movie", "tv"]
    search_queries: list[str] = Field(min_length=1, max_length=3)
    year_hint: int | None = None
    season_hint: int | None = None
    disc_hint: int | None = None


class Choice(BaseModel):
    tmdb_id: int | None
    kind: Literal["movie", "tv"]
    season: int | None = None
    first_episode: int | None = None
    reason: str = ""


QUERY_SYSTEM = """You identify DVDs from their volume label and title layout.
Labels are often abbreviated, uppercase, with underscores, region/format suffixes
(WS, FS, NTSC, PAL, R1, SE, DISC1, D2) or studio codes. Expand them into likely
real titles. Search queries must be bare titles only (no year, no "season", no
"disc", no format words); put the year in year_hint. Reply with ONLY a JSON object:
{"kind": "movie"|"tv", "search_queries": [1-3 strings], "year_hint": int|null,
 "season_hint": int|null, "disc_hint": int|null}"""

CHOOSE_SYSTEM = """You match a DVD to exactly one TMDB entry from the candidates given,
or null if none fit. Use title similarity, year, and especially runtime: the disc's
main title duration should match a movie's runtime within a few minutes, and episode
durations should match a show's episode runtime. For TV, also give the season and
the episode number of the first episode on this disc (use the disc number hint and
remembered progress if provided). Never invent an id not in the candidates.
Reply with ONLY a JSON object:
{"tmdb_id": int|null, "kind": "movie"|"tv", "season": int|null,
 "first_episode": int|null, "reason": "short explanation"}"""


def extract_json(text: str) -> dict:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fence:
        return json.loads(fence.group(1))
    start = text.find("{")
    depth = 0
    for i in range(start, len(text)) if start >= 0 else ():
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start : i + 1])
    raise ValueError("no JSON object in model output")


class LLM:
    def __init__(self, base_url: str, model: str, timeout_s: float = 60.0,
                 client: httpx.AsyncClient | None = None):
        self.model = model
        self.client = client or httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout_s)

    async def _complete(self, system: str, user: dict) -> str:
        r = await self.client.post("/chat/completions", json={
            "model": self.model,
            "temperature": 0,
            "max_tokens": 400,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
            ],
        })
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]

    async def _structured[T: BaseModel](self, system: str, user: dict, model: type[T]) -> T | None:
        for _ in range(2):
            try:
                return model.model_validate(extract_json(await self._complete(system, user)))
            except (ValueError, ValidationError, KeyError):
                continue
            except httpx.HTTPError:
                return None  # server down or slow: no opinion, job goes to review
        return None

    async def plan_queries(self, disc: dict) -> QueryPlan | None:
        return await self._structured(QUERY_SYSTEM, disc, QueryPlan)

    async def choose(self, disc: dict, candidates: list[dict], memory: dict | None = None) -> Choice | None:
        payload = {"disc": disc, "candidates": candidates}
        if memory:
            payload["remembered_progress"] = memory
        choice = await self._structured(CHOOSE_SYSTEM, payload, Choice)
        if choice and choice.tmdb_id is not None and choice.tmdb_id not in {c["tmdb_id"] for c in candidates}:
            return None  # hallucinated id
        return choice

    async def healthy(self) -> bool:
        try:
            r = await self.client.get("/models", timeout=3)
            return r.status_code == 200
        except httpx.HTTPError:
            return False

    async def aclose(self) -> None:
        await self.client.aclose()
