"""Integration tests against a live Docker daemon.

These are the tests that prove the product claim rather than the code's
internal consistency: that a sandbox is genuinely isolated, that the host
project is genuinely untouched, and that nothing is left running afterwards.

Run with: pytest -m integration
"""

from __future__ import annotations

import contextlib
from pathlib import Path

import docker
import pytest
from docker.errors import NotFound

from sandbox_mcp.app import SandboxMCPApp
from sandbox_mcp.config import Settings
from sandbox_mcp.errors import ImageError, SandboxMCPError
from sandbox_mcp.models import ExperimentStatus, JobStatus
from sandbox_mcp.sandbox.docker import MANAGED_LABEL, DockerSandboxBackend

from .conftest import TEST_IMAGE, requires_docker

pytestmark = [pytest.mark.integration, requires_docker]


def raw_client(settings: Settings) -> docker.DockerClient:
    """A direct client, used only to assert on what the backend created."""
    return docker.DockerClient(base_url=settings.resolved_docker_host())


class TestTheFullLifecycle:
    async def test_the_nine_step_isolation_checklist(
        self, docker_app: SandboxMCPApp, node_project: Path
    ) -> None:
        experiments = docker_app.experiments

        # 1. Create a sandbox. 2. Copy the project into it.
        created = await experiments.create(
            project_path=str(node_project), base_image=TEST_IMAGE, objective="isolation check"
        )
        assert created.status is ExperimentStatus.READY
        assert created.files_copied == 2  # .env is withheld

        experiment = await experiments.get(created.experiment_id)
        container_id = experiment.container_id
        assert container_id

        # 3. Create a file inside the sandbox.
        job = await experiments.execute(
            created.experiment_id, "echo 'made inside' > sandbox-only.txt"
        )
        assert job.exit_code == 0

        # 4. It must not exist in the host project.
        assert not (node_project / "sandbox-only.txt").exists()

        # 5 & 6. Execute a command and capture both streams.
        job = await experiments.execute(
            created.experiment_id, "echo out; echo err >&2; ls sandbox-only.txt"
        )
        assert job.exit_code == 0
        assert "out" in job.stdout
        assert "err" in job.stderr

        # 7. Inspect the changes.
        changes = await experiments.inspect_changes(created.experiment_id, include_diff=True)
        assert changes.files_created == ["sandbox-only.txt"]
        assert changes.files_modified == []
        assert changes.insertions == 1

        # 8. Destroy the sandbox.
        destroyed = await experiments.destroy(created.experiment_id)
        assert destroyed.container_removed is True
        assert destroyed.snapshot_removed is True

        # 9. Verify cleanup: container gone, snapshot gone, host untouched.
        with pytest.raises(NotFound):
            raw_client(docker_app.settings).containers.get(container_id)
        assert not Path(experiment.snapshot_dir or "/nonexistent").exists()
        assert sorted(p.name for p in node_project.rglob("*") if p.is_file()) == [
            ".env",
            "README.md",
            "app.js",
        ]

    async def test_the_host_project_is_byte_for_byte_unchanged(
        self, docker_app: SandboxMCPApp, node_project: Path
    ) -> None:
        before = {
            p.relative_to(node_project).as_posix(): p.read_bytes()
            for p in node_project.rglob("*")
            if p.is_file()
        }

        created = await docker_app.experiments.create(
            project_path=str(node_project), base_image=TEST_IMAGE
        )
        await docker_app.experiments.execute(
            created.experiment_id,
            "rm -rf src && echo destroyed > README.md && echo new > extra.txt",
        )
        await docker_app.experiments.destroy(created.experiment_id)

        after = {
            p.relative_to(node_project).as_posix(): p.read_bytes()
            for p in node_project.rglob("*")
            if p.is_file()
        }
        assert before == after


class TestIsolationGuarantees:
    async def test_the_network_is_off_by_default(self, docker_app: SandboxMCPApp) -> None:
        created = await docker_app.experiments.create(base_image=TEST_IMAGE)
        job = await docker_app.experiments.execute(
            created.experiment_id,
            "wget -q -T 3 -O- https://example.com || echo NO_NETWORK",
            timeout=20,
        )
        assert "NO_NETWORK" in job.stdout

        info = raw_client(docker_app.settings).containers.get(
            (await docker_app.experiments.get(created.experiment_id)).container_id or ""
        )
        assert "none" in info.attrs["HostConfig"]["NetworkMode"]

    async def test_secrets_never_reach_the_sandbox(
        self, docker_app: SandboxMCPApp, node_project: Path
    ) -> None:
        created = await docker_app.experiments.create(
            project_path=str(node_project), base_image=TEST_IMAGE
        )
        job = await docker_app.experiments.execute(
            created.experiment_id, "cat .env 2>/dev/null || echo ABSENT"
        )
        assert "ABSENT" in job.stdout
        assert "must-not-be-copied" not in job.stdout

    async def test_only_allowlisted_environment_variables_are_present(
        self, docker_app: SandboxMCPApp, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SBX_HARMLESS", "visible")
        monkeypatch.setenv("SBX_SECRET_TOKEN", "must-not-leak")
        monkeypatch.setenv("SBX_NOT_REQUESTED", "also-absent")

        created = await docker_app.experiments.create(
            base_image=TEST_IMAGE,
            environment_allowlist=["SBX_HARMLESS", "SBX_SECRET_TOKEN"],
        )
        job = await docker_app.experiments.execute(created.experiment_id, "env")

        assert "SBX_HARMLESS=visible" in job.stdout
        assert "must-not-leak" not in job.stdout
        assert "also-absent" not in job.stdout

    async def test_the_docker_socket_is_not_reachable(self, docker_app: SandboxMCPApp) -> None:
        """Mounting the socket would hand the agent the whole host."""
        created = await docker_app.experiments.create(base_image=TEST_IMAGE)
        job = await docker_app.experiments.execute(
            created.experiment_id, "ls /var/run/docker.sock 2>/dev/null || echo NO_SOCKET"
        )
        assert "NO_SOCKET" in job.stdout

        info = raw_client(docker_app.settings).containers.get(
            (await docker_app.experiments.get(created.experiment_id)).container_id or ""
        )
        binds = info.attrs["HostConfig"]["Binds"] or []
        assert not any("docker.sock" in bind for bind in binds)

    async def test_the_container_is_hardened(self, docker_app: SandboxMCPApp) -> None:
        created = await docker_app.experiments.create(base_image=TEST_IMAGE)
        experiment = await docker_app.experiments.get(created.experiment_id)
        host_config = (
            raw_client(docker_app.settings)
            .containers.get(experiment.container_id or "")
            .attrs["HostConfig"]
        )

        assert host_config["Privileged"] is False
        assert host_config["CapDrop"] == ["ALL"]
        assert "no-new-privileges:true" in (host_config["SecurityOpt"] or [])
        # The capabilities added back must exclude the escape-relevant ones.
        assert not {"NET_RAW", "SYS_ADMIN", "MKNOD", "SYS_CHROOT", "SETFCAP"} & set(
            host_config["CapAdd"] or []
        )


class TestResourceLimits:
    async def test_limits_are_applied_to_the_container(self, docker_app: SandboxMCPApp) -> None:
        created = await docker_app.experiments.create(
            base_image=TEST_IMAGE, cpu_limit=1.5, memory_limit="256MB"
        )
        experiment = await docker_app.experiments.get(created.experiment_id)
        host_config = (
            raw_client(docker_app.settings)
            .containers.get(experiment.container_id or "")
            .attrs["HostConfig"]
        )

        assert host_config["NanoCpus"] == 1_500_000_000
        assert host_config["Memory"] == 256 * 1024**2
        # Equal swap and memory: the cap cannot be escaped by swapping.
        assert host_config["MemorySwap"] == host_config["Memory"]
        assert host_config["PidsLimit"] == docker_app.settings.default_pids_limit

    async def test_requests_beyond_the_ceiling_are_clamped(self, docker_app: SandboxMCPApp) -> None:
        created = await docker_app.experiments.create(
            base_image=TEST_IMAGE, cpu_limit=999, memory_limit="900GB", timeout=999_999
        )
        assert created.resource_limits["cpu"] == docker_app.settings.max_cpu_limit
        assert created.resource_limits["memory"] == docker_app.settings.max_memory_limit
        assert len(created.warnings) >= 3

    async def test_the_memory_cap_actually_bites(self, docker_app: SandboxMCPApp) -> None:
        """Allocate well past the limit; the kernel must stop it, not the host."""
        created = await docker_app.experiments.create(base_image=TEST_IMAGE, memory_limit="64MB")
        job = await docker_app.experiments.execute(
            created.experiment_id,
            "dd if=/dev/zero of=/dev/shm/fill bs=1M count=512 2>/dev/null; echo EXIT=$?",
            timeout=60,
        )
        assert "EXIT=0" not in job.stdout


class TestTimeoutsAndCancellation:
    async def test_a_slow_command_is_killed_at_the_timeout(self, docker_app: SandboxMCPApp) -> None:
        created = await docker_app.experiments.create(base_image=TEST_IMAGE)
        job = await docker_app.experiments.execute(created.experiment_id, "sleep 60", timeout=3)
        assert job.status is JobStatus.TIMEOUT
        assert job.exit_code is None

    async def test_the_sandbox_survives_a_timeout(self, docker_app: SandboxMCPApp) -> None:
        """One runaway command must not poison the whole experiment."""
        created = await docker_app.experiments.create(base_image=TEST_IMAGE)
        await docker_app.experiments.execute(created.experiment_id, "sleep 60", timeout=2)
        after = await docker_app.experiments.execute(created.experiment_id, "echo still-here")
        assert after.exit_code == 0
        assert "still-here" in after.stdout

    async def test_output_is_captured_up_to_the_moment_of_the_kill(
        self, docker_app: SandboxMCPApp
    ) -> None:
        created = await docker_app.experiments.create(base_image=TEST_IMAGE)
        job = await docker_app.experiments.execute(
            created.experiment_id, "echo before-the-wait; sleep 60", timeout=4
        )
        assert job.status is JobStatus.TIMEOUT
        assert "before-the-wait" in job.stdout

    async def test_cancelling_a_background_job_kills_the_process(
        self, docker_app: SandboxMCPApp
    ) -> None:
        import asyncio

        created = await docker_app.experiments.create(base_image=TEST_IMAGE)
        experiment = await docker_app.experiments.get(created.experiment_id)
        job = await docker_app.execution.submit(
            experiment,
            docker_app.experiments.handle_for(experiment),
            "sleep 300",
            timeout=300,
        )
        await asyncio.sleep(1.5)
        cancelled = await docker_app.execution.cancel(job.id)
        assert cancelled.status is JobStatus.CANCELLED

        check = await docker_app.experiments.execute(
            created.experiment_id, "ps -o args | grep -c '[s]leep 300' || echo 0"
        )
        assert check.stdout.strip().splitlines()[-1] == "0"


class TestFailureHandling:
    async def test_an_unknown_image_fails_cleanly(self, docker_app: SandboxMCPApp) -> None:
        with pytest.raises(ImageError):
            await docker_app.experiments.create(base_image="alpine:this-tag-does-not-exist-99999")
        experiments = await docker_app.repository.list_experiments()
        assert experiments[0].status is ExperimentStatus.FAILED

    async def test_a_failed_creation_leaves_no_container_or_snapshot(
        self, docker_app: SandboxMCPApp, node_project: Path
    ) -> None:
        with pytest.raises(SandboxMCPError):
            await docker_app.experiments.create(
                project_path=str(node_project),
                base_image="alpine:this-tag-does-not-exist-99999",
            )
        assert not list(docker_app.settings.sandboxes_dir.glob("*/workspace"))
        managed = raw_client(docker_app.settings).containers.list(
            all=True, filters={"label": f"{MANAGED_LABEL}=true"}
        )
        assert managed == []

    async def test_a_crashing_command_is_a_result_not_an_exception(
        self, docker_app: SandboxMCPApp
    ) -> None:
        created = await docker_app.experiments.create(base_image=TEST_IMAGE)
        job = await docker_app.experiments.execute(created.experiment_id, "exit 42")
        assert job.status is JobStatus.COMPLETED
        assert job.exit_code == 42

    async def test_destroy_is_idempotent_against_a_real_daemon(
        self, docker_app: SandboxMCPApp
    ) -> None:
        created = await docker_app.experiments.create(base_image=TEST_IMAGE)
        first = await docker_app.experiments.destroy(created.experiment_id)
        second = await docker_app.experiments.destroy(created.experiment_id)
        assert first.container_removed is True
        assert second.already_destroyed is True

    async def test_orphaned_containers_are_swept_at_startup(
        self, docker_settings: Settings
    ) -> None:
        """A crashed server must not leave sandboxes burning CPU forever."""
        client = raw_client(docker_settings)
        orphan = client.containers.run(
            TEST_IMAGE,
            command=["sh", "-c", "sleep 300"],
            detach=True,
            labels={MANAGED_LABEL: "true"},
            network_mode="none",
        )
        try:
            backend = DockerSandboxBackend(docker_settings)
            assert await backend.cleanup_orphans(set()) >= 1
            with pytest.raises(NotFound):
                client.containers.get(orphan.id)
        finally:
            with contextlib.suppress(NotFound):
                client.containers.get(orphan.id).remove(force=True)


class TestArtifactsAndTests:
    async def test_collects_artifacts_out_of_the_sandbox(
        self, docker_app: SandboxMCPApp, node_project: Path
    ) -> None:
        created = await docker_app.experiments.create(
            project_path=str(node_project), base_image=TEST_IMAGE
        )
        await docker_app.experiments.execute(
            created.experiment_id, "mkdir -p dist && echo built > dist/bundle.js"
        )
        result = await docker_app.experiments.collect_artifacts(
            created.experiment_id, ["dist/*.js"]
        )
        assert len(result.artifacts) == 1
        assert Path(result.artifacts[0].host_path).read_text() == "built\n"

    async def test_artifact_patterns_cannot_escape_the_workspace(
        self, docker_app: SandboxMCPApp
    ) -> None:
        created = await docker_app.experiments.create(base_image=TEST_IMAGE)
        result = await docker_app.experiments.collect_artifacts(
            created.experiment_id, ["/etc/passwd", "../../etc/shadow"]
        )
        assert result.artifacts == []
        assert len(result.skipped) == 2

    async def test_detects_and_runs_a_test_suite(self, docker_app: SandboxMCPApp) -> None:
        created = await docker_app.experiments.create(base_image=TEST_IMAGE)
        # alpine has no `make`, so exercise the parse path with an explicit command.
        job, _framework, summary = await docker_app.experiments.run_tests(
            created.experiment_id, command="echo '# tests 3'; echo '# pass 3'; echo '# fail 0'"
        )
        assert job.exit_code == 0
        assert summary.detected and summary.passed == 3
