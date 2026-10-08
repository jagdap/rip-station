from pathlib import Path

import pytest

from ripstation.makemkv import parse_info

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixture_info():
    return lambda name: parse_info((FIX / f"{name}.txt").read_text().splitlines())
