import asyncio
import os
import stat
import sys
from pathlib import Path

import pytest

from ripstation import drives as drv
from ripstation import station as station_mod
from ripstation.config import Settings
from ripstation.station import Station

from .test_naming import MOVIES, fake_llm, fake_tmdb

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture
def fake_bin(tmp_path):
    p = tmp_path / "makemkvcon"
    p.write_text(f"#!/bin/sh\nexec {sys.executable} {Path(__file__).parent / 'fake_makemkvcon.py'} \"$@\"\n")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)


class FakeHardware:
    def __init__(self):
        self.drives = [drv.Drive(1, "ASUS", "SDRW-08U9M-U", "A112", "USB")]
        self.media = {1: drv.Media(True, "/dev/disk4", "DVD-ROM")}
        self.ejected = []

    async def list_drives(self):
        return list(self.drives)

    async def media_status(self, d):
        return self.media.get(d.index, drv.Media(False))

    async def eject(self, d, media=None):
        self.ejected.append(d.index)
        self.media.pop(d.index, None)


async def wait_for(cond, timeout=10):
    for _ in range(int(timeout / 0.05)):
        if cond():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("timed out")


@pytest.fixture
def station(tmp_path, fake_bin, monkeypatch):
    # tmp dirs are on the local disk; treat the fake NAS as a separate filesystem
    from ripstation import finisher
    monkeypatch.setattr(finisher, "nas_available", lambda root, net=True: root.is_dir())
    monkeypatch.setattr(station_mod, "nas_available", lambda root, net=True: root.is_dir())
    hw = FakeHardware()
    for name in ("list_drives", "media_status", "eject"):
        monkeypatch.setattr(drv, name, getattr(hw, name))
    monkeypatch.setattr(station_mod, "notify", lambda *a: None)
    nas = tmp_path / "nas"
    nas.mkdir()
    cfg = Settings.model_validate({
        "makemkv": {"binary": fake_bin},
        "rip": {"min_size_ratio": 0},  # the fake writes tiny files
        "paths": {"staging": str(tmp_path / "staging"), "data": str(tmp_path / "data"), "nas_root": str(nas)},
        "tmdb": {"key": "k"},
        "poll_media_s": 0.05, "poll_drives_s": 0.05, "rescan_wait_s": 0.01, "notify": "none",
    })
    st = Station(cfg)
    st.hw, st.nas = hw, nas
    return st


async def test_end_to_end_movie_auto(station, monkeypatch):
    monkeypatch.setenv("FAKE_FIXTURE", str(FIX / "synthetic_movie.txt"))
    station.tmdb = fake_tmdb(MOVIES)
    station.namer.tmdb = station.tmdb
    station.namer.llm = fake_llm([{"kind": "movie", "search_queries": ["The Matrix"]},
                                  {"tmdb_id": 603, "kind": "movie", "reason": "runtime"}])
    await station.start()
    await wait_for(lambda: any(j.status == "done" for j in station.store.jobs.values()))
    out = station.nas / "Movies/The Matrix (1999)/The Matrix (1999).mkv"
    assert out.exists()
    assert station.hw.ejected == [1]
    assert not any((station.cfg.paths.staging).iterdir())  # staging cleaned
    assert "decisions.jsonl" in os.listdir(station.cfg.paths.data)


async def test_review_then_user_confirms(station, monkeypatch):
    monkeypatch.setenv("FAKE_FIXTURE", str(FIX / "synthetic_generic.txt"))
    station.tmdb = fake_tmdb(MOVIES)
    station.namer.tmdb = station.tmdb
    station.namer.llm = fake_llm([{"kind": "movie", "search_queries": ["dvd"]}, {"tmdb_id": None, "kind": "movie"}])
    await station.start()
    await wait_for(lambda: any(j.status == "review" for j in station.store.jobs.values()))
    job = next(iter(station.store.jobs.values()))
    assert any("generic" in r for r in job.proposal["gate_reasons"])
    await station.accept(job, {"kind": "movie", "tmdb_id": 604}, source="user")
    assert job.status == "done"
    assert (station.nas / "Movies/The Matrix Reloaded (2003)/The Matrix Reloaded (2003).mkv").exists()


async def test_tv_disc_episodes_and_memory(station, monkeypatch):
    monkeypatch.setenv("FAKE_FIXTURE", str(FIX / "synthetic_tv.txt"))
    tv = {1668: {"name": "Friends", "date": "1994-09-22"}}
    seasons = {(1668, 2): [{"episode_number": n, "name": f"The One {n}", "runtime": 21 + n % 3} for n in range(1, 25)]}
    station.tmdb = fake_tmdb(tv=tv, seasons=seasons)
    station.namer.tmdb = station.tmdb
    station.namer.llm = fake_llm([{"kind": "tv", "search_queries": ["Friends"]},
                                  {"tmdb_id": 1668, "kind": "tv", "season": 2, "first_episode": 1}])
    await station.start()
    await wait_for(lambda: any(j.status == "done" for j in station.store.jobs.values()))
    season_dir = station.nas / "TV/Friends/Season 02"
    names = sorted(p.name for p in season_dir.iterdir())
    assert names == [f"Friends - S02E0{n} - The One {n}.mkv" for n in range(1, 5)]
    assert station.store.memory == {"1668:2": 5}


async def test_hot_plug_drives(station, monkeypatch):
    monkeypatch.setenv("FAKE_FIXTURE", str(FIX / "synthetic_movie.txt"))
    station.hw.media.clear()  # empty drive
    await station.start()
    await wait_for(lambda: len(station.workers) == 1)
    station.hw.drives.append(drv.Drive(2, "HL-DT-ST", "DVDRW GP65NB60", "PF00", "USB"))
    await wait_for(lambda: len(station.workers) == 2 and len(station.store.drives) == 2)
    station.hw.drives.pop(0)
    await wait_for(lambda: len(station.workers) == 1)
    assert list(station.store.drives) == ["2:HL-DT-ST DVDRW GP65NB60"]


async def test_nas_unmounted_fails_then_retry(station, monkeypatch):
    monkeypatch.setenv("FAKE_FIXTURE", str(FIX / "synthetic_movie.txt"))
    station.tmdb = fake_tmdb(MOVIES)
    station.namer.tmdb = station.tmdb
    station.namer.llm = fake_llm([{"kind": "movie", "search_queries": ["The Matrix"]},
                                  {"tmdb_id": 603, "kind": "movie"}])
    real_nas = station.cfg.paths.nas_root
    station.cfg.paths.nas_root = real_nas / "missing"
    await station.start()
    await wait_for(lambda: any(j.status == "failed" for j in station.store.jobs.values()))
    job = next(iter(station.store.jobs.values()))
    assert job.failed_stage == "finishing" and "not mounted" in job.error
    station.cfg.paths.nas_root = real_nas  # NAS comes back: picked up without clicking Retry
    await wait_for(lambda: job.status == "done")


async def test_diagnostics_masks_secrets_and_reports(station, monkeypatch):
    from ripstation.diagnostics import collect
    monkeypatch.setenv("FAKE_FIXTURE", str(FIX / "synthetic_movie.txt"))
    station.cfg.tmdb.key = "0123456789abcdef0123456789abcdef"
    station.cfg.plex.token = "supersecrettoken"
    station.tmdb = fake_tmdb(MOVIES)
    station.llm = None
    d = await collect(station)
    flat = str(d)
    assert "0123456789abcdef0123456789abcdef" not in flat and "supersecrettoken" not in flat
    assert d["checks"]["drives"]["ok"] and d["checks"]["drives"]["drives"][0]["disc_present"]
    assert d["checks"]["makemkv"]["ok"] and d["checks"]["makemkv"]["drives"][0]["disc"] == "THE_MATRIX_WS"
    assert d["checks"]["llm"] == {"ok": True, "enabled": False, "ms": d["checks"]["llm"]["ms"]}
    assert d["storage"]["nas_root"]["available"]


async def _run_scenario(station, monkeypatch, tmp_path, scenario, salvage=True):
    monkeypatch.setenv("FAKE_FIXTURE", str(FIX / "synthetic_movie.txt"))
    monkeypatch.setenv("FAKE_SCENARIO", scenario)
    monkeypatch.setenv("FAKE_LOG", str(tmp_path / "attempts.log"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))  # isolate from the real MakeMKV settings
    station.cfg.rip.salvage = salvage
    station.tmdb = fake_tmdb(MOVIES)
    station.namer.tmdb = station.tmdb
    station.namer.llm = fake_llm([{"kind": "movie", "search_queries": ["The Matrix"]},
                                  {"tmdb_id": 603, "kind": "movie"}])
    await station.start()
    await wait_for(lambda: any(j.status in ("done", "failed", "review") for j in station.store.jobs.values()))
    attempts = (tmp_path / "attempts.log").read_text().splitlines()
    return next(iter(station.store.jobs.values())), attempts


async def test_scratched_disc_recovers_with_more_retries(station, monkeypatch, tmp_path):
    job, attempts = await _run_scenario(station, monkeypatch, tmp_path, "scratched")
    assert attempts == ["|", "retries |"]
    assert job.status == "done" and not job.warnings  # re-reading recovered everything


async def test_damaged_disc_salvaged_goes_to_review_with_warning(station, monkeypatch, tmp_path):
    job, attempts = await _run_scenario(station, monkeypatch, tmp_path, "damaged")
    assert attempts == ["|", "retries |", "retries salvage|"]
    assert job.status == "review"  # never auto-accept a salvaged rip
    assert "skipped unreadable spots near 1:08" in job.warnings[0]  # ~50% of a 2:16 movie


async def test_dead_disc_fails_ejects_and_explains(station, monkeypatch, tmp_path):
    job, attempts = await _run_scenario(station, monkeypatch, tmp_path, "dead")
    assert len(attempts) == 3
    assert job.status == "failed" and "Clean it" in job.error and "read error" in job.error
    assert station.hw.ejected == [1]
    assert not any(station.cfg.paths.staging.rglob("*.mkv"))  # partial files removed


async def test_drive_error_does_not_escalate(station, monkeypatch, tmp_path):
    job, attempts = await _run_scenario(station, monkeypatch, tmp_path, "drive")
    assert attempts == ["|"]  # retrying the disc harder wouldn't help
    assert job.status == "failed" and "USB cable and power" in job.error


async def test_review_reasons_include_rip_warnings(station, monkeypatch, tmp_path):
    job, _ = await _run_scenario(station, monkeypatch, tmp_path, "damaged")
    assert job.proposal and job.warnings


async def test_unreadable_first_scan_is_retried(station, monkeypatch, tmp_path):
    """Real NAS case: right after a restart makemkvcon returned 0 titles and no label."""
    empty = tmp_path / "empty.txt"
    empty.write_text('MSG:5010,0,0,"Failed to open disc","Failed to open disc"\n')
    good = FIX / "synthetic_movie.txt"
    calls = {"n": 0}
    real_info = station.mkv.info

    async def flaky_info(source):
        calls["n"] += 1
        monkeypatch.setenv("FAKE_FIXTURE", str(empty if calls["n"] == 1 else good))
        return await real_info(source)

    monkeypatch.setenv("FAKE_FIXTURE", str(good))
    station.mkv.info = flaky_info
    station.tmdb = fake_tmdb(MOVIES)
    station.namer.tmdb = station.tmdb
    station.namer.llm = fake_llm([{"kind": "movie", "search_queries": ["The Matrix"]},
                                  {"tmdb_id": 603, "kind": "movie"}])
    await station.start()
    await wait_for(lambda: any(j.status == "done" for j in station.store.jobs.values()))
    assert calls["n"] == 2 and station.hw.ejected == [1]


FTT = {4603: {"name": "Faerie Tale Theatre", "date": "1982-09-11"}}
FTT_SEASONS = {
    (4603, 1): [{"episode_number": n, "name": name, "runtime": 51}
                for n, name in [(1, "The Tale of the Frog Prince"), (2, "Rumpelstiltskin")]],
    (4603, 2): [{"episode_number": n, "name": f"S2 tale {n}", "runtime": 51} for n in range(1, 7)],
}


async def _ftt_to_review(station, monkeypatch):
    monkeypatch.setenv("FAKE_FIXTURE", str(FIX / "synthetic_tv.txt"))  # 4 episode titles
    station.tmdb = fake_tmdb(tv=FTT, seasons=FTT_SEASONS)
    station.namer.tmdb = station.tmdb
    station.namer.llm = fake_llm([{"kind": "tv", "search_queries": ["Faerie Tale Theatre"]},
                                  {"tmdb_id": 4603, "kind": "tv", "season": 1, "first_episode": 1}])
    await station.start()
    await wait_for(lambda: any(j.status == "review" for j in station.store.jobs.values()))
    return next(iter(station.store.jobs.values()))


async def test_uniform_runtimes_force_review(station, monkeypatch):
    job = await _ftt_to_review(station, monkeypatch)
    assert any("same length" in r for r in job.proposal["gate_reasons"])


async def test_per_title_mapping_across_seasons_with_skip(station, monkeypatch):
    job = await _ftt_to_review(station, monkeypatch)
    await station.accept(job, {"kind": "tv", "tmdb_id": 4603, "episodes": [
        {"title_id": 0, "season": 1, "episode": 2},
        {"title_id": 1, "season": 2, "episode": 1},
        {"title_id": 2, "skip": True},              # e.g. a bonus feature
        {"title_id": 3, "season": 2, "episode": 2},
    ]}, source="user")
    assert job.status == "done", job.error
    tv = station.nas / "TV/Faerie Tale Theatre"
    assert sorted(str(p.relative_to(tv)) for p in tv.rglob("*.mkv")) == [
        "Season 01/Faerie Tale Theatre - S01E02 - Rumpelstiltskin.mkv",
        "Season 02/Faerie Tale Theatre - S02E01 - S2 tale 1.mkv",
        "Season 02/Faerie Tale Theatre - S02E02 - S2 tale 2.mkv",
    ]
    assert station.ripped_episodes(4603) == [(1, 2), (2, 1), (2, 2)]
    assert station.store.memory["4603:2"] == 3


async def test_consecutive_fill_continues_into_next_season(station, monkeypatch):
    job = await _ftt_to_review(station, monkeypatch)
    await station.accept(job, {"kind": "tv", "tmdb_id": 4603, "season": 1, "first_episode": 1}, source="user")
    assert job.status == "done", job.error
    assert [(e["season"], e["episode"]) for e in job.final["episodes"]] == [(1, 1), (1, 2), (2, 1), (2, 2)]


async def test_duplicate_episode_rejected(station, monkeypatch):
    job = await _ftt_to_review(station, monkeypatch)
    with pytest.raises(ValueError, match="same episode"):
        await station.accept(job, {"kind": "tv", "tmdb_id": 4603, "episodes": [
            {"title_id": 0, "season": 2, "episode": 1}, {"title_id": 1, "season": 2, "episode": 1}]},
            source="user")
    assert job.status == "review"
