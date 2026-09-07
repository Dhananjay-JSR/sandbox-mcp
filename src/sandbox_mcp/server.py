"""The MCP surface.

Tool names are domain operations -- ``create_experiment``, ``run_tests``,
``inspect_changes`` -- not Docker verbs. That is the whole design: the agent
reasons about experiments, and Docker stays an implementation detail it cannot
reach. There is no tool here that runs a command on the host, no tool that
takes a host path to write to, and no tool that exposes the Docker API.

Descriptions are written for the model, not for a human browsing docs: each one
says what the tool does, when to reach for it, what comes back, and what it
will refuse to do.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from functools import wraps
from typing import Annotated, Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from .app import SandboxMCPApp
from .config import Settings
from .errors import SandboxMCPError
from .logging import get_logger
from .models import (
    ArtifactCollectionResult,
    ChangeSet,
    CreateExperimentResult,
    DestroyResult,
    ExecutionResult,
    ExperimentComparison,
    ExperimentReport,
    ExperimentStatus,
    JobStatusResult,
    TestRunResult,
)

log = get_logger(__name__)

INSTRUCTIONS = """\
Sandbox MCP gives you disposable, isolated environments to experiment in.

Use it whenever a task would otherwise modify the developer's machine: installing
dependencies, running builds or migrations, trying a risky refactor, testing an
upgrade, or running code you have not read. Create an experiment, work inside it,
inspect what changed, then destroy it. The developer's working tree is copied,
never mounted for writing, so nothing you do inside a sandbox can reach it.

Typical flow:
  create_experiment -> execute_experiment / write_sandbox_file -> run_tests
  -> inspect_changes -> collect_artifacts -> destroy_experiment

Defaults are deliberately strict: no network, no host environment variables,
capped CPU, memory and wall-clock. Ask for more explicitly when the experiment
genuinely needs it (network_mode='restricted' for a package install, for
example) and say why in the objective.

Always destroy_experiment when you are done, and report back what changed.
"""


def _tool_errors[F: Callable[..., Any]](function: F) -> F:
    """Turn domain errors into clean MCP errors.

    The agent gets a stable code, a sentence it can act on, and the structured
    details. It never gets a Python traceback -- those go to the log, where
    they belong.
    """

    @wraps(function)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await function(*args, **kwargs)
        except SandboxMCPError as exc:
            detail = f" {json.dumps(exc.details, default=str)}" if exc.details else ""
            raise ToolError(f"{exc}{detail}") from None
        except Exception as exc:
            log.exception("tool_failed", operation=function.__name__)
            raise ToolError(
                f"[INTERNAL_ERROR] {function.__name__} failed: {type(exc).__name__}. "
                "See the server log for details."
            ) from None

    return wrapper  # type: ignore[return-value]


def create_server(app: SandboxMCPApp | None = None, settings: Settings | None = None) -> FastMCP:
    """Build the MCP server around a wired application."""
    application = app or SandboxMCPApp.build(settings)
    experiments = application.experiments

    @asynccontextmanager
    async def lifespan(_server: FastMCP) -> AsyncIterator[dict[str, Any]]:
        await application.startup()
        try:
            yield {"app": application}
        finally:
            await application.shutdown()

    mcp: FastMCP = FastMCP(
        name="Sandbox MCP",
        version="0.1.0",
        instructions=INSTRUCTIONS,
        lifespan=lifespan,
    )

    # -- lifecycle ---------------------------------------------------------

    @mcp.tool(
        name="create_experiment",
        description=(
            "Create a disposable, isolated environment and copy a project into it.\n\n"
            "USE THIS when you are about to do something you should not do on the "
            "developer's machine: install dependencies, run a build or a migration, try "
            "an upgrade, run unfamiliar code, or explore a fix you are not sure about. "
            "Reach for it *before* the risky step, not after.\n\n"
            "The project is SNAPSHOT-COPIED into the sandbox. Files you change inside "
            "never propagate back; the developer's working tree is untouched by "
            "construction. Secrets (.env files, keys, credential directories) are "
            "withheld from the copy, as are node_modules and other build output.\n\n"
            "RETURNS an experiment_id plus the isolation actually applied -- read the "
            "`warnings` field, which tells you where your request was clamped.\n\n"
            "SAFETY: network is disabled unless you ask for it; no host environment "
            "variable is passed unless you name it, and credential-shaped names are "
            "refused even then; CPU, memory, PIDs and wall-clock are capped."
        ),
    )
    @_tool_errors
    async def create_experiment(
        project_path: Annotated[
            str | None,
            Field(
                description=(
                    "Absolute path to the project to copy in. Omit for an empty sandbox. "
                    "Must not be a home or system directory."
                )
            ),
        ] = None,
        base_image: Annotated[
            str | None,
            Field(
                description=(
                    "Docker image to run in, e.g. 'node:22-slim', 'python:3.12-slim'. "
                    "Choose the runtime the experiment is actually about."
                )
            ),
        ] = None,
        network_mode: Annotated[
            str | None,
            Field(
                description=(
                    "'none' (default, no network at all), 'restricted' (egress on a "
                    "private bridge, no reach to other sandboxes), or 'enabled'. Use "
                    "'restricted' when you must install packages."
                )
            ),
        ] = None,
        cpu_limit: Annotated[float | None, Field(description="CPU cores. Clamped.")] = None,
        memory_limit: Annotated[
            str | None, Field(description="Memory, e.g. '2GB'. Clamped.")
        ] = None,
        timeout: Annotated[
            int | None,
            Field(description="Default per-command wall-clock limit in seconds."),
        ] = None,
        environment_allowlist: Annotated[
            list[str] | None,
            Field(
                description=(
                    "Environment variables to expose. 'NAME' forwards the host's value; "
                    "'NAME=value' injects a literal. Nothing else crosses the boundary."
                )
            ),
        ] = None,
        setup_commands: Annotated[
            list[str] | None,
            Field(
                description=(
                    "Commands to run once the sandbox is ready, in order. Stops at the "
                    "first failure and reports it."
                )
            ),
        ] = None,
        mount_strategy: Annotated[
            str | None,
            Field(
                description=(
                    "'COPY_TO_SANDBOX' (default, safest) or 'READ_ONLY_BIND_MOUNT' for a "
                    "large repo you only need to read. Writable host mounts do not exist."
                )
            ),
        ] = None,
        objective: Annotated[
            str | None,
            Field(description="What you are trying to find out. Shows up in the report."),
        ] = None,
    ) -> CreateExperimentResult:
        return await experiments.create(
            project_path=project_path,
            base_image=base_image,
            network_mode=network_mode,
            mount_strategy=mount_strategy,
            cpu_limit=cpu_limit,
            memory_limit=memory_limit,
            timeout=timeout,
            environment_allowlist=environment_allowlist,
            setup_commands=setup_commands,
            objective=objective,
        )

    @mcp.tool(
        name="destroy_experiment",
        description=(
            "Destroy a sandbox and everything in it, returning a final report.\n\n"
            "USE THIS as soon as an experiment has told you what you needed. Always "
            "call it -- a sandbox left running keeps consuming the developer's CPU and "
            "memory.\n\n"
            "IDEMPOTENT: calling it on an already-destroyed experiment is safe and "
            "returns the stored report rather than an error.\n\n"
            "RETURNS the final report, including what changed inside the sandbox, so "
            "you can summarise the experiment after it is gone."
        ),
    )
    @_tool_errors
    async def destroy_experiment(
        experiment_id: Annotated[str, Field(description="The experiment to destroy.")],
    ) -> DestroyResult:
        return await experiments.destroy(experiment_id)

    # -- execution ---------------------------------------------------------

    @mcp.tool(
        name="execute_experiment",
        description=(
            "Run a shell command inside a sandbox.\n\n"
            "USE THIS for anything you would otherwise run in the developer's terminal: "
            "installs, builds, scripts, migrations, one-off exploration. The command "
            "runs in the container, never on the host.\n\n"
            "RETURNS exit code, stdout, stderr and duration. A non-zero exit is a "
            "normal result, not an error -- read it and decide what to try next.\n\n"
            "Set background=true for something long-running, then poll get_job_status "
            "and fetch get_job_result when it finishes. Otherwise this waits, bounded "
            "by the timeout, so it cannot hang your session."
        ),
    )
    @_tool_errors
    async def execute_experiment(
        experiment_id: Annotated[str, Field(description="The experiment to run in.")],
        command: Annotated[
            str, Field(description="Shell command. Runs via /bin/sh in the workspace.")
        ],
        timeout: Annotated[
            int | None,
            Field(
                description="Seconds before the command is killed. Defaults to the experiment's."
            ),
        ] = None,
        workdir: Annotated[
            str | None, Field(description="Working directory. Must be inside the workspace.")
        ] = None,
        background: Annotated[
            bool,
            Field(description="Return a job id immediately instead of waiting."),
        ] = False,
    ) -> ExecutionResult:
        job = await experiments.execute(
            experiment_id, command, timeout=timeout, workdir=workdir, background=background
        )
        return ExecutionResult.from_job(job)

    @mcp.tool(
        name="run_tests",
        description=(
            "Run the project's test suite inside a sandbox and parse the results.\n\n"
            "USE THIS instead of execute_experiment when you want to know whether the "
            "project still works -- it detects the runner (npm, pytest, cargo, go, make) "
            "by looking at what is actually in the sandbox, and parses counts out of the "
            "output.\n\n"
            "RETURNS exit code, stdout/stderr, duration and, when parseable, a summary "
            "with passed/failed/total and the names of failing tests. If "
            "`test_summary.detected` is false, trust the exit code, not the zeros.\n\n"
            "Pass `command` to override detection."
        ),
    )
    @_tool_errors
    async def run_tests(
        experiment_id: Annotated[str, Field(description="The experiment to test in.")],
        command: Annotated[
            str | None,
            Field(description="Explicit test command. Omit to auto-detect."),
        ] = None,
        timeout: Annotated[
            int | None, Field(description="Seconds before the run is killed.")
        ] = None,
    ) -> TestRunResult:
        job, framework, summary = await experiments.run_tests(experiment_id, command, timeout)
        base = ExecutionResult.from_job(job)
        return TestRunResult(
            **base.model_dump(),
            framework=framework or summary.framework,
            test_summary=summary,
        )

    # -- working inside the sandbox ---------------------------------------

    @mcp.tool(
        name="read_sandbox_file",
        description=(
            "Read a file from inside the sandbox as text.\n\n"
            "USE THIS to investigate a failure -- read the source, the config, a log -- "
            "without guessing from stack traces. Paths are workspace-relative and cannot "
            "escape it; this tool cannot read the developer's machine."
        ),
    )
    @_tool_errors
    async def read_sandbox_file(
        experiment_id: Annotated[str, Field(description="The experiment to read from.")],
        path: Annotated[str, Field(description="Workspace-relative path, e.g. 'src/index.js'.")],
    ) -> str:
        return await experiments.read_file(experiment_id, path)

    @mcp.tool(
        name="write_sandbox_file",
        description=(
            "Write a file inside the sandbox, creating parent directories as needed.\n\n"
            "USE THIS to apply a candidate fix. Prefer it over shell heredocs: no "
            "quoting to get wrong.\n\n"
            "SAFETY: writes land in the sandbox copy only. There is no tool here that "
            "writes to the developer's project -- if a change is worth keeping, show "
            "them the diff from inspect_changes and let them apply it."
        ),
    )
    @_tool_errors
    async def write_sandbox_file(
        experiment_id: Annotated[str, Field(description="The experiment to write in.")],
        path: Annotated[str, Field(description="Workspace-relative path.")],
        content: Annotated[str, Field(description="Full new contents of the file.")],
    ) -> str:
        resolved = await experiments.write_file(experiment_id, path, content)
        return f"Wrote {len(content)} bytes to {resolved} (sandbox only)."

    # -- inspection --------------------------------------------------------

    @mcp.tool(
        name="inspect_changes",
        description=(
            "Show what the experiment changed, against the project as it was copied in.\n\n"
            "USE THIS before destroying a sandbox, and before telling the developer what "
            "you found. It is the evidence for your conclusion.\n\n"
            "RETURNS created / modified / deleted file lists plus insertion and deletion "
            "counts, and optionally the unified diff. Build output and dependency "
            "directories are excluded, so a 30,000-file node_modules will not bury the "
            "two lines that matter."
        ),
    )
    @_tool_errors
    async def inspect_changes(
        experiment_id: Annotated[str, Field(description="The experiment to inspect.")],
        include_diff: Annotated[
            bool, Field(description="Include unified diff bodies, not just statistics.")
        ] = False,
        max_files_with_diff: Annotated[
            int, Field(description="Cap on files given line-level detail.", ge=1, le=500)
        ] = 50,
    ) -> ChangeSet:
        return await experiments.inspect_changes(
            experiment_id, include_diff=include_diff, max_files_with_diff=max_files_with_diff
        )

    @mcp.tool(
        name="collect_artifacts",
        description=(
            "Copy selected files out of a sandbox before it is destroyed.\n\n"
            "USE THIS for build output, test reports, coverage, benchmark results or "
            "logs you want to keep or quote.\n\n"
            "Patterns are workspace-relative shell globs ('dist/*.js'), or '**/name' for "
            "a recursive search ('**/junit.xml'). Files land in the server's own state "
            "directory; this tool cannot write anywhere on the developer's machine."
        ),
    )
    @_tool_errors
    async def collect_artifacts(
        experiment_id: Annotated[str, Field(description="The experiment to collect from.")],
        patterns: Annotated[
            list[str], Field(description="Workspace-relative globs, e.g. ['dist/*.js'].")
        ],
    ) -> ArtifactCollectionResult:
        return await experiments.collect_artifacts(experiment_id, patterns)

    @mcp.tool(
        name="get_experiment",
        description=(
            "Fetch an experiment's full state and a summary of what happened in it.\n\n"
            "USE THIS to re-orient -- after a long gap, or to check whether a sandbox is "
            "still alive before sending more commands.\n\n"
            "RETURNS status, base image, isolation settings, resource limits, how many "
            "commands ran and how many failed, the last test summary, change statistics "
            "and artifact count."
        ),
    )
    @_tool_errors
    async def get_experiment(
        experiment_id: Annotated[str, Field(description="The experiment to describe.")],
    ) -> ExperimentReport:
        return await experiments.report(experiment_id)

    @mcp.tool(
        name="list_experiments",
        description=(
            "List experiments, most recent first.\n\n"
            "USE THIS to find a sandbox you created earlier, or to check for ones you "
            "forgot to destroy before creating another."
        ),
    )
    @_tool_errors
    async def list_experiments(
        status: Annotated[
            str | None,
            Field(description="Filter by status, e.g. 'READY', 'FAILED', 'DESTROYED'."),
        ] = None,
        limit: Annotated[int, Field(description="Maximum rows.", ge=1, le=200)] = 20,
    ) -> list[dict[str, Any]]:
        parsed = ExperimentStatus(status.upper()) if status else None
        records = await experiments.list_experiments(status=parsed, limit=limit)
        return [
            {
                "experiment_id": record.id,
                "project_name": record.project_name,
                "objective": record.objective,
                "base_image": record.base_image,
                "status": record.status.value,
                "network_mode": record.network_mode.value,
                "created_at": record.created_at.isoformat(),
                "destroyed_at": record.destroyed_at.isoformat() if record.destroyed_at else None,
            }
            for record in records
        ]

    # -- asynchronous jobs -------------------------------------------------

    @mcp.tool(
        name="get_job_status",
        description=(
            "Check on a command started with background=true.\n\n"
            "RETURNS status and elapsed milliseconds. Poll this, then call "
            "get_job_result once `finished` is true."
        ),
    )
    @_tool_errors
    async def get_job_status(
        job_id: Annotated[str, Field(description="Job id from execute_experiment.")],
    ) -> JobStatusResult:
        job = await application.execution.get_job(job_id)
        return JobStatusResult(
            job_id=job.id,
            experiment_id=job.experiment_id,
            status=job.status,
            command=job.command,
            elapsed_ms=job.elapsed_ms(),
            exit_code=job.exit_code,
            finished=job.status.is_terminal,
        )

    @mcp.tool(
        name="get_job_result",
        description=(
            "Fetch the completed result of a background command.\n\n"
            "RETURNS the same shape as execute_experiment: exit code, stdout, stderr, "
            "duration. If the job is still running this waits for it, bounded by the "
            "job's own timeout."
        ),
    )
    @_tool_errors
    async def get_job_result(
        job_id: Annotated[str, Field(description="Job id to fetch.")],
    ) -> ExecutionResult:
        job = await application.execution.wait_for(job_id)
        return ExecutionResult.from_job(job)

    @mcp.tool(
        name="cancel_job",
        description=(
            "Stop a running command and kill its process inside the sandbox.\n\n"
            "USE THIS when a command is clearly stuck or no longer needed. Safe on a "
            "job that already finished -- it returns the final state untouched."
        ),
    )
    @_tool_errors
    async def cancel_job(
        job_id: Annotated[str, Field(description="Job id to cancel.")],
    ) -> ExecutionResult:
        job = await application.execution.cancel(job_id)
        return ExecutionResult.from_job(job)

    # -- comparison --------------------------------------------------------

    @mcp.tool(
        name="compare_experiments",
        description=(
            "Compare two or more experiments side by side.\n\n"
            "USE THIS when you tried several approaches -- three candidate fixes, two "
            "runtime versions, a couple of dependency upgrades -- and have to recommend "
            "one. Run each approach in its own experiment, then compare.\n\n"
            "RETURNS per-experiment test results, failing test names, exit codes, change "
            "size, duration and artifact counts, plus a recommendation when the evidence "
            "supports one. Destroyed experiments are still comparable: their findings "
            "were recorded before teardown."
        ),
    )
    @_tool_errors
    async def compare_experiments(
        experiment_ids: Annotated[
            list[str], Field(description="Two or more experiment ids.", min_length=2)
        ],
        labels: Annotated[
            dict[str, str] | None,
            Field(description="Optional experiment_id -> human label, e.g. {'exp_a': 'Node 22'}."),
        ] = None,
    ) -> ExperimentComparison:
        return await experiments.compare(experiment_ids, labels)

    @mcp.tool(
        name="check_sandbox_runtime",
        description=(
            "Confirm the sandbox runtime is available and report the active defaults.\n\n"
            "USE THIS first if create_experiment fails, to tell a stopped Docker daemon "
            "apart from a rejected request. Returns the Docker version the server is "
            "talking to and the isolation defaults every experiment starts from."
        ),
    )
    @_tool_errors
    async def check_sandbox_runtime() -> dict[str, Any]:
        health = await application.backend.health_check()
        settings_ = application.settings
        return {
            **health,
            "defaults": {
                "base_image": settings_.default_base_image,
                "network_mode": settings_.default_network_mode.value,
                "mount_strategy": settings_.default_mount_strategy.value,
                "cpu_limit": settings_.default_cpu_limit,
                "memory_limit": settings_.default_memory_limit,
                "timeout_seconds": settings_.default_timeout_seconds,
            },
            "ceilings": {
                "cpu_limit": settings_.max_cpu_limit,
                "memory_limit": settings_.max_memory_limit,
                "timeout_seconds": settings_.max_timeout_seconds,
                "concurrent_experiments": settings_.max_concurrent_experiments,
            },
        }

    # -- resources ---------------------------------------------------------

    @mcp.resource(
        "sandbox://experiments",
        name="experiments",
        description="Every experiment this server knows about, most recent first.",
        mime_type="application/json",
    )
    async def experiments_resource() -> dict[str, Any]:
        records = await experiments.list_experiments(limit=100)
        return {
            "experiments": [
                {
                    "experiment_id": record.id,
                    "project_name": record.project_name,
                    "status": record.status.value,
                    "base_image": record.base_image,
                    "objective": record.objective,
                    "created_at": record.created_at.isoformat(),
                }
                for record in records
            ]
        }

    @mcp.resource(
        "sandbox://experiments/{experiment_id}",
        name="experiment",
        description="Full metadata, state history and current status for one experiment.",
        mime_type="application/json",
    )
    async def experiment_resource(experiment_id: str) -> dict[str, Any]:
        record = await experiments.get(experiment_id)
        transitions = await application.repository.list_transitions(experiment_id)
        return {
            "experiment": record.model_dump(mode="json"),
            "state_history": [
                {
                    "from": transition.from_status.value if transition.from_status else None,
                    "to": transition.to_status.value,
                    "reason": transition.reason,
                    "at": transition.at.isoformat(),
                }
                for transition in transitions
            ],
        }

    @mcp.resource(
        "sandbox://experiments/{experiment_id}/logs",
        name="experiment_logs",
        description="Every command run in an experiment, with exit codes and output.",
        mime_type="application/json",
    )
    async def experiment_logs_resource(experiment_id: str) -> dict[str, Any]:
        jobs = await application.repository.list_jobs(experiment_id)
        return {
            "experiment_id": experiment_id,
            "jobs": [
                {
                    "job_id": job.id,
                    "kind": job.kind,
                    "command": job.command,
                    "status": job.status.value,
                    "exit_code": job.exit_code,
                    "duration_ms": job.duration_ms,
                    "stdout": job.stdout,
                    "stderr": job.stderr,
                    "started_at": job.started_at.isoformat() if job.started_at else None,
                }
                for job in jobs
            ],
        }

    @mcp.resource(
        "sandbox://experiments/{experiment_id}/diff",
        name="experiment_diff",
        description="Unified diff of everything the experiment changed in its sandbox.",
        mime_type="application/json",
    )
    async def experiment_diff_resource(experiment_id: str) -> dict[str, Any]:
        changeset = await experiments.inspect_changes(experiment_id, include_diff=True)
        return changeset.model_dump(mode="json")

    @mcp.resource(
        "sandbox://experiments/{experiment_id}/artifacts",
        name="experiment_artifacts",
        description="Files collected out of an experiment, with sizes and checksums.",
        mime_type="application/json",
    )
    async def experiment_artifacts_resource(experiment_id: str) -> dict[str, Any]:
        artifacts = await application.repository.list_artifacts(experiment_id)
        return {
            "experiment_id": experiment_id,
            "artifacts": [artifact.model_dump(mode="json") for artifact in artifacts],
        }

    return mcp
