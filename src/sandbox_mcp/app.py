"""Composition root.

One place where the object graph is wired, so every other module can depend on
interfaces and the tests can swap any layer for a fake. ``server.py`` holds the
MCP surface and nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass

from .artifacts.manager import ArtifactManager
from .config import Settings, get_settings
from .execution.executor import SandboxJobExecutor
from .execution.manager import ExecutionManager
from .experiments.manager import ExperimentManager
from .experiments.repository import ExperimentRepository, SQLiteRepository
from .logging import get_logger
from .sandbox.docker import DockerSandboxBackend
from .sandbox.interface import SandboxBackend
from .security.policy import PolicyEngine

log = get_logger(__name__)


@dataclass(slots=True)
class SandboxMCPApp:
    settings: Settings
    backend: SandboxBackend
    repository: ExperimentRepository
    execution: ExecutionManager
    artifacts: ArtifactManager
    experiments: ExperimentManager

    @classmethod
    def build(
        cls,
        settings: Settings | None = None,
        backend: SandboxBackend | None = None,
        repository: ExperimentRepository | None = None,
    ) -> SandboxMCPApp:
        resolved = settings or get_settings()
        resolved.ensure_directories()

        sandbox_backend = backend or DockerSandboxBackend(resolved)
        store = repository or SQLiteRepository(resolved.database_path)
        execution = ExecutionManager(resolved, SandboxJobExecutor(sandbox_backend), store)
        artifacts = ArtifactManager(resolved, sandbox_backend)
        experiments = ExperimentManager(
            settings=resolved,
            backend=sandbox_backend,
            repository=store,
            execution=execution,
            artifacts=artifacts,
            policy=PolicyEngine(resolved),
        )
        return cls(
            settings=resolved,
            backend=sandbox_backend,
            repository=store,
            execution=execution,
            artifacts=artifacts,
            experiments=experiments,
        )

    async def startup(self) -> None:
        await self.experiments.startup()
        log.info(
            "server_ready",
            operation="startup",
            state_dir=str(self.settings.state_dir),
            docker_host=self.settings.resolved_docker_host(),
            default_network_mode=self.settings.default_network_mode.value,
            default_image=self.settings.default_base_image,
        )

    async def shutdown(self) -> None:
        await self.repository.close()
        log.info("server_stopped", operation="shutdown")
