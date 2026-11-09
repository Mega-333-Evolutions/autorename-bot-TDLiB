"""The pins in requirements.txt.  Pytdbot is made for one TDLib release and warns at every start when another one is installed."""
import re
from importlib import metadata
from pathlib import Path

import pytdbot.types

REQUIREMENTS = Path(__file__).resolve().parent.parent / "requirements.txt"


def pinned(name: str) -> str:
    for line in REQUIREMENTS.read_text().splitlines():
        match = re.fullmatch(rf"{name}==(\S+)", line.strip(), re.IGNORECASE)
        if match:
            return match.group(1)
    raise AssertionError(f"{name} is not pinned in requirements.txt")


def test_the_tdjson_pin_is_the_tdlib_release_pytdbot_is_made_for():
    assert pinned("tdjson") == pytdbot.types.TDLIB_VERSION


def test_the_installed_libraries_are_the_pinned_ones():
    assert metadata.version("tdjson") == pinned("tdjson")
    assert metadata.version("Pytdbot") == pinned("Pytdbot")
