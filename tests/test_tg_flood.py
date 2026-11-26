"""The Telegram client with a ``FloodControl``: what it tells it about Telegram's limits, and what it waits for."""
import asyncio

import pytest

from bot import flood as flood_module
from bot.flood import FloodControl
from bot.tg import Client, Message, errors
from bot.tg import types as E

from .tdfakes import FakeTd, document_content, error, td_file, td_message
from .timeline import Timeline

STOPPED = errors.DOWNLOAD_STOPPED
OK = {"@type": "ok"}


def flood_wait(seconds):
    return error(429, f"Too Many Requests: retry after {seconds}")


@pytest.fixture
def td():
    return FakeTd()


@pytest.fixture
def line(monkeypatch):
    line = Timeline()
    monkeypatch.setattr("bot.tg.client.asyncio.sleep", line.sleep)  # whatever the client waits for, the clock of the flood control moves on
    return line


@pytest.fixture
def flood(line):
    return FloodControl(clock=line.clock, sleep=line.sleep, jitter=lambda: 0.0)


@pytest.fixture
def tg(td, tmp_path, flood):
    return Client(td, tmp_dir=tmp_path / "send", flood=flood)


def incoming(size=100):
    return Message(td_message(document_content("a.mkv", size, file_id=7, path="", completed=False), chat_id=5, plain_id=10))


def again(td):
    return lambda request: td._default(request)  # the answer a request gets when nothing else was set


# ---------------------------------------------------------------------------
# flood waits of requests
# ---------------------------------------------------------------------------


async def test_a_flood_wait_met_by_a_request_is_told_to_the_flood_control_and_waited_out(td, tg, flood, line):
    td.reply("deleteMessages", flood_wait(3), OK)
    await tg.delete_messages(5, [10])
    assert len(td.calls("deleteMessages")) == 2 and flood.hits == 1 and line.waited() == [3.5]
    assert (flood.uploads.size, flood.downloads.size) == (1, 2)  # a short one is the chat's limit, not the connection's


async def test_a_request_also_waits_for_a_flood_that_is_longer_than_its_own(td, tg, flood, line):
    flood.hit(20)
    td.reply("deleteMessages", flood_wait(3), OK)
    await tg.delete_messages(5, [10])
    assert line.waited() == [20.5]


async def test_a_long_flood_wait_is_raised_and_still_holds_back_the_rest_for_a_while(td, tg, flood, line):
    td.reply("deleteMessages", flood_wait(120))
    with pytest.raises(errors.FloodWaitError):
        await tg.delete_messages(5, [10])
    assert line.waited() == [] and flood.hits == 1 and flood.cooling_left() == flood_module.MAX_COOL


async def test_without_a_flood_control_nothing_is_held_back_for_the_flood(td, tmp_path, line):
    tg = Client(td, tmp_dir=tmp_path / "send")
    td.reply("deleteMessages", flood_wait(3), OK)
    await tg.delete_messages(5, [10])
    assert line.waited() == [3.5] and tg.flood is None


# ---------------------------------------------------------------------------
# edits
# ---------------------------------------------------------------------------


async def test_a_progress_edit_is_not_waited_out_but_it_does_close_the_gate(td, tg, flood, line):
    td.reply("editMessageText", flood_wait(3))
    with pytest.raises(errors.FloodWaitError):
        await tg.edit_message(5, 77, "x", quiet=True)
    assert len(td.calls("editMessageText")) == 1 and line.waited() == []
    assert flood.cooling_left() == pytest.approx(3.5)


async def test_a_progress_edit_without_a_flood_control_is_not_waited_out_either(td, tmp_path, line):
    tg = Client(td, tmp_dir=tmp_path / "send")
    td.reply("editMessageText", flood_wait(3))
    with pytest.raises(errors.FloodWaitError):
        await tg.edit_message(5, 77, "x", quiet=True)
    assert len(td.calls("editMessageText")) == 1 and line.waited() == []


async def test_the_buttons_and_the_caption_of_a_progress_edit_are_not_waited_out_either(td, tg, flood, line):
    td.reply("editMessageReplyMarkup", flood_wait(3))
    with pytest.raises(errors.FloodWaitError):
        await tg.edit_message(5, 77, None, quiet=True)
    td.reply("editMessageText", error(400, "Bad Request: there is no text in the message to edit"))
    td.reply("editMessageCaption", flood_wait(3))
    with pytest.raises(errors.FloodWaitError):
        await tg.edit_message(6, 78, "x", quiet=True)  # (another chat, so that this does not wait for its turn)
    assert len(td.calls("editMessageReplyMarkup")) == 1 and len(td.calls("editMessageCaption")) == 1 and line.waited() == []


async def test_an_edit_that_has_to_get_through_is_waited_out_and_repeated(td, tg, flood, line):
    td.reply("editMessageText", flood_wait(3), again(td))
    await tg.edit_message(5, 77, "x", paced=True)
    assert len(td.calls("editMessageText")) == 2 and line.waited() == [3.5]


async def test_an_edit_that_has_to_get_through_waits_for_the_flood_to_be_over_first(td, tg, flood, line):
    flood.hit(3)
    await tg.edit_message(5, 77, "x", paced=True)
    assert len(td.calls("editMessageText")) == 1 and line.waited() == [3.5]


async def test_a_progress_edit_waits_for_its_turn_in_the_chat_but_not_for_the_flood(td, tg, flood, line):
    flood.hit(3)
    await tg.edit_message(5, 77, "x", quiet=True)
    await tg.edit_message(5, 77, "y", quiet=True)
    assert len(td.calls("editMessageText")) == 2 and line.waited() == pytest.approx([flood_module.EDIT_GAP])


async def test_edits_in_two_chats_do_not_wait_for_each_other(td, tg, flood, line):
    await tg.edit_message(5, 77, "x", quiet=True)
    await tg.edit_message(6, 77, "y", quiet=True)
    assert line.waited() == []


async def test_an_edit_that_is_neither_quiet_nor_paced_is_not_held_back(td, tg, flood, line):
    flood.hit(3)
    await tg.edit_message(5, 77, "x")
    await tg.edit_message(5, 77, "y")
    assert len(td.calls("editMessageText")) == 2 and line.waited() == []


async def test_the_kinds_of_edit_are_accepted_by_a_client_without_a_flood_control_too(td, tmp_path, line):
    tg = Client(td, tmp_dir=tmp_path / "send")
    await tg.edit_message(5, 77, "x", quiet=True)
    await tg.edit_message(5, 77, "y", paced=True)
    assert len(td.calls("editMessageText")) == 2 and line.waited() == []


# ---------------------------------------------------------------------------
# sending
# ---------------------------------------------------------------------------


async def test_a_file_that_meets_a_flood_wait_on_its_way_out_lowers_the_limits_even_when_it_is_short(td, tg, tmp_path, flood, line):
    source = tmp_path / "a.bin"
    source.write_bytes(b"x" * 100)
    td.fail_sends = [{"code": 400, "message": "Too Many Requests: retry after 3"}]
    sent = await tg.send_file(5, source, attributes=[E.DocumentAttributeFilename("n.bin")], cancel=asyncio.Event())
    assert sent.id and line.waited() == [3.5] and flood.hits == 1
    assert flood.downloads.size == 1


async def test_a_text_that_meets_a_short_flood_wait_leaves_the_limits_alone(td, tg, flood, line):
    td.fail_sends = [{"code": 400, "message": "Too Many Requests: retry after 3"}]
    sent = await tg.send_message(5, "x")
    assert sent.id and line.waited() == [3.5] and flood.hits == 1
    assert (flood.uploads.size, flood.downloads.size) == (1, 2)


async def test_a_text_that_meets_a_long_flood_wait_lowers_them(td, tg, flood, line):
    td.fail_sends = [{"code": 400, "message": "Too Many Requests: retry after 8"}]
    await tg.send_message(5, "x")
    assert line.waited() == [8.5] and flood.downloads.size == 1


async def test_a_send_waits_for_a_flood_that_is_longer_than_its_own(td, tg, flood, line):
    flood.hit(20)
    td.fail_sends = [{"code": 400, "message": "Too Many Requests: retry after 3"}]
    await tg.send_message(5, "x")
    assert line.waited() == [20.5]


async def test_a_send_without_a_flood_control_waits_for_its_own_flood_only(td, tmp_path, line):
    tg = Client(td, tmp_dir=tmp_path / "send")
    td.fail_sends = [{"code": 400, "message": "Too Many Requests: retry after 3"}]
    await tg.send_message(5, "x")
    assert line.waited() == [3.5]


# ---------------------------------------------------------------------------
# downloads
# ---------------------------------------------------------------------------


async def test_a_download_that_stops_short_is_a_flood_to_the_flood_control_and_waits_for_it(td, tg, tmp_path, flood, line):
    kept = tmp_path / "td" / "file.bin"
    kept.parent.mkdir()
    kept.write_bytes(b"x" * 100)
    td.reply("downloadFile", error(400, STOPPED), error(400, STOPPED), error(400, STOPPED), td_file(7, 100, path=str(kept), completed=True))
    await tg.download_to(incoming(), tmp_path / "work" / "src.mkv")
    # the first pause is as long as the flood is (8 s and a little), then the pauses of a download that gets nowhere
    assert line.waited() == [8.5, 15.0, 30.0] and flood.hits == 3
    assert flood.downloads.size == 1  # and fewer downloads run at once from then on


async def test_a_download_that_stops_short_waits_for_a_flood_that_is_longer_than_its_pause(td, tg, tmp_path, flood, line):
    kept = tmp_path / "td" / "file.bin"
    kept.parent.mkdir()
    kept.write_bytes(b"x" * 100)
    flood.hit(30)
    td.reply("downloadFile", error(400, STOPPED), td_file(7, 100, path=str(kept), completed=True))
    await tg.download_to(incoming(), tmp_path / "work" / "src.mkv")
    assert line.waited() == [30.0]


async def test_without_a_flood_control_a_download_that_stops_short_waits_its_pause_only(td, tmp_path, line):
    tg = Client(td, tmp_dir=tmp_path / "send")
    kept = tmp_path / "td" / "file.bin"
    kept.parent.mkdir()
    kept.write_bytes(b"x" * 100)
    td.reply("downloadFile", error(400, STOPPED), td_file(7, 100, path=str(kept), completed=True))
    await tg.download_to(incoming(), tmp_path / "work" / "src.mkv")
    assert line.waited() == [5.0]


# ---------------------------------------------------------------------------
# which failures are only Telegram being busy
# ---------------------------------------------------------------------------


def test_a_flood_wait_and_a_download_that_kept_stopping_short_are_only_telegram_being_busy():
    assert errors.is_busy(errors.FloodWaitError(5))
    assert errors.is_busy(errors.from_td(429, "Too Many Requests: retry after 7"))
    assert errors.is_busy(errors.RPCError(STOPPED, 400))
    assert errors.is_busy(errors.RPCError(STOPPED + " (stopped short 8 times)", 400))


def test_other_errors_are_not():
    assert not errors.is_busy(errors.RPCError("FILE_REFERENCE_EXPIRED", 400))
    assert not errors.is_busy(errors.MessageTooLongError("MESSAGE_TOO_LONG", 400))
    assert not errors.is_busy(ValueError(STOPPED))
    assert not errors.is_busy(errors.TransferCancelled())
