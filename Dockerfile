# Sandbox MCP server image.
#
# This runs the *trusted* server, not a sandbox. It needs the Docker socket in
# order to create sandboxes, which makes it a privileged component: run it only
# where you would run the Docker CLI itself. The socket is never passed into an
# experiment container -- see docker-compose.yml and the README.

FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies resolve from the lockfile in their own layer, so application
# edits do not invalidate them.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project --no-dev

COPY src/ src/
RUN uv sync --frozen --no-dev


FROM python:3.12-slim AS runtime

# A non-root user for the server process itself. It still needs access to the
# mounted Docker socket, so grant that via the socket's group at run time
# (docker-compose passes `group_add`).
RUN useradd --create-home --uid 10001 sandbox

WORKDIR /app
COPY --from=builder --chown=sandbox:sandbox /app/.venv /app/.venv
COPY --from=builder --chown=sandbox:sandbox /app/src /app/src

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SANDBOX_MCP_STATE_DIR=/data

RUN mkdir -p /data && chown sandbox:sandbox /data
VOLUME ["/data"]
USER sandbox

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD ["sandbox-mcp", "--check"]

# Streamable HTTP by default: a containerised server is being reached over the
# network, not over a local stdio pipe.
ENTRYPOINT ["sandbox-mcp"]
CMD ["--transport", "http", "--host", "0.0.0.0", "--port", "8000"]
