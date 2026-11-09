"""/speed: the command around the speed test."""
import asyncio

from bot import app as appmod
from bot import speed
from bot.speed import SpeedResult

from .appkit import ADMIN_CFG, OWNER, STRANGER, say

RESULT = SpeedResult(
    download_mbps=250.0, upload_mbps=100.0, ping_ms=4.2, jitter_ms=0.5, packet_loss=0.0, isp="Hetzner",
    server="Example Telecom", location="Frankfurt, Germany", url="https://www.speedtest.net/result/c/abc", source="Ookla Speedtest CLI",
)


def serve(monkeypatch, result=RESULT, error=None, gate=None):
    calls = []

    async def fake():
        calls.append(1)
        if gate is not None:
            await gate.wait()
        if error is not None:
            raise error
        return result

    monkeypatch.setattr(speed, "run_speedtest", fake)
    return calls


async def test_speed_shows_a_status_and_then_the_figures(ctx, monkeypatch):
    calls = serve(monkeypatch)
    ev = await say(ctx, OWNER, "/speed")
    assert ev.texts == ["⚡ Running the speed test… this takes about half a minute."]
    (status, text, kw), = ctx.client.edited
    assert status.id == 1001 and kw["parse_mode"] == "html" and kw["link_preview"] is True  # Telegram shows the result page under the figures
    assert "SPEEDTEST" in text and "250.00 Mbps" in text and "100.00 Mbps" in text and "speedtest.net/result/c/abc" in text
    assert len(calls) == 1 and "may be lower" not in text


async def test_a_result_without_a_page_gets_no_preview(ctx, monkeypatch):
    serve(monkeypatch, result=SpeedResult(download_mbps=1.0, upload_mbps=1.0, ping_ms=1.0, url=""))
    await say(ctx, OWNER, "/speed")
    assert ctx.client.edited[-1][2]["link_preview"] is False


async def test_a_failed_test_says_why(ctx, monkeypatch):
    serve(monkeypatch, error=speed.SpeedError("the Speedtest CLI is not installed; the speedtest-cli package is not installed <x>"))
    await say(ctx, OWNER, "/speed")
    text, kw = ctx.client.edited[-1][1:]
    assert text.startswith("❌ I couldn't run the speed test: the Speedtest CLI is not installed") and "&lt;x&gt;" in text
    assert kw["link_preview"] is False  # an error message is never previewed, whatever it quotes


async def test_an_unexpected_crash_is_reported_and_does_not_leave_the_lock_taken(ctx, monkeypatch):
    serve(monkeypatch, error=RuntimeError("boom"))
    await say(ctx, OWNER, "/speed")
    assert "failed unexpectedly" in ctx.client.edited[-1][1]
    serve(monkeypatch)
    await say(ctx, OWNER, "/speed")
    assert "250.00 Mbps" in ctx.client.edited[-1][1]


async def test_only_one_test_runs_at_a_time(ctx, monkeypatch):
    gate = asyncio.Event()
    calls = serve(monkeypatch, gate=gate)
    first = asyncio.ensure_future(say(ctx, OWNER, "/speed"))
    await asyncio.sleep(0.05)
    second = await say(ctx, ADMIN_CFG, "/speed")
    assert "already running" in second.texts[0] and len(calls) == 1
    gate.set()
    await first
    assert "250.00 Mbps" in ctx.client.edited[-1][1]
    again = await say(ctx, ADMIN_CFG, "/speed")  # free again
    assert again.texts[0].startswith("⚡")


async def test_the_result_mentions_a_running_job(ctx, monkeypatch):
    serve(monkeypatch)
    await say(ctx, OWNER, "/speed")
    assert "A rename job was running" not in ctx.client.edited[-1][1]
    monkeypatch.setattr(type(ctx.app.queue), "running", property(lambda self: 1))
    await say(ctx, OWNER, "/speed")
    assert "A rename job was running during the test" in ctx.client.edited[-1][1]


async def test_strangers_cannot_run_it_and_it_is_in_the_menu(ctx, monkeypatch):
    calls = serve(monkeypatch)
    assert (await say(ctx, STRANGER, "/speed")).out == [] and calls == []
    assert "speed" in dict(appmod.COMMANDS)
