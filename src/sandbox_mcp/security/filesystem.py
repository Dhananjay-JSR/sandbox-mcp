"""Filesystem isolation.

Two jobs:

* Decide whether a host path may be read at all (:func:`validate_project_path`)
  and whether a sandbox path may be written or fetched
  (:func:`validate_sandbox_path`).
* Take an immutable snapshot of the project (:class:`ProjectSnapshotter`) that
  becomes both the container's workspace *and* the baseline every later diff is
  computed against. The developer's tree is opened read-only, once, and never
  touched again.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from ..config import SECRET_FILE_PATTERNS, Settings
from ..errors import InvalidProjectPathError, PolicyViolationError, UnauthorizedPathError

CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True, slots=True)
class FileFingerprint:
    """Enough to tell "changed" from "same" without keeping the content."""

    size: int
    digest: str
    kind: str = "file"  # file | link


FileManifest = dict[str, FileFingerprint]


@dataclass(slots=True)
class SnapshotResult:
    root: Path
    manifest: FileManifest
    file_count: int
    total_bytes: int
    skipped: list[str] = field(default_factory=list)


def _matches_any(name: str, relpath: str, patterns: tuple[str, ...] | list[str]) -> bool:
    """Match a glob against the entry's own name or its full relative path.

    ``node_modules`` therefore excludes it at any depth, while ``src/*.log``
    only excludes the shallow ones -- which is what people expect.
    """
    for pattern in patterns:
        if fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(relpath, pattern):
            return True
        if "/" not in pattern and any(
            fnmatch.fnmatch(part, pattern) for part in PurePosixPath(relpath).parts
        ):
            return True
    return False


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def validate_project_path(raw_path: str, settings: Settings) -> Path:
    """Resolve and authorise a host project directory.

    Rejects: non-directories, anything inside a denied root (``~/.ssh``,
    ``/etc``, ...), anything outside ``allowed_project_roots`` when that is
    configured, and the bare home or filesystem root -- copying either is
    never what the agent meant and would sweep up credentials wholesale.
    """
    candidate = Path(raw_path).expanduser()
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise InvalidProjectPathError(
            f"Project path does not exist or cannot be resolved: {raw_path}",
            path=raw_path,
        ) from exc

    if not resolved.is_dir():
        raise InvalidProjectPathError(
            f"Project path is not a directory: {resolved}", path=str(resolved)
        )

    home = Path.home().resolve()
    if resolved == home or resolved == Path(resolved.anchor):
        raise UnauthorizedPathError(
            "Refusing to snapshot the home or filesystem root. Point at a project directory.",
            path=str(resolved),
        )

    for denied in settings.denied_project_roots:
        denied_resolved = Path(denied).expanduser()
        if _is_within(resolved, denied_resolved) or resolved == denied_resolved:
            raise UnauthorizedPathError(
                f"Project path is inside a protected location: {denied_resolved}",
                path=str(resolved),
            )

    if settings.allowed_project_roots:
        allowed = [Path(root).expanduser().resolve() for root in settings.allowed_project_roots]
        if not any(_is_within(resolved, root) or resolved == root for root in allowed):
            raise UnauthorizedPathError(
                "Project path is outside every configured allowed root.",
                path=str(resolved),
                allowed_roots=[str(root) for root in allowed],
            )

    return resolved


def validate_sandbox_path(raw_path: str, workspace: str) -> str:
    """Normalise a path *inside* the sandbox and confine it to the workspace.

    Returns an absolute POSIX path. Relative inputs are taken as workspace
    relative; ``..`` traversal and absolute paths outside the workspace are
    refused, so an agent cannot ask us to fetch ``/etc/shadow`` from the
    container or write outside its own tree.
    """
    if not raw_path or raw_path.strip() == "":
        raise UnauthorizedPathError("Empty sandbox path.", path=raw_path)

    workspace_path = PurePosixPath(workspace)
    candidate = PurePosixPath(raw_path)
    joined = candidate if candidate.is_absolute() else workspace_path / candidate

    # Manual normalisation: PurePosixPath keeps '..' segments as-is.
    parts: list[str] = []
    for part in joined.parts:
        if part == "..":
            if len(parts) <= 1:
                raise UnauthorizedPathError(
                    f"Path escapes the sandbox workspace: {raw_path}", path=raw_path
                )
            parts.pop()
        elif part not in (".",):
            parts.append(part)

    normalised = PurePosixPath(*parts)
    if not normalised.is_relative_to(workspace_path):
        raise UnauthorizedPathError(
            f"Path is outside the sandbox workspace {workspace}: {raw_path}",
            path=raw_path,
            workspace=workspace,
        )
    return str(normalised)


def hash_file(path: Path) -> tuple[str, int]:
    """Streaming sha256 plus size, so large files don't land in memory."""
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK_SIZE):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ProjectSnapshotter:
    """Copies a project into a sandbox-owned directory, filtering as it goes.

    Filtering is two-tier: ``settings.snapshot_excludes`` is the tunable
    noise filter (``node_modules``, build output), while
    :data:`SECRET_FILE_PATTERNS` is unconditional -- ``.env`` files and key
    material never enter a sandbox even if someone empties the exclude list.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def snapshot(self, source: Path, destination: Path) -> SnapshotResult:
        destination.mkdir(parents=True, exist_ok=True)
        excludes = list(self._settings.snapshot_excludes)
        manifest: FileManifest = {}
        skipped: list[str] = []
        file_count = 0
        total_bytes = 0

        for dirpath, dirnames, filenames in os.walk(source, followlinks=False):
            current = Path(dirpath)
            rel_dir = current.relative_to(source)

            # Prune excluded directories in place so os.walk never descends.
            kept_dirs = []
            for dirname in dirnames:
                rel = (rel_dir / dirname).as_posix()
                if _matches_any(dirname, rel, SECRET_FILE_PATTERNS):
                    skipped.append(f"{rel}/ (protected)")
                elif _matches_any(dirname, rel, excludes):
                    skipped.append(f"{rel}/ (excluded)")
                else:
                    kept_dirs.append(dirname)
            dirnames[:] = kept_dirs

            for dirname in dirnames:
                (destination / rel_dir / dirname).mkdir(parents=True, exist_ok=True)

            for filename in filenames:
                rel = (rel_dir / filename).as_posix()
                source_file = current / filename

                if _matches_any(filename, rel, SECRET_FILE_PATTERNS):
                    skipped.append(f"{rel} (protected)")
                    continue
                if _matches_any(filename, rel, excludes):
                    continue

                target = destination / rel_dir / filename
                target.parent.mkdir(parents=True, exist_ok=True)

                if source_file.is_symlink():
                    link_target = os.readlink(source_file)
                    if target.exists() or target.is_symlink():
                        target.unlink()
                    os.symlink(link_target, target)
                    manifest[rel] = FileFingerprint(
                        size=0, digest=hash_bytes(link_target.encode()), kind="link"
                    )
                    continue

                if not source_file.is_file():
                    skipped.append(f"{rel} (not a regular file)")
                    continue

                digest, size = hash_file(source_file)
                file_count += 1
                total_bytes += size
                if file_count > self._settings.max_project_files:
                    raise PolicyViolationError(
                        f"Project exceeds the {self._settings.max_project_files} file limit. "
                        "Narrow the project path or extend snapshot_excludes.",
                        limit="max_project_files",
                    )
                if total_bytes > self._settings.max_project_bytes:
                    raise PolicyViolationError(
                        f"Project exceeds the {self._settings.max_project_bytes} byte limit. "
                        "Narrow the project path or extend snapshot_excludes.",
                        limit="max_project_bytes",
                    )

                shutil.copy2(source_file, target)
                manifest[rel] = FileFingerprint(size=size, digest=digest)

        return SnapshotResult(
            root=destination,
            manifest=manifest,
            file_count=file_count,
            total_bytes=total_bytes,
            skipped=skipped,
        )


def remove_tree(path: Path) -> bool:
    """Delete a snapshot directory. Returns whether anything was removed."""
    if not path.exists():
        return False
    shutil.rmtree(path, ignore_errors=True)
    return True
