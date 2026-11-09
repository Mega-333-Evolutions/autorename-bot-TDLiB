import asyncio
import json
import os
import re
import shutil
import stat
import sys
import time

import pytest

from bot import speed
from bot.speed import SpeedError, SpeedResult
from bot.tg import html as tl_html
from bot.tg import types as tl_types

CLI_RESULT = {
    "type": "result", "timestamp": "2026-10-21T04:00:00Z",
    "ping": {"jitter": 0.52, "latency": 4.21, "low": 3.9, "high": 5.0},
    "download": {"bandwidth": 31_250_000, "bytes": 400_000_000, "elapsed": 12000},
    "upload": {"bandwidth": 12_500_000, "bytes": 150_000_000, "elapsed": 11000},
    "packetLoss": 0, "isp": "Hetzner Online GmbH",
    "server": {"id": 1234, "host": "speed.example.net", "name": "Example Telecom", "location": "Frankfurt", "country": "Germany"},
    "result": {"id": "abc", "url": "https://www.speedtest.net/result/c/abc"},
}
PY_RESULT = {
    "download": 245_310_000.0, "upload": 118_200_000.0, "ping": 11.5, "share": "http://www.speedtest.net/result/1.png",
    "server": {"sponsor": "Example Telecom", "name": "Frankfurt", "country": "Germany"},
    "client": {"isp": "Hetzner Online GmbH", "ip": "1.2.3.4"},
}


def fake_binary(directory, name, version_text, body):
    """An executable `name` that answers --version with ``version_text`` and anything else by running ``body`` (sh)."""
    path = directory / name
    path.write_text(f'#!/bin/sh\nif [ "$1" = "--version" ]; then\n  echo "{version_text}"\n  exit 0\nfi\n{body}\n')
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


SLEEP = shutil.which("sleep") or "/bin/sleep"  # PATH is replaced in these tests, so scripts need absolute paths
CAT = shutil.which("cat") or "/bin/cat"


def emit(bindir, obj):
    """Shell line that prints ``obj`` as one JSON line (it lives in a file, so quotes inside cannot break the script)."""
    path = bindir.parent / f"out{len(list(bindir.parent.glob('out*.json')))}.json"
    path.write_text(json.dumps(obj) + "\n")
    return f"{CAT} {path}"


@pytest.fixture
def bindir(tmp_path, monkeypatch):
    d = tmp_path / "bin"
    d.mkdir()
    monkeypatch.setenv("PATH", str(d))  # only our fakes can be found
    monkeypatch.delenv("SPEEDTEST_BIN", raising=False)
    return d


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------


def test_parse_cli_converts_bytes_per_second_to_megabits():
    r = speed.parse_cli(CLI_RESULT)
    assert round(r.download_mbps, 2) == 250.0 and round(r.upload_mbps, 2) == 100.0
    assert r.ping_ms == 4.21 and r.jitter_ms == 0.52 and r.packet_loss == 0
    assert r.isp == "Hetzner Online GmbH" and r.server == "Example Telecom" and r.location == "Frankfurt, Germany"
    assert r.url == "https://www.speedtest.net/result/c/abc" and r.source == "Ookla Speedtest CLI"


def test_parse_cli_copes_with_missing_details_but_not_missing_speeds():
    r = speed.parse_cli({"download": {"bandwidth": 1_000_000}, "upload": {"bandwidth": 500_000}})
    assert r.ping_ms == 0.0 and r.jitter_ms is None and r.packet_loss is None and r.isp == "" and r.url == "" and r.location == ""
    for broken in ({}, {"download": {"bandwidth": 1}}, {"download": {"bandwidth": "x"}, "upload": {"bandwidth": 1}}):
        with pytest.raises(SpeedError):
            speed.parse_cli(broken)


def test_parse_python_converts_bits_per_second_to_megabits():
    r = speed.parse_python(PY_RESULT)
    assert round(r.download_mbps, 2) == 245.31 and round(r.upload_mbps, 2) == 118.2 and r.ping_ms == 11.5
    assert r.isp == "Hetzner Online GmbH" and r.server == "Example Telecom" and r.location == "Frankfurt, Germany"
    assert r.url.endswith("1.png") and "speedtest-cli" in r.source
    with pytest.raises(SpeedError):
        speed.parse_python({"ping": 3})


def test_the_result_line_is_picked_out_of_the_cli_output():
    out = "\n".join(["noise", json.dumps({"type": "log", "message": "x"}), json.dumps(CLI_RESULT), "", "{broken json"])
    assert speed._last_result(out)["isp"] == "Hetzner Online GmbH"
    assert speed._last_result("nothing useful") is None
    assert speed._last_result(json.dumps({"type": "log", "message": "x"})) is None


def test_the_reason_for_a_failure_comes_from_the_log_line():
    out = json.dumps({"type": "log", "level": "error", "message": "Configuration - Couldn't resolve host name"})
    assert speed._log_message(out, "") == "Configuration - Couldn't resolve host name"
    assert speed._log_message("", "segmentation fault") == "segmentation fault"
    assert speed._log_message("", "") == "no output"


# ---------------------------------------------------------------------------
# finding and running the CLI
# ---------------------------------------------------------------------------


async def test_the_official_cli_is_found_by_its_version_text(bindir):
    assert await speed.find_ookla_cli() is None
    fake_binary(bindir, "speedtest", "speedtest-cli 2.1.3\nPython 3.12", "exit 1")  # the pip package's script is not the CLI
    assert await speed.find_ookla_cli() is None
    ookla = fake_binary(bindir, "ookla-speedtest", "Speedtest by Ookla 1.2.0.84 (5ae238b) Linux/x86_64", "exit 1")
    assert await speed.find_ookla_cli() == str(ookla)


async def test_a_plain_speedtest_binary_is_accepted_when_it_is_the_ookla_one(bindir):
    path = fake_binary(bindir, "speedtest", "Speedtest by Ookla 1.2.0.84", "exit 1")
    assert await speed.find_ookla_cli() == str(path)


async def test_the_speedtest_bin_variable_wins(bindir, tmp_path, monkeypatch):
    other = tmp_path / "elsewhere"
    other.mkdir()
    chosen = fake_binary(other, "mytest", "Speedtest by Ookla 1.2.0.84", "exit 1")
    fake_binary(bindir, "ookla-speedtest", "Speedtest by Ookla 1.2.0.84", "exit 1")
    monkeypatch.setenv("SPEEDTEST_BIN", str(chosen))
    assert await speed.find_ookla_cli() == str(chosen)


async def test_run_cli_returns_the_parsed_result(bindir):
    path = fake_binary(bindir, "ookla-speedtest", "Speedtest by Ookla 1.2.0.84", emit(bindir, CLI_RESULT))
    r = await speed.run_cli(str(path))
    assert round(r.download_mbps) == 250 and r.server == "Example Telecom"


async def test_run_cli_passes_the_flags_that_make_it_non_interactive(bindir, tmp_path):
    args_file = tmp_path / "args.txt"
    path = fake_binary(bindir, "ookla-speedtest", "Speedtest by Ookla 1.2.0.84", f'echo "$@" > {args_file}\n{emit(bindir, CLI_RESULT)}')
    await speed.run_cli(str(path))
    args = args_file.read_text().split()
    assert "--accept-license" in args and "--accept-gdpr" in args and "--format=json" in args


async def test_run_cli_reports_why_it_failed(bindir):
    log = {"type": "log", "level": "error", "message": "Configuration - Couldn't resolve host name"}
    path = fake_binary(bindir, "ookla-speedtest", "Speedtest by Ookla 1.2.0.84", f"{emit(bindir, log)}\nexit 1")
    with pytest.raises(SpeedError, match="Couldn't resolve host name"):
        await speed.run_cli(str(path))


async def test_run_cli_gives_up_after_the_timeout_and_kills_the_process(bindir, tmp_path):
    pid_file = tmp_path / "pid"
    path = fake_binary(bindir, "ookla-speedtest", "Speedtest by Ookla 1.2.0.84", f"echo $$ > {pid_file}\nexec {SLEEP} 30")
    started = time.monotonic()
    with pytest.raises(SpeedError, match="took too long"):
        await speed.run_cli(str(path), timeout=0.5)
    assert time.monotonic() - started < 5
    pid = int(pid_file.read_text())
    await asyncio.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


async def test_a_binary_that_cannot_start_is_a_speed_error(tmp_path):
    broken = tmp_path / "broken"
    broken.write_text("not executable")
    with pytest.raises(SpeedError):
        await speed.run_cli(str(broken))


# ---------------------------------------------------------------------------
# run_speedtest: CLI first, Python client second
# ---------------------------------------------------------------------------


async def test_the_cli_is_used_when_it_works(bindir, monkeypatch):
    fake_binary(bindir, "ookla-speedtest", "Speedtest by Ookla 1.2.0.84", emit(bindir, CLI_RESULT))

    def boom():
        raise AssertionError("the Python client is not needed")

    monkeypatch.setattr(speed, "_run_python", boom)
    r = await speed.run_speedtest()
    assert r.source == "Ookla Speedtest CLI"


async def test_the_python_client_takes_over_when_the_cli_is_missing(bindir, monkeypatch):
    monkeypatch.setattr(speed, "_run_python", lambda: PY_RESULT)
    r = await speed.run_speedtest()
    assert "speedtest-cli" in r.source and round(r.download_mbps) == 245


async def test_the_python_client_takes_over_when_the_cli_fails(bindir, monkeypatch):
    fake_binary(bindir, "ookla-speedtest", "Speedtest by Ookla 1.2.0.84", "exit 1")
    monkeypatch.setattr(speed, "_run_python", lambda: PY_RESULT)
    assert "speedtest-cli" in (await speed.run_speedtest()).source


async def test_when_both_fail_the_reasons_are_all_given(bindir, monkeypatch):
    fake_binary(bindir, "ookla-speedtest", "Speedtest by Ookla 1.2.0.84", "echo 'server unreachable'\nexit 1")

    def failing():
        raise SpeedError("speedtest-cli failed: HTTP Error 403: Forbidden")

    monkeypatch.setattr(speed, "_run_python", failing)
    with pytest.raises(SpeedError) as exc:
        await speed.run_speedtest()
    assert "server unreachable" in str(exc.value) and "403" in str(exc.value)


async def test_a_missing_python_client_is_explained(bindir, monkeypatch):
    monkeypatch.setitem(sys.modules, "speedtest", None)  # makes `import speedtest` fail, installed or not
    with pytest.raises(SpeedError) as exc:
        await speed.run_speedtest()
    assert "Speedtest CLI is not installed" in str(exc.value) and "speedtest-cli package is not installed" in str(exc.value)


async def test_errors_from_the_python_client_are_wrapped(bindir, monkeypatch):
    class FakeSpeedtest:
        def __init__(self, *a, **kw):
            raise RuntimeError("HTTP Error 403: Forbidden")

    monkeypatch.setitem(sys.modules, "speedtest", type(sys)("speedtest"))
    sys.modules["speedtest"].Speedtest = FakeSpeedtest
    with pytest.raises(SpeedError, match="speedtest-cli failed: HTTP Error 403"):
        await speed.run_speedtest()


async def test_a_python_client_that_hangs_is_given_up_on(bindir, monkeypatch):
    monkeypatch.setattr(speed, "CLI_TIMEOUT", 0.1)
    monkeypatch.setattr(speed, "_run_python", lambda: time.sleep(0.4) or PY_RESULT)
    with pytest.raises(SpeedError, match="took too long"):
        await speed.run_speedtest()


# ---------------------------------------------------------------------------
# the message
# ---------------------------------------------------------------------------


def test_format_result_lists_every_detail():
    text = speed.format_result(speed.parse_cli(CLI_RESULT))
    assert "SPEEDTEST" in text and "Ookla" in text
    assert "<code>250.00 Mbps</code>" in text and "<code>100.00 Mbps</code>" in text
    assert "<code>4.21 ms</code>" in text and "jitter <code>0.52 ms</code>" in text and "<code>0%</code>" in text
    assert "Hetzner Online GmbH" in text and "Example Telecom · Frankfurt, Germany" in text
    assert '<a href="https://www.speedtest.net/result/c/abc">speedtest.net</a>' in text


def test_format_result_skips_what_is_unknown_and_escapes_the_rest():
    r = SpeedResult(download_mbps=1234.5, upload_mbps=2.0, ping_ms=30, isp="A&B <ISP>", server="", location="", url="javascript:alert(1)")
    text = speed.format_result(r)
    assert "<code>1,234.50 Mbps</code>" in text and "A&amp;B &lt;ISP&gt;" in text
    assert "Server" not in text and "Packet loss" not in text and "jitter" not in text and "<a " not in text
    quoted = SpeedResult(download_mbps=1, upload_mbps=1, ping_ms=1, url='https://x.test/a"b')
    assert 'href="https://x.test/a&quot;b"' in speed.format_result(quoted)


def link_order(html_text):
    """Every link Telegram could preview, first to last: the links the message carries, and words that look like a domain (Telegram
    makes links of those itself, e.g. an ISP called "Amazon.com").  The text is plain BMP, so the UTF-16 offsets are string offsets."""
    plain, entities = tl_html.parse(html_text)
    found = [(e.offset, e.url) for e in entities if isinstance(e, tl_types.MessageEntityTextUrl)]
    found += [(m.start(), m.group()) for m in re.finditer(r"\b[\w-]+(?:\.[\w-]+)*\.(?:com|net|org)\b", plain, re.I)]
    return [url for _offset, url in sorted(found)]


RESULT_URL = "https://www.speedtest.net/result/c/abc"


def test_the_result_page_is_the_first_link_so_that_telegram_previews_it():
    r = SpeedResult(
        download_mbps=6385.9, upload_mbps=5202.8, ping_ms=0.85, isp="Amazon.com", server="speed.example.net",
        location="Ashburn, VA, United States", url=RESULT_URL,
    )
    order = link_order(speed.format_result(r))
    assert "Amazon.com" in order and "speed.example.net" in order  # the words that would be previewed instead are really there
    assert order[0] == RESULT_URL


def test_that_first_link_is_invisible_and_changes_nothing_else():
    r = SpeedResult(download_mbps=1, upload_mbps=1, ping_ms=1, url=RESULT_URL, source="Ookla Speedtest CLI")
    text = speed.format_result(r)
    plain, entities = tl_html.parse(text)
    first = entities[0]
    assert isinstance(first, tl_types.MessageEntityTextUrl) and (first.offset, first.length, first.url) == (0, 1, RESULT_URL)
    assert plain[0] == "​" and plain[1:].startswith("⚡ SPEEDTEST · Ookla")
    assert tl_html.parse(text.replace(f'<a href="{RESULT_URL}">​</a>', "", 1))[0] == plain[1:]


def test_no_result_page_means_no_invisible_link_and_no_preview():
    for url in ("", "javascript:alert(1)", "speedtest.net/result/abc"):
        r = SpeedResult(download_mbps=1, upload_mbps=1, ping_ms=1, url=url)
        assert speed.result_link(r) == "" and "<a " not in speed.format_result(r) and "​" not in speed.format_result(r)
    assert speed.result_link(SpeedResult(download_mbps=1, upload_mbps=1, ping_ms=1, url="http://www.speedtest.net/result/1.png")) != ""
