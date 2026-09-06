"""Artifact collection.

Build output, test reports, benchmark results -- the things worth surviving the
sandbox. Files move one way only: out of the container, into the server's own
state directory. There is no tool that writes an artifact back to the host
project, and no path an agent can supply that reaches outside the workspace.
"""

from __future__ import annotations

import hashlib
import shlex
from pathlib import Path, PurePosixPath

from ..config import Settings
from ..errors import SandboxBackendError
from ..logging import get_logger
from ..models import Artifact, ArtifactCollectionResult, Experiment
from ..sandbox.interface import SandboxBackend, SandboxHandle
from ..security.filesystem import validate_sandbox_path

log = get_logger(__name__)


class ArtifactManager:
    """Expands patterns inside the sandbox, then copies the matches out."""

    def __init__(self, settings: Settings, backend: SandboxBackend) -> None:
        self._settings = settings
        self._backend = backend

    def experiment_dir(self, experiment_id: str) -> Path:
        return self._settings.artifacts_dir / experiment_id

    async def collect(
        self,
        experiment: Experiment,
        handle: SandboxHandle,
        patterns: list[str],
    ) -> ArtifactCollectionResult:
        matches, skipped = await self._expand(handle, patterns)
        destination = self.experiment_dir(experiment.id)
        destination.mkdir(parents=True, exist_ok=True)

        collected: list[Artifact] = []
        total_bytes = 0

        for sandbox_path in matches:
            if len(collected) >= self._settings.max_artifacts_per_experiment:
                skipped.append(
                    f"{sandbox_path} (per-experiment limit of "
                    f"{self._settings.max_artifacts_per_experiment} reached)"
                )
                break
            try:
                payload = await self._backend.read_file(
                    handle, sandbox_path, self._settings.max_artifact_bytes
                )
            except SandboxBackendError as exc:
                skipped.append(f"{sandbox_path} ({exc.message})")
                continue

            relative = PurePosixPath(sandbox_path).relative_to(handle.workspace)
            host_path = destination / relative
            host_path.parent.mkdir(parents=True, exist_ok=True)
            host_path.write_bytes(payload)

            artifact = Artifact(
                experiment_id=experiment.id,
                sandbox_path=sandbox_path,
                host_path=str(host_path),
                size_bytes=len(payload),
                sha256=hashlib.sha256(payload).hexdigest(),
            )
            collected.append(artifact)
            total_bytes += len(payload)

        log.info(
            "artifacts_collected",
            operation="artifacts.collect",
            experiment_id=experiment.id,
            count=len(collected),
            skipped=len(skipped),
            total_bytes=total_bytes,
        )
        return ArtifactCollectionResult(
            experiment_id=experiment.id,
            artifacts=collected,
            skipped=skipped,
            total_bytes=total_bytes,
        )

    async def _expand(
        self, handle: SandboxHandle, patterns: list[str]
    ) -> tuple[list[str], list[str]]:
        """Resolve globs *inside* the sandbox, then re-validate every result.

        Two forms are supported: ordinary shell globs (``dist/*.js``) and a
        leading ``**/`` for a recursive name search (``**/junit.xml``). The
        expansion runs in the container, so a pattern can only ever name files
        the container can see -- and each hit is still confined to the
        workspace before it is fetched.
        """
        skipped: list[str] = []
        script_parts: list[str] = [f"cd {shlex.quote(handle.workspace)} 2>/dev/null || exit 3"]

        for pattern in patterns:
            cleaned = pattern.strip()
            if not cleaned or cleaned.startswith("/") or ".." in PurePosixPath(cleaned).parts:
                skipped.append(f"{pattern} (pattern must be workspace-relative)")
                continue
            if cleaned.startswith("**/"):
                name = cleaned[3:]
                script_parts.append(
                    f"find . -type f -name {shlex.quote(name)} 2>/dev/null | sed 's|^\\./||'"
                )
            else:
                # Unquoted expansion is the point here: the shell does the glob.
                script_parts.append(
                    f'for f in {cleaned}; do [ -f "$f" ] && printf "%s\\n" "$f"; done'
                )

        if len(script_parts) == 1:
            return [], skipped

        outcome = await self._backend.execute(
            handle, "; ".join(script_parts), timeout=60, workdir=handle.workspace
        )
        if outcome.exit_code == 3:
            raise SandboxBackendError(
                "Workspace is missing inside the sandbox.", workspace=handle.workspace
            )

        matches: list[str] = []
        seen: set[str] = set()
        for line in outcome.stdout.splitlines():
            candidate = line.strip()
            if not candidate:
                continue
            try:
                resolved = validate_sandbox_path(candidate, handle.workspace)
            except Exception:
                skipped.append(f"{candidate} (outside the workspace)")
                continue
            if resolved not in seen:
                seen.add(resolved)
                matches.append(resolved)

        if not matches and not skipped:
            skipped.append("no files matched the requested patterns")
        return matches, skipped
