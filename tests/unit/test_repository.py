"""Persistence must survive a restart, and must never store a secret."""

from __future__ import annotations

from pathlib import Path

import pytest

from sandbox_mcp.config import Settings
from sandbox_mcp.experiments.repository import SQLiteRepository
from sandbox_mcp.models import (
    Artifact,
    Experiment,
    ExperimentStatus,
    Job,
    JobStatus,
    MountStrategy,
    NetworkMode,
    ResourceLimits,
    StateTransition,
)
from sandbox_mcp.security.filesystem import FileFingerprint


def make_experiment(**overrides: object) -> Experiment:
    defaults: dict[str, object] = {
        "project_name": "demo",
        "base_image": "node:22-slim",
        "network_mode": NetworkMode.NONE,
        "mount_strategy": MountStrategy.COPY_TO_SANDBOX,
        "resources": ResourceLimits(
            cpu_limit=2,
            memory_limit="2GB",
            memory_bytes=2 * 1024**3,
            timeout_seconds=120,
            pids_limit=512,
        ),
    }
    return Experiment(**{**defaults, **overrides})  # type: ignore[arg-type]


@pytest.fixture
async def repository(settings: Settings) -> SQLiteRepository:
    store = SQLiteRepository(settings.database_path)
    await store.initialize()
    return store


class TestExperiments:
    async def test_round_trips_every_field(self, repository: SQLiteRepository) -> None:
        experiment = make_experiment(
            objective="upgrade",
            project_path="/tmp/demo",
            container_id="abc123",
            snapshot_dir="/tmp/snap",
            environment_keys=["CI", "NODE_ENV"],
            setup_commands=["npm ci"],
            backend_metadata={"network": "sandbox-net"},
        )
        await repository.save_experiment(experiment)
        loaded = await repository.get_experiment(experiment.id)

        assert loaded is not None
        assert loaded.model_dump() == experiment.model_dump()

    async def test_save_is_an_upsert(self, repository: SQLiteRepository) -> None:
        experiment = make_experiment()
        await repository.save_experiment(experiment)
        experiment.status = ExperimentStatus.READY
        experiment.container_id = "later"
        await repository.save_experiment(experiment)

        loaded = await repository.get_experiment(experiment.id)
        assert loaded is not None
        assert loaded.status is ExperimentStatus.READY
        assert loaded.container_id == "later"
        assert len(await repository.list_experiments()) == 1

    async def test_missing_experiment_is_none(self, repository: SQLiteRepository) -> None:
        assert await repository.get_experiment("exp_nope") is None

    async def test_lists_filter_by_status(self, repository: SQLiteRepository) -> None:
        for status in (ExperimentStatus.READY, ExperimentStatus.READY, ExperimentStatus.DESTROYED):
            await repository.save_experiment(make_experiment(status=status))
        assert len(await repository.list_experiments(status=ExperimentStatus.READY)) == 2
        assert len(await repository.list_experiments()) == 3

    async def test_environment_values_are_never_persisted(
        self, repository: SQLiteRepository, settings: Settings
    ) -> None:
        """Only names are stored. The database is not a place secrets can settle."""
        experiment = make_experiment(environment_keys=["MY_API_KEY"])
        await repository.save_experiment(experiment)
        await repository.close()
        raw = Path(settings.database_path).read_bytes()
        assert b"MY_API_KEY" in raw
        assert b"the-actual-secret-value" not in raw


class TestJobs:
    async def test_round_trips(self, repository: SQLiteRepository) -> None:
        experiment = make_experiment()
        await repository.save_experiment(experiment)
        job = Job(experiment_id=experiment.id, command="npm test", timeout_seconds=60, kind="test")
        await repository.save_job(job)
        job.status = JobStatus.COMPLETED
        job.exit_code = 0
        job.stdout = "ok"
        await repository.save_job(job)

        loaded = await repository.get_job(job.id)
        assert loaded is not None
        assert loaded.status is JobStatus.COMPLETED
        assert loaded.stdout == "ok"
        assert loaded.kind == "test"

    async def test_lists_in_creation_order(self, repository: SQLiteRepository) -> None:
        experiment = make_experiment()
        await repository.save_experiment(experiment)
        for command in ["one", "two", "three"]:
            await repository.save_job(
                Job(experiment_id=experiment.id, command=command, timeout_seconds=10)
            )
        assert [job.command for job in await repository.list_jobs(experiment.id)] == [
            "one",
            "two",
            "three",
        ]

    async def test_restart_reconciliation_fails_ghost_jobs(
        self, repository: SQLiteRepository
    ) -> None:
        """After a crash there are rows but no tasks; RUNNING would be a lie."""
        experiment = make_experiment()
        await repository.save_experiment(experiment)
        running = Job(
            experiment_id=experiment.id,
            command="sleep 600",
            timeout_seconds=600,
            status=JobStatus.RUNNING,
        )
        done = Job(
            experiment_id=experiment.id,
            command="ls",
            timeout_seconds=10,
            status=JobStatus.COMPLETED,
        )
        await repository.save_job(running)
        await repository.save_job(done)

        assert await repository.mark_orphaned_jobs_failed() == 1
        reloaded = await repository.get_job(running.id)
        assert reloaded is not None
        assert reloaded.status is JobStatus.FAILED
        assert "restarted" in (reloaded.error or "")
        unchanged = await repository.get_job(done.id)
        assert unchanged is not None and unchanged.status is JobStatus.COMPLETED


class TestAuxiliaryTables:
    async def test_transitions_are_appended_in_order(self, repository: SQLiteRepository) -> None:
        experiment = make_experiment()
        await repository.save_experiment(experiment)
        for from_status, to_status in [
            (None, ExperimentStatus.CREATING),
            (ExperimentStatus.CREATING, ExperimentStatus.READY),
            (ExperimentStatus.READY, ExperimentStatus.RUNNING),
        ]:
            await repository.record_transition(
                StateTransition(
                    experiment_id=experiment.id, from_status=from_status, to_status=to_status
                )
            )
        history = await repository.list_transitions(experiment.id)
        assert [t.to_status.value for t in history] == ["CREATING", "READY", "RUNNING"]
        assert history[0].from_status is None

    async def test_baseline_round_trips(self, repository: SQLiteRepository) -> None:
        experiment = make_experiment()
        await repository.save_experiment(experiment)
        manifest = {
            "a.txt": FileFingerprint(size=10, digest="a" * 64),
            "link": FileFingerprint(size=0, digest="b" * 64, kind="link"),
        }
        await repository.save_baseline(experiment.id, manifest)
        assert await repository.get_baseline(experiment.id) == manifest

    async def test_change_stats_survive_teardown(self, repository: SQLiteRepository) -> None:
        """Recorded before destroy, so a dead experiment stays comparable."""
        experiment = make_experiment()
        await repository.save_experiment(experiment)
        await repository.save_change_stats(experiment.id, {"files_changed": 3, "insertions": 9})
        experiment.status = ExperimentStatus.DESTROYED
        await repository.save_experiment(experiment)
        assert await repository.get_change_stats(experiment.id) == {
            "files_changed": 3,
            "insertions": 9,
        }

    async def test_artifacts_round_trip(self, repository: SQLiteRepository) -> None:
        experiment = make_experiment()
        await repository.save_experiment(experiment)
        artifact = Artifact(
            experiment_id=experiment.id,
            sandbox_path="/workspace/dist/app.js",
            host_path="/tmp/art/app.js",
            size_bytes=42,
            sha256="c" * 64,
        )
        await repository.save_artifact(artifact)
        assert (await repository.list_artifacts(experiment.id))[0].sha256 == "c" * 64
        assert (await repository.get_artifact(artifact.id)) is not None


async def test_state_survives_reopening_the_database(settings: Settings) -> None:
    store = SQLiteRepository(settings.database_path)
    await store.initialize()
    experiment = make_experiment(status=ExperimentStatus.READY)
    await store.save_experiment(experiment)
    await store.close()

    reopened = SQLiteRepository(settings.database_path)
    await reopened.initialize()
    loaded = await reopened.get_experiment(experiment.id)
    assert loaded is not None and loaded.status is ExperimentStatus.READY
    await reopened.close()
