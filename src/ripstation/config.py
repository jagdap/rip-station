"""Settings loaded from config.toml (see config.example.toml)."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

from pydantic import BaseModel, Field


class MakeMKV(BaseModel):
    binary: str = "/Applications/MakeMKV.app/Contents/MacOS/makemkvcon"
    # Must be identical for `info` and `mkv` so title ids line up.
    minlength: int = 300
    # Track selection (audio/subtitle languages) can't be set from the CLI; makemkvcon
    # reads it from MakeMKV's preferences. Default keeps every track (best for archiving).
    # "dev" addresses drives by device path (stable); "disc" by makemkv index.
    address: str = "dev"


class Rip(BaseModel):
    # On read errors: retry with more re-reads, then (if salvage) skip unreadable spots.
    retry_count: int = 64            # MakeMKV io_ErrorRetryCount for the retry attempt
    salvage: bool = True             # last resort: io_IgnoreReadErrors, job always goes to review
    min_size_ratio: float = 0.9      # output smaller than this x the title size = incomplete
    drive_failures_warn: int = 2     # consecutive failed discs before blaming the drive


class Paths(BaseModel):
    staging: Path = Path("staging")
    data: Path = Path("data")
    nas_root: Path = Path("/Volumes/Media")
    # True on the Mac (nas_root must be a network mount, never a stale local folder);
    # False when running on the NAS itself, where the media folder is local.
    nas_is_network_mount: bool = True
    movies_dir: str = "Movies"
    tv_dir: str = "TV"


class TMDB(BaseModel):
    # v3 api key or v4 read access token; both work.
    key: str = ""
    language: str = "en-US"


class LLM(BaseModel):
    enabled: bool = True
    base_url: str = "http://127.0.0.1:8080/v1"
    model: str = "mlx-community/Qwen3-4B-Instruct-2507-4bit"
    timeout_s: float = 60.0


class Plex(BaseModel):
    url: str = ""
    token: str = ""


class Picker(BaseModel):
    movie_min_s: int = 60 * 60
    episode_min_s: int = 18 * 60
    episode_max_s: int = 65 * 60
    alt_cut_ratio: float = 0.05
    runtime_tolerance_min: int = 8


class Server(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8765


class Settings(BaseModel):
    makemkv: MakeMKV = Field(default_factory=MakeMKV)
    rip: Rip = Field(default_factory=Rip)
    paths: Paths = Field(default_factory=Paths)
    tmdb: TMDB = Field(default_factory=TMDB)
    llm: LLM = Field(default_factory=LLM)
    plex: Plex = Field(default_factory=Plex)
    picker: Picker = Field(default_factory=Picker)
    server: Server = Field(default_factory=Server)
    poll_media_s: float = 5.0
    poll_drives_s: float = 10.0
    rescan_wait_s: float = 15.0  # unreadable disc: re-read after this, then 3x this
    # "auto" (macOS pop-up on a Mac, nothing elsewhere), "none", or an ntfy topic URL
    # such as "https://ntfy.sh/my-rip-station" for phone notifications.
    notify: str = "auto"


def load(path: str | Path | None = None) -> Settings:
    p = Path(path or os.environ.get("RIPSTATION_CONFIG", "config.toml"))
    if not p.exists():
        return Settings()
    return Settings.model_validate(tomllib.loads(p.read_text()))
