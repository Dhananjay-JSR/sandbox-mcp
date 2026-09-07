"""The MCP surface, exercised the way a client sees it."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from sandbox_mcp.app import SandboxMCPApp
from sandbox_mcp.config import Settings
from sandbox_mcp.experiments.repository import SQLiteRepository
from sandbox_mcp.server import create_server
from tests.fakes import FakeSandboxBackend, outcome

EXPECTED_TOOLS = {
    "create_experiment",
    "execute_experiment",
    "run_tests",
    "read_sandbox_file",
    "write_sandbox_file",
    "inspect_changes",
    "collect_artifacts",
    "get_experiment",
    "list_experiments",
    "get_job_status",
    "get_job_result",
    "cancel_job",
    "destroy_experiment",
    "compare_experiments",
    "check_sandbox_runtime",
}


@pytest.fixture
async def client(settings: Settings, backend: FakeSandboxBackend) -> AsyncIterator[Client]:
    server = create_server(
        SandboxMCPApp.build(
            settings=settings, backend=backend, repository=SQLiteRepository(settings.database_path)
        )
    )
    async with Client(server) as connected:
        yield connected


def payload(result: Any) -> Any:
    data = result.structured_content
    if isinstance(data, dict) and set(data) == {"result"}:
        return data["result"]
    return data


class TestSurface:
    async def test_exposes_the_expected_tools(self, client: Client) -> None:
        assert {tool.name for tool in await client.list_tools()} == EXPECTED_TOOLS

    async def test_exposes_no_docker_verbs(self, client: Client) -> None:
        """The abstraction is the product. A docker_* tool would defeat it."""
        names = {tool.name for tool in await client.list_tools()}
        assert not any(name.startswith(("docker_", "container_", "image_")) for name in names)

    async def test_every_tool_tells_the_model_when_to_use_it(self, client: Client) -> None:
        for tool in await client.list_tools():
            assert tool.description
            assert len(tool.description) > 120, tool.name

    async def test_exposes_the_experiment_resources(self, client: Client) -> None:
        templates = {template.uri_template for template in await client.list_resource_templates()}
        assert templates == {
            "sandbox://experiments/{experiment_id}",
            "sandbox://experiments/{experiment_id}/logs",
            "sandbox://experiments/{experiment_id}/diff",
            "sandbox://experiments/{experiment_id}/artifacts",
        }
        assert {str(r.uri) for r in await client.list_resources()} == {"sandbox://experiments"}


class TestLifecycleThroughTheClient:
    async def test_full_round_trip(self, client: Client, project: Path) -> None:
        created = payload(
            await client.call_tool(
                "create_experiment",
                {
                    "project_path": str(project),
                    "base_image": "node:22-slim",
                    "objective": "upgrade check",
                },
            )
        )
        experiment_id = created["experiment_id"]
        assert created["status"] == "READY"
        assert created["network_mode"] == "none"

        executed = payload(
            await client.call_tool(
                "execute_experiment", {"experiment_id": experiment_id, "command": "echo hi"}
            )
        )
        assert executed["exit_code"] == 0

        await client.call_tool(
            "write_sandbox_file",
            {"experiment_id": experiment_id, "path": "src/new.js", "content": "// added\n"},
        )
        read_back = payload(
            await client.call_tool(
                "read_sandbox_file", {"experiment_id": experiment_id, "path": "src/new.js"}
            )
        )
        assert read_back == "// added\n"

        changes = payload(
            await client.call_tool("inspect_changes", {"experiment_id": experiment_id})
        )
        assert changes["files_created"] == ["src/new.js"]

        destroyed = payload(
            await client.call_tool("destroy_experiment", {"experiment_id": experiment_id})
        )
        assert destroyed["status"] == "DESTROYED"
        assert destroyed["report"]["host_working_tree"] == "UNCHANGED"

    async def test_run_tests_reports_a_parsed_summary(
        self, client: Client, backend: FakeSandboxBackend, project: Path
    ) -> None:
        backend.responses["npm test"] = outcome(
            1, "# tests 40\n# pass 37\n# fail 3\nnot ok 1 - seal round-trips\n"
        )
        created = payload(
            await client.call_tool("create_experiment", {"project_path": str(project)})
        )
        result = payload(
            await client.call_tool(
                "run_tests", {"experiment_id": created["experiment_id"], "command": "npm test"}
            )
        )
        assert result["exit_code"] == 1
        assert result["test_summary"]["failed"] == 3
        assert result["test_summary"]["failing_tests"] == ["seal round-trips"]

    async def test_background_job_polling(
        self, client: Client, backend: FakeSandboxBackend
    ) -> None:
        backend.command_delay = 0.05
        created = payload(await client.call_tool("create_experiment", {}))
        submitted = payload(
            await client.call_tool(
                "execute_experiment",
                {
                    "experiment_id": created["experiment_id"],
                    "command": "slow",
                    "background": True,
                },
            )
        )
        status = payload(await client.call_tool("get_job_status", {"job_id": submitted["job_id"]}))
        assert status["status"] in {"PENDING", "RUNNING"}

        finished = payload(
            await client.call_tool("get_job_result", {"job_id": submitted["job_id"]})
        )
        assert finished["status"] == "COMPLETED"

    async def test_cancel_job(self, client: Client, backend: FakeSandboxBackend) -> None:
        backend.command_delay = 5
        created = payload(await client.call_tool("create_experiment", {}))
        submitted = payload(
            await client.call_tool(
                "execute_experiment",
                {
                    "experiment_id": created["experiment_id"],
                    "command": "sleep 999",
                    "background": True,
                },
            )
        )
        cancelled = payload(await client.call_tool("cancel_job", {"job_id": submitted["job_id"]}))
        assert cancelled["status"] == "CANCELLED"

    async def test_collect_artifacts(
        self, client: Client, backend: FakeSandboxBackend, project: Path
    ) -> None:
        backend.responses["for f in"] = outcome(0, "package.json\n")
        created = payload(
            await client.call_tool("create_experiment", {"project_path": str(project)})
        )
        result = payload(
            await client.call_tool(
                "collect_artifacts",
                {"experiment_id": created["experiment_id"], "patterns": ["package.json"]},
            )
        )
        assert len(result["artifacts"]) == 1
        assert Path(result["artifacts"][0]["host_path"]).read_bytes()

    async def test_compare_experiments(self, client: Client, backend: FakeSandboxBackend) -> None:
        first = payload(await client.call_tool("create_experiment", {"base_image": "node:20-slim"}))
        second = payload(
            await client.call_tool("create_experiment", {"base_image": "node:22-slim"})
        )
        backend.responses["npm test"] = outcome(0, "# tests 40\n# pass 40\n# fail 0\n")
        await client.call_tool(
            "run_tests", {"experiment_id": first["experiment_id"], "command": "npm test"}
        )
        backend.responses["npm test"] = outcome(1, "# tests 40\n# pass 37\n# fail 3\n")
        await client.call_tool(
            "run_tests", {"experiment_id": second["experiment_id"], "command": "npm test"}
        )

        comparison = payload(
            await client.call_tool(
                "compare_experiments",
                {"experiment_ids": [first["experiment_id"], second["experiment_id"]]},
            )
        )
        assert comparison["dimensions"]["tests_failed"][second["experiment_id"]] == 3
        assert comparison["recommendation"]

    async def test_list_experiments(self, client: Client) -> None:
        await client.call_tool("create_experiment", {})
        listed = payload(await client.call_tool("list_experiments", {}))
        assert len(listed) == 1
        assert listed[0]["status"] == "READY"


class TestErrorSurface:
    async def test_domain_errors_arrive_as_a_code_and_a_sentence(self, client: Client) -> None:
        with pytest.raises(ToolError) as info:
            await client.call_tool(
                "execute_experiment", {"experiment_id": "exp_nope", "command": "ls"}
            )
        assert "[EXPERIMENT_NOT_FOUND]" in str(info.value)

    async def test_no_python_traceback_reaches_the_client(self, client: Client) -> None:
        with pytest.raises(ToolError) as info:
            await client.call_tool("create_experiment", {"base_image": "evil.io/miner:latest"})
        message = str(info.value)
        assert "[INVALID_IMAGE]" in message
        assert "Traceback" not in message
        assert "  File " not in message

    async def test_a_rejected_path_says_which_rule_it_broke(
        self, client: Client, tmp_path: Path
    ) -> None:
        with pytest.raises(ToolError) as info:
            await client.call_tool("create_experiment", {"project_path": str(tmp_path / "gone")})
        assert "[INVALID_PROJECT_PATH]" in str(info.value)

    async def test_working_in_a_destroyed_experiment_is_refused(self, client: Client) -> None:
        created = payload(await client.call_tool("create_experiment", {}))
        await client.call_tool("destroy_experiment", {"experiment_id": created["experiment_id"]})
        with pytest.raises(ToolError) as info:
            await client.call_tool(
                "execute_experiment",
                {"experiment_id": created["experiment_id"], "command": "ls"},
            )
        assert "[EXPERIMENT_DESTROYED]" in str(info.value)


class TestResources:
    async def test_experiment_resource_includes_the_state_history(
        self, client: Client, project: Path
    ) -> None:
        created = payload(
            await client.call_tool("create_experiment", {"project_path": str(project)})
        )
        contents = await client.read_resource(f"sandbox://experiments/{created['experiment_id']}")
        body = json.loads(contents[0].text)
        assert body["experiment"]["status"] == "READY"
        assert [entry["to"] for entry in body["state_history"]] == ["CREATING", "READY"]

    async def test_logs_resource_lists_every_command(self, client: Client) -> None:
        created = payload(await client.call_tool("create_experiment", {}))
        await client.call_tool(
            "execute_experiment",
            {"experiment_id": created["experiment_id"], "command": "echo one"},
        )
        contents = await client.read_resource(
            f"sandbox://experiments/{created['experiment_id']}/logs"
        )
        body = json.loads(contents[0].text)
        assert [job["command"] for job in body["jobs"]] == ["echo one"]

    async def test_diff_resource_returns_a_unified_diff(
        self, client: Client, project: Path
    ) -> None:
        created = payload(
            await client.call_tool("create_experiment", {"project_path": str(project)})
        )
        await client.call_tool(
            "write_sandbox_file",
            {
                "experiment_id": created["experiment_id"],
                "path": "src/index.js",
                "content": "module.exports = 2;\n",
            },
        )
        contents = await client.read_resource(
            f"sandbox://experiments/{created['experiment_id']}/diff"
        )
        body = json.loads(contents[0].text)
        assert body["files_modified"] == ["src/index.js"]
        assert "+module.exports = 2;" in body["changes"][0]["diff"]

    async def test_experiments_collection_resource(self, client: Client) -> None:
        await client.call_tool("create_experiment", {})
        contents = await client.read_resource("sandbox://experiments")
        assert len(json.loads(contents[0].text)["experiments"]) == 1
