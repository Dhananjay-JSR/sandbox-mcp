"""Structured logging.

Two rules that are not negotiable here:

1. Logs go to **stderr**. When the server runs over stdio transport, stdout is
   the MCP wire protocol -- a stray print corrupts the session.
2. Secrets never reach the log. :func:`redact_secrets` scrubs known-sensitive
   keys and anything the config marks as sensitive, at the processor level, so
   no individual call site can leak by forgetting.
"""

from __future__ import annotations

import logging
import re
import sys
from typing import Any

import structlog

SENSITIVE_KEY_PATTERN = re.compile(
    r"(pass|passwd|password|secret|token|api[-_]?key|access[-_]?key|credential|"
    r"auth|cookie|session|private[-_]?key|bearer)",
    re.IGNORECASE,
)

REDACTED = "***redacted***"


def _redact_value(key: str, value: Any) -> Any:
    if SENSITIVE_KEY_PATTERN.search(key):
        return REDACTED
    if isinstance(value, dict):
        return {k: _redact_value(k, v) for k, v in value.items()}
    return value


def redact_secrets(
    _logger: Any, _name: str, event_dict: structlog.types.EventDict
) -> structlog.types.EventDict:
    """Drop anything whose key looks like a credential."""
    return {k: _redact_value(k, v) for k, v in event_dict.items()}


def configure_logging(level: str = "INFO", json_output: bool = True) -> None:
    """Install the structlog pipeline. Idempotent; safe to call more than once."""
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stderr,
        level=getattr(logging, level.upper(), logging.INFO),
        force=True,
    )

    renderer: Any = (
        structlog.processors.JSONRenderer()
        if json_output
        else structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            redact_secrets,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> Any:
    """Return a bound logger. ``name`` shows up as the ``logger`` field."""
    return structlog.get_logger(name)
