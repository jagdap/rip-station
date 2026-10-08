"""makemkvcon robot-mode (-r) runner and parser.

Robot lines look like `KEY:field,field,"quoted, field"`. Attribute ids come from
MakeMKV's apdefs.h; only the ones we use are named here. Verify against real
fixtures captured in Phase 0 (tests/fixtures/).
"""

from __future__ import annotations

import asyncio
import csv
import os
import re
import shutil
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator

# apdefs.h: ap_ItemAttributeId
ATTR_TYPE = 1
ATTR_NAME = 2
ATTR_LANG_CODE = 3
ATTR_LANG_NAME = 4
ATTR_CODEC_SHORT = 6
ATTR_CHAPTERS = 8
ATTR_DURATION = 9
ATTR_SIZE_BYTES = 11
ATTR_STREAM_FLAGS = 22
ATTR_SEGMENTS_MAP = 26
ATTR_OUTPUT_FILE = 27
ATTR_VOLUME_NAME = 32

# apdefs.h: AP_AVStreamFlag_ForcedSubtitles
STREAM_FLAG_FORCED = 2048

# DRV "visible" field: 0 empty, 1 tray open, 2 disc inserted, 3 loading, 256 no drive
DRV_NO_DRIVE = 256
DRV_INSERTED = 2

PRGV_MAX = 65536


def split_fields(payload: str) -> list[str]:
    return next(csv.reader([payload], skipinitialspace=False))


def parse_duration(s: str) -> int:
    """'1:52:33' -> seconds."""
    total = 0
    for part in s.strip().split(":"):
        total = total * 60 + int(part or 0)
    return total


@dataclass
class Stream:
    index: int
    kind: str = ""  # Video / Audio / Subtitles
    lang: str = ""
    lang_name: str = ""
    codec: str = ""
    name: str = ""
    flags: int = 0

    @property
    def forced(self) -> bool:
        return bool(self.flags & STREAM_FLAG_FORCED) or "forced" in self.name.lower()


@dataclass
class Title:
    id: int
    name: str = ""
    duration_s: int = 0
    size_bytes: int = 0
    chapters: int = 0
    segments: str = ""
    output_file: str = ""
    streams: list[Stream] = field(default_factory=list)


@dataclass
class DriveSlot:
    index: int
    visible: int
    enabled: int
    flags: int
    drive_name: str
    disc_name: str
    device: str

    @property
    def present(self) -> bool:
        return self.visible != DRV_NO_DRIVE and bool(self.drive_name)

    @property
    def has_disc(self) -> bool:
        return self.visible == DRV_INSERTED


@dataclass
class Message:
    code: int
    flags: int
    text: str
    params: list[str] = field(default_factory=list)


@dataclass
class Progress:
    current: int
    total: int
    max: int = PRGV_MAX

    @property
    def fraction(self) -> float:
        return self.total / self.max if self.max else 0.0


@dataclass
class Phase:
    """A PRGT (top-level task) or PRGC (sub-step) change, e.g. 'Decrypting data'."""
    code: int
    name: str
    top_level: bool


# PRGT code for the actual copy; PRGV before this belongs to opening/decrypting the disc,
# whose bar fills to ~90% and then resets (seen on real discs, MakeMKV 2.0.0).
PRGT_SAVING = 5024


@dataclass
class DiscInfo:
    disc_name: str = ""
    volume_name: str = ""
    titles: list[Title] = field(default_factory=list)
    drives: list[DriveSlot] = field(default_factory=list)
    messages: list[Message] = field(default_factory=list)

    @property
    def label(self) -> str:
        return self.volume_name or self.disc_name


def parse_line(line: str):
    """Return (key, fields) or None for non-robot lines."""
    line = line.rstrip("\r\n")
    key, sep, payload = line.partition(":")
    if not sep or not key.isupper():
        return None
    try:
        return key, split_fields(payload)
    except (csv.Error, StopIteration):
        return None


def parse_info(lines) -> DiscInfo:
    info = DiscInfo()
    titles: dict[int, Title] = {}

    def title(i: int) -> Title:
        return titles.setdefault(i, Title(id=i))

    for raw in lines:
        parsed = parse_line(raw)
        if not parsed:
            continue
        key, f = parsed
        if key == "DRV" and len(f) >= 7:
            info.drives.append(DriveSlot(int(f[0]), int(f[1]), int(f[2]), int(f[3]), f[4], f[5], f[6]))
        elif key == "MSG" and len(f) >= 4:
            info.messages.append(Message(int(f[0]), int(f[1]), f[3], f[5:]))
        elif key == "CINFO" and len(f) >= 3:
            attr, value = int(f[0]), f[2]
            if attr == ATTR_NAME:
                info.disc_name = value
            elif attr == ATTR_VOLUME_NAME:
                info.volume_name = value
        elif key == "TINFO" and len(f) >= 4:
            t, attr, value = title(int(f[0])), int(f[1]), f[3]
            if attr == ATTR_NAME:
                t.name = value
            elif attr == ATTR_DURATION:
                t.duration_s = parse_duration(value)
            elif attr == ATTR_SIZE_BYTES:
                t.size_bytes = int(value or 0)
            elif attr == ATTR_CHAPTERS:
                t.chapters = int(value or 0)
            elif attr == ATTR_SEGMENTS_MAP:
                t.segments = value
            elif attr == ATTR_OUTPUT_FILE:
                t.output_file = value
        elif key == "SINFO" and len(f) >= 5:
            t, si, attr, value = title(int(f[0])), int(f[1]), int(f[2]), f[4]
            while len(t.streams) <= si:
                t.streams.append(Stream(index=len(t.streams)))
            s = t.streams[si]
            if attr == ATTR_TYPE:
                s.kind = value
            elif attr == ATTR_LANG_CODE:
                s.lang = value
            elif attr == ATTR_LANG_NAME:
                s.lang_name = value
            elif attr == ATTR_CODEC_SHORT:
                s.codec = value
            elif attr == ATTR_NAME:
                s.name = value
            elif attr == ATTR_STREAM_FLAGS:
                s.flags = int(value or 0)
    info.titles = [titles[k] for k in sorted(titles)]
    return info


# Message codes we act on.
MSG_TOO_OLD = 5021  # "This application version is too old" (beta key expired)
MSG_EVAL_EXPIRED = 5055


class MakeMKVError(RuntimeError):
    pass


# Physical read problems (dirty/scratched/defective disc). Texts per MakeMKV's
# "rip errors" FAQ; matched on text because the MSG codes aren't documented.
_READ_ERROR = re.compile(
    r"MEDIUM ERROR|READ RETRIES EXHAUSTED|L-EC UNCORRECTABLE|POSITIONING ERROR|NO SEEK COMPLETE"
    r"|CANNOT READ MEDIUM|occurred while reading|read error", re.I)
# Drive/connection problems: not the disc's fault, retrying with salvage won't help.
_DRIVE_ERROR = re.compile(
    r"HARDWARE ERROR|LOGICAL UNIT|device (?:was )?(?:removed|disconnected)|can't find any usable optical"
    r"|NOT READY:(?!CANNOT READ MEDIUM)", re.I)
_SAVED_OK = re.compile(r"\b[1-9]\d* titles? saved", re.I)
_TITLE_FAILED = re.compile(r"failed to save title|titles saved, [1-9]\d* failed|\b[1-9]\d* failed\b", re.I)


def classify(text: str) -> str | None:
    """'read' | 'drive' | 'failed' | None for a makemkvcon message."""
    if _READ_ERROR.search(text):
        return "read"
    if _DRIVE_ERROR.search(text):
        return "drive"
    if _TITLE_FAILED.search(text):
        return "failed"
    return None


@contextmanager
def settings_home(options: dict[str, str] | None):
    """Yield an env dict whose HOME holds a copy of MakeMKV's settings with `options`
    applied, so one makemkvcon run can use e.g. more read retries without touching
    the settings other drives are using. Yields None when there's nothing to change."""
    if not options:
        yield None
        return
    from . import keys

    real = keys.settings_dir()
    with tempfile.TemporaryDirectory(prefix="ripstation-home-") as tmp:
        home = Path(tmp)
        target = keys.settings_dir(home)
        if real.is_dir():
            shutil.copytree(real, target)
        target.mkdir(parents=True, exist_ok=True)
        conf = target / "settings.conf"
        conf.write_text(keys.set_options(conf.read_text() if conf.exists() else "", options))
        yield {**os.environ, "HOME": str(home)}


def fatal_message(info_or_msgs) -> str | None:
    msgs = info_or_msgs.messages if isinstance(info_or_msgs, DiscInfo) else info_or_msgs
    for m in msgs:
        if m.code in (MSG_TOO_OLD, MSG_EVAL_EXPIRED):
            return m.text
    return None


class MakeMKV:
    def __init__(self, binary: str, minlength: int):
        self.binary = binary
        self.minlength = minlength

    def _base(self) -> list[str]:
        return [self.binary, "-r", "--noscan", f"--minlength={self.minlength}"]

    async def _lines(self, args: list[str], rc: list[int] | None = None,
                     env: dict[str, str] | None = None) -> AsyncIterator[str]:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
        try:
            assert proc.stdout is not None
            async for raw in proc.stdout:
                yield raw.decode("utf-8", errors="replace")
        finally:
            if proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            await proc.wait()
            if rc is not None:
                rc.append(proc.returncode)

    async def scan_drives(self) -> list[DriveSlot]:
        lines = [l async for l in self._lines([self.binary, "-r", "--cache=1", "info", "disc:9999"])]
        info = parse_info(lines)
        if err := fatal_message(info):
            raise MakeMKVError(err)
        return [d for d in info.drives if d.present]

    async def info(self, source: str) -> tuple[DiscInfo, list[str]]:
        lines = [l async for l in self._lines([*self._base(), "info", source])]
        info = parse_info(lines)
        if err := fatal_message(info):
            raise MakeMKVError(err)
        return info, lines

    async def rip(self, source: str, title_id: int | str, out_dir: Path,
                  settings: dict[str, str] | None = None) -> AsyncIterator[Progress | Message | Phase]:
        """settings: MakeMKV options for this run only, e.g. {"io_ErrorRetryCount": "64"}."""
        out_dir.mkdir(parents=True, exist_ok=True)
        args = [*self._base(), "--progress=-same", "mkv", source, str(title_id), str(out_dir)]
        with settings_home(settings) as env:
            async for ev in self._rip_events(args, env):
                yield ev

    async def _rip_events(self, args: list[str], env) -> AsyncIterator[Progress | Message | Phase]:
        rc: list[int] = []
        saving = False
        saved_ok = False
        async for raw in self._lines(args, rc, env):
            parsed = parse_line(raw)
            if not parsed:
                continue
            key, f = parsed
            if key in ("PRGT", "PRGC") and len(f) >= 3:
                if key == "PRGT":
                    saving = int(f[0]) == PRGT_SAVING or f[2].lower().startswith("saving")
                yield Phase(int(f[0]), f[2], key == "PRGT")
            elif key == "PRGV" and len(f) >= 3 and saving:
                yield Progress(int(f[0]), int(f[1]), int(f[2]) or PRGV_MAX)
            elif key == "MSG" and len(f) >= 4:
                msg = Message(int(f[0]), int(f[1]), f[3], f[5:])
                if fatal_message([msg]):
                    raise MakeMKVError(msg.text)
                if _SAVED_OK.search(msg.text) and not _TITLE_FAILED.search(msg.text):
                    saved_ok = True
                yield msg
        # In some containers asyncio can't read the exit status and reports 255; trust
        # makemkvcon's own "N titles saved" message over the exit code. The caller still
        # verifies the output file.
        if rc and rc[0] not in (0, 255) and not saved_ok:
            raise MakeMKVError(f"makemkvcon exited with {rc[0]}")


def source_for(address: str, device: str | None, index: int | None) -> str:
    """Build a makemkvcon source spec. 'dev' uses the device path (/dev/rdiskN on macOS,
    /dev/srN on Linux), 'disc' the makemkv index."""
    if address == "dev" and device:
        if device.startswith("/dev/disk"):  # macOS: raw device is faster
            device = device.replace("/dev/", "/dev/r", 1)
        return f"dev:{device}"
    if index is None:
        raise ValueError("disc addressing needs a makemkv drive index")
    return f"disc:{index}"
