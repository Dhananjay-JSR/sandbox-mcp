"""The contract a sandbox runtime must satisfy.

Nothing above this layer knows what a container is. That is deliberate: swapping
Docker for Firecracker, a Kubernetes Job or a remote worker means writing one
new implementation of :class:`SandboxBackend`, and no change at all to the MCP
tool surface or the experiment manager.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from ..models import ExperimentSpec
from ..security.filesystem import FileManifest


@dataclass(slots=True)
class SandboxHandle:
    """Opaque reference to a live sandbox, owned by the experiment record."""

    sandbox_id: str
    workspace: str
    backend: str = "docker"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ExecOutcome:
    """Result of one command. ``timed_out`` and ``cancelled`` are distinct
    outcomes from a non-zero exit code and must not be collapsed into one."""

    exit_code: int | None
    stdout: str
    stderr: str
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    timed_out: bool = False
    cancelled: bool = False


class SandboxBackend(ABC):
    """Lifecycle and I/O for disposable execution environments."""

    name: str = "abstract"

    @abstractmethod
    async def health_check(self) -> dict[str, Any]:
        """Confirm the runtime is reachable. Raises DockerUnavailableError if not."""

    @abstractmethod
    async def create(self, spec: ExperimentSpec, snapshot_dir: str | None) -> SandboxHandle:
        """Provision an isolated environment and seed it with the project copy."""

    @abstractmethod
    async def execute(
        self,
        handle: SandboxHandle,
        command: str,
        timeout: int,
        workdir: str | None = None,
        environment: dict[str, str] | None = None,
    ) -> ExecOutcome:
        """Run one shell command inside the sandbox and capture its streams.

        Must enforce ``timeout`` by terminating the process, and must return
        rather than raise for ordinary command failure.
        """

    @abstractmethod
    async def read_manifest(self, handle: SandboxHandle, excludes: list[str]) -> FileManifest:
        """Fingerprint the workspace as it stands now, for diffing."""

    @abstractmethod
    async def read_file(self, handle: SandboxHandle, path: str, max_bytes: int) -> bytes:
        """Fetch one file out of the sandbox."""

    @abstractmethod
    async def write_file(self, handle: SandboxHandle, path: str, content: bytes) -> None:
        """Place one file into the sandbox."""

    @abstractmethod
    async def destroy(self, handle: SandboxHandle) -> bool:
        """Tear the sandbox down. Must be idempotent and must not raise if it
        is already gone. Returns whether anything was actually removed."""
