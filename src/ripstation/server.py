"""FastAPI dashboard: state snapshot, SSE live updates, review actions."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from . import drives as drv
from . import version
from .diagnostics import collect
from .finisher import nas_available
from .station import Station

STATIC = Path(__file__).parent / "static"


class EpisodeAssign(BaseModel):
    title_id: int
    season: int | None = None
    episode: int | None = None
    skip: bool = False


class NameBody(BaseModel):
    kind: Literal["movie", "tv"]
    tmdb_id: int | None = None
    title: str | None = None  # manual entry when TMDB has nothing
    year: int | None = None
    season: int | None = None  # TV: consecutive numbering from (season, first_episode) ...
    first_episode: int | None = None
    episodes: list[EpisodeAssign] | None = None  # ... or an explicit episode per title


def create_app(station: Station) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await station.start()
        yield

    app = FastAPI(lifespan=lifespan)

    def job_or_404(job_id: str):
        job = station.store.jobs.get(job_id)
        if not job:
            raise HTTPException(404, "no such job")
        return job

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/state")
    async def state():
        snap = station.store.snapshot()
        snap["version"] = version.label()
        snap["llm"] = bool(station.llm) and await station.llm.healthy()
        snap["tmdb"] = station.tmdb.configured
        snap["nas"] = nas_available(station.cfg.paths.nas_root, station.cfg.paths.nas_is_network_mount)
        return snap

    @app.get("/events")
    async def events():
        q = station.store.subscribe()

        async def stream():
            try:
                yield f"data: {json.dumps(station.store.snapshot())}\n\n"
                while True:
                    try:
                        snap = await asyncio.wait_for(q.get(), timeout=15)
                        yield f"data: {json.dumps(snap)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                station.store.unsubscribe(q)

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.get("/api/diagnostics")
    async def diagnostics():
        return await collect(station)

    @app.get("/api/search")
    async def search(kind: Literal["movie", "tv"], q: str, year: int | None = None):
        if not station.tmdb.configured:
            raise HTTPException(400, "TMDB key not configured")
        return [{**c.brief(), "poster": c.poster} for c in await station.tmdb.search(kind, q, year, limit=8)]

    @app.post("/api/jobs/{job_id}/name")
    async def name(job_id: str, body: NameBody):
        job = job_or_404(job_id)
        if job.status not in ("review", "failed"):
            raise HTTPException(409, f"job is {job.status}")
        if not body.tmdb_id and not body.title:
            raise HTTPException(400, "pick a TMDB result or type a title")
        if body.kind == "tv" and body.episodes is None and (body.season is None or body.first_episode is None):
            raise HTTPException(400, "TV needs an episode for each title (or season and first episode)")
        job.kind = body.kind
        try:
            await station.accept(job, body.model_dump(), source="user")
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"ok": True, "status": job.status, "error": job.error}

    @app.get("/api/tv/{tmdb_id}/episodes")
    async def episodes(tmdb_id: int):
        if not station.tmdb.configured:
            raise HTTPException(400, "TMDB key not configured")
        try:
            eps = await station.show_episodes(tmdb_id)
        except Exception as e:
            raise HTTPException(502, f"TMDB: {e}")
        return {"episodes": eps, "ripped": station.ripped_episodes(tmdb_id)}

    @app.post("/api/jobs/{job_id}/retry")
    async def retry(job_id: str):
        job = job_or_404(job_id)
        try:
            await station.retry(job)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"ok": True}

    @app.delete("/api/jobs/{job_id}")
    async def dismiss(job_id: str):
        try:
            station.dismiss(job_or_404(job_id))
        except ValueError as e:
            raise HTTPException(409, str(e))
        return {"ok": True}

    @app.post("/api/drives/eject")
    async def eject(key: str):
        w = station.workers.get(key)
        if not w:
            raise HTTPException(404, "no such drive")
        if w.busy:
            raise HTTPException(409, "drive is busy")
        await drv.eject(w.drive, await drv.media_status(w.drive))
        return {"ok": True}

    return app
