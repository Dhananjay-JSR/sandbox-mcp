"""What actually runs a command.

Kept behind an interface for the same reason the sandbox is: the job lifecycle
(state, persistence, timeouts, cancellation) should not have to change when the
execution substrate does.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import Job
from ..sandbox.interface import ExecOutcome, SandboxBackend, SandboxHandle


class JobExecutor(ABC):
    @abstractmethod
    async def run(self, job: Job, handle: SandboxHandle) -> ExecOutcome:
        """Run one job to completion and return its outcome.

        Must not raise for ordinary command failure -- a non-zero exit code is
        a result, not an error.
        """


class SandboxJobExecutor(JobExecutor):
    """Runs jobs inside a :class:`SandboxBackend`. The only implementation that
    matters today, and the reason no command ever reaches the host shell."""

    def __init__(self, backend: SandboxBackend) -> None:
        self._backend = backend

    async def run(self, job: Job, handle: SandboxHandle) -> ExecOutcome:
        return await self._backend.execute(
            handle,
            command=job.command,
            timeout=job.timeout_seconds,
            workdir=job.workdir,
        )
