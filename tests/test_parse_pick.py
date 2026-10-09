from ripstation.config import Picker
from ripstation.drives import parse_drutil_list, parse_drutil_status
from ripstation.makemkv import parse_info, parse_line, source_for
from ripstation.picker import pick

CFG = Picker()


def test_parse_movie(fixture_info):
    info = fixture_info("synthetic_movie")
    assert info.label == "THE_MATRIX_WS"
    assert len(info.titles) == 3
    t = info.titles[0]
    assert t.duration_s == 8170 and t.chapters == 38 and t.output_file == "THE_MATRIX_WS_t00.mkv"
    kinds = [s.kind for s in t.streams]
    assert kinds == ["Video", "Audio", "Subtitles"]
    assert t.streams[1].lang == "eng" and t.streams[2].forced
    assert info.drives[0].has_disc and info.drives[0].device == "/dev/rdisk4"
    assert not info.drives[1].present


def test_quoted_commas_and_too_old():
    key, f = parse_line('MSG:5021,131332,1,"too old, please update","%1","http://x"')
    assert key == "MSG" and f[3] == "too old, please update"
    info = parse_info(['MSG:5021,131332,1,"This application version is too old.","x","y"'])
    from ripstation.makemkv import fatal_message
    assert fatal_message(info)
    assert parse_line("not a robot line") is None


def test_pick_movie(fixture_info):
    sel = pick(fixture_info("synthetic_movie").titles, CFG)
    assert sel.kind == "movie" and sel.title_ids == [0] and not sel.needs_review


def test_pick_tv_drops_play_all(fixture_info):
    sel = pick(fixture_info("synthetic_tv").titles, CFG)
    assert sel.kind == "tv" and sel.title_ids == [0, 1, 2, 3] and not sel.needs_review


def test_pick_alt_cut_flags_review(fixture_info):
    sel = pick(fixture_info("synthetic_alt_cut").titles, CFG)
    assert sel.kind == "movie" and sel.needs_review and "alternate" in sel.reasons[0]


def test_movie_with_episode_length_extras_is_movie(fixture_info):
    sel = pick(fixture_info("synthetic_movie_extras").titles, CFG)
    assert sel.kind == "movie" and sel.title_ids == [0]


def test_drutil_parsing():
    lst = """   Vendor   Product           Rev   Bus       SupportLevel
1  ASUS     SDRW-08U9M-U      A112  USB       Unsupported
2  HL-DT-ST DVDRW  GP65NB60   PF00  USB       Unsupported
"""
    drives = parse_drutil_list(lst)
    assert [d.index for d in drives] == [1, 2]
    assert drives[0].product == "SDRW-08U9M-U" and drives[1].product == "DVDRW  GP65NB60"
    status = """ Vendor   Product           Rev
 ASUS     SDRW-08U9M-U      A112

           Type: DVD-ROM              Name: /dev/disk4
       Sessions: 1                  Tracks: 1
"""
    m = parse_drutil_status(status)
    assert m.present and m.device == "/dev/disk4" and m.media_type == "DVD-ROM"
    assert not parse_drutil_status("           Type: No Media Inserted\n").present


def test_source_for():
    assert source_for("dev", "/dev/disk4", None) == "dev:/dev/rdisk4"
    assert source_for("disc", None, 2) == "disc:2"


def test_real_beetlejuice_disc(fixture_info):
    """Real capture (MakeMKV 2.0.0): feature exists twice with different track order."""
    info = fixture_info("beetlejuice")
    assert info.label == "BEETLEJUICE"  # no CINFO:32 on this disc; falls back to CINFO:2
    assert [t.duration_s for t in info.titles] == [5503, 735, 736, 734, 5503]
    sel = pick(info.titles, CFG)
    assert sel.kind == "movie" and sel.title_ids == [0] and not sel.needs_review


def test_real_baby_sitter_disc(fixture_info):
    """Real capture: the film twice, 1:42:00 each, differing only in a few branching cells."""
    info = fixture_info("baby_sitter")
    assert [t.duration_s for t in info.titles] == [6120, 6120]
    sel = pick(info.titles, CFG)
    assert sel.kind == "movie" and sel.title_ids == [0] and not sel.needs_review


def test_real_alternate_cut_still_ripped():
    from ripstation.makemkv import Title
    cuts = [Title(id=0, duration_s=6000, size_bytes=int(3.5e9), segments="1-20"),
            Title(id=1, duration_s=6240, size_bytes=int(3.6e9), segments="1-12,30,13-20")]
    sel = pick(cuts, CFG)
    assert sel.title_ids == [0, 1] and sel.needs_review


def test_partial_play_all_is_tv():
    """Real disc (AMERICAN_DREAMS): 4 episodes plus a play-all of only episodes 2-4."""
    from ripstation.makemkv import Title
    eps = [(47.5, 4), (44.1, 4), (42.6, 4), (41.6, 4), (128.4, 12)]
    titles = [Title(id=i, duration_s=round(m * 60), size_bytes=int(m * 4.5e7), chapters=c, segments=str(i))
              for i, (m, c) in enumerate(eps)]
    sel = pick(titles, CFG)
    assert sel.kind == "tv" and sel.title_ids == [0, 1, 2, 3]


def test_movie_with_episode_length_extras_stays_movie():
    from ripstation.makemkv import Title
    spec = [(128.0, 28), (44.0, 3), (42.5, 2), (41.5, 2)]   # extras add up, chapters don't
    titles = [Title(id=i, duration_s=round(m * 60), size_bytes=int(m * 4.5e7), chapters=c, segments=str(i))
              for i, (m, c) in enumerate(spec)]
    sel = pick(titles, CFG)
    assert sel.kind == "movie" and sel.title_ids == [0]


def test_nas_available_rejects_local_folder(tmp_path):
    from ripstation.finisher import nas_available
    assert not nas_available(tmp_path)          # local disk, e.g. a stale /Volumes/media
    assert not nas_available(tmp_path / "nope")
    assert nas_available(tmp_path, network_mount=False)  # on the NAS itself, local is fine


async def test_rip_progress_ignores_opening_phase(tmp_path):
    import sys
    from pathlib import Path as P
    import os
    from ripstation.makemkv import MakeMKV, Phase, Progress
    fake = tmp_path / "mk"
    fake.write_text(f"#!/bin/sh\nexec {sys.executable} {P(__file__).parent / 'fake_makemkvcon.py'} \"$@\"\n")
    fake.chmod(0o755)
    os.environ["FAKE_FIXTURE"] = str(P(__file__).parent / "fixtures" / "beetlejuice.txt")
    evs = [e async for e in MakeMKV(str(fake), 300).rip("dev:/dev/rdisk9", 0, tmp_path / "out")]
    fracs = [e.fraction for e in evs if isinstance(e, Progress)]
    assert fracs == sorted(fracs) and fracs[0] == 0.0 and fracs[-1] == 1.0  # never jumps to 90% then back
    assert any(isinstance(e, Phase) and e.name == "Decrypting data" for e in evs)


def test_linux_sysfs_drives(tmp_path):
    from ripstation.drives import linux_list_drives
    for n, (vendor, model) in {0: ("HL-DT-ST", "DVDRAM AP70NS50"), 2: ("ASUS", "SDRW-08U9M-U")}.items():
        d = tmp_path / "sys" / f"sr{n}" / "device"
        d.mkdir(parents=True)
        (d / "vendor").write_text(vendor + "  \n")
        (d / "model").write_text(model + "\n")
        (d / "rev").write_text("1.01\n")
    (tmp_path / "sys" / "sda").mkdir()  # not optical
    drives = linux_list_drives(tmp_path / "sys", tmp_path / "dev")
    assert [(d.index, d.product, d.dev) for d in drives] == [
        (0, "DVDRAM AP70NS50", str(tmp_path / "dev/sr0")), (2, "SDRW-08U9M-U", str(tmp_path / "dev/sr2"))]
    assert source_for("dev", drives[0].dev, None) == f"dev:{tmp_path}/dev/sr0"


def test_source_for_linux():
    assert source_for("dev", "/dev/sr1", None) == "dev:/dev/sr1"


def test_log_redaction():
    import logging
    from ripstation.diagnostics import RingBuffer, redact
    line = ('GET https://api.themoviedb.org/3/configuration?language=en-US&api_key=0123456789abcdef0123456789abcdef '
            'http://x:32400/library?X-Plex-Token=abc123 Authorization: Bearer eyJhbGciOi.xyz app_Key = "T-'
            + "A" * 60 + '"')
    out = redact(line)
    assert "01234567" not in out and "abc123" not in out and "eyJhbGciOi" not in out and "AAAAAAAA" not in out
    assert "language=en-US" in out and "api_key=[redacted]" in out
    buf = RingBuffer()
    buf.emit(logging.LogRecord("httpx", logging.INFO, "", 0, line, None, None))
    assert "01234567" not in buf.lines[0]


def _titles(*mins):
    from ripstation.makemkv import Title
    return [Title(id=i, duration_s=int(m * 60), size_bytes=int(m * 1e8), segments=str(i)) for i, m in enumerate(mins)]


def test_old_show_two_episodes_is_tv():
    sel = pick(_titles(54, 51, 6, 3), CFG)
    assert sel.kind == "tv" and sel.title_ids == [0, 1]


def test_unsure_disc_rips_everything_plausible():
    # nothing movie-length, episodes too different to be sure: keep them all for review
    sel = pick(_titles(54, 30, 20, 6), CFG)
    assert sel.kind == "movie" and sel.needs_review and sel.title_ids == [0, 1, 2]


def test_alt_cuts_both_ripped():
    sel = pick(_titles(117, 116, 10), CFG)
    assert sel.title_ids == [0, 1] and sel.needs_review


def test_episodes_in_separate_title_sets_not_deduped():
    """Real NAS disc FAERIE_TALE_THEATRE: 4 episodes, each in its own title set, same segment map."""
    from ripstation.makemkv import Title
    mins = [54.5, 39.5, 50.7, 53.7]
    titles = [Title(id=i, duration_s=int(m * 60), size_bytes=int(m * 9e7), chapters=5, segments="1-5")
              for i, m in enumerate(mins)]
    sel = pick(titles, CFG)
    assert sel.kind == "tv" and sel.title_ids == [0, 1, 2, 3]
