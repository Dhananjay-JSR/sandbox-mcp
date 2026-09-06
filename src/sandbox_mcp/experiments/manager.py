"""The experiment manager: the domain core.

Everything an MCP tool can do routes through here. The manager owns the
sequence that makes the guarantee true --

    validate path -> snapshot -> apply policy -> create sandbox -> baseline

-- and owns the state machine, so no caller can put an experiment into a state
its sandbox does not match.
"""

from __future__ import annotations

import contextlib
import shutil
from pathlib import Path
from typing import Any

from ..artifacts.manager import ArtifactManager
from ..config import Settings
from ..errors import (
    CapacityError,
    ExperimentDestroyedError,
    ExperimentNotFoundError,
    SandboxMCPError,
)
from ..execution.manager import ExecutionManager
from ..logging import get_logger
from ..models import (
    ArtifactCollectionResult,
    ChangeSet,
    CreateExperimentResult,
    DestroyResult,
    Experiment,
    ExperimentComparison,
    ExperimentComparisonEntry,
    ExperimentReport,
    ExperimentSpec,
    ExperimentStatus,
    Job,
    JobStatus,
    StateTransition,
    TestSummary,
    new_id,
    utcnow,
)
from ..sandbox.interface import SandboxBackend, SandboxHandle
from ..security.filesystem import (
    ProjectSnapshotter,
    validate_project_path,
    validate_sandbox_path,
)
from ..security.policy import PolicyEngine
from .changes import compute_changes
from .repository import ExperimentRepository
from .state import LIVE_STATES, assert_transition
from .testing import choose_framework, detection_script, parse_test_output

log = get_logger(__name__)


class ExperimentManager:
    def __init__(
        self,
        settings: Settings,
        backend: SandboxBackend,
        repository: ExperimentRepository,
        execution: ExecutionManager,
        artifacts: ArtifactManager,
        policy: PolicyEngine | None = None,
    ) -> None:
        self._settings = settings
        self._backend = backend
        self._repository = repository
        self._execution = execution
        self._artifacts = artifacts
        self._policy = policy or PolicyEngine(settings)
        self._snapshotter = ProjectSnapshotter(settings)

    # --- startup ----------------------------------------------------------

    async def startup(self) -> None:
        """Prepare storage and clean up after any previous crash."""
        self._settings.ensure_directories()
        await self._repository.initialize()
        orphaned = await self._repository.mark_orphaned_jobs_failed()
        if orphaned:
            log.info("orphan_jobs_reconciled", operation="startup", count=orphaned)

        # Sweep containers no live experiment claims. Best effort: a missing
        # Docker daemon must not stop the server from starting.
        try:
            live = await self._repository.list_experiments(limit=500)
            live_ids = {
                experiment.container_id
                for experiment in live
                if experiment.container_id and experiment.status in LIVE_STATES
            }
            cleanup = getattr(self._backend, "cleanup_orphans", None)
            if cleanup is not None:
                await cleanup({cid for cid in live_ids if cid})
        except SandboxMCPError as exc:
            log.warning("startup_cleanup_skipped", operation="startup", reason=exc.message)

    # --- lookup -----------------------------------------------------------

    async def get(self, experiment_id: str) -> Experiment:
        experiment = await self._repository.get_experiment(experiment_id)
        if experiment is None:
            raise ExperimentNotFoundError(
                f"No experiment with id {experiment_id}.", experiment_id=experiment_id
            )
        return experiment

    async def get_live(self, experiment_id: str) -> Experiment:
        """Fetch an experiment that still has a sandbox behind it."""
        experiment = await self.get(experiment_id)
        if experiment.status is ExperimentStatus.DESTROYED:
            raise ExperimentDestroyedError(
                f"Experiment {experiment_id} was destroyed at "
                f"{experiment.destroyed_at.isoformat() if experiment.destroyed_at else 'unknown'}; "
                "its sandbox no longer exists. Create a new experiment.",
                experiment_id=experiment_id,
            )
        if experiment.container_id is None:
            raise ExperimentDestroyedError(
                f"Experiment {experiment_id} has no sandbox attached.",
                experiment_id=experiment_id,
            )
        return experiment

    async def list_experiments(
        self, status: ExperimentStatus | None = None, limit: int = 50
    ) -> list[Experiment]:
        return await self._repository.list_experiments(status=status, limit=limit)

    @staticmethod
    def handle_for(experiment: Experiment) -> SandboxHandle:
        return SandboxHandle(
            sandbox_id=experiment.container_id or "",
            workspace=experiment.workspace_path,
            metadata=dict(experiment.backend_metadata),
        )

    # --- state ------------------------------------------------------------

    async def _transition(
        self, experiment: Experiment, target: ExperimentStatus, reason: str | None = None
    ) -> Experiment:
        current = experiment.status
        assert_transition(experiment.id, current, target)
        if current == target:
            return experiment

        experiment.status = target
        now = utcnow()
        if target is ExperimentStatus.READY and experiment.ready_at is None:
            experiment.ready_at = now
        elif target is ExperimentStatus.RUNNING and experiment.started_at is None:
            experiment.started_at = now
        elif target in {
            ExperimentStatus.COMPLETED,
            ExperimentStatus.FAILED,
            ExperimentStatus.TIMEOUT,
            ExperimentStatus.CANCELLED,
        }:
            experiment.completed_at = now
        elif target is ExperimentStatus.DESTROYED:
            experiment.destroyed_at = now

        await self._repository.save_experiment(experiment)
        await self._repository.record_transition(
            StateTransition(
                experiment_id=experiment.id,
                from_status=current,
                to_status=target,
                reason=reason,
                at=now,
            )
        )
        log.info(
            "experiment_state_changed",
            operation="experiment.transition",
            experiment_id=experiment.id,
            from_status=current.value,
            to_status=target.value,
            reason=reason,
        )
        return experiment

    # --- creation ---------------------------------------------------------

    async def create(
        self,
        *,
        project_path: str | None = None,
        base_image: str | None = None,
        network_mode: str | None = None,
        mount_strategy: str | None = None,
        cpu_limit: float | None = None,
        memory_limit: str | None = None,
        timeout: int | None = None,
        pids_limit: int | None = None,
        environment_allowlist: list[str] | None = None,
        setup_commands: list[str] | None = None,
        writable: bool = True,
        objective: str | None = None,
    ) -> CreateExperimentResult:
        await self._enforce_capacity()

        resolved_project: Path | None = None
        project_name = "scratch"
        if project_path:
            resolved_project = validate_project_path(project_path, self._settings)
            project_name = resolved_project.name

        decision = self._policy.build_spec(
            project_name=project_name,
            project_path=str(resolved_project) if resolved_project else None,
            base_image=base_image,
            network_mode=network_mode,
            mount_strategy=mount_strategy,
            cpu_limit=cpu_limit,
            memory_limit=memory_limit,
            timeout=timeout,
            pids_limit=pids_limit,
            environment_allowlist=environment_allowlist,
            setup_commands=setup_commands,
            writable=writable,
            objective=objective,
        )
        spec = decision.spec
        warnings = list(decision.warnings)

        experiment = Experiment(
            id=new_id("exp"),
            project_name=project_name,
            project_path=str(resolved_project) if resolved_project else None,
            objective=objective,
            base_image=spec.base_image,
            network_mode=spec.network_mode,
            mount_strategy=spec.mount_strategy,
            resources=spec.resources,
            workspace_path=spec.workspace_path,
            environment_keys=sorted(spec.environment),
            setup_commands=spec.setup_commands,
        )
        await self._repository.save_experiment(experiment)
        await self._repository.record_transition(
            StateTransition(
                experiment_id=experiment.id,
                from_status=None,
                to_status=ExperimentStatus.CREATING,
                reason="create_experiment",
            )
        )

        snapshot_dir: Path | None = None
        files_copied: int | None = None
        try:
            snapshot_dir, files_copied, snapshot_warnings = await self._prepare_snapshot(
                experiment, spec, resolved_project
            )
            warnings.extend(snapshot_warnings)

            handle = await self._backend.create(spec, str(snapshot_dir) if snapshot_dir else None)
            experiment.container_id = handle.sandbox_id
            experiment.backend_metadata = {
                key: str(value) for key, value in handle.metadata.items() if value is not None
            }
            experiment.snapshot_dir = str(snapshot_dir) if snapshot_dir else None
            await self._repository.save_experiment(experiment)
            await self._capture_baseline(experiment, handle, snapshot_dir)
            await self._transition(experiment, ExperimentStatus.READY, "sandbox provisioned")

        except SandboxMCPError as exc:
            await self._fail_creation(experiment, snapshot_dir, exc.message)
            raise
        except Exception as exc:
            await self._fail_creation(experiment, snapshot_dir, f"{type(exc).__name__}: {exc}")
            log.exception("experiment_create_failed", experiment_id=experiment.id)
            raise

        setup_results = await self._run_setup_commands(experiment, handle, warnings)

        log.info(
            "experiment_created",
            operation="experiment.create",
            experiment_id=experiment.id,
            base_image=experiment.base_image,
            network_mode=experiment.network_mode.value,
            files_copied=files_copied,
            status=experiment.status.value,
        )
        return CreateExperimentResult(
            experiment_id=experiment.id,
            status=experiment.status,
            base_image=experiment.base_image,
            network_mode=experiment.network_mode,
            mount_strategy=experiment.mount_strategy,
            workspace_path=experiment.workspace_path,
            resource_limits=experiment.resources.summary(),
            project_name=experiment.project_name,
            files_copied=files_copied,
            environment_passed=experiment.environment_keys,
            setup_jobs=setup_results,
            warnings=warnings,
        )

    async def _enforce_capacity(self) -> None:
        experiments = await self._repository.list_experiments(limit=500)
        live = [e for e in experiments if e.status in LIVE_STATES]
        if len(live) >= self._settings.max_concurrent_experiments:
            raise CapacityError(
                f"{len(live)} experiments are already live, at the configured ceiling of "
                f"{self._settings.max_concurrent_experiments}. Destroy one first.",
                live=len(live),
                limit=self._settings.max_concurrent_experiments,
            )

    async def _prepare_snapshot(
        self, experiment: Experiment, spec: ExperimentSpec, project: Path | None
    ) -> tuple[Path | None, int | None, list[str]]:
        """Copy the project into sandbox-owned storage. Never mutates the source."""
        if project is None or spec.mount_strategy.value == "READ_ONLY_BIND_MOUNT":
            return None, None, []

        destination = self._settings.sandboxes_dir / experiment.id / "workspace"
        result = self._snapshotter.snapshot(project, destination)
        warnings: list[str] = []
        protected = [entry for entry in result.skipped if "(protected)" in entry]
        if protected:
            warnings.append(
                f"{len(protected)} sensitive path(s) were withheld from the sandbox: "
                + ", ".join(entry.split(" (")[0] for entry in protected[:5])
            )
        log.info(
            "project_snapshotted",
            operation="experiment.snapshot",
            experiment_id=experiment.id,
            files=result.file_count,
            bytes=result.total_bytes,
            skipped=len(result.skipped),
        )
        return destination, result.file_count, warnings

    async def _capture_baseline(
        self, experiment: Experiment, handle: SandboxHandle, snapshot_dir: Path | None
    ) -> None:
        """Fingerprint the workspace as the sandbox actually sees it.

        Read from inside the container rather than from the host copy, so a
        bind-mounted project gets a baseline too and any transformation the
        upload applied is already accounted for.
        """
        try:
            manifest = await self._backend.read_manifest(handle, self._settings.snapshot_excludes)
        except SandboxMCPError as exc:
            log.warning(
                "baseline_capture_failed",
                operation="experiment.baseline",
                experiment_id=experiment.id,
                reason=exc.message,
            )
            return
        await self._repository.save_baseline(experiment.id, manifest)

    async def _run_setup_commands(
        self, experiment: Experiment, handle: SandboxHandle, warnings: list[str]
    ) -> list[Any]:
        from ..models import ExecutionResult

        results: list[ExecutionResult] = []
        if not experiment.setup_commands:
            return results

        await self._transition(experiment, ExperimentStatus.RUNNING, "setup commands")
        failed = False
        for command in experiment.setup_commands:
            job = await self._execution.submit_and_wait(experiment, handle, command, kind="setup")
            results.append(ExecutionResult.from_job(job))
            if job.status is not JobStatus.COMPLETED or job.exit_code != 0:
                failed = True
                warnings.append(
                    f"Setup command failed ({job.status.value}, exit {job.exit_code}): {command}"
                )
                break

        await self._transition(
            experiment,
            ExperimentStatus.FAILED if failed else ExperimentStatus.READY,
            "setup failed" if failed else "setup complete",
        )
        return results

    async def _fail_creation(
        self, experiment: Experiment, snapshot_dir: Path | None, reason: str
    ) -> None:
        experiment.error = reason
        await self._repository.save_experiment(experiment)
        if snapshot_dir is not None:
            shutil.rmtree(snapshot_dir.parent, ignore_errors=True)
        if experiment.container_id:
            with contextlib.suppress(SandboxMCPError):
                await self._backend.destroy(self.handle_for(experiment))
        await self._transition(experiment, ExperimentStatus.FAILED, reason)

    # --- execution --------------------------------------------------------

    async def execute(
        self,
        experiment_id: str,
        command: str,
        timeout: int | None = None,
        workdir: str | None = None,
        background: bool = False,
    ) -> Job:
        experiment = await self.get_live(experiment_id)
        handle = self.handle_for(experiment)
        await self._transition(experiment, ExperimentStatus.RUNNING, "execute_experiment")

        if background:
            return await self._execution.submit(experiment, handle, command, timeout, workdir)

        job = await self._execution.submit_and_wait(experiment, handle, command, timeout, workdir)
        await self._settle(experiment, job)
        return job

    async def _settle(self, experiment: Experiment, job: Job) -> None:
        """Move the experiment out of RUNNING once a foreground job finishes."""
        target = {
            JobStatus.COMPLETED: (
                ExperimentStatus.READY if job.exit_code == 0 else ExperimentStatus.FAILED
            ),
            JobStatus.TIMEOUT: ExperimentStatus.TIMEOUT,
            JobStatus.CANCELLED: ExperimentStatus.CANCELLED,
            JobStatus.FAILED: ExperimentStatus.FAILED,
        }.get(job.status, ExperimentStatus.READY)
        await self._transition(experiment, target, f"job {job.id} {job.status.value.lower()}")

    async def run_tests(
        self,
        experiment_id: str,
        command: str | None = None,
        timeout: int | None = None,
    ) -> tuple[Job, str | None, TestSummary]:
        experiment = await self.get_live(experiment_id)
        handle = self.handle_for(experiment)

        framework_name: str | None = None
        if command is None:
            framework = await self._detect_framework(handle)
            if framework is None:
                raise SandboxMCPError(
                    "Could not detect a test runner in the sandbox. Pass an explicit "
                    "`command`, e.g. 'npm test' or 'pytest -q'.",
                )
            command, framework_name = framework.command, framework.name

        await self._transition(experiment, ExperimentStatus.RUNNING, "run_tests")
        job = await self._execution.submit_and_wait(
            experiment, handle, command, timeout, kind="test"
        )
        await self._settle(experiment, job)

        summary = parse_test_output(f"{job.stdout}\n{job.stderr}", framework_name)
        log.info(
            "tests_completed",
            operation="experiment.run_tests",
            experiment_id=experiment.id,
            job_id=job.id,
            framework=summary.framework,
            passed=summary.passed,
            failed=summary.failed,
            exit_code=job.exit_code,
        )
        return job, framework_name, summary

    async def _detect_framework(self, handle: SandboxHandle) -> Any:
        outcome = await self._backend.execute(
            handle, detection_script(), timeout=30, workdir=handle.workspace
        )
        present = {line.strip() for line in outcome.stdout.splitlines() if line.strip()}
        return choose_framework(present)

    # --- inspection -------------------------------------------------------

    async def inspect_changes(
        self,
        experiment_id: str,
        include_diff: bool = False,
        max_files_with_diff: int = 50,
    ) -> ChangeSet:
        experiment = await self.get_live(experiment_id)
        handle = self.handle_for(experiment)

        baseline = await self._repository.get_baseline(experiment_id)
        if baseline is None:
            return ChangeSet(
                experiment_id=experiment_id,
                note=(
                    "No baseline was captured for this experiment, so changes cannot be "
                    "computed. This happens when the sandbox was created without a project."
                ),
            )

        current = await self._backend.read_manifest(handle, self._settings.snapshot_excludes)

        async def read_file(path: str) -> bytes | None:
            full = f"{handle.workspace.rstrip('/')}/{path}"
            return await self._backend.read_file(handle, full, self._settings.max_diff_file_bytes)

        changeset = await compute_changes(
            experiment_id=experiment_id,
            baseline=baseline,
            current=current,
            read_sandbox_file=read_file,
            snapshot_dir=Path(experiment.snapshot_dir) if experiment.snapshot_dir else None,
            include_diff=include_diff,
            max_files_with_diff=max_files_with_diff,
            max_file_bytes=self._settings.max_diff_file_bytes,
        )
        await self._repository.save_change_stats(experiment_id, _change_stats(changeset))
        return changeset

    async def read_file(self, experiment_id: str, path: str) -> str:
        """Read one workspace file as text. Confined to the workspace."""
        experiment = await self.get_live(experiment_id)
        handle = self.handle_for(experiment)
        resolved = validate_sandbox_path(path, handle.workspace)
        payload = await self._backend.read_file(
            handle, resolved, self._settings.max_diff_file_bytes
        )
        return payload.decode("utf-8", errors="replace")

    async def write_file(self, experiment_id: str, path: str, content: str) -> str:
        """Write one workspace file. Confined to the workspace, and to the
        *copy* -- there is no code path from here to the host project."""
        experiment = await self.get_live(experiment_id)
        handle = self.handle_for(experiment)
        resolved = validate_sandbox_path(path, handle.workspace)
        await self._backend.write_file(handle, resolved, content.encode("utf-8"))
        log.info(
            "sandbox_file_written",
            operation="experiment.write_file",
            experiment_id=experiment_id,
            path=resolved,
            bytes=len(content),
        )
        return resolved

    async def collect_artifacts(
        self, experiment_id: str, patterns: list[str]
    ) -> ArtifactCollectionResult:
        experiment = await self.get_live(experiment_id)
        handle = self.handle_for(experiment)
        result = await self._artifacts.collect(experiment, handle, patterns)
        for artifact in result.artifacts:
            await self._repository.save_artifact(artifact)
        return result

    # --- teardown ---------------------------------------------------------

    async def destroy(self, experiment_id: str) -> DestroyResult:
        """Tear down a sandbox. Safe to call repeatedly."""
        experiment = await self.get(experiment_id)

        if experiment.status is ExperimentStatus.DESTROYED:
            return DestroyResult(
                experiment_id=experiment.id,
                status=experiment.status,
                already_destroyed=True,
                report=await self.report(experiment_id, include_changes=False),
            )

        cancelled = await self._execution.cancel_experiment_jobs(experiment.id)

        # Capture the diff while the sandbox still exists, so the finding
        # outlives the container it came from.
        with contextlib.suppress(SandboxMCPError):
            await self.inspect_changes(experiment_id, include_diff=False)

        report = await self.report(experiment_id, include_changes=False)

        container_removed = False
        if experiment.container_id:
            try:
                container_removed = await self._backend.destroy(self.handle_for(experiment))
            except SandboxMCPError as exc:
                log.warning(
                    "sandbox_destroy_failed",
                    operation="experiment.destroy",
                    experiment_id=experiment.id,
                    reason=exc.message,
                )

        snapshot_removed = False
        if experiment.snapshot_dir:
            snapshot_root = Path(experiment.snapshot_dir).parent
            if snapshot_root.exists():
                shutil.rmtree(snapshot_root, ignore_errors=True)
                snapshot_removed = True

        await self._transition(experiment, ExperimentStatus.DESTROYED, "destroy_experiment")
        report.status = ExperimentStatus.DESTROYED
        report.sandbox = "DESTROYED"
        report.duration_ms = experiment.duration_ms()

        log.info(
            "experiment_destroyed",
            operation="experiment.destroy",
            experiment_id=experiment.id,
            container_removed=container_removed,
            snapshot_removed=snapshot_removed,
            jobs_cancelled=cancelled,
        )
        return DestroyResult(
            experiment_id=experiment.id,
            status=ExperimentStatus.DESTROYED,
            container_removed=container_removed,
            snapshot_removed=snapshot_removed,
            jobs_cancelled=cancelled,
            report=report,
        )

    # --- reporting --------------------------------------------------------

    async def report(self, experiment_id: str, include_changes: bool = True) -> ExperimentReport:
        experiment = await self.get(experiment_id)
        jobs = await self._repository.list_jobs(experiment_id)
        artifacts = await self._repository.list_artifacts(experiment_id)

        changes: dict[str, Any] | None = await self._repository.get_change_stats(experiment_id)
        if include_changes and experiment.status is not ExperimentStatus.DESTROYED:
            with contextlib.suppress(SandboxMCPError):
                changes = _change_stats(await self.inspect_changes(experiment_id))

        return ExperimentReport(
            experiment_id=experiment.id,
            objective=experiment.objective,
            project_name=experiment.project_name,
            base_image=experiment.base_image,
            status=experiment.status,
            network_mode=experiment.network_mode,
            resources=experiment.resources.summary(),
            commands_run=len(jobs),
            failed_commands=sum(1 for job in jobs if _job_failed(job)),
            test_summary=_last_test_summary(jobs),
            changes=changes,
            artifacts=len(artifacts),
            duration_ms=experiment.duration_ms(),
            host_working_tree="UNCHANGED",
            sandbox=("DESTROYED" if experiment.status is ExperimentStatus.DESTROYED else "LIVE"),
        )

    async def compare(
        self, experiment_ids: list[str], labels: dict[str, str] | None = None
    ) -> ExperimentComparison:
        """Put experiments side by side on the dimensions that decide things."""
        entries: list[ExperimentComparisonEntry] = []
        notes: list[str] = []

        for experiment_id in experiment_ids:
            experiment = await self.get(experiment_id)
            jobs = await self._repository.list_jobs(experiment_id)
            artifacts = await self._repository.list_artifacts(experiment_id)
            stats = await self._repository.get_change_stats(experiment_id)
            summary = _last_test_summary(jobs)
            last_job = jobs[-1] if jobs else None

            if stats is None and experiment.status is not ExperimentStatus.DESTROYED:
                try:
                    stats = _change_stats(await self.inspect_changes(experiment_id))
                except SandboxMCPError:
                    notes.append(f"{experiment_id}: change statistics unavailable.")

            entries.append(
                ExperimentComparisonEntry(
                    experiment_id=experiment.id,
                    label=(labels or {}).get(experiment.id)
                    or experiment.objective
                    or experiment.base_image,
                    base_image=experiment.base_image,
                    status=experiment.status,
                    tests_passed=summary.passed if summary else None,
                    tests_failed=summary.failed if summary else None,
                    failing_tests=summary.failing_tests if summary else [],
                    last_exit_code=last_job.exit_code if last_job else None,
                    commands_run=len(jobs),
                    failed_commands=sum(1 for job in jobs if _job_failed(job)),
                    files_changed=(stats or {}).get("files_changed"),
                    insertions=(stats or {}).get("insertions"),
                    deletions=(stats or {}).get("deletions"),
                    artifacts=len(artifacts),
                    duration_ms=_total_job_duration(jobs),
                )
            )

        dimensions: dict[str, dict[str, Any]] = {
            "tests_failed": {e.experiment_id: e.tests_failed for e in entries},
            "tests_passed": {e.experiment_id: e.tests_passed for e in entries},
            "last_exit_code": {e.experiment_id: e.last_exit_code for e in entries},
            "files_changed": {e.experiment_id: e.files_changed for e in entries},
            "duration_ms": {e.experiment_id: e.duration_ms for e in entries},
            "failed_commands": {e.experiment_id: e.failed_commands for e in entries},
        }
        return ExperimentComparison(
            experiments=entries,
            dimensions=dimensions,
            recommendation=_recommend(entries),
            notes=notes,
        )


# --- helpers --------------------------------------------------------------


def _job_failed(job: Job) -> bool:
    if job.status is JobStatus.COMPLETED:
        return job.exit_code not in (0, None)
    return job.status in {JobStatus.FAILED, JobStatus.TIMEOUT}


def _last_test_summary(jobs: list[Job]) -> TestSummary | None:
    for job in reversed(jobs):
        if job.kind == "test":
            return parse_test_output(f"{job.stdout}\n{job.stderr}")
    return None


def _total_job_duration(jobs: list[Job]) -> int | None:
    durations = [job.duration_ms for job in jobs if job.duration_ms is not None]
    return sum(durations) if durations else None


def _change_stats(changeset: ChangeSet) -> dict[str, Any]:
    return {
        "files_created": len(changeset.files_created),
        "files_modified": len(changeset.files_modified),
        "files_deleted": len(changeset.files_deleted),
        "files_changed": changeset.total_changed,
        "insertions": changeset.insertions,
        "deletions": changeset.deletions,
    }


def _recommend(entries: list[ExperimentComparisonEntry]) -> str | None:
    """Pick a winner only when the evidence actually supports one."""
    scored = [e for e in entries if e.tests_failed is not None or e.last_exit_code is not None]
    if len(scored) < 2:
        return None

    def rank(entry: ExperimentComparisonEntry) -> tuple[int, int, int, int]:
        return (
            entry.tests_failed if entry.tests_failed is not None else 999,
            0 if entry.last_exit_code == 0 else 1,
            entry.failed_commands,
            entry.files_changed if entry.files_changed is not None else 999,
        )

    ordered = sorted(scored, key=rank)
    best, runner_up = ordered[0], ordered[1]
    if rank(best) == rank(runner_up):
        return (
            f"{best.label} and {runner_up.label} are indistinguishable on tests, exit code "
            "and change size. Choose on other grounds."
        )
    reasons = []
    if best.tests_failed is not None and best.tests_failed < (runner_up.tests_failed or 999):
        reasons.append(f"{best.tests_failed} failing tests vs {runner_up.tests_failed}")
    if best.last_exit_code == 0 and runner_up.last_exit_code != 0:
        reasons.append("its last command exited cleanly")
    if (
        best.files_changed is not None
        and runner_up.files_changed is not None
        and best.files_changed < runner_up.files_changed
    ):
        reasons.append(f"a smaller diff ({best.files_changed} vs {runner_up.files_changed} files)")
    detail = "; ".join(reasons) if reasons else "it ranks first on failures then diff size"
    return f"{best.label} ({best.experiment_id}) looks best: {detail}."
