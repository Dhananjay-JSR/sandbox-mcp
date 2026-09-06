"""Docker implementation of :class:`SandboxBackend`.

Talks to the Docker Engine API through the official Python SDK over the unix
socket. There is no ``subprocess`` here and no ``docker`` binary is required.

The hardening applied to every container is in :meth:`_host_config`; it is the
single place isolation is decided, so it is short on purpose and worth reading
in full.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import shlex
import tarfile
import time
from pathlib import Path
from typing import Any

import docker
from docker.errors import APIError, DockerException, ImageNotFound, NotFound

from ..config import Settings
from ..errors import (
    DockerUnavailableError,
    ImageError,
    SandboxBackendError,
    SandboxStartupError,
)
from ..logging import get_logger
from ..models import ExperimentSpec, NetworkMode
from ..security.filesystem import FileFingerprint, FileManifest, hash_bytes
from .interface import ExecOutcome, SandboxBackend, SandboxHandle

log = get_logger(__name__)

MANAGED_LABEL = "sandbox-mcp.managed"
EXPERIMENT_LABEL = "sandbox-mcp.experiment"

# Keeps the container alive without a workload of its own. Present in both
# coreutils and busybox, so it works from alpine to ubuntu.
KEEPALIVE_COMMAND = "tail -f /dev/null"

# Where per-job pid files live, so a timed-out command can be signalled.
CONTROL_DIR = "/tmp/.sandbox-mcp"


class DockerSandboxBackend(SandboxBackend):
    """One Docker container per experiment, hardened and disposable."""

    name = "docker"

    def __init__(self, settings: Settings, client: docker.DockerClient | None = None) -> None:
        self._settings = settings
        self._client = client
        self._client_lock = asyncio.Lock()

    # --- client management ------------------------------------------------

    async def client(self) -> docker.DockerClient:
        """Lazily connect, so importing the server never requires a daemon."""
        if self._client is not None:
            return self._client
        async with self._client_lock:
            if self._client is None:
                self._client = await asyncio.to_thread(self._connect)
        return self._client

    def _connect(self) -> docker.DockerClient:
        base_url = self._settings.resolved_docker_host()
        try:
            if base_url:
                client = docker.DockerClient(
                    base_url=base_url, timeout=self._settings.docker_timeout_seconds
                )
            else:
                client = docker.from_env(timeout=self._settings.docker_timeout_seconds)
            client.ping()
        except DockerException as exc:
            raise DockerUnavailableError(
                "Cannot reach the Docker daemon. Start Docker Desktop / OrbStack, or set "
                "DOCKER_HOST to the right socket.",
                docker_host=base_url,
                reason=str(exc),
            ) from exc
        return client

    async def health_check(self) -> dict[str, Any]:
        client = await self.client()
        try:
            version = await asyncio.to_thread(client.version)
        except DockerException as exc:
            raise DockerUnavailableError(
                "Docker daemon stopped responding.", reason=str(exc)
            ) from exc
        return {
            "backend": self.name,
            "docker_host": self._settings.resolved_docker_host(),
            "server_version": version.get("Version"),
            "api_version": version.get("ApiVersion"),
            "os": version.get("Os"),
            "arch": version.get("Arch"),
        }

    # --- creation ---------------------------------------------------------

    async def create(self, spec: ExperimentSpec, snapshot_dir: str | None) -> SandboxHandle:
        client = await self.client()
        await self._ensure_image(client, spec.base_image)

        network_name = await self._resolve_network(client, spec)
        container = None
        try:
            container = await asyncio.to_thread(
                client.containers.create, **self._container_config(spec, network_name)
            )
            await asyncio.to_thread(container.start)
        except (APIError, DockerException) as exc:
            if container is not None:
                await self._safe_remove(container)
            await self._remove_network(client, network_name)
            raise SandboxStartupError(
                f"Could not start a sandbox from {spec.base_image}.",
                image=spec.base_image,
                reason=str(exc),
            ) from exc

        container_id = str(container.id)
        handle = SandboxHandle(
            sandbox_id=container_id,
            workspace=spec.workspace_path,
            metadata={"network": network_name, "image": spec.base_image, "user": spec.user},
        )

        try:
            await self._prepare_workspace(container, spec)
            if snapshot_dir:
                await self._upload_project(container, spec, Path(snapshot_dir))
        except Exception:
            await self.destroy(handle)
            raise

        log.info(
            "sandbox_created",
            operation="sandbox.create",
            container_id=container_id[:12],
            image=spec.base_image,
            network_mode=spec.network_mode.value,
            cpu=spec.resources.cpu_limit,
            memory=spec.resources.memory_limit,
        )
        return handle

    async def _ensure_image(self, client: docker.DockerClient, image: str) -> None:
        """Pull on miss. The *server* reaches the registry here -- never the sandbox."""
        try:
            await asyncio.to_thread(client.images.get, image)
            return
        except ImageNotFound:
            pass
        except DockerException as exc:
            raise SandboxBackendError("Failed to inspect local images.", reason=str(exc)) from exc

        log.info("image_pull_started", operation="image.pull", image=image)
        started = time.monotonic()
        try:
            await asyncio.wait_for(
                asyncio.to_thread(client.images.pull, image),
                timeout=self._settings.image_pull_timeout_seconds,
            )
        except TimeoutError as exc:
            raise ImageError(
                f"Timed out pulling {image} after {self._settings.image_pull_timeout_seconds}s.",
                image=image,
            ) from exc
        except ImageNotFound as exc:
            raise ImageError(f"Image {image} does not exist in the registry.", image=image) from exc
        except (APIError, DockerException) as exc:
            raise ImageError(f"Could not pull {image}.", image=image, reason=str(exc)) from exc
        log.info(
            "image_pull_completed",
            operation="image.pull",
            image=image,
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    async def _resolve_network(self, client: docker.DockerClient, spec: ExperimentSpec) -> str:
        """``restricted`` gets a private bridge of its own; the others are built in."""
        if spec.network_mode is NetworkMode.NONE:
            return "none"
        if spec.network_mode is NetworkMode.ENABLED:
            return "bridge"

        name = f"sandbox-mcp-{spec.project_name[:20]}-{int(time.time() * 1000) % 1_000_000}"
        try:
            await asyncio.to_thread(
                client.networks.create,
                name,
                driver="bridge",
                labels={MANAGED_LABEL: "true"},
                check_duplicate=True,
            )
        except (APIError, DockerException) as exc:
            raise SandboxStartupError(
                "Could not create the restricted sandbox network.", reason=str(exc)
            ) from exc
        return name

    def _container_config(self, spec: ExperimentSpec, network_name: str) -> dict[str, Any]:
        resources = spec.resources
        config: dict[str, Any] = {
            "image": spec.base_image,
            "entrypoint": ["/bin/sh", "-c"],
            "command": [KEEPALIVE_COMMAND],
            "working_dir": spec.workspace_path,
            "environment": dict(spec.environment),
            "labels": {MANAGED_LABEL: "true", EXPERIMENT_LABEL: spec.project_name},
            "detach": True,
            "tty": False,
            "stdin_open": False,
            "network_mode": network_name,
            "auto_remove": False,
            # --- isolation ---
            "privileged": False,
            "cap_drop": ["ALL"],
            "cap_add": list(self._settings.sandbox_capabilities),
            "security_opt": ["no-new-privileges:true"],
            # --- resource ceilings ---
            "nano_cpus": int(resources.cpu_limit * 1_000_000_000),
            "mem_limit": resources.memory_bytes,
            # Equal swap and memory limits means the container cannot escape the
            # memory cap by swapping.
            "memswap_limit": resources.memory_bytes,
            "pids_limit": resources.pids_limit,
            "tmpfs": {"/tmp": f"size={self._settings.tmpfs_size_mb}m,mode=1777"},
        }
        if spec.user:
            config["user"] = spec.user
        if spec.mount_strategy.value == "READ_ONLY_BIND_MOUNT" and spec.project_path:
            config["volumes"] = {
                spec.project_path: {"bind": spec.workspace_path, "mode": "ro"},
            }
        return config

    async def _prepare_workspace(self, container: Any, spec: ExperimentSpec) -> None:
        outcome = await self._exec(
            container,
            f"mkdir -p {shlex.quote(spec.workspace_path)} {CONTROL_DIR}",
            timeout=30,
            workdir="/",
        )
        if outcome.exit_code not in (0, None):
            raise SandboxStartupError(
                "Could not prepare the sandbox workspace.",
                stderr=outcome.stderr[:500],
            )

    async def _upload_project(
        self, container: Any, spec: ExperimentSpec, snapshot_dir: Path
    ) -> None:
        """Stream the snapshot in as a tar. The host tree is never bind-mounted."""
        archive = await asyncio.to_thread(_tar_directory, snapshot_dir, _owner_ids(spec.user))
        try:
            await asyncio.to_thread(container.put_archive, spec.workspace_path, archive)
        except (APIError, DockerException) as exc:
            raise SandboxStartupError(
                "Could not copy the project snapshot into the sandbox.", reason=str(exc)
            ) from exc
        if spec.user:
            await self._exec(
                container,
                f"chown -R {shlex.quote(spec.user)} {shlex.quote(spec.workspace_path)}",
                timeout=120,
                workdir="/",
            )

    # --- execution --------------------------------------------------------

    async def execute(
        self,
        handle: SandboxHandle,
        command: str,
        timeout: int,
        workdir: str | None = None,
        environment: dict[str, str] | None = None,
    ) -> ExecOutcome:
        container = await self._get_container(handle)
        return await self._exec(
            container,
            command,
            timeout=timeout,
            workdir=workdir or handle.workspace,
            environment=environment,
        )

    async def _exec(
        self,
        container: Any,
        command: str,
        timeout: int,
        workdir: str,
        environment: dict[str, str] | None = None,
    ) -> ExecOutcome:
        client = await self.client()
        api = client.api
        pid_file = f"{CONTROL_DIR}/{int(time.time() * 1_000_000)}.pid"

        # Run the command as a backgrounded child shell, record that child's pid,
        # then wait on it. The Engine API has no "kill this exec" call, so the
        # pid file is the only handle a timeout has. Quoting the command and
        # handing it to a child shell (rather than exec'ing it) is what makes
        # pipelines, `&&` chains and builtins behave.
        wrapped = (
            f"mkdir -p {CONTROL_DIR} 2>/dev/null; "
            f"/bin/sh -c {shlex.quote(command)} & __sbx_pid=$!; "
            f"echo $__sbx_pid > {pid_file}; "
            f"wait $__sbx_pid"
        )

        try:
            created = await asyncio.to_thread(
                api.exec_create,
                container.id,
                ["/bin/sh", "-c", wrapped],
                workdir=workdir,
                environment=environment or None,
                stdout=True,
                stderr=True,
            )
        except (APIError, DockerException) as exc:
            raise SandboxBackendError(
                "Could not create the command execution.", reason=str(exc)
            ) from exc

        exec_id = created["Id"]
        collector = _OutputCollector(self._settings.max_output_bytes)
        pump = asyncio.create_task(asyncio.to_thread(self._pump_output, api, exec_id, collector))

        timed_out = False
        cancelled = False
        try:
            await asyncio.wait_for(asyncio.shield(pump), timeout=timeout)
        except TimeoutError:
            timed_out = True
            await self._signal_pid(container, pid_file)
            await self._await_pump(pump)
        except asyncio.CancelledError:
            cancelled = True
            await self._signal_pid(container, pid_file)
            await self._await_pump(pump)
            raise
        finally:
            await self._cleanup_pid_file(container, pid_file)
            if cancelled or timed_out:
                log.info(
                    "command_interrupted",
                    operation="sandbox.execute",
                    timed_out=timed_out,
                    cancelled=cancelled,
                )

        exit_code = await self._exec_exit_code(api, exec_id)
        return ExecOutcome(
            exit_code=None if timed_out else exit_code,
            stdout=collector.stdout_text(),
            stderr=collector.stderr_text(),
            stdout_truncated=collector.stdout_truncated,
            stderr_truncated=collector.stderr_truncated,
            timed_out=timed_out,
        )

    @staticmethod
    def _pump_output(api: Any, exec_id: str, collector: _OutputCollector) -> None:
        """Drain the demultiplexed exec stream. Runs on a worker thread."""
        stream = api.exec_start(exec_id, stream=True, demux=True)
        for stdout_chunk, stderr_chunk in stream:
            if stdout_chunk:
                collector.add_stdout(stdout_chunk)
            if stderr_chunk:
                collector.add_stderr(stderr_chunk)

    @staticmethod
    async def _await_pump(pump: asyncio.Task[None]) -> None:
        """Let the reader thread finish after the process was signalled.

        Bounded, because a wedged stream must not wedge the server too.
        """
        # Best effort: a wedged stream must not wedge the server too.
        with contextlib.suppress(Exception):
            await asyncio.wait_for(asyncio.shield(pump), timeout=15)

    async def _signal_pid(self, container: Any, pid_file: str) -> None:
        """TERM then KILL the command. Survivors are reaped when the sandbox dies."""
        script = (
            f'pid=$(cat {pid_file} 2>/dev/null); [ -n "$pid" ] || exit 0; '
            'kill -TERM "$pid" 2>/dev/null; sleep 1; kill -KILL "$pid" 2>/dev/null; exit 0'
        )
        await self._fire_and_forget(container, script)

    async def _cleanup_pid_file(self, container: Any, pid_file: str) -> None:
        await self._fire_and_forget(container, f"rm -f {pid_file}")

    async def _fire_and_forget(self, container: Any, script: str) -> None:
        client = await self.client()
        try:
            created = await asyncio.to_thread(
                client.api.exec_create, container.id, ["/bin/sh", "-c", script]
            )
            await asyncio.to_thread(client.api.exec_start, created["Id"], detach=True)
        except (APIError, DockerException, NotFound):
            pass

    @staticmethod
    async def _exec_exit_code(api: Any, exec_id: str) -> int | None:
        try:
            info = await asyncio.to_thread(api.exec_inspect, exec_id)
        except (APIError, DockerException):
            return None
        return info.get("ExitCode")

    # --- workspace inspection --------------------------------------------

    async def read_manifest(self, handle: SandboxHandle, excludes: list[str]) -> FileManifest:
        """Hash every workspace file, in the container, with the same exclusions
        the snapshot used -- so ``npm install`` does not report 30,000 new files.

        Falls back to streaming the workspace out as a tar when the image has
        no ``sha256sum``.
        """
        container = await self._get_container(handle)
        prune = _find_prune_expression(excludes)
        script = (
            f"cd {shlex.quote(handle.workspace)} 2>/dev/null || exit 3; "
            f"find . {prune} -type f -print0 2>/dev/null | xargs -0 -r sha256sum 2>/dev/null"
        )
        outcome = await self._exec(container, script, timeout=180, workdir=handle.workspace)

        if outcome.exit_code == 3:
            raise SandboxBackendError(
                "Workspace directory is missing inside the sandbox.",
                workspace=handle.workspace,
            )
        if outcome.exit_code == 0 and (outcome.stdout.strip() or not outcome.stderr.strip()):
            return _filter_manifest(_parse_sha256sum_output(outcome.stdout), excludes)

        log.warning(
            "manifest_fallback",
            operation="sandbox.manifest",
            reason="sha256sum unavailable in image",
        )
        return await self._manifest_via_archive(container, handle, excludes)

    async def _manifest_via_archive(
        self, container: Any, handle: SandboxHandle, excludes: list[str]
    ) -> FileManifest:
        from ..security.filesystem import _matches_any  # local import: private helper

        try:
            stream, _ = await asyncio.to_thread(container.get_archive, handle.workspace)
        except (APIError, DockerException, NotFound) as exc:
            raise SandboxBackendError(
                "Could not read the sandbox workspace.", reason=str(exc)
            ) from exc

        def build() -> FileManifest:
            buffer = io.BytesIO(b"".join(stream))
            manifest: FileManifest = {}
            with tarfile.open(fileobj=buffer, mode="r|*") as tar:
                for member in tar:
                    if not member.isfile():
                        continue
                    relative = _strip_archive_root(member.name)
                    if not relative or _matches_any(Path(relative).name, relative, excludes):
                        continue
                    extracted = tar.extractfile(member)
                    if extracted is None:
                        continue
                    payload = extracted.read()
                    manifest[relative] = FileFingerprint(
                        size=len(payload), digest=hash_bytes(payload)
                    )
            return manifest

        return await asyncio.to_thread(build)

    async def read_file(self, handle: SandboxHandle, path: str, max_bytes: int) -> bytes:
        container = await self._get_container(handle)
        try:
            stream, stat = await asyncio.to_thread(container.get_archive, path)
        except NotFound as exc:
            raise SandboxBackendError(f"No such file in the sandbox: {path}", path=path) from exc
        except (APIError, DockerException) as exc:
            raise SandboxBackendError(
                f"Could not read {path} from the sandbox.", path=path, reason=str(exc)
            ) from exc

        if stat.get("size", 0) > max_bytes:
            raise SandboxBackendError(
                f"{path} is {stat['size']} bytes, above the {max_bytes} byte limit.",
                path=path,
                size=stat.get("size"),
            )

        def extract() -> bytes:
            buffer = io.BytesIO(b"".join(stream))
            with tarfile.open(fileobj=buffer, mode="r|*") as tar:
                for member in tar:
                    if member.isfile():
                        handle_ = tar.extractfile(member)
                        if handle_ is not None:
                            return handle_.read(max_bytes + 1)
            return b""

        return await asyncio.to_thread(extract)

    async def write_file(self, handle: SandboxHandle, path: str, content: bytes) -> None:
        container = await self._get_container(handle)
        posix = Path(path)
        archive = await asyncio.to_thread(
            _tar_single_file, posix.name, content, _owner_ids(handle.metadata.get("user"))
        )
        parent = posix.parent.as_posix()
        await self._exec(container, f"mkdir -p {shlex.quote(parent)}", timeout=30, workdir="/")
        try:
            await asyncio.to_thread(container.put_archive, parent, archive)
        except (APIError, DockerException) as exc:
            raise SandboxBackendError(
                f"Could not write {path} into the sandbox.", path=path, reason=str(exc)
            ) from exc

    # --- teardown ---------------------------------------------------------

    async def destroy(self, handle: SandboxHandle) -> bool:
        client = await self.client()
        removed = False
        try:
            container = await asyncio.to_thread(client.containers.get, handle.sandbox_id)
        except NotFound:
            container = None
        except (APIError, DockerException) as exc:
            raise SandboxBackendError(
                "Could not reach Docker to destroy the sandbox.", reason=str(exc)
            ) from exc

        if container is not None:
            removed = await self._safe_remove(container)

        network = handle.metadata.get("network")
        if network and network not in {"none", "bridge", "host"}:
            await self._remove_network(client, network)

        log.info(
            "sandbox_destroyed",
            operation="sandbox.destroy",
            container_id=handle.sandbox_id[:12],
            removed=removed,
        )
        return removed

    @staticmethod
    async def _safe_remove(container: Any) -> bool:
        try:
            await asyncio.to_thread(container.remove, force=True, v=True)
            return True
        except NotFound:
            return False
        except (APIError, DockerException) as exc:
            log.warning("sandbox_remove_failed", operation="sandbox.destroy", reason=str(exc))
            return False

    @staticmethod
    async def _remove_network(client: docker.DockerClient, name: str | None) -> None:
        if not name or name in {"none", "bridge", "host"}:
            return
        try:
            network = await asyncio.to_thread(client.networks.get, name)
            await asyncio.to_thread(network.remove)
        except (NotFound, APIError, DockerException):
            pass

    async def _get_container(self, handle: SandboxHandle) -> Any:
        client = await self.client()
        try:
            return await asyncio.to_thread(client.containers.get, handle.sandbox_id)
        except NotFound as exc:
            raise SandboxBackendError(
                "The sandbox container no longer exists.", sandbox_id=handle.sandbox_id
            ) from exc
        except (APIError, DockerException) as exc:
            raise SandboxBackendError("Could not reach Docker.", reason=str(exc)) from exc

    async def cleanup_orphans(self, live_ids: set[str]) -> int:
        """Remove managed containers no live experiment claims.

        Runs at startup: a crashed server must not leave sandboxes charging the
        developer's CPU forever.
        """
        client = await self.client()
        try:
            containers = await asyncio.to_thread(
                client.containers.list, all=True, filters={"label": f"{MANAGED_LABEL}=true"}
            )
        except (APIError, DockerException):
            return 0
        removed = 0
        for container in containers:
            if container.id in live_ids:
                continue
            if await self._safe_remove(container):
                removed += 1
        if removed:
            log.info("orphans_removed", operation="sandbox.cleanup", count=removed)
        return removed


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class _OutputCollector:
    """Accumulates stream chunks up to a cap, then keeps counting but stops storing."""

    def __init__(self, max_bytes: int) -> None:
        self._max = max_bytes
        self._stdout: list[bytes] = []
        self._stderr: list[bytes] = []
        self._stdout_size = 0
        self._stderr_size = 0
        self.stdout_truncated = False
        self.stderr_truncated = False

    def add_stdout(self, chunk: bytes) -> None:
        if self._stdout_size >= self._max:
            self.stdout_truncated = True
            return
        room = self._max - self._stdout_size
        self._stdout.append(chunk[:room])
        self._stdout_size += min(len(chunk), room)
        if len(chunk) > room:
            self.stdout_truncated = True

    def add_stderr(self, chunk: bytes) -> None:
        if self._stderr_size >= self._max:
            self.stderr_truncated = True
            return
        room = self._max - self._stderr_size
        self._stderr.append(chunk[:room])
        self._stderr_size += min(len(chunk), room)
        if len(chunk) > room:
            self.stderr_truncated = True

    def stdout_text(self) -> str:
        return b"".join(self._stdout).decode("utf-8", errors="replace")

    def stderr_text(self) -> str:
        return b"".join(self._stderr).decode("utf-8", errors="replace")


def _tar_directory(source: Path, owner: tuple[int, int] = (0, 0)) -> bytes:
    """Tar a directory's *contents* for ``put_archive``.

    Host uid/gid are stripped: they mean nothing inside the container and, when
    preserved, leave the workspace unwritable by the sandbox user. Modes are
    normalised to 0644/0755, keeping only the executable bit.
    """
    uid, gid = owner
    buffer = io.BytesIO()

    def normalise(info: tarfile.TarInfo) -> tarfile.TarInfo:
        info.uid, info.gid = uid, gid
        info.uname = info.gname = ""
        if info.isdir():
            info.mode = 0o755
        elif info.isfile():
            info.mode = 0o755 if info.mode & 0o100 else 0o644
        return info

    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for entry in sorted(source.rglob("*")):
            tar.add(
                entry,
                arcname=entry.relative_to(source).as_posix(),
                recursive=False,
                filter=normalise,
            )
    return buffer.getvalue()


def _tar_single_file(name: str, content: bytes, owner: tuple[int, int] = (0, 0)) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        info = tarfile.TarInfo(name=name)
        info.size = len(content)
        info.mode = 0o644
        info.uid, info.gid = owner
        tar.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def _owner_ids(user: str | None) -> tuple[int, int]:
    """Map a Docker ``user`` spec to numeric ids for tar ownership.

    Named users cannot be resolved from outside the image, so they fall back to
    root-owned files; the explicit chown after upload fixes those up.
    """
    if not user:
        return (0, 0)
    uid, _, gid = user.partition(":")
    try:
        return (int(uid), int(gid) if gid else int(uid))
    except ValueError:
        return (0, 0)


def _strip_archive_root(name: str) -> str:
    """``get_archive`` prefixes every member with the directory's basename."""
    parts = Path(name).parts
    return Path(*parts[1:]).as_posix() if len(parts) > 1 else ""


def _find_prune_expression(excludes: list[str]) -> str:
    """Turn exclude globs into a ``find`` prune clause.

    Only directory-shaped patterns (no slash, no glob) are pruned; the rest are
    filtered afterwards on the host, where the matching rules already live.
    """
    names = sorted({e for e in excludes if "/" not in e and "*" not in e})
    if not names:
        return ""
    clauses = " -o ".join(f"-name {shlex.quote(name)}" for name in names)
    return f"\\( {clauses} \\) -prune -o"


def _filter_manifest(manifest: FileManifest, excludes: list[str]) -> FileManifest:
    """Apply the glob-shaped exclusions ``find -prune`` could not express."""
    from ..security.filesystem import _matches_any

    return {
        path: fingerprint
        for path, fingerprint in manifest.items()
        if not _matches_any(Path(path).name, path, excludes)
    }


def _parse_sha256sum_output(output: str) -> FileManifest:
    manifest: FileManifest = {}
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        digest, _, path = stripped.partition("  ")
        if not path:
            continue
        relative = path[2:] if path.startswith("./") else path
        manifest[relative] = FileFingerprint(size=-1, digest=digest)
    return manifest
