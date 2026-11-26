"""The report the bot sends once a user's queue is empty, just before it asks whether to sort: how the batch of files went."""
import asyncio
import logging
import time
from types import SimpleNamespace

import pytest

from bot import app as appmod
from bot.pipeline import Result
from bot.queue import Job, JobQueue

from .appkit import ADMIN_CFG, OWNER, send_doc
from .tgfakes import document_message

OP = "{n} - One Piece [Dual] 720p @Anime_X_Universe.mkv"
DELAY = 0.1  # SORT_ASK_DELAY in these tests
QUIET = 0.4  # long enough for a report that is going to be sent to have been sent


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(appmod, "SORT_ASK_DELAY", DELAY)


class Finishing:
    """Stands in for the renamer behind the queue.  A job ends the way ``plan`` says for its file name (renamed and checked when it says
    nothing), and the app hears of it as it hears of the real pipeline: first the file is in its chat, then how the job ended."""

    def __init__(self, ctx):
        self.ctx = ctx
        self.plan = {}  # file name -> the Result the job ends with
        self.names = {}  # file name -> what the file was renamed to
        ctx.app.queue = JobQueue(self.run, workers=4, on_cancelled=ctx.app._on_job_cancelled, on_left=ctx.app._on_job_left)
        ctx.app.queue.start()

    async def run(self, job):
        self.names[job.file_name] = job.new_name
        result = job.result = self.plan.get(job.file_name) or Result(state="done", checks="size · name · thumbnail")
        job.state = result.state
        if result.state == "done":
            sent = SimpleNamespace(id=5000 + job.id, raw_text="")
            self.ctx.client.messages[sent.id] = document_message(job.new_name, 1024, mid=sent.id)
            await self.ctx.app.renamer._after_delivery(job, job.chat_id, sent)
        await self.ctx.app.renamer._tell_result(job)


@pytest.fixture
def finishing(ctx):
    return Finishing(ctx)


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


def reports(ctx, uid=OWNER):
    return [text for entity, text, _kw in ctx.client.sent if entity == uid and "Batch finished" in text]


def questions(ctx, uid=OWNER):
    return [text for entity, text, _kw in ctx.client.sent if entity == uid and "Do you want me to sort" in text]


def order_sent(ctx, uid=OWNER):
    """What the user was sent that is not about one file: ``report`` and ``question``, in the order they came."""
    return ["report" if "Batch finished" in text else "question" for entity, text, _kw in ctx.client.sent
            if entity == uid and ("Batch finished" in text or "Do you want me to sort" in text)]


# ---------------------------------------------------------------------------
# when the report comes
# ---------------------------------------------------------------------------


async def test_the_app_hears_how_every_job_ended(ctx):
    assert ctx.app.renamer.on_result == ctx.app._note_result


async def test_the_report_comes_once_the_queue_is_empty_and_before_the_question_about_sorting(ctx, finishing):
    await feed(ctx, episodes(range(1, 4)))
    await until(lambda: questions(ctx))
    await quiet()
    assert order_sent(ctx) == ["report", "question"]
    (text,) = reports(ctx)
    assert "all 3 files renamed" in text and "✅ 3 checked after sending: size · name · thumbnail" in text
    assert ctx.app._outcomes == {}  # and the next batch starts from nothing


async def test_the_report_names_the_files_that_failed_or_came_with_a_warning(ctx, finishing):
    names = episodes(range(1, 5))
    finishing.plan[names[1]] = Result(state="failed", reason="RPCError: File download has failed or was canceled (stopped short 4 times) (400)", retries=2)
    finishing.plan[names[2]] = Result(state="done", warnings=["Telegram sent the file without its thumbnail."], checks="size · name", thumb_resent=True)
    await feed(ctx, names)
    await until(lambda: reports(ctx))
    (text,) = reports(ctx)
    assert "3 of 4 files renamed" in text and "✅ 2 checked after sending: size · name · thumbnail" in text
    assert f"❌ 1 failed:\n• <code>{finishing.names[names[1]]}</code>\n  RPCError: File download has failed" in text
    assert f"⚠️ 1 renamed, with a warning:\n• <code>{finishing.names[names[2]]}</code>\n  Telegram sent the file without its thumbnail." in text
    assert "🖼 1 lost its thumbnail on the way and was sent again" in text and "🔁 1 had to be tried again" in text


async def test_one_file_alone_gets_no_report_and_is_not_counted_in_the_next_batch(ctx, finishing):
    await feed(ctx, episodes([1]))
    await until(lambda: ctx.app.queue.running == 0 and not ctx.app.queue.for_user(OWNER) and ctx.app._outcomes)
    await quiet()
    assert reports(ctx) == [] and ctx.app._outcomes == {}  # (its own message says it all, and it is forgotten)
    await feed(ctx, episodes([2, 3]), first_id=200)
    await until(lambda: reports(ctx))
    assert "all 2 files renamed" in reports(ctx)[0]


async def test_a_batch_that_is_not_sorted_is_reported_all_the_same(ctx, finishing):
    manual = await ctx.db.create_format(OWNER, "One Piece", template="One Piece - E{episode}")
    manual = await ctx.db.get_format(OWNER, manual["id"])
    for k in range(3):
        name = f"m{k}.mkv"
        ctx.app.queue.add(Job(id=ctx.app.queue.new_id(), user_id=OWNER, chat_id=OWNER, msg_id=9 + k, file_name=name, size=1, new_name=name, fmt=manual))
    await until(lambda: reports(ctx))
    await quiet()
    assert "all 3 files renamed" in reports(ctx)[0] and questions(ctx) == [] and ctx.app.sort_batches == {}  # (nothing to sort: only the report)


async def test_the_report_waits_while_more_files_are_on_their_way_in(ctx, finishing):
    await feed(ctx, episodes(range(1, 4)))
    ctx.app._intake[OWNER] = 1  # a fourth file is being received
    await until(lambda: len(ctx.app._outcomes.get(OWNER, [])) == 3 and not ctx.app.queue.for_user(OWNER))
    await quiet()
    assert reports(ctx) == [] and questions(ctx) == []
    del ctx.app._intake[OWNER]
    await feed(ctx, episodes([4]), first_id=200)  # it arrives, and is part of the same batch
    await until(lambda: reports(ctx))
    assert "all 4 files renamed" in reports(ctx)[0]


async def test_a_report_that_cannot_be_sent_does_not_stop_the_question_about_sorting(ctx, finishing, monkeypatch, caplog):
    send = ctx.client.send_message

    async def refusing(entity, text="", **kw):
        if "Batch finished" in text:
            raise ConnectionError("offline")
        return await send(entity, text, **kw)

    monkeypatch.setattr(ctx.client, "send_message", refusing)
    with caplog.at_level(logging.ERROR, logger="bot"):
        await feed(ctx, episodes(range(1, 4)))
        await until(lambda: questions(ctx))
    assert reports(ctx) == [] and "could not send the report on the batch" in caplog.text
    assert ctx.app._outcomes == {}  # (it is not sent again with the next batch)


async def test_each_user_gets_the_report_on_their_own_files(ctx, finishing):
    await feed(ctx, episodes(range(1, 4)), uid=OWNER)
    await feed(ctx, episodes(range(11, 13)), uid=ADMIN_CFG, first_id=300)
    await until(lambda: reports(ctx, OWNER) and reports(ctx, ADMIN_CFG))
    assert "all 3 files renamed" in reports(ctx, OWNER)[0] and "all 2 files renamed" in reports(ctx, ADMIN_CFG)[0]
    assert len(reports(ctx, OWNER)) == len(reports(ctx, ADMIN_CFG)) == 1


# ---------------------------------------------------------------------------
# what is remembered
# ---------------------------------------------------------------------------


async def test_only_the_latest_outcomes_are_remembered(ctx, monkeypatch):
    monkeypatch.setattr(appmod, "OUTCOMES_KEEP", 3)
    for n in range(5):
        job = SimpleNamespace(new_name=f"{n}.mkv", chat_id=OWNER, user_id=OWNER)
        await ctx.app._note_result(job, Result(state="done"))
    assert [o.name for o in ctx.app._outcomes[OWNER]] == ["2.mkv", "3.mkv", "4.mkv"]
