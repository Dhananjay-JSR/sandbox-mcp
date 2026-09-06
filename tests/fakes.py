"""In-memory sandbox backend.

Lets the unit tests exercise the manager, the job lifecycle and the whole MCP
surface without a Docker daemon -- which is the point of having
:class:`SandboxBackend` be an interface. It models the parts of a sandbox the
domain actually depends on: a filesystem, command results, and the ability to
be destroyed exactly once.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sandbox_mcp.errors import SandboxBackendError, SandboxStartupError
from sandbox_mcp.models import ExperimentSpec
from sandbox_mcp.sandbox.interface import ExecOutcome, SandboxBackend, SandboxHandle
from sandbox_mcp.security.filesystem import FileFingerprint, FileManifest

CommandHandler = Callable[[str], ExecOutcome]


@dataclass
class FakeSandbox:
    files: dict[str, bytes] = field(default_factory=dict)
    destroyed: bool = False
    spec: ExperimentSpec | None = None


class FakeSandboxBackend(SandboxBackend):
    name = "fake"

    def __init__(
        self,
        responses: dict[str, ExecOutcome] | None = None,
        default: ExecOutcome | None = None,
        fail_create: bool = False,
    ) -> None:
        self.sandboxes: dict[str, FakeSandbox] = {}
        self.responses = responses or {}
        self.default = default or ExecOutcome(exit_code=0, stdout="", stderr="")
        self.fail_create = fail_create
        self.commands: list[str] = []
        self.destroy_calls = 0
        self._counter = 0
        self.command_delay = 0.0

    async def health_check(self) -> dict[str, Any]:
        return {"backend": self.name, "ok": True}

    async def create(self, spec: ExperimentSpec, snapshot_dir: str | None) -> SandboxHandle:
        if self.fail_create:
            raise SandboxStartupError("fake backend refused to start", image=spec.base_image)
        self._counter += 1
        sandbox_id = f"fake{self._counter:04d}"
        sandbox = FakeSandbox(spec=spec)
        if snapshot_dir:
            from pathlib import Path

            root = Path(snapshot_dir)
            for path in root.rglob("*"):
                if path.is_file():
                    sandbox.files[path.relative_to(root).as_posix()] = path.read_bytes()
        self.sandboxes[sandbox_id] = sandbox
        return SandboxHandle(
            sandbox_id=sandbox_id,
            workspace=spec.workspace_path,
            backend=self.name,
            metadata={"image": spec.base_image},
        )

    def _require(self, handle: SandboxHandle) -> FakeSandbox:
        sandbox = self.sandboxes.get(handle.sandbox_id)
        if sandbox is None or sandbox.destroyed:
            raise SandboxBackendError("sandbox is gone", sandbox_id=handle.sandbox_id)
        return sandbox

    async def execute(
        self,
        handle: SandboxHandle,
        command: str,
        timeout: int,
        workdir: str | None = None,
        environment: dict[str, str] | None = None,
    ) -> ExecOutcome:
        self._require(handle)
        self.commands.append(command)
        if self.command_delay:
            await asyncio.sleep(self.command_delay)
        for pattern, outcome in self.responses.items():
            if pattern in command:
                return outcome
        return self.default

    async def read_manifest(self, handle: SandboxHandle, excludes: list[str]) -> FileManifest:
        sandbox = self._require(handle)
        return {
            path: FileFingerprint(size=len(body), digest=hashlib.sha256(body).hexdigest())
            for path, body in sandbox.files.items()
        }

    async def read_file(self, handle: SandboxHandle, path: str, max_bytes: int) -> bytes:
        sandbox = self._require(handle)
        relative = path.removeprefix(handle.workspace).lstrip("/")
        if relative not in sandbox.files:
            raise SandboxBackendError(f"no such file: {path}", path=path)
        return sandbox.files[relative]

    async def write_file(self, handle: SandboxHandle, path: str, content: bytes) -> None:
        sandbox = self._require(handle)
        sandbox.files[path.removeprefix(handle.workspace).lstrip("/")] = content

    async def destroy(self, handle: SandboxHandle) -> bool:
        self.destroy_calls += 1
        sandbox = self.sandboxes.get(handle.sandbox_id)
        if sandbox is None or sandbox.destroyed:
            return False
        sandbox.destroyed = True
        return True

    # Convenience for tests that assert on sandbox contents.
    def files(self, handle_or_id: SandboxHandle | str) -> dict[str, bytes]:
        key = handle_or_id.sandbox_id if isinstance(handle_or_id, SandboxHandle) else handle_or_id
        return self.sandboxes[key].files


def outcome(
    exit_code: int = 0, stdout: str = "", stderr: str = "", timed_out: bool = False
) -> ExecOutcome:
    return ExecOutcome(exit_code=exit_code, stdout=stdout, stderr=stderr, timed_out=timed_out)
