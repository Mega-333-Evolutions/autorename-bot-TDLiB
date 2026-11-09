"""A local stand-in for api.telegra.ph (createAccount and createPage), used by the Telegraph and /mediainfo tests."""
import asyncio

from aiohttp import web
from aiohttp.test_utils import TestServer


class FakeTelegraph:
    """api.telegra.ph for tests: createAccount and createPage, with switches for the ways it can go wrong."""

    def __init__(self):
        self.accounts = 0
        self.valid = set()
        self.attempts = []  # every createPage form, accepted or not
        self.pages = []  # the accepted ones
        self.error = None  # answer every createPage with this error
        self.reject_all = False  # a token is never good enough
        self.delay = 0.0
        self.raw = None  # (status, body) to answer createPage with instead of JSON

    async def create_account(self, request):
        form = await request.post()
        self.accounts += 1
        token = f"token{self.accounts}"
        self.valid.add(token)
        return web.json_response({"ok": True, "result": {"short_name": form["short_name"], "access_token": token}})

    async def create_page(self, request):
        form = dict(await request.post())
        self.attempts.append(form)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.raw is not None:
            return web.Response(status=self.raw[0], text=self.raw[1], content_type="text/html")
        if self.error:
            return web.json_response({"ok": False, "error": self.error})
        if self.reject_all or form.get("access_token") not in self.valid:
            return web.json_response({"ok": False, "error": "ACCESS_TOKEN_INVALID"})
        self.pages.append(form)
        n = len(self.pages)
        return web.json_response({"ok": True, "result": {"path": f"Page-{n}", "url": f"https://telegra.ph/Page-{n}", "title": form["title"]}})


async def serve_fake_telegraph() -> "FakeTelegraph":
    """Start the fake server; ``fake.url`` is the API address and ``await fake.server.close()`` stops it."""
    fake = FakeTelegraph()
    app = web.Application()
    app.router.add_post("/createAccount", fake.create_account)
    app.router.add_post("/createPage", fake.create_page)
    fake.server = TestServer(app)
    await fake.server.start_server()
    fake.url = str(fake.server.make_url("")).rstrip("/")
    return fake
