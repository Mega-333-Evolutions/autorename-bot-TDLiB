"""A clock for tests of anything that waits: it moves only when something sleeps, and by exactly as long as it slept."""
import asyncio
from typing import List

_real_sleep = asyncio.sleep  # taken when this module is loaded, before a test patches ``asyncio.sleep``


class Timeline:
    def __init__(self, now: float = 0.0):
        self.now = now
        self.waits: List[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float, *_args) -> None:
        self.waits.append(seconds)
        self.now += max(0.0, seconds)
        await _real_sleep(0)

    def waited(self) -> List[float]:
        """The waits that were more than none."""
        return [wait for wait in self.waits if wait]


async def settle(turns: int = 30) -> None:
    """Let every task that can run, run."""
    for _ in range(turns):
        await _real_sleep(0)
