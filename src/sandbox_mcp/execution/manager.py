"""Job lifecycle.

Every command becomes a :class:`Job` backed by an asyncio task, whether the
caller waits for it or not. That uniformity is what makes ``execute_experiment``
and ``get_job_result`` return the same shape, and what makes cancellation work
identically in both paths.

    submit ─► PENDING ─► RUNNING ─┬─► COMPLETED   (process exited, any code)
                                  ├─► TIMEOUT     (we killed it)
                                  ├─► CANCELLED   (caller killed it)
                                  └─► FAILED      (the sandbox itself broke)
"""

from __future__ import annotations

import asyncio
import contextlib

from ..config import Settings
from ..errors import JobNotFoundError, SandboxMCPError
from ..experiments.repository import ExperimentRepository
from ..logging import get_logger
from ..models import Experiment, Job, JobStatus, utcnow
from ..sandbox.interface import SandboxHandle
from .executor import JobExecutor
from .jobs import JobRegistry

log = get_logger(__name__)


class ExecutionManager:
    """Submits, tracks, times out and cancels commands."""

    def __init__(
        self,
        settings: Settings,
        executor: JobExecutor,
        repository: ExperimentRepository,
    ) -> None:
        self._settings = settings
        self._executor = executor
        self._repository = repository
        self._registry = JobRegistry(settings.max_concurrent_jobs_per_experiment)

    @property
    def registry(self) -> JobRegistry:
        return self._registry

    # --- submission -------------------------------------------------------

    async def submit(
        self,
        experiment: Experiment,
        handle: SandboxHandle,
        command: str,
        timeout: int | None = None,
        workdir: str | None = None,
        kind: str = "command",
    ) -> Job:
        """Queue a command and return immediately with a PENDING/RUNNING job."""
        job = Job(
            experiment_id=experiment.id,
            command=command,
            workdir=workdir or experiment.workspace_path,
            timeout_seconds=timeout or experiment.resources.timeout_seconds,
            kind=kind,  # type: ignore[arg-type]
        )
        await self._repository.save_job(job)

        task = asyncio.create_task(self._run(job, handle), name=f"job-{job.id}")
        await self._registry.add(job, task)
        log.info(
            "job_submitted",
            event_source="execution",
            operation="job.submit",
            experiment_id=experiment.id,
            job_id=job.id,
            command=command,
            timeout_seconds=job.timeout_seconds,
        )
        return job

    async def submit_and_wait(
        self,
        experiment: Experiment,
        handle: SandboxHandle,
        command: str,
        timeout: int | None = None,
        workdir: str | None = None,
        kind: str = "command",
    ) -> Job:
        """Queue a command and wait for it. The job's own timeout still applies,
        so this cannot hang the MCP request indefinitely."""
        job = await self.submit(experiment, handle, command, timeout, workdir, kind)
        return await self.wait_for(job.id)

    async def wait_for(self, job_id: str) -> Job:
        record = self._registry.get(job_id)
        if record is None:
            return await self.get_job(job_id)
        try:
            return await asyncio.shield(record.task)
        except asyncio.CancelledError:
            # The job was cancelled out from under us; report its final state.
            return await self.get_job(job_id)

    # --- the run loop -----------------------------------------------------

    async def _run(self, job: Job, handle: SandboxHandle) -> Job:
        semaphore = self._registry.semaphore(job.experiment_id)
        try:
            async with semaphore:
                job.status = JobStatus.RUNNING
                job.started_at = utcnow()
                await self._repository.save_job(job)
                outcome = await self._executor.run(job, handle)

            job.stdout = outcome.stdout
            job.stderr = outcome.stderr
            job.stdout_truncated = outcome.stdout_truncated
            job.stderr_truncated = outcome.stderr_truncated
            if outcome.timed_out:
                job.status = JobStatus.TIMEOUT
                job.error = f"Command exceeded its {job.timeout_seconds}s timeout and was killed."
            else:
                job.status = JobStatus.COMPLETED
                job.exit_code = outcome.exit_code

        except asyncio.CancelledError:
            job.status = JobStatus.CANCELLED
            job.error = "Cancelled."
            job.finished_at = utcnow()
            with contextlib.suppress(Exception):
                await self._repository.save_job(job)
            await self._registry.remove(job.id)
            log.info(
                "job_cancelled",
                event_source="execution",
                operation="job.run",
                experiment_id=job.experiment_id,
                job_id=job.id,
                duration_ms=job.duration_ms,
            )
            raise

        except SandboxMCPError as exc:
            job.status = JobStatus.FAILED
            job.error = str(exc)
        except Exception as exc:  # never let a backend bug wedge the job record
            job.status = JobStatus.FAILED
            job.error = f"[INTERNAL_ERROR] {type(exc).__name__}: {exc}"
            log.exception(
                "job_crashed",
                operation="job.run",
                experiment_id=job.experiment_id,
                job_id=job.id,
            )

        job.finished_at = utcnow()
        await self._repository.save_job(job)
        await self._registry.remove(job.id)
        log.info(
            "command_completed",
            event_source="execution",
            operation="job.run",
            experiment_id=job.experiment_id,
            job_id=job.id,
            status=job.status.value,
            exit_code=job.exit_code,
            duration_ms=job.duration_ms,
        )
        return job

    # --- queries and control ---------------------------------------------

    async def get_job(self, job_id: str) -> Job:
        if record := self._registry.get(job_id):
            return record.job
        job = await self._repository.get_job(job_id)
        if job is None:
            raise JobNotFoundError(f"No job with id {job_id}.", job_id=job_id)
        return job

    async def cancel(self, job_id: str) -> Job:
        """Cancel a running job. Terminal jobs are returned untouched, so
        calling this twice is harmless."""
        job = await self.get_job(job_id)
        if job.status.is_terminal:
            return job

        record = self._registry.get(job_id)
        if record is None:
            job.status = JobStatus.CANCELLED
            job.error = "Cancelled; no live task was attached."
            job.finished_at = utcnow()
            await self._repository.save_job(job)
            return job

        self._registry.mark_cancelled(job_id)
        record.task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await record.task
        return await self.get_job(job_id)

    async def cancel_experiment_jobs(self, experiment_id: str) -> int:
        """Stop everything still running for an experiment. Used by destroy."""
        records = self._registry.for_experiment(experiment_id)
        for record in records:
            self._registry.mark_cancelled(record.job.id)
            record.task.cancel()
        for record in records:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await record.task
        self._registry.discard_experiment(experiment_id)
        return len(records)

    async def list_jobs(self, experiment_id: str) -> list[Job]:
        return await self._repository.list_jobs(experiment_id)
