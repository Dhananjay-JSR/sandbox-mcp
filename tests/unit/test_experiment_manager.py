"""The domain core: creation order, isolation guarantees, teardown, comparison."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sandbox_mcp.app import SandboxMCPApp
from sandbox_mcp.config import Settings
from sandbox_mcp.errors import (
    CapacityError,
    ExperimentDestroyedError,
    ExperimentNotFoundError,
    ImageError,
    InvalidProjectPathError,
    UnauthorizedPathError,
)
from sandbox_mcp.models import ExperimentStatus, JobStatus
from tests.fakes import FakeSandboxBackend, outcome


class TestCreation:
    async def test_creates_a_ready_experiment(self, app: SandboxMCPApp, project: Path) -> None:
        result = await app.experiments.create(
            project_path=str(project), base_image="node:22-slim", objective="upgrade"
        )
        assert result.status is ExperimentStatus.READY
        assert result.experiment_id.startswith("exp_")
        assert result.files_copied == 3
        assert result.network_mode.value == "none"

    async def test_the_host_project_is_never_modified(
        self, app: SandboxMCPApp, project: Path, backend: FakeSandboxBackend
    ) -> None:
        """The core promise. Everything the sandbox writes stays in the sandbox."""
        before = {
            p.relative_to(project).as_posix(): p.read_bytes()
            for p in project.rglob("*")
            if p.is_file()
        }

        result = await app.experiments.create(project_path=str(project))
        experiment = await app.experiments.get(result.experiment_id)
        handle = app.experiments.handle_for(experiment)
        backend.files(handle)["src/index.js"] = b"CLOBBERED"
        backend.files(handle)["brand-new.js"] = b"new"

        after = {
            p.relative_to(project).as_posix(): p.read_bytes()
            for p in project.rglob("*")
            if p.is_file()
        }
        assert before == after

    async def test_secrets_are_withheld_and_reported(
        self, app: SandboxMCPApp, project: Path, backend: FakeSandboxBackend
    ) -> None:
        result = await app.experiments.create(project_path=str(project))
        experiment = await app.experiments.get(result.experiment_id)
        files = backend.files(app.experiments.handle_for(experiment))
        assert ".env" not in files
        assert "id_rsa" not in files
        assert any("sensitive" in warning for warning in result.warnings)

    async def test_records_environment_names_but_not_values(
        self, app: SandboxMCPApp, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CI", "true")
        result = await app.experiments.create(environment_allowlist=["CI", "NODE_ENV=test"])
        experiment = await app.experiments.get(result.experiment_id)
        assert experiment.environment_keys == ["CI", "NODE_ENV"]
        assert "true" not in json.dumps(experiment.model_dump(mode="json"))

    async def test_captures_a_baseline_for_diffing(self, app: SandboxMCPApp, project: Path) -> None:
        result = await app.experiments.create(project_path=str(project))
        baseline = await app.repository.get_baseline(result.experiment_id)
        assert baseline is not None
        assert set(baseline) == {"package.json", "src/index.js", "src/util.js"}

    async def test_records_the_state_history(self, app: SandboxMCPApp, project: Path) -> None:
        result = await app.experiments.create(project_path=str(project))
        history = await app.repository.list_transitions(result.experiment_id)
        assert [t.to_status.value for t in history] == ["CREATING", "READY"]

    async def test_rejects_an_image_outside_the_allowlist(self, app: SandboxMCPApp) -> None:
        with pytest.raises(ImageError):
            await app.experiments.create(base_image="evil.io/miner:latest")

    async def test_rejects_a_missing_project(self, app: SandboxMCPApp, tmp_path: Path) -> None:
        with pytest.raises(InvalidProjectPathError):
            await app.experiments.create(project_path=str(tmp_path / "nope"))

    async def test_rejects_a_protected_project_path(self, app: SandboxMCPApp) -> None:
        with pytest.raises(UnauthorizedPathError):
            await app.experiments.create(project_path=str(Path.home()))

    async def test_enforces_the_concurrent_experiment_ceiling(self, app: SandboxMCPApp) -> None:
        for _ in range(app.settings.max_concurrent_experiments):
            await app.experiments.create()
        with pytest.raises(CapacityError):
            await app.experiments.create()

    async def test_a_failed_start_cleans_up_and_records_why(
        self, settings: Settings, project: Path
    ) -> None:
        from sandbox_mcp.errors import SandboxStartupError
        from sandbox_mcp.experiments.repository import SQLiteRepository

        broken = FakeSandboxBackend(fail_create=True)
        application = SandboxMCPApp.build(
            settings=settings, backend=broken, repository=SQLiteRepository(settings.database_path)
        )
        await application.startup()

        with pytest.raises(SandboxStartupError):
            await application.experiments.create(project_path=str(project))

        experiments = await application.repository.list_experiments()
        assert experiments[0].status is ExperimentStatus.FAILED
        assert experiments[0].error
        assert not list((settings.sandboxes_dir).glob("*/workspace"))
        await application.shutdown()


class TestSetupCommands:
    async def test_runs_them_in_order(
        self, app: SandboxMCPApp, backend: FakeSandboxBackend
    ) -> None:
        result = await app.experiments.create(setup_commands=["one", "two"])
        assert result.status is ExperimentStatus.READY
        assert backend.commands[-2:] == ["one", "two"]
        assert [job.status for job in result.setup_jobs] == [JobStatus.COMPLETED] * 2

    async def test_stops_at_the_first_failure_and_marks_the_experiment_failed(
        self, settings: Settings
    ) -> None:
        from sandbox_mcp.experiments.repository import SQLiteRepository

        backend = FakeSandboxBackend(responses={"two": outcome(1, "", "nope")})
        application = SandboxMCPApp.build(
            settings=settings, backend=backend, repository=SQLiteRepository(settings.database_path)
        )
        await application.startup()

        result = await application.experiments.create(setup_commands=["one", "two", "three"])

        assert result.status is ExperimentStatus.FAILED
        assert len(result.setup_jobs) == 2
        assert "three" not in backend.commands
        assert any("Setup command failed" in warning for warning in result.warnings)
        await application.shutdown()


class TestExecution:
    async def test_runs_a_command_and_settles_the_state(self, app: SandboxMCPApp) -> None:
        created = await app.experiments.create()
        job = await app.experiments.execute(created.experiment_id, "echo hi")
        assert job.status is JobStatus.COMPLETED
        experiment = await app.experiments.get(created.experiment_id)
        assert experiment.status is ExperimentStatus.READY

    async def test_a_failing_command_leaves_the_experiment_failed_but_usable(
        self, settings: Settings
    ) -> None:
        from sandbox_mcp.experiments.repository import SQLiteRepository

        backend = FakeSandboxBackend(responses={"bad": outcome(2, "", "err")})
        application = SandboxMCPApp.build(
            settings=settings, backend=backend, repository=SQLiteRepository(settings.database_path)
        )
        await application.startup()
        created = await application.experiments.create()

        await application.experiments.execute(created.experiment_id, "bad")
        assert (
            await application.experiments.get(created.experiment_id)
        ).status is ExperimentStatus.FAILED

        follow_up = await application.experiments.execute(created.experiment_id, "good")
        assert follow_up.exit_code == 0
        await application.shutdown()

    async def test_refuses_to_run_in_a_destroyed_experiment(self, app: SandboxMCPApp) -> None:
        created = await app.experiments.create()
        await app.experiments.destroy(created.experiment_id)
        with pytest.raises(ExperimentDestroyedError):
            await app.experiments.execute(created.experiment_id, "ls")

    async def test_unknown_experiment_is_reported_clearly(self, app: SandboxMCPApp) -> None:
        with pytest.raises(ExperimentNotFoundError):
            await app.experiments.execute("exp_missing", "ls")


class TestFileAccess:
    async def test_reads_and_writes_inside_the_workspace(
        self, app: SandboxMCPApp, project: Path
    ) -> None:
        created = await app.experiments.create(project_path=str(project))
        assert "module.exports" in await app.experiments.read_file(
            created.experiment_id, "src/index.js"
        )
        await app.experiments.write_file(created.experiment_id, "src/new.js", "// added\n")
        assert await app.experiments.read_file(created.experiment_id, "src/new.js") == "// added\n"

    @pytest.mark.parametrize("path", ["../../etc/passwd", "/etc/shadow"])
    async def test_refuses_paths_outside_the_workspace(self, app: SandboxMCPApp, path: str) -> None:
        created = await app.experiments.create()
        with pytest.raises(UnauthorizedPathError):
            await app.experiments.read_file(created.experiment_id, path)
        with pytest.raises(UnauthorizedPathError):
            await app.experiments.write_file(created.experiment_id, path, "x")


class TestChangeInspection:
    async def test_reports_created_modified_and_deleted(
        self, app: SandboxMCPApp, project: Path, backend: FakeSandboxBackend
    ) -> None:
        created = await app.experiments.create(project_path=str(project))
        experiment = await app.experiments.get(created.experiment_id)
        files = backend.files(app.experiments.handle_for(experiment))
        files["src/index.js"] = b"module.exports = 2;\n"
        files["src/added.js"] = b"exports.z = 3;\n"
        del files["package.json"]

        changes = await app.experiments.inspect_changes(created.experiment_id, include_diff=True)

        assert changes.files_created == ["src/added.js"]
        assert changes.files_modified == ["src/index.js"]
        assert changes.files_deleted == ["package.json"]
        assert changes.insertions >= 1
        assert any(change.diff for change in changes.changes)

    async def test_an_untouched_sandbox_reports_nothing(
        self, app: SandboxMCPApp, project: Path
    ) -> None:
        created = await app.experiments.create(project_path=str(project))
        changes = await app.experiments.inspect_changes(created.experiment_id)
        assert changes.total_changed == 0


class TestTeardown:
    async def test_destroy_removes_the_sandbox_and_the_snapshot(
        self, app: SandboxMCPApp, project: Path, backend: FakeSandboxBackend
    ) -> None:
        created = await app.experiments.create(project_path=str(project))
        experiment = await app.experiments.get(created.experiment_id)
        snapshot = Path(experiment.snapshot_dir or "")

        result = await app.experiments.destroy(created.experiment_id)

        assert result.status is ExperimentStatus.DESTROYED
        assert result.container_removed is True
        assert result.snapshot_removed is True
        assert not snapshot.exists()
        assert backend.sandboxes[experiment.container_id or ""].destroyed

    async def test_destroy_is_idempotent(self, app: SandboxMCPApp) -> None:
        created = await app.experiments.create()
        first = await app.experiments.destroy(created.experiment_id)
        second = await app.experiments.destroy(created.experiment_id)

        assert first.already_destroyed is False
        assert second.already_destroyed is True
        assert second.status is ExperimentStatus.DESTROYED
        assert second.report is not None

    async def test_destroy_captures_the_findings_before_teardown(
        self, app: SandboxMCPApp, project: Path, backend: FakeSandboxBackend
    ) -> None:
        """The diff has to be taken while the sandbox still exists."""
        created = await app.experiments.create(project_path=str(project))
        experiment = await app.experiments.get(created.experiment_id)
        backend.files(app.experiments.handle_for(experiment))["src/index.js"] = b"changed\n"

        result = await app.experiments.destroy(created.experiment_id)

        assert result.report is not None
        assert result.report.changes == {
            "files_created": 0,
            "files_modified": 1,
            "files_deleted": 0,
            "files_changed": 1,
            "insertions": 1,
            "deletions": 1,
        }

    async def test_destroy_cancels_running_jobs(self, app: SandboxMCPApp) -> None:
        created = await app.experiments.create()
        experiment = await app.experiments.get(created.experiment_id)
        app.backend.command_delay = 5  # type: ignore[attr-defined]
        job = await app.execution.submit(
            experiment, app.experiments.handle_for(experiment), "sleep 999"
        )

        result = await app.experiments.destroy(created.experiment_id)

        assert result.jobs_cancelled == 1
        assert (await app.execution.get_job(job.id)).status is JobStatus.CANCELLED


class TestComparison:
    async def test_ranks_experiments_and_recommends_one(self, app: SandboxMCPApp) -> None:
        good = await app.experiments.create(base_image="node:20-slim", objective="Node 20")
        bad = await app.experiments.create(base_image="node:22-slim", objective="Node 22")

        app.backend.responses["npm test"] = outcome(  # type: ignore[attr-defined]
            0, "# tests 40\n# pass 40\n# fail 0\n"
        )
        await app.experiments.run_tests(good.experiment_id, command="npm test")
        app.backend.responses["npm test"] = outcome(  # type: ignore[attr-defined]
            1, "# tests 40\n# pass 37\n# fail 3\nnot ok 1 - seal round-trips\n"
        )
        await app.experiments.run_tests(bad.experiment_id, command="npm test")

        comparison = await app.experiments.compare(
            [good.experiment_id, bad.experiment_id],
            {good.experiment_id: "Node 20", bad.experiment_id: "Node 22"},
        )

        assert [e.label for e in comparison.experiments] == ["Node 20", "Node 22"]
        assert comparison.dimensions["tests_failed"] == {
            good.experiment_id: 0,
            bad.experiment_id: 3,
        }
        assert comparison.recommendation is not None
        assert "Node 20" in comparison.recommendation

    async def test_a_destroyed_experiment_is_still_comparable(
        self, app: SandboxMCPApp, project: Path, backend: FakeSandboxBackend
    ) -> None:
        first = await app.experiments.create(project_path=str(project), objective="A")
        second = await app.experiments.create(project_path=str(project), objective="B")
        experiment = await app.experiments.get(first.experiment_id)
        backend.files(app.experiments.handle_for(experiment))["src/index.js"] = b"changed\n"
        await app.experiments.destroy(first.experiment_id)

        comparison = await app.experiments.compare([first.experiment_id, second.experiment_id])

        assert comparison.experiments[0].status is ExperimentStatus.DESTROYED
        assert comparison.experiments[0].files_changed == 1
        assert comparison.experiments[1].files_changed == 0


class TestReporting:
    async def test_summarises_an_experiment(self, app: SandboxMCPApp, project: Path) -> None:
        created = await app.experiments.create(
            project_path=str(project), objective="upgrade", base_image="node:22-slim"
        )
        await app.experiments.execute(created.experiment_id, "echo one")

        report = await app.experiments.report(created.experiment_id)

        assert report.objective == "upgrade"
        assert report.commands_run == 1
        assert report.failed_commands == 0
        assert report.host_working_tree == "UNCHANGED"
        assert report.sandbox == "LIVE"
