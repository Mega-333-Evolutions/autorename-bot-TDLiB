"""``transfer.Relay`` (the streamed rename) against a TDLib that behaves as it is documented to: it downloads a file in parts, and it uploads
a file the bot generates only as far as the bot says that file is ready.

None of this was run against Telegram itself: that is what the feature being experimental (and off by default) is about."""
import asyncio
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from bot import transfer
from bot.errors import Cancelled
from bot.tg import Client, Generated, Message, errors, types

from .tdfakes import FakeTd, document_content, td_file, td_message

NAME = "Episode 05.mkv"
NEW_NAME = "[S01E05] Episode.mkv"
FILE_ID = 7


class StreamTd(FakeTd):
    """A TDLib that delivers ``data`` as a download in ``parts`` (``delay`` apart, the way a connection would) and that uploads a
    generated file as far as the bot reports it is ready.  Whatever the bot does that TDLib's documentation does not allow is put
    in ``violations`` (the file holds fewer bytes than it was told, a different file at the end, a count that goes back ...)."""

    def __init__(self, tmp: Path, data: bytes, *, parts: int = 4, delay: float = 0.0, quiet: bool = False, generation_delay: float = 0.0):
        super().__init__()
        self.tmp, self.data, self.parts, self.delay = tmp, data, parts, delay
        self.quiet = quiet  # no ``updateFile`` at all while the download goes on: the bot has to ask how far it is
        self.generation_delay = generation_delay  # how long TDLib takes to ask the bot for the file it was told to upload
        self.have, self.path, self.active, self.final = 0, "", False, False
        self.dies_at = None  # the download stops for good after this many bytes
        self.generations = {}
        self.violations = []
        self.progress = []  # (generation, expected_size, local_prefix_size) as reported
        self.finished = {}  # generation -> the error it was finished with (None: it went well)
        self.stopped_generations = []

    # ------------------------------------------------------------------ the download
    def file(self):
        done = self.final
        item = td_file(FILE_ID, len(self.data), remote_id="BQACAgEAAxkBAAI7", path=self.path, completed=done, downloaded=self.have)
        item["local"]["is_downloading_active"] = self.active and not done
        return item

    def arrived(self):
        """The whole file is in TDLib's folder (a download that was done before)."""
        folder = self.tmp / "documents"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "file.mkv").write_bytes(self.data)
        self.path, self.have, self.final = str(folder / "file.mkv"), len(self.data), True

    async def _download(self):
        step = -(-len(self.data) // self.parts)
        folder = self.tmp / "partial"
        folder.mkdir(parents=True, exist_ok=True)
        partial = folder / "file.part"
        self.path = str(partial)
        with open(partial, "wb") as out:
            while self.have < len(self.data):
                if self.dies_at is not None and self.have >= self.dies_at:
                    self.active = False
                    await self.emit({"@type": "updateFile", "file": self.file()})  # and nothing more comes
                    return
                await asyncio.sleep(self.delay)
                chunk = self.data[self.have:self.have + step]
                out.write(chunk)
                out.flush()
                self.have += len(chunk)
                if not self.quiet:
                    await self.emit({"@type": "updateFile", "file": self.file()})
        done = self.tmp / "documents"
        done.mkdir(parents=True, exist_ok=True)
        os.replace(partial, done / "file.mkv")  # a finished download is moved to its place
        self.path, self.active, self.final = str(done / "file.mkv"), False, True
        if not self.quiet:
            await self.emit({"@type": "updateFile", "file": self.file()})

    # ------------------------------------------------------------------ the upload of a generated file
    def _generated(self, request):
        content = request["input_message_content"]
        for part in content.values():
            if isinstance(part, dict):
                for inner in part.values():
                    if isinstance(inner, dict) and inner.get("@type") == "inputFileGenerated":
                        return inner
        return None

    async def start_generation(self, pending, generated):
        generation = 9000 + self._next()
        destination = self.tmp / "generated" / str(generation) / os.path.basename(generated["original_path"])
        destination.parent.mkdir(parents=True, exist_ok=True)
        self.generations[generation] = SimpleNamespace(destination=destination, pending=pending, generated=generated)
        await asyncio.sleep(self.generation_delay)
        await self.emit({"@type": "updateFileGenerationStart", "generation_id": generation, "original_path": generated["original_path"],
                         "destination_path": str(destination), "conversion": generated["conversion"]})
        return generation

    def _progress(self, request):
        generation = int(request["generation_id"])
        known = self.generations.get(generation)
        prefix = int(request["local_prefix_size"])
        if known is None:
            self.violations.append(f"progress for generation {generation}, which TDLib never asked for")
            return
        held = os.path.getsize(known.destination)
        if prefix > held:
            self.violations.append(f"{prefix} bytes reported ready, the file holds {held}")
        elif known.destination.read_bytes()[:prefix] != self.data[:prefix]:
            self.violations.append("the bytes reported ready are not the start of the original")
        last = [p for g, _e, p in self.progress if g == generation]
        if last and prefix < last[-1]:
            self.violations.append(f"the count went back from {last[-1]} to {prefix}")
        if int(request["expected_size"]) != len(self.data):
            self.violations.append(f"expected size {request['expected_size']}, the file is {len(self.data)}")
        self.progress.append((generation, int(request["expected_size"]), prefix))

    def _finish(self, request):
        generation = int(request["generation_id"])
        known = self.generations[generation]
        error = request.get("error")
        self.finished[generation] = error
        if error is None:
            if known.destination.read_bytes() != self.data:
                self.violations.append("the finished file is not the original")
            self._tasks.append(asyncio.ensure_future(self.confirm(known.pending)))
        else:
            self._tasks.append(asyncio.ensure_future(self.fail(known.pending, 400, "Bad Request: FILE_GENERATE_FAILED")))

    def _delete(self, request):
        for generation, known in self.generations.items():
            if known.pending["id"] in request["message_ids"] and generation not in self.finished:
                self.stopped_generations.append(generation)
                self._tasks.append(asyncio.ensure_future(self.emit({"@type": "updateFileGenerationStop", "generation_id": generation})))

    # ------------------------------------------------------------------ the answers
    def _default(self, request):
        kind = request["@type"]
        if kind == "downloadFile":
            if not self.active and not self.final and self.have < len(self.data):
                self.active = True
                self._tasks.append(asyncio.ensure_future(self._download()))
            return self.file()
        if kind == "getFile":
            return self.file()
        if kind == "setFileGenerationProgress":
            self._progress(request)
            return {"@type": "ok"}
        if kind == "finishFileGeneration":
            self._finish(request)
            return {"@type": "ok"}
        if kind == "deleteMessages":
            self._delete(request)
            return {"@type": "ok"}
        if kind == "sendMessage":
            generated = self._generated(request)
            if generated is not None:
                pending = self._pending(request)
                self._tasks.append(asyncio.ensure_future(self.start_generation(pending, generated)))
                return pending
        return super()._default(request)

    async def wait_for(self, condition, seconds=5.0):
        for _ in range(int(seconds / 0.005)):
            if condition():
                return
            await asyncio.sleep(0.005)
        raise AssertionError("it did not happen in time")


@pytest.fixture(autouse=True)
def report_every_part(monkeypatch):
    monkeypatch.setattr(transfer, "REPORT_EVERY", 0.0)  # TDLib hears of every part, whatever the pace (one test checks the pauses)


def make(tmp_path, size=600_000, **kwargs):
    data = os.urandom(size)
    td = StreamTd(tmp_path / "td", data, **kwargs)
    client = Client(td, tmp_dir=tmp_path / "send")
    message = Message(td_message(document_content(NAME, size, file_id=FILE_ID, remote_id="BQACAgEAAxkBAAI7")))
    return data, td, client, message


def attrs():
    return [types.DocumentAttributeFilename(NEW_NAME)]


async def send(client, relay, **kwargs):
    return await client.send_file(1, relay.source, caption="cap", attributes=attrs(), cancel=relay.cancel, **kwargs)


# --------------------------------------------------------------------------- what the client sends for a generated file
async def test_a_generated_file_is_sent_as_the_input_file_it_is(tmp_path):
    td = FakeTd()
    client = Client(td, tmp_dir=tmp_path)
    await client.send_file(1, Generated("[S01E05] Episode.mkv", "relay:abc", 123456), attributes=attrs(), force_document=True)
    (request,) = td.calls("sendMessage")
    document = request["input_message_content"]["document"]["document"]
    assert document["@type"] == "inputFileGenerated" and document["conversion"] == "relay:abc" and document["expected_size"] == 123456
    assert os.path.basename(document["original_path"]) == "[S01E05] Episode.mkv"  # TDLib names the upload after this path


async def test_a_generated_video_is_sent_as_a_video(tmp_path):
    td = FakeTd()
    client = Client(td, tmp_dir=tmp_path)
    video = types.DocumentAttributeVideo(duration=60, w=1280, h=720, supports_streaming=True)
    await client.send_file(1, Generated("A.mkv", "relay:v", 9), attributes=[types.DocumentAttributeFilename("A.mkv"), video])
    content = td.calls("sendMessage")[0]["input_message_content"]
    assert content["@type"] == "inputMessageVideo" and content["video"]["video"]["@type"] == "inputFileGenerated"
    assert (content["video"]["duration"], content["video"]["width"], content["video"]["height"]) == (60, 1280, 720)


# --------------------------------------------------------------------------- the relay
async def test_the_file_goes_into_the_upload_as_it_arrives(tmp_path):
    data, td, client, message = make(tmp_path, parts=5, delay=0.01)
    seen = []
    async with transfer.Relay(client, message, NEW_NAME) as relay:
        sent = await send(client, relay, progress=lambda done, total: seen.append((done, total)))
    assert sent.id
    assert td.violations == []
    (generation,) = td.generations
    assert td.finished == {generation: None}
    assert td.progress[-1] == (generation, len(data), len(data))  # the last report says all of it is there
    assert len({p for _g, _e, p in td.progress}) >= 3  # it was handed over in pieces, not at the end
    assert relay.complete and relay.error is None and not relay.cancel.is_set()
    (request,) = td.calls("downloadFile")
    assert (request["file_id"], request["offset"], request["limit"], request["synchronous"]) == (FILE_ID, 0, 0, False)
    assert [r["file_id"] for r in td.calls("deleteFile")] == [FILE_ID]  # TDLib's copy of the original goes: the disk gets it back
    assert td.calls("cancelDownloadFile") == []
    assert not td.generations[generation].destination.exists()  # and so does the file that was uploaded from
    assert client.generators == {}


async def test_progress_is_not_reported_more_often_than_every_so_often(tmp_path, monkeypatch):
    monkeypatch.setattr(transfer, "REPORT_EVERY", 30.0)
    data, td, client, message = make(tmp_path, parts=10, delay=0.005)
    async with transfer.Relay(client, message, NEW_NAME) as relay:
        await send(client, relay)
    assert td.violations == [] and relay.complete
    assert len(td.progress) <= 3 and td.progress[-1][2] == len(data)  # a first report, and the last one that says it is all there


async def test_a_file_that_is_already_downloaded_is_copied_whole(tmp_path):
    data, td, client, message = make(tmp_path)
    td.arrived()
    async with transfer.Relay(client, message, NEW_NAME) as relay:
        await send(client, relay)
    assert td.violations == [] and relay.complete and relay.error is None
    assert td.progress and td.progress[-1][2] == len(data)


async def test_the_download_that_moves_to_its_place_when_done_is_followed(tmp_path):
    """TDLib keeps a download in a temporary file and moves it when it is done: the path the bot reads from changes on the way."""
    data, td, client, message = make(tmp_path, parts=3, delay=0.02)
    async with transfer.Relay(client, message, NEW_NAME) as relay:
        await send(client, relay)
    assert td.violations == [] and relay.complete
    assert td.path.endswith("file.mkv") and "partial" not in td.path


async def test_a_download_that_stops_ends_the_streaming_and_leaves_the_rest_to_the_usual_way(tmp_path):
    data, td, client, message = make(tmp_path, parts=6, delay=0.005)
    td.dies_at = len(data) // 2
    async with transfer.Relay(client, message, NEW_NAME, stall_timeout=0.3, look_every=0.05) as relay:
        with pytest.raises((errors.RPCError, errors.TransferCancelled)):
            await send(client, relay)
    assert isinstance(relay.error, TimeoutError) and relay.cancel.is_set() and not relay.complete
    (generation,) = td.generations
    assert td.finished[generation]["message"].startswith("TimeoutError")  # TDLib is told, so it lets go of the upload
    assert td.violations == []
    assert td.calls("cancelDownloadFile") and td.calls("deleteFile") == []  # what was downloaded stays for the usual way to carry on from


async def test_a_file_telegram_does_not_let_the_bot_download_is_a_failure_of_the_streaming(tmp_path):
    data, td, client, message = make(tmp_path)

    def refuse(_request):
        item = td.file()
        item["local"]["can_be_downloaded"] = False
        return item

    td.reply("downloadFile", refuse)
    async with transfer.Relay(client, message, NEW_NAME, stall_timeout=5) as relay:
        with pytest.raises((errors.RPCError, errors.TransferCancelled)):
            await send(client, relay)
    assert isinstance(relay.error, RuntimeError) and "download" in str(relay.error)


async def test_cancelling_the_job_stops_the_upload_and_the_download(tmp_path):
    data, td, client, message = make(tmp_path, parts=50, delay=0.02)
    job_cancel = asyncio.Event()
    async with transfer.Relay(client, message, NEW_NAME, cancel=job_cancel) as relay:
        task = asyncio.ensure_future(send(client, relay))
        await td.wait_for(lambda: len(td.progress) >= 2)
        job_cancel.set()
        with pytest.raises(errors.TransferCancelled):
            await task
    assert relay.error is None  # a cancel is not a failure of the streaming
    (generation,) = td.generations
    told = td.finished.get(generation)  # whichever saw the cancel first: the relay tells TDLib, or the half-sent message is deleted and TDLib stops
    assert (told is not None and told["message"] == "cancelled") or generation in td.stopped_generations
    assert td.calls("cancelDownloadFile")
    assert not td.active or td.have < len(data)


async def test_the_relay_gives_the_job_the_same_cancel_error_it_would_raise(tmp_path):
    """The pipeline turns what the send raises into ``Cancelled`` itself when the job's event is set; the relay only has to end things."""
    data, td, client, message = make(tmp_path, parts=50, delay=0.02)
    job_cancel = asyncio.Event()
    async with transfer.Relay(client, message, NEW_NAME, cancel=job_cancel) as relay:
        task = asyncio.ensure_future(send(client, relay))
        await td.wait_for(lambda: len(td.progress) >= 1)
        job_cancel.set()
        with pytest.raises(Exception):
            await task
    assert job_cancel.is_set() and relay.cancel.is_set()
    assert issubclass(Cancelled, Exception)


async def test_when_telegram_stops_asking_for_the_file_nothing_more_is_said_about_it(tmp_path):
    data, td, client, message = make(tmp_path, parts=30, delay=0.01)
    async with transfer.Relay(client, message, NEW_NAME) as relay:
        task = asyncio.ensure_future(send(client, relay))
        await td.wait_for(lambda: len(td.progress) >= 2)
        (generation,) = td.generations
        await td.emit({"@type": "updateFileGenerationStop", "generation_id": generation})
        await td.settle(50)
        reports = len(td.progress)
        await asyncio.sleep(0.5)  # long enough for the whole download to arrive: a copy that went on would be heard of
        assert td.calls("finishFileGeneration") == [] and len(td.progress) == reports  # it stopped, and told no one
        assert relay.error is None and not relay.cancel.is_set()
        relay.cancel.set()  # (ends the test's own wait for the message)
        with pytest.raises(errors.TransferCancelled):
            await task


async def test_a_second_request_for_the_file_starts_the_copy_again(tmp_path):
    data, td, client, message = make(tmp_path, parts=8, delay=0.02)
    async with transfer.Relay(client, message, NEW_NAME, look_every=0.02) as relay:
        task = asyncio.ensure_future(send(client, relay))
        await td.wait_for(lambda: len(td.progress) >= 2)
        (first,) = td.generations
        pending = td.generations[first].pending
        second = 9000 + td._next()
        destination = td.tmp / "generated" / str(second) / NEW_NAME
        destination.parent.mkdir(parents=True)
        td.generations[second] = SimpleNamespace(destination=destination, pending=pending, generated=td.generations[first].generated)
        await td.emit({"@type": "updateFileGenerationStart", "generation_id": second, "original_path": "x/" + NEW_NAME,
                       "destination_path": str(destination), "conversion": relay.conversion})
        await task
    await asyncio.sleep(0.3)  # the old request, if it were still alive, would have said something by now
    assert td.violations == []
    assert first not in td.finished and td.finished[second] is None  # the old request is dropped, the new one is answered
    assert not destination.exists() and not td.generations[first].destination.exists()  # neither file is left behind


async def test_a_cancel_that_is_already_set_stops_the_copy_before_a_byte_is_moved(tmp_path):
    data, td, client, message = make(tmp_path, parts=4, delay=0.01)
    td.reply("finishFileGeneration", {"@type": "ok"})  # no upload is behind this request: only what the relay says is looked at
    async with transfer.Relay(client, message, NEW_NAME) as relay:
        relay.cancel.set()
        await relay._start({"generation_id": 5, "destination_path": str(tmp_path / "g" / NEW_NAME), "conversion": relay.conversion})
    (finish,) = td.calls("finishFileGeneration")
    assert finish["generation_id"] == 5 and finish["error"]["message"] == "cancelled"
    assert td.calls("setFileGenerationProgress") == [] and not relay.complete and relay.error is None


async def test_a_job_that_is_already_cancelled_moves_nothing_either(tmp_path):
    data, td, client, message = make(tmp_path, parts=4, delay=0.01)
    td.reply("finishFileGeneration", {"@type": "ok"})
    outer = asyncio.Event()
    async with transfer.Relay(client, message, NEW_NAME, cancel=outer) as relay:
        outer.set()
        await relay._start({"generation_id": 6, "destination_path": str(tmp_path / "g" / NEW_NAME), "conversion": relay.conversion})
    (finish,) = td.calls("finishFileGeneration")
    assert finish["error"]["message"] == "cancelled" and td.calls("setFileGenerationProgress") == [] and not relay.complete


async def test_cancelling_the_job_before_telegram_asks_for_the_file_stops_the_upload_at_once(tmp_path):
    data, td, client, message = make(tmp_path, generation_delay=0.3)
    job_cancel = asyncio.Event()
    async with transfer.Relay(client, message, NEW_NAME, cancel=job_cancel) as relay:
        task = asyncio.ensure_future(send(client, relay))
        await td.settle(20)
        job_cancel.set()
        with pytest.raises(errors.TransferCancelled):
            await asyncio.wait_for(task, 0.2)  # well before the 0.3 seconds TDLib takes to ask: there is nothing for the relay to do yet
    assert td.calls("downloadFile") == [] and td.progress == []


async def test_a_new_request_after_telegram_let_go_of_the_old_one_is_served(tmp_path):
    data, td, client, message = make(tmp_path, parts=10, delay=0.02)
    async with transfer.Relay(client, message, NEW_NAME) as relay:
        task = asyncio.ensure_future(send(client, relay))
        await td.wait_for(lambda: len(td.progress) >= 2)
        (first,) = td.generations
        await td.emit({"@type": "updateFileGenerationStop", "generation_id": first})
        await td.settle(50)
        second = 9000 + td._next()
        destination = td.tmp / "generated" / str(second) / NEW_NAME
        destination.parent.mkdir(parents=True)
        td.generations[second] = SimpleNamespace(destination=destination, pending=td.generations[first].pending, generated=td.generations[first].generated)
        await td.emit({"@type": "updateFileGenerationStart", "generation_id": second, "original_path": "x/" + NEW_NAME,
                       "destination_path": str(destination), "conversion": relay.conversion})
        await asyncio.wait_for(task, 5)
    assert td.violations == [] and td.finished == {second: None}  # the stop of the first request does not hold the second back


async def test_leaving_the_relay_ends_the_copy_and_what_follows_the_job(tmp_path):
    data, td, client, message = make(tmp_path, parts=60, delay=0.01)
    outer = asyncio.Event()
    with pytest.raises(RuntimeError):
        async with transfer.Relay(client, message, NEW_NAME, cancel=outer) as relay:
            task = asyncio.ensure_future(send(client, relay))
            await td.wait_for(lambda: len(td.progress) >= 2)
            raise RuntimeError("the send failed for another reason")
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    reports = len(td.progress)
    await asyncio.sleep(0.15)
    assert len(td.progress) == reports  # nothing is copied for an upload that is over
    assert relay._task.done() and relay._follower.done()


async def test_a_download_that_is_slow_but_never_still_is_not_given_up(tmp_path):
    data, td, client, message = make(tmp_path, parts=30, delay=0.02)  # 0.6 s in all, longer than the patience for a pause
    async with transfer.Relay(client, message, NEW_NAME, stall_timeout=0.4, look_every=0.05) as relay:
        await send(client, relay)
    assert relay.error is None and relay.complete and td.violations == []


async def test_a_download_is_followed_even_when_telegram_says_nothing_about_it(tmp_path):
    data, td, client, message = make(tmp_path, parts=5, delay=0.02, quiet=True)
    async with transfer.Relay(client, message, NEW_NAME, look_every=0.02) as relay:
        await asyncio.wait_for(send(client, relay), 5)
    assert relay.complete and relay.error is None and td.violations == []
    assert td.calls("getFile")  # no update came, so the bot looked by itself


async def test_a_download_that_says_it_is_done_before_its_file_is_whole_is_not_taken_for_done(tmp_path):
    data, td, client, message = make(tmp_path)
    folder = td.tmp / "documents"
    folder.mkdir(parents=True)
    (folder / "file.mkv").write_bytes(data[:1000])
    td.path, td.have, td.final = str(folder / "file.mkv"), len(data), True  # "all of it is there" says TDLib; the file holds 1000 bytes
    async with transfer.Relay(client, message, NEW_NAME, stall_timeout=0.2, look_every=0.02) as relay:
        with pytest.raises((errors.RPCError, errors.TransferCancelled)):
            await send(client, relay)
    assert isinstance(relay.error, TimeoutError) and not relay.complete and td.violations == []  # not finished as if it were whole


async def test_the_size_of_a_file_telegram_has_not_told_yet_is_the_one_it_expects(tmp_path):
    _data, _td, client, message = make(tmp_path)
    message.file.td_file["size"], message.file.td_file["expected_size"] = 0, 4321
    relay = transfer.Relay(client, message, NEW_NAME)
    assert relay.size == 4321 and relay.source.size == 4321


async def test_a_message_without_a_file_cannot_be_relayed(tmp_path):
    _data, _td, client, _message = make(tmp_path)
    with pytest.raises(ValueError):
        transfer.Relay(client, Message(td_message({"@type": "messageText", "text": {"@type": "formattedText", "text": "hi", "entities": []}})), NEW_NAME)


def test_copy_part_goes_on_through_a_file_bigger_than_one_chunk(tmp_path, monkeypatch):
    monkeypatch.setattr(transfer, "COPY_CHUNK", 100)
    source = tmp_path / "s.bin"
    source.write_bytes(os.urandom(1000))
    out = tmp_path / "o.bin"
    fd = os.open(out, os.O_WRONLY | os.O_CREAT)
    try:
        assert transfer.copy_part(str(source), fd, 250, 950) == 950  # from the middle of the file, over seven chunks
    finally:
        os.close(fd)
    assert out.read_bytes() == source.read_bytes()[250:950]


def test_copy_part_writes_everything_even_when_the_system_takes_it_in_small_pieces(tmp_path, monkeypatch):
    source = tmp_path / "s.bin"
    source.write_bytes(os.urandom(500))
    out = tmp_path / "o.bin"
    real_write = os.write
    monkeypatch.setattr(transfer.os, "write", lambda fd, data: real_write(fd, bytes(data[:7])))  # a pipe or a full disk may do this
    fd = os.open(out, os.O_WRONLY | os.O_CREAT)
    try:
        assert transfer.copy_part(str(source), fd, 0, 500) == 500
    finally:
        os.close(fd)
    monkeypatch.undo()
    assert out.read_bytes() == source.read_bytes()


def test_copy_part_copies_what_is_there_and_says_how_far_it_got(tmp_path):
    source = tmp_path / "s.bin"
    source.write_bytes(bytes(range(256)) * 4)
    out = tmp_path / "o.bin"
    fd = os.open(out, os.O_WRONLY | os.O_CREAT)
    try:
        assert transfer.copy_part(str(source), fd, 0, 100) == 100
        assert transfer.copy_part(str(source), fd, 100, 5000) == 1024  # the file ends there
    finally:
        os.close(fd)
    assert out.read_bytes() == source.read_bytes()


def test_stream_failures_are_counted():
    stats = transfer.TransferStats()
    stats.stream_failed(RuntimeError("boom"))
    assert (stats.stream_fallbacks, stats.last_stream_fallback) == (1, "RuntimeError: boom")
