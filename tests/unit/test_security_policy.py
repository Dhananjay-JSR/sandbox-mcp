"""Policy is the file that has to be right for the isolation claims to hold."""

from __future__ import annotations

import pytest

from sandbox_mcp.config import Settings
from sandbox_mcp.errors import ImageError, PolicyViolationError
from sandbox_mcp.models import MountStrategy, NetworkMode
from sandbox_mcp.security.policy import PolicyEngine


@pytest.fixture
def policy(settings: Settings) -> PolicyEngine:
    return PolicyEngine(settings)


class TestImageAllowlist:
    @pytest.mark.parametrize(
        "image",
        ["node:22-slim", "python:3.12-slim", "alpine", "mcr.microsoft.com/dotnet/sdk:8.0"],
    )
    def test_allows_known_prefixes(self, policy: PolicyEngine, image: str) -> None:
        assert policy.validate_image(image) == image

    @pytest.mark.parametrize(
        "image",
        ["evil.io/miner:latest", "nodejs-typosquat/payload", "node-evil:1", "totally/unknown"],
    )
    def test_rejects_everything_else(self, policy: PolicyEngine, image: str) -> None:
        """A prefix must match a whole path component, or typosquats slip through."""
        with pytest.raises(ImageError):
            policy.validate_image(image)

    def test_rejects_malformed_reference(self, policy: PolicyEngine) -> None:
        with pytest.raises(ImageError):
            policy.validate_image("node:22 ; rm -rf /")

    def test_falls_back_to_the_default(self, policy: PolicyEngine, settings: Settings) -> None:
        assert policy.validate_image(None) == settings.default_base_image

    def test_allow_any_image_opens_the_gate(self, settings: Settings) -> None:
        settings.allow_any_image = True
        assert PolicyEngine(settings).validate_image("evil.io/whatever:1") == "evil.io/whatever:1"


class TestEnvironmentFiltering:
    def test_host_environment_is_not_inherited(self, policy: PolicyEngine) -> None:
        environment, _, _ = policy.filter_environment(None)
        assert environment == {}

    def test_named_variable_is_forwarded(
        self, policy: PolicyEngine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("CI", "true")
        environment, _, _ = policy.filter_environment(["CI"])
        assert environment == {"CI": "true"}

    def test_literal_value_is_injected(self, policy: PolicyEngine) -> None:
        environment, _, _ = policy.filter_environment(["NODE_ENV=test"])
        assert environment == {"NODE_ENV": "test"}

    @pytest.mark.parametrize(
        "name",
        [
            "AWS_SECRET_ACCESS_KEY",
            "GITHUB_TOKEN",
            "DATABASE_URL",
            "MY_API_KEY",
            "SSH_AUTH_SOCK",
            "db_password",
        ],
    )
    def test_credential_names_are_refused_even_when_asked_for(
        self, policy: PolicyEngine, monkeypatch: pytest.MonkeyPatch, name: str
    ) -> None:
        monkeypatch.setenv(name, "leak-me")
        environment, rejected, warnings = policy.filter_environment([name])
        assert environment == {}
        assert name in rejected
        assert warnings

    def test_denylist_can_be_disabled_deliberately(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_x")
        settings.strict_env_denylist = False
        environment, rejected, _ = PolicyEngine(settings).filter_environment(["GITHUB_TOKEN"])
        assert environment == {"GITHUB_TOKEN": "ghp_x"}
        assert rejected == []

    def test_unset_variable_is_skipped_with_a_warning(self, policy: PolicyEngine) -> None:
        environment, _, warnings = policy.filter_environment(["DEFINITELY_NOT_SET_12345"])
        assert environment == {}
        assert any("not set" in warning for warning in warnings)

    def test_malformed_name_is_rejected(self, policy: PolicyEngine) -> None:
        _, rejected, _ = policy.filter_environment(["not-a-valid-name"])
        assert rejected == ["not-a-valid-name"]


class TestResourceLimits:
    def test_defaults_come_from_settings(self, policy: PolicyEngine, settings: Settings) -> None:
        limits, warnings = policy.build_resource_limits()
        assert limits.cpu_limit == settings.default_cpu_limit
        assert limits.timeout_seconds == settings.default_timeout_seconds
        assert warnings == []

    def test_requests_above_the_ceiling_are_clamped_and_reported(
        self, policy: PolicyEngine, settings: Settings
    ) -> None:
        limits, warnings = policy.build_resource_limits(
            cpu_limit=64, memory_limit="512GB", timeout=99999, pids_limit=100_000
        )
        assert limits.cpu_limit == settings.max_cpu_limit
        assert limits.memory_bytes == 8 * 1024**3
        assert limits.timeout_seconds == settings.max_timeout_seconds
        assert limits.pids_limit == settings.max_pids_limit
        assert len(warnings) == 4

    def test_smaller_requests_are_honoured(self, policy: PolicyEngine) -> None:
        limits, warnings = policy.build_resource_limits(cpu_limit=0.5, memory_limit="256MB")
        assert limits.cpu_limit == 0.5
        assert limits.memory_bytes == 256 * 1024**2
        assert warnings == []

    @pytest.mark.parametrize("cpu,timeout", [(0, None), (-1, None), (None, 0), (None, -5)])
    def test_nonsense_limits_are_rejected(
        self, policy: PolicyEngine, cpu: float | None, timeout: int | None
    ) -> None:
        with pytest.raises(PolicyViolationError):
            policy.build_resource_limits(cpu_limit=cpu, timeout=timeout)


class TestIsolationModes:
    def test_network_defaults_to_none(self, policy: PolicyEngine) -> None:
        assert policy.resolve_network_mode(None) is NetworkMode.NONE

    @pytest.mark.parametrize("value", ["none", "restricted", "enabled", "RESTRICTED"])
    def test_known_network_modes_parse(self, policy: PolicyEngine, value: str) -> None:
        assert isinstance(policy.resolve_network_mode(value), NetworkMode)

    def test_unknown_network_mode_is_rejected(self, policy: PolicyEngine) -> None:
        with pytest.raises(PolicyViolationError):
            policy.resolve_network_mode("host")

    def test_mount_strategy_defaults_to_copying(self, policy: PolicyEngine) -> None:
        strategy, _ = policy.resolve_mount_strategy(None, writable=True)
        assert strategy is MountStrategy.COPY_TO_SANDBOX

    def test_writable_bind_mount_warns_rather_than_writing_to_the_host(
        self, policy: PolicyEngine
    ) -> None:
        strategy, warnings = policy.resolve_mount_strategy("READ_ONLY_BIND_MOUNT", writable=True)
        assert strategy is MountStrategy.READ_ONLY_BIND_MOUNT
        assert any("read-only" in warning for warning in warnings)

    def test_writable_bind_mount_is_refused_when_the_operator_enabled_them(
        self, settings: Settings
    ) -> None:
        settings.allow_writable_bind_mounts = True
        with pytest.raises(PolicyViolationError):
            PolicyEngine(settings).resolve_mount_strategy("READ_ONLY_BIND_MOUNT", writable=True)


class TestSpecComposition:
    def test_builds_a_complete_spec(self, policy: PolicyEngine) -> None:
        decision = policy.build_spec(
            project_name="demo",
            project_path="/tmp/demo",
            base_image="node:22-slim",
            network_mode="restricted",
            mount_strategy=None,
            cpu_limit=1,
            memory_limit="1GB",
            timeout=60,
            pids_limit=None,
            environment_allowlist=["NODE_ENV=test"],
            setup_commands=["npm ci", "  "],
            writable=True,
            objective="upgrade",
        )
        spec = decision.spec
        assert spec.base_image == "node:22-slim"
        assert spec.network_mode is NetworkMode.RESTRICTED
        assert spec.environment == {"NODE_ENV": "test"}
        assert spec.setup_commands == ["npm ci"]

    def test_warns_when_a_setup_command_needs_a_network_it_does_not_have(
        self, policy: PolicyEngine
    ) -> None:
        decision = policy.build_spec(
            project_name="demo",
            project_path=None,
            base_image="node:22-slim",
            network_mode="none",
            mount_strategy=None,
            cpu_limit=None,
            memory_limit=None,
            timeout=None,
            pids_limit=None,
            environment_allowlist=None,
            setup_commands=["npm install express"],
            writable=True,
        )
        assert any("network" in warning for warning in decision.warnings)
