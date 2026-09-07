# Sandbox MCP

AI coding agents are powerful because they can execute code. That is also their
biggest risk: experimentation can modify the developer's environment.

An agent that is genuinely useful has to be able to run `npm install`, apply a
migration, try a build, or run code it has not read. Every one of those actions,
performed on your machine, can install something you did not want, overwrite
work in progress, burn your CPU, read your credentials, or leave behind an
environment that no longer builds.

The usual answers are both bad. Deny the agent those tools and it can only
guess. Allow them and you are trusting a probabilistic system with your laptop.

Sandbox MCP is the third answer: give the agent somewhere real to work that
**is not your machine**.

> "Experiment with this project, but don't touch my actual environment."

---

## Without a sandbox

```
Claude Code
     |
     v
Host Machine
     |
     +-- npm install
     +-- scripts
     +-- migrations
     +-- file modifications
     +-- arbitrary execution
```

## With Sandbox MCP

```
Claude Code
     |
     v
FastMCP
     |
     v
Experiment Policy
     |
     v
Docker Sandbox
     |
     +-- execute
     +-- modify
     +-- test
     +-- build
     +-- experiment
     |
     v
Results
     |
     v
Claude Code
```

---

## What makes this different from a Docker MCP server

Existing Docker MCP implementations expose Docker operations to AI agents.
Sandbox MCP exposes an **experimentation abstraction**.

That is not a wording preference; it changes what the agent can do.

|                     | A Docker MCP server                        | Sandbox MCP                                             |
| ------------------- | ------------------------------------------ | ------------------------------------------------------- |
| Tools               | `docker_run`, `docker_exec`, `docker_ps`   | `create_experiment`, `run_tests`, `inspect_changes`      |
| The agent decides   | flags, mounts, network mode, privileges     | what it wants to find out                                |
| Isolation           | whatever the agent passed                   | enforced by policy before Docker is called               |
| Host filesystem     | reachable via `-v $(pwd):/app`              | not reachable; the project is snapshot-copied            |
| "What changed?"     | ask the agent to diff it                    | `inspect_changes`, against a baseline taken pre-container |
| Failure mode        | an agent that mounts `/` writable           | a request the policy engine rejects                      |

The agent never receives Docker API access. It states an intent; the server
decides how that intent is realised.

```
Claude Code
     |
     | MCP
     v
Sandbox MCP
     |
     +--> Policy / Security     <- validates, clamps, refuses
     +--> Experiment Manager    <- lifecycle, state machine
     +--> Job Manager           <- async execution, timeouts, cancellation
     +--> Result Collector      <- diffs, artifacts, reports
     |
     v
Docker Engine API  (via /var/run/docker.sock, SDK only -- no docker CLI)
     |
     v
Disposable Sandbox
     |
     v
Results -> Claude Code
```

**Docker** is the isolation mechanism.
**MCP** is the agent interface.
**Sandbox MCP** is the experimentation layer.

There is deliberately no orchestrator here. The agent already is one: Claude
Code plans, reads output, forms a hypothesis and retries better than any loop
this server could ship. Putting a second, dumber loop underneath it would only
compete with the caller. This project's job is the surface the agent drives —
and making that surface one it cannot hurt you with.

---

## The central abstraction: an experiment

```
Experiment:      exp_2859b8eb8656
Project:         payments-service
Base image:      node:22-slim
Network:         disabled
CPU:             2 cores
Memory:          1GB
Timeout:         300s
Status:          READY

Commands:        1. npm install --no-audit --no-fund   (exit 1, EBADENGINE)
                 2. npm install --no-audit --no-fund   (exit 0)
                 3. npm test                           (37 passed, 3 failed)
                 4. npm test                           (40 passed, 0 failed)

Changes:         3 files modified, +9 -4
```

Experiments move through an explicit, validated state machine. An illegal
transition is an error, not a silently corrupted record — which matters,
because `DESTROYED` is what tells the server a container no longer exists.

```
CREATING ──► READY ──► RUNNING ──┬─► COMPLETED ──► DESTROYED
    │          │         │       ├─► FAILED    ──► DESTROYED
    │          │         │       ├─► TIMEOUT   ──► DESTROYED
    │          │         │       └─► CANCELLED ──► DESTROYED
    └──► FAILED          └──► READY
```

---

## Security model

The Docker socket is a highly privileged interface — access to it is
effectively root on the host. It is held by the server and by nothing else.
The agent talks to the server; the server talks to Docker.

### Filesystem

The project is **copied**, not mounted:

```
HOST PROJECT
     |
     | snapshot (read-only pass, filtered)
     v
SANDBOX PROJECT COPY  ── also the baseline every diff is computed against
     |
     v
DOCKER CONTAINER
```

- Default strategy is `COPY_TO_SANDBOX`. `READ_ONLY_BIND_MOUNT` exists for
  large repositories you only need to read. **Writable host bind mounts are
  not implemented** — there is no configuration that produces one.
- `.env` files, `*.pem`, `*.key`, `id_rsa*`, `.netrc`, `.ssh/`, `.aws/`,
  `.gnupg/`, `.kube/` and similar are **never** copied, regardless of
  configuration. Build output (`node_modules`, `dist`, `target`, …) is skipped
  by default and that list is configurable.
- Project paths are resolved before they are checked, so a symlink cannot
  launder a denied location. The home directory and filesystem root are
  refused outright.
- Paths inside the sandbox are confined to the workspace: `../../etc/passwd`
  and `/etc/shadow` are rejected by every tool that takes a path.

One deliberate exception worth knowing: `.npmrc` **is** copied, because the
real behaviour of `npm install` often depends on it. If yours holds an
`_authToken`, add it to `SANDBOX_MCP_SNAPSHOT_EXCLUDES`.

### Network

Disabled by default. `network_mode` is an explicit decision at creation time:

| Mode         | Behaviour                                                                    |
| ------------ | ---------------------------------------------------------------------------- |
| `none`       | No interfaces at all. **The default.**                                        |
| `restricted` | A private bridge network of its own — egress works, no reach to other sandboxes |
| `enabled`    | The daemon's default bridge. Full egress.                                     |

`restricted` prevents lateral movement between sandboxes. It does not firewall
egress; per-experiment network allowlists are listed under future extensions.

### Environment variables

The host environment is never inherited. Variables cross the boundary only when
named: `"CI"` forwards the host's value, `"NODE_ENV=test"` injects a literal.

Credential-shaped names (`*_TOKEN`, `*_SECRET`, `*API_KEY*`, `AWS_*`,
`DATABASE_URL`, …) are refused **even when explicitly allowlisted**, because an
agent asking for `AWS_SECRET_ACCESS_KEY` inside a throwaway container is never
right. Set `SANDBOX_MCP_STRICT_ENV_DENYLIST=false` if you disagree.

Values are never written to the database or the logs — only names.

### Container privileges

- `privileged=False`, always. There is no setting that changes it.
- `cap_drop: ALL`, then a small set added back: `CHOWN`, `DAC_OVERRIDE`,
  `FOWNER`, `FSETID`, `SETUID`, `SETGID`, `KILL`. That is what package managers
  actually need (they chown caches and drop privileges for lifecycle scripts).
  It excludes `NET_RAW`, `MKNOD`, `SYS_ADMIN`, `SYS_CHROOT`, `SETPCAP` and
  `SETFCAP` — the ones that matter for escape.
- `no-new-privileges:true`.
- **The Docker socket is never mounted into a sandbox.** There is no code path
  that does it, and an integration test asserts its absence.
- `SANDBOX_MCP_SANDBOX_USER` runs containers as a non-root user where the
  toolchain tolerates it.

### Resource limits

Every sandbox is capped, and a request may only ask for *less* than the
configured ceiling. Asking for more is clamped, and the clamp is returned to the
agent as a warning rather than failing silently.

| Limit          | Default | Ceiling | Enforced by                             |
| -------------- | ------- | ------- | --------------------------------------- |
| CPU            | 2 cores | 4 cores | `NanoCpus`                              |
| Memory         | 2GB     | 8GB     | `Memory` **and** `MemorySwap` (no swap escape) |
| Processes      | 512     | 2048    | `PidsLimit`                             |
| Wall clock     | 120s    | 1800s   | the job manager, which kills the process |
| `/tmp`         | 512MB   | —       | `tmpfs`                                 |
| Captured output| 1MB/stream | —    | truncated, and flagged as truncated      |

---

## Installation

Requires **Python 3.12+** and a running Docker engine (Docker Desktop,
OrbStack, Colima or Rancher Desktop).

```bash
git clone https://github.com/Dhananjay-JSR/sandbox-mcp.git
cd sandbox-mcp
uv sync                     # exact versions from uv.lock
```

Confirm the server can reach Docker before wiring it to anything:

```bash
uv run sandbox-mcp --check
```

```json
{
  "docker_host": "unix:///var/run/docker.sock",
  "state_dir": "/Users/you/.sandbox-mcp",
  "defaults": { "base_image": "debian:bookworm-slim", "network_mode": "none", ... },
  "docker": { "server_version": "29.4.0", "api_version": "1.54" },
  "ok": true
}
```

`ok: false` with `DOCKER_UNAVAILABLE` means the daemon is not running or the
socket is somewhere unusual. The server probes `/var/run/docker.sock`,
`~/.docker/run/docker.sock`, `~/.orbstack/run/docker.sock`,
`~/.colima/default/docker.sock` and `~/.rd/docker.sock` in that order — the
Docker CLI's *context* is not consulted, so set `DOCKER_HOST` explicitly if
yours is elsewhere.

Optional extras:

```bash
uv sync --extra dev     # pytest, ruff, mypy
```

---

## Connecting Claude Code

### Local (stdio) — recommended

```bash
claude mcp add sandbox -- uv --directory /absolute/path/to/sandbox-mcp run sandbox-mcp
```

Or, if you installed it into an environment already on your `PATH`:

```bash
claude mcp add sandbox -- sandbox-mcp
```

Then, inside Claude Code:

```
/mcp                       # should list "sandbox" as connected
```

Equivalent `.mcp.json`, if you prefer to commit the configuration:

```json
{
  "mcpServers": {
    "sandbox": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/sandbox-mcp", "run", "sandbox-mcp"],
      "env": {
        "SANDBOX_MCP_LOG_LEVEL": "INFO"
      }
    }
  }
}
```

### Remote (Streamable HTTP)

```bash
uv run sandbox-mcp --transport http --host 127.0.0.1 --port 8000
claude mcp add --transport http sandbox http://127.0.0.1:8000/mcp
```

Or run the server itself in a container:

```bash
docker compose up -d
claude mcp add --transport http sandbox http://127.0.0.1:8000/mcp
```

The compose file mounts the Docker socket **into the server** — read the
warning at the top of it first, and do not expose port 8000 beyond localhost
without authentication in front of it.

```
Claude Code
    |
    v
Sandbox MCP        (holds the socket)
    |
    v
Docker Desktop / OrbStack
    |
    v
Sandbox            (no socket, no host filesystem, no network)
```

---

## Tools

| Tool                       | Purpose                                                          |
| -------------------------- | ---------------------------------------------------------------- |
| `create_experiment`        | Create a disposable environment and copy a project into it        |
| `execute_experiment`       | Run a command inside the sandbox (sync or background)             |
| `run_tests`                | Detect the test runner, run it, parse the results                 |
| `read_sandbox_file`        | Read a file from the sandbox to investigate a failure             |
| `write_sandbox_file`       | Apply a candidate fix without shell-quoting hazards               |
| `inspect_changes`          | What changed, against the pre-container baseline                  |
| `collect_artifacts`        | Lift build output, reports or logs out before teardown            |
| `get_experiment`           | Full state and a summary of what happened                         |
| `list_experiments`         | Find earlier experiments, including ones left running             |
| `get_job_status`           | Poll a background command                                         |
| `get_job_result`           | Fetch a completed background result                               |
| `cancel_job`               | Stop a running command and kill its process                       |
| `destroy_experiment`       | Tear down; idempotent; returns the final report                   |
| `compare_experiments`      | Rank several approaches and recommend one                         |
| `check_sandbox_runtime`    | Confirm Docker is reachable and show the active policy            |

Notably absent: `docker_run`, `docker_exec`, `docker_ps`, `docker_build`,
`docker_pull`. A test asserts that no tool name begins with `docker_`.

### Resources

```
sandbox://experiments                              all experiments
sandbox://experiments/{experiment_id}              metadata + state history
sandbox://experiments/{experiment_id}/logs         every command, with output
sandbox://experiments/{experiment_id}/diff         unified diff
sandbox://experiments/{experiment_id}/artifacts    collected files
```

---

## The primary demo: Node 20 → Node 22

`examples/node-upgrade/` is a real, dependency-free Node library pinned to
Node 20. It fails on Node 22 for two independent and entirely realistic
reasons:

1. `package.json` declares `engines.node: ">=18 <21"` and `.npmrc` sets
   `engine-strict=true`, so `npm install` fails with `EBADENGINE` before a
   single test runs.
2. `src/crypto.js` calls `crypto.createCipher`, deprecated since Node 10 and
   **removed in Node 22.0.0**.

Ask Claude Code:

> Determine whether `examples/node-upgrade` can be upgraded from Node 20 to
> Node 22. You may install dependencies, modify files, run tests and experiment
> freely, but **do not modify my actual working tree**.

What happens — every number below is from an actual run, and the whole thing
works with the network disabled:

```
1.  create_experiment      node:22-slim, network none      -> exp_2859b8eb8656, 13 files
2.  execute_experiment     npm install                     -> exit 1, EBADENGINE
3.  read_sandbox_file      package.json                    -> engines.node ">=18 <21"
4.  write_sandbox_file     package.json                    -> engines.node ">=18"
5.  execute_experiment     npm install                     -> exit 0
6.  run_tests              (auto-detected: npm)            -> 37 passed, 3 failed
                                                              seal produces hex output
                                                              seal and open round-trip
                                                              seal round-trips unicode
7.  read_sandbox_file      src/crypto.js                   -> crypto.createCipher(...)
8.  write_sandbox_file     src/crypto.js                   -> createCipheriv + scrypt key + IV
9.  run_tests                                              -> 40 passed, 0 failed
10. inspect_changes                                        -> 3 modified, +9 -4
11. collect_artifacts      src/crypto.js                   -> kept for the developer
12. destroy_experiment                                     -> report, sandbox removed
```

```
Experiment:         Node 20 → Node 22
Result:             COMPATIBLE (with 2 source changes)
Tests:              40 passed, 0 failed
Changes:            3 files modified (+9 -4)
Host working tree:  UNCHANGED
Sandbox:            DESTROYED
```

That last pair of lines is the product. This exact sequence runs as a test:

```bash
uv run pytest tests/integration/test_node_upgrade_demo.py -m integration
```

### Other things this makes possible

- **Dependency upgrade** — "try upgrading React to the latest compatible
  version; don't modify my working tree." Needs
  `network_mode="restricted"` to reach the registry.
- **Database migration** — create an experiment on `postgres:16`, run the
  migration, report whether it applied, destroy it.
- **Build debugging** — "work out why this build fails; experiment freely."
- **Multi-approach debugging** — `examples/failing-project/` is a Python
  project whose money arithmetic is done in floats; 3 of its 14 tests fail.
  Three fixes are plausible (`round()`, `Decimal`, truncation) and they are not
  equivalent. Run each in its own experiment, then call `compare_experiments`,
  which ranks them on failures, exit code, change size and duration, and
  recommends one.

---

## Why there is no orchestration layer

An earlier cut of this server shipped a LangGraph loop — plan, test, diagnose,
fix, retest — behind a `run_autonomous_experiment` tool. It was removed, on
purpose.

The caller is already an agent. Claude Code reads a failing test, forms a
hypothesis, edits a file and retries; it does that better than a graph of
hardcoded repair strategies ever will, because it has judgement and the
strategies had a lookup table. Shipping a second, weaker loop underneath a
strong one does not add a capability — it competes with the caller for the
same decision, and it is the one more likely to be wrong.

What is left is the part the agent genuinely cannot do for itself: the
isolation boundary, the policy that enforces it, the state machine, the
baseline diff, and the audit trail. Those are the product.

Concretely, the loop lives in the transcript instead of in the server:

```
create_experiment  ─┐
execute_experiment  │
run_tests           │   Claude Code decides what to do next at each step,
read_sandbox_file   ├─  with the full output in front of it. The server
write_sandbox_file  │   just refuses anything that would reach the host.
run_tests           │
inspect_changes    ─┘
destroy_experiment
```

That is the primary demo, and it is what the integration suite runs.

## Observability

Every operation emits a structured JSON event to **stderr** (stdout carries the
MCP protocol under stdio transport):

```json
{"event": "command_completed", "experiment_id": "exp_2859b8eb8656",
 "job_id": "job_4f1c8a2b90de", "operation": "job.run", "status": "COMPLETED",
 "exit_code": 0, "duration_ms": 1432, "timestamp": "2026-09-07T10:29:41Z"}
```

Credential-shaped keys are scrubbed at the processor level, so no individual
call site can leak by forgetting.

Everything needed to answer *"what exactly did the agent do?"* is persisted in
SQLite (`~/.sandbox-mcp/sandbox.db`): experiments, jobs with their commands,
exit codes and captured output, state transitions with timestamps and reasons,
artifacts with checksums, the pre-container baseline, and the change statistics
— which are written **before** teardown, so a destroyed experiment is still
comparable against a live one.

The database stores no secrets: environment variable names, never values.

---

## Project layout

```
src/sandbox_mcp/
├── server.py            MCP tools and resources — the agent-facing surface
├── app.py               composition root; the only place the graph is wired
├── config.py            every tunable, in one auditable place
├── models.py            domain models (experiments, jobs, changes, reports)
├── errors.py            structured errors; no traceback ever reaches a client
├── logging.py           structured logging to stderr, with redaction
├── sandbox/
│   ├── interface.py     SandboxBackend — the seam other runtimes target
│   └── docker.py        Docker Engine API via the SDK; all hardening lives here
├── experiments/
│   ├── manager.py       the domain core
│   ├── state.py         the validated state machine
│   ├── repository.py    ExperimentRepository + the SQLite implementation
│   ├── changes.py       diffing the sandbox against its baseline
│   └── testing.py       test-runner detection and output parsing
├── execution/
│   ├── manager.py       job lifecycle: timeouts, cancellation, persistence
│   ├── jobs.py          live task registry and concurrency limits
│   └── executor.py      JobExecutor interface + the sandbox implementation
├── artifacts/manager.py pulling files out before teardown
└── security/
    ├── policy.py        validates and clamps every request
    ├── filesystem.py    path confinement and snapshotting
    └── resources.py     limit parsing
```

`SandboxBackend`, `ExperimentRepository` and `JobExecutor` are abstract. Docker
implements the first; the MCP tool surface does not know it exists. Supporting
Firecracker, Kubernetes Jobs or remote workers means one new implementation and
no change to the agent-facing API — the in-memory `FakeSandboxBackend` the unit
tests run against is the proof that the seam is real.

Three deviations from a literal reading of the brief, all deliberate:
`experiments/changes.py` and `experiments/testing.py` exist as focused modules
rather than being folded into `manager.py`; `commands`/`results` are columns on
the `jobs` table rather than separate tables, since a job *is* a command and its
result; and there is no `orchestration/` package, for the reason given above.

---

## Development

```bash
uv sync --extra dev

uv run pytest -m "not integration"     # 225 unit tests, no Docker needed
uv run pytest -m integration           # 23 tests against a live daemon
uv run pytest                          # everything

uv run ruff check . && uv run ruff format --check .
uv run mypy
```

The unit suite runs against `FakeSandboxBackend` and needs no daemon. The
integration suite is the one that proves the product claim: the nine-step
isolation checklist, the host tree unchanged byte-for-byte, the network
genuinely off, secrets genuinely absent, the memory cap actually biting,
timeouts killing the process, cancellation killing the process, hardening flags
present on the real container, orphan containers swept at startup, and the full
Node 20 → 22 demo.

---

## MVP boundaries

Not built, on purpose: a web dashboard, authentication, cloud infrastructure,
Kubernetes support, distributed scheduling, billing, multi-tenant production
infrastructure. This is a polished local-first MVP.

Known limits, stated plainly:

- Killing a timed-out command signals the command's own process. Descendants it
  spawned may survive until the sandbox is destroyed; `pids_limit` bounds the
  damage in the meantime.
- `restricted` networking isolates sandboxes from each other but does not
  filter egress.
- Change detection covers regular files. Symlinks are copied but not tracked in
  diffs.
- Per-sandbox disk quotas need a storage driver that supports them (overlay2 on
  XFS with `pquota`); `/tmp` is capped via tmpfs, the workspace is not.

## Future extensions

1. Remote sandbox execution
2. Firecracker isolation
3. Kubernetes sandbox workers
4. Persistent base-image cache
5. Snapshot / restore
6. Parallel experiments
7. Experiment replay
8. Resource usage analytics
9. Network allowlists
10. Human approval before applying changes
11. Agent experiment benchmarking

---

## Licence

MIT.
