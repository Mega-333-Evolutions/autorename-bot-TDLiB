"""Tiny health-check server.

A Hugging Face Docker Space expects something listening on its app port; your
cron pinger can hit ``/`` or ``/health`` to keep the Space awake.
"""
from __future__ import annotations

import logging

from aiohttp import web

from . import __version__, sysinfo
from .utils import human_uptime

log = logging.getLogger(__name__)


def make_app() -> web.Application:
    async def index(_request: web.Request) -> web.Response:
        return web.Response(text=f"Auto Rename Bot v{__version__} is running ✅\nuptime: {human_uptime(sysinfo.uptime())}\n")

    async def health(_request: web.Request) -> web.Response:
        return web.json_response({"status": "ok", "version": __version__, "uptime_seconds": int(sysinfo.uptime())})

    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/health", health)
    return app


async def start_web(port: int, host: str = "0.0.0.0") -> web.AppRunner:
    runner = web.AppRunner(make_app(), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    log.info("health server listening on %s:%s", host, port)
    return runner
