"""Identify a disc (LLM + TMDB), decide whether to auto-accept, and build Plex paths.

The auto-accept gate is deterministic and does not trust the LLM's own confidence:
runtimes from TMDB must agree with the disc's actual title durations.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import PurePosixPath

from .llm import LLM, Choice
from .tmdb import TMDB, Candidate, Episode

_JUNK = re.compile(
    # edition/format tags DVD labels append: widescreen, full screen, pan & scan, letterbox, ...
    r"\b(WS|FS|PS|FF|LB|NTSC|PAL|R\d|SE|CE|UE|DVD|VIDEO|DISC\s*\d+|D\d+|SIDE\s*[AB]|WIDESCREEN|"
    r"FULLSCREEN|FULL\s*SCREEN|PAN\s*SCAN|LETTERBOX|SPECIAL|EDITION|COLLECTORS?|UNRATED|EXTENDED|"
    r"THEATRICAL|ANNIVERSARY|DELUXE|ULTIMATE|AC3|DTS)\b",
    re.I,
)
_GENERIC = {"", "DVD", "DVD_VIDEO", "DVDVIDEO", "VIDEO_TS", "DISC", "DISC1", "UNTITLED", "NO_NAME"}


def clean_label(label: str) -> str:
    s = re.sub(r"[_\.]+", " ", label)
    s = _JUNK.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s.title()


def strip_year(query: str) -> tuple[str, int | None]:
    """'beetlejuice 1988' -> ('beetlejuice', 1988); TMDB search doesn't match years in text."""
    m = re.search(r"\((19\d\d|20\d\d)\)|(?<=\S)\s+(19\d\d|20\d\d)\s*$", query)
    if not m:
        return query.strip(), None
    year = int(m.group(1) or m.group(2))
    return re.sub(r"\s+", " ", query[: m.start()] + query[m.end():]).strip(), year


def generic_label(label: str) -> bool:
    return label.strip().upper() in _GENERIC


def season_disc_hints(label: str) -> tuple[int | None, int | None]:
    up = label.upper()
    season = re.search(r"(?:(?<![A-Z])S|SEASON[ _]?)(\d{1,2})(?!\d)", up)
    disc = re.search(r"(?:(?<![A-Z])D|DISC[ _]?)(\d{1,2})(?!\d)", up)
    return (int(season.group(1)) if season else None, int(disc.group(1)) if disc else None)


@dataclass
class Proposal:
    kind: str  # movie | tv | unknown
    queries: list[str] = field(default_factory=list)  # what was searched (for diagnostics)
    candidates: list[dict] = field(default_factory=list)
    choice: dict | None = None  # {tmdb_id, kind, season, first_episode, reason}
    auto_accept: bool = False
    gate_reasons: list[str] = field(default_factory=list)
    episodes: list[dict] = field(default_factory=list)  # season episode list for tv choice


def _near(a_min: float, b_min: float | None, tol: float) -> bool:
    return b_min is not None and abs(a_min - b_min) <= tol


def _norm(title: str) -> str:
    return re.sub(r"[^a-z0-9]", "", title.lower().removeprefix("the "))


def movie_gate(candidates: list[Candidate], chosen_id: int, main_duration_s: int, tol_min: float) -> list[str]:
    reasons = []
    main_min = main_duration_s / 60
    chosen = next((c for c in candidates if c.tmdb_id == chosen_id), None)
    if chosen is None:
        return ["chosen id not in candidates"]
    if chosen.runtime_min is None:
        reasons.append("TMDB has no runtime for the chosen movie")
    elif not _near(main_min, chosen.runtime_min, tol_min):
        reasons.append(f"runtime mismatch: disc {main_min:.0f} min vs TMDB {chosen.runtime_min} min")
    # Same title + similar runtime (a remake, a re-release) is ambiguous; sequels aren't.
    twins = [c for c in candidates if c.kind == "movie" and c.tmdb_id != chosen_id
             and _norm(c.title) == _norm(chosen.title) and _near(main_min, c.runtime_min, tol_min)]
    if twins:
        reasons.append("same title and runtime as: " + ", ".join(f"{c.title} ({c.year})" for c in twins))
    return reasons


def tv_gate(episodes: list[Episode], choice: Choice, ep_durations_s: list[int],
            expected_next: int | None, tol_min: float) -> list[str]:
    reasons = []
    if choice.season is None or choice.first_episode is None:
        return ["model didn't give season and first episode"]
    if expected_next is not None and choice.first_episode != expected_next:
        reasons.append(f"expected episode {expected_next} next, model said {choice.first_episode}")
    if expected_next is None and choice.first_episode != 1:
        reasons.append("no remembered progress for this season and first episode isn't 1")
    by_num = {e.number: e for e in episodes}
    for i, dur in enumerate(ep_durations_s):
        ep = by_num.get(choice.first_episode + i)
        if ep is None:
            reasons.append(f"season {choice.season} has no episode {choice.first_episode + i}")
            break
        if not _near(dur / 60, ep.runtime_min, tol_min):
            reasons.append(f"episode {ep.number} runtime mismatch: disc {dur / 60:.0f} min vs TMDB {ep.runtime_min}")
            break
    return reasons


class Namer:
    def __init__(self, tmdb: TMDB, llm: LLM | None, tol_min: float = 8):
        self.tmdb = tmdb
        self.llm = llm
        self.tol = tol_min

    async def _candidates(self, kind: str, queries: list[str], year: int | None) -> list[Candidate]:
        seen: dict[int, Candidate] = {}
        for q in queries:
            for c in await self.tmdb.search(kind, q, year):
                seen.setdefault(c.tmdb_id, c)
        if year and not seen:  # a wrong year hint shouldn't hide the right answer
            for q in queries:
                for c in await self.tmdb.search(kind, q):
                    seen.setdefault(c.tmdb_id, c)
        cands = list(seen.values())[:8]
        for c in cands:
            await self.tmdb.enrich(c)
        return cands

    async def identify(self, label: str, kind: str, durations_s: list[int],
                       memory: dict[str, int] | None = None) -> Proposal:
        """durations_s: [main title] for movies, episode durations in order for tv."""
        season_hint, disc_hint = season_disc_hints(label)
        disc = {
            "volume_label": label,
            "cleaned_label": clean_label(label),
            "picker_guess": kind,
            "title_minutes": [round(d / 60, 1) for d in durations_s],
            "season_hint": season_hint,
            "disc_hint": disc_hint,
        }
        prop = Proposal(kind=kind)
        if not self.tmdb.configured:
            prop.gate_reasons.append("TMDB key not configured")
            return prop

        plan = await self.llm.plan_queries(disc) if self.llm else None
        if plan:
            kind = plan.kind if kind == "unknown" else kind
            queries, year = [], plan.year_hint
            for q in plan.search_queries:
                q, y = strip_year(q)
                year = year or y
                if q:
                    queries.append(q)
        else:
            queries, year = [clean_label(label)], None
            if self.llm:
                prop.gate_reasons.append("LLM gave no usable search plan")
        if generic_label(label):
            prop.gate_reasons.append(f"generic disc label '{label}'")
        elif clean_label(label) and clean_label(label).lower() not in {q.lower() for q in queries}:
            queries.append(clean_label(label))  # always try the literal label too
        search_kind = "tv" if kind == "tv" else "movie"
        prop.queries = list(queries)
        cands = await self._candidates(search_kind, queries, year)
        prop.kind = search_kind
        prop.candidates = [{**c.brief(), "poster": c.poster} for c in cands]
        if not cands:
            prop.gate_reasons.append("no TMDB results")
            return prop
        if not self.llm:
            prop.gate_reasons.append("LLM disabled")
            return prop

        mem = None
        if search_kind == "tv" and memory:
            mem = {k: v for k, v in memory.items()}
        choice = await self.llm.choose(disc, [c.brief() for c in cands], mem)
        if choice is None or choice.tmdb_id is None:
            prop.gate_reasons.append("LLM couldn't pick a candidate")
            return prop
        prop.choice = choice.model_dump()

        if search_kind == "movie":
            prop.gate_reasons += movie_gate(cands, choice.tmdb_id, durations_s[0], self.tol)
        else:
            season = choice.season or season_hint
            if season is None:
                prop.gate_reasons.append("unknown season")
            else:
                choice.season = season
                eps = await self.tmdb.season(choice.tmdb_id, season)
                prop.episodes = [asdict(e) for e in eps]
                expected = (memory or {}).get(f"{choice.tmdb_id}:{season}")
                prop.gate_reasons += tv_gate(eps, choice, durations_s, expected, self.tol)
                if len(eps) > 1 and len({e.runtime_min for e in eps}) <= 1:
                    prop.gate_reasons.append(
                        "TMDB lists every episode at the same length, so the episode order can't be "
                        "checked; match titles to episodes against the disc case")
                prop.choice = choice.model_dump()
        prop.auto_accept = not prop.gate_reasons
        return prop


_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe(name: str) -> str:
    return _BAD.sub("", name).strip().rstrip(".")


def movie_path(movies_dir: str, title: str, year: int | None) -> PurePosixPath:
    base = safe(f"{title} ({year})" if year else title)
    return PurePosixPath(movies_dir) / base / f"{base}.mkv"


def episode_path(tv_dir: str, show: str, season: int, episode: int, ep_title: str = "") -> PurePosixPath:
    show_s = safe(show)
    name = f"{show_s} - S{season:02d}E{episode:02d}"
    if ep_title:
        name += f" - {safe(ep_title)}"
    return PurePosixPath(tv_dir) / show_s / f"Season {season:02d}" / f"{name}.mkv"
