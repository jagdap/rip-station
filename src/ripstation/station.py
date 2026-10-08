"""The engine: one worker per detected drive, plus naming and finishing pipelines.

Drives rip into staging and eject immediately; naming and copying happen in
background tasks so a drive never waits on TMDB, the LLM, the NAS, or you.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import subprocess
import sys
from pathlib import Path

import httpx

from . import drives as drv
from . import keys
from .config import Settings
from .finisher import FinishError, finish, nas_available, plex_refresh
from .llm import LLM
from .makemkv import MakeMKV, MakeMKVError, Message, Phase, Progress, classify, source_for
from .naming import Namer
from .picker import pick
from .state import DriveState, Job, Store
from .tmdb import TMDB

log = logging.getLogger("ripstation")


def notify(target: str, title: str, body: str) -> None:
    """target: 'auto' | 'none' | ntfy topic URL. Never raises."""
    try:
        if target.startswith("http"):
            httpx.post(target, content=body.encode(), headers={"Title": title}, timeout=5)
        elif target == "auto" and sys.platform == "darwin":
            script = f"display notification {json.dumps(body)} with title {json.dumps(title)}"
            subprocess.Popen(["osascript", "-e", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        log.warning("notification failed", exc_info=True)


def _clock(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


class RipReport:
    """What went wrong during one makemkvcon attempt."""

    def __init__(self):
        self.read_errors = 0
        self.drive_errors = 0
        self.failed = False
        self.positions: list[float] = []  # progress fraction when each read error appeared
        self.last = ""

    def add(self, kind: str | None, fraction: float, text: str) -> None:
        if kind == "read":
            self.read_errors += 1
            self.positions.append(fraction)
        elif kind == "drive":
            self.drive_errors += 1
        elif kind == "failed":
            self.failed = True
        if kind:
            self.last = text

    def where(self, duration_s: int) -> str:
        if not self.positions or not duration_s:
            return ""
        lo, hi = min(self.positions) * duration_s, max(self.positions) * duration_s
        return f"near {_clock(lo)}" if hi - lo < 60 else f"between {_clock(lo)} and {_clock(hi)}"

    def summary(self) -> str:
        return f"{self.read_errors} read error(s), {self.drive_errors} drive error(s); last: {self.last}"


class RipFailed(Exception):
    def __init__(self, report: RipReport, title: dict):
        super().__init__(report.summary())
        self.report, self.title = report, title


class DriveWorker:
    def __init__(self, station: "Station", drive: drv.Drive):
        self.station = station
        self.drive = drive
        self.state = DriveState(key=drive.key, name=drive.label)
        self.task: asyncio.Task | None = None
        self.handled_device: str | None = None  # disc we already ripped (if eject failed)
        self.busy = False
        self.consecutive_failures = 0

    def set(self, state: str, message: str = "", job_id: str | None = None) -> None:
        self.state.state, self.state.message, self.state.job_id = state, message, job_id
        self.station.store.changed(persist=False)

    async def run(self) -> None:
        cfg = self.station.cfg
        while True:
            try:
                media = await drv.media_status(self.drive)
                if not media.present:
                    self.handled_device = None
                    if self.state.state not in ("empty",):
                        self.set("empty")
                elif media.device != self.handled_device:
                    self.busy = True
                    try:
                        await self.process(media)
                    finally:
                        self.busy = False
            except asyncio.CancelledError:
                raise
            except Exception as e:  # keep the worker alive
                log.exception("drive %s", self.drive.key)
                self.set("error", str(e))
            await asyncio.sleep(cfg.poll_media_s)

    async def _source(self, media: drv.Media) -> str:
        cfg = self.station.cfg.makemkv
        if cfg.address == "dev" and media.device:
            return source_for("dev", media.device, None)
        # disc addressing: find makemkv's index for this device
        for slot in await self.station.mkv.scan_drives():
            if media.device and slot.device.replace("/dev/r", "/dev/") == media.device:
                return source_for("disc", None, slot.index)
        raise MakeMKVError(f"makemkv doesn't list a drive for {media.device}")

    async def process(self, media: drv.Media) -> None:
        st, cfg = self.station, self.station.cfg
        self.set("scanning", "reading disc")
        source = await self._source(media)
        info, raw = await st.mkv.info(source)
        unit = self.station.cfg.rescan_wait_s
        for wait in (unit, 3 * unit):
            if info.titles or info.label:
                break
            # No titles *and* no label means the drive couldn't read the disc yet (still
            # spinning up, or recovering from an interrupted read), not an empty disc.
            log.warning("%s: disc not readable yet (%s), retrying in %ss", self.drive.label,
                        "; ".join(m.text for m in info.messages[-2:]) or "no output", wait)
            self.set("scanning", "disc not readable yet, retrying…")
            await asyncio.sleep(wait)
            info, raw = await st.mkv.info(source)
        label = info.label or "UNKNOWN"
        if not info.titles:
            for m in info.messages[-8:]:
                log.warning("%s: makemkv: %s", self.drive.label, m.text)
        (cfg.paths.data / "scans").mkdir(parents=True, exist_ok=True)
        (cfg.paths.data / "scans" / f"{label}.txt").write_text("".join(raw))

        sel = pick(info.titles, cfg.picker)
        log.info("%s: disc %s, %d titles, picked %s %s%s", self.drive.label, label, len(info.titles),
                 sel.kind, sel.title_ids, f" (review: {'; '.join(sel.reasons)})" if sel.reasons else "")
        by_id = {t.id: t for t in info.titles}
        titles = [
            {"id": i, "duration_s": by_id[i].duration_s, "size_bytes": by_id[i].size_bytes,
             "output_file": by_id[i].output_file}
            for i in sel.title_ids
        ]
        job = Job.new(self.drive.label, label, sel.kind, titles, sel.reasons)
        job.disc_titles = [{"id": t.id, "min": round(t.duration_s / 60, 1), "gb": round(t.size_bytes / 1e9, 2),
                            "chapters": t.chapters} for t in info.titles]
        st.store.jobs[job.id] = job
        st.store.changed()
        if not titles:
            detail = "; ".join(m.text for m in info.messages[-3:])
            job.status, job.failed_stage = "failed", "picking"
            job.error = "; ".join(sel.reasons) + (
                f". MakeMKV said: {detail}. Clean the disc and re-insert it." if not info.label else "")
            await self._eject(media)
            st.store.changed()
            return

        out_dir = cfg.paths.staging / f"{job.id}_{drv_safe(label)}"
        self.set("ripping", label, job.id)
        try:
            for n, t in enumerate(titles):
                job.files[str(t["id"])] = str(await self._rip_title(source, job, n, t, out_dir))
        except RipFailed as e:
            await self._rip_failed(job, media, e)
            return
        except MakeMKVError as e:  # key expired, binary missing, etc.
            log.error("%s: rip of %s failed: %s", self.drive.label, label, e)
            job.status, job.error, job.failed_stage = "failed", str(e), "ripping"
            st.store.changed()
            if "too old" in str(e).lower() or "registration" in str(e).lower():
                st.store.banner = f"MakeMKV: {e}"
            self.set("error", str(e), job.id)
            self.handled_device = media.device
            return

        self.consecutive_failures = 0
        job.status, job.progress, job.current = "naming", 1.0, ""
        st.store.changed()
        log.info("%s: ripped %s (%d file(s)), ejecting", self.drive.label, label, len(job.files))
        await self._eject(media)
        st.spawn(st.name_job(job))
        extra = f" (warning: {job.warnings[0]})" if job.warnings else ""
        await asyncio.to_thread(notify, cfg.notify, "Rip Station",
                                f"{self.drive.label}: done with {label}{extra}, ready for next disc")

    async def _rip_title(self, source: str, job: Job, n: int, t: dict, out_dir: Path) -> Path:
        """Rip one title, escalating on read errors: normal -> more retries -> salvage."""
        st, rc = self.station, self.station.cfg.rip
        attempts: list[tuple[str, dict[str, str] | None]] = [("normal", None)]
        attempts.append(("more retries", {"io_ErrorRetryCount": str(rc.retry_count)}))
        if rc.salvage:
            attempts.append(("salvage", {"io_ErrorRetryCount": str(rc.retry_count), "io_IgnoreReadErrors": "true"}))
        total = len(job.titles)
        expected = out_dir / t["output_file"] if t["output_file"] else None
        report = RipReport()
        for i, (mode, settings) in enumerate(attempts):
            report = RipReport()
            before = set(out_dir.glob("*.mkv")) if out_dir.exists() else set()
            note = "" if i == 0 else f" (retry {i}: {mode})"
            fraction = 0.0
            try:
                async for ev in st.mkv.rip(source, t["id"], out_dir, settings):
                    if isinstance(ev, Phase) and not ev.top_level and not ev.name.lower().startswith("saving"):
                        st.store.progress(job, n / total, f"title {n + 1}/{total}: {ev.name}…{note}")
                    elif isinstance(ev, Progress):
                        fraction = ev.fraction
                        st.store.progress(job, (n + fraction) / total, f"title {n + 1}/{total}{note}")
                    elif isinstance(ev, Message):
                        report.add(classify(ev.text), fraction, ev.text)
            except MakeMKVError as e:
                if classify(str(e)) is None and "exited with" not in str(e):
                    raise  # registration/binary problems: not a disc issue
                report.add(classify(str(e)) or "failed", fraction, str(e))
            new = sorted(set(out_dir.glob("*.mkv")) - before)
            path = expected if expected and expected.exists() else (new[0] if new else None)
            complete = path is not None and (
                not t["size_bytes"] or path.stat().st_size >= rc.min_size_ratio * t["size_bytes"])
            if path is not None and not complete:
                report.add("failed", fraction, f"incomplete output ({path.stat().st_size / 1e9:.2f} of "
                                               f"{t['size_bytes'] / 1e9:.2f} GB)")
            if complete and not report.failed:
                if report.read_errors:
                    where = report.where(t["duration_s"])
                    if mode == "salvage":
                        job.warnings.append(f"title {t['id']}: skipped unreadable spots {where} — check playback there")
                    log.warning("%s: %s title %s: %d read error(s) %s, %s", self.drive.label, job.label,
                                t["id"], report.read_errors, where,
                                "salvaged" if mode == "salvage" else "recovered by re-reading")
                return path
            for f in new:  # drop the partial file before retrying
                f.unlink(missing_ok=True)
            log.warning("%s: %s title %s attempt '%s' failed: %s", self.drive.label, job.label, t["id"],
                        mode, report.summary())
            if report.drive_errors and not report.read_errors:
                break  # the drive/connection, not the disc: retrying differently won't help
            if not report.read_errors:
                break  # failed without read errors: nothing more retries can fix
        raise RipFailed(report, t)

    async def _rip_failed(self, job: Job, media: drv.Media, e: "RipFailed") -> None:
        st, cfg = self.station, self.station.cfg
        self.consecutive_failures += 1
        r = e.report
        if r.read_errors:
            job.error = (f"Couldn't read the disc ({r.read_errors} read error(s) {r.where(e.title['duration_s'])}). "
                         "Clean it (soft cloth, center outward) and re-insert, or try another drive.")
        elif r.drive_errors:
            job.error = ("The drive reported a hardware/connection problem. Check its USB cable and power "
                         f"(plug it straight into the NAS or a powered hub). Detail: {r.last}")
        else:
            job.error = f"Rip failed: {r.last or 'no output file'}"
        job.status, job.failed_stage = "failed", "ripping"
        st.store.changed()
        log.error("%s: %s: %s", self.drive.label, job.label, job.error)
        await self._eject(media)  # hand the disc back so it can be cleaned
        if self.consecutive_failures >= cfg.rip.drive_failures_warn:
            msg = (f"Failed the last {self.consecutive_failures} discs — if they play elsewhere, "
                   "check this drive's cable/power or try another drive")
        else:
            msg = f"Last disc ({job.label}) couldn't be ripped — see Jobs"
        self.set("empty", msg)
        await asyncio.to_thread(notify, cfg.notify, "Rip Station", f"{self.drive.label}: {job.label} — {job.error}")

    async def _eject(self, media: drv.Media) -> None:
        self.set("ejecting")
        self.handled_device = media.device
        await drv.eject(self.drive, media)
        self.set("empty")


def drv_safe(s: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in s)[:40]


class Station:
    def __init__(self, cfg: Settings):
        self.cfg = cfg
        self.store = Store(cfg.paths.data)
        self.mkv = MakeMKV(cfg.makemkv.binary, cfg.makemkv.minlength)
        self.tmdb = TMDB(cfg.tmdb.key, cfg.tmdb.language)
        self.llm = LLM(cfg.llm.base_url, cfg.llm.model, cfg.llm.timeout_s) if cfg.llm.enabled else None
        self.namer = Namer(self.tmdb, self.llm, cfg.picker.runtime_tolerance_min)
        self.workers: dict[str, DriveWorker] = {}
        self._tasks: set[asyncio.Task] = set()
        self._episode_cache: dict[int, list[dict]] = {}

    def spawn(self, coro) -> asyncio.Task:
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)
        return t

    async def start(self) -> None:
        self.cfg.paths.staging.mkdir(parents=True, exist_ok=True)
        # resume jobs interrupted mid-pipeline
        for job in list(self.store.jobs.values()):
            if job.status == "naming":
                self.spawn(self.name_job(job))
            elif job.status == "finishing":
                self.spawn(self.finish_job(job))
        self.spawn(self.watch_drives())
        self.spawn(self.watch_makemkv())

    async def check_makemkv(self) -> bool:
        """Verify makemkvcon runs; with MAKEMKV_KEY=auto, refresh an expired beta key."""
        for attempt in range(2):
            try:
                await self.mkv.scan_drives()
                if self.store.banner:
                    self.store.banner = ""
                    self.store.changed(persist=False)
                return True
            except MakeMKVError as e:
                if attempt == 0 and await asyncio.to_thread(keys.ensure_key, True):
                    continue
                self.store.banner = f"MakeMKV: {e}"
            except OSError as e:
                self.store.banner = f"MakeMKV not found at {self.cfg.makemkv.binary}: {e}"
            self.store.changed(persist=False)
            return False
        return False

    async def watch_makemkv(self) -> None:
        await asyncio.to_thread(keys.ensure_key)
        while True:
            # cheap, and catches the beta key expiring mid-month; skip while drives are busy
            if not any(w.busy for w in self.workers.values()):
                await self.check_makemkv()
            await asyncio.sleep(6 * 3600 if not self.store.banner else 600)

    async def watch_drives(self) -> None:
        while True:
            try:
                await self.sync_drives(await drv.list_drives())
                await self.retry_waiting_for_nas()
            except Exception:
                log.exception("drive scan")
            await asyncio.sleep(self.cfg.poll_drives_s)

    async def retry_waiting_for_nas(self) -> None:
        """Copy jobs that failed because the NAS was away, once it's back."""
        waiting = [j for j in self.store.jobs.values()
                   if j.status == "failed" and j.failed_stage == "finishing" and j.final
                   and "not mounted" in j.error]
        if waiting and nas_available(self.cfg.paths.nas_root, self.cfg.paths.nas_is_network_mount):
            for job in waiting:
                log.info("NAS is back, retrying %s", job.label)
                await self.finish_job(job)

    async def sync_drives(self, found: list[drv.Drive]) -> None:
        keys = {d.key for d in found}
        for key in list(self.workers):
            if key not in keys:
                w = self.workers.pop(key)
                if w.task:
                    w.task.cancel()
                if w.state.job_id and (job := self.store.jobs.get(w.state.job_id)) and job.status == "ripping":
                    job.status, job.error, job.failed_stage = "failed", "drive disconnected", "ripping"
                self.store.drives.pop(key, None)
                log.info("drive removed: %s", key)
        for d in found:
            if d.key not in self.workers:
                w = DriveWorker(self, d)
                self.workers[d.key] = w
                self.store.drives[d.key] = w.state
                w.task = self.spawn(w.run())
                log.info("drive added: %s", d.key)
        self.store.changed(persist=False)

    # --- naming ---
    def _durations(self, job: Job) -> list[int]:
        if job.kind == "movie":
            return [max(t["duration_s"] for t in job.titles)]
        return [t["duration_s"] for t in job.titles]

    async def name_job(self, job: Job) -> None:
        try:
            prop = await self.namer.identify(job.label, job.kind, self._durations(job), self.store.memory)
        except Exception as e:
            log.exception("naming %s", job.label)
            job.proposal = {"kind": job.kind, "candidates": [], "choice": None, "auto_accept": False,
                            "gate_reasons": [f"naming error: {e}"], "episodes": []}
            job.status = "review"
            self.store.changed()
            return
        job.proposal = prop.__dict__
        reasons = prop.gate_reasons + job.pick_reasons + job.warnings
        auto = prop.auto_accept and not job.pick_reasons and not job.warnings  # salvaged rips: always review
        log.info("naming %s: choice=%s auto=%s reasons=%s", job.label,
                 (prop.choice or {}).get("tmdb_id"), auto, reasons)
        if auto:
            await self.accept(job, prop.choice, source="auto")
        else:
            job.status = "review"
            self.store.changed()
            self.store.log_decision({"job": job.id, "label": job.label, "event": "review",
                                     "choice": prop.choice, "reasons": reasons})
            await asyncio.to_thread(notify, self.cfg.notify, "Rip Station", f"{job.label} needs a name")

    async def accept(self, job: Job, choice: dict, source: str) -> None:
        """choice: {tmdb_id?, kind, title?, year?, season?, first_episode?}"""
        final = {"kind": choice["kind"], "tmdb_id": choice.get("tmdb_id")}
        cands = {c["tmdb_id"]: c for c in (job.proposal or {}).get("candidates", [])}
        cand = cands.get(choice.get("tmdb_id"))
        if choice.get("tmdb_id") and not cand and self.tmdb.configured:
            cand = (await self.tmdb.details(choice["kind"], choice["tmdb_id"])).brief()
        final["title"] = choice.get("title") or (cand or {}).get("title")
        final["year"] = choice.get("year") or (cand or {}).get("year")
        if not final["title"]:
            raise ValueError("no title")
        if final["kind"] == "tv":
            final["episodes"] = await self._episode_mapping(job, final["tmdb_id"], choice)
            first = next(e for e in final["episodes"] if not e.get("skip"))
            final["season"], final["first_episode"] = first["season"], first["episode"]
        job.final = final
        self.store.log_decision({"job": job.id, "label": job.label, "event": "accept", "source": source,
                                 "llm_choice": (job.proposal or {}).get("choice"), "final": final,
                                 "durations_min": [round(d / 60, 1) for d in self._durations(job)]})
        await self.finish_job(job)

    async def show_episodes(self, tmdb_id: int) -> list[dict]:
        """All episodes of a show in order, cached per run."""
        if tmdb_id not in self._episode_cache:
            eps = await self.tmdb.all_episodes(tmdb_id)
            self._episode_cache[tmdb_id] = [
                {"season": e.season, "episode": e.number, "name": e.name, "runtime_min": e.runtime_min}
                for e in eps]
        return self._episode_cache[tmdb_id]

    def ripped_episodes(self, tmdb_id: int) -> list[tuple[int, int]]:
        out = []
        for j in self.store.jobs.values():
            f = j.final or {}
            if j.status in ("done", "finishing") and f.get("kind") == "tv" and f.get("tmdb_id") == tmdb_id:
                if f.get("episodes"):
                    out += [(e["season"], e["episode"]) for e in f["episodes"] if not e.get("skip")]
                elif f.get("season") is not None:
                    out += [(f["season"], f["first_episode"] + i) for i in range(len(j.titles))]
        return sorted(set(out))

    async def _episode_mapping(self, job: Job, tmdb_id: int | None, choice: dict) -> list[dict]:
        """Per-title episode assignment: explicit from the review screen, or consecutive from
        (season, first_episode), continuing into the next season when one ends."""
        titles = [t["id"] for t in job.titles]
        show = []
        if tmdb_id and self.tmdb.configured:
            try:
                show = await self.show_episodes(tmdb_id)
            except Exception:
                log.warning("couldn't load episode list for %s", tmdb_id, exc_info=True)
        names = {(e["season"], e["episode"]): e["name"] for e in show}
        if choice.get("episodes") is not None:
            given = {a["title_id"]: a for a in choice["episodes"] if a}
            mapping = []
            for tid in titles:
                a = given.get(tid)
                if not a or a.get("skip") or a.get("season") is None or a.get("episode") is None:
                    mapping.append({"title_id": tid, "skip": True})
                else:
                    se = (int(a["season"]), int(a["episode"]))
                    mapping.append({"title_id": tid, "season": se[0], "episode": se[1], "name": names.get(se, "")})
        else:
            season, first = int(choice["season"]), int(choice["first_episode"])
            order = [(e["season"], e["episode"]) for e in show]
            if (season, first) in order:
                i = order.index((season, first))
                seq = order[i:i + len(titles)]
            else:
                seq = []
            seq += [(season, first + len(seq) + k) for k in range(len(titles) - len(seq))] if not seq else []
            if len(seq) < len(titles):  # ran off the end of the show
                raise ValueError(f"the show has only {len(seq)} episode(s) from S{season:02d}E{first:02d} on")
            mapping = [{"title_id": tid, "season": se[0], "episode": se[1], "name": names.get(se, "")}
                       for tid, se in zip(titles, seq)]
        kept = [(m["season"], m["episode"]) for m in mapping if not m.get("skip")]
        if not kept:
            raise ValueError("assign at least one title to an episode")
        if len(kept) != len(set(kept)):
            raise ValueError("two titles are assigned to the same episode")
        return mapping

    async def finish_job(self, job: Job) -> None:
        job.status, job.error = "finishing", ""
        self.store.changed()
        try:
            job.outputs = await finish(job, self.cfg)
        except (FinishError, OSError) as e:
            err = str(e)
            if isinstance(e, OSError) and not nas_available(self.cfg.paths.nas_root, self.cfg.paths.nas_is_network_mount):
                err = f"NAS share not mounted (disconnected during copy: {e})"  # auto-retried later
            job.status, job.error, job.failed_stage = "failed", err, "finishing"
            log.error("finishing %s failed: %s", job.label, err)
            self.store.changed()
            return
        f = job.final or {}
        if f.get("kind") == "tv" and f.get("tmdb_id") and f.get("episodes"):
            last = max((e for e in f["episodes"] if not e.get("skip")), key=lambda e: (e["season"], e["episode"]))
            self.store.memory[f"{f['tmdb_id']}:{last['season']}"] = last["episode"] + 1
        job.status = "done"
        self.store.changed()
        log.info("done %s -> %s", job.label, ", ".join(job.outputs))
        try:
            await plex_refresh(self.cfg)
        except Exception:
            log.warning("plex refresh failed", exc_info=True)

    async def retry(self, job: Job) -> None:
        if job.failed_stage == "finishing" and job.final:
            await self.finish_job(job)
        elif job.failed_stage in ("naming", "") and job.files:
            job.status = "naming"
            self.store.changed()
            await self.name_job(job)
        else:
            raise ValueError("re-insert the disc to rip it again")

    def dismiss(self, job: Job) -> None:
        if job.status in ("ripping", "finishing"):
            raise ValueError("job is busy")
        self.store.jobs.pop(job.id, None)
        for path in {str(Path(p).parent) for p in job.files.values()}:
            shutil.rmtree(path, ignore_errors=True)
        self.store.changed()
