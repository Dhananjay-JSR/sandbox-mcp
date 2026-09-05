"""Domain models.

The vocabulary of this server is *experiments* and *jobs*, not containers and
execs. Containers appear in exactly one field (``container_id``) and only
because operators need it for forensics.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id(prefix: str) -> str:
    """Short, sortable-enough, human-quotable identifier: ``exp_9f2c1a4b8d3e``."""
    return f"{prefix}_{uuid4().hex[:12]}"


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class ExperimentStatus(StrEnum):
    CREATING = "CREATING"
    READY = "READY"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"
    DESTROYED = "DESTROYED"


class JobStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL_JOB_STATUSES


_TERMINAL_JOB_STATUSES = frozenset(
    {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.TIMEOUT, JobStatus.CANCELLED}
)


class NetworkMode(StrEnum):
    """How much of the network the sandbox can see.

    ``NONE``       -- no interfaces at all. The default.
    ``RESTRICTED`` -- an isolated bridge network shared by nothing else; egress
                      works, but the sandbox cannot reach other sandboxes or
                      the host's service ports.
    ``ENABLED``    -- the daemon's default bridge. Full egress.
    """

    NONE = "none"
    RESTRICTED = "restricted"
    ENABLED = "enabled"


class MountStrategy(StrEnum):
    """How the project reaches the sandbox.

    ``COPY_TO_SANDBOX``      -- snapshot the tree, hand the copy to the
                                container. The host tree is unreachable.
    ``READ_ONLY_BIND_MOUNT`` -- bind the real tree read-only. Faster on large
                                repos; writes to it fail by construction.
    """

    COPY_TO_SANDBOX = "COPY_TO_SANDBOX"
    READ_ONLY_BIND_MOUNT = "READ_ONLY_BIND_MOUNT"


class ChangeType(StrEnum):
    CREATED = "created"
    MODIFIED = "modified"
    DELETED = "deleted"


# ---------------------------------------------------------------------------
# Core entities
# ---------------------------------------------------------------------------


class ResourceLimits(BaseModel):
    """Caps enforced by the container runtime, not by convention."""

    cpu_limit: float = Field(description="CPU cores, fractional allowed.", gt=0)
    memory_limit: str = Field(description="Human form, e.g. '2GB'.")
    memory_bytes: int = Field(description="Parsed byte count actually passed to Docker.", gt=0)
    timeout_seconds: int = Field(description="Wall-clock ceiling for a single command.", gt=0)
    pids_limit: int = Field(description="Max processes, to blunt fork bombs.", gt=0)

    def summary(self) -> dict[str, Any]:
        return {
            "cpu": self.cpu_limit,
            "memory": self.memory_limit,
            "timeout": self.timeout_seconds,
            "pids": self.pids_limit,
        }


class ExperimentSpec(BaseModel):
    """A validated, policy-approved request to create a sandbox.

    Produced by the policy engine; the sandbox backend only ever sees this,
    never the raw MCP arguments.
    """

    project_path: str | None = None
    project_name: str
    base_image: str
    network_mode: NetworkMode
    mount_strategy: MountStrategy
    resources: ResourceLimits
    environment: dict[str, str] = Field(default_factory=dict)
    setup_commands: list[str] = Field(default_factory=list)
    writable: bool = True
    workspace_path: str = "/workspace"
    user: str | None = None
    objective: str | None = None


class Experiment(BaseModel):
    """A disposable environment plus everything known about its life."""

    model_config = ConfigDict(use_enum_values=False)

    id: str = Field(default_factory=lambda: new_id("exp"))
    project_name: str
    project_path: str | None = None
    objective: str | None = None
    base_image: str
    status: ExperimentStatus = ExperimentStatus.CREATING
    network_mode: NetworkMode
    mount_strategy: MountStrategy
    resources: ResourceLimits
    workspace_path: str = "/workspace"
    container_id: str | None = None
    snapshot_dir: str | None = Field(
        default=None, description="Host-side immutable baseline used to compute diffs."
    )
    backend_metadata: dict[str, str] = Field(
        default_factory=dict,
        description="Opaque backend state (network, image) needed to reattach after a restart.",
    )
    environment_keys: list[str] = Field(
        default_factory=list, description="Names only. Values are never persisted."
    )
    setup_commands: list[str] = Field(default_factory=list)
    error: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    ready_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    destroyed_at: datetime | None = None

    @property
    def is_live(self) -> bool:
        """True while the sandbox still exists and can accept commands."""
        return self.status not in {ExperimentStatus.DESTROYED, ExperimentStatus.CREATING}

    def duration_ms(self) -> int | None:
        end = self.destroyed_at or self.completed_at
        if end is None:
            return None
        return int((end - self.created_at).total_seconds() * 1000)


class StateTransition(BaseModel):
    """One edge of the experiment state machine, persisted for audit."""

    experiment_id: str
    from_status: ExperimentStatus | None
    to_status: ExperimentStatus
    reason: str | None = None
    at: datetime = Field(default_factory=utcnow)


class Job(BaseModel):
    """A single command execution inside a sandbox."""

    id: str = Field(default_factory=lambda: new_id("job"))
    experiment_id: str
    command: str
    argv: list[str] = Field(default_factory=list)
    workdir: str | None = None
    status: JobStatus = JobStatus.PENDING
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    timeout_seconds: int
    error: str | None = None
    kind: Literal["command", "setup", "test"] = "command"
    created_at: datetime = Field(default_factory=utcnow)
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @property
    def duration_ms(self) -> int | None:
        if self.started_at is None:
            return None
        end = self.finished_at or utcnow()
        return int((end - self.started_at).total_seconds() * 1000)

    def elapsed_ms(self) -> int:
        start = self.started_at or self.created_at
        end = self.finished_at or utcnow()
        return int((end - start).total_seconds() * 1000)


class TestSummary(BaseModel):
    """Counts parsed out of a test runner's output. Best effort, never a lie.

    ``detected`` is False when nothing could be parsed -- callers should fall
    back to the exit code rather than trusting zeros.
    """

    framework: str | None = None
    detected: bool = False
    total: int | None = None
    passed: int | None = None
    failed: int | None = None
    skipped: int | None = None
    failing_tests: list[str] = Field(default_factory=list)


class FileChange(BaseModel):
    path: str
    change_type: ChangeType
    size_bytes: int | None = None
    insertions: int = 0
    deletions: int = 0
    binary: bool = False
    diff: str | None = None


class ChangeSet(BaseModel):
    """What the sandbox looks like now versus the untouched baseline."""

    experiment_id: str
    files_created: list[str] = Field(default_factory=list)
    files_modified: list[str] = Field(default_factory=list)
    files_deleted: list[str] = Field(default_factory=list)
    insertions: int = 0
    deletions: int = 0
    changes: list[FileChange] = Field(default_factory=list)
    truncated: bool = False
    note: str | None = None

    @property
    def total_changed(self) -> int:
        return len(self.files_created) + len(self.files_modified) + len(self.files_deleted)


class Artifact(BaseModel):
    """A file lifted out of the sandbox and stored under the server's state dir."""

    id: str = Field(default_factory=lambda: new_id("art"))
    experiment_id: str
    sandbox_path: str
    host_path: str
    size_bytes: int
    sha256: str
    collected_at: datetime = Field(default_factory=utcnow)


class ExperimentReport(BaseModel):
    """The concise summary an agent should quote back to the developer."""

    experiment_id: str
    objective: str | None
    project_name: str
    base_image: str
    status: ExperimentStatus
    network_mode: NetworkMode
    resources: dict[str, Any]
    commands_run: int
    failed_commands: int
    test_summary: TestSummary | None = None
    changes: dict[str, Any] | None = None
    artifacts: int = 0
    duration_ms: int | None = None
    host_working_tree: str = "UNCHANGED"
    sandbox: str = "LIVE"


# ---------------------------------------------------------------------------
# Tool payloads
# ---------------------------------------------------------------------------


class CreateExperimentResult(BaseModel):
    experiment_id: str
    status: ExperimentStatus
    base_image: str
    network_mode: NetworkMode
    mount_strategy: MountStrategy
    workspace_path: str
    resource_limits: dict[str, Any]
    project_name: str
    files_copied: int | None = None
    environment_passed: list[str] = Field(default_factory=list)
    setup_jobs: list[ExecutionResult] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class ExecutionResult(BaseModel):
    """Outcome of one command. Identical shape whether it ran sync or async."""

    job_id: str
    experiment_id: str
    status: JobStatus
    command: str
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    duration_ms: int | None = None
    error: str | None = None

    @classmethod
    def from_job(cls, job: Job) -> ExecutionResult:
        return cls(
            job_id=job.id,
            experiment_id=job.experiment_id,
            status=job.status,
            command=job.command,
            exit_code=job.exit_code,
            stdout=job.stdout,
            stderr=job.stderr,
            stdout_truncated=job.stdout_truncated,
            stderr_truncated=job.stderr_truncated,
            duration_ms=job.duration_ms,
            error=job.error,
        )


class TestRunResult(ExecutionResult):
    framework: str | None = None
    test_summary: TestSummary | None = None


class JobStatusResult(BaseModel):
    job_id: str
    experiment_id: str
    status: JobStatus
    command: str
    elapsed_ms: int
    exit_code: int | None = None
    finished: bool = False


class ArtifactCollectionResult(BaseModel):
    experiment_id: str
    artifacts: list[Artifact]
    skipped: list[str] = Field(default_factory=list)
    total_bytes: int = 0


class DestroyResult(BaseModel):
    experiment_id: str
    status: ExperimentStatus
    already_destroyed: bool = False
    container_removed: bool = False
    snapshot_removed: bool = False
    jobs_cancelled: int = 0
    report: ExperimentReport | None = None


class ExperimentComparisonEntry(BaseModel):
    experiment_id: str
    label: str
    base_image: str
    status: ExperimentStatus
    tests_passed: int | None = None
    tests_failed: int | None = None
    failing_tests: list[str] = Field(default_factory=list)
    last_exit_code: int | None = None
    commands_run: int = 0
    failed_commands: int = 0
    files_changed: int | None = None
    insertions: int | None = None
    deletions: int | None = None
    artifacts: int = 0
    duration_ms: int | None = None


class ExperimentComparison(BaseModel):
    experiments: list[ExperimentComparisonEntry]
    dimensions: dict[str, dict[str, Any]] = Field(
        default_factory=dict, description="Per-dimension map of experiment_id -> value."
    )
    recommendation: str | None = None
    notes: list[str] = Field(default_factory=list)


CreateExperimentResult.model_rebuild()
