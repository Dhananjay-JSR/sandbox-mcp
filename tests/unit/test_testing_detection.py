from __future__ import annotations

import pytest

from sandbox_mcp.experiments.testing import (
    FRAMEWORKS,
    choose_framework,
    detection_script,
    parse_test_output,
)


class TestFrameworkDetection:
    @pytest.mark.parametrize(
        "present,expected",
        [
            ({"package.json"}, "npm"),
            ({"Cargo.toml"}, "cargo"),
            ({"go.mod"}, "go"),
            ({"pyproject.toml"}, "pytest"),
            ({"pytest.ini"}, "pytest"),
            ({"Makefile"}, "make"),
        ],
    )
    def test_picks_the_right_runner(self, present: set[str], expected: str) -> None:
        framework = choose_framework(present)
        assert framework is not None and framework.name == expected

    def test_prefers_the_more_specific_runner(self) -> None:
        """A Rust project with a Makefile is still a cargo project."""
        framework = choose_framework({"Cargo.toml", "Makefile", "package.json"})
        assert framework is not None and framework.name == "cargo"

    def test_returns_nothing_when_it_cannot_tell(self) -> None:
        assert choose_framework(set()) is None
        assert choose_framework({"README.md"}) is None

    def test_the_probe_script_asks_about_every_marker(self) -> None:
        script = detection_script()
        for framework in FRAMEWORKS:
            for marker in framework.marker_files:
                assert marker in script


class TestOutputParsing:
    def test_node_test_runner(self) -> None:
        summary = parse_test_output(
            "# tests 40\n# pass 37\n# fail 3\nnot ok 3 - seal produces hex output\n"
        )
        assert summary.detected
        assert (summary.total, summary.passed, summary.failed) == (40, 37, 3)
        assert summary.failing_tests == ["seal produces hex output"]

    def test_pytest(self) -> None:
        summary = parse_test_output(
            "FAILED tests/test_a.py::test_x\n"
            "=========== 3 failed, 181 passed, 2 skipped in 1.20s ============"
        )
        assert (summary.passed, summary.failed, summary.skipped) == (181, 3, 2)
        assert summary.failing_tests == ["tests/test_a.py::test_x"]

    def test_jest(self) -> None:
        summary = parse_test_output("Tests:       2 failed, 1 skipped, 181 passed, 184 total")
        assert (summary.total, summary.passed, summary.failed) == (184, 181, 2)

    def test_mocha(self) -> None:
        summary = parse_test_output("  184 passing (2s)\n  2 failing\n  1 pending")
        assert (summary.passed, summary.failed, summary.skipped) == (184, 2, 1)

    def test_cargo_sums_across_binaries(self) -> None:
        summary = parse_test_output(
            "test result: ok. 12 passed; 0 failed; 1 ignored; 0 measured\n"
            "test result: FAILED. 3 passed; 2 failed; 0 ignored; 0 measured"
        )
        assert (summary.passed, summary.failed, summary.skipped) == (15, 2, 1)

    def test_go(self) -> None:
        summary = parse_test_output("--- PASS: TestA\n--- FAIL: TestB\n--- PASS: TestC")
        assert (summary.passed, summary.failed) == (2, 1)
        assert summary.failing_tests == ["TestB"]

    def test_unrecognised_output_reports_nothing_rather_than_zeros(self) -> None:
        """Claiming '0 failed' because a regex missed would be worse than silence."""
        summary = parse_test_output("Build succeeded. Have a nice day.")
        assert summary.detected is False
        assert summary.passed is None
        assert summary.failed is None

    def test_carries_the_detected_framework_through(self) -> None:
        summary = parse_test_output("nothing parseable", framework="npm")
        assert summary.framework == "npm"
        assert summary.detected is False
