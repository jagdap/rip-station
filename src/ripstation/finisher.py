"""Move staged rips to the NAS under Plex naming, verify, and ask Plex to rescan."""

from __future__ import annotations

import asyncio
import errno
import os
import shutil
from pathlib import Path, PurePosixPath

import httpx

from .config import Settings
from .naming import episode_path, movie_path
from .state import Job


class FinishError(RuntimeError):
    pass


def plan_targets(job: Job, cfg: Settings) -> list[tuple[Path, PurePosixPath]]:
    """[(staged file, path relative to NAS root)] from job.final."""
    f = job.final or {}
    if f.get("kind") == "tv" and f.get("episodes"):
        out = []
        for e in f["episodes"]:
            if e.get("skip"):
                continue
            src = job.files.get(str(e["title_id"]))
            if not src:
                raise FinishError(f"no ripped file for title {e['title_id']}")
            out.append((Path(src), episode_path(cfg.paths.tv_dir, f["title"], e["season"], e["episode"],
                                                e.get("name", ""))))
        if not out:
            raise FinishError("every title was skipped")
        return out
    staged = [(t, job.files.get(str(t["id"]))) for t in job.titles]
    missing = [t["id"] for t, p in staged if not p]
    if missing:
        raise FinishError(f"no ripped file for title(s) {missing}")
    if f.get("kind") == "movie":
        main = max(staged, key=lambda tp: tp[0]["duration_s"])
        return [(Path(main[1]), movie_path(cfg.paths.movies_dir, f["title"], f.get("year")))]
    first, season = f["first_episode"], f["season"]
    names = {int(k): v for k, v in (f.get("episode_names") or {}).items()}
    return [
        (Path(p), episode_path(cfg.paths.tv_dir, f["title"], season, first + i, names.get(first + i, "")))
        for i, (_, p) in enumerate(staged)
    ]


def _copy(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        raise FinishError(f"already exists on NAS: {dst}")
    if src.stat().st_dev == dst.parent.stat().st_dev:
        try:
            os.rename(src, dst)  # same filesystem (running on the NAS): instant, atomic
            return
        except OSError as e:
            if e.errno != errno.EXDEV:  # different mounts of one disk: fall through to copy
                raise
    part = dst.with_name(dst.name + ".part")
    shutil.copyfile(src, part)
    if part.stat().st_size != src.stat().st_size:
        part.unlink(missing_ok=True)
        raise FinishError(f"size mismatch copying {src.name}")
    part.rename(dst)


def nas_available(root: Path, network_mount: bool = True) -> bool:
    """True if root exists and, for a network share, lives on a different filesystem
    than this machine's home.

    The second check guards against a stale /Volumes/<share> folder left behind after a
    disconnect, which would otherwise silently fill the Mac's disk."""
    try:
        if not root.is_dir():
            return False
        return not network_mount or root.stat().st_dev != Path.home().stat().st_dev
    except OSError:
        return False


async def finish(job: Job, cfg: Settings) -> list[str]:
    root = cfg.paths.nas_root
    if not nas_available(root, cfg.paths.nas_is_network_mount):
        raise FinishError(f"NAS share not mounted at {root}")
    targets = plan_targets(job, cfg)
    out = []
    for src, rel in targets:
        dst = root / rel
        await asyncio.to_thread(_copy, src, dst)
        out.append(str(dst))
    staging = Path(targets[0][0]).parent
    await asyncio.to_thread(shutil.rmtree, staging, True)
    return out


async def plex_refresh(cfg: Settings) -> None:
    if not (cfg.plex.url and cfg.plex.token):
        return
    params = {"X-Plex-Token": cfg.plex.token}
    async with httpx.AsyncClient(base_url=cfg.plex.url, timeout=10,
                                 headers={"Accept": "application/json"}) as c:
        r = await c.get("/library/sections", params=params)
        r.raise_for_status()
        for d in r.json().get("MediaContainer", {}).get("Directory", []):
            if d.get("type") in ("movie", "show"):
                await c.get(f"/library/sections/{d['key']}/refresh", params=params)
