"""Diffing the sandbox against its baseline.

The baseline is the fingerprint taken at snapshot time -- before the container
existed. Comparing it with a fresh fingerprint from inside the sandbox answers
the question the agent actually has: *what did my experiment change?*

Note the exclusions carry over from the snapshot. ``npm install`` writing
30,000 files into ``node_modules`` is not a change worth reporting, and drowning
the real two-line edit in it would make this tool useless.
"""

from __future__ import annotations

import difflib
from collections.abc import Awaitable, Callable
from pathlib import Path

from ..logging import get_logger
from ..models import ChangeSet, ChangeType, FileChange
from ..security.filesystem import FileManifest

log = get_logger(__name__)

FileReader = Callable[[str], Awaitable[bytes | None]]

# Files that never diff usefully. Reported as changed, shown as binary.
BINARY_SNIFF_BYTES = 8000


def is_binary(payload: bytes) -> bool:
    if b"\x00" in payload[:BINARY_SNIFF_BYTES]:
        return True
    try:
        payload[:BINARY_SNIFF_BYTES].decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


def _decode(payload: bytes) -> list[str]:
    return payload.decode("utf-8", errors="replace").splitlines(keepends=True)


def unified_diff(path: str, before: bytes, after: bytes) -> tuple[str, int, int]:
    """Return ``(diff_text, insertions, deletions)`` in the usual git shape."""
    lines = list(
        difflib.unified_diff(
            _decode(before),
            _decode(after),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            n=3,
        )
    )
    insertions = sum(1 for line in lines if line.startswith("+") and not line.startswith("+++"))
    deletions = sum(1 for line in lines if line.startswith("-") and not line.startswith("---"))
    return "".join(lines), insertions, deletions


async def compute_changes(
    *,
    experiment_id: str,
    baseline: FileManifest,
    current: FileManifest,
    read_sandbox_file: FileReader,
    snapshot_dir: Path | None,
    include_diff: bool = False,
    max_files_with_diff: int = 50,
    max_file_bytes: int = 512 * 1024,
) -> ChangeSet:
    """Build a :class:`ChangeSet`.

    Line statistics require reading both versions, which costs a round trip per
    file, so they are gathered for at most ``max_files_with_diff`` files. Beyond
    that the file lists stay complete and ``truncated`` is set -- an honest
    partial answer beats a slow one or a silent one.
    """
    baseline_paths = set(baseline)
    current_paths = set(current)

    created = sorted(current_paths - baseline_paths)
    deleted = sorted(baseline_paths - current_paths)
    modified = sorted(
        path
        for path in baseline_paths & current_paths
        if baseline[path].digest != current[path].digest
    )

    changeset = ChangeSet(
        experiment_id=experiment_id,
        files_created=created,
        files_modified=modified,
        files_deleted=deleted,
    )

    budget = max_files_with_diff
    for path, change_type in (
        [(p, ChangeType.MODIFIED) for p in modified]
        + [(p, ChangeType.CREATED) for p in created]
        + [(p, ChangeType.DELETED) for p in deleted]
    ):
        if budget <= 0:
            changeset.truncated = True
            break
        budget -= 1

        before = _read_baseline(snapshot_dir, path, max_file_bytes)
        after = (
            None
            if change_type is ChangeType.DELETED
            else await _read_current(read_sandbox_file, path, max_file_bytes)
        )

        change = _build_change(
            path=path,
            change_type=change_type,
            before=before,
            after=after,
            include_diff=include_diff,
            max_file_bytes=max_file_bytes,
        )
        changeset.changes.append(change)
        changeset.insertions += change.insertions
        changeset.deletions += change.deletions

    if changeset.truncated:
        changeset.note = (
            f"Line statistics cover the first {max_files_with_diff} changed files. "
            "The file lists above are complete."
        )
    return changeset


def _build_change(
    *,
    path: str,
    change_type: ChangeType,
    before: bytes | None,
    after: bytes | None,
    include_diff: bool,
    max_file_bytes: int,
) -> FileChange:
    payload = after if after is not None else before
    size = len(after) if after is not None else None

    if payload is None:
        # Unreadable on both sides: still a real change, just an opaque one.
        return FileChange(path=path, change_type=change_type, size_bytes=size)

    if len(payload) > max_file_bytes:
        # Too big to diff usefully; report the change, skip the body.
        return FileChange(path=path, change_type=change_type, size_bytes=size)

    if is_binary(payload):
        return FileChange(path=path, change_type=change_type, size_bytes=size, binary=True)

    diff_text, insertions, deletions = unified_diff(path, before or b"", after or b"")
    return FileChange(
        path=path,
        change_type=change_type,
        size_bytes=size,
        insertions=insertions,
        deletions=deletions,
        diff=diff_text if include_diff else None,
    )


def _read_baseline(snapshot_dir: Path | None, path: str, max_bytes: int) -> bytes | None:
    """Read from the host-side snapshot: the pristine copy, never the user's tree."""
    if snapshot_dir is None:
        return None
    candidate = snapshot_dir / path
    try:
        if not candidate.is_file() or candidate.stat().st_size > max_bytes:
            return None
        return candidate.read_bytes()
    except OSError:
        return None


async def _read_current(reader: FileReader, path: str, max_bytes: int) -> bytes | None:
    try:
        return await reader(path)
    except Exception as exc:  # a single unreadable file must not fail the diff
        log.debug("diff_read_failed", operation="changes.read", path=path, reason=str(exc))
        return None
