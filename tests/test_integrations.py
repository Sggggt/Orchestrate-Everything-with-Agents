"""Integration contracts. All runtime state and fixtures stay in OS temp dirs.

Run: python -B -m unittest discover -s tests -v
Requires the web and mcp dependency groups; never calls external services.
"""

import asyncio
import hashlib
import json
import os
import sys
import tempfile
import unittest
import socket
import subprocess
from pathlib import Path
from unittest.mock import patch

import apsw
import httpx

from agent.llm import LLMApiAgent
from ma_runtime import (
    AgentConfig,
    AgentSkill,
    MCPBinding,
    MCPServerConfig,
    PlatformClient,
    Runtime,
)
from ma_runtime.errors import RuntimeFault
from ma_runtime.models import json_dump
from ma_runtime.mcp_service import tool_alias
from ma_runtime.profiles import skill_from_markdown
from ma_runtime.web.app import create_app

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/mcp_server.py"


def payload(result):
    return result.get("structuredContent") or json.loads(result["content"][0]["text"])


class ScriptedLLM(LLMApiAgent):
    """Local completion double; the MCP transport remains a real subprocess."""

    def __init__(self, config):
        super().__init__(config)
        self.captured = []
        self.round = 0

    async def _completion(self, messages, context, tools=None):
        self.captured.append({"messages": list(messages), "tools": tools})
        if self.round == 0:
            calls = [
                {
                    "id": "skill-read",
                    "type": "function",
                    "function": {
                        "name": "read_skill",
                        "arguments": '{"skill_id":"review"}',
                    },
                }
            ]
        elif self.round == 1:
            alias = next(
                t["function"]["name"]
                for t in tools
                if t["function"]["name"].startswith("mcp_")
            )
            calls = [
                {
                    "id": "mcp-echo",
                    "type": "function",
                    "function": {
                        "name": alias,
                        "arguments": '{"text":"through-agent"}',
                    },
                }
            ]
        else:
            calls = []
        self.round += 1
        yield {
            "calls": calls,
            "text": "Agent profile and MCP tools verified" if not calls else "",
            "finish_reason": "stop",
        }


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="multi-agent-integrations-")
        self.rt = await Runtime.open(
            self.temp.name, {"reserved_external_slots": 0}
        ).__aenter__()

    async def asyncTearDown(self):
        await self.rt.aclose()
        self.temp.cleanup()

    async def register(self, identity="fixture", **options):
        config = MCPServerConfig(
            id=identity,
            name="Local fixture",
            command=sys.executable,
            args=("-B", str(FIXTURE)),
            **options,
        )
        await self.rt.mcp.configure(config)
        return config

    async def test_shared_connection_routes_concurrent_clients(self):
        await self.register(session_scope="shared", max_concurrency=3)
        await self.rt.mcp.connect("fixture", "a")
        first = await self.rt.mcp.call(
            "fixture", "echo", {"text": "a"}, consumer_id="a"
        )
        second = await self.rt.mcp.call(
            "fixture", "echo", {"text": "b"}, consumer_id="b"
        )
        self.assertEqual(payload(first)["pid"], payload(second)["pid"])
        results = await asyncio.gather(
            *(
                self.rt.mcp.call(
                    "fixture",
                    "delay_echo",
                    {"text": str(i), "delay": 0.05},
                    consumer_id=str(i),
                )
                for i in range(6)
            )
        )
        self.assertEqual(
            [payload(r)["text"] for r in results], list(map(str, range(6)))
        )
        self.assertEqual((await self.rt.mcp.get("fixture"))["connections"], 1)

    async def test_consumer_connections_isolate_upstream_selection(self):
        await self.register(session_scope="consumer")
        await self.rt.mcp.call(
            "fixture", "select_project", {"project": "alpha"}, consumer_id="a"
        )
        a = payload(
            await self.rt.mcp.call("fixture", "echo", {"text": "a"}, consumer_id="a")
        )
        b = payload(
            await self.rt.mcp.call("fixture", "echo", {"text": "b"}, consumer_id="b")
        )
        self.assertEqual(a["project"], "alpha")
        self.assertEqual(b["project"], "default")
        self.assertNotEqual(a["pid"], b["pid"])

    async def test_idempotency_does_not_repeat_side_effect(self):
        await self.register(session_scope="shared")
        a = await self.rt.mcp.call("fixture", "increment", {}, idempotency_key="once")
        b = await self.rt.mcp.call("fixture", "increment", {}, idempotency_key="once")
        self.assertEqual(payload(a)["counter"], 1)
        self.assertEqual(payload(b)["counter"], 1)
        self.assertTrue(b["reused"])
        with self.assertRaises(RuntimeFault) as error:
            await self.rt.mcp.call(
                "fixture", "echo", {"text": "other"}, idempotency_key="once"
            )
        self.assertEqual(error.exception.code, "IDEMPOTENCY_CONFLICT")
        self.assertEqual(len(await self.rt.mcp.calls("fixture")), 1)

    async def test_credential_values_are_write_only_and_schema_is_preserved(self):
        config = await self.register()
        secret = "fixture-secret-not-a-real-credential"
        await self.rt.mcp.configure(config, {"env": {"FIXTURE_SECRET": secret}})
        info = await self.rt.mcp.connect("fixture")
        self.assertEqual(info["env_keys"], ["FIXTURE_SECRET"])
        self.assertNotIn(secret, json_dump(info))
        schema = next(
            t for t in info["catalog"]["tools"] if t["name"] == "credential_echo"
        )["inputSchema"]
        self.assertIsInstance(schema["properties"]["password"], dict)
        result = await self.rt.mcp.call(
            "fixture", "credential_echo", {}, consumer_id="console"
        )
        self.assertNotIn(secret, json_dump(result))
        self.assertEqual(payload(result)["value"], "[redacted]")
        row = await self.rt.store.call(
            lambda db: next(
                iter(db.execute("SELECT config_json,credentials_json FROM mcp_servers"))
            )
        )
        self.assertNotIn(secret, json_dump(row))
        call = await self.rt.store.call(
            lambda db: next(iter(db.execute("SELECT result_json FROM mcp_calls")))
        )
        self.assertIsNone(call.result_json)

    async def test_resources_prompts_and_connection_lifecycle(self):
        await self.register()
        self.assertTrue((await self.rt.mcp.resources("fixture"))["resources"])
        self.assertIn(
            "platform contract tests",
            (await self.rt.mcp.read_resource("fixture", "fixture://readme"))[
                "contents"
            ][0]["text"],
        )
        self.assertTrue((await self.rt.mcp.prompts("fixture"))["prompts"])
        response = await self.rt.mcp.get_prompt(
            "fixture", "review_request", {"topic": "contracts"}
        )
        self.assertIn("contracts", response["messages"][0]["content"]["text"])
        self.assertEqual(
            (await self.rt.mcp.disconnect("fixture"))["status"], "DISCONNECTED"
        )
        self.assertEqual((await self.rt.mcp.connect("fixture"))["status"], "CONNECTED")

    async def test_external_grants_and_input_schema_are_enforced(self):
        await self.register(expose_to_codex=False)
        with self.assertRaises(RuntimeFault) as error:
            await self.rt.mcp.call("fixture", "echo", {"text": "blocked"})
        self.assertEqual(error.exception.code, "PERMISSION_DENIED")
        config = MCPServerConfig.model_validate(
            {
                **self.rt.mcp.configs["fixture"].model_dump(),
                "expose_to_codex": True,
                "allowed_tools": ["echo"],
            }
        )
        await self.rt.mcp.configure(config)
        with self.assertRaises(RuntimeFault) as error:
            await self.rt.mcp.call("fixture", "increment", {})
        self.assertEqual(error.exception.code, "PERMISSION_DENIED")
        with self.assertRaises(RuntimeFault) as error:
            await self.rt.mcp.call("fixture", "echo", {"text": 3})
        self.assertEqual(error.exception.code, "INVALID_REQUEST")
        self.assertEqual(await self.rt.mcp.calls(), [])

    async def test_cancel_one_call_preserves_shared_connection(self):
        await self.register(session_scope="shared", max_concurrency=2)
        await self.rt.mcp.connect("fixture")
        pending = asyncio.create_task(
            self.rt.mcp.call("fixture", "delay_echo", {"text": "cancel", "delay": 2})
        )
        await asyncio.sleep(0.05)
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        result = await self.rt.mcp.call("fixture", "echo", {"text": "still-connected"})
        self.assertEqual(payload(result)["text"], "still-connected")

    async def test_read_timeout_records_failure_without_replay(self):
        await self.register(session_scope="shared", timeout_seconds=0.15)
        await self.rt.mcp.connect("fixture")
        with self.assertRaises(RuntimeFault):
            await self.rt.mcp.call(
                "fixture",
                "delay_echo",
                {"text": "timeout", "delay": 2},
                idempotency_key="timeout",
            )
        self.assertEqual((await self.rt.mcp.calls())[0]["state"], "FAILED")
        with self.assertRaises(RuntimeFault) as error:
            await self.rt.mcp.call(
                "fixture",
                "delay_echo",
                {"text": "timeout", "delay": 2},
                idempotency_key="timeout",
            )
        self.assertEqual(error.exception.code, "MCP_RESULT_NOT_RETAINED")

    async def test_bad_startup_returns_safe_error(self):
        await self.rt.mcp.configure(
            MCPServerConfig(
                id="missing",
                name="Missing",
                command=str(Path(self.temp.name) / "does-not-exist.exe"),
            )
        )
        with self.assertRaises(RuntimeFault) as error:
            await self.rt.mcp.connect("missing")
        self.assertEqual(error.exception.code, "MCP_CONNECT_FAILED")
        self.assertEqual((await self.rt.mcp.get("missing"))["status"], "ERROR")

    async def test_streamable_http_transport(self):
        with socket.socket() as available:
            available.bind(("127.0.0.1", 0))
            port = available.getsockname()[1]
        process = subprocess.Popen(
            [
                sys.executable,
                "-B",
                str(FIXTURE),
                "--transport",
                "streamable-http",
                "--port",
                str(port),
            ],
            cwd=self.temp.name,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        try:
            async with httpx.AsyncClient() as probe:
                for _ in range(60):
                    try:
                        await probe.get(f"http://127.0.0.1:{port}/mcp")
                        break
                    except httpx.ConnectError:
                        await asyncio.sleep(0.05)
                else:
                    self.fail("Local HTTP MCP fixture did not start")
            await self.rt.mcp.configure(
                MCPServerConfig(
                    id="http-fixture",
                    name="HTTP fixture",
                    transport="streamable-http",
                    url=f"http://127.0.0.1:{port}/mcp",
                    session_scope="shared",
                )
            )
            result = await self.rt.mcp.call(
                "http-fixture", "echo", {"text": "http-transport"}
            )
            self.assertEqual(payload(result)["text"], "http-transport")
            await self.rt.mcp.disconnect("http-fixture")
        finally:
            process.terminate()
            await asyncio.to_thread(process.wait, 10)

    async def test_platform_mcp_bridge_exposes_management_tools(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        with tempfile.TemporaryDirectory(prefix="ma-bridge-fixture-") as directory:
            params = StdioServerParameters(
                command=sys.executable,
                args=["-B", "-m", "ma_runtime.cli", "mcp", "--data-dir", directory],
                env={"PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1"},
            )
            with open(os.devnull, "w") as log:
                async with stdio_client(params, errlog=log) as (read, write):
                    async with ClientSession(read, write) as client:
                        await client.initialize()
                        names = {t.name for t in (await client.list_tools()).tools}
                        self.assertTrue(
                            {
                                "platform_describe",
                                "mcp_server_configure",
                                "mcp_tool_call",
                                "agent_configure",
                                "agent_skill_import",
                            }
                            <= names
                        )
                        result = await client.call_tool(
                            "mcp_server_configure",
                            {
                                "config": {
                                    "id": "fixture",
                                    "name": "Fixture",
                                    "command": sys.executable,
                                    "args": ["-B", str(FIXTURE)],
                                }
                            },
                        )
                        self.assertFalse(result.isError)
                        result = await client.call_tool(
                            "mcp_tool_call",
                            {
                                "server_id": "fixture",
                                "tool_name": "echo",
                                "arguments": {"text": "codex-bridge"},
                                "consumer_id": "test-chat",
                                "idempotency_key": "bridge-test",
                            },
                        )
                        self.assertFalse(result.isError)
                        data = result.structuredContent or json.loads(
                            result.content[0].text
                        )
                        self.assertEqual(
                            payload(data["result"])["text"], "codex-bridge"
                        )

    async def test_agent_profile_and_actual_mcp_tool_round_trip(self):
        await self.register(session_scope="consumer")
        config = AgentConfig(
            id="researcher",
            name="Researcher",
            kind="llm",
            agent_md="Always explain verification evidence.",
            base_url="http://model-fixture.invalid/v1",
            model="offline-scripted",
            skills=(
                AgentSkill(
                    id="review",
                    name="Review",
                    description="Use for verification tasks",
                    content="# Verification checklist\nCheck actual behavior.",
                ),
            ),
            mcp_bindings=(MCPBinding(server_id="fixture", tools=("echo",)),),
        )
        await self.rt.agents.configure(config)
        adapter = ScriptedLLM(config)
        self.rt.adapters[config.id] = adapter
        session = await self.rt.sessions.create(title="Agent integration test")
        thread = await self.rt.threads.create(session.id, config.id)
        handle = await self.rt.threads.turn(
            thread.id, "Verify the platform", "agent-roundtrip"
        )
        result = await self.rt.tasks.result(handle.id, timeout=15)
        self.assertIn("verified", result.output["text"])
        first = adapter.captured[0]
        self.assertIn(
            "Always explain verification evidence", first["messages"][0]["content"]
        )
        self.assertNotIn("Check actual behavior", first["messages"][0]["content"])
        names = [t["function"]["name"] for t in first["tools"]]
        self.assertIn("read_skill", names)
        self.assertIn(tool_alias("fixture", "echo"), names)
        self.assertNotIn(tool_alias("fixture", "increment"), names)
        self.assertIn(
            "Check actual behavior", adapter.captured[1]["messages"][-1]["content"]
        )
        self.assertIn("through-agent", adapter.captured[2]["messages"][-1]["content"])
        self.assertEqual(
            (await self.rt.mcp.calls())[0]["consumer_id"], "thread:" + thread.id
        )

    async def test_profile_changes_apply_after_checkpoint(self):
        await self.register()
        config = AgentConfig(
            id="researcher",
            name="Researcher",
            agent_md="Original instructions",
            base_url="http://model-fixture.invalid/v1",
            model="offline-scripted",
            skills=(AgentSkill(id="review", name="Review", content="Old checklist"),),
            mcp_bindings=(MCPBinding(server_id="fixture", tools=("echo",)),),
        )
        await self.rt.agents.configure(config)
        self.rt.adapters[config.id] = ScriptedLLM(config)
        session = await self.rt.sessions.create(title="Profiles")
        thread = await self.rt.threads.create(session.id, config.id)
        first = await self.rt.threads.turn(thread.id, "First", "profile-first")
        await self.rt.tasks.result(first.id, timeout=15)
        updated = config.model_copy(update={"agent_md": "New instructions"})
        await self.rt.agents.configure(updated)
        next_adapter = ScriptedLLM(updated)
        self.rt.adapters[config.id] = next_adapter
        second = await self.rt.threads.turn(thread.id, "Second", "profile-second")
        await self.rt.tasks.result(second.id, timeout=15)
        prompts = [
            m["content"]
            for m in next_adapter.captured[0]["messages"]
            if m["role"] == "system"
        ]
        self.assertEqual(len(prompts), 1)
        self.assertIn("New instructions", prompts[0])
        self.assertNotIn("Original instructions", prompts[0])

    async def test_stdio_agent_sdk_uses_granted_tools_and_skills(self):
        await self.register()
        config = AgentConfig(
            id="integration-inspector",
            name="Inspector",
            kind="process",
            command=(sys.executable, "-B", str(ROOT / "examples/integration_agent.py")),
            agent_md="# Inspector\nVerify capabilities.",
            skills=(
                AgentSkill(
                    id="review",
                    name="Review",
                    content="# Review\nInspect the current integration.",
                ),
            ),
            mcp_bindings=(MCPBinding(server_id="fixture", tools=("echo",)),),
        )
        await self.rt.agents.configure(config)
        session = await self.rt.sessions.create(title="Process SDK")
        thread = await self.rt.threads.create(session.id, config.id)
        handle = await self.rt.tasks.submit(
            {
                "session_id": session.id,
                "thread_id": thread.id,
                "agent_id": config.id,
                "payload": {
                    "input": "SDK test",
                    "tool": tool_alias("fixture", "echo"),
                    "arguments": {"text": "process-sdk"},
                },
                "idempotency_key": "process-sdk-test",
            }
        )
        result = await self.rt.tasks.result(handle.id, timeout=15)
        self.assertTrue(result.output["agent_md_loaded"])
        self.assertEqual(result.output["skill_loaded"], "skills/review/SKILL.md")
        self.assertEqual(payload(result.output["tool_result"])["text"], "process-sdk")

    async def test_http_sdk_documents_and_registry(self):
        app = create_app(runtime=self.rt)
        async with app.router.lifespan_context(app):
            async with PlatformClient(
                "http://127.0.0.1",
                app.state.token,
                transport=httpx.ASGITransport(app=app),
            ) as client:
                protocol = await client.protocol()
                self.assertIn("mcp.configure", protocol["methods"])
                await client.mcp.configure(
                    {
                        "id": "fixture",
                        "name": "Fixture",
                        "command": sys.executable,
                        "args": ["-B", str(FIXTURE)],
                    }
                )
                self.assertEqual(
                    (await client.mcp.connect("fixture"))["status"], "CONNECTED"
                )
                await client.agents.configure(
                    AgentConfig(id="writer", name="Writer", kind="mock")
                )
                await client.agents.set_instructions(
                    "writer", "# Writer\nFollow the style guide."
                )
                await client.agents.import_skill(
                    "writer",
                    "style",
                    "---\nname: Style guide\ndescription: Use for writing\n---\n# Style\nBe concise.",
                )
                docs = await client.agents.documents("writer")
                self.assertEqual(docs["skills"][0]["name"], "Style guide")
                self.assertIn("Follow the style guide", docs["instructions"]["content"])
                await client.agents.update(
                    "writer", mcp_bindings=[{"server_id": "fixture", "tools": ["echo"]}]
                )
                self.assertEqual(
                    (await client.mcp.get("fixture"))["bound_agents"], ["writer"]
                )
                result = await client.mcp.call_tool(
                    "fixture", "echo", {"text": "sdk"}, idempotency_key="sdk"
                )
                self.assertEqual(payload(result)["text"], "sdk")
                self.assertNotIn(
                    "client-token", json_dump(await client.mcp.codex_config())
                )

    async def test_codex_import_preserves_existing_registration(self):
        with tempfile.TemporaryDirectory(prefix="ma-codex-fixture-") as directory:
            config = f'[mcp_servers.local_fixture]\ncommand={json.dumps(sys.executable)}\nargs=["-B",{json.dumps(str(FIXTURE))}]\n[mcp_servers.local_fixture.env]\nFIXTURE_SECRET="import-example-secret"\n'
            (Path(directory) / "config.toml").write_text(config, encoding="utf-8")
            with patch.dict(os.environ, {"CODEX_HOME": directory}):
                candidates = await self.rt.mcp.codex_candidates()
                self.assertEqual(candidates[0]["name"], "local_fixture")
                self.assertNotIn("import-example-secret", json_dump(candidates))
                first = await self.rt.mcp.import_codex(["local_fixture"])
                self.assertEqual(len(first["imported"]), 1)
                second = await self.rt.mcp.import_codex(["local_fixture"])
                self.assertEqual(len(second["skipped"]), 1)
                result = await self.rt.mcp.call("local_fixture", "credential_echo", {})
                self.assertEqual(payload(result)["value"], "[redacted]")


class MigrationAndModelTests(unittest.IsolatedAsyncioTestCase):
    async def test_v5_workflow_is_preserved_by_recoverable_delete_migration(self):
        from ma_runtime.workflows import default_graph

        with tempfile.TemporaryDirectory(prefix="ma-workflow-migration-") as directory:
            root = Path(directory)
            db = apsw.Connection(str(root / "runtime.sqlite"))
            for version in range(1, 6):
                name = "schema.sql" if version == 1 else f"schema-{version}.sql"
                schema = (ROOT / "src/ma_runtime/store" / name).read_text(encoding="utf-8")
                db.execute(schema)
                db.execute("INSERT INTO schema_migrations VALUES(?,?,1)",
                           (version, hashlib.sha256(schema.encode()).hexdigest()))
            graph = default_graph()
            db.execute("INSERT INTO workflows(id,project_id,name,graph_json,creation_key,request_hash,created_at,updated_at) VALUES('legacy-flow','default','Preserved graph',?,'legacy','legacy',1,1)",
                       (json_dump(graph),))
            db.close()
            async with Runtime.open(root) as rt:
                saved = await rt.workflows.get("legacy-flow")
                self.assertEqual(saved.graph, graph)
                self.assertEqual(saved.project_id, "default")
                self.assertTrue((root / "backups/before-schema-6.sqlite").is_file())
                await rt.workflows.delete(saved.id, confirmed=True)
                self.assertEqual((await rt.workflows.get(saved.id)).state, "DELETED")
                self.assertEqual((await rt.workflows.restore(saved.id)).graph, graph)

    async def test_existing_v1_database_is_backed_up_and_migrated(self):
        with tempfile.TemporaryDirectory(prefix="ma-migration-") as directory:
            root = Path(directory)
            schema = (ROOT / "src/ma_runtime/store/schema.sql").read_text(
                encoding="utf-8"
            )
            db = apsw.Connection(str(root / "runtime.sqlite"))
            db.execute(schema)
            db.execute(
                "INSERT INTO schema_migrations VALUES(1,?,1)",
                (hashlib.sha256(schema.encode()).hexdigest(),),
            )
            db.execute(
                "INSERT INTO sessions(id,state,title,metadata_json,created_at) VALUES('old','OPEN','Preserved workspace','{}',1)"
            )
            db.close()
            async with Runtime.open(root) as rt:
                self.assertEqual(
                    (await rt.sessions.get("old")).title, "Preserved workspace"
                )
                versions = await rt.store.call(
                    lambda db: [
                        row.version
                        for row in db.execute(
                            "SELECT version FROM schema_migrations ORDER BY version"
                        )
                    ]
                )
                self.assertEqual(versions, list(range(1, 27)))
                self.assertTrue((root / "backups/before-schema-2.sqlite").is_file())
                self.assertTrue((root / "backups/before-schema-3.sqlite").is_file())
                self.assertTrue((root / "backups/before-schema-4.sqlite").is_file())
                self.assertTrue((root / "backups/before-schema-5.sqlite").is_file())
                self.assertTrue((root / "backups/before-schema-6.sqlite").is_file())
                self.assertTrue((root / "backups/before-schema-11.sqlite").is_file())
                self.assertTrue(await rt.store.call(lambda db: list(db.execute(
                    "SELECT name FROM sqlite_master WHERE type='index' AND name='events_task_type_seq'"))))
                self.assertEqual((await rt.sessions.get("old")).project_id, "default")
            async with Runtime.open(root) as rt:
                self.assertEqual(await rt.mcp.list(), [])

    async def test_instruction_and_skill_validation(self):
        skill = skill_from_markdown(
            "review",
            "---\nname: Reviewer\ndescription: >\n  Review work\n  carefully.\n---\n# Review",
        )
        self.assertIn("Review work carefully", skill.description)
        with self.assertRaises(ValueError):
            AgentSkill(id="bad", name="Bad", resources={"../outside": "no"})
        with self.assertRaises(ValueError):
            AgentConfig(id="bad", name="Bad", skills=(skill, skill))
        with self.assertRaises(ValueError):
            MCPServerConfig(
                id="bad",
                name="Bad",
                transport="streamable-http",
                url="https://user:password@example.invalid/mcp",
            )


if __name__ == "__main__":
    unittest.main()
