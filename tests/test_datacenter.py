"""Data centers: which one a file is stored on and which one the bot is on, and /dc, which tells both."""
from types import SimpleNamespace

import pytest

from bot import app as appmod
from bot import datacenter, panels
from bot.tg import Message

from .appkit import OWNER, STRANGER, say
from .tdfakes import document_content, photo_content, remote_id, td_message, text_content, video_content
from .tgfakes import FakeMessage, document_message

HINT = "Reply to a file, video or picture with /dc to see which data center it is stored on."


def file_on(dc_id, **kw):
    """A message with a file that Telegram keeps on this data center."""
    message = document_message("Episode 05.mkv", **kw)
    message.document.dc_id = dc_id
    return message


def photo_on(dc_id):
    message = FakeMessage("", photo=True)
    message.photo = SimpleNamespace(dc_id=dc_id)
    return message


# ---------------------------------------------------------------------------
# the helpers
# ---------------------------------------------------------------------------


def test_the_dc_of_a_message_is_the_one_its_file_or_photo_is_stored_on():
    assert datacenter.of_message(SimpleNamespace(document=SimpleNamespace(dc_id=4), photo=None)) == 4
    assert datacenter.of_message(SimpleNamespace(document=None, photo=SimpleNamespace(dc_id=2))) == 2


@pytest.mark.parametrize("message", [
    SimpleNamespace(document=None, photo=None),  # no file at all
    SimpleNamespace(),  # not even those attributes
    SimpleNamespace(document=SimpleNamespace(), photo=None),  # a file Telegram gave no DC for
    SimpleNamespace(document=SimpleNamespace(dc_id=0), photo=None),  # 0 is "none", not a data center
    SimpleNamespace(document=SimpleNamespace(dc_id="4"), photo=None),  # only a whole number is one
])
def test_a_message_whose_dc_is_not_known_has_none(message):
    assert datacenter.of_message(message) is None


def test_the_dc_is_read_from_real_tdlib_messages():
    """The fakes above make up their attributes; these are built from messages the way TDLib hands them over: the data center is in the
    file's persistent id."""
    def read(content):
        return Message(td_message(content))

    assert datacenter.of_message(read(document_content(remote_id=remote_id(4)))) == 4
    assert datacenter.of_message(read(video_content(remote_id=remote_id(5, kind=4)))) == 5
    assert datacenter.of_message(read(photo_content(remote_id=remote_id(2, kind=2)))) == 2
    assert datacenter.of_message(read(document_content(remote_id=""))) is None  # a file that has no id yet (it is still being sent)
    assert datacenter.of_message(read(document_content(remote_id="not an id"))) is None
    assert datacenter.of_message(read(text_content("just words"))) is None
    assert datacenter.of_message(read({"@type": "messageLocation"})) is None


@pytest.mark.parametrize("persistent_id, dc", [
    ("BQACAgEAAxkBAAIB", 1), ("BQACAgIAAxkBAAIB", 2), ("BQACAgMAAxkBAAIB", 3), ("BQACAgQAAxkBAAIB", 4), ("BQACAgUAAxkBAAIB", 5),
])
def test_the_dc_is_in_the_ids_telegram_really_hands_out(persistent_id, dc):
    """Ids of the shape every bot gets for a document (``BQACAg`` + the data center + ``AAxkBAAI``)."""
    assert datacenter.of_message(Message(td_message(document_content(remote_id=persistent_id)))) == dc


def test_the_dc_of_the_bot_is_the_one_its_session_is_on():
    assert datacenter.home_of(SimpleNamespace(session=SimpleNamespace(dc_id=2))) == 2
    for client in (SimpleNamespace(session=SimpleNamespace(dc_id=None)), SimpleNamespace(session=None), SimpleNamespace()):
        assert datacenter.home_of(client) is None


def test_a_dc_is_named_by_its_number_and_where_it_is():
    assert datacenter.label(4) == "DC 4"
    assert [datacenter.label(n, place=True) for n in range(1, 6)] == [
        "DC 1 · Miami", "DC 2 · Amsterdam", "DC 3 · Miami", "DC 4 · Amsterdam", "DC 5 · Singapore",
    ]
    assert datacenter.label(9, place=True) == "DC 9"  # one that is not among the five has no known place


# ---------------------------------------------------------------------------
# /dc
# ---------------------------------------------------------------------------


async def test_dc_tells_where_a_file_is_stored_and_where_the_bot_is(ctx):
    ev = await say(ctx, OWNER, "/dc", reply=file_on(4))
    (text, _buttons, kw), = ev.out
    assert text == "<b>📡 Data center</b>\n◈ <b>File</b> ▸ DC 4 · Amsterdam\n◈ <b>Bot</b> ▸ DC 1 · Miami"  # the fake bot's session is on DC 1
    assert kw["parse_mode"] == "html"


async def test_dc_reads_a_photo_too(ctx):
    ev = await say(ctx, OWNER, "/dc", reply=photo_on(5))
    assert ev.texts == ["<b>📡 Data center</b>\n◈ <b>File</b> ▸ DC 5 · Singapore\n◈ <b>Bot</b> ▸ DC 1 · Miami"]


async def test_dc_names_the_dc_the_bot_is_really_on(ctx):
    ctx.client.session.dc_id = 4
    ev = await say(ctx, OWNER, "/dc", reply=file_on(2))
    assert ev.texts == ["<b>📡 Data center</b>\n◈ <b>File</b> ▸ DC 2 · Amsterdam\n◈ <b>Bot</b> ▸ DC 4 · Amsterdam"]


async def test_dc_leaves_out_the_bot_when_its_dc_is_not_known(ctx):
    ctx.client.session.dc_id = None
    ev = await say(ctx, OWNER, "/dc", reply=file_on(4))
    assert ev.texts == ["<b>📡 Data center</b>\n◈ <b>File</b> ▸ DC 4 · Amsterdam"]


async def test_dc_without_a_reply_or_without_media_says_what_to_do(ctx):
    assert (await say(ctx, OWNER, "/dc")).texts == [HINT]
    assert (await say(ctx, OWNER, "/dc", reply=FakeMessage("just words"))).texts == [HINT]


async def test_dc_of_media_that_is_not_a_file_says_so(ctx):
    poll = FakeMessage("")
    poll.media = object()  # something with media but no file or photo: a poll, a location ...
    assert (await say(ctx, OWNER, "/dc", reply=poll)).texts == ["❌ There's no file in that."]
    assert (await say(ctx, OWNER, "/dc", reply=file_on(None))).texts == ["❌ There's no file in that."]  # Telegram gave no DC


async def test_strangers_cannot_use_dc_and_it_is_in_the_menu_and_the_help(ctx):
    assert (await say(ctx, STRANGER, "/dc", reply=file_on(4))).out == []
    assert "dc" in dict(appmod.COMMANDS)
    assert "/dc" in panels.help_panel()[0]
