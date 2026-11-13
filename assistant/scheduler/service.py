"""A small in-process scheduler: runs registered jobs on every tick.

All state lives in the database (reminders, held notifications, last briefing date), so
nothing is lost on restart and anything missed while the server was down runs on the
first tick.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from ..core.config import AppConfig
from ..core.models import utcnow

log = logging.getLogger(__name__)

Job = Callable[[datetime], Awaitable[Any]]


class Scheduler:
    def __init__(self, get_config: Callable[[], AppConfig], clock: Callable[[], datetime] = utcnow):
        self._get_config = get_config
        self._clock = clock
        self._jobs: list[tuple[str, Job]] = []
        self._task: asyncio.Task[None] | None = None

    def add_job(self, name: str, job: Job) -> None:
        self._jobs.append((name, job))

    async def tick(self, now: datetime | None = None) -> None:
        """Run every job once. A failing job never stops the others."""
        now = now or self._clock()
        for name, job in self._jobs:
            try:
                await job(now)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("scheduler job %s failed", name)

    async def _loop(self) -> None:
        while True:
            await self.tick()
            await asyncio.sleep(max(1.0, self._get_config().scheduler.tick_seconds))

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
