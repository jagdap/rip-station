"""MakeMKV registration: write the key into makemkvcon's settings file.

MAKEMKV_KEY=<key> uses that key; MAKEMKV_KEY=auto fetches the current free beta key
from the MakeMKV forum (it rotates roughly monthly). Settings live in
$HOME/.MakeMKV/settings.conf, which makemkvcon reads on every run.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from pathlib import Path

import httpx

log = logging.getLogger("ripstation")

BETA_KEY_URL = "https://forum.makemkv.com/forum/viewtopic.php?t=1053"
_KEY = re.compile(r"<code>\s*(T-[A-Za-z0-9@_]{40,})\s*</code>")
_SETTING = re.compile(r'^\s*app_Key\s*=.*$', re.M)


def settings_dir(home: Path | None = None) -> Path:
    home = home or Path(os.environ.get("HOME", "~")).expanduser()
    return home / "Library" / "MakeMKV" if sys.platform == "darwin" else home / ".MakeMKV"


def settings_path() -> Path:
    return settings_dir() / "settings.conf"


def set_options(text: str, options: dict[str, str]) -> str:
    """Set `name = "value"` lines in a MakeMKV settings.conf body."""
    for name, value in options.items():
        line = f'{name} = "{value}"'
        pat = re.compile(rf"^\s*{re.escape(name)}\s*=.*$", re.M)
        text = pat.sub(line, text) if pat.search(text) else text + ("" if text.endswith("\n") or not text else "\n") + line + "\n"
    return text


def parse_beta_key(html: str) -> str | None:
    m = _KEY.search(html)
    return m.group(1) if m else None


def fetch_beta_key(timeout: float = 15) -> str | None:
    try:
        r = httpx.get(BETA_KEY_URL, timeout=timeout, follow_redirects=True)
        r.raise_for_status()
        return parse_beta_key(r.text)
    except httpx.HTTPError:
        log.warning("couldn't fetch the MakeMKV beta key", exc_info=True)
        return None


def write_key(key: str, path: Path | None = None) -> None:
    path = path or settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(set_options(path.read_text() if path.exists() else "", {"app_Key": key}))


def ensure_key(refresh: bool = False) -> bool:
    """Apply MAKEMKV_KEY if set. Returns True if a key was written."""
    want = os.environ.get("MAKEMKV_KEY", "").strip()
    if not want:
        return False
    if want.lower() == "auto":
        if not refresh and settings_path().exists() and _SETTING.search(settings_path().read_text()):
            return False  # keep the current key until makemkvcon says it's expired
        key = fetch_beta_key()
        if not key:
            return False
        log.info("installed current MakeMKV beta key")
    else:
        key = want
    write_key(key)
    return True
