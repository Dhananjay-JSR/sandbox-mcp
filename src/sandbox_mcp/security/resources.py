"""Resource limit parsing and clamping.

A request may always ask for *less* than the configured ceiling. Asking for
more is not an error -- it is clamped, and the clamp is reported back as a
warning so the agent knows what it actually got.
"""

from __future__ import annotations

import re

from ..errors import PolicyViolationError

_MEMORY_PATTERN = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([a-zA-Z]*)\s*$")

# Decimal and binary suffixes both accepted; "GB" is treated as 1024^3 because
# that is what every developer means when they type it into a container config.
_MEMORY_UNITS: dict[str, int] = {
    "": 1,
    "b": 1,
    "k": 1024,
    "kb": 1024,
    "kib": 1024,
    "m": 1024**2,
    "mb": 1024**2,
    "mib": 1024**2,
    "g": 1024**3,
    "gb": 1024**3,
    "gib": 1024**3,
    "t": 1024**4,
    "tb": 1024**4,
    "tib": 1024**4,
}

# Docker refuses anything below this.
MIN_MEMORY_BYTES = 6 * 1024 * 1024


def parse_memory(value: str | int) -> int:
    """Turn ``'2GB'`` / ``'512m'`` / ``2147483648`` into a byte count."""
    if isinstance(value, int):
        parsed = value
    else:
        match = _MEMORY_PATTERN.match(value)
        if not match:
            raise PolicyViolationError(
                f"Cannot parse memory limit {value!r}. Use forms like '512MB', '2GB', '1GiB'.",
                field="memory_limit",
                value=str(value),
            )
        amount, unit = match.groups()
        multiplier = _MEMORY_UNITS.get(unit.lower())
        if multiplier is None:
            raise PolicyViolationError(
                f"Unknown memory unit {unit!r} in {value!r}.",
                field="memory_limit",
                value=str(value),
            )
        parsed = int(float(amount) * multiplier)

    if parsed < MIN_MEMORY_BYTES:
        raise PolicyViolationError(
            f"Memory limit {value!r} is below Docker's "
            f"{MIN_MEMORY_BYTES // (1024 * 1024)}MB floor.",
            field="memory_limit",
            value=str(value),
        )
    return parsed


def format_memory(num_bytes: int) -> str:
    """Inverse of :func:`parse_memory`, for display."""
    for unit, size in (("GB", 1024**3), ("MB", 1024**2), ("KB", 1024)):
        if num_bytes >= size:
            amount = num_bytes / size
            rendered = f"{amount:.2f}".rstrip("0").rstrip(".")
            return f"{rendered}{unit}"
    return f"{num_bytes}B"
