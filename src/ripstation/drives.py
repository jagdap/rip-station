"""Optical drive discovery, media detection and eject.

macOS uses `drutil` / `diskutil`; Linux (the NAS container) uses sysfs plus cdrom
ioctls on /dev/srN. Drive count is never configured: whatever is attached is what
we run. These calls are cheap and don't touch makemkvcon, so polling them is safe
while other drives are mid-rip.
"""

from __future__ import annotations

import asyncio
import fcntl
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Drive:
    index: int  # drutil index (1-based); can shift when drives are unplugged
    vendor: str
    product: str
    rev: str
    bus: str
    dev: str | None = None  # Linux: /dev/srN

    @property
    def key(self) -> str:
        return f"{self.index}:{self.vendor} {self.product}".strip()

    @property
    def label(self) -> str:
        return f"{self.vendor} {self.product}".strip()


@dataclass(frozen=True)
class Media:
    present: bool
    device: str | None = None  # /dev/diskN
    media_type: str = ""


_LIST_ROW = re.compile(r"^\s*(\d+)\s+(\S+)\s+(.+?)\s{2,}(\S+)\s+(\S+)\s+(\S+)\s*$")


def parse_drutil_list(text: str) -> list[Drive]:
    drives = []
    for line in text.splitlines():
        m = _LIST_ROW.match(line)
        if m:
            idx, vendor, product, rev, bus, _support = m.groups()
            drives.append(Drive(int(idx), vendor, product.strip(), rev, bus))
    return drives


def parse_drutil_status(text: str) -> Media:
    type_m = re.search(r"Type:\s*(.+?)(?:\s{2,}|$)", text, re.M)
    name_m = re.search(r"Name:\s*(/dev/disk\d+)", text)
    media_type = type_m.group(1).strip() if type_m else ""
    if not type_m or "No Media" in media_type:
        return Media(present=False)
    return Media(present=True, device=name_m.group(1) if name_m else None, media_type=media_type)


async def _run(*args: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    out, _ = await proc.communicate()
    return out.decode("utf-8", errors="replace")


# --- Linux ---------------------------------------------------------------

SYSFS_BLOCK = Path(os.environ.get("RIPSTATION_SYSFS_BLOCK", "/sys/block"))
DEV_DIR = Path(os.environ.get("RIPSTATION_DEV_DIR", "/dev"))

# linux/cdrom.h
CDROMEJECT = 0x5309
CDROM_DRIVE_STATUS = 0x5326
CDROM_LOCKDOOR = 0x5329
CDSL_CURRENT = 0x7FFFFFFF
CDS_DISC_OK = 4


def _read(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def linux_list_drives(sysfs: Path | None = None, dev_dir: Path | None = None) -> list[Drive]:
    sysfs, dev_dir = sysfs or SYSFS_BLOCK, dev_dir or DEV_DIR
    drives = []
    for p in sorted(sysfs.glob("sr*"), key=lambda p: int(p.name[2:] or 0)):
        if not p.name[2:].isdigit():
            continue
        dev = p / "device"
        bus = "USB" if "usb" in os.path.realpath(p) else "SATA"
        drives.append(Drive(int(p.name[2:]), _read(dev / "vendor"), _read(dev / "model"),
                            _read(dev / "rev"), bus, str(dev_dir / p.name)))
    return drives


def _linux_status(dev: str) -> int:
    fd = os.open(dev, os.O_RDONLY | os.O_NONBLOCK)
    try:
        return fcntl.ioctl(fd, CDROM_DRIVE_STATUS, CDSL_CURRENT)
    finally:
        os.close(fd)


def _linux_eject(dev: str) -> None:
    fd = os.open(dev, os.O_RDONLY | os.O_NONBLOCK)
    try:
        try:
            fcntl.ioctl(fd, CDROM_LOCKDOOR, 0)
        except OSError:
            pass
        fcntl.ioctl(fd, CDROMEJECT)
    finally:
        os.close(fd)


async def linux_media_status(drive: Drive) -> Media:
    try:
        status = await asyncio.to_thread(_linux_status, drive.dev)
    except OSError:
        return Media(present=False)
    return Media(present=True, device=drive.dev, media_type="disc") if status == CDS_DISC_OK else Media(False)


async def linux_eject(drive: Drive, media: Media | None = None) -> None:
    try:
        await asyncio.to_thread(_linux_eject, drive.dev)
    except OSError:
        await _run("eject", drive.dev)


# --- dispatch ----------------------------------------------------------------

IS_LINUX = sys.platform.startswith("linux")


async def list_drives() -> list[Drive]:
    if IS_LINUX:
        return await asyncio.to_thread(linux_list_drives)
    return parse_drutil_list(await _run("drutil", "list"))


async def media_status(drive: Drive) -> Media:
    if IS_LINUX:
        return await linux_media_status(drive)
    return parse_drutil_status(await _run("drutil", "-drive", str(drive.index), "status"))


async def eject(drive: Drive, media: Media | None = None) -> None:
    if IS_LINUX:
        return await linux_eject(drive, media)
    # diskutil by device is stable even if drutil indices shift; fall back to drutil.
    if media and media.device:
        out = await _run("diskutil", "eject", media.device)
        if "ejected" in out.lower():
            return
    await _run("drutil", "-drive", str(drive.index), "eject")
