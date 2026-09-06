"""Test-suite detection and output parsing.

Two jobs an agent would otherwise do badly by hand:

* Work out *how* this project runs its tests, by looking at what is actually
  in the sandbox rather than guessing from the base image.
* Turn a wall of runner output into counts it can reason about.

Parsing is strictly best effort. When nothing matches, ``detected`` stays False
and the caller falls back to the exit code -- reporting "0 failed" because a
regex missed would be worse than reporting nothing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..models import TestSummary


@dataclass(frozen=True, slots=True)
class Framework:
    name: str
    command: str
    marker_files: tuple[str, ...]


# Ordered by specificity: the first framework whose markers are present wins.
FRAMEWORKS: tuple[Framework, ...] = (
    Framework("cargo", "cargo test", ("Cargo.toml",)),
    Framework("go", "go test ./...", ("go.mod",)),
    Framework("npm", "npm test", ("package.json",)),
    Framework("pytest", "pytest -q", ("pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml")),
    Framework("make", "make test", ("Makefile",)),
)

DETECTION_SCRIPT = 'for f in {names}; do [ -e "$f" ] && printf \'%s\\n\' "$f"; done; exit 0'


def detection_script() -> str:
    names = " ".join(
        sorted({marker for framework in FRAMEWORKS for marker in framework.marker_files})
    )
    return DETECTION_SCRIPT.format(names=names)


def choose_framework(present_files: set[str]) -> Framework | None:
    for framework in FRAMEWORKS:
        if any(marker in present_files for marker in framework.marker_files):
            return framework
    return None


# --- output parsing -------------------------------------------------------

# node:test / TAP
_TAP_PASS = re.compile(r"^# pass (\d+)", re.MULTILINE)
_TAP_FAIL = re.compile(r"^# fail (\d+)", re.MULTILINE)
_TAP_SKIP = re.compile(r"^# skipped? (\d+)", re.MULTILINE)
_TAP_TOTAL = re.compile(r"^# tests (\d+)", re.MULTILINE)
_TAP_NOT_OK = re.compile(r"^not ok \d+ - (.+?)\s*$", re.MULTILINE)

# jest / vitest
_JEST = re.compile(
    r"^\s*Tests:\s+(?:(\d+) failed,\s*)?(?:(\d+) skipped,\s*)?(?:(\d+) todo,\s*)?"
    r"(?:(\d+) passed,\s*)?(\d+) total",
    re.MULTILINE,
)

# mocha
_MOCHA_PASS = re.compile(r"^\s*(\d+) passing", re.MULTILINE)
_MOCHA_FAIL = re.compile(r"^\s*(\d+) failing", re.MULTILINE)
_MOCHA_PENDING = re.compile(r"^\s*(\d+) pending", re.MULTILINE)

# pytest summary line
_PYTEST_PART = re.compile(r"(\d+) (passed|failed|skipped|error|errors|xfailed|xpassed)")
_PYTEST_SUMMARY = re.compile(r"^=+ .*\b\d+ (?:passed|failed|error).*=+$", re.MULTILINE)
_PYTEST_FAILED = re.compile(r"^FAILED (\S+)", re.MULTILINE)

# cargo
_CARGO = re.compile(
    r"test result: (?:ok|FAILED)\. (\d+) passed; (\d+) failed; (\d+) ignored", re.MULTILINE
)

# go
_GO_FAIL = re.compile(r"^\s*--- FAIL: (\S+)", re.MULTILINE)
_GO_PASS = re.compile(r"^\s*--- PASS: (\S+)", re.MULTILINE)


def parse_test_output(output: str, framework: str | None = None) -> TestSummary:
    """Extract counts from combined stdout+stderr. Order matters: the more
    specific formats are tried before the generic ones."""
    for parser in (
        _parse_tap,
        _parse_jest,
        _parse_pytest,
        _parse_cargo,
        _parse_go,
        _parse_mocha,
    ):
        summary = parser(output)
        if summary is not None:
            summary.framework = summary.framework or framework
            return summary
    return TestSummary(framework=framework, detected=False)


def _int(match: re.Match[str] | None, group: int = 1) -> int | None:
    return int(match.group(group)) if match else None


def _parse_tap(output: str) -> TestSummary | None:
    passed = _int(_TAP_PASS.search(output))
    failed = _int(_TAP_FAIL.search(output))
    if passed is None and failed is None:
        return None
    total = _int(_TAP_TOTAL.search(output))
    return TestSummary(
        framework="node:test",
        detected=True,
        total=total if total is not None else (passed or 0) + (failed or 0),
        passed=passed or 0,
        failed=failed or 0,
        skipped=_int(_TAP_SKIP.search(output)),
        failing_tests=_TAP_NOT_OK.findall(output)[:25],
    )


def _parse_jest(output: str) -> TestSummary | None:
    match = _JEST.search(output)
    if not match:
        return None
    failed, skipped, todo, passed, total = (int(g) if g else 0 for g in match.groups())
    return TestSummary(
        framework="jest",
        detected=True,
        total=total,
        passed=passed,
        failed=failed,
        skipped=skipped + todo,
        failing_tests=[],
    )


def _parse_pytest(output: str) -> TestSummary | None:
    if not _PYTEST_SUMMARY.search(output):
        return None
    counts = {kind: int(value) for value, kind in _PYTEST_PART.findall(output)}
    passed = counts.get("passed", 0)
    failed = counts.get("failed", 0) + counts.get("error", 0) + counts.get("errors", 0)
    skipped = counts.get("skipped", 0)
    return TestSummary(
        framework="pytest",
        detected=True,
        total=passed + failed + skipped,
        passed=passed,
        failed=failed,
        skipped=skipped,
        failing_tests=_PYTEST_FAILED.findall(output)[:25],
    )


def _parse_cargo(output: str) -> TestSummary | None:
    matches = _CARGO.findall(output)
    if not matches:
        return None
    passed = sum(int(m[0]) for m in matches)
    failed = sum(int(m[1]) for m in matches)
    ignored = sum(int(m[2]) for m in matches)
    return TestSummary(
        framework="cargo",
        detected=True,
        total=passed + failed + ignored,
        passed=passed,
        failed=failed,
        skipped=ignored,
    )


def _parse_go(output: str) -> TestSummary | None:
    failures = _GO_FAIL.findall(output)
    passes = _GO_PASS.findall(output)
    if not failures and not passes:
        return None
    return TestSummary(
        framework="go",
        detected=True,
        total=len(failures) + len(passes),
        passed=len(passes),
        failed=len(failures),
        skipped=0,
        failing_tests=failures[:25],
    )


def _parse_mocha(output: str) -> TestSummary | None:
    passed = _int(_MOCHA_PASS.search(output))
    failed = _int(_MOCHA_FAIL.search(output))
    if passed is None and failed is None:
        return None
    pending = _int(_MOCHA_PENDING.search(output)) or 0
    return TestSummary(
        framework="mocha",
        detected=True,
        total=(passed or 0) + (failed or 0) + pending,
        passed=passed or 0,
        failed=failed or 0,
        skipped=pending,
    )
