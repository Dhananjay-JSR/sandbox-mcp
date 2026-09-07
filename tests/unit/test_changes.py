from __future__ import annotations

from pathlib import Path

import pytest

from sandbox_mcp.experiments.changes import compute_changes, is_binary, unified_diff
from sandbox_mcp.security.filesystem import FileFingerprint as FP


def reader(files: dict[str, bytes]):
    async def read(path: str) -> bytes | None:
        return files.get(path)

    return read


class TestDiffPrimitives:
    def test_counts_insertions_and_deletions(self) -> None:
        diff, insertions, deletions = unified_diff("a.txt", b"one\ntwo\n", b"one\nthree\nfour\n")
        assert (insertions, deletions) == (2, 1)
        assert "+three" in diff and "-two" in diff

    def test_headers_are_not_counted_as_changes(self) -> None:
        _, insertions, deletions = unified_diff("a.txt", b"", b"only\n")
        assert (insertions, deletions) == (1, 0)

    @pytest.mark.parametrize(
        "payload,expected",
        [(b"plain text\n", False), (b"\x00\x01\x02", True), (b"\xff\xfe\xfd\x00", True)],
    )
    def test_detects_binary(self, payload: bytes, expected: bool) -> None:
        assert is_binary(payload) is expected


class TestChangeSets:
    async def test_classifies_every_kind_of_change(self, tmp_path: Path) -> None:
        (tmp_path / "kept.txt").write_text("same\n")
        (tmp_path / "edited.txt").write_text("one\ntwo\n")
        (tmp_path / "removed.txt").write_text("gone\n")

        baseline = {
            "kept.txt": FP(5, "a"),
            "edited.txt": FP(8, "b"),
            "removed.txt": FP(5, "c"),
        }
        current = {"kept.txt": FP(5, "a"), "edited.txt": FP(14, "b2"), "added.txt": FP(4, "d")}
        files = {"edited.txt": b"one\ntwo\nthree\n", "added.txt": b"new\n"}

        changes = await compute_changes(
            experiment_id="exp_1",
            baseline=baseline,
            current=current,
            read_sandbox_file=reader(files),
            snapshot_dir=tmp_path,
            include_diff=True,
        )

        assert changes.files_created == ["added.txt"]
        assert changes.files_modified == ["edited.txt"]
        assert changes.files_deleted == ["removed.txt"]
        assert "kept.txt" not in changes.files_modified
        assert changes.insertions == 2  # one added line, one created file
        assert changes.deletions == 1  # the deleted file's single line
        assert changes.total_changed == 3

    async def test_omits_diff_bodies_unless_asked(self, tmp_path: Path) -> None:
        (tmp_path / "a.txt").write_text("one\n")
        changes = await compute_changes(
            experiment_id="exp_1",
            baseline={"a.txt": FP(4, "x")},
            current={"a.txt": FP(8, "y")},
            read_sandbox_file=reader({"a.txt": b"one\ntwo\n"}),
            snapshot_dir=tmp_path,
            include_diff=False,
        )
        assert changes.changes[0].diff is None
        assert changes.changes[0].insertions == 1

    async def test_marks_binary_files_without_diffing_them(self, tmp_path: Path) -> None:
        (tmp_path / "logo.png").write_bytes(b"\x89PNG\x00old")
        changes = await compute_changes(
            experiment_id="exp_1",
            baseline={"logo.png": FP(7, "x")},
            current={"logo.png": FP(7, "y")},
            read_sandbox_file=reader({"logo.png": b"\x89PNG\x00new"}),
            snapshot_dir=tmp_path,
            include_diff=True,
        )
        assert changes.changes[0].binary is True
        assert changes.changes[0].diff is None

    async def test_truncates_line_stats_but_keeps_the_file_lists_complete(
        self, tmp_path: Path
    ) -> None:
        current = {f"f{n}.txt": FP(2, str(n)) for n in range(10)}
        changes = await compute_changes(
            experiment_id="exp_1",
            baseline={},
            current=current,
            read_sandbox_file=reader(dict.fromkeys(current, b"x\n")),
            snapshot_dir=tmp_path,
            include_diff=False,
            max_files_with_diff=3,
        )
        assert len(changes.files_created) == 10
        assert len(changes.changes) == 3
        assert changes.truncated is True
        assert changes.note is not None

    async def test_an_unreadable_file_is_still_reported_as_changed(self, tmp_path: Path) -> None:
        changes = await compute_changes(
            experiment_id="exp_1",
            baseline={},
            current={"vanished.txt": FP(1, "x")},
            read_sandbox_file=reader({}),
            snapshot_dir=tmp_path,
            include_diff=True,
        )
        assert changes.files_created == ["vanished.txt"]
        assert changes.changes[0].insertions == 0
