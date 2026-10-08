"""Job store: in-memory state, JSON persistence, and change broadcast for the dashboard."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

# Job lifecycle: ripping -> naming -> (review) -> finishing -> done; any -> failed
ACTIVE = {"ripping", "naming", "finishing"}


@dataclass
class Job:
    id: str
    drive: str
    label: str
    kind: str
    titles: list[dict]  # [{id, duration_s, size_bytes, output_file}] for selected titles, disc order
    status: str = "ripping"
    created: float = field(default_factory=time.time)
    progress: float = 0.0
    current: str = ""
    pick_reasons: list[str] = field(default_factory=list)
    files: dict[str, str] = field(default_factory=dict)  # title id -> staged path
    proposal: dict | None = None
    final: dict | None = None
    outputs: list[str] = field(default_factory=list)
    error: str = ""
    failed_stage: str = ""
    warnings: list[str] = field(default_factory=list)  # e.g. "skipped unreadable spots near 1:02:13"
    disc_titles: list[dict] = field(default_factory=list)  # every title on the disc: id, minutes, GB, chapters

    @staticmethod
    def new(drive: str, label: str, kind: str, titles: list[dict], reasons: list[str]) -> "Job":
        return Job(id=uuid.uuid4().hex[:8], drive=drive, label=label, kind=kind,
                   titles=titles, pick_reasons=reasons)


@dataclass
class DriveState:
    key: str
    name: str
    state: str = "empty"  # empty | scanning | ripping | ejecting | error
    job_id: str | None = None
    message: str = ""


class Store:
    def __init__(self, data_dir: Path):
        self.dir = data_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "jobs.json"
        self.jobs: dict[str, Job] = {}
        self.drives: dict[str, DriveState] = {}
        self.memory: dict[str, int] = {}  # "tmdb_id:season" -> next episode number
        self.banner: str = ""  # global problem (e.g. MakeMKV key expired)
        self._subs: set[asyncio.Queue] = set()
        self._last_progress_emit: dict[str, float] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        raw = json.loads(self.path.read_text())
        self.memory = raw.get("memory", {})
        for j in raw.get("jobs", []):
            job = Job(**j)
            if job.status == "ripping":
                job.status, job.error, job.failed_stage = "failed", "interrupted by restart", "ripping"
            self.jobs[job.id] = job

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"memory": self.memory, "jobs": [asdict(j) for j in self.jobs.values()]},
                                  indent=1))
        tmp.replace(self.path)

    def log_decision(self, record: dict) -> None:
        with (self.dir / "decisions.jsonl").open("a") as f:
            f.write(json.dumps({"ts": time.time(), **record}) + "\n")

    # --- broadcast ---
    def snapshot(self) -> dict:
        return {
            "banner": self.banner,
            "drives": [asdict(d) for d in self.drives.values()],
            "jobs": sorted((asdict(j) for j in self.jobs.values()), key=lambda j: -j["created"]),
        }

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=50)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def changed(self, persist: bool = True) -> None:
        if persist:
            self.save()
        snap = self.snapshot()
        for q in list(self._subs):
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(snap)

    def progress(self, job: Job, fraction: float, current: str) -> None:
        job.progress, job.current = fraction, current
        now = time.monotonic()
        if now - self._last_progress_emit.get(job.id, 0) >= 0.5:
            self._last_progress_emit[job.id] = now
            self.changed(persist=False)
