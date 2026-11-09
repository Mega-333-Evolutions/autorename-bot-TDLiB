import asyncio
import time
from types import SimpleNamespace

from bot.progress import ProgressReporter
from bot.queue import Job, JobQueue
from bot.tg import errors


def job(queue, user=1, name="a.mkv"):
    return Job(id=queue.new_id(), user_id=user, chat_id=user, msg_id=1, file_name=name, size=1, new_name=name)


async def test_runs_jobs_fifo_with_single_worker():
    order = []
    gate = asyncio.Event()

    async def runner(j):
        order.append(("start", j.id))
        await gate.wait()
        order.append(("end", j.id))

    q = JobQueue(runner, workers=1)
    q.start()
    a, b, c = job(q), job(q), job(q)
    assert q.add(a) == 0 and q.add(b) == 1 and q.add(c) == 2
    await asyncio.sleep(0.05)
    assert q.running == 1 and q.waiting == 2
    assert q.position(a) == 0 and q.position(b) == 1 and q.position(c) == 2
    gate.set()
    await asyncio.sleep(0.1)
    assert order == [("start", 1), ("end", 1), ("start", 2), ("end", 2), ("start", 3), ("end", 3)]
    assert q.jobs == {}  # finished jobs are forgotten
    await q.stop()


async def test_two_workers_run_in_parallel():
    running = []
    peak = 0
    gate = asyncio.Event()

    async def runner(j):
        nonlocal peak
        running.append(j.id)
        peak = max(peak, len(running))
        await gate.wait()
        running.remove(j.id)

    q = JobQueue(runner, workers=2)
    q.start()
    for _ in range(4):
        q.add(job(q))
    await asyncio.sleep(0.05)
    assert q.running == 2 and peak == 2
    gate.set()
    await asyncio.sleep(0.1)
    await q.stop()


async def test_cancel_queued_job_skips_it_and_calls_back():
    done, cancelled = [], []
    gate = asyncio.Event()

    async def runner(j):
        done.append(j.id)
        await gate.wait()

    async def on_cancelled(j):
        cancelled.append(j.id)

    q = JobQueue(runner, workers=1, on_cancelled=on_cancelled)
    q.start()
    a, b, c = job(q), job(q), job(q)
    for j in (a, b, c):
        q.add(j)
    await asyncio.sleep(0.05)
    assert q.cancel(b.id) is True
    assert q.cancel(b.id) is False  # already gone
    await asyncio.sleep(0.05)
    assert cancelled == [b.id] and b.state == "cancelled"
    gate.set()
    await asyncio.sleep(0.1)
    assert done == [a.id, c.id]
    await q.stop()


async def test_cancel_running_job_sets_its_event_and_cancel_is_scoped_to_user():
    seen = {}
    started = asyncio.Event()

    async def runner(j):
        started.set()
        await j.cancel.wait()
        seen["cancelled"] = True

    q = JobQueue(runner, workers=1)
    q.start()
    a = job(q, user=5)
    q.add(a)
    await started.wait()
    assert q.cancel(a.id, user_id=99) is False  # someone else's job
    assert q.cancel(a.id, user_id=5) is True
    await asyncio.sleep(0.05)
    assert seen == {"cancelled": True}
    await q.stop()


async def test_cancel_user_and_cancel_all():
    gate = asyncio.Event()

    async def runner(j):
        await gate.wait()

    q = JobQueue(runner, workers=1)
    q.start()
    jobs = [job(q, user=1), job(q, user=1), job(q, user=2), job(q, user=2)]
    for j in jobs:
        q.add(j)
    await asyncio.sleep(0.05)
    assert q.cancel_user(1) == 2
    assert [j.id for j in q.for_user(2)] == [jobs[2].id, jobs[3].id]
    assert q.cancel_all() == 2
    gate.set()
    await q.stop()


async def test_runner_crash_does_not_kill_the_worker():
    ran = []

    async def runner(j):
        ran.append(j.id)
        if j.id == 1:
            raise RuntimeError("boom")

    q = JobQueue(runner, workers=1)
    q.start()
    q.add(job(q))
    q.add(job(q))
    await asyncio.sleep(0.1)
    assert ran == [1, 2]
    await q.stop()


def ordered_job(queue, order, user=1):
    return Job(id=queue.new_id(), user_id=user, chat_id=user, msg_id=order, file_name=f"{order}.mkv", size=1,
               new_name=f"{order}.mkv", order=order)


async def idle_queue(runner, **kw):
    """A started queue whose worker is already waiting for work, like the real bot between files."""
    q = JobQueue(runner, workers=1, **kw)
    q.start()
    await asyncio.sleep(0.02)
    return q


async def test_a_batch_queued_out_of_order_runs_in_message_order():
    ran = []

    async def runner(j):
        ran.append(j.order)

    q = await idle_queue(runner, settle=0.1)
    for order in (1146, 1147, 1145):  # the order in which three concurrent handlers happened to finish
        q.add(ordered_job(q, order))
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.4)
    assert ran == [1145, 1146, 1147]
    await q.stop()


async def test_without_a_settle_time_the_first_queued_job_starts_at_once():
    ran = []

    async def runner(j):
        ran.append(j.order)

    q = await idle_queue(runner)
    for order in (1146, 1147, 1145):
        q.add(ordered_job(q, order))
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.1)
    assert ran[0] == 1146  # which is why the bot uses a settle time
    await q.stop()


async def test_jobs_queued_while_busy_are_picked_oldest_message_first():
    ran, gate = [], asyncio.Event()

    async def runner(j):
        ran.append(j.order)
        if j.order == 1:
            await gate.wait()

    q = await idle_queue(runner)
    q.add(ordered_job(q, 1))
    await asyncio.sleep(0.02)
    for order in (30, 20, 25):
        q.add(ordered_job(q, order))
    gate.set()
    await asyncio.sleep(0.1)
    assert ran == [1, 20, 25, 30]
    await q.stop()


async def test_an_idle_worker_waits_for_the_settle_time_before_starting():
    ran = []

    async def runner(j):
        ran.append(j.id)

    q = await idle_queue(runner, settle=0.3)
    q.add(job(q))
    await asyncio.sleep(0.1)
    assert ran == [] and q.waiting == 1
    await asyncio.sleep(0.5)
    assert ran == [1]
    await q.stop()


async def test_every_new_job_extends_the_wait_so_a_slow_batch_is_still_grouped():
    ran = []

    async def runner(j):
        ran.append(j.order)

    q = await idle_queue(runner, settle=0.3)
    for order in (3, 2, 1):  # each arrives 0.2 s after the previous one: more than one settle period in total
        q.add(ordered_job(q, order))
        await asyncio.sleep(0.2)
    await asyncio.sleep(0.6)
    assert ran == [1, 2, 3]
    await q.stop()


async def test_a_steady_stream_cannot_hold_the_worker_back_forever():
    started_at, last_add = [], []

    async def runner(j):
        started_at.append(time.monotonic())

    q = await idle_queue(runner, settle=0.15)  # capped at SETTLE_CAP * 0.15 s
    for order in range(30, 0, -1):
        q.add(ordered_job(q, order))
        last_add.append(time.monotonic())
        await asyncio.sleep(0.05)
    assert started_at and started_at[0] < last_add[-1] - 0.3  # it started well before the stream stopped
    await q.stop()


async def test_position_and_listing_follow_message_order():
    async def runner(j):
        pass

    q = JobQueue(runner, workers=1)  # never started: everything stays queued
    a, b, c = ordered_job(q, 30), ordered_job(q, 10), ordered_job(q, 20)
    for j in (a, b, c):
        q.add(j)
    assert [q.position(j) for j in (b, c, a)] == [1, 2, 3]
    assert [j.order for j in q.for_user(1)] == [10, 20, 30]


async def test_cancelling_during_the_settle_time_skips_the_job():
    ran = []

    async def runner(j):
        ran.append(j.id)

    q = await idle_queue(runner, settle=0.2)
    a, b = job(q), job(q)
    q.add(a)
    q.add(b)
    await asyncio.sleep(0.05)
    assert q.cancel(a.id)
    await asyncio.sleep(0.6)
    assert ran == [b.id]
    await q.stop()


# ---------------------------------------------------------------------------
# progress reporter
# ---------------------------------------------------------------------------


class FakeClient:
    def __init__(self):
        self.edits = []
        self.error = None

    async def edit_message(self, message, text, buttons=None, parse_mode=None):
        if self.error:
            raise self.error
        self.edits.append((text, buttons))


async def test_reporter_throttles_and_shows_stage_and_cancel_button():
    client = FakeClient()
    r = ProgressReporter(client, SimpleNamespace(id=1), 7, "My <File>.mkv", interval=60)
    r.set_stage("⬇️ Downloading", 1000)
    await asyncio.sleep(0.02)
    assert len(client.edits) == 1
    text, buttons = client.edits[0]
    assert "Downloading" in text and "My &lt;File&gt;.mkv" in text
    assert buttons[0][0].type.data == b"job:x:7"

    for cur in range(1, 500, 50):
        r(cur, 1000)  # within the throttle window: no more edits
    await asyncio.sleep(0.02)
    assert len(client.edits) == 1

    r(1000, 1000)  # completion always shows
    await asyncio.sleep(0.02)
    assert len(client.edits) == 2 and "100.0%" in client.edits[-1][0]


async def test_stage_change_is_shown_even_while_an_edit_is_in_flight():
    class SlowClient(FakeClient):
        async def edit_message(self, message, text, buttons=None, parse_mode=None):
            await asyncio.sleep(0.05)
            await super().edit_message(message, text, buttons=buttons, parse_mode=parse_mode)

    client = SlowClient()
    r = ProgressReporter(client, None, 1, "x", interval=0)
    r.set_stage("⬇️ Downloading", 100)
    r(100, 100)  # dropped: the first edit is still running
    r.set_stage("⚙️ Applying metadata", 0)  # must not be dropped
    await r.finish("✅ Done")
    stages = [t for t, _b in client.edits]
    assert "Downloading" in stages[0] and "Applying metadata" in stages[1] and stages[-1] == "✅ Done"
    assert len(stages) == 3


async def test_reporter_render_numbers():
    r = ProgressReporter(FakeClient(), None, 1, "x", interval=4)
    r.stage = "⬆️ Uploading"
    r._speed = 1024 * 1024
    text = r.render(512 * 1024 * 1024, 1024 * 1024 * 1024)
    assert "50.0%" in text and "512.00 MB / 1.00 GB" in text and "1.00 MB/s" in text and "ETA 8:32" in text
    assert "Working" in r.render(0, 0)


async def test_reporter_shows_the_dc_beside_the_stage_it_was_given_for_and_only_there():
    client = FakeClient()
    r = ProgressReporter(client, None, 1, "x", interval=0)
    r.set_stage("⬇️ Downloading", 1000, dc="DC 4")
    await asyncio.sleep(0.02)
    assert client.edits[-1][0].startswith("<b>⬇️ Downloading</b> · DC 4\n<code>x</code>\n")
    r(500, 1000)  # the numbers that follow keep it
    await asyncio.sleep(0.02)
    assert "50.0%" in client.edits[-1][0] and client.edits[-1][0].startswith("<b>⬇️ Downloading</b> · DC 4\n")
    r.set_stage("⬆️ Uploading", 1000)  # the next stage does not come from there
    await asyncio.sleep(0.02)
    assert client.edits[-1][0].startswith("<b>⬆️ Uploading</b>\n") and "DC" not in client.edits[-1][0]


async def test_reporter_survives_flood_wait_and_message_errors():
    client = FakeClient()
    r = ProgressReporter(client, None, 1, "x", interval=0)
    client.error = errors.FloodWaitError(3)
    r.set_stage("a", 10)
    await asyncio.sleep(0.02)
    assert r._muted_until > 0  # backed off instead of crashing
    client.error = errors.MessageNotModifiedError("message is not modified")
    r._muted_until = 0
    r(5, 10)
    await asyncio.sleep(0.02)
    client.error = RuntimeError("deleted")
    r(10, 10)
    await asyncio.sleep(0.02)


async def test_reporter_finish_removes_buttons():
    client = FakeClient()
    r = ProgressReporter(client, None, 1, "x")
    await r.finish("✅ Done")
    assert client.edits[-1] == ("✅ Done", None)
