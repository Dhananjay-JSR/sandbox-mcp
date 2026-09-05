"""Runtime configuration.

Everything tunable lives here so that policy decisions are made in one place
and can be audited. Values come from the environment with a ``SANDBOX_MCP_``
prefix, or from a ``.env`` file. See ``.env.example``.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .models import MountStrategy, NetworkMode

# Socket locations probed, in order, when DOCKER_HOST is unset. Covers Docker
# Desktop, OrbStack, Colima and Rancher Desktop on macOS plus stock Linux.
DOCKER_SOCKET_CANDIDATES: tuple[str, ...] = (
    "/var/run/docker.sock",
    "~/.docker/run/docker.sock",
    "~/.orbstack/run/docker.sock",
    "~/.colima/default/docker.sock",
    "~/.rd/docker.sock",
)

# Images an experiment may be based on. Prefix match against the repository
# part of the reference. Widen deliberately, or set allow_any_image.
DEFAULT_IMAGE_ALLOWLIST: tuple[str, ...] = (
    "alpine",
    "busybox",
    "debian",
    "ubuntu",
    "node",
    "python",
    "golang",
    "rust",
    "openjdk",
    "eclipse-temurin",
    "ruby",
    "php",
    "postgres",
    "mysql",
    "mariadb",
    "redis",
    "mongo",
    "gcc",
    "mcr.microsoft.com/dotnet",
)

# Copied project trees skip these. Heavy build output plus anything that
# routinely holds credentials -- see SECRET_FILE_PATTERNS for the second half.
DEFAULT_SNAPSHOT_EXCLUDES: tuple[str, ...] = (
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "target",
    "dist",
    "build",
    ".next",
    ".nuxt",
    ".turbo",
    ".gradle",
    ".terraform",
    "vendor",
    ".DS_Store",
    "*.pyc",
    "*.log",
)

# Never copied into a sandbox, regardless of user configuration.
SECRET_FILE_PATTERNS: tuple[str, ...] = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "id_rsa*",
    "id_ed25519*",
    ".netrc",
    ".git-credentials",
    ".ssh",
    ".aws",
    ".gnupg",
    ".kube",
    "credentials.json",
    "service-account*.json",
)

# Added back after cap_drop=ALL. Deliberately excludes NET_RAW, MKNOD, SYS_CHROOT,
# SYS_ADMIN, SETPCAP and SETFCAP -- the ones that matter for container escape.
DEFAULT_SANDBOX_CAPABILITIES: tuple[str, ...] = (
    "CHOWN",
    "DAC_OVERRIDE",
    "FOWNER",
    "FSETID",
    "SETUID",
    "SETGID",
    "KILL",
)

# Host directories a project path may never resolve into.
DENIED_PROJECT_ROOTS: tuple[str, ...] = (
    "/etc",
    "/var/run",
    "/private/etc",
    "/private/var/run",
    "/System",
    "/Library/Keychains",
    "/proc",
    "/sys",
    "/dev",
    "~/.ssh",
    "~/.aws",
    "~/.gnupg",
    "~/.kube",
    "~/.docker",
    "~/.config/gcloud",
    "~/Library/Keychains",
)


def discover_docker_host() -> str | None:
    """Best-effort Docker endpoint discovery.

    ``DOCKER_HOST`` wins if set. Otherwise probe the well-known sockets --
    notably the docker CLI's *context* is not consulted by the Python SDK, so
    an OrbStack or Colima user with no DOCKER_HOST would otherwise fail.
    """
    if env_host := os.environ.get("DOCKER_HOST"):
        return env_host
    for candidate in DOCKER_SOCKET_CANDIDATES:
        path = Path(candidate).expanduser()
        if path.exists():
            return f"unix://{path}"
    return None


class Settings(BaseSettings):
    """Server-wide configuration and the ceilings every experiment is clamped to."""

    model_config = SettingsConfigDict(
        env_prefix="SANDBOX_MCP_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Docker ---------------------------------------------------------
    docker_host: str | None = Field(
        default=None,
        description="Docker endpoint. Defaults to DOCKER_HOST, else socket discovery.",
    )
    docker_timeout_seconds: int = Field(default=60, ge=5, le=600)
    image_pull_timeout_seconds: int = Field(default=600, ge=30)

    # --- state ----------------------------------------------------------
    state_dir: Path = Field(
        default=Path("~/.sandbox-mcp"),
        description="Root for the SQLite database, project snapshots and artifacts.",
    )

    # --- defaults applied when an experiment omits a value ---------------
    default_base_image: str = "debian:bookworm-slim"
    default_network_mode: NetworkMode = NetworkMode.NONE
    default_mount_strategy: MountStrategy = MountStrategy.COPY_TO_SANDBOX
    default_cpu_limit: float = Field(default=2.0, gt=0)
    default_memory_limit: str = "2GB"
    default_timeout_seconds: int = Field(default=120, gt=0)
    default_pids_limit: int = Field(default=512, gt=0)

    # --- hard ceilings: a request may ask for less, never for more -------
    max_cpu_limit: float = Field(default=4.0, gt=0)
    max_memory_limit: str = "8GB"
    max_timeout_seconds: int = Field(default=1800, gt=0)
    max_pids_limit: int = Field(default=2048, gt=0)
    max_concurrent_experiments: int = Field(default=10, gt=0)
    max_concurrent_jobs_per_experiment: int = Field(default=4, gt=0)

    # --- output and payload caps ----------------------------------------
    max_output_bytes: int = Field(
        default=1_000_000, gt=0, description="Per-stream cap on captured stdout/stderr."
    )
    max_project_bytes: int = Field(default=512 * 1024 * 1024, gt=0)
    max_project_files: int = Field(default=50_000, gt=0)
    max_artifact_bytes: int = Field(default=64 * 1024 * 1024, gt=0)
    max_artifacts_per_experiment: int = Field(default=200, gt=0)
    max_diff_file_bytes: int = Field(
        default=512 * 1024, gt=0, description="Files above this are reported without a diff body."
    )
    tmpfs_size_mb: int = Field(
        default=512, gt=0, description="Size of the sandbox's in-memory /tmp, counted against RAM."
    )

    # --- security -------------------------------------------------------
    allow_any_image: bool = False
    allowed_image_prefixes: list[str] = Field(default_factory=lambda: list(DEFAULT_IMAGE_ALLOWLIST))
    allowed_project_roots: list[Path] = Field(
        default_factory=list,
        description="If non-empty, project paths must resolve inside one of these.",
    )
    denied_project_roots: list[Path] = Field(
        default_factory=lambda: [Path(p) for p in DENIED_PROJECT_ROOTS]
    )
    snapshot_excludes: list[str] = Field(default_factory=lambda: list(DEFAULT_SNAPSHOT_EXCLUDES))
    strict_env_denylist: bool = Field(
        default=True,
        description="Refuse credential-shaped variable names even when explicitly allowlisted.",
    )
    allow_writable_bind_mounts: bool = Field(
        default=False,
        description="Opt in to writable host bind mounts. Off by default; it defeats the point.",
    )
    sandbox_capabilities: list[str] = Field(
        default_factory=lambda: list(DEFAULT_SANDBOX_CAPABILITIES),
        description=(
            "Capabilities added back after dropping ALL. The default set is what real "
            "toolchains need (package managers chown caches and drop privileges for "
            "lifecycle scripts) minus everything that helps an escape: no NET_RAW, no "
            "MKNOD, no SYS_*, no SETPCAP/SETFCAP."
        ),
    )
    sandbox_user: str | None = Field(
        default=None,
        description="Run containers as this user (e.g. '1000:1000'). None uses the image default.",
    )
    workspace_path: str = "/workspace"

    # --- logging --------------------------------------------------------
    log_level: str = "INFO"
    log_json: bool = True

    @field_validator("state_dir")
    @classmethod
    def _expand_state_dir(cls, value: Path) -> Path:
        return value.expanduser().resolve()

    @field_validator("denied_project_roots", "allowed_project_roots")
    @classmethod
    def _expand_roots(cls, value: list[Path]) -> list[Path]:
        return [Path(p).expanduser() for p in value]

    @property
    def database_path(self) -> Path:
        return self.state_dir / "sandbox.db"

    @property
    def sandboxes_dir(self) -> Path:
        """Host-side project snapshots. One immutable baseline per experiment."""
        return self.state_dir / "sandboxes"

    @property
    def artifacts_dir(self) -> Path:
        return self.state_dir / "artifacts"

    def resolved_docker_host(self) -> str | None:
        return self.docker_host or discover_docker_host()

    def ensure_directories(self) -> None:
        for directory in (self.state_dir, self.sandboxes_dir, self.artifacts_dir):
            directory.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton."""
    return Settings()
