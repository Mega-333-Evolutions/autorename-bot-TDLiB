import pytest

from bot import sysinfo, transfer


def test_parse_cpu_max():
    assert sysinfo.parse_cpu_max("200000 100000") == 2.0
    assert sysinfo.parse_cpu_max("max 100000") is None
    assert sysinfo.parse_cpu_max("garbage") is None
    assert sysinfo.parse_cpu_max(None) is None


def test_parse_mem_limit():
    assert sysinfo.parse_mem_limit("17179869184") == 17179869184
    assert sysinfo.parse_mem_limit("max") is None
    assert sysinfo.parse_mem_limit("9223372036854771712") is None  # cgroup v1 "unlimited"
    assert sysinfo.parse_mem_limit(None) is None


def test_parse_stat_value():
    text = "usage_usec 12345\nuser_usec 1\ninactive_file 99"
    assert sysinfo.parse_stat_value(text, "usage_usec") == 12345
    assert sysinfo.parse_stat_value(text, "inactive_file") == 99
    assert sysinfo.parse_stat_value(text, "missing") is None


@pytest.fixture
def fake_cgroup(tmp_path, monkeypatch):
    (tmp_path / "cpu.max").write_text("200000 100000\n")
    (tmp_path / "memory.max").write_text("17179869184\n")
    (tmp_path / "memory.current").write_text("3000000000\n")
    (tmp_path / "memory.stat").write_text("anon 1\ninactive_file 1000000000\n")
    (tmp_path / "cpu.stat").write_text("usage_usec 1000000\n")
    monkeypatch.setattr(sysinfo, "CGROUP_ROOT", str(tmp_path))
    return tmp_path


def test_container_memory_uses_cgroup_and_ignores_page_cache(fake_cgroup):
    used, total = sysinfo.memory_usage()
    assert used == 2_000_000_000
    assert total <= 17179869184
    assert sysinfo.cpu_cores() == 2.0


async def test_container_cpu_percent_from_cgroup(fake_cgroup, monkeypatch):
    calls = {"n": 0}
    real = sysinfo._cgroup_cpu_usec

    def fake_usec():
        calls["n"] += 1
        return 1_000_000 if calls["n"] == 1 else 1_500_000  # 0.5 cpu-seconds over ~0.1s window

    monkeypatch.setattr(sysinfo, "_cgroup_cpu_usec", fake_usec)
    pct = await sysinfo.cpu_percent(interval=0.1)
    assert 0 < pct <= 100
    monkeypatch.setattr(sysinfo, "_cgroup_cpu_usec", real)


def test_falls_back_to_psutil_without_cgroup(tmp_path, monkeypatch):
    monkeypatch.setattr(sysinfo, "CGROUP_ROOT", str(tmp_path / "does-not-exist"))
    used, total = sysinfo.memory_usage()
    assert total > 0 and used >= 0
    assert sysinfo.cpu_cores() >= 1


async def test_format_status_and_stats(tmp_path):
    text = sysinfo.format_status(123.4, 1, "TDLib 1.8.69 · pytdbot 0.10.1")
    assert "123 ms" in text and "DC 1" in text and "v1.0.0" in text and "Uptime" in text
    assert "n/a" in sysinfo.format_status(None, None, "x")
    stats = await sysinfo.format_stats(3, 12, 4_500_000_000, 1, 2, str(tmp_path))
    for needle in ("Users:</b> 3", "Files renamed", "1 running · 2 waiting", "CPU", "RAM", "Disk", "Network"):
        assert needle in stats


# ---------------------------------------------------------------------------
# what makes transfers fast: cryptg, uvloop, parallel transfer
# ---------------------------------------------------------------------------


def test_the_library_line_names_tdlib_and_pytdbot_as_installed():
    # also a guard for the deployment: requirements.txt must keep installing both
    line = sysinfo.library_version()
    assert line.startswith("TDLib 1.") and " · pytdbot 0." in line and "?" not in line


def test_the_library_line_says_when_a_package_cannot_be_found(monkeypatch):
    from importlib import metadata

    def version(name):
        if name == "pytdbot":
            raise metadata.PackageNotFoundError(name)
        return {"tdjson": "1.8.69"}[name]

    monkeypatch.setattr(metadata, "version", version)
    assert sysinfo.library_version() == "TDLib 1.8.69 · pytdbot ?"


async def test_loop_backend_sees_the_standard_loop():
    assert sysinfo.loop_backend() == "asyncio"


def test_loop_backend_outside_a_loop_and_on_uvloop():
    uvloop = pytest.importorskip("uvloop")
    assert sysinfo.loop_backend() == "none"
    loop = uvloop.new_event_loop()
    try:
        assert loop.run_until_complete(_loop_name()) == "uvloop"
    finally:
        loop.close()


async def _loop_name():
    return sysinfo.loop_backend()


def test_the_bot_really_starts_on_uvloop():
    from bot import main as bot_main

    pytest.importorskip("uvloop")
    seen = []

    async def probe():
        seen.append(sysinfo.loop_backend())
        return 0

    assert bot_main._run(probe) == 0 and seen == ["uvloop"]


def test_speed_summary_and_checks_mark_what_is_off(monkeypatch):
    monkeypatch.setattr(sysinfo, "loop_backend", lambda: "uvloop")
    assert sysinfo.speed_summary() == "TDLib ✅ · uvloop ✅"
    assert all(fine for _short, _detail, fine in sysinfo.speed_checks())

    monkeypatch.setattr(sysinfo, "loop_backend", lambda: "asyncio")
    assert sysinfo.speed_summary() == "TDLib ✅ · no uvloop ⚠️"
    details = [detail for _short, detail, _fine in sysinfo.speed_checks()]
    assert "TDLib" in details[0] and "asyncio loop: uvloop is not running" in details[1]


def test_the_speed_section_shows_the_stack_the_counts_and_the_last_failure(monkeypatch):
    monkeypatch.setattr(sysinfo, "loop_backend", lambda: "uvloop")
    stats = transfer.TransferStats(downloads=12, uploads=9)
    text = sysinfo.format_speed(stats)
    assert "⚡ <b>Speed</b>" in text and "Transfers: TDLib: native code that moves a file in parts, in parallel ✅" in text
    assert "Event loop: uvloop ✅" in text and "<b>Downloads:</b> 12 · ⬆ <b>Uploads:</b> 9" in text
    assert "Streaming" not in text and "Streamed" not in text and "⚠️" not in text

    monkeypatch.setattr(sysinfo, "loop_backend", lambda: "asyncio")
    assert "Event loop: asyncio loop: uvloop is not running ⚠️" in sysinfo.format_speed(stats)


def test_format_status_adds_the_speed_section_only_when_given_one():
    library = "TDLib 1.8.69 · pytdbot 0.10.1"
    plain = sysinfo.format_status(10, 1, library)
    assert "Speed" not in plain and plain.endswith(library)
    with_speed = sysinfo.format_status(10, 1, library, "⚡ <b>Speed</b>\nline")
    assert with_speed.startswith(plain) and with_speed.endswith("\n\n⚡ <b>Speed</b>\nline")


def test_the_speed_summary_and_section_say_whether_streaming_is_on(monkeypatch):
    monkeypatch.setattr(sysinfo, "loop_backend", lambda: "uvloop")
    assert sysinfo.speed_summary(True) == "TDLib ✅ · uvloop ✅ · streaming ✅"
    assert sysinfo.speed_summary(False) == "TDLib ✅ · uvloop ✅ · no streaming ✅"  # off is the normal state, not a warning
    assert sysinfo.speed_summary() == "TDLib ✅ · uvloop ✅"  # nothing added when it is not asked for

    stats = transfer.TransferStats(streamed=7)
    text = sysinfo.format_speed(stats, True)
    assert "⬇⬆ Streaming rename: on (experimental): big files are uploaded while they download ✅" in text
    assert "<b>Streamed renames:</b> 7" in text and "Streaming failed" not in text and "⚠️" not in text
    assert "Streaming rename" not in sysinfo.format_speed(stats) and "Streamed renames" not in sysinfo.format_speed(stats)
    assert "Streaming rename: off (STREAM_RENAME=0) ✅" in sysinfo.format_speed(stats, False)

    stats.stream_failed(ValueError("<x>"))
    text = sysinfo.format_speed(stats, False)
    assert "⚠️ Streaming failed 1× and those files were saved first and then uploaded. Last: <code>ValueError: &lt;x&gt;</code>" in text
    assert "Streaming failed" in sysinfo.format_speed(stats)  # said whether or not streaming is on now
