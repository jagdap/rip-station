"""Decide which titles on a disc are worth ripping, and whether it's a movie or TV."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from statistics import median
from typing import Literal

from .config import Picker as PickerCfg
from .makemkv import Title

Kind = Literal["movie", "tv", "unknown"]


@dataclass
class Selection:
    kind: Kind
    title_ids: list[int]
    reasons: list[str] = field(default_factory=list)  # why a human should look

    @property
    def needs_review(self) -> bool:
        return bool(self.reasons) or not self.title_ids


def _dedupe(titles: list[Title]) -> list[Title]:
    """Drop titles that are the same content, keeping the copy with the most tracks.

    Same content = same length and size (and same segment map when known). Segment maps
    alone aren't enough: cell numbers restart in each title set, so different episodes
    in different title sets can all read "1-5" (seen on a real Faerie Tale Theatre disc)."""
    best: dict[tuple, Title] = {}
    for t in titles:
        key = (t.segments, t.duration_s, t.size_bytes)
        if key not in best or len(t.streams) > len(best[key].streams):
            best[key] = t
    keep = {id(t) for t in best.values()}
    return [t for t in titles if id(t) in keep]


def _cells(t: Title) -> list[str]:
    """Segment map as an ordered list of cells; "(2,4)" (an angle group) is one entry."""
    return re.findall(r"\([^)]*\)|[^,()]+", t.segments.replace(" ", ""))


def _shared(a: list[str], b: list[str]) -> float:
    return SequenceMatcher(None, a, b, autojunk=False).ratio() if a and b else 0.0


def _branch_variants(titles: list[Title]) -> list[Title]:
    """Collapse seamless-branching variants of one movie into a single title.

    Some discs carry the film twice with a few cells swapped (e.g. localized opening
    credits): same length to the second, size within 0.5%, nearly all cells shared.
    A real alternate cut differs by minutes, so it survives this."""
    kept: list[Title] = []
    for t in sorted(titles, key=lambda t: (-len(t.streams), t.id)):
        twin = next((k for k in kept
                     if abs(k.duration_s - t.duration_s) <= 2
                     and abs(k.size_bytes - t.size_bytes) <= 0.005 * max(k.size_bytes, 1)
                     and _shared(_cells(k), _cells(t)) >= 0.8), None)
        if twin is None:
            kept.append(t)
    keep = {id(t) for t in kept}
    return [t for t in titles if id(t) in keep]


def _is_play_all(t: Title, eps: list[Title]) -> bool:
    total = sum(e.duration_s for e in eps)
    return len(eps) >= 2 and abs(t.duration_s - total) <= 0.05 * total


def pick(titles: list[Title], cfg: PickerCfg) -> Selection:
    titles = _branch_variants(_dedupe(titles))
    if not titles:
        return Selection("unknown", [], ["no titles above the minimum length"])

    episodic = [t for t in titles if cfg.episode_min_s <= t.duration_s <= cfg.episode_max_s]
    long_titles = [t for t in titles if t.duration_s > cfg.episode_max_s]
    longest = max(titles, key=lambda t: t.duration_s)
    eps: list[Title] = []
    if episodic:
        mid = median(t.duration_s for t in episodic)
        eps = [t for t in episodic if abs(t.duration_s - mid) <= 0.25 * mid]
    # A long title that isn't a "play all" of the episodes means a movie with extras.
    feature = [t for t in long_titles if not _is_play_all(t, eps)]
    if not feature and (len(eps) >= 3 or (len(eps) >= 2 and longest.duration_s < cfg.movie_min_s)):
        # 3+ similar episodes, or 2 on a disc with nothing movie-length (old shows: 2 x 50 min)
        reasons = []
        if len(eps) != len(episodic):
            reasons.append(f"{len(episodic) - len(eps)} episode-length title(s) didn't fit the pattern")
        return Selection("tv", [t.id for t in sorted(eps, key=lambda t: t.id)], reasons)

    reasons = []
    picked = [longest]
    if longest.duration_s < cfg.movie_min_s:
        reasons.append(f"longest title is only {longest.duration_s // 60} min")
        # Unsure what this disc is: rip everything that could be content so nothing is lost;
        # the review step decides what to keep.
        picked += [t for t in titles if t is not longest and t.duration_s >= cfg.episode_min_s]
    rivals = [
        t for t in titles
        if t is not longest and t.duration_s >= longest.duration_s * (1 - cfg.alt_cut_ratio)
    ]
    if rivals:
        reasons.append(
            "possible alternate cut/angle: titles "
            + ", ".join(str(t.id) for t in [longest, *rivals])
            + " are within 5% of each other"
        )
        picked += [t for t in rivals if t not in picked]
    return Selection("movie", sorted(t.id for t in picked), reasons)
