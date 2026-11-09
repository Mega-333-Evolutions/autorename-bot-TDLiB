"""The Telegraph client, against a local server that answers like api.telegra.ph."""
import asyncio
import json

import pytest

from bot import telegraph
from bot.telegraph import Telegraph, TelegraphError

from .faketelegraph import serve_fake_telegraph

NODES = [{"tag": "h4", "children": ["General"]}, {"tag": "pre", "children": ["Format : Matroska"]}]


@pytest.fixture
async def server():
    fake = await serve_fake_telegraph()
    yield fake
    await fake.server.close()


def make(server, store=None, **kw):
    store = {} if store is None else store

    async def load():
        return store.get("token")

    async def save(token):
        store["token"] = token

    return Telegraph(load, save, api=server.url, **kw), store


# ---------------------------------------------------------------------------


async def test_the_first_page_makes_an_account_and_remembers_its_token(server):
    tg, store = make(server)
    url = await tg.create_page("MediaInfo – Episode 01.mkv", NODES)
    assert url == "https://telegra.ph/Page-1"
    assert server.accounts == 1 and store == {"token": "token1"}
    form = server.pages[0]
    assert form["access_token"] == "token1" and form["title"] == "MediaInfo – Episode 01.mkv"
    assert form["author_name"] == "Auto Rename Bot" and form["return_content"] == "false" and "author_url" not in form
    assert json.loads(form["content"]) == NODES


async def test_later_pages_use_the_same_account(server):
    tg, store = make(server)
    await tg.create_page("one", NODES)
    await tg.create_page("two", NODES)
    assert server.accounts == 1 and [p["access_token"] for p in server.pages] == ["token1", "token1"]


async def test_a_token_saved_by_an_earlier_run_is_used_without_a_new_account(server):
    server.valid.add("kept")
    tg, store = make(server, {"token": "kept"})
    await tg.create_page("x", NODES)
    assert server.accounts == 0 and server.pages[0]["access_token"] == "kept"


async def test_a_rejected_token_is_replaced_once_and_the_page_still_gets_made(server):
    tg, store = make(server, {"token": "stale"})
    url = await tg.create_page("x", NODES)
    assert url == "https://telegra.ph/Page-1"
    assert server.accounts == 1 and store == {"token": "token1"}
    assert [a["access_token"] for a in server.attempts] == ["stale", "token1"]


async def test_a_token_that_is_rejected_again_is_an_error(server):
    server.reject_all = True
    tg, store = make(server, {"token": "stale"})
    with pytest.raises(TelegraphError) as err:
        await tg.create_page("x", NODES)
    assert err.value.code == "ACCESS_TOKEN_INVALID" and server.accounts == 1 and len(server.attempts) == 2


async def test_the_first_use_from_several_places_makes_one_account(server):
    tg, _store = make(server)
    urls = await asyncio.gather(*(tg.create_page(f"p{i}", NODES) for i in range(6)))
    assert server.accounts == 1 and len(set(urls)) == 6


async def test_a_flood_wait_says_how_long(server):
    server.error = "FLOOD_WAIT_7"
    tg, _store = make(server)
    with pytest.raises(TelegraphError) as err:
        await tg.create_page("x", NODES)
    assert str(err.value) == "Telegraph asks to wait 7 seconds" and err.value.code == "FLOOD_WAIT_7"


async def test_other_errors_keep_their_code(server):
    server.error = "PAGE_SAVE_FAILED"
    tg, _store = make(server)
    with pytest.raises(TelegraphError) as err:
        await tg.create_page("x", NODES)
    assert str(err.value) == "Telegraph said PAGE_SAVE_FAILED" and err.value.code == "PAGE_SAVE_FAILED"


async def test_a_page_over_the_size_limit_is_not_even_sent(server):
    tg, _store = make(server)
    big = [{"tag": "pre", "children": ["x" * 70_000]}]
    with pytest.raises(TelegraphError) as err:
        await tg.create_page("x", big)
    assert err.value.code == "CONTENT_TOO_BIG" and server.attempts == [] and server.accounts == 0


async def test_an_answer_that_is_not_json_is_an_error(server):
    server.raw = (502, "<html>Bad gateway</html>")
    tg, _store = make(server)
    with pytest.raises(TelegraphError) as err:
        await tg.create_page("x", NODES)
    assert "couldn't reach Telegraph" in str(err.value)


async def test_a_slow_server_times_out(server):
    server.delay = 1.0
    tg, _store = make(server, timeout=0.2)
    with pytest.raises(TelegraphError) as err:
        await tg.create_page("x", NODES)
    assert "too long" in str(err.value)


async def test_an_unreachable_server_is_an_error():
    async def load():
        return "token"

    async def save(token):
        pass

    tg = Telegraph(load, save, api="http://127.0.0.1:9", timeout=2)  # nothing listens on the discard port
    with pytest.raises(TelegraphError) as err:
        await tg.create_page("x", NODES)
    assert "couldn't reach Telegraph" in str(err.value)


async def test_the_author_and_its_link_are_sent_and_kept_within_telegraphs_limits(server):
    tg, _store = make(server)
    tg.set_author("Anime Rename Bot", "https://t.me/rename_bot")
    await tg.create_page("x", NODES)
    assert server.pages[0]["author_name"] == "Anime Rename Bot" and server.pages[0]["author_url"] == "https://t.me/rename_bot"
    tg.set_author("N" * 300)
    tg.set_author("   ")  # a blank name changes nothing
    await tg.create_page("y", NODES)
    assert server.pages[1]["author_name"] == "N" * 128 and "author_url" not in server.pages[1]  # no link was given this time


async def test_titles_are_never_empty_or_too_long(server):
    tg, _store = make(server)
    await tg.create_page("   ", NODES)
    await tg.create_page("T" * 400, NODES)
    assert server.pages[0]["title"] == "MediaInfo" and len(server.pages[1]["title"]) == 256


def test_content_size_counts_bytes_not_characters():
    nodes = [{"tag": "p", "children": ["日本語"]}]
    assert telegraph.content_size(nodes) == len(json.dumps(nodes, ensure_ascii=False, separators=(",", ":")).encode()) > len("日本語")
