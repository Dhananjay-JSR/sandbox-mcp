"""In-flight job bookkeeping.

The repository is the durable record; this is the live one -- the asyncio tasks
that can still be cancelled and the per-experiment concurrency limits. Split
apart because a restarted server has rows but no tasks, and conflating the two
is how orphaned jobs get reported as RUNNING forever.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from ..models import Job


@dataclass(slots=True)
class RunningJob:
    job: Job
    task: asyncio.Task[Job]
    cancelled_by_user: bool = field(default=False)


class JobRegistry:
    """Tracks running jobs and caps concurrency per experiment."""

    def __init__(self, max_per_experiment: int) -> None:
        self._max_per_experiment = max_per_experiment
        self._running: dict[str, RunningJob] = {}
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._lock = asyncio.Lock()

    def semaphore(self, experiment_id: str) -> asyncio.Semaphore:
        if experiment_id not in self._semaphores:
            self._semaphores[experiment_id] = asyncio.Semaphore(self._max_per_experiment)
        return self._semaphores[experiment_id]

    async def add(self, job: Job, task: asyncio.Task[Job]) -> None:
        async with self._lock:
            self._running[job.id] = RunningJob(job=job, task=task)

    async def remove(self, job_id: str) -> None:
        async with self._lock:
            self._running.pop(job_id, None)

    def get(self, job_id: str) -> RunningJob | None:
        return self._running.get(job_id)

    def for_experiment(self, experiment_id: str) -> list[RunningJob]:
        return [r for r in self._running.values() if r.job.experiment_id == experiment_id]

    def mark_cancelled(self, job_id: str) -> None:
        if record := self._running.get(job_id):
            record.cancelled_by_user = True

    def was_cancelled_by_user(self, job_id: str) -> bool:
        record = self._running.get(job_id)
        return bool(record and record.cancelled_by_user)

    def discard_experiment(self, experiment_id: str) -> None:
        self._semaphores.pop(experiment_id, None)

    @property
    def running_count(self) -> int:
        return len(self._running)
