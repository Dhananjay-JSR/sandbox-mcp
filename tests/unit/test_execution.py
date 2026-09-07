"""Job lifecycle: timeouts, cancellation, concurrency, persistence."""

from __future__ import annotations

import asyncio

import pytest

from sandbox_mcp.config import Settings
from sandbox_mcp.errors import JobNotFoundError
from sandbox_mcp.execution.executor import SandboxJobExecutor
from sandbox_mcp.execution.manager import ExecutionManager
from sandbox_mcp.experiments.repository import SQLiteRepository
from sandbox_mcp.models import (
    Experiment,
    JobStatus,
    MountStrategy,
    NetworkMode,
    ResourceLimits,
)
from sandbox_mcp.sandbox.interface import ExecOutcome, SandboxHandle
from tests.fakes import FakeSandboxBackend, outcome


@pytest.fixture
def experiment() -> Experiment:
    return Experiment(
        project_name="demo",
        base_image="alpine:3.20",
        network_mode=NetworkMode.NONE,
        mount_strategy=MountStrategy.COPY_TO_SANDBOX,
        resources=ResourceLimits(
            cpu_limit=1,
            memory_limit="1GB",
            memory_bytes=1024**3,
            timeout_seconds=30,
            pids_limit=100,
        ),
        container_id="fake0001",
    )


@pytest.fixture
def handle() -> SandboxHandle:
    return SandboxHandle(sandbox_id="fake0001", workspace="/workspace")


async def make_manager(
    settings: Settings, backend: FakeSandboxBackend, *experiments: Experiment
) -> tuple[ExecutionManager, SQLiteRepository]:
    repository = SQLiteRepository(settings.database_path)
    await repository.initialize()
    for record in experiments:
        await repository.save_experiment(record)
    return ExecutionManager(settings, SandboxJobExecutor(backend), repository), repository


class TestHappyPath:
    async def test_records_a_successful_command(
        self, settings: Settings, experiment: Experiment, handle: SandboxHandle
    ) -> None:
        backend = FakeSandboxBackend(default=outcome(0, "hello\n"))
        await backend.create(_spec(experiment), None)
        manager, repository = await make_manager(settings, backend, experiment)

        job = await manager.submit_and_wait(experiment, handle, "echo hello")

        assert job.status is JobStatus.COMPLETED
        assert job.exit_code == 0
        assert job.stdout == "hello\n"
        assert job.duration_ms is not None
        stored = await repository.get_job(job.id)
        assert stored is not None and stored.status is JobStatus.COMPLETED

    async def test_a_nonzero_exit_is_a_result_not_an_error(
        self, settings: Settings, experiment: Experiment, handle: SandboxHandle
    ) -> None:
        backend = FakeSandboxBackend(default=outcome(1, "", "boom\n"))
        await backend.create(_spec(experiment), None)
        manager, _ = await make_manager(settings, backend, experiment)

        job = await manager.submit_and_wait(experiment, handle, "false")

        assert job.status is JobStatus.COMPLETED
        assert job.exit_code == 1
        assert job.stderr == "boom\n"

    async def test_background_jobs_return_immediately(
        self, settings: Settings, experiment: Experiment, handle: SandboxHandle
    ) -> None:
        backend = FakeSandboxBackend(default=outcome(0, "done"))
        backend.command_delay = 0.05
        await backend.create(_spec(experiment), None)
        manager, _ = await make_manager(settings, backend, experiment)

        job = await manager.submit(experiment, handle, "slow")
        assert job.status in {JobStatus.PENDING, JobStatus.RUNNING}

        finished = await manager.wait_for(job.id)
        assert finished.status is JobStatus.COMPLETED
        assert finished.stdout == "done"


class TestTimeoutAndCancellation:
    async def test_timeout_is_distinct_from_failure(
        self, settings: Settings, experiment: Experiment, handle: SandboxHandle
    ) -> None:
        backend = FakeSandboxBackend(default=ExecOutcome(None, "partial", "", timed_out=True))
        await backend.create(_spec(experiment), None)
        manager, _ = await make_manager(settings, backend, experiment)

        job = await manager.submit_and_wait(experiment, handle, "sleep 999", timeout=1)

        assert job.status is JobStatus.TIMEOUT
        assert job.exit_code is None
        assert job.stdout == "partial"
        assert "timeout" in (job.error or "").lower()

    async def test_cancel_stops_a_running_job(
        self, settings: Settings, experiment: Experiment, handle: SandboxHandle
    ) -> None:
        backend = FakeSandboxBackend(default=outcome(0))
        backend.command_delay = 5
        await backend.create(_spec(experiment), None)
        manager, repository = await make_manager(settings, backend, experiment)

        job = await manager.submit(experiment, handle, "sleep 999")
        await asyncio.sleep(0.05)
        cancelled = await manager.cancel(job.id)

        assert cancelled.status is JobStatus.CANCELLED
        stored = await repository.get_job(job.id)
        assert stored is not None and stored.status is JobStatus.CANCELLED

    async def test_cancelling_a_finished_job_is_harmless(
        self, settings: Settings, experiment: Experiment, handle: SandboxHandle
    ) -> None:
        backend = FakeSandboxBackend(default=outcome(0, "quick"))
        await backend.create(_spec(experiment), None)
        manager, _ = await make_manager(settings, backend, experiment)

        job = await manager.submit_and_wait(experiment, handle, "echo quick")
        again = await manager.cancel(job.id)

        assert again.status is JobStatus.COMPLETED
        assert again.exit_code == 0

    async def test_cancelling_an_experiment_stops_all_of_its_jobs(
        self, settings: Settings, experiment: Experiment, handle: SandboxHandle
    ) -> None:
        backend = FakeSandboxBackend(default=outcome(0))
        backend.command_delay = 5
        await backend.create(_spec(experiment), None)
        manager, _ = await make_manager(settings, backend, experiment)

        jobs = [await manager.submit(experiment, handle, f"sleep {n}") for n in range(3)]
        await asyncio.sleep(0.05)

        assert await manager.cancel_experiment_jobs(experiment.id) == 3
        for job in jobs:
            assert (await manager.get_job(job.id)).status is JobStatus.CANCELLED


class TestFailureHandling:
    async def test_a_backend_error_fails_the_job_rather_than_the_server(
        self, settings: Settings, experiment: Experiment
    ) -> None:
        backend = FakeSandboxBackend()
        manager, _ = await make_manager(settings, backend, experiment)
        missing = SandboxHandle(sandbox_id="does-not-exist", workspace="/workspace")

        job = await manager.submit_and_wait(experiment, missing, "ls")

        assert job.status is JobStatus.FAILED
        assert "sandbox is gone" in (job.error or "")

    async def test_unknown_job_id_is_reported_clearly(self, settings: Settings) -> None:
        manager, _ = await make_manager(settings, FakeSandboxBackend())
        with pytest.raises(JobNotFoundError):
            await manager.get_job("job_missing")


class TestConcurrency:
    async def test_per_experiment_concurrency_is_capped(
        self, settings: Settings, experiment: Experiment, handle: SandboxHandle
    ) -> None:
        settings.max_concurrent_jobs_per_experiment = 2
        backend = FakeSandboxBackend(default=outcome(0))
        backend.command_delay = 0.1
        await backend.create(_spec(experiment), None)
        manager, _ = await make_manager(settings, backend, experiment)

        jobs = [await manager.submit(experiment, handle, f"cmd{n}") for n in range(4)]
        await asyncio.sleep(0.05)
        running = sum(
            1
            for job in jobs
            if (record := manager.registry.get(job.id)) and record.job.status is JobStatus.RUNNING
        )
        assert running <= 2

        for job in jobs:
            assert (await manager.wait_for(job.id)).status is JobStatus.COMPLETED

    async def test_jobs_from_different_experiments_do_not_block_each_other(
        self, settings: Settings, experiment: Experiment, handle: SandboxHandle
    ) -> None:
        settings.max_concurrent_jobs_per_experiment = 1
        backend = FakeSandboxBackend(default=outcome(0))
        backend.command_delay = 0.1
        await backend.create(_spec(experiment), None)
        manager, _ = await make_manager(settings, backend, experiment)

        other = experiment.model_copy(update={"id": "exp_other"})
        await _repository_of(manager).save_experiment(other)
        first = await manager.submit(experiment, handle, "a")
        second = await manager.submit(other, handle, "b")

        results = await asyncio.gather(manager.wait_for(first.id), manager.wait_for(second.id))
        assert all(job.status is JobStatus.COMPLETED for job in results)


def _repository_of(manager: ExecutionManager) -> SQLiteRepository:
    return manager._repository


def _spec(experiment: Experiment):
    from sandbox_mcp.models import ExperimentSpec

    return ExperimentSpec(
        project_name=experiment.project_name,
        base_image=experiment.base_image,
        network_mode=experiment.network_mode,
        mount_strategy=experiment.mount_strategy,
        resources=experiment.resources,
    )
