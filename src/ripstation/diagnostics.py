"""Read-only health report for remote debugging: GET /api/diagnostics.

Never includes secrets (TMDB key, Plex token, MakeMKV key are masked). Never runs
makemkvcon while a drive is busy, so it can't disturb a rip.
"""

from __future__ import annotations

import asyncio
import collections
import logging
import os
import platform
import re
import shutil
import sys
import time
from pathlib import Path

import httpx

from . import drives as drv
from . import keys
from . import version
from . import finisher
from .makemkv import MakeMKVError


# Secrets that can appear inside log lines (e.g. request URLs).
_SECRETS = re.compile(
    r"((?:api_key|X-Plex-Token|token|key)=)[^&\s\"']+"
    r"|(Bearer\s+)[A-Za-z0-9._\-]+"
    r"|\b(T-)[A-Za-z0-9@_]{20,}",
    re.I,
)


def redact(text: str) -> str:
    return _SECRETS.sub(lambda m: (m.group(1) or m.group(2) or m.group(3)) + "[redacted]", text)


class RingBuffer(logging.Handler):
    """Keeps the last N formatted log lines in memory, with secrets redacted."""

    def __init__(self, size: int = 200):
        super().__init__()
        self.lines: collections.deque[str] = collections.deque(maxlen=size)
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.lines.append(redact(self.format(record)))
        except Exception:
            pass


LOG_BUFFER = RingBuffer()


def install_log_buffer() -> None:
    root = logging.getLogger()
    if LOG_BUFFER not in root.handlers:
        root.addHandler(LOG_BUFFER)
    # httpx logs every request URL at INFO (TMDB's key is a URL parameter) and the
    # dashboard polls the LLM every 30s; only keep its warnings.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def mask(value: str) -> str:
    if not value:
        return ""
    return value[:4] + "…" + f"({len(value)} chars)"


def _devices() -> list[dict]:
    """Linux only: the optical (/dev/srN) and SCSI-generic (/dev/sgN, used by MakeMKV) nodes."""
    if not drv.IS_LINUX:
        return []
    return [{"path": str(p), "readable": os.access(p, os.R_OK), "writable": os.access(p, os.W_OK)}
            for pat in ("sr*", "sg*") for p in sorted(drv.DEV_DIR.glob(pat))]


def _disk(path: Path) -> dict:
    try:
        u = shutil.disk_usage(path)
        return {"path": str(path), "exists": True, "writable": os.access(path, os.W_OK),
                "free_gb": round(u.free / 1e9, 1), "total_gb": round(u.total / 1e9, 1)}
    except OSError as e:
        return {"path": str(path), "exists": path.exists(), "error": str(e)}


def _config(cfg) -> dict:
    d = cfg.model_dump(mode="json")
    d["tmdb"]["key"] = mask(d["tmdb"]["key"])
    d["plex"]["token"] = mask(d["plex"]["token"])
    return d


def _makemkv_key() -> dict:
    p = keys.settings_path()
    text = p.read_text() if p.exists() else ""
    m = keys._SETTING.search(text)
    key = m.group(0).split("=", 1)[1].strip().strip('"') if m else ""
    return {"settings_file": str(p), "key": mask(key), "MAKEMKV_KEY_env": "auto" if
            os.environ.get("MAKEMKV_KEY", "").lower() == "auto" else ("set" if os.environ.get("MAKEMKV_KEY") else "unset")}


async def _check(name: str, coro, timeout: float = 10) -> dict:
    t0 = time.monotonic()
    try:
        res = await asyncio.wait_for(coro, timeout)
        return {"ok": True, **(res or {}), "ms": int((time.monotonic() - t0) * 1000)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "ms": int((time.monotonic() - t0) * 1000)}


async def collect(station) -> dict:
    cfg = station.cfg

    async def drives_report():
        found = await drv.list_drives()
        out = []
        for d in found:
            m = await drv.media_status(d)
            w = station.workers.get(d.key)
            out.append({"key": d.key, "model": d.label, "rev": d.rev, "bus": d.bus, "dev": d.dev,
                        "disc_present": m.present, "worker_state": w.state.state if w else "no worker",
                        "worker_message": w.state.message if w else ""})
        return {"drives": out}

    async def makemkv_report():
        if any(w.busy for w in station.workers.values()):
            return {"skipped": "a drive is busy; not running makemkvcon"}
        slots = await station.mkv.scan_drives()
        return {"binary": cfg.makemkv.binary, "drives": [
            {"index": s.index, "name": s.drive_name, "disc": s.disc_name, "device": s.device, "visible": s.visible}
            for s in slots]}

    async def llm_report():
        if not station.llm:
            return {"enabled": False}
        r = await station.llm.client.get("/models", timeout=5)
        r.raise_for_status()
        return {"base_url": cfg.llm.base_url, "models": [m.get("id") for m in r.json().get("data", [])]}

    async def tmdb_report():
        if not station.tmdb.configured:
            raise RuntimeError("no TMDB key in config.toml")
        await station.tmdb._get("/configuration")
        return {}

    async def plex_report():
        if not cfg.plex.url:
            return {"configured": False}
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(f"{cfg.plex.url}/identity", headers={"Accept": "application/json"})
            r.raise_for_status()
            mc = r.json().get("MediaContainer", {})
            return {"version": mc.get("version"), "token_set": bool(cfg.plex.token)}

    checks = dict(zip(
        ["drives", "makemkv", "llm", "tmdb", "plex"],
        await asyncio.gather(
            _check("drives", drives_report()),
            _check("makemkv", makemkv_report(), timeout=60),
            _check("llm", llm_report()),
            _check("tmdb", tmdb_report()),
            _check("plex", plex_report()),
        ),
    ))
    jobs = list(station.store.jobs.values())
    return {
        "time": time.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "version": version.label(),
        "platform": {"python": sys.version.split()[0], "os": platform.platform(), "linux_backend": drv.IS_LINUX},
        "banner": station.store.banner,
        "devices": _devices(),
        "checks": checks,
        "makemkv_key": _makemkv_key(),
        "storage": {
            "nas_root": {**_disk(cfg.paths.nas_root),
                         "available": finisher.nas_available(cfg.paths.nas_root, cfg.paths.nas_is_network_mount)},
            "staging": _disk(cfg.paths.staging),
        },
        "jobs": {
            "counts": dict(collections.Counter(j.status for j in jobs)),
            "recent": [{"id": j.id, "label": j.label, "status": j.status, "kind": j.kind,
                        "progress": round(j.progress, 3), "current": j.current, "error": j.error,
                        "failed_stage": j.failed_stage, "picked": [t["id"] for t in j.titles],
                        "pick_reasons": j.pick_reasons, "warnings": j.warnings, "disc_titles": j.disc_titles,
                        "queries": (j.proposal or {}).get("queries")}
                       for j in sorted(jobs, key=lambda j: -j.created)[:10]],
        },
        "config": _config(cfg),
        "log": list(LOG_BUFFER.lines),
    }
