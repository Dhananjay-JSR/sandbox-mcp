"""Persistence.

SQLite, reached from async code through :func:`asyncio.to_thread` and one
serialising lock. WAL is on so a reader (an MCP resource fetch) never blocks a
writer (a job finishing).

What is *not* stored is as deliberate as what is: environment variable values
never touch the database, only their names. Nothing here should ever be a
place a leaked credential can come to rest.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..models import (
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
from ..security.filesystem import FileFingerprint, FileManifest

SCHEMA = """
CREATE TABLE IF NOT EXISTS experiments (
    id                TEXT PRIMARY KEY,
    project_name      TEXT NOT NULL,
    project_path      TEXT,
    objective         TEXT,
    base_image        TEXT NOT NULL,
    status            TEXT NOT NULL,
    network_mode      TEXT NOT NULL,
    mount_strategy    TEXT NOT NULL,
    cpu_limit         REAL NOT NULL,
    memory_limit      TEXT NOT NULL,
    memory_bytes      INTEGER NOT NULL,
    timeout_seconds   INTEGER NOT NULL,
    pids_limit        INTEGER NOT NULL,
    workspace_path    TEXT NOT NULL,
    container_id      TEXT,
    snapshot_dir      TEXT,
    backend_metadata  TEXT NOT NULL DEFAULT '{}',
    environment_keys  TEXT NOT NULL DEFAULT '[]',
    setup_commands    TEXT NOT NULL DEFAULT '[]',
    error             TEXT,
    created_at        TEXT NOT NULL,
    ready_at          TEXT,
    started_at        TEXT,
    completed_at      TEXT,
    destroyed_at      TEXT
);

CREATE TABLE IF NOT EXISTS jobs (
    id                TEXT PRIMARY KEY,
    experiment_id     TEXT NOT NULL REFERENCES experiments(id) ON DELETE CASCADE,
    command           TEXT NOT NULL,
    argv              TEXT NOT NULL DEFAULT '[]',
    workdir           TEXT,
    kind              TEXT NOT NULL DEFAULT 'command',
    status            TEXT NOT NULL,
    exit_code         INTEGER,
    stdout            TEXT NOT NULL DEFAULT '',
    stderr            TEXT NOT NULL DEFAULT '',
    stdout_truncated  INTEGER NOT NULL DEFAULT 0,
    stderr_truncated  INTEGER NOT NULL DEFAULT 0,
    timeout_seconds   INTEGER NOT NULL,
    error             TEXT,
    created_at        TEXT NOT NULL,
    started_at        TEXT,
    finished_at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_experiment ON jobs(experiment_id, created_at);

CREATE TABLE IF NOT EXISTS state_transitions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id TEXT NOT NULL REFERENCES experiments(id) ON DELETE CASCADE,
    from_status   TEXT,
    to_status     TEXT NOT NULL,
    reason        TEXT,
    at            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_transitions_experiment ON state_transitions(experiment_id, id);

CREATE TABLE IF NOT EXISTS artifacts (
    id            TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL REFERENCES experiments(id) ON DELETE CASCADE,
    sandbox_path  TEXT NOT NULL,
    host_path     TEXT NOT NULL,
    size_bytes    INTEGER NOT NULL,
    sha256        TEXT NOT NULL,
    collected_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_artifacts_experiment ON artifacts(experiment_id);

-- Last computed diff summary. Kept so a destroyed experiment can still be
-- compared against a live one -- the sandbox is gone, the finding is not.
CREATE TABLE IF NOT EXISTS change_stats (
    experiment_id TEXT PRIMARY KEY REFERENCES experiments(id) ON DELETE CASCADE,
    stats         TEXT NOT NULL,
    at            TEXT NOT NULL
);

-- The pristine fingerprint of the project as it was copied in. Every diff is
-- computed against this, so it must outlive the process.
CREATE TABLE IF NOT EXISTS baselines (
    experiment_id TEXT PRIMARY KEY REFERENCES experiments(id) ON DELETE CASCADE,
    manifest      TEXT NOT NULL
);
"""


class ExperimentRepository(ABC):
    """Storage contract. SQLite is the MVP; Postgres would slot in unchanged."""

    @abstractmethod
    async def initialize(self) -> None: ...

    @abstractmethod
    async def save_experiment(self, experiment: Experiment) -> None: ...

    @abstractmethod
    async def get_experiment(self, experiment_id: str) -> Experiment | None: ...

    @abstractmethod
    async def list_experiments(
        self, status: ExperimentStatus | None = None, limit: int = 100
    ) -> list[Experiment]: ...

    @abstractmethod
    async def record_transition(self, transition: StateTransition) -> None: ...

    @abstractmethod
    async def list_transitions(self, experiment_id: str) -> list[StateTransition]: ...

    @abstractmethod
    async def save_job(self, job: Job) -> None: ...

    @abstractmethod
    async def get_job(self, job_id: str) -> Job | None: ...

    @abstractmethod
    async def list_jobs(self, experiment_id: str) -> list[Job]: ...

    @abstractmethod
    async def save_artifact(self, artifact: Artifact) -> None: ...

    @abstractmethod
    async def list_artifacts(self, experiment_id: str) -> list[Artifact]: ...

    @abstractmethod
    async def get_artifact(self, artifact_id: str) -> Artifact | None: ...

    @abstractmethod
    async def save_change_stats(self, experiment_id: str, stats: dict[str, Any]) -> None: ...

    @abstractmethod
    async def get_change_stats(self, experiment_id: str) -> dict[str, Any] | None: ...

    @abstractmethod
    async def mark_orphaned_jobs_failed(self) -> int: ...

    @abstractmethod
    async def save_baseline(self, experiment_id: str, manifest: FileManifest) -> None: ...

    @abstractmethod
    async def get_baseline(self, experiment_id: str) -> FileManifest | None: ...

    @abstractmethod
    async def close(self) -> None: ...


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _parse_dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _require_dt(value: str | None) -> datetime:
    """For NOT NULL timestamp columns, which the schema guarantees are present."""
    if value is None:
        raise ValueError("expected a timestamp in a NOT NULL column")
    return datetime.fromisoformat(value)


class SQLiteRepository(ExperimentRepository):
    def __init__(self, database_path: Path) -> None:
        self._path = database_path
        self._connection: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()

    # --- plumbing --------------------------------------------------------

    async def initialize(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        await self._run(self._connect_and_migrate)

    def _connect_and_migrate(self) -> None:
        connection = sqlite3.connect(self._path, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript(SCHEMA)
        connection.commit()
        self._connection = connection

    async def _run(self, function: Any, *args: Any) -> Any:
        """All SQLite access is serialised through here."""
        async with self._lock:
            return await asyncio.to_thread(function, *args)

    def _require_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise RuntimeError("Repository used before initialize().")
        return self._connection

    def _write(self, sql: str, parameters: tuple[Any, ...]) -> None:
        connection = self._require_connection()
        connection.execute(sql, parameters)
        connection.commit()

    def _query(self, sql: str, parameters: tuple[Any, ...]) -> list[sqlite3.Row]:
        return list(self._require_connection().execute(sql, parameters))

    async def close(self) -> None:
        async with self._lock:
            if self._connection is not None:
                await asyncio.to_thread(self._connection.close)
                self._connection = None

    # --- experiments ------------------------------------------------------

    async def save_experiment(self, experiment: Experiment) -> None:
        await self._run(
            self._write,
            """
            INSERT INTO experiments (
                id, project_name, project_path, objective, base_image, status,
                network_mode, mount_strategy, cpu_limit, memory_limit, memory_bytes,
                timeout_seconds, pids_limit, workspace_path, container_id, snapshot_dir,
                backend_metadata, environment_keys, setup_commands, error, created_at,
                ready_at, started_at, completed_at, destroyed_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                status=excluded.status,
                container_id=excluded.container_id,
                snapshot_dir=excluded.snapshot_dir,
                backend_metadata=excluded.backend_metadata,
                environment_keys=excluded.environment_keys,
                setup_commands=excluded.setup_commands,
                error=excluded.error,
                ready_at=excluded.ready_at,
                started_at=excluded.started_at,
                completed_at=excluded.completed_at,
                destroyed_at=excluded.destroyed_at
            """,
            (
                experiment.id,
                experiment.project_name,
                experiment.project_path,
                experiment.objective,
                experiment.base_image,
                experiment.status.value,
                experiment.network_mode.value,
                experiment.mount_strategy.value,
                experiment.resources.cpu_limit,
                experiment.resources.memory_limit,
                experiment.resources.memory_bytes,
                experiment.resources.timeout_seconds,
                experiment.resources.pids_limit,
                experiment.workspace_path,
                experiment.container_id,
                experiment.snapshot_dir,
                json.dumps(experiment.backend_metadata),
                json.dumps(experiment.environment_keys),
                json.dumps(experiment.setup_commands),
                experiment.error,
                _iso(experiment.created_at),
                _iso(experiment.ready_at),
                _iso(experiment.started_at),
                _iso(experiment.completed_at),
                _iso(experiment.destroyed_at),
            ),
        )

    async def get_experiment(self, experiment_id: str) -> Experiment | None:
        rows = await self._run(
            self._query, "SELECT * FROM experiments WHERE id = ?", (experiment_id,)
        )
        return _row_to_experiment(rows[0]) if rows else None

    async def list_experiments(
        self, status: ExperimentStatus | None = None, limit: int = 100
    ) -> list[Experiment]:
        if status is None:
            rows = await self._run(
                self._query,
                "SELECT * FROM experiments ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
        else:
            rows = await self._run(
                self._query,
                "SELECT * FROM experiments WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                (status.value, limit),
            )
        return [_row_to_experiment(row) for row in rows]

    # --- transitions ------------------------------------------------------

    async def record_transition(self, transition: StateTransition) -> None:
        await self._run(
            self._write,
            """INSERT INTO state_transitions (experiment_id, from_status, to_status, reason, at)
               VALUES (?,?,?,?,?)""",
            (
                transition.experiment_id,
                transition.from_status.value if transition.from_status else None,
                transition.to_status.value,
                transition.reason,
                _iso(transition.at),
            ),
        )

    async def list_transitions(self, experiment_id: str) -> list[StateTransition]:
        rows = await self._run(
            self._query,
            "SELECT * FROM state_transitions WHERE experiment_id = ? ORDER BY id",
            (experiment_id,),
        )
        return [
            StateTransition(
                experiment_id=row["experiment_id"],
                from_status=ExperimentStatus(row["from_status"]) if row["from_status"] else None,
                to_status=ExperimentStatus(row["to_status"]),
                reason=row["reason"],
                at=_require_dt(row["at"]),
            )
            for row in rows
        ]

    # --- jobs -------------------------------------------------------------

    async def save_job(self, job: Job) -> None:
        await self._run(
            self._write,
            """
            INSERT INTO jobs (
                id, experiment_id, command, argv, workdir, kind, status, exit_code,
                stdout, stderr, stdout_truncated, stderr_truncated, timeout_seconds,
                error, created_at, started_at, finished_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                status=excluded.status,
                exit_code=excluded.exit_code,
                stdout=excluded.stdout,
                stderr=excluded.stderr,
                stdout_truncated=excluded.stdout_truncated,
                stderr_truncated=excluded.stderr_truncated,
                error=excluded.error,
                started_at=excluded.started_at,
                finished_at=excluded.finished_at
            """,
            (
                job.id,
                job.experiment_id,
                job.command,
                json.dumps(job.argv),
                job.workdir,
                job.kind,
                job.status.value,
                job.exit_code,
                job.stdout,
                job.stderr,
                int(job.stdout_truncated),
                int(job.stderr_truncated),
                job.timeout_seconds,
                job.error,
                _iso(job.created_at),
                _iso(job.started_at),
                _iso(job.finished_at),
            ),
        )

    async def get_job(self, job_id: str) -> Job | None:
        rows = await self._run(self._query, "SELECT * FROM jobs WHERE id = ?", (job_id,))
        return _row_to_job(rows[0]) if rows else None

    async def list_jobs(self, experiment_id: str) -> list[Job]:
        rows = await self._run(
            self._query,
            "SELECT * FROM jobs WHERE experiment_id = ? ORDER BY created_at, id",
            (experiment_id,),
        )
        return [_row_to_job(row) for row in rows]

    # --- artifacts --------------------------------------------------------

    async def save_artifact(self, artifact: Artifact) -> None:
        await self._run(
            self._write,
            """INSERT OR REPLACE INTO artifacts
               (id, experiment_id, sandbox_path, host_path, size_bytes, sha256, collected_at)
               VALUES (?,?,?,?,?,?,?)""",
            (
                artifact.id,
                artifact.experiment_id,
                artifact.sandbox_path,
                artifact.host_path,
                artifact.size_bytes,
                artifact.sha256,
                _iso(artifact.collected_at),
            ),
        )

    async def list_artifacts(self, experiment_id: str) -> list[Artifact]:
        rows = await self._run(
            self._query,
            "SELECT * FROM artifacts WHERE experiment_id = ? ORDER BY collected_at",
            (experiment_id,),
        )
        return [_row_to_artifact(row) for row in rows]

    async def get_artifact(self, artifact_id: str) -> Artifact | None:
        rows = await self._run(self._query, "SELECT * FROM artifacts WHERE id = ?", (artifact_id,))
        return _row_to_artifact(rows[0]) if rows else None

    # --- change statistics ------------------------------------------------

    async def save_change_stats(self, experiment_id: str, stats: dict[str, Any]) -> None:
        await self._run(
            self._write,
            "INSERT OR REPLACE INTO change_stats (experiment_id, stats, at) VALUES (?,?,?)",
            (experiment_id, json.dumps(stats), _iso(datetime.now(UTC))),
        )

    async def get_change_stats(self, experiment_id: str) -> dict[str, Any] | None:
        rows = await self._run(
            self._query,
            "SELECT stats FROM change_stats WHERE experiment_id = ?",
            (experiment_id,),
        )
        return json.loads(rows[0]["stats"]) if rows else None

    # --- recovery ---------------------------------------------------------

    async def mark_orphaned_jobs_failed(self) -> int:
        """A restarted server has job rows but no asyncio tasks. Anything still
        marked RUNNING is a ghost and must not be reported as live."""

        def run() -> int:
            connection = self._require_connection()
            cursor = connection.execute(
                """UPDATE jobs
                   SET status = ?, error = ?, finished_at = COALESCE(finished_at, ?)
                   WHERE status IN (?, ?)""",
                (
                    JobStatus.FAILED.value,
                    "Abandoned: the server restarted while this job was running.",
                    _iso(datetime.now(UTC)),
                    JobStatus.RUNNING.value,
                    JobStatus.PENDING.value,
                ),
            )
            connection.commit()
            return cursor.rowcount

        return await self._run(run)

    # --- baselines --------------------------------------------------------

    async def save_baseline(self, experiment_id: str, manifest: FileManifest) -> None:
        payload = json.dumps({path: [fp.size, fp.digest, fp.kind] for path, fp in manifest.items()})
        await self._run(
            self._write,
            "INSERT OR REPLACE INTO baselines (experiment_id, manifest) VALUES (?,?)",
            (experiment_id, payload),
        )

    async def get_baseline(self, experiment_id: str) -> FileManifest | None:
        rows = await self._run(
            self._query, "SELECT manifest FROM baselines WHERE experiment_id = ?", (experiment_id,)
        )
        if not rows:
            return None
        raw = json.loads(rows[0]["manifest"])
        return {
            path: FileFingerprint(size=size, digest=digest, kind=kind)
            for path, (size, digest, kind) in raw.items()
        }


# --- row mapping ---------------------------------------------------------


def _row_to_experiment(row: sqlite3.Row) -> Experiment:
    return Experiment(
        id=row["id"],
        project_name=row["project_name"],
        project_path=row["project_path"],
        objective=row["objective"],
        base_image=row["base_image"],
        status=ExperimentStatus(row["status"]),
        network_mode=NetworkMode(row["network_mode"]),
        mount_strategy=MountStrategy(row["mount_strategy"]),
        resources=ResourceLimits(
            cpu_limit=row["cpu_limit"],
            memory_limit=row["memory_limit"],
            memory_bytes=row["memory_bytes"],
            timeout_seconds=row["timeout_seconds"],
            pids_limit=row["pids_limit"],
        ),
        workspace_path=row["workspace_path"],
        container_id=row["container_id"],
        snapshot_dir=row["snapshot_dir"],
        backend_metadata=json.loads(row["backend_metadata"]),
        environment_keys=json.loads(row["environment_keys"]),
        setup_commands=json.loads(row["setup_commands"]),
        error=row["error"],
        created_at=_require_dt(row["created_at"]),
        ready_at=_parse_dt(row["ready_at"]),
        started_at=_parse_dt(row["started_at"]),
        completed_at=_parse_dt(row["completed_at"]),
        destroyed_at=_parse_dt(row["destroyed_at"]),
    )


def _row_to_job(row: sqlite3.Row) -> Job:
    return Job(
        id=row["id"],
        experiment_id=row["experiment_id"],
        command=row["command"],
        argv=json.loads(row["argv"]),
        workdir=row["workdir"],
        kind=row["kind"],
        status=JobStatus(row["status"]),
        exit_code=row["exit_code"],
        stdout=row["stdout"],
        stderr=row["stderr"],
        stdout_truncated=bool(row["stdout_truncated"]),
        stderr_truncated=bool(row["stderr_truncated"]),
        timeout_seconds=row["timeout_seconds"],
        error=row["error"],
        created_at=_require_dt(row["created_at"]),
        started_at=_parse_dt(row["started_at"]),
        finished_at=_parse_dt(row["finished_at"]),
    )


def _row_to_artifact(row: sqlite3.Row) -> Artifact:
    return Artifact(
        id=row["id"],
        experiment_id=row["experiment_id"],
        sandbox_path=row["sandbox_path"],
        host_path=row["host_path"],
        size_bytes=row["size_bytes"],
        sha256=row["sha256"],
        collected_at=_require_dt(row["collected_at"]),
    )
