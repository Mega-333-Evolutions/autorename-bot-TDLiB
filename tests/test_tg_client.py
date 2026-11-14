"""The Telegram client (``bot.tg.client``) against a fake TDLib that checks every request against pytdbot's real schema."""
import asyncio
import base64
import io
import logging
from pathlib import Path

import pytest
from PIL import Image

from bot.errors import Cancelled
from bot.tg import Button, Client, Message, errors, events
from bot.tg import types as E

from .tdfakes import (
    FakeTd, SHIFT, document_content, error, remote_id, td_file, td_message, text_content, video_content,
)


@pytest.fixture
def td():
    return FakeTd()


@pytest.fixture
def tg(td, tmp_path):
    return Client(td, tmp_dir=tmp_path / "send")


def jpeg_bytes(size=(320, 180)):
    buf = io.BytesIO()
    Image.new("RGB", size, (10, 120, 200)).save(buf, "JPEG")
    return buf.getvalue()


def leftovers(tg):
    """What the client left in its folder for files it makes while sending (nothing, once the message is sent)."""
    root = Path(tg.tmp_dir)
    return sorted(str(p.relative_to(root)) for p in root.glob("**/*")) if root.exists() else []


def request_of(td, function="sendMessage", index=0):
    return td.calls(function)[index]


def sent_file(request):
    """The InputFile and its holder in a sendMessage request that carries a file."""
    content = request["input_message_content"]
    for key in ("document", "video", "audio", "photo", "animation"):
        if key in content:
            holder = content[key]
            return holder, holder[key]
    raise AssertionError("no file in " + str(content))


# ---------------------------------------------------------------------------
# text
# ---------------------------------------------------------------------------


async def test_a_text_message_goes_out_formatted_and_comes_back_once_telegram_has_it(td, tg):
    buttons = [[Button.inline("A", b"x:1"), Button.url("U", "https://x.io")], [Button.switch_inline("S", "q")]]
    sent = await tg.send_message(5, "<b>Hi</b> there", reply_to=77, buttons=buttons, parse_mode="html")
    request = request_of(td)
    assert request["chat_id"] == 5
    assert request["input_message_content"]["text"]["text"] == "Hi there"
    (entity,) = request["input_message_content"]["text"]["entities"]
    assert (entity["offset"], entity["length"], entity["type"]["@type"]) == (0, 2, "textEntityTypeBold")
    assert request["reply_to"]["message_id"] == 77 << SHIFT
    rows = request["reply_markup"]["rows"]
    assert [[b["type"]["@type"] for b in row] for row in rows] == [
        ["inlineKeyboardButtonTypeCallback", "inlineKeyboardButtonTypeUrl"], ["inlineKeyboardButtonTypeSwitchInline"]]
    assert base64.b64decode(rows[0][0]["type"]["data"]) == b"x:1"
    assert isinstance(sent, Message) and sent.out
    assert sent.td_id % (1 << SHIFT) == 0 and sent.id == sent.td_id >> SHIFT  # Telegram's id (a message still waiting has none)
    assert sent.td_id != td.sent[0]["id"]
    assert sent.raw_text == "Hi there"
    assert "link_preview_options" not in request["input_message_content"]


async def test_the_link_preview_can_be_switched_off(td, tg):
    await tg.send_message(5, "https://x.io", link_preview=False)
    assert request_of(td)["input_message_content"]["link_preview_options"]["is_disabled"] is True


async def test_a_client_made_without_a_parse_mode_sends_text_as_it_is(td, tmp_path):
    plain_client = Client(td, tmp_dir=tmp_path, parse_mode=None)
    await plain_client.send_message(5, "<b>Hi</b>")
    assert request_of(td)["input_message_content"]["text"] == {"@type": "formattedText", "text": "<b>Hi</b>", "entities": []}
    await plain_client.send_message(5, "<b>Hi</b>", parse_mode="html")
    assert request_of(td, index=1)["input_message_content"]["text"]["text"] == "Hi"


async def test_the_default_is_html(td, tg):
    await tg.send_message(5, "<i>x</i>")
    assert request_of(td)["input_message_content"]["text"]["entities"][0]["type"]["@type"] == "textEntityTypeItalic"


async def test_ready_made_entities_are_sent_as_they_are(td, tg):
    await tg.send_message(5, "link here", formatting_entities=[E.MessageEntityTextUrl(0, 4, "https://x.io")])
    (entity,) = request_of(td)["input_message_content"]["text"]["entities"]
    assert entity["type"] == {"@type": "textEntityTypeTextUrl", "url": "https://x.io"}


async def test_a_message_without_buttons_has_no_markup(td, tg):
    await tg.send_message(5, "x", buttons=[])
    assert request_of(td).get("reply_markup") is None


async def test_telegram_refusing_a_message_after_it_was_accepted_raises_the_matching_error(td, tg):
    td.fail_sends = [{"code": 400, "message": "MESSAGE_TOO_LONG"}]
    with pytest.raises(errors.MessageTooLongError):
        await tg.send_message(5, "x")


async def test_telegram_refusing_a_request_raises_the_matching_error(td, tg):
    td.reply("sendMessage", error(400, "Bad Request: BOT_INLINE_DISABLED"))
    with pytest.raises(errors.BotInlineDisabledError):
        await tg.send_message(5, "x", buttons=[[Button.switch_inline("S", "q")]])
    td.reply("sendMessage", error(400, "Bad Request: chat not found"))
    with pytest.raises(errors.RPCError) as caught:
        await tg.send_message(5, "x")
    assert caught.value.code == 400 and "chat not found" in caught.value.message


# ---------------------------------------------------------------------------
# flood waits
# ---------------------------------------------------------------------------


@pytest.fixture
def slept(monkeypatch):
    waits = []
    real = asyncio.sleep

    async def fake(seconds, *args):
        waits.append(seconds)
        await real(0)

    monkeypatch.setattr("bot.tg.client.asyncio.sleep", fake)
    return waits


async def test_a_flood_wait_that_is_short_enough_is_waited_out_and_the_request_repeated(td, tg, slept):
    td.reply("deleteMessages", error(429, "Too Many Requests: retry after 3"), {"@type": "ok"})
    await tg.delete_messages(5, [10])
    assert len(td.calls("deleteMessages")) == 2
    assert slept == [3.5]


async def test_a_long_flood_wait_is_raised(td, tg, slept):
    td.reply("deleteMessages", error(429, "Too Many Requests: retry after 120"))
    with pytest.raises(errors.FloodWaitError) as caught:
        await tg.delete_messages(5, [10])
    assert caught.value.seconds == 120 and slept == []


async def test_waiting_flood_waits_out_gives_up_after_three_tries(td, tg, slept):
    td.reply("deleteMessages", error(429, "Too Many Requests: retry after 1"))
    with pytest.raises(errors.FloodWaitError):
        await tg.delete_messages(5, [10])
    assert len(td.calls("deleteMessages")) == 4 and len(slept) == 3


# ---------------------------------------------------------------------------
# editing, deleting, reading
# ---------------------------------------------------------------------------


async def test_an_edit_changes_the_text_and_the_buttons_and_none_given_removes_them(td, tg):
    await tg.edit_message(5, 77, "<b>x</b>", buttons=[[Button.inline("A", b"a")]], parse_mode="html")
    request = request_of(td, "editMessageText")
    assert (request["chat_id"], request["message_id"]) == (5, 77 << SHIFT)
    assert request["input_message_content"]["text"]["text"] == "x"
    assert request["reply_markup"]["rows"][0][0]["text"] == "A"
    await tg.edit_message(5, 77, "y")
    assert request_of(td, "editMessageText", 1)["reply_markup"] is None  # Telethon's edit without buttons took the keyboard off


async def test_an_edit_can_be_given_the_message_itself(td, tg):
    message = Message(td_message(text_content("old"), chat_id=5, plain_id=77))
    await tg.edit_message(message, "new", parse_mode=None)
    request = request_of(td, "editMessageText")
    assert (request["chat_id"], request["message_id"]) == (5, 77 << SHIFT)
    assert request["input_message_content"]["text"]["text"] == "new"


async def test_an_edit_of_a_file_changes_its_caption_and_remembers_which_kind_of_message_it_is(td, tg):
    td.reply("editMessageText", error(400, "Bad Request: there is no text in the message to edit"))
    await tg.edit_message(5, 77, "<b>caption</b>", parse_mode="html")
    assert request_of(td, "editMessageCaption")["caption"]["text"] == "caption"
    await tg.edit_message(5, 77, "again")
    assert len(td.calls("editMessageText")) == 1 and len(td.calls("editMessageCaption")) == 2  # asked once, then it went straight to the caption


async def test_a_file_we_sent_is_edited_as_a_caption_from_the_start(td, tg, tmp_path):
    path = tmp_path / "a.bin"
    path.write_bytes(b"abc")
    sent = await tg.send_file(5, path, caption="c")
    await tg.edit_message(5, sent.id, "new caption")
    assert td.calls("editMessageText") == [] and len(td.calls("editMessageCaption")) == 1


async def test_a_text_we_sent_is_edited_as_text(td, tg):
    sent = await tg.send_message(5, "x")
    await tg.edit_message(5, sent.id, "y")
    assert len(td.calls("editMessageText")) == 1 and td.calls("editMessageCaption") == []


async def test_other_edit_errors_are_raised_and_not_modified_is_told_apart(td, tg):
    td.reply("editMessageText", error(400, "Bad Request: message is not modified: specified new message content and reply markup are "
                                           "exactly the same as a current content and reply markup of the message"))
    with pytest.raises(errors.MessageNotModifiedError):
        await tg.edit_message(5, 77, "x")
    td.reply("editMessageText", error(400, "Bad Request: message to edit not found"))
    with pytest.raises(errors.RPCError) as caught:
        await tg.edit_message(5, 77, "x")
    assert not isinstance(caught.value, errors.MessageNotModifiedError)


async def test_an_edit_without_text_changes_only_the_buttons(td, tg):
    await tg.edit_message(5, 77, buttons=[[Button.inline("A", b"a")]])
    assert td.calls("editMessageText") == []
    assert request_of(td, "editMessageReplyMarkup")["reply_markup"]["rows"][0][0]["text"] == "A"


async def test_messages_are_deleted_by_one_id_or_a_list(td, tg):
    await tg.delete_messages(5, 10)
    await tg.delete_messages(5, [11, Message(td_message(text_content(), plain_id=12))])
    assert [(r["chat_id"], r["message_ids"], r["revoke"]) for r in td.calls("deleteMessages")] == [
        (5, [10 << SHIFT], True), (5, [11 << SHIFT, 12 << SHIFT], True)]


async def test_messages_are_read_by_id_and_a_missing_one_is_none(td, tg):
    found = td_message(document_content("a.mkv"), chat_id=5, plain_id=10)
    td.reply("getMessages", {"@type": "messages", "total_count": 1, "messages": [found, None]})
    first, second = await tg.get_messages(5, [10, 11])
    assert first.id == 10 and first.file.name == "a.mkv" and second is None
    assert request_of(td, "getMessages")["message_ids"] == [10 << SHIFT, 11 << SHIFT]
    td.reply("getMessages", {"@type": "messages", "total_count": 1, "messages": [found]})
    assert (await tg.get_messages(5, ids=10)).id == 10
    td.reply("getMessages", {"@type": "messages", "total_count": 0, "messages": [None]})
    assert await tg.get_messages(5, ids=10) is None


async def test_a_reply_and_the_message_of_a_button_are_read(td, tg):
    td.reply("getRepliedMessage", td_message(text_content("the reply"), chat_id=5, plain_id=3))
    assert (await tg.get_replied_message(5, 9)).raw_text == "the reply"
    assert request_of(td, "getRepliedMessage")["message_id"] == 9 << SHIFT
    td.reply("getRepliedMessage", error(404, "Not Found"))
    assert await tg.get_replied_message(5, 9) is None
    td.reply("getCallbackQueryMessage", td_message(text_content("panel"), chat_id=5, plain_id=9))
    assert (await tg.get_callback_message(5, 9, 321)).raw_text == "panel"
    assert request_of(td, "getCallbackQueryMessage")["callback_query_id"] == 321
    td.reply("getCallbackQueryMessage", error(400, "Message not found"))
    assert await tg.get_callback_message(5, 9, 321) is None


async def test_forwarding_and_copying_wait_for_the_new_messages(td, tg):
    copy = await tg.forward_messages(7, 10, from_peer=5, drop_author=True)
    request = request_of(td, "forwardMessages")
    assert (request["chat_id"], request["from_chat_id"], request["message_ids"], request["send_copy"]) == (7, 5, [10 << SHIFT], True)
    assert isinstance(copy, Message) and copy.td_id % (1 << SHIFT) == 0  # the message Telegram has, not the pending one
    both = await tg.forward_messages(7, [10, 11], from_peer=5)
    assert len(both) == 2 and request_of(td, "forwardMessages", 1)["send_copy"] is False


async def test_a_forward_without_a_source_chat_takes_it_from_the_message(td, tg):
    await tg.forward_messages(7, Message(td_message(text_content(), chat_id=5, plain_id=10)))
    assert request_of(td, "forwardMessages")["from_chat_id"] == 5


# ---------------------------------------------------------------------------
# files out
# ---------------------------------------------------------------------------


def holding(td, seen):
    """Answer a sendMessage like TDLib does, after noting what its files looked like at that moment (they are removed afterwards)."""

    def reply(request):
        holder, input_file = sent_file(request)
        path = Path(input_file["path"]) if input_file["@type"] == "inputFileLocal" else None
        seen.append({"name": path.name if path else None, "data": path.read_bytes() if path else None,
                     "thumb": (Path(holder["thumbnail"]["thumbnail"]["path"]).read_bytes() if holder.get("thumbnail") else None),
                     "path": path, "holder": holder})
        return td._default(request)

    td.reply("sendMessage", reply)


async def test_a_file_is_sent_as_a_document_under_the_name_it_is_given(td, tg, tmp_path):
    source = tmp_path / "out.bin"
    source.write_bytes(b"movie")
    seen = []
    holding(td, seen)
    sent = await tg.send_file(5, source, caption="<b>c</b>", parse_mode="html", reply_to=3, attributes=[E.DocumentAttributeFilename("New Name.mkv")])
    request = request_of(td)
    assert request["input_message_content"]["@type"] == "inputMessageDocument"
    assert request["input_message_content"]["caption"]["text"] == "c"
    assert request["input_message_content"]["document"]["disable_content_type_detection"] is True
    assert request["reply_to"]["message_id"] == 3 << SHIFT
    assert seen[0]["name"] == "New Name.mkv" and seen[0]["data"] == b"movie"  # TDLib names the file after its path: a link carries the new name
    assert not seen[0]["path"].exists()  # the link is gone once the file is sent ...
    assert source.read_bytes() == b"movie"  # ... and the file itself is not touched
    assert sent.file.name == "New Name.mkv"


async def test_a_file_keeps_its_own_name_when_none_is_given(td, tg, tmp_path):
    source = tmp_path / "own name.txt"
    source.write_bytes(b"x")
    seen = []
    holding(td, seen)
    await tg.send_file(5, str(source))
    assert seen[0]["name"] == "own name.txt" and seen[0]["path"] == source


async def test_names_are_made_safe_and_long_names_keep_their_extension(td, tg, tmp_path):
    source = tmp_path / "a.bin"
    source.write_bytes(b"x")
    seen = []
    holding(td, seen)
    await tg.send_file(5, source, attributes=[E.DocumentAttributeFilename("a/b\\c.mkv")])
    assert seen[0]["name"] == "a_b\\c.mkv"
    await tg.send_file(5, source, attributes=[E.DocumentAttributeFilename("é" * 200 + ".mkv")])
    assert len(seen[1]["name"].encode()) <= 240 and seen[1]["name"].endswith(".mkv")


async def test_a_gif_is_sent_as_an_animation_with_its_details(td, tg, tmp_path):
    source = tmp_path / "start.mp4"
    source.write_bytes(b"x")
    clip = E.DocumentAttributeVideo(duration=3.5, w=480, h=270, supports_streaming=True)
    await tg.send_file(5, source, caption="hi", attributes=[clip, E.DocumentAttributeAnimated()])
    await tg.send_file(5, source, force_document=True, attributes=[clip, E.DocumentAttributeAnimated()])  # asked for as a file: it stays one
    await tg.send_file(5, source, attributes=[clip])  # without the mark it is a video
    content = [r["input_message_content"] for r in td.calls("sendMessage")]
    assert [c["@type"] for c in content] == ["inputMessageAnimation", "inputMessageDocument", "inputMessageVideo"]
    gif = content[0]["animation"]
    assert (gif["duration"], gif["width"], gif["height"]) == (3, 480, 270) and gif["animation"]["@type"] == "inputFileLocal"
    assert content[0]["caption"]["text"] == "hi"


async def test_what_a_file_is_decides_how_it_is_sent(td, tg, tmp_path):
    source = tmp_path / "a.bin"
    source.write_bytes(b"x")
    video = E.DocumentAttributeVideo(duration=61.4, w=1280, h=720, supports_streaming=True)
    audio = E.DocumentAttributeAudio(duration=200, title="T", performer="P")
    await tg.send_file(5, source, attributes=[E.DocumentAttributeFilename("a.mkv"), video])
    await tg.send_file(5, source, attributes=[E.DocumentAttributeFilename("a.mp3"), audio])
    await tg.send_file(5, source, attributes=[E.DocumentAttributeFilename("a.png")])
    await tg.send_file(5, source, attributes=[E.DocumentAttributeFilename("a.JPG")])
    await tg.send_file(5, source, force_document=True, attributes=[E.DocumentAttributeFilename("a.png")])
    await tg.send_file(5, source, force_document=True, attributes=[E.DocumentAttributeFilename("a.mkv"), video])
    await tg.send_file(5, source, attributes=[E.DocumentAttributeFilename("a.mkv"), E.DocumentAttributeVideo(round_message=True)])
    await tg.send_file(5, source, attributes=[E.DocumentAttributeFilename("a.ogg"), E.DocumentAttributeAudio(voice=True)])
    kinds = [r["input_message_content"]["@type"] for r in td.calls("sendMessage")]
    assert kinds == ["inputMessageVideo", "inputMessageAudio", "inputMessagePhoto", "inputMessagePhoto", "inputMessageDocument",
                     "inputMessageDocument", "inputMessageDocument", "inputMessageDocument"]
    clip = request_of(td)["input_message_content"]["video"]
    assert (clip["duration"], clip["width"], clip["height"], clip["supports_streaming"]) == (61, 1280, 720, True)
    song = request_of(td, index=1)["input_message_content"]["audio"]
    assert (song["duration"], song["title"], song["performer"]) == (200, "T", "P")


async def test_a_thumbnail_goes_along_with_its_size_and_is_removed_afterwards(td, tg, tmp_path):
    source = tmp_path / "a.bin"
    source.write_bytes(b"x")
    thumb = jpeg_bytes((320, 180))
    seen = []
    holding(td, seen)
    await tg.send_file(5, source, thumb=thumb, attributes=[E.DocumentAttributeFilename("a.mkv"), E.DocumentAttributeVideo(duration=5, w=320, h=180)])
    assert seen[0]["thumb"] == thumb
    assert (seen[0]["holder"]["thumbnail"]["width"], seen[0]["holder"]["thumbnail"]["height"]) == (320, 180)
    assert leftovers(tg) == []  # nothing is left behind


async def test_bytes_and_file_objects_are_sent_too(td, tg):
    seen = []
    holding(td, seen)
    await tg.send_file(5, b"abc", attributes=[E.DocumentAttributeFilename("a.txt")])
    stream = io.BytesIO(b"def")
    stream.name = "b.txt"
    await tg.send_file(5, stream)
    assert [(s["name"], s["data"]) for s in seen] == [("a.txt", b"abc"), ("b.txt", b"def")]
    assert leftovers(tg) == []


async def test_a_message_given_as_the_file_is_copied_with_the_new_caption(td, tg):
    original = Message(td_message(document_content("a.mkv"), chat_id=5, plain_id=10))
    await tg.send_file(7, original, caption="<b>new</b>", parse_mode="html", buttons=[[Button.inline("A", b"a")]])
    content = request_of(td)["input_message_content"]
    assert content["@type"] == "inputMessageForwarded"
    assert (content["from_chat_id"], content["message_id"]) == (5, 10 << SHIFT)
    assert content["copy_options"]["send_copy"] is True and content["copy_options"]["replace_caption"] is True
    assert content["copy_options"]["new_caption"]["text"] == "new"
    assert request_of(td)["reply_markup"]["rows"][0][0]["text"] == "A"


async def test_a_persistent_file_id_is_sent_as_the_kind_of_file_it_is(td, tg):
    await tg.send_file(5, remote_id(1, 4), caption="v")
    await tg.send_file(5, remote_id(1, 2))
    await tg.send_file(5, remote_id(1, 5))
    await tg.send_file(5, remote_id(1, 9))
    kinds = [r["input_message_content"]["@type"] for r in td.calls("sendMessage")]
    assert kinds == ["inputMessageVideo", "inputMessagePhoto", "inputMessageDocument", "inputMessageAudio"]
    assert sent_file(request_of(td))[1] == {"@type": "inputFileRemote", "id": remote_id(1, 4)}


async def test_upload_progress_is_reported_from_the_file_updates(td, tg, tmp_path):
    td.hold_sends = True
    source = tmp_path / "a.bin"
    source.write_bytes(b"x" * 100)
    heard = []
    job = asyncio.ensure_future(tg.send_file(5, source, progress=lambda done, total: heard.append((done, total))))
    await td.settle()
    file_id = sent_file(request_of(td))[1] and td.sent[0]["content"]["document"]["document"]["id"]
    await td.emit({"@type": "updateFile", "file": td_file(file_id, 100, uploaded=40)})
    await td.emit({"@type": "updateFile", "file": td_file(file_id + 1, 100, uploaded=99)})  # some other file
    await td.emit({"@type": "updateFile", "file": td_file(file_id, 100, uploaded=100)})
    assert heard == [(40, 100), (100, 100)]
    await td.confirm()
    assert (await job).file is not None
    await td.emit({"@type": "updateFile", "file": td_file(file_id, 100, uploaded=100)})
    assert len(heard) == 2  # nobody listens after the file is sent


async def test_a_cancelled_upload_deletes_the_message_that_was_waiting_and_raises_cancelled(td, tg, tmp_path):
    td.hold_sends = True
    source = tmp_path / "a.bin"
    source.write_bytes(b"x" * 100)
    cancel = asyncio.Event()
    job = asyncio.ensure_future(tg.send_file(5, source, cancel=cancel))
    await td.settle()
    cancel.set()
    with pytest.raises(Cancelled):
        await job
    (request,) = td.calls("deleteMessages")
    assert request["message_ids"] == [td.sent[0]["id"]] and request["chat_id"] == 5
    assert leftovers(tg) == []


async def test_an_upload_that_ends_before_the_cancel_is_not_cancelled(td, tg, tmp_path):
    source = tmp_path / "a.bin"
    source.write_bytes(b"x")
    sent = await tg.send_file(5, source, cancel=asyncio.Event())
    assert sent.id and td.calls("deleteMessages") == []


async def test_an_upload_telegram_refuses_raises_the_error_and_cleans_up(td, tg, tmp_path):
    source = tmp_path / "a.bin"
    source.write_bytes(b"x")
    td.fail_sends = [{"code": 400, "message": "MEDIA_CAPTION_TOO_LONG"}]
    with pytest.raises(errors.MediaCaptionTooLongError):
        await tg.send_file(5, source, caption="c", attributes=[E.DocumentAttributeFilename("n.bin")])
    assert leftovers(tg) == []


async def test_the_data_center_the_bot_is_on_is_learnt_from_the_first_file_it_sends(td, tmp_path):
    client = Client(td, tmp_dir=tmp_path)
    assert client.session.dc_id is None
    await client.send_message(5, "text")  # nothing to learn from a text
    assert client.session.dc_id is None
    td.hold_sends = True
    source = tmp_path / "a.bin"
    source.write_bytes(b"x")
    job = asyncio.ensure_future(client.send_file(5, source))
    await td.settle()
    td.pending[0]["content"]["document"]["document"]["remote"]["id"] = remote_id(4)
    await td.confirm()
    await job
    assert client.session.dc_id == 4
    job = asyncio.ensure_future(client.send_file(5, source))
    await td.settle()
    td.pending[0]["content"]["document"]["document"]["remote"]["id"] = remote_id(2)
    await td.confirm()
    await job
    assert client.session.dc_id == 4  # learnt once


async def test_a_data_center_that_was_set_is_kept(td, tmp_path):
    client = Client(td, tmp_dir=tmp_path, bot_dc=2)
    assert client.session.dc_id == 2
    source = tmp_path / "a.bin"
    source.write_bytes(b"x")
    td.hold_sends = True
    job = asyncio.ensure_future(client.send_file(5, source))
    await td.settle()
    td.pending[0]["content"]["document"]["document"]["remote"]["id"] = remote_id(5)
    await td.confirm()
    await job
    assert client.session.dc_id == 2


# ---------------------------------------------------------------------------
# files in
# ---------------------------------------------------------------------------


def incoming(td_path="", size=100, *, completed=False, downloaded=0, name="a.mkv", file_id=7):
    return Message(td_message(document_content(name, size, file_id=file_id, path=str(td_path), completed=completed), chat_id=5, plain_id=10))


async def test_a_downloaded_file_is_moved_to_where_it_is_wanted_and_tdlibs_copy_is_forgotten(td, tg, tmp_path):
    kept = tmp_path / "td" / "file.bin"
    kept.parent.mkdir()
    kept.write_bytes(b"x" * 100)
    td.reply("downloadFile", td_file(7, 100, path=str(kept), completed=True))
    wrote = await tg.download_to(incoming(), tmp_path / "work" / "src.mkv")
    request = request_of(td, "downloadFile")
    assert (request["file_id"], request["offset"], request["limit"], request["synchronous"]) == (7, 0, 0, True)
    assert wrote == 100 and (tmp_path / "work" / "src.mkv").read_bytes() == b"x" * 100 and not kept.exists()
    assert request_of(td, "deleteFile")["file_id"] == 7


async def test_a_file_tdlib_already_has_is_not_downloaded_again(td, tg, tmp_path):
    kept = tmp_path / "file.bin"
    kept.write_bytes(b"y" * 10)
    message = incoming(kept, 10, completed=True)
    assert await tg.fetch_file(message.file.td_file) == str(kept)
    assert td.requests == []


async def test_download_progress_is_reported_from_the_file_updates(td, tg, tmp_path):
    kept = tmp_path / "file.bin"
    kept.write_bytes(b"x" * 100)
    heard = []

    async def download(request):
        await td.emit({"@type": "updateFile", "file": td_file(7, 100, downloaded=25)})
        await td.emit({"@type": "updateFile", "file": td_file(8, 100, downloaded=50)})  # another file
        await td.emit({"@type": "updateFile", "file": td_file(7, 100, downloaded=100)})
        return td_file(7, 100, path=str(kept), completed=True)

    td.reply("downloadFile", download)
    await tg.download_to(incoming(), tmp_path / "src", progress=lambda done, total: heard.append((done, total)))
    assert heard == [(25, 100), (100, 100)]
    assert tg._watchers == {}


async def test_a_cancelled_download_is_stopped_and_raises_cancelled(td, tg, tmp_path):
    never = asyncio.Event()

    async def download(request):
        await never.wait()

    td.reply("downloadFile", download)
    cancel = asyncio.Event()
    job = asyncio.ensure_future(tg.download_to(incoming(), tmp_path / "src", cancel=cancel))
    await td.settle()
    cancel.set()
    with pytest.raises(Cancelled):
        await job
    assert request_of(td, "cancelDownloadFile")["file_id"] == 7
    assert tg._watchers == {}


async def test_a_download_that_did_not_finish_is_an_error(td, tg, tmp_path):
    td.reply("downloadFile", td_file(7, 100, path=str(tmp_path / "half"), downloaded=50))
    with pytest.raises(errors.RPCError):
        await tg.download_to(incoming(), tmp_path / "src")
    td.reply("downloadFile", error(400, "FILE_REFERENCE_EXPIRED"))
    with pytest.raises(errors.RPCError):
        await tg.download_to(incoming(), tmp_path / "src")


async def test_the_start_of_a_file_can_be_downloaded_alone(td, tg, tmp_path):
    kept = tmp_path / "td.bin"
    kept.write_bytes(bytes(range(100)))
    td.reply("downloadFile", td_file(7, 100, path=str(kept), downloaded=40))  # not complete: 40 bytes are there
    wrote = await tg.download_to(incoming(), tmp_path / "head", max_bytes=30)
    assert request_of(td, "downloadFile")["limit"] == 30
    assert wrote == 30 and (tmp_path / "head").read_bytes() == bytes(range(30))
    assert kept.exists() and request_of(td, "deleteFile")["file_id"] == 7  # TDLib is told to forget its copy


async def test_asking_for_more_than_the_file_has_downloads_all_of_it(td, tg, tmp_path):
    kept = tmp_path / "td.bin"
    kept.write_bytes(b"z" * 100)
    td.reply("downloadFile", td_file(7, 100, path=str(kept), completed=True))
    assert await tg.download_to(incoming(), tmp_path / "all", max_bytes=500) == 100
    assert (tmp_path / "all").read_bytes() == b"z" * 100


async def test_a_start_that_did_not_arrive_is_an_error(td, tg, tmp_path):
    td.reply("downloadFile", td_file(7, 100, path=str(tmp_path / "td.bin"), downloaded=10))
    with pytest.raises(errors.RPCError):
        await tg.download_to(incoming(), tmp_path / "head", max_bytes=30)


async def test_a_message_without_a_file_cannot_be_downloaded(td, tg, tmp_path):
    with pytest.raises(errors.RPCError):
        await tg.download_to(Message(td_message(text_content("x"))), tmp_path / "x")


async def test_media_is_given_as_bytes_or_saved(td, tg, tmp_path):
    kept = tmp_path / "td.bin"
    kept.write_bytes(b"data")
    td.reply("downloadFile", td_file(7, 4, path=str(kept), completed=True))
    message = incoming(size=4)
    assert await tg.download_media(message, file=bytes) == b"data"
    assert request_of(td, "deleteFile")["file_id"] == 7
    target = tmp_path / "saved.bin"
    assert await tg.download_media(message, file=str(target)) == str(target) and target.read_bytes() == b"data"
    assert await tg.download_media(Message(td_message(text_content("x"))), file=bytes) is None


async def test_the_preview_of_a_file_is_its_thumbnail_or_else_the_tiny_one(td, tg, tmp_path):
    kept = tmp_path / "thumb.jpg"
    kept.write_bytes(b"jpeg!")
    thumbnail = {"@type": "thumbnail", "format": {"@type": "thumbnailFormatJpeg"}, "width": 90, "height": 60,
                 "file": td_file(11, 5)}  # not downloaded yet
    td.reply("downloadFile", td_file(11, 5, path=str(kept), completed=True))
    with_thumb = Message(td_message(video_content(thumbnail=thumbnail), chat_id=5, plain_id=10))
    assert await tg.download_media(with_thumb, file=bytes, thumb=-1) == b"jpeg!"
    assert request_of(td, "downloadFile")["file_id"] == 11
    mini = base64.b64encode(b"tiny").decode()
    only_mini = Message(td_message(video_content(minithumbnail=mini), chat_id=5, plain_id=10))
    assert await tg.download_media(only_mini, file=bytes, thumb=-1) == b"tiny"
    assert len(td.calls("downloadFile")) == 1  # the tiny preview comes with the message: nothing to download
    assert await tg.download_media(Message(td_message(video_content(), chat_id=5, plain_id=10)), file=bytes, thumb=-1) is None


# ---------------------------------------------------------------------------
# answers, commands, the bot itself
# ---------------------------------------------------------------------------


async def test_answers_to_buttons_and_inline_queries_and_the_command_list(td, tg):
    await tg.answer_callback(321, "Done", alert=True)
    request = request_of(td, "answerCallbackQuery")
    assert (request["callback_query_id"], request["text"], request["show_alert"]) == (321, "Done", True)
    await tg.answer_callback(322)
    assert request_of(td, "answerCallbackQuery", 1)["text"] == ""
    builder = events.InlineBuilder(tg)
    card = await builder.article("Title", "Description", text="<b>Body</b>", parse_mode="html", buttons=[[Button.switch_inline("S", "q")]])
    await tg.answer_inline(77, [card], cache_time=5, private=True)
    request = request_of(td, "answerInlineQuery")
    assert (request["inline_query_id"], request["is_personal"], request["cache_time"]) == (77, True, 5)
    assert request["results"][0]["input_message_content"]["text"]["text"] == "Body" and request["results"][0]["title"] == "Title"
    await tg.set_bot_commands([E.BotCommand("start", "Start"), E.BotCommand("help", "Help")])
    request = request_of(td, "setCommands")
    assert [(c["command"], c["description"]) for c in request["commands"]] == [("start", "Start"), ("help", "Help")]
    assert request["scope"] == {"@type": "botCommandScopeDefault"} and request["language_code"] == ""


async def test_the_bot_knows_itself_and_whether_inline_mode_is_on(td, tg):
    me = await tg.get_me()
    assert (me.id, me.username, me.first_name, me.bot_inline_placeholder) == (999, "RenameBot", "Rename Bot", "Pick a format")
    await tg.get_me()
    assert len(td.calls("getMe")) == 1  # asked once
    off = FakeTd()
    reply = (await off.invoke({"@type": "getMe"}))
    reply = {"@type": "user", "id": 1, "first_name": "B", "usernames": {"@type": "usernames", "editable_username": "b", "active_usernames": [],
             "disabled_usernames": []}, "type": {"@type": "userTypeBot", "is_inline": False, "inline_query_placeholder": ""}}
    off.reply("getMe", reply)
    assert (await Client(off).get_me()).bot_inline_placeholder is None


async def test_the_chat_of_a_message_must_be_known_to_telegram_is_checked_with_getchat(td, tg):
    await tg.get_input_entity(-1001234567890)
    assert request_of(td, "getChat")["chat_id"] == -1001234567890
    td.reply("getChat", error(400, "Chat not found"))
    with pytest.raises(errors.RPCError):
        await tg.get_input_entity(-1001234567890)


async def test_ping_measures_a_round_trip_in_milliseconds(td, tg):
    assert 0 <= await tg.ping() < 1000
    assert len(td.calls("getCommands")) == 1
    assert request_of(td, "getCommands")["scope"] == {"@type": "botCommandScopeDefault"}


async def test_ping_does_not_use_testnetwork_which_telegram_refuses_for_a_bot(td, tg):
    with pytest.raises(errors.RPCError, match="BOT_METHOD_INVALID"):  # what the real thing does, and why /status showed "n/a"
        await tg.call({"@type": "testNetwork"})
    await tg.ping()
    assert [r["@type"] for r in td.requests].count("testNetwork") == 1  # only the probe above


async def test_a_request_goes_on_when_the_one_waiting_for_it_is_cancelled(td, tg):
    """pytdbot's future for the answer dies with the waiter, and the answer TDLib still sends then fails there (``InvalidStateError``,
    reported as ``Task exception was never retrieved``).  The request has to finish with nobody listening."""
    answered, release = asyncio.Event(), asyncio.Event()

    async def slow(_request):
        await release.wait()  # cancelled here when the waiter takes the request with it
        answered.set()
        return {"@type": "ok"}

    td.reply("getCommands", slow)
    waiter = asyncio.ensure_future(tg.ping())
    await asyncio.sleep(0.01)
    assert len(td.calls("getCommands")) == 1
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    release.set()
    await asyncio.wait_for(answered.wait(), 1)  # the request ran to its end


async def test_a_request_that_fails_after_its_waiter_was_cancelled_is_not_reported_as_unretrieved(td, tg):
    import gc

    release, seen = asyncio.Event(), []
    asyncio.get_running_loop().set_exception_handler(lambda _loop, context: seen.append(context))

    async def failing(_request):
        await release.wait()
        raise ValueError("Failed to parse JSON object as TDLib request")

    td.reply("getCommands", failing)
    waiter = asyncio.ensure_future(tg.ping())
    await asyncio.sleep(0.01)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    release.set()
    for _ in range(5):
        await asyncio.sleep(0)
    gc.collect()
    await asyncio.sleep(0)
    assert seen == []  # asyncio's "Task exception was never retrieved" would be in here


async def test_the_late_answer_to_a_cancelled_request_does_not_fail_inside_pytdbot(tmp_path):
    """The reported traceback, with pytdbot's own client: ``process_update`` -> ``result.set_result(...)`` -> ``InvalidStateError``."""
    from types import SimpleNamespace

    from pytdbot import Client as Pytdbot

    sent = []
    real = Pytdbot(token="999:AAHexample", api_id=1, api_hash="0" * 32, database_encryption_key="key", files_directory=str(tmp_path))
    real.client_manager = SimpleNamespace(send=lambda _client_id, request: sent.append(request))  # nothing is sent to TDLib here
    real.client_id = 1
    tg = Client(real, tmp_dir=tmp_path / "send")

    waiter = asyncio.ensure_future(tg.ping())
    await asyncio.sleep(0.01)
    assert [r["@type"] for r in sent] == ["getCommands"]
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    # TDLib answers all the same
    await real.process_update({"@type": "botCommands", "bot_user_id": 999, "commands": [], "@extra": sent[0]["@extra"]})
    assert real._results == {}


async def test_a_failed_ping_is_not_swallowed_by_the_client(td, tg):
    td.reply("getCommands", error(500, "Internal"))
    with pytest.raises(errors.RPCError):
        await tg.ping()


async def test_start_run_and_disconnect_drive_the_tdlib_client(td, tg):
    assert await tg.start() is tg and td.started
    runner = asyncio.ensure_future(tg.run_until_disconnected())
    await td.settle()
    assert not runner.done()
    await tg.disconnect()
    await asyncio.wait_for(runner, 1)
    assert td.stopped


# ---------------------------------------------------------------------------
# updates and handlers
# ---------------------------------------------------------------------------


def new_message(text="/start", *, chat=5, sender=5, plain_id=10, out=False):
    return {"@type": "updateNewMessage", "message": td_message(text_content(text), chat_id=chat, plain_id=plain_id, sender=sender, out=out)}


def callback_update(data=b"job:x:1", *, chat=5, sender=5, message=10, query=321):
    return {"@type": "updateNewCallbackQuery", "id": query, "sender_user_id": sender, "chat_id": chat, "message_id": message << SHIFT,
            "chat_instance": 1, "payload": {"@type": "callbackQueryPayloadData", "data": base64.b64encode(data).decode()}}


async def test_every_matching_handler_runs_in_order_until_one_stops_the_rest(td, tg):
    order = []

    async def first(event):
        order.append("first")

    async def second(event):
        order.append("second")
        raise events.StopPropagation

    async def third(event):
        order.append("third")

    for handler in (first, second, third):
        tg.add_event_handler(handler, events.NewMessage(incoming=True))
    await td.emit(new_message())
    await td.settle()
    assert order == ["first", "second"]


async def test_a_handler_that_fails_is_logged_and_the_next_ones_still_run(td, tg, caplog):
    seen = []

    async def broken(event):
        raise RuntimeError("boom")

    async def fine(event):
        seen.append(event.raw_text)

    tg.add_event_handler(broken, events.NewMessage())
    tg.add_event_handler(fine, events.NewMessage())
    with caplog.at_level(logging.ERROR):
        await td.emit(new_message("hello"))
        await td.settle()
    assert seen == ["hello"]
    assert any("boom" in str(r.exc_info[1]) for r in caplog.records if r.exc_info)


async def test_each_update_is_handled_on_its_own_so_a_slow_handler_blocks_nothing(td, tg):
    release = asyncio.Event()
    done = []

    async def slow(event):
        await release.wait()
        done.append(event.raw_text)

    tg.add_event_handler(slow, events.NewMessage())
    await td.emit(new_message("one", plain_id=1))
    await td.emit(new_message("two", plain_id=2))  # the first one is still going: this is not held up
    await td.settle()
    assert done == []
    release.set()
    await td.settle()
    assert sorted(done) == ["one", "two"]


async def test_message_filters_pick_the_updates_a_handler_wants(td, tg):
    got = {}

    def record(name):
        async def handler(event):
            got.setdefault(name, []).append(event.raw_text)
        return handler

    tg.add_event_handler(record("all"), events.NewMessage())
    tg.add_event_handler(record("incoming"), events.NewMessage(incoming=True))
    tg.add_event_handler(record("outgoing"), events.NewMessage(outgoing=True))
    tg.add_event_handler(record("pattern"), events.NewMessage(pattern=r"/rename\b"))
    tg.add_event_handler(record("func"), events.NewMessage(func=lambda e: e.is_private))
    tg.add_event_handler(record("chats"), events.NewMessage(chats=[-100123]))
    tg.add_event_handler(record("users"), events.NewMessage(from_users=[42]))
    await td.emit(new_message("/rename x", chat=5, sender=5))
    await td.emit(new_message("hi", chat=-100123, sender=42))
    await td.emit(new_message("echo", chat=5, sender=999, out=True))
    await td.settle()
    assert got == {
        "all": ["/rename x", "hi", "echo"], "incoming": ["/rename x", "hi"], "outgoing": ["echo"], "pattern": ["/rename x"],
        "func": ["/rename x", "echo"], "chats": ["hi"], "users": ["hi"],
    }


async def test_a_message_event_can_reply_respond_edit_delete_and_download(td, tg, tmp_path):
    results = []

    async def handler(event):
        assert (event.chat_id, event.sender_id, event.id, event.is_private) == (5, 5, 10, True)
        results.append(await event.reply("<b>r</b>", parse_mode="html"))
        results.append(await event.respond("plain", buttons=[[Button.inline("A", b"a")]]))
        await event.edit("edited")
        await event.delete()

    tg.add_event_handler(handler, events.NewMessage())
    await td.emit(new_message("hi"))
    await td.settle()
    first, second = td.calls("sendMessage")
    assert first["reply_to"]["message_id"] == 10 << SHIFT and first["input_message_content"]["text"]["text"] == "r"
    assert second.get("reply_to") is None and second["reply_markup"] is not None
    assert request_of(td, "editMessageText")["message_id"] == 10 << SHIFT
    assert request_of(td, "deleteMessages")["message_ids"] == [10 << SHIFT]
    assert len(results) == 2


async def test_a_message_event_can_reply_with_a_file(td, tg, tmp_path):
    source = tmp_path / "x.txt"
    source.write_bytes(b"x")

    async def handler(event):
        await event.respond("caption", file=str(source))

    tg.add_event_handler(handler, events.NewMessage())
    await td.emit(new_message("hi"))
    await td.settle()
    assert request_of(td)["input_message_content"]["caption"]["text"] == "caption"


async def test_the_message_a_message_replies_to_is_fetched_on_demand(td, tg):
    seen = []

    async def handler(event):
        seen.append((await event.get_reply_message()))

    tg.add_event_handler(handler, events.NewMessage())
    td.reply("getRepliedMessage", td_message(document_content("a.mkv"), chat_id=5, plain_id=3))
    replying = td_message(text_content("/dc"), chat_id=5, plain_id=10, reply_to=3)
    await td.emit({"@type": "updateNewMessage", "message": replying})
    await td.emit(new_message("no reply", plain_id=11))
    await td.settle()
    assert [m.file.name if m else None for m in seen] == ["a.mkv", None] or [m.file.name if m else None for m in seen] == [None, "a.mkv"]
    assert len(td.calls("getRepliedMessage")) == 1


async def test_a_button_press_reaches_the_handler_with_its_data_and_can_be_answered_and_edited(td, tg):
    seen = []

    async def handler(event):
        seen.append((event.data, event.sender_id, event.chat_id, event.message_id, event.query_id))
        await event.answer("Done", alert=True)
        await event.edit("<b>new</b>", parse_mode="html", buttons=[[Button.inline("B", b"b")]])
        await event.respond("more")
        await event.reply("re")
        message = await event.get_message()
        seen.append(message.raw_text)
        await event.delete()

    tg.add_event_handler(handler, events.CallbackQuery())
    tg.add_event_handler(lambda e: seen.append("other"), events.CallbackQuery(data=b"nope"))
    td.reply("getCallbackQueryMessage", td_message(text_content("panel"), chat_id=5, plain_id=10))
    await td.emit(callback_update(b"job:x:1"))
    await td.settle()
    assert seen[0] == (b"job:x:1", 5, 5, 10, 321) and seen[1] == "panel"
    answer = request_of(td, "answerCallbackQuery")
    assert (answer["callback_query_id"], answer["text"], answer["show_alert"]) == (321, "Done", True)
    assert request_of(td, "editMessageText")["message_id"] == 10 << SHIFT
    assert td.calls("sendMessage")[1]["reply_to"]["message_id"] == 10 << SHIFT
    assert request_of(td, "deleteMessages")["message_ids"] == [10 << SHIFT]


async def test_a_callback_filter_can_pick_by_data_or_function(td, tg):
    got = []

    async def by_data(event):
        got.append(("data", event.data))

    async def by_func(event):
        got.append(("func", event.data))

    tg.add_event_handler(by_data, events.CallbackQuery(data=b"a:1"))
    tg.add_event_handler(by_func, events.CallbackQuery(func=lambda e: e.data.startswith(b"b:")))
    for data in (b"a:1", b"a:2", b"b:9"):
        await td.emit(callback_update(data))
    await td.settle()
    assert sorted(got) == [("data", b"a:1"), ("func", b"b:9")]


async def test_an_inline_query_is_answered_with_cards_the_builder_makes(td, tg):
    async def handler(event):
        card = await event.builder.article("Rename", "a card", text="<b>/format</b>", parse_mode="html")
        await event.answer([card], cache_time=0, private=True)

    tg.add_event_handler(handler, events.InlineQuery())
    await td.emit({"@type": "updateNewInlineQuery", "id": 77, "sender_user_id": 5, "user_location": None, "chat_type": None,
                   "query": "fmt", "offset": ""})
    await td.settle()
    request = request_of(td, "answerInlineQuery")
    assert request["inline_query_id"] == 77 and request["results"][0]["title"] == "Rename"
    assert request["results"][0]["input_message_content"]["text"]["entities"][0]["type"]["@type"] == "textEntityTypeBold"


async def test_kinds_of_updates_do_not_mix(td, tg):
    got = []

    async def on_message(event):
        got.append("message")

    async def on_callback(event):
        got.append("callback")

    tg.add_event_handler(on_message, events.NewMessage())
    tg.add_event_handler(on_callback, events.CallbackQuery())
    await td.emit(callback_update())
    await td.settle()
    assert got == ["callback"]


async def test_a_broken_filter_is_logged_and_skipped(td, tg, caplog):
    got = []

    async def handler(event):
        got.append(1)

    tg.add_event_handler(handler, events.NewMessage(func=lambda e: 1 / 0))
    tg.add_event_handler(handler, events.NewMessage())
    with caplog.at_level(logging.ERROR):
        await td.emit(new_message())
        await td.settle()
    assert got == [1] and any("filter" in r.getMessage() for r in caplog.records)


async def test_disconnecting_stops_the_handlers_that_are_still_running(td, tg):
    never = asyncio.Event()
    started = []

    async def handler(event):
        started.append(1)
        await never.wait()

    tg.add_event_handler(handler, events.NewMessage())
    await td.emit(new_message())
    await td.settle()
    assert started == [1] and tg._tasks
    await tg.disconnect()
    await td.settle()
    assert not tg._tasks


# ---------------------------------------------------------------------------
# generated files (used by the streaming rename)
# ---------------------------------------------------------------------------


async def test_tdlib_asking_for_a_file_to_be_made_starts_the_matching_maker_and_unknown_ones_are_refused(td, tg):
    started = []

    async def maker(update):
        started.append(update["generation_id"])

    tg.generators["relay"] = maker
    await td.emit({"@type": "updateFileGenerationStart", "generation_id": "5", "original_path": "/a", "destination_path": "/b",
                   "conversion": "relay"})
    await td.settle()
    assert started == [5]  # pytdbot hands the id over as the number it is
    await td.emit({"@type": "updateFileGenerationStart", "generation_id": "6", "original_path": "/a", "destination_path": "/b",
                   "conversion": "other"})
    await td.settle()
    request = request_of(td, "finishFileGeneration")
    assert request["generation_id"] == 6 and request["error"]["message"]


# ---------------------------------------------------------------------------
# against the real pytdbot
# ---------------------------------------------------------------------------


def test_the_handlers_register_on_a_real_pytdbot_client(tmp_path):
    import pytdbot

    real = pytdbot.Client(api_id=1, api_hash="0123456789abcdef0123456789abcdef", token="1:abc", files_directory=str(tmp_path / "td"),
                          database_encryption_key="", use_file_database=False, use_chat_info_database=True, use_message_database=False)
    Client(real)
    assert {"updateNewMessage", "updateNewCallbackQuery", "updateNewInlineQuery", "updateFile", "updateMessageSendSucceeded",
            "updateMessageSendFailed", "updateFileGenerationStart", "updateFileGenerationStop"} <= set(real._handlers)


def test_errors_from_tdlib_become_the_exceptions_the_handlers_catch():
    assert isinstance(errors.from_td(429, "Too Many Requests: retry after 7"), errors.FloodWaitError)
    assert errors.from_td(429, "Too Many Requests: retry after 7").seconds == 7
    assert isinstance(errors.from_td(420, "FLOOD_WAIT_9"), errors.FloodWaitError)
    assert isinstance(errors.from_td(400, "Bad Request: message is not modified"), errors.MessageNotModifiedError)
    assert isinstance(errors.from_td(400, "MESSAGE_TOO_LONG"), errors.MessageTooLongError)
    assert isinstance(errors.from_td(400, "Message caption is too long"), errors.MediaCaptionTooLongError)
    assert isinstance(errors.from_td(400, "ENTITY_BOUNDS_INVALID"), errors.EntityBoundsInvalidError)
    assert isinstance(errors.from_td(400, "Can't parse entities: wrong bounds"), errors.EntityBoundsInvalidError)
    assert isinstance(errors.from_td(400, "BOT_INLINE_DISABLED"), errors.BotInlineDisabledError)
    plain_error = errors.from_td(400, "Bad Request: something else", "sendMessage")
    assert type(plain_error) is errors.RPCError and plain_error.request == "sendMessage" and "something else" in str(plain_error)
    assert not isinstance(errors.from_td(0, ""), errors.FloodWaitError)
