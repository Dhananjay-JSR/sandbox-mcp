"""Filesystem confinement: what may be read, what is copied, what can never escape."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from sandbox_mcp.config import Settings
from sandbox_mcp.errors import (
    InvalidProjectPathError,
    PolicyViolationError,
    UnauthorizedPathError,
)
from sandbox_mcp.security.filesystem import (
    ProjectSnapshotter,
    remove_tree,
    validate_project_path,
    validate_sandbox_path,
)


class TestProjectPathValidation:
    def test_accepts_a_real_project(self, project: Path, settings: Settings) -> None:
        assert validate_project_path(str(project), settings) == project.resolve()

    def test_rejects_a_missing_path(self, tmp_path: Path, settings: Settings) -> None:
        with pytest.raises(InvalidProjectPathError):
            validate_project_path(str(tmp_path / "nope"), settings)

    def test_rejects_a_file(self, project: Path, settings: Settings) -> None:
        with pytest.raises(InvalidProjectPathError):
            validate_project_path(str(project / "package.json"), settings)

    def test_rejects_the_home_directory(self, settings: Settings) -> None:
        """Snapshotting a home directory would sweep up every credential on disk."""
        with pytest.raises(UnauthorizedPathError):
            validate_project_path(str(Path.home()), settings)

    def test_rejects_the_filesystem_root(self, settings: Settings) -> None:
        with pytest.raises(UnauthorizedPathError):
            validate_project_path("/", settings)

    def test_rejects_protected_locations(self, settings: Settings, tmp_path: Path) -> None:
        secrets = tmp_path / "secrets"
        (secrets / "inner").mkdir(parents=True)
        settings.denied_project_roots = [secrets]
        with pytest.raises(UnauthorizedPathError):
            validate_project_path(str(secrets / "inner"), settings)

    def test_allowed_roots_exclude_everything_else(
        self, settings: Settings, tmp_path: Path, project: Path
    ) -> None:
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        settings.allowed_project_roots = [elsewhere]
        with pytest.raises(UnauthorizedPathError):
            validate_project_path(str(project), settings)
        assert validate_project_path(str(elsewhere), settings) == elsewhere.resolve()

    def test_symlink_to_a_protected_location_is_resolved_before_checking(
        self, settings: Settings, tmp_path: Path
    ) -> None:
        """Resolving first is what stops a symlink from laundering a denied path."""
        protected = tmp_path / "protected"
        protected.mkdir()
        settings.denied_project_roots = [protected]
        link = tmp_path / "innocent-looking"
        link.symlink_to(protected)
        with pytest.raises(UnauthorizedPathError):
            validate_project_path(str(link), settings)


class TestSandboxPathConfinement:
    @pytest.mark.parametrize(
        "given,expected",
        [
            ("src/a.ts", "/workspace/src/a.ts"),
            ("./src/a.ts", "/workspace/src/a.ts"),
            ("/workspace/b.txt", "/workspace/b.txt"),
            ("/workspace/x/../y.txt", "/workspace/y.txt"),
            ("a/b/../c.txt", "/workspace/a/c.txt"),
        ],
    )
    def test_normalises_workspace_relative_paths(self, given: str, expected: str) -> None:
        assert validate_sandbox_path(given, "/workspace") == expected

    @pytest.mark.parametrize(
        "given",
        [
            "../etc/passwd",
            "../../../../etc/shadow",
            "/etc/passwd",
            "/workspace/../etc/passwd",
            "/root/.ssh/id_rsa",
            "",
        ],
    )
    def test_refuses_anything_outside_the_workspace(self, given: str) -> None:
        with pytest.raises(UnauthorizedPathError):
            validate_sandbox_path(given, "/workspace")


class TestSnapshotting:
    def test_copies_the_project_without_touching_the_source(
        self, project: Path, settings: Settings, tmp_path: Path
    ) -> None:
        before = {
            p.relative_to(project).as_posix(): p.read_bytes()
            for p in project.rglob("*")
            if p.is_file()
        }
        result = ProjectSnapshotter(settings).snapshot(project, tmp_path / "snap")
        after = {
            p.relative_to(project).as_posix(): p.read_bytes()
            for p in project.rglob("*")
            if p.is_file()
        }
        assert before == after
        assert (tmp_path / "snap" / "src" / "index.js").read_text() == "module.exports = 1;\n"
        assert result.file_count == 3

    def test_never_copies_secrets(self, project: Path, settings: Settings, tmp_path: Path) -> None:
        result = ProjectSnapshotter(settings).snapshot(project, tmp_path / "snap")
        assert not (tmp_path / "snap" / ".env").exists()
        assert not (tmp_path / "snap" / "id_rsa").exists()
        assert ".env" not in result.manifest
        assert any("protected" in entry for entry in result.skipped)

    def test_secrets_are_withheld_even_with_an_empty_exclude_list(
        self, project: Path, settings: Settings, tmp_path: Path
    ) -> None:
        """The secret filter is unconditional, not merely a default."""
        settings.snapshot_excludes = []
        ProjectSnapshotter(settings).snapshot(project, tmp_path / "snap")
        assert not (tmp_path / "snap" / ".env").exists()
        assert (tmp_path / "snap" / "node_modules").exists()

    def test_skips_configured_noise(
        self, project: Path, settings: Settings, tmp_path: Path
    ) -> None:
        result = ProjectSnapshotter(settings).snapshot(project, tmp_path / "snap")
        assert not (tmp_path / "snap" / "node_modules").exists()
        assert not any(path.startswith("node_modules") for path in result.manifest)

    def test_manifest_fingerprints_every_copied_file(
        self, project: Path, settings: Settings, tmp_path: Path
    ) -> None:
        result = ProjectSnapshotter(settings).snapshot(project, tmp_path / "snap")
        assert set(result.manifest) == {"package.json", "src/index.js", "src/util.js"}
        assert all(len(fp.digest) == 64 for fp in result.manifest.values())

    def test_preserves_symlinks_as_links_rather_than_following_them(
        self, project: Path, settings: Settings, tmp_path: Path
    ) -> None:
        """Following a symlink out of the project would copy host files in."""
        outside = tmp_path / "outside.txt"
        outside.write_text("host data\n")
        (project / "link.txt").symlink_to(outside)
        ProjectSnapshotter(settings).snapshot(project, tmp_path / "snap")
        copied = tmp_path / "snap" / "link.txt"
        assert copied.is_symlink()
        assert os.readlink(copied) == str(outside)

    def test_refuses_a_project_over_the_file_limit(
        self, project: Path, settings: Settings, tmp_path: Path
    ) -> None:
        settings.max_project_files = 2
        with pytest.raises(PolicyViolationError):
            ProjectSnapshotter(settings).snapshot(project, tmp_path / "snap")

    def test_refuses_a_project_over_the_byte_limit(
        self, project: Path, settings: Settings, tmp_path: Path
    ) -> None:
        settings.max_project_bytes = 10
        with pytest.raises(PolicyViolationError):
            ProjectSnapshotter(settings).snapshot(project, tmp_path / "snap")


def test_remove_tree_is_idempotent(tmp_path: Path) -> None:
    target = tmp_path / "gone"
    target.mkdir()
    assert remove_tree(target) is True
    assert remove_tree(target) is False
