"""A minimal Telegraph (telegra.ph) client: one account for the bot, pages created from node lists."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable, Dict, List, Optional

import aiohttp

log = logging.getLogger(__name__)

API = "https://api.telegra.ph"
CONTENT_LIMIT = 60_000  # Telegraph refuses a page whose content is over 64 KB; stay clear of it
TITLE_LIMIT = 256

Node = Dict[str, Any]


class TelegraphError(RuntimeError):
    """Telegraph refused or could not be reached; the message is safe to show to the user."""

    def __init__(self, message: str, code: str = ""):
        super().__init__(message)
        self.code = code


def content_size(nodes: List[Node]) -> int:
    """Bytes the content takes as it is sent."""
    return len(dump(nodes).encode("utf-8"))


def dump(nodes: List[Node]) -> str:
    return json.dumps(nodes, ensure_ascii=False, separators=(",", ":"))


class Telegraph:
    """Creates pages under one account.  The access token comes from ``load_token`` and is saved with ``save_token``."""

    def __init__(
        self,
        load_token: Callable[[], Awaitable[Optional[str]]],
        save_token: Callable[[str], Awaitable[None]],
        *,
        api: str = API,
        short_name: str = "AutoRename",
        author: str = "Auto Rename Bot",
        timeout: float = 30,
    ):
        self._load = load_token
        self._save = save_token
        self.api = api.rstrip("/")
        self.short_name = short_name
        self.author = author
        self.author_url = ""
        self.timeout = timeout
        self._token: Optional[str] = None
        self._lock = asyncio.Lock()

    def set_author(self, name: str, url: str = "") -> None:
        """The name (and link) shown under the title of the pages; the bot's own, once it is known."""
        if name.strip():
            self.author = name.strip()[:128]
            self.author_url = url.strip()[:512]

    async def _call(self, method: str, **params: str) -> Dict[str, Any]:
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout)) as session:
                async with session.post(f"{self.api}/{method}", data=params) as resp:
                    data = await resp.json(content_type=None)
        except asyncio.TimeoutError:
            raise TelegraphError("Telegraph took too long to answer") from None
        except (aiohttp.ClientError, ValueError) as exc:
            raise TelegraphError(f"I couldn't reach Telegraph ({type(exc).__name__})") from exc
        if not isinstance(data, dict) or not data.get("ok"):
            code = str(data.get("error") if isinstance(data, dict) else "") or "UNKNOWN"
            if code.startswith("FLOOD_WAIT_"):
                raise TelegraphError(f"Telegraph asks to wait {code.rsplit('_', 1)[-1]} seconds", code)
            raise TelegraphError(f"Telegraph said {code}", code)
        result = data.get("result")
        if not isinstance(result, dict):
            raise TelegraphError("Telegraph sent an answer I don't understand")
        return result

    async def token(self) -> str:
        """The account's access token: remembered, else loaded, else a new account is made."""
        async with self._lock:
            if self._token:
                return self._token
            saved = await self._load()
            if saved:
                self._token = saved
                return saved
            result = await self._call("createAccount", short_name=self.short_name, author_name=self.author)
            token = str(result.get("access_token") or "")
            if not token:
                raise TelegraphError("Telegraph did not give an access token")
            self._token = token
            await self._save(token)
            return token

    async def _forget_token(self) -> None:
        async with self._lock:
            self._token = None
            await self._save("")

    async def create_page(self, title: str, nodes: List[Node]) -> str:
        """Create a page and return its URL."""
        content = dump(nodes)
        if len(content.encode("utf-8")) > 64 * 1024:
            raise TelegraphError("The page is too big for Telegraph", "CONTENT_TOO_BIG")
        for attempt in range(2):
            token = await self.token()
            params = {"access_token": token, "title": (title.strip() or "MediaInfo")[:TITLE_LIMIT],
                      "author_name": self.author, "content": content, "return_content": "false"}
            if self.author_url:
                params["author_url"] = self.author_url
            try:
                result = await self._call("createPage", **params)
            except TelegraphError as exc:
                if exc.code == "ACCESS_TOKEN_INVALID" and attempt == 0:
                    log.warning("the saved Telegraph token was rejected; making a new account")
                    await self._forget_token()
                    continue
                raise
            url = str(result.get("url") or "")
            if not url:
                raise TelegraphError("Telegraph did not return the page address")
            return url
        raise TelegraphError("Telegraph rejected the account twice")  # pragma: no cover - the loop returns or raises
