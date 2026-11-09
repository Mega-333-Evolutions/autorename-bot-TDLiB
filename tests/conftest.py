import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.errorlog import ChannelLogHandler  # noqa: E402

from .appkit import ctx  # noqa: E402  the handler-level fixture, shared by every test module

__all__ = ["ctx", "needs_ffmpeg"]

HAVE_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
needs_ffmpeg = pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg/ffprobe not installed")


@pytest.fixture(autouse=True)
def _no_stray_error_reporters():
    """A test that stops halfway must not leave an error reporter on the root logger for the tests after it."""
    yield
    for handler in [h for h in logging.getLogger().handlers if isinstance(h, ChannelLogHandler)]:
        handler.detach()

SRT_A = "1\n00:00:00,000 --> 00:00:01,000\nkonnichiwa\n\n"
SRT_B = "1\n00:00:00,000 --> 00:00:01,000\nhello\n\n"
FFMETA = """;FFMETADATA1
title=Original Title
[CHAPTER]
TIMEBASE=1/1000
START=0
END=1000
title=Intro
[CHAPTER]
TIMEBASE=1/1000
START=1000
END=2000
title=Main
"""


def run(cmd):
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def ffprobe_json(path, *extra):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", *extra, str(path)],
        check=True, capture_output=True,
    ).stdout
    return json.loads(out)


@pytest.fixture(scope="session")
def media_dir(tmp_path_factory):
    d = tmp_path_factory.mktemp("media")
    if not HAVE_FFMPEG:
        return d
    (d / "a.srt").write_text(SRT_A)
    (d / "b.srt").write_text(SRT_B)
    (d / "meta.ffmeta").write_text(FFMETA)
    (d / "font.ttf").write_bytes(os.urandom(2048))
    lavfi_v = ["-f", "lavfi", "-i", "testsrc=duration=2:size=320x240:rate=15"]
    lavfi_a = lambda f: ["-f", "lavfi", "-i", f"sine=frequency={f}:duration=2"]  # noqa: E731

    # MKV: 1 video, 3 audio (jpn, eng, hin), 2 subs (jpn, eng), 1 font attachment, 2 chapters
    run(
        ["ffmpeg", "-y", "-loglevel", "error", *lavfi_v, *lavfi_a(440), *lavfi_a(880), *lavfi_a(220),
         "-i", str(d / "a.srt"), "-i", str(d / "b.srt"), "-i", str(d / "meta.ffmeta"),
         "-attach", str(d / "font.ttf"), "-metadata:s:t:0", "mimetype=application/x-truetype-font",
         "-map", "0:v", "-map", "1:a", "-map", "2:a", "-map", "3:a", "-map", "4:s", "-map", "5:s",
         "-map_metadata", "6", "-map_chapters", "6",
         "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-c:s", "srt",
         "-metadata:s:a:0", "language=jpn", "-metadata:s:a:0", "title=Japanese Original",
         "-metadata:s:a:1", "language=eng", "-metadata:s:a:2", "language=hin",
         "-metadata:s:s:0", "language=jpn", "-metadata:s:s:1", "language=eng",
         "-disposition:a:0", "default", str(d / "sample.mkv")]
    )
    # MP4: 1 video, 2 audio (jpn, eng), 1 eng subtitle (mov_text)
    run(
        ["ffmpeg", "-y", "-loglevel", "error", *lavfi_v, *lavfi_a(440), *lavfi_a(880), "-i", str(d / "b.srt"),
         "-map", "0:v", "-map", "1:a", "-map", "2:a", "-map", "3:s",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-c:s", "mov_text",
         "-metadata:s:a:0", "language=jpn", "-metadata:s:a:1", "language=eng", "-metadata:s:s:0", "language=eng",
         str(d / "sample.mp4")]
    )
    # MP3 with a title tag
    run(
        ["ffmpeg", "-y", "-loglevel", "error", *lavfi_a(440), "-c:a", "libmp3lame", "-metadata", "title=Old",
         str(d / "sample.mp3")]
    )
    return d
