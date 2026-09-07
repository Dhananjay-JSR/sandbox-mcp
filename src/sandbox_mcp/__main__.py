"""Command-line entry point.

    sandbox-mcp                      # stdio, for a local Claude Code / IDE client
    sandbox-mcp --transport http     # Streamable HTTP, for remote MCP clients
    sandbox-mcp --check              # verify Docker and print the active policy

stdio is the default because that is how a local editor attaches. Logging goes
to stderr in every mode -- stdout belongs to the protocol.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from .app import SandboxMCPApp
from .config import get_settings
from .errors import SandboxMCPError
from .logging import configure_logging, get_logger
from .server import create_server

log = get_logger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sandbox-mcp",
        description="MCP server providing disposable Docker sandboxes for AI coding agents.",
    )
    parser.add_argument(
        "--transport",
        choices=("stdio", "http"),
        default="stdio",
        help="stdio for a local client (default); http for Streamable HTTP.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="HTTP bind address.")
    parser.add_argument("--port", type=int, default=8000, help="HTTP port.")
    parser.add_argument("--path", default="/mcp", help="HTTP path for the MCP endpoint.")
    parser.add_argument(
        "--log-level", default=None, help="Override SANDBOX_MCP_LOG_LEVEL (INFO, DEBUG, ...)."
    )
    parser.add_argument(
        "--log-format",
        choices=("json", "console"),
        default=None,
        help="Log rendering. Defaults to JSON.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check the Docker connection and print the active configuration, then exit.",
    )
    return parser


async def _check() -> int:
    """Preflight, so a broken setup fails with an explanation rather than at
    the first tool call."""
    settings = get_settings()
    app = SandboxMCPApp.build(settings)
    report: dict[str, object] = {
        "docker_host": settings.resolved_docker_host(),
        "state_dir": str(settings.state_dir),
        "database": str(settings.database_path),
        "defaults": {
            "base_image": settings.default_base_image,
            "network_mode": settings.default_network_mode.value,
            "mount_strategy": settings.default_mount_strategy.value,
            "cpu_limit": settings.default_cpu_limit,
            "memory_limit": settings.default_memory_limit,
            "timeout_seconds": settings.default_timeout_seconds,
        },
        "image_allowlist": (
            "ANY (allow_any_image=true)"
            if settings.allow_any_image
            else settings.allowed_image_prefixes
        ),
    }
    try:
        report["docker"] = await app.backend.health_check()
        report["ok"] = True
        exit_code = 0
    except SandboxMCPError as exc:
        report["ok"] = False
        report["error"] = {"code": exc.code, "message": exc.message, "details": exc.details}
        exit_code = 1

    print(json.dumps(report, indent=2, default=str))
    return exit_code


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    settings = get_settings()
    configure_logging(
        level=arguments.log_level or settings.log_level,
        json_output=(arguments.log_format or ("json" if settings.log_json else "console"))
        == "json",
    )

    if arguments.check:
        return asyncio.run(_check())

    server = create_server()
    if arguments.transport == "stdio":
        server.run(transport="stdio", show_banner=False)
    else:
        server.run(
            transport="http",
            host=arguments.host,
            port=arguments.port,
            path=arguments.path,
            show_banner=False,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
