"""The policy engine.

Every experiment request passes through here before Docker sees anything. It
turns loosely-typed MCP arguments into a validated :class:`ExperimentSpec`,
clamping what it can and refusing what it must. The sandbox backend trusts the
spec completely, which means this is the only file that has to be right for
the isolation guarantees to hold.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from ..config import Settings
from ..errors import ImageError, PolicyViolationError
from ..models import ExperimentSpec, MountStrategy, NetworkMode, ResourceLimits
from .resources import format_memory, parse_memory

# Names that look like credentials. Blocked even when explicitly allowlisted
# while settings.strict_env_denylist is on: an agent asking for
# AWS_SECRET_ACCESS_KEY inside a throwaway container is never right.
CREDENTIAL_NAME_PATTERN = re.compile(
    r"(SECRET|PASSWORD|PASSWD|TOKEN|API[_-]?KEY|ACCESS[_-]?KEY|PRIVATE[_-]?KEY|"
    r"CREDENTIAL|SESSION|COOKIE|AUTH|SIGNING|CERT|SSH|GPG)",
    re.IGNORECASE,
)

CREDENTIAL_NAME_EXACT = frozenset(
    {
        "AWS_ACCESS_KEY_ID",
        "AWS_SESSION_TOKEN",
        "AWS_PROFILE",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "AZURE_CLIENT_ID",
        "AZURE_TENANT_ID",
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "NPM_TOKEN",
        "DOCKER_AUTH_CONFIG",
        "DATABASE_URL",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
    }
)

_IMAGE_REFERENCE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._\-/]*(?::[A-Za-z0-9._\-]+)?(?:@sha256:[a-f0-9]{64})?$"
)


@dataclass(slots=True)
class PolicyDecision:
    """A spec the backend may act on, plus everything the agent should know."""

    spec: ExperimentSpec
    warnings: list[str] = field(default_factory=list)
    rejected_environment: list[str] = field(default_factory=list)


class PolicyEngine:
    """Validates and clamps experiment requests."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    # --- images ---------------------------------------------------------

    def validate_image(self, image: str | None) -> str:
        reference = (image or self._settings.default_base_image).strip()
        if not reference:
            raise ImageError("Base image must not be empty.", image=image)
        if not _IMAGE_REFERENCE.match(reference):
            raise ImageError(
                f"Base image reference is malformed: {reference!r}",
                image=reference,
            )
        if self._settings.allow_any_image:
            return reference

        repository = reference.split("@", 1)[0].rsplit(":", 1)[0]
        # Component-wise match only: the prefix 'node' accepts 'node' and
        # 'node/foo', never 'nodejs-typosquat'.
        if not any(
            repository == prefix or repository.startswith(f"{prefix}/")
            for prefix in self._settings.allowed_image_prefixes
        ):
            raise ImageError(
                f"Base image {reference!r} is not in the configured allowlist. "
                "Add a prefix to SANDBOX_MCP_ALLOWED_IMAGE_PREFIXES or set "
                "SANDBOX_MCP_ALLOW_ANY_IMAGE=true.",
                image=reference,
                allowed_prefixes=self._settings.allowed_image_prefixes,
            )
        return reference

    # --- environment ----------------------------------------------------

    def filter_environment(
        self, allowlist: list[str] | None
    ) -> tuple[dict[str, str], list[str], list[str]]:
        """Resolve an environment allowlist into concrete variables.

        Two accepted forms:

        * ``"NAME"``        -- forward the host's value, if the variable is set.
        * ``"NAME=value"``  -- inject a literal the agent chose.

        Nothing else crosses the boundary: the host environment is never
        inherited wholesale.

        Returns ``(environment, rejected, warnings)``.
        """
        environment: dict[str, str] = {}
        rejected: list[str] = []
        warnings: list[str] = []

        for entry in allowlist or []:
            raw = entry.strip()
            if not raw:
                continue
            name, _, literal = raw.partition("=")
            name = name.strip()
            if not name.isidentifier() and not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name):
                rejected.append(name)
                warnings.append(f"Ignored malformed environment variable name {name!r}.")
                continue

            if self._settings.strict_env_denylist and self._is_credential_name(name):
                rejected.append(name)
                warnings.append(
                    f"Refused to pass {name} into the sandbox: the name is credential-shaped. "
                    "Set SANDBOX_MCP_STRICT_ENV_DENYLIST=false to override."
                )
                continue

            if _:
                environment[name] = literal
                continue

            host_value = os.environ.get(name)
            if host_value is None:
                warnings.append(f"Environment variable {name} is not set on the host; skipped.")
                continue
            environment[name] = host_value

        return environment, rejected, warnings

    @staticmethod
    def _is_credential_name(name: str) -> bool:
        upper = name.upper()
        return upper in CREDENTIAL_NAME_EXACT or bool(CREDENTIAL_NAME_PATTERN.search(upper))

    # --- resources ------------------------------------------------------

    def build_resource_limits(
        self,
        cpu_limit: float | None = None,
        memory_limit: str | None = None,
        timeout: int | None = None,
        pids_limit: int | None = None,
    ) -> tuple[ResourceLimits, list[str]]:
        warnings: list[str] = []
        settings = self._settings

        cpu = float(cpu_limit if cpu_limit is not None else settings.default_cpu_limit)
        if cpu <= 0:
            raise PolicyViolationError("cpu_limit must be greater than zero.", field="cpu_limit")
        if cpu > settings.max_cpu_limit:
            warnings.append(
                f"cpu_limit {cpu} clamped to the {settings.max_cpu_limit} core ceiling."
            )
            cpu = settings.max_cpu_limit

        memory_bytes = parse_memory(memory_limit or settings.default_memory_limit)
        max_memory_bytes = parse_memory(settings.max_memory_limit)
        if memory_bytes > max_memory_bytes:
            warnings.append(
                f"memory_limit {format_memory(memory_bytes)} clamped to "
                f"{format_memory(max_memory_bytes)}."
            )
            memory_bytes = max_memory_bytes

        seconds = int(timeout if timeout is not None else settings.default_timeout_seconds)
        if seconds <= 0:
            raise PolicyViolationError("timeout must be greater than zero.", field="timeout")
        if seconds > settings.max_timeout_seconds:
            warnings.append(
                f"timeout {seconds}s clamped to the {settings.max_timeout_seconds}s ceiling."
            )
            seconds = settings.max_timeout_seconds

        pids = int(pids_limit if pids_limit is not None else settings.default_pids_limit)
        if pids <= 0:
            raise PolicyViolationError("pids_limit must be greater than zero.", field="pids_limit")
        if pids > settings.max_pids_limit:
            warnings.append(f"pids_limit {pids} clamped to {settings.max_pids_limit}.")
            pids = settings.max_pids_limit

        return (
            ResourceLimits(
                cpu_limit=cpu,
                memory_limit=format_memory(memory_bytes),
                memory_bytes=memory_bytes,
                timeout_seconds=seconds,
                pids_limit=pids,
            ),
            warnings,
        )

    def clamp_timeout(self, timeout: int | None, fallback: int) -> int:
        seconds = int(timeout if timeout is not None else fallback)
        if seconds <= 0:
            raise PolicyViolationError("timeout must be greater than zero.", field="timeout")
        return min(seconds, self._settings.max_timeout_seconds)

    # --- isolation modes -------------------------------------------------

    def resolve_network_mode(self, mode: str | NetworkMode | None) -> NetworkMode:
        if mode is None:
            return self._settings.default_network_mode
        try:
            return NetworkMode(str(mode).lower())
        except ValueError as exc:
            raise PolicyViolationError(
                f"Unknown network_mode {mode!r}. Use one of: "
                f"{', '.join(m.value for m in NetworkMode)}.",
                field="network_mode",
            ) from exc

    def resolve_mount_strategy(
        self, strategy: str | MountStrategy | None, writable: bool
    ) -> tuple[MountStrategy, list[str]]:
        warnings: list[str] = []
        if strategy is None:
            resolved = self._settings.default_mount_strategy
        else:
            try:
                resolved = MountStrategy(str(strategy).upper())
            except ValueError as exc:
                raise PolicyViolationError(
                    f"Unknown mount strategy {strategy!r}. Use one of: "
                    f"{', '.join(m.value for m in MountStrategy)}.",
                    field="mount_strategy",
                ) from exc

        if resolved is MountStrategy.READ_ONLY_BIND_MOUNT and writable:
            if not self._settings.allow_writable_bind_mounts:
                warnings.append(
                    "READ_ONLY_BIND_MOUNT cannot be writable; the host tree stays read-only. "
                    "Use COPY_TO_SANDBOX if the experiment needs to modify files."
                )
            else:
                raise PolicyViolationError(
                    "Writable host bind mounts are disabled. This server never gives a sandbox "
                    "write access to the developer's working tree.",
                    field="mount_strategy",
                )
        return resolved, warnings

    # --- composition -----------------------------------------------------

    def build_spec(
        self,
        *,
        project_name: str,
        project_path: str | None,
        base_image: str | None,
        network_mode: str | NetworkMode | None,
        mount_strategy: str | MountStrategy | None,
        cpu_limit: float | None,
        memory_limit: str | None,
        timeout: int | None,
        pids_limit: int | None,
        environment_allowlist: list[str] | None,
        setup_commands: list[str] | None,
        writable: bool,
        objective: str | None = None,
    ) -> PolicyDecision:
        """Single entry point. Either returns an approved spec or raises."""
        warnings: list[str] = []

        image = self.validate_image(base_image)
        resolved_network = self.resolve_network_mode(network_mode)
        strategy, mount_warnings = self.resolve_mount_strategy(mount_strategy, writable)
        warnings.extend(mount_warnings)

        resources, resource_warnings = self.build_resource_limits(
            cpu_limit, memory_limit, timeout, pids_limit
        )
        warnings.extend(resource_warnings)

        environment, rejected, env_warnings = self.filter_environment(environment_allowlist)
        warnings.extend(env_warnings)

        commands = [c for c in (setup_commands or []) if c and c.strip()]

        if resolved_network is NetworkMode.NONE and any(
            _looks_network_bound(command) for command in commands
        ):
            warnings.append(
                "A setup command looks like it needs the network, but network_mode is 'none'. "
                "Pass network_mode='restricted' if the experiment must reach a registry."
            )

        spec = ExperimentSpec(
            project_path=project_path,
            project_name=project_name,
            base_image=image,
            network_mode=resolved_network,
            mount_strategy=strategy,
            resources=resources,
            environment=environment,
            setup_commands=commands,
            writable=writable,
            workspace_path=self._settings.workspace_path,
            user=self._settings.sandbox_user,
            objective=objective,
        )
        return PolicyDecision(spec=spec, warnings=warnings, rejected_environment=rejected)


_NETWORK_HINTS = (
    "npm install",
    "npm ci",
    "yarn install",
    "pnpm install",
    "pip install",
    "uv sync",
    "poetry install",
    "cargo fetch",
    "go mod download",
    "apt-get install",
    "apk add",
    "curl ",
    "wget ",
    "git clone",
)


def _looks_network_bound(command: str) -> bool:
    lowered = command.lower()
    return any(hint in lowered for hint in _NETWORK_HINTS)
