"""rip-station CLI.

  rip-station serve                 run the engine + dashboard
  rip-station drives                show detected drives and media (drutil + makemkv)
  rip-station capture NAME [-d N]   Phase 0: save `makemkvcon -r info` for the disc in drive N
  rip-station identify FIXTURE      run picker + LLM/TMDB naming on a captured fixture
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from pathlib import Path

from . import config as config_mod
from . import drives as drv
from .makemkv import MakeMKV, MakeMKVError, parse_info, source_for
from .picker import pick


async def cmd_drives(cfg) -> None:
    found = await drv.list_drives()
    if not found:
        print("drutil: no optical drives connected")
    for d in found:
        m = await drv.media_status(d)
        print(f"drutil #{d.index}: {d.label} ({d.bus})  media={m.media_type or 'none'} device={m.device}")
    mkv = MakeMKV(cfg.makemkv.binary, cfg.makemkv.minlength)
    try:
        slots = await mkv.scan_drives()
    except MakeMKVError as e:
        raise SystemExit(f"makemkv: {e}")
    for s in slots:
        print(f"makemkv disc:{s.index}: {s.drive_name}  visible={s.visible} disc={s.disc_name!r} device={s.device!r}")


async def cmd_capture(cfg, name: str, drive_index: int | None, out_dir: Path) -> None:
    found = await drv.list_drives()
    if not found:
        raise SystemExit("no optical drives connected")
    d = next((x for x in found if x.index == drive_index), found[0]) if drive_index else found[0]
    media = await drv.media_status(d)
    if not media.present:
        raise SystemExit(f"no disc in {d.label}")
    mkv = MakeMKV(cfg.makemkv.binary, cfg.makemkv.minlength)
    source = source_for(cfg.makemkv.address, media.device, d.index - 1)
    print(f"reading {source} ({d.label}) …")
    info, lines = await mkv.info(source)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{name}.txt"
    path.write_text("".join(lines))
    print(f"saved {path}  label={info.label!r}  titles={len(info.titles)}")
    _summarize(info, cfg)


def _summarize(info, cfg) -> None:
    for t in info.titles:
        langs = ",".join(s.lang for s in t.streams if s.kind.lower().startswith("audio"))
        print(f"  title {t.id:>2}: {t.duration_s // 60:>3} min  {t.size_bytes / 1e9:5.2f} GB  "
              f"ch={t.chapters:<3} audio={langs}  file={t.output_file}")
    sel = pick(info.titles, cfg.picker)
    print(f"picker: kind={sel.kind} titles={sel.title_ids} review={sel.reasons or 'no'}")
    return sel


async def cmd_identify(cfg, fixture: Path) -> None:
    from .llm import LLM
    from .naming import Namer
    from .tmdb import TMDB

    info = parse_info(fixture.read_text().splitlines())
    print(f"label={info.label!r}")
    sel = _summarize(info, cfg)
    by_id = {t.id: t for t in info.titles}
    durs = ([max(by_id[i].duration_s for i in sel.title_ids)] if sel.kind == "movie"
            else [by_id[i].duration_s for i in sel.title_ids])
    tmdb = TMDB(cfg.tmdb.key, cfg.tmdb.language)
    llm = LLM(cfg.llm.base_url, cfg.llm.model, cfg.llm.timeout_s) if cfg.llm.enabled else None
    prop = await Namer(tmdb, llm, cfg.picker.runtime_tolerance_min).identify(info.label, sel.kind, durs)
    for c in prop.candidates:
        mark = "→" if prop.choice and prop.choice["tmdb_id"] == c["tmdb_id"] else " "
        print(f" {mark} {c['tmdb_id']:>8} {c['title']} ({c['year']}) {c['runtime_min']} min")
    print(f"choice: {prop.choice}")
    print(f"auto_accept={prop.auto_accept} reasons={prop.gate_reasons}")


def main() -> None:
    ap = argparse.ArgumentParser(prog="rip-station")
    ap.add_argument("--config", default=None, help="default: $RIPSTATION_CONFIG or ./config.toml")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve")
    sub.add_parser("drives")
    c = sub.add_parser("capture")
    c.add_argument("name")
    c.add_argument("-d", "--drive", type=int, help="drutil drive index (default: first)")
    c.add_argument("-o", "--out", type=Path, default=Path("tests/fixtures"))
    i = sub.add_parser("identify")
    i.add_argument("fixture", type=Path)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if hasattr(signal, "SIGCHLD"):
        # A container runtime can hand us SIGCHLD=ignored, which makes the kernel reap
        # makemkvcon before asyncio reads its exit status ("returncode 255").
        signal.signal(signal.SIGCHLD, signal.SIG_DFL)
    cfg = config_mod.load(args.config)

    if args.cmd == "serve":
        import uvicorn

        from .diagnostics import install_log_buffer
        from .server import create_app
        from .station import Station

        install_log_buffer()
        app = create_app(Station(cfg))
        from . import version
        print(f"Rip Station {version.label()} on http://{cfg.server.host}:{cfg.server.port}")
        logging.getLogger("ripstation").info("Rip Station %s starting", version.label())
        uvicorn.run(app, host=cfg.server.host, port=cfg.server.port, log_level="warning")
    elif args.cmd == "drives":
        asyncio.run(cmd_drives(cfg))
    elif args.cmd == "capture":
        asyncio.run(cmd_capture(cfg, args.name, args.drive, args.out))
    elif args.cmd == "identify":
        asyncio.run(cmd_identify(cfg, args.fixture))
