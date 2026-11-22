"""The question the bot asks once its queue is empty: sort the files it has renamed?  (Yes / No / Send more)"""
import asyncio
import time
from types import SimpleNamespace

import pytest

from bot import app as appmod
from bot.queue import Job, JobQueue
from bot.sequence import detect, detect_quality

from .appkit import ADMIN_CFG, OWNER, click, data_of, say, send_doc
from .tgfakes import FakeEvent, dispatch, document_message, flat_buttons

OP = "{n} - One Piece [Dual] 720p @Anime_X_Universe.mkv"
DELAY = 0.1  # SORT_ASK_DELAY in these tests
QUIET = 0.4  # long enough for a question that is going to be asked to have been asked


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(appmod, "SORT_ASK_DELAY", DELAY)
    monkeypatch.setattr(appmod, "SORT_GAP", 0)
    monkeypatch.setattr(appmod, "SORT_SLOW_GAP", 0)


class Renaming:
    """Stands in for the renamer behind the queue: a job waits while its user is held, then the renamed file is "in its chat" (the
    real hook of the pipeline hears of it) or the job fails or is cancelled."""

    def __init__(self, ctx):
        self.ctx = ctx
        self.held = {}  # user -> how many more of their jobs may finish; no entry: they finish at once
        self.fail = set()  # names of files whose rename fails
        ctx.app.queue = JobQueue(self.run, workers=4, on_cancelled=ctx.app._on_job_cancelled, on_left=ctx.app._on_job_left)
        ctx.app.queue.start()

    def hold(self, uid):
        self.held[uid] = 0

    def let(self, uid, count=None):
        """Let ``count`` more of the user's jobs finish (all of them, from now on, when ``None``)."""
        if count is None:
            self.held.pop(uid, None)
        else:
            self.held[uid] = self.held.get(uid, 0) + count

    async def run(self, job):
        while self.held.get(job.user_id, 1) <= 0 and not job.cancel.is_set():
            await asyncio.sleep(0.005)
        if job.cancel.is_set():
            job.state = "cancelled"
            return
        if job.user_id in self.held:
            self.held[job.user_id] -= 1
        if job.file_name in self.fail:
            job.state = "failed"
            return
        settings = await self.ctx.db.get_settings(job.user_id)
        target = settings.get("target_channel") or job.chat_id
        sent = SimpleNamespace(id=5000 + job.id, raw_text="")
        self.ctx.client.messages[sent.id] = document_message(job.new_name, 1024, mid=sent.id)
        job.state = "done"
        await self.ctx.app.renamer._after_delivery(job, target, sent)


@pytest.fixture
def renaming(ctx):
    return Renaming(ctx)


async def feed(ctx, names, uid=OWNER, first_id=100):
    for k, name in enumerate(names):
        await send_doc(ctx, uid, name, mid=first_id + k)


def episodes(numbers):
    return [OP.format(n=n) for n in numbers]


async def until(check, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if check():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("timed out waiting")


async def quiet():
    await asyncio.sleep(QUIET)


def questions(ctx, uid=OWNER):
    """The "sort these files now?" messages the user got: ``(message id, text, callback data of its buttons)``."""
    return [
        (2000 + number, text, data_of(kw["buttons"]))
        for number, (entity, text, kw) in enumerate(ctx.client.sent, 1)
        if entity == uid and "Do you want me to sort" in text
    ]


def answer_data(ctx, action, which=-1, uid=OWNER):
    """The callback data of the button ``action`` (y, n or m) of one of the questions."""
    (data,) = [d for d in questions(ctx, uid)[which][2] if d.split(":")[1] == action]
    return data


def sent_back(ctx, chat=OWNER):
    """``(season, episode, picture height)`` of the files sent again, in the order they went out."""
    return [
        (*detect(f.file.name, "", "filename"), detect_quality(f.file.name, "", "filename"))
        for entity, f, _kw in ctx.client.files
        if entity == chat
    ]


def reports(ctx, uid=OWNER):
    return [text for entity, text, _kw in ctx.client.sent if entity == uid and ("Sorted" in text or "Stopped" in text)]


# ---------------------------------------------------------------------------
# when the question is asked
# ---------------------------------------------------------------------------


async def test_a_hundred_files_are_renamed_and_then_the_bot_asks_once_about_all_of_them(ctx, renaming):
    await feed(ctx, episodes(range(1101, 1201)))
    await until(lambda: questions(ctx))
    await quiet()
    ((_mid, text, data),) = questions(ctx)  # one question, and not before the last file was renamed
    assert "sort these 100 files now?" in text and "queue is empty" in text
    assert [d.split(":")[1] for d in data] == ["y", "n", "m"]
    assert ctx.app.queue.jobs == {} and len(ctx.app.sort_batches[(OWNER, OWNER)].items) == 100


async def test_the_buttons_are_yes_no_and_send_more(ctx, renaming):
    await feed(ctx, episodes((1, 2, 3)))
    await until(lambda: questions(ctx))
    (kw,) = [kw for entity, text, kw in ctx.client.sent if "Do you want me to sort" in text]
    assert [label for label, _data in flat_buttons(kw["buttons"])] == ["✅ Yes", "❌ No", "➕ Send more"]


async def test_nothing_is_asked_while_files_are_still_waiting_or_being_renamed(ctx, renaming):
    renaming.hold(OWNER)
    await feed(ctx, episodes((1, 2, 3, 4)))
    renaming.let(OWNER, 2)  # two are renamed, two are not
    await quiet()
    assert questions(ctx) == [] and len(ctx.app.queue.for_user(OWNER)) == 2
    renaming.let(OWNER)
    await until(lambda: questions(ctx))
    await quiet()
    assert len(questions(ctx)) == 1 and "sort these 4 files" in questions(ctx)[0][1]


async def test_files_sent_before_the_first_batch_is_finished_are_added_and_one_question_covers_all(ctx, renaming):
    renaming.hold(OWNER)
    await feed(ctx, episodes((1, 2, 3, 4)))
    renaming.let(OWNER, 2)
    await quiet()
    await feed(ctx, episodes((5, 6, 7, 8)), first_id=200)  # the first batch is not done yet
    renaming.let(OWNER)
    await until(lambda: questions(ctx))
    await quiet()
    ((_mid, text, _data),) = questions(ctx)
    assert "sort these 8 files now?" in text


async def test_a_lone_file_has_nothing_to_be_sorted_with_so_nothing_is_asked(ctx, renaming):
    await feed(ctx, episodes((1,)))
    await quiet()
    assert questions(ctx) == [] and ctx.app.sort_batches == {}  # and it is not kept for later either
    await feed(ctx, episodes((2,)), first_id=200)
    await quiet()
    assert questions(ctx) == []


async def test_files_that_failed_or_were_cancelled_are_not_counted(ctx, renaming):
    renaming.fail.add(OP.format(n=2))
    renaming.hold(OWNER)
    await feed(ctx, episodes((1, 2, 3, 4, 5)))
    await until(lambda: ctx.app.queue.running == 4)
    (waiting,) = [j for j in ctx.app.queue.for_user(OWNER) if j.state == "queued"]
    assert ctx.app.queue.cancel(waiting.id) is True
    renaming.let(OWNER)
    await until(lambda: questions(ctx))
    ((_mid, text, _data),) = questions(ctx)
    assert "sort these 3 files now?" in text


async def test_cancelling_the_rest_of_the_queue_still_asks_about_the_files_that_were_renamed(ctx, renaming):
    renaming.hold(OWNER)
    await feed(ctx, episodes((1, 2, 3, 4, 5, 6)))
    await until(lambda: ctx.app.queue.running == 4)
    renaming.let(OWNER, 3)
    await until(lambda: ctx.app.queue.running + ctx.app.queue.waiting == 3)
    await say(ctx, OWNER, "/cancel")  # the queued ones are cancelled at once, the running one stops when it gets to it
    await until(lambda: questions(ctx))
    assert "sort these 3 files now?" in questions(ctx)[0][1] and ctx.app.queue.jobs == {}


async def test_each_user_is_asked_about_their_own_files_when_their_own_queue_is_empty(ctx, renaming):
    renaming.hold(ADMIN_CFG)  # the admin's files are still being renamed
    await feed(ctx, episodes((1, 2, 3)), uid=OWNER)
    await feed(ctx, episodes((7, 8)), uid=ADMIN_CFG, first_id=300)
    await until(lambda: questions(ctx, OWNER))
    await quiet()
    assert "sort these 3 files" in questions(ctx, OWNER)[0][1] and questions(ctx, ADMIN_CFG) == []
    renaming.let(ADMIN_CFG)
    await until(lambda: questions(ctx, ADMIN_CFG))
    assert "sort these 2 files" in questions(ctx, ADMIN_CFG)[0][1] and len(questions(ctx, OWNER)) == 1


async def test_a_file_that_is_still_on_its_way_into_the_queue_keeps_the_question_back(ctx, renaming):
    class Slow(FakeEvent):
        async def reply(self, text=None, **kw):
            await asyncio.sleep(0.4)  # the reply to the file takes longer than the bot waits for an empty queue
            return await super().respond(text, **kw)

    await feed(ctx, episodes((1, 2, 3)))
    slow = Slow(OWNER, message=document_message(OP.format(n=4), 1024, mid=150))
    arriving = asyncio.ensure_future(dispatch(ctx.client, slow))
    await asyncio.sleep(0.25)  # the three are renamed, the fourth is not queued yet
    assert questions(ctx) == []
    await arriving
    await until(lambda: questions(ctx))
    await quiet()
    assert len(questions(ctx)) == 1 and "sort these 4 files now?" in questions(ctx)[0][1]


async def test_files_sent_while_the_question_is_still_going_out_take_it_back(ctx, renaming):
    gate = asyncio.Event()
    real = ctx.client.send_message

    async def slow(entity, text="", **kw):
        sent = await real(entity, text, **kw)
        if "Do you want me to sort" in text:
            await gate.wait()  # the question is out, but the bot has not heard that yet (a flood wait, say)
        return sent

    ctx.client.send_message = slow
    await feed(ctx, episodes((1, 2, 3)))
    await until(lambda: questions(ctx))
    assert ctx.app.sort_batches[(OWNER, OWNER)].prompt_id is None

    renaming.hold(OWNER)
    await feed(ctx, episodes((4, 5)), first_id=200)  # more files come in while the question is on its way
    gate.set()
    await until(lambda: ctx.client.deleted)
    assert ctx.client.deleted == [(OWNER, [questions(ctx)[0][0]])]  # so it is answered "Send more" and taken down
    assert ctx.app.sort_batches[(OWNER, OWNER)].prompt_id is None

    renaming.let(OWNER)  # and the question comes once they are all renamed, about all five
    await until(lambda: len(questions(ctx)) == 2)
    assert "sort these 5 files now?" in questions(ctx)[1][1]


async def test_manual_renames_are_not_part_of_it_and_do_not_answer_the_question(ctx, renaming):
    manual = await ctx.db.create_format(OWNER, "One Piece", template="One Piece - E{episode}")
    manual = await ctx.db.get_format(OWNER, manual["id"])
    job = Job(id=ctx.app.queue.new_id(), user_id=OWNER, chat_id=OWNER, msg_id=9, file_name="m.mkv", size=1, new_name="m.mkv", fmt=manual)
    ctx.app.queue.add(job)
    await quiet()
    assert job.state == "done" and ctx.app.sort_batches == {} and questions(ctx) == []  # a manual rename is not asked about

    await feed(ctx, episodes((1, 2, 3)))
    await until(lambda: questions(ctx))
    event = FakeEvent(OWNER, message=document_message("m2.mkv", 1024, mid=400))
    ctx.client.messages[400] = event.message
    await ctx.app.enqueue(event, event.message, fmt=manual)  # sending a file for a manual rename leaves the open question alone
    await quiet()
    assert ctx.app.sort_batches[(OWNER, OWNER)].prompt_id is not None and ctx.client.deleted == [] and len(questions(ctx)) == 1


# ---------------------------------------------------------------------------
# the answers
# ---------------------------------------------------------------------------


async def test_yes_sorts_and_sends_the_files_in_episode_order_and_reports(ctx, renaming):
    await feed(ctx, episodes((1147, 1145, 1146, 1149)))  # sent out of order, 1148 is missing
    await until(lambda: questions(ctx))
    (prompt_id, _text, _data) = questions(ctx)[0]

    cb = await click(ctx, OWNER, answer_data(ctx, "y"), msg_id=prompt_id)
    assert cb.deleted is True and cb.alerts == []  # the question is taken down
    assert [e for (_s, e, _q) in sent_back(ctx)] == [1145, 1146, 1147, 1149]
    (report,) = reports(ctx)
    assert "Sorted 4 files" in report and "E1145 → E1149" in report and "E1148" in report
    assert ctx.app.sort_batches == {} and OWNER not in ctx.app._sorting
    await quiet()
    assert len(questions(ctx)) == 1  # nothing else is asked


async def test_no_forgets_the_files_so_the_next_question_is_only_about_the_files_sent_after_it(ctx, renaming):
    await feed(ctx, episodes((1, 2, 3)))
    await until(lambda: questions(ctx))
    cb = await click(ctx, OWNER, answer_data(ctx, "n"))
    text, buttons = cb.edits[-1]
    assert "won't sort these 3 files" in text and buttons is None
    assert ctx.client.files == [] and ctx.app.sort_batches == {}

    await feed(ctx, episodes((4, 5)), first_id=200)
    await until(lambda: len(questions(ctx)) == 2)
    assert "sort these 2 files now?" in questions(ctx)[1][1]  # not 5


async def test_send_more_keeps_the_files_and_waits_for_more_before_it_asks_again(ctx, renaming):
    await feed(ctx, episodes((1, 2, 3)))
    await until(lambda: questions(ctx))
    old_more = answer_data(ctx, "m")
    cb = await click(ctx, OWNER, old_more)
    text, buttons = cb.edits[-1]
    assert text.startswith("➕ Okay, send me more files") and "3 so far" in text and buttons is None
    await quiet()
    assert len(questions(ctx)) == 1  # it waits for the next files, it does not ask again by itself
    assert (await click(ctx, OWNER, old_more)).alerts == ["That question is over."]  # the answer was given: it cannot be given twice

    await feed(ctx, episodes((4, 5)), first_id=200)
    await until(lambda: len(questions(ctx)) == 2)
    assert "sort these 5 files now?" in questions(ctx)[1][1]  # the three it kept and the two new ones
    assert ctx.client.deleted == []  # the "send me more files" answer stays in the chat


async def test_files_sent_while_the_question_is_open_count_as_send_more(ctx, renaming):
    await feed(ctx, episodes((1, 2, 3)))
    await until(lambda: questions(ctx))
    (prompt_id, _text, _data) = questions(ctx)[0]
    old_yes = answer_data(ctx, "y")

    await feed(ctx, episodes((4, 5)), first_id=200)  # no answer, more files instead
    await until(lambda: ctx.client.deleted)
    assert ctx.client.deleted == [(OWNER, [prompt_id])]  # the open question is taken down
    await until(lambda: len(questions(ctx)) == 2)
    await quiet()
    assert len(questions(ctx)) == 2 and "sort these 5 files now?" in questions(ctx)[1][1]

    cb = await click(ctx, OWNER, old_yes)  # a button of the question that was taken down
    assert cb.alerts == ["That question is over."] and ctx.client.files == []
    assert len(ctx.app.sort_batches[(OWNER, OWNER)].items) == 5


async def test_a_file_that_is_refused_also_counts_as_send_more_but_the_question_comes_back(ctx, renaming):
    await feed(ctx, episodes((1, 2, 3)))
    await until(lambda: questions(ctx))
    await send_doc(ctx, OWNER, OP.format(n=9), size=101 * 1024 * 1024, mid=300)  # over the limit: refused, never queued
    await until(lambda: ctx.client.deleted)  # the open question was taken down ...
    await until(lambda: len(questions(ctx)) == 2)  # ... and nobody else would ask again, so the bot does
    assert "sort these 3 files now?" in questions(ctx)[1][1]


async def test_yes_does_nothing_while_the_last_sort_is_still_going_and_the_question_stays(ctx, renaming):
    await feed(ctx, episodes((1, 2, 3)))
    await until(lambda: questions(ctx))
    ctx.app._sorting[OWNER] = asyncio.Event()  # a /sort is sending files
    cb = await click(ctx, OWNER, answer_data(ctx, "y"))
    assert cb.alerts == ["I'm still sorting your last batch. /cancel to stop it."] and not cb.deleted
    assert ctx.client.files == [] and len(ctx.app.sort_batches[(OWNER, OWNER)].items) == 3

    del ctx.app._sorting[OWNER]
    await click(ctx, OWNER, answer_data(ctx, "y"))
    assert len(ctx.client.files) == 3


async def test_buttons_of_a_question_the_bot_no_longer_knows_are_refused(ctx, renaming):
    cb = await click(ctx, OWNER, "so:y:%d:7" % OWNER)  # e.g. after a restart
    assert cb.alerts == ["That question is over."] and ctx.client.files == []
    cb = await click(ctx, OWNER, "so:y:x:7")
    assert cb.alerts == [] and cb.answers == [(None, False)]  # nonsense data is just acknowledged

    await feed(ctx, episodes((1, 2, 3)))
    await until(lambda: questions(ctx))
    cb = await click(ctx, OWNER, "so:y:%d:999" % OWNER)  # a question number that is not the open one
    assert cb.alerts == ["That question is over."] and ctx.client.files == []
    cb = await click(ctx, ADMIN_CFG, answer_data(ctx, "y"))  # somebody else's question
    assert cb.alerts == ["That question is over."] and ctx.client.files == []


# ---------------------------------------------------------------------------
# what is sorted and where it goes
# ---------------------------------------------------------------------------


async def test_the_same_episode_comes_season_by_season_and_picture_by_picture(ctx, renaming):
    names = [
        "Show S02E03 1080p.mkv", "Show S01E03 2160p.mkv", "Show S01E03 480p.mkv", "Show S01E04 720p.mkv",
        "Show S01E03 1080p.mkv", "Show S01E03 720p.mkv", "Show S02E03 480p.mkv", "Show S01E02 1080p.mkv",
    ]
    await feed(ctx, names)
    await until(lambda: questions(ctx))
    await click(ctx, OWNER, answer_data(ctx, "y"))
    assert sent_back(ctx) == [
        (1, 2, 1080), (1, 3, 480), (1, 3, 720), (1, 3, 1080), (1, 3, 2160), (1, 4, 720), (2, 3, 480), (2, 3, 1080),
    ]
    (report,) = reports(ctx)
    assert "S01E02 → S02E03" in report and "Repeated" not in report  # versions of an episode are not repeats


async def test_files_sent_to_the_users_channel_are_sorted_there_and_the_question_says_so(ctx, renaming):
    channel = -1001234567890
    await ctx.db.update_settings(OWNER, target_channel=channel)
    await feed(ctx, episodes((3, 1, 2)))
    await until(lambda: questions(ctx))
    assert "📣 They are in your channel" in questions(ctx)[0][1]  # the question itself comes to the chat with the bot
    assert ctx.app.sort_batches[(OWNER, channel)].home == OWNER

    await click(ctx, OWNER, answer_data(ctx, "y"))
    assert [e for (_s, e, _q) in sent_back(ctx, channel)] == [1, 2, 3] and sent_back(ctx, OWNER) == []
    assert "Sorted 3 files" in reports(ctx, OWNER)[0]  # the report comes to the chat with the bot


async def test_files_that_went_to_different_chats_are_asked_about_separately(ctx, renaming):
    channel = -1001234567890
    await feed(ctx, episodes((1, 2)))
    await until(lambda: len(questions(ctx)) == 1)
    await click(ctx, OWNER, answer_data(ctx, "m"))
    await ctx.db.update_settings(OWNER, target_channel=channel)  # the user sets a channel in between
    await feed(ctx, episodes((3, 4, 5)), first_id=200)
    await until(lambda: len(questions(ctx)) == 3)
    two = [t for _mid, t, _d in questions(ctx)[1:] if "these 2 files" in t]
    three = [t for _mid, t, _d in questions(ctx)[1:] if "these 3 files" in t]
    assert len(two) == 1 and len(three) == 1 and "in your channel" in three[0] and "in your channel" not in two[0]
