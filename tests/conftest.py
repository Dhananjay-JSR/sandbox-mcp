from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from sandbox_mcp.app import SandboxMCPApp
from sandbox_mcp.config import Settings
from sandbox_mcp.experiments.repository import SQLiteRepository
from sandbox_mcp.logging import configure_logging
from tests.fakes import FakeSandboxBackend

configure_logging("CRITICAL", json_output=True)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Isolated state directory per test; nothing touches the real ~/.sandbox-mcp."""
    return Settings(
        state_dir=tmp_path / "state",
        log_level="CRITICAL",
        default_base_image="alpine:3.20",
        default_timeout_seconds=30,
        max_concurrent_experiments=5,
    )


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A small, realistic project tree, including things that must not be copied."""
    root = tmp_path / "demo-project"
    (root / "src").mkdir(parents=True)
    (root / "node_modules" / "left-pad").mkdir(parents=True)
    (root / "src" / "index.js").write_text("module.exports = 1;\n")
    (root / "src" / "util.js").write_text("exports.x = 1;\nexports.y = 2;\n")
    (root / "package.json").write_text(json.dumps({"name": "demo", "version": "1.0.0"}) + "\n")
    (root / ".env").write_text("API_KEY=super-secret\n")
    (root / "id_rsa").write_text("-----BEGIN PRIVATE KEY-----\n")
    (root / "node_modules" / "left-pad" / "index.js").write_text("// noise\n")
    return root


@pytest.fixture
def backend() -> FakeSandboxBackend:
    return FakeSandboxBackend()


@pytest.fixture
async def app(settings: Settings, backend: FakeSandboxBackend) -> Iterator[SandboxMCPApp]:
    application = SandboxMCPApp.build(
        settings=settings,
        backend=backend,
        repository=SQLiteRepository(settings.database_path),
    )
    await application.startup()
    yield application
    await application.shutdown()
