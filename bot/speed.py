"""/speed: an Ookla Speedtest run from the bot's own network.

The official Speedtest CLI is used when it is installed.  If it is missing or fails, the ``speedtest-cli`` Python
package (also an Ookla client) is tried.  Both give the same figures; only the parsing differs.
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import shutil
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .utils import esc

log = logging.getLogger(__name__)

CLI_TIMEOUT = 120  # seconds for one run of either client
CANDIDATES = ("ookla-speedtest", "speedtest")  # the Dockerfile installs the official binary as ookla-speedtest


class SpeedError(RuntimeError):
    """The speed test could not be run; the message is safe to show to the user."""


@dataclass
class SpeedResult:
    download_mbps: float
    upload_mbps: float
    ping_ms: float
    jitter_ms: Optional[float] = None
    packet_loss: Optional[float] = None
    isp: str = ""
    server: str = ""
    location: str = ""
    url: str = ""  # the speedtest.net page of this result
    source: str = ""


def _num(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_cli(data: Dict[str, Any]) -> SpeedResult:
    """The JSON the official CLI prints (bandwidth is in bytes per second)."""
    download = _num((data.get("download") or {}).get("bandwidth"))
    upload = _num((data.get("upload") or {}).get("bandwidth"))
    if download is None or upload is None:
        raise SpeedError("the Speedtest CLI gave no download or upload figure")
    ping = data.get("ping") or {}
    server = data.get("server") or {}
    location = ", ".join(part for part in (server.get("location"), server.get("country")) if part)
    return SpeedResult(
        download_mbps=download * 8 / 1e6,
        upload_mbps=upload * 8 / 1e6,
        ping_ms=_num(ping.get("latency")) or 0.0,
        jitter_ms=_num(ping.get("jitter")),
        packet_loss=_num(data.get("packetLoss")),
        isp=str(data.get("isp") or ""),
        server=str(server.get("name") or server.get("host") or ""),
        location=location,
        url=str((data.get("result") or {}).get("url") or ""),
        source="Ookla Speedtest CLI",
    )


def parse_python(data: Dict[str, Any]) -> SpeedResult:
    """``Speedtest.results.dict()`` of the speedtest-cli package (speeds are in bits per second)."""
    download, upload = _num(data.get("download")), _num(data.get("upload"))
    if download is None or upload is None:
        raise SpeedError("speedtest-cli gave no download or upload figure")
    server = data.get("server") or {}
    client = data.get("client") or {}
    location = ", ".join(part for part in (server.get("name"), server.get("country")) if part)
    return SpeedResult(
        download_mbps=download / 1e6,
        upload_mbps=upload / 1e6,
        ping_ms=_num(data.get("ping")) or 0.0,
        isp=str(client.get("isp") or ""),
        server=str(server.get("sponsor") or ""),
        location=location,
        url=str(data.get("share") or ""),
        source="speedtest-cli (Ookla)",
    )


def _last_result(stdout: str) -> Optional[Dict[str, Any]]:
    """The ``{"type": "result", ...}`` line of the CLI's JSON output."""
    found = None
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("type") == "result":
            found = obj
    return found


def _log_message(stdout: str, stderr: str) -> str:
    """Why the CLI failed, as far as it says."""
    for text in (stdout, stderr):
        for line in reversed(text.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict) and obj.get("message"):
                    return str(obj["message"])
            elif line:
                return line
    return "no output"


async def _run(cmd: List[str], timeout: float) -> "tuple[int, str, str]":
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
    except (FileNotFoundError, PermissionError, OSError) as exc:
        raise SpeedError(f"{cmd[0]} cannot be started ({exc})") from exc
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise SpeedError("the speed test took too long") from None
    return proc.returncode or 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


async def find_ookla_cli() -> Optional[str]:
    """The official CLI, if installed.  ``speedtest`` can also be the Python package's script, so ``--version`` decides."""
    names = [os.environ.get("SPEEDTEST_BIN", "")] + list(CANDIDATES)
    for name in dict.fromkeys(n for n in names if n):
        path = shutil.which(name)
        if not path:
            continue
        try:
            code, out, _err = await _run([path, "--version"], 15)
        except SpeedError:
            continue
        if code == 0 and "ookla" in out.lower():
            return path
    return None


async def run_cli(path: str, timeout: float = CLI_TIMEOUT) -> SpeedResult:
    code, out, err = await _run([path, "--accept-license", "--accept-gdpr", "--format=json"], timeout)
    result = _last_result(out)
    if result is None:
        raise SpeedError(f"the Speedtest CLI failed: {_log_message(out, err)}")
    return parse_cli(result)


def _run_python() -> Dict[str, Any]:
    """Blocking: one test with the speedtest-cli package."""
    try:
        import speedtest  # type: ignore
    except ImportError as exc:
        raise SpeedError("the speedtest-cli package is not installed") from exc
    try:
        test = speedtest.Speedtest(secure=True)
        test.get_best_server()
        test.download()
        test.upload()
        return dict(test.results.dict())
    except Exception as exc:  # noqa: BLE001 - the package raises its own family of errors
        raise SpeedError(f"speedtest-cli failed: {exc}") from exc


async def run_speedtest() -> SpeedResult:
    """Run a test with the official CLI, or with the Python client when the CLI is missing or fails."""
    problems: List[str] = []
    cli = await find_ookla_cli()
    if cli is not None:
        try:
            return await run_cli(cli)
        except SpeedError as exc:
            log.warning("Speedtest CLI failed: %s", exc)
            problems.append(str(exc))
    else:
        problems.append("the Speedtest CLI is not installed")
    try:
        data = await asyncio.wait_for(asyncio.to_thread(_run_python), CLI_TIMEOUT)
        return parse_python(data)
    except asyncio.TimeoutError:
        problems.append("speedtest-cli took too long")
    except SpeedError as exc:
        problems.append(str(exc))
    raise SpeedError("; ".join(problems))


def _mbps(value: float) -> str:
    return f"{value:,.2f} Mbps"


def result_link(r: SpeedResult) -> str:
    """The speedtest.net page of the result; "" when there is none or it is not a web link."""
    return r.url if r.url.startswith(("http://", "https://")) else ""


def format_result(r: SpeedResult) -> str:
    lines = [
        "<b>⚡ SPEEDTEST</b> · Ookla",
        "",
        f"◈ <b>Download</b> ▸ <code>{_mbps(r.download_mbps)}</code>",
        f"◈ <b>Upload</b> ▸ <code>{_mbps(r.upload_mbps)}</code>",
        f"◈ <b>Ping</b> ▸ <code>{r.ping_ms:.2f} ms</code>" + (f" · jitter <code>{r.jitter_ms:.2f} ms</code>" if r.jitter_ms is not None else ""),
    ]
    if r.packet_loss is not None:
        lines.append(f"◈ <b>Packet loss</b> ▸ <code>{r.packet_loss:g}%</code>")
    if r.isp:
        lines.append(f"◈ <b>ISP</b> ▸ {esc(r.isp)}")
    server = " · ".join(part for part in (esc(r.server), esc(r.location)) if part)
    if server:
        lines.append(f"◈ <b>Server</b> ▸ {server}")
    link = result_link(r)
    if link:
        lines.append(f"◈ <b>Result</b> ▸ <a href=\"{html.escape(link, quote=True)}\">speedtest.net</a>")
    lines.append(f"\n<i>Measured from the machine the bot runs on · {esc(r.source)}</i>")
    text = "\n".join(lines)
    if link:
        # Telegram previews the FIRST link of a message, and it makes links of words like an ISP called "Amazon.com" too: an invisible link
        # on the very first character makes the result page the one that is previewed.
        text = f"<a href=\"{html.escape(link, quote=True)}\">​</a>{text}"
    return text
