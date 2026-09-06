"""Logs must never carry a secret, and must never touch stdout."""

from __future__ import annotations

import io
import json
import sys

import pytest
import structlog

from sandbox_mcp.logging import REDACTED, configure_logging, get_logger, redact_secrets


@pytest.mark.parametrize(
    "key",
    [
        "password",
        "api_key",
        "API_KEY",
        "github_token",
        "aws_access_key",
        "authorization",
        "session_cookie",
        "private_key",
    ],
)
def test_credential_shaped_keys_are_redacted(key: str) -> None:
    assert redact_secrets(None, "", {key: "leak-me"})[key] == REDACTED


def test_ordinary_fields_pass_through() -> None:
    event = {"event": "job_done", "experiment_id": "exp_1", "exit_code": 0}
    assert redact_secrets(None, "", dict(event)) == event


def test_redaction_reaches_into_nested_maps() -> None:
    scrubbed = redact_secrets(
        None, "", {"config": {"host": "db.example.com", "password": "hunter2"}}
    )
    assert scrubbed["config"] == {"host": "db.example.com", "password": REDACTED}


def test_logs_go_to_stderr_not_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    """Under stdio transport, stdout is the MCP wire protocol."""
    configure_logging("INFO", json_output=True)
    get_logger("test").info("hello", experiment_id="exp_1")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "exp_1" in captured.err


def test_structured_events_are_json_with_the_expected_fields() -> None:
    stream = io.StringIO()
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            redact_secrets,
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.PrintLoggerFactory(file=stream),
    )
    structlog.get_logger("t").info(
        "command_completed", experiment_id="exp_1", job_id="job_2", exit_code=0, duration_ms=1432
    )
    record = json.loads(stream.getvalue())
    assert record["event"] == "command_completed"
    assert record["experiment_id"] == "exp_1"
    assert record["duration_ms"] == 1432
    assert "timestamp" in record
    configure_logging("CRITICAL", json_output=True)


def test_configure_logging_is_idempotent() -> None:
    for _ in range(3):
        configure_logging("INFO", json_output=False)
    assert sys.stderr is not None
    configure_logging("CRITICAL", json_output=True)
