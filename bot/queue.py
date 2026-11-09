"""FIFO job queue with a small pool of workers."""
from __future__ import annotations

import asyncio
import itertools
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

log = logging.getLogger(__name__)


@dataclass
class Job:
    id: int
    user_id: int
    chat_id: int
    msg_id: int
    file_name: str
    size: int
    new_name: str
    fmt: Optional[Dict[str, Any]] = None  # the saved format (auto or manual) this job uses, a snapshot taken when it was queued; None: no auto format
    notes: List[str] = field(default_factory=list)  # warnings shown with the queued / finished message
    order: int = 0  # id of the message that triggered the job: files are processed oldest message first
    status_msg: Any = None
    state: str = "queued"  # queued | running | done | failed | cancelled
    cancel: asyncio.Event = field(default_factory=asyncio.Event)
    created: float = field(default_factory=time.time)
    started: float = 0.0


Runner = Callable[[Job], Awaitable[None]]

SETTLE_CAP = 4  # an idle worker never waits longer than this many settle periods in a row


def _order_key(job: Job) -> tuple:
    return (job.order, job.id)


class JobQueue:
    """Jobs run oldest-message-first by a small pool of workers.

    Messages that arrive together are handled concurrently, so jobs are not necessarily added in the
    order the files were sent.  Picking by message id (not by insertion order) and letting an idle worker wait
    until nothing new was queued for ``settle`` seconds (at most ``SETTLE_CAP`` times that) keeps a forwarded
    batch in its original order.
    """

    def __init__(self, runner: Runner, workers: int = 1, on_cancelled: Optional[Runner] = None, settle: float = 0.0):
        self._runner = runner
        self._on_cancelled = on_cancelled
        self._workers = max(1, workers)
        self._settle = max(0.0, settle)
        self._wake = asyncio.Event()
        self._ids = itertools.count(1)
        self._tasks: List[asyncio.Task] = []
        self.jobs: Dict[int, Job] = {}

    def new_id(self) -> int:
        return next(self._ids)

    def start(self) -> None:
        for _ in range(self._workers):
            self._tasks.append(asyncio.ensure_future(self._worker()))

    async def stop(self) -> None:
        for job in list(self.jobs.values()):
            job.cancel.set()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    def add(self, job: Job) -> int:
        """Queue a job; returns how many jobs are ahead of it (running + waiting)."""
        ahead = self.running + self.waiting
        self.jobs[job.id] = job
        self._wake.set()
        return ahead

    @property
    def running(self) -> int:
        return sum(1 for j in self.jobs.values() if j.state == "running")

    @property
    def waiting(self) -> int:
        return sum(1 for j in self.jobs.values() if j.state == "queued")

    def position(self, job: Job) -> int:
        """0 = running now, 1 = next up, ..."""
        if job.state == "running":
            return 0
        waiting = sorted((j for j in self.jobs.values() if j.state == "queued"), key=_order_key)
        try:
            return waiting.index(job) + 1
        except ValueError:
            return 0

    def for_user(self, user_id: int) -> List[Job]:
        return sorted((j for j in self.jobs.values() if j.user_id == user_id), key=_order_key)

    def cancel(self, job_id: int, user_id: Optional[int] = None) -> bool:
        job = self.jobs.get(job_id)
        if job is None or (user_id is not None and job.user_id != user_id):
            return False
        return self._cancel_job(job)

    def cancel_user(self, user_id: int) -> int:
        return sum(self._cancel_job(j) for j in self.for_user(user_id))

    def cancel_all(self) -> int:
        return sum(self._cancel_job(j) for j in list(self.jobs.values()))

    def _cancel_job(self, job: Job) -> bool:
        if job.state in ("done", "failed", "cancelled") or job.cancel.is_set():
            return False  # finished, or a cancel is already on its way
        job.cancel.set()
        if job.state == "queued":
            job.state = "cancelled"
            self.jobs.pop(job.id, None)
            if self._on_cancelled is not None:
                asyncio.ensure_future(self._safe(self._on_cancelled, job))
        return True

    async def _safe(self, fn: Runner, job: Job) -> None:
        try:
            await fn(job)
        except Exception:  # noqa: BLE001
            log.exception("job callback failed")

    def _claim(self) -> Optional[Job]:
        """Take the oldest queued job (no await in between, so two workers never take the same one)."""
        queued = [j for j in self.jobs.values() if j.state == "queued"]
        if not queued:
            return None
        job = min(queued, key=_order_key)
        job.state = "running"
        job.started = time.time()
        return job

    async def _settle_batch(self) -> None:
        """The rest of a forwarded batch may still be on its way: wait until nothing new was queued for a while."""
        if not self._settle:
            return
        deadline = time.monotonic() + self._settle * SETTLE_CAP
        while time.monotonic() < deadline:
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._settle)
            except asyncio.TimeoutError:
                return

    async def _next(self) -> Job:
        while True:
            job = self._claim()
            if job is not None:
                return job
            self._wake.clear()
            await self._wake.wait()
            await self._settle_batch()

    async def _worker(self) -> None:
        while True:
            job = await self._next()
            try:
                await self._runner(job)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the runner reports its own failures
                log.exception("job %s crashed", job.id)
                job.state = "failed"
            finally:
                if job.state == "running":
                    job.state = "done"
                self.jobs.pop(job.id, None)
