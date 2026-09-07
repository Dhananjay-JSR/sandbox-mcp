from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

from sandbox_mcp.app import SandboxMCPApp
from sandbox_mcp.config import Settings
from sandbox_mcp.errors import SandboxMCPError
from sandbox_mcp.experiments.repository import SQLiteRepository
from sandbox_mcp.sandbox.docker import DockerSandboxBackend

# Small and universally available. Everything here must work on the busybox
# userland, which is the harshest environment the backend has to support.
TEST_IMAGE = "alpine:3.20"

pytestmark = pytest.mark.integration


def _docker_available() -> bool:
    async def probe() -> bool:
        try:
            await DockerSandboxBackend(Settings()).health_check()
        except SandboxMCPError:
            return False
        return True

    try:
        return asyncio.run(probe())
    except Exception:
        return False


DOCKER_AVAILABLE = _docker_available()

requires_docker = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="no reachable Docker daemon (set DOCKER_HOST or start Docker)"
)


@pytest.fixture
def docker_settings(tmp_path: Path) -> Settings:
    return Settings(
        state_dir=tmp_path / "state",
        log_level="CRITICAL",
        default_base_image=TEST_IMAGE,
        default_memory_limit="256MB",
        default_cpu_limit=1,
        default_timeout_seconds=60,
    )


@pytest.fixture
async def docker_app(docker_settings: Settings) -> AsyncIterator[SandboxMCPApp]:
    application = SandboxMCPApp.build(
        settings=docker_settings,
        backend=DockerSandboxBackend(docker_settings),
        repository=SQLiteRepository(docker_settings.database_path),
    )
    await application.startup()
    yield application
    # Belt and braces: never leave a container behind, even if a test failed.
    for experiment in await application.repository.list_experiments(limit=100):
        if experiment.status.value != "DESTROYED":
            with contextlib.suppress(SandboxMCPError):
                await application.experiments.destroy(experiment.id)
    await application.shutdown()


@pytest.fixture
def node_project(tmp_path: Path) -> Iterator[Path]:
    root = tmp_path / "svc"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.js").write_text("module.exports = () => 'hello';\n")
    (root / "README.md").write_text("# svc\n")
    (root / ".env").write_text("API_KEY=must-not-be-copied\n")
    yield root
