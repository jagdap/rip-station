"""Version shown on the dashboard, so you can tell which image is running."""

import os
from importlib.metadata import PackageNotFoundError, version as _pkg_version

try:
    VERSION = _pkg_version("rip-station")
except PackageNotFoundError:
    VERSION = "dev"

# Set at image build time by scripts/build-nas-image.sh (e.g. "2026-10-07 17:12").
BUILD = os.environ.get("RIPSTATION_BUILD", "local")


def label() -> str:
    return f"v{VERSION} · build {BUILD}"
