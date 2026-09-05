"""Structured error types.

Every failure the MCP client can observe is one of these. The server converts
them into a compact ``[CODE] message`` string plus a structured ``details``
payload; raw tracebacks stay in the server log and never reach the agent.
"""

from __future__ import annotations

from typing import Any


class SandboxMCPError(Exception):
    """Base class for every error surfaced to an MCP client."""

    code = "INTERNAL_ERROR"
    """Stable, machine-readable identifier. Agents may branch on this."""

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": self.message, "details": self.details}}

    def __str__(self) -> str:
        return f"[{self.code}] {self.message}"


# --- infrastructure -------------------------------------------------------


class DockerUnavailableError(SandboxMCPError):
    """The Docker daemon could not be reached."""

    code = "DOCKER_UNAVAILABLE"


class ImageError(SandboxMCPError):
    """The requested base image is missing, unpullable, or not permitted."""

    code = "INVALID_IMAGE"


class SandboxStartupError(SandboxMCPError):
    """The container could not be created or started."""

    code = "SANDBOX_STARTUP_FAILED"


class SandboxBackendError(SandboxMCPError):
    """The sandbox backend failed while servicing a request."""

    code = "SANDBOX_BACKEND_ERROR"


# --- policy ---------------------------------------------------------------


class PolicyViolationError(SandboxMCPError):
    """The request was rejected by the security policy."""

    code = "POLICY_VIOLATION"


class UnauthorizedPathError(SandboxMCPError):
    """A filesystem path fell outside the permitted roots."""

    code = "UNAUTHORIZED_PATH"


class InvalidProjectPathError(SandboxMCPError):
    """The project path does not exist or is not a directory."""

    code = "INVALID_PROJECT_PATH"


# --- domain ---------------------------------------------------------------


class ExperimentNotFoundError(SandboxMCPError):
    code = "EXPERIMENT_NOT_FOUND"


class ExperimentDestroyedError(SandboxMCPError):
    """The experiment exists but its sandbox is gone."""

    code = "EXPERIMENT_DESTROYED"


class InvalidStateTransitionError(SandboxMCPError):
    code = "INVALID_STATE_TRANSITION"


class JobNotFoundError(SandboxMCPError):
    code = "JOB_NOT_FOUND"


class JobNotFinishedError(SandboxMCPError):
    """A result was requested for a job that is still running."""

    code = "JOB_NOT_FINISHED"


class JobTimeoutError(SandboxMCPError):
    code = "JOB_TIMEOUT"


class JobCancelledError(SandboxMCPError):
    code = "JOB_CANCELLED"


class ArtifactNotFoundError(SandboxMCPError):
    code = "ARTIFACT_NOT_FOUND"


class CapacityError(SandboxMCPError):
    """Too many live experiments; destroy one before creating another."""

    code = "CAPACITY_EXCEEDED"
