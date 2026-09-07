"""The primary demo, as a test.

Answers the question the product exists for: can an agent take a project pinned
to Node 20, find out whether it runs on Node 22, fix what breaks, and report --
without the developer's working tree changing by a single byte?

Runs entirely offline (``network_mode='none'``). Roughly a minute.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from sandbox_mcp.app import SandboxMCPApp
from sandbox_mcp.models import ExperimentStatus

from .conftest import requires_docker

pytestmark = [pytest.mark.integration, requires_docker]

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "node-upgrade"

# The migration a maintainer would write: createCipher derived its key from the
# password with MD5 and used no IV; createCipheriv needs both, explicitly.
CIPHER_MIGRATION = (
    (
        "const ALGORITHM = 'aes-192-cbc';",
        "const ALGORITHM = 'aes-192-cbc';\n"
        "const IV = Buffer.alloc(16, 0);\n\n"
        "function keyFor(password) {\n"
        "  return crypto.scryptSync(password, 'payments-service', 24);\n"
        "}",
    ),
    (
        "const cipher = crypto.createCipher(ALGORITHM, password);",
        "const cipher = crypto.createCipheriv(ALGORITHM, keyFor(password), IV);",
    ),
    (
        "const decipher = crypto.createDecipher(ALGORITHM, password);",
        "const decipher = crypto.createDecipheriv(ALGORITHM, keyFor(password), IV);",
    ),
)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A copy, so a failing test can never damage the example in the repo."""
    if not EXAMPLE.is_dir():
        pytest.skip("examples/node-upgrade is missing")
    destination = tmp_path / "payments-service"
    shutil.copytree(EXAMPLE, destination)
    return destination


def snapshot(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


async def test_node_20_to_22_upgrade_experiment(docker_app: SandboxMCPApp, project: Path) -> None:
    experiments = docker_app.experiments
    before = snapshot(project)

    # The agent creates a disposable Node 22 environment. No network.
    created = await experiments.create(
        project_path=str(project),
        base_image="node:22-slim",
        objective="Can payments-service move from Node 20 to Node 22?",
        memory_limit="1GB",
        timeout=300,
    )
    experiment_id = created.experiment_id
    assert created.status is ExperimentStatus.READY
    assert created.network_mode.value == "none"

    # First wall: engine-strict refuses to install on Node 22.
    install = await experiments.execute(experiment_id, "npm install --no-audit --no-fund")
    assert install.exit_code == 1
    assert "EBADENGINE" in install.stderr or "notsup" in install.stderr

    # The agent widens the engine range and reinstalls.
    manifest = json.loads(await experiments.read_file(experiment_id, "package.json"))
    manifest["engines"]["node"] = ">=18"
    await experiments.write_file(
        experiment_id, "package.json", json.dumps(manifest, indent=2) + "\n"
    )
    install = await experiments.execute(experiment_id, "npm install --no-audit --no-fund")
    assert install.exit_code == 0

    # Second wall: crypto.createCipher was removed in Node 22.
    _, framework, summary = await experiments.run_tests(experiment_id)
    assert framework == "npm"
    assert summary.detected
    assert (summary.passed, summary.failed, summary.total) == (37, 3, 40)
    assert "seal and open round-trip" in summary.failing_tests

    # The agent reads the source, migrates the call sites, and retests.
    source = await experiments.read_file(experiment_id, "src/crypto.js")
    for old, new in CIPHER_MIGRATION:
        assert old in source
        source = source.replace(old, new)
    await experiments.write_file(experiment_id, "src/crypto.js", source)

    job, _, summary = await experiments.run_tests(experiment_id)
    assert job.exit_code == 0
    assert (summary.passed, summary.failed) == (40, 0)

    # The evidence for the conclusion.
    changes = await experiments.inspect_changes(experiment_id, include_diff=True)
    assert set(changes.files_modified) == {"package.json", "package-lock.json", "src/crypto.js"}
    assert changes.files_deleted == []
    assert changes.insertions > 0
    assert any("createCipheriv" in (change.diff or "") for change in changes.changes)

    # Keep the migrated file, then dispose of the sandbox.
    artifacts = await experiments.collect_artifacts(experiment_id, ["src/crypto.js"])
    assert len(artifacts.artifacts) == 1
    assert "createCipheriv" in Path(artifacts.artifacts[0].host_path).read_text()

    destroyed = await experiments.destroy(experiment_id)
    assert destroyed.container_removed is True
    assert destroyed.report is not None
    assert destroyed.report.test_summary is not None
    assert destroyed.report.test_summary.passed == 40
    assert destroyed.report.changes == {
        "files_created": 0,
        "files_modified": 3,
        "files_deleted": 0,
        "files_changed": 3,
        "insertions": changes.insertions,
        "deletions": changes.deletions,
    }

    # The whole point.
    assert snapshot(project) == before
