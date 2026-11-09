"""Uptime / ping / resource numbers for /status and /stats.

Inside a container psutil reports the *host's* CPU and RAM, so the cgroup files
are read first and psutil is only the fallback.
"""
from __future__ import annotations

import asyncio
import os
import platform
import shutil
import time
from typing import Any, List, Optional, Tuple

import psutil

from . import __version__
from .utils import clip, esc, human_size, human_uptime

START_TIME = time.time()
CGROUP_ROOT = "/sys/fs/cgroup"
_NET_BASE = psutil.net_io_counters()


def _read(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return None


def parse_cpu_max(text: Optional[str]) -> Optional[float]:
    """cgroup v2 ``cpu.max`` -> number of cores, ``None`` when unlimited."""
    if not text:
        return None
    parts = text.split()
    if len(parts) != 2 or parts[0] == "max":
        return None
    try:
        quota, period = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    return quota / period if quota > 0 and period > 0 else None


def parse_mem_limit(text: Optional[str]) -> Optional[int]:
    """cgroup ``memory.max`` / v1 ``limit_in_bytes`` -> bytes, ``None`` when unlimited."""
    if not text or text.strip() == "max":
        return None
    try:
        value = int(text.strip())
    except ValueError:
        return None
    return value if value < 2 ** 60 else None


def parse_stat_value(text: Optional[str], key: str) -> Optional[int]:
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == key:
            try:
                return int(parts[1])
            except ValueError:
                return None
    return None


def cpu_cores() -> float:
    quota = parse_cpu_max(_read(f"{CGROUP_ROOT}/cpu.max"))
    return quota if quota else float(psutil.cpu_count() or 1)


def _cgroup_cpu_usec() -> Optional[int]:
    return parse_stat_value(_read(f"{CGROUP_ROOT}/cpu.stat"), "usage_usec")


async def cpu_percent(interval: float = 0.5) -> float:
    """CPU use of this container as a percentage of its own limit."""
    first = _cgroup_cpu_usec()
    if first is None:
        return psutil.cpu_percent(interval=None)
    t0 = time.monotonic()
    await asyncio.sleep(interval)
    second = _cgroup_cpu_usec()
    elapsed = time.monotonic() - t0
    if second is None or elapsed <= 0:
        return psutil.cpu_percent(interval=None)
    return max(0.0, min(100.0, (second - first) / (elapsed * 1_000_000) / cpu_cores() * 100))


def memory_usage() -> Tuple[int, int]:
    """(used, total) bytes for this container."""
    current = _read(f"{CGROUP_ROOT}/memory.current")
    limit = parse_mem_limit(_read(f"{CGROUP_ROOT}/memory.max"))
    if current is None:  # cgroup v1
        current = _read(f"{CGROUP_ROOT}/memory/memory.usage_in_bytes")
        limit = parse_mem_limit(_read(f"{CGROUP_ROOT}/memory/memory.limit_in_bytes"))
    host_total = psutil.virtual_memory().total
    if current is None or not current.isdigit():
        vm = psutil.virtual_memory()
        return vm.used, vm.total
    used = int(current)
    inactive = parse_stat_value(_read(f"{CGROUP_ROOT}/memory.stat"), "inactive_file") or 0
    used = max(0, used - inactive)  # do not count reclaimable page cache
    return used, min(limit, host_total) if limit else host_total


def disk_usage(path: str) -> Tuple[int, int, int]:
    os.makedirs(path, exist_ok=True)
    du = shutil.disk_usage(path)
    return du.used, du.total, du.free


def net_since_start() -> Tuple[int, int]:
    now = psutil.net_io_counters()
    return max(0, now.bytes_recv - _NET_BASE.bytes_recv), max(0, now.bytes_sent - _NET_BASE.bytes_sent)


def uptime() -> float:
    return time.time() - START_TIME


def library_version() -> str:
    """The Telegram library for /status: ``TDLib 1.8.65 · pytdbot 0.10.1`` (the version of the ``tdjson`` package is TDLib's)."""
    from importlib import metadata

    def version(name: str) -> str:
        try:
            return metadata.version(name)
        except metadata.PackageNotFoundError:
            return "?"

    return f"TDLib {version('tdjson')} · pytdbot {version('pytdbot')}"


def loop_backend() -> str:
    """``uvloop`` when the running event loop is uvloop's, ``asyncio`` for the standard one, ``none`` outside a loop."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return "none"
    return "uvloop" if type(loop).__module__.split(".")[0] == "uvloop" else "asyncio"


def speed_checks(stream: Optional[bool] = None) -> List[Tuple[str, str, bool]]:
    """``(short name, detail, fine)`` for the things transfer speed depends on; ``stream`` (when given) adds the streaming rename."""
    loop = loop_backend()
    transfers = ("TDLib", "TDLib: native code that moves a file in parts, in parallel", True)
    event_loop = ("uvloop", "uvloop", True) if loop == "uvloop" else ("no uvloop", f"{loop} loop: uvloop is not running", False)
    checks = [transfers, event_loop]
    if stream is not None:
        checks.append(
            ("streaming", "on (experimental): big files are uploaded while they download", True) if stream
            else ("no streaming", "off (STREAM_RENAME=0)", True)
        )
    return checks


def speed_summary(stream: Optional[bool] = None) -> str:
    """One line for the start-up notice: ``TDLib ✅ · uvloop ✅ · no streaming ✅``."""
    return " · ".join(f"{short} {'✅' if fine else '⚠️'}" for short, _detail, fine in speed_checks(stream))


def format_speed(stats: Any, stream: Optional[bool] = None) -> str:
    """The Speed section of /status: what is switched on, and how the transfers since the start went."""
    labels = ("🚀 Transfers", "🔁 Event loop", "⬇⬆ Streaming rename")
    lines = ["⚡ <b>Speed</b>"]
    for label, (_short, detail, fine) in zip(labels, speed_checks(stream)):
        lines.append(f"{label}: {esc(detail)} {'✅' if fine else '⚠️'}")
    lines.append(f"⬇ <b>Downloads:</b> {stats.downloads} · ⬆ <b>Uploads:</b> {stats.uploads}")
    if stream is not None:
        lines.append(f"⬇⬆ <b>Streamed renames:</b> {stats.streamed}")
    if stats.stream_fallbacks:
        lines.append(
            f"⚠️ Streaming failed {stats.stream_fallbacks}× and those files were saved first and then uploaded. "
            f"Last: <code>{esc(clip(stats.last_stream_fallback, 160))}</code>"
        )
    return "\n".join(lines)


def format_status(ping_ms: Optional[float], dc_id: Optional[int], library: str, speed: str = "") -> str:
    ping = f"{ping_ms:.0f} ms" if ping_ms is not None else "n/a"
    dc = f" (DC {dc_id})" if dc_id else ""
    return (
        "🟢 <b>Bot status</b>\n\n"
        f"⏱ <b>Uptime:</b> {human_uptime(uptime())}\n"
        f"🏓 <b>Ping:</b> {ping}{dc}\n"
        f"🤖 <b>Version:</b> v{__version__}\n"
        f"🐍 Python {platform.python_version()} · {library}"
        + (f"\n\n{speed}" if speed else "")
    )


async def format_stats(users: int, files: int, total_bytes: int, running: int, waiting: int, temp_dir: str) -> str:
    cpu = await cpu_percent()
    used, total = memory_usage()
    d_used, d_total, d_free = disk_usage(temp_dir)
    rx, tx = net_since_start()
    rss = psutil.Process().memory_info().rss
    ram_pct = used / total * 100 if total else 0.0
    disk_pct = d_used / d_total * 100 if d_total else 0.0
    return (
        "📊 <b>System stats</b>\n\n"
        f"👥 <b>Users:</b> {users}\n"
        f"📁 <b>Files renamed:</b> {files} ({human_size(total_bytes)})\n"
        f"🧵 <b>Queue:</b> {running} running · {waiting} waiting\n\n"
        f"🖥 <b>CPU:</b> {cpu:.1f}% of {cpu_cores():g} core(s)\n"
        f"🧠 <b>RAM:</b> {human_size(used)} / {human_size(total)} ({ram_pct:.1f}%) · bot {human_size(rss)}\n"
        f"💾 <b>Disk:</b> {human_size(d_used)} / {human_size(d_total)} ({disk_pct:.1f}%) · {human_size(d_free)} free\n"
        f"🌐 <b>Network since start:</b> ↓ {human_size(rx)} · ↑ {human_size(tx)}"
    )
