from __future__ import annotations

import pytest

from sandbox_mcp.errors import PolicyViolationError
from sandbox_mcp.security.resources import format_memory, parse_memory


@pytest.mark.parametrize(
    "given,expected",
    [
        ("2GB", 2 * 1024**3),
        ("2gb", 2 * 1024**3),
        ("1GiB", 1024**3),
        ("512MB", 512 * 1024**2),
        ("512m", 512 * 1024**2),
        ("1.5GB", int(1.5 * 1024**3)),
        (1024**3, 1024**3),
    ],
)
def test_parses_the_forms_people_actually_type(given: str | int, expected: int) -> None:
    assert parse_memory(given) == expected


@pytest.mark.parametrize("given", ["", "lots", "2 parsecs", "GB", "-1GB"])
def test_rejects_nonsense(given: str) -> None:
    with pytest.raises(PolicyViolationError):
        parse_memory(given)


def test_rejects_below_dockers_floor() -> None:
    """Docker itself refuses under 6MB; failing here gives a better message."""
    with pytest.raises(PolicyViolationError):
        parse_memory("1MB")


@pytest.mark.parametrize(
    "given,expected",
    [(2 * 1024**3, "2GB"), (512 * 1024**2, "512MB"), (1536 * 1024**2, "1.5GB"), (1024, "1KB")],
)
def test_formats_for_humans(given: int, expected: str) -> None:
    assert format_memory(given) == expected


@pytest.mark.parametrize("value", ["2GB", "512MB", "1.5GB", "8GB"])
def test_round_trips(value: str) -> None:
    assert format_memory(parse_memory(value)) == value
