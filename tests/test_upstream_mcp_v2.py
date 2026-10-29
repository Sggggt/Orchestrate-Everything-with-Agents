"""Real business MCP subprocesses, grants, concurrency, and late receipts."""

import asyncio
import json
import os
import socket
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from oea import AgentDefinition, Completion, MCPBinding, MCPServer, Model, RunSpec, Tools
from oea.contracts.errors import RuntimeFault
from oea.runtime.host import Host
from oea.storage.persistence import one, rows
from oea.mcp.upstream import alias
from oea.tools.functions import tool
from oea.tools.discovery import search


SERVER = '''
import asyncio
import os
from pathlib import Path
from mcp.server.fastmcp import FastMCP
mcp = FastMCP("business-fixture", log_level="WARNING")
active = count = peak = 0
ready, release = asyncio.Event(), asyncio.Event()
@mcp.tool()
async def hold(value: int) -> dict:
    global active, count, peak
    count += 1
    active += 1
    peak = max(active, peak)
    if active == 20:
        ready.set()
    try:
        await release.wait()
        return {"value": value, "count": count, "peak": peak}
    finally:
        active -= 1
@mcp.tool()
async def observe_ready() -> dict:
    await ready.wait()
    return {"active": active, "peak": peak}
@mcp.tool()
async def finish() -> dict:
    release.set()
    return {"count": count, "peak": peak}
@mcp.tool()
async def echo(value: int) -> dict:
    global count
    count += 1
    return {"value": value, "count": count}
@mcp.tool()
async def disconnect(value: int) -> dict:
    with Path(os.environ["EFFECT_FILE"]).open("a", encoding="utf-8") as output:
        output.write(str(value))
        output.flush()
        os.fsync(output.fileno())
    os._exit(2)
if __name__ == "__main__":
    mcp.run(transport="stdio")
'''


def content(result):
    if isinstance(result, dict):
        if result.get("isError"):
            raise AssertionError(result)
        return result.get("structured_content") or json.loads(result["content"][0]["text"])
    if result.isError:
        raise AssertionError(result)
    return result.structuredContent or json.loads(result.content[0].text)


class ToolModel:
    """Only the model decision is a fixture; MCP transport and service are real."""
    def __init__(self, upstream="echo", before_call=None):
        self.upstream, self.before_call, self.after_tool, self.calls = upstream, before_call, None, 0

    async def complete(self, model, messages, **options):
        self.calls += 1
        if any(call["function"]["name"].startswith("mcp_") for message in messages for call in message.get("tool_calls", ())):
            if self.after_tool:
                await self.after_tool()
            return Completion(text="Observed", usage={"prompt_tokens": 30, "completion_tokens": 2})
        name = next((tool["function"]["name"] for tool in options["tools"] if tool["function"]["name"].startswith("mcp_")), None)
        if name is None:
            results = [json.loads(message["content"]) for message in messages if message["role"] == "tool"]
            page = next((value for value in reversed(results) if value.get("items")), None)
            arguments = {"name": page["items"][0]["name"]} if page else {"query": ""}
            return Completion(tool_calls=({"id": "discover-" + str(self.calls), "type": "function", "function": {
                "name": "tool_guide", "arguments": json.dumps(arguments)}},),
                finish_reason="tool_calls", usage={"prompt_tokens": 30, "completion_tokens": 2})
        if self.before_call:
            await self.before_call(name)
        return Completion(tool_calls=({"id": "business", "type": "function", "function": {"name": name, "arguments": '{"value":37}'}},),
                          finish_reason="tool_calls", usage={"prompt_tokens": 30, "completion_tokens": 2})


class UpstreamTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="oea-upstream-")
        self.root = Path(self.temporary.name)
        self.script = self.root / "business_service.py"
        self.script.write_text(SERVER, encoding="utf-8")
        self.host = await Host(self.root / "state", model_api=ToolModel()).__aenter__()
        await self.host.projects.create("Project", project_id="p")
        self.session = await self.host.sessions.create("p")
        await self.host.mcp.save(MCPServer(id="business", project_id="p", session_scope="shared", command=(sys.executable, "-B", str(self.script)),
            environment={"PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1", "TEMP": os.environ["TEMP"], "TMP": os.environ["TMP"], "EFFECT_FILE": str(self.root / "effect.txt")}, timeout_seconds=60), expected_revision=0)

    async def asyncTearDown(self):
        await self.host.aclose()
        self.temporary.cleanup()

    async def configure(self, upstream="echo", server_id="business"):
        definition = AgentDefinition(id="worker", name="Worker", project_id="p", model=Model(base_url="https://model.invalid/v1", model="protocol-fixture"),
            tools=Tools(mcp=(MCPBinding(server_id=server_id, tools=(upstream,)),)))
        await self.host.agents.save(definition)
        return definition

    async def submit(self, name="Worker"):
        instance = await self.host.instances.create(self.session["id"], "worker", name=name)
        return await self.host.runs.submit(RunSpec(instance_id=instance["id"], input="Use the business tool", idempotency_key="work"))

    async def test_discovery_alias_actual_call_and_retained_receipt(self):
        await self.configure()
        before = await self.host.mcp.inspect("business", "p")
        self.assertEqual(before["connection_state"], "disconnected")
        self.assertIsNone(before["tool_count"])
        self.assertEqual(self.host.mcp.connections, {})
        catalog = await self.host.mcp.discover("business", "p")
        self.assertEqual(len(catalog["tools"]), 5)
        self.assertTrue(all(value["spec"]["cancellation"] == "none" for value in catalog["tools"]))
        found = search(self.host.tools, [value["spec"]["name"] for value in catalog["tools"]], query="echo")
        self.assertEqual([item["source"] for item in found["items"]], ["business/echo"])
        run = await self.submit()
        self.assertEqual((await self.host.runs.result(run.id, timeout=10)).text, "Observed")
        receipts = await self.host.store.call(lambda db: rows(db, "SELECT * FROM actions WHERE run_id=? AND name LIKE 'mcp_%'", (run.id,)))
        self.assertEqual(len(receipts), 1)
        self.assertEqual(content(json.loads(receipts[0]["result_json"])), {"value": 37, "count": 1})
        connection = await self.host.mcp.connection("business", 1)
        self.assertEqual(content(await connection.session.call_tool("finish"))["count"], 1)
        observed = await self.host.mcp.inspect("business", "p")
        self.assertEqual((observed["connection_state"], observed["tool_count"], observed["active_calls"]), ("connected", 5, 0))
        self.assertEqual(observed["bound_agents"], ["worker"])
        history = await self.host.mcp.calls("business", "p", limit=1)
        self.assertEqual(history["items"][0]["id"], receipts[0]["id"])
        self.assertNotIn("result_json", history["items"][0])
        self.assertIsNone(history["cursor"])
        await self.host.mcp.disconnect("business", "p")
        self.assertEqual((await self.host.mcp.inspect("business", "p"))["connection_state"], "disconnected")
        with self.assertRaises(RuntimeFault) as denied:
            await self.host.mcp.discover("business", "another-project")
        self.assertEqual(denied.exception.code, "PERMISSION_DENIED")

    async def test_revoke_after_schema_was_seen_prevents_external_invocation(self):
        definition = await self.configure()
        async def revoke(name):
            await self.host.agents.save(definition.model_copy(update={"tools": Tools()}), expected_revision=1)
        self.host.model_api = ToolModel(before_call=revoke)
        run = await self.submit()
        await self.host.runs.result(run.id, timeout=10)
        connection = await self.host.mcp.connection("business", 1)
        self.assertEqual(content(await connection.session.call_tool("finish"))["count"], 0)
        receipts = await self.host.store.call(lambda db: rows(db, "SELECT * FROM actions WHERE run_id=? AND name LIKE 'mcp_%'", (run.id,)))
        self.assertEqual(receipts, [])

    async def test_twenty_calls_share_connection_without_capacity_wait_and_cancel_is_honest(self):
        await self.configure("hold")
        self.host.model_api = ToolModel("hold")
        await self.host.mcp.discover("business", "p")
        connection = await self.host.mcp.connection("business", 1)
        runs = [await self.submit("Worker " + str(index)) for index in range(20)]
        ready = await asyncio.wait_for(connection.session.call_tool("observe_ready"), 15)
        self.assertEqual(content(ready), {"active": 20, "peak": 20})
        self.assertEqual((await self.host.mcp.inspect("business", "p"))["active_calls"], 20)
        with self.assertRaises(RuntimeFault) as busy:
            await self.host.mcp.disconnect("business", "p")
        self.assertEqual(busy.exception.code, "MCP_IN_USE")
        saved = await self.host.mcp.get("business")
        await self.host.mcp.save(MCPServer.model_validate(saved["definition"]).model_copy(update={"name": "Updated display name"}), expected_revision=1)
        self.assertFalse(connection.task.done())
        cancelled = await self.host.runs.cancel(runs[0].id)
        self.assertEqual(cancelled["state"], "needs_review")
        self.assertIsNone(cancelled["stopped_at"])
        pending = await self.host.store.call(lambda db: rows(db, "SELECT id,state FROM actions WHERE run_id=? AND name LIKE 'mcp_%'", (runs[0].id,)))
        self.assertEqual(pending[0]["state"], "needs_review")
        with self.assertRaises(RuntimeFault) as still_running:
            await self.host.runs.confirm_action(pending[0]["id"], "p", "cannot-confirm-live", "The owned invocation is still running", "not_applied")
        self.assertEqual(still_running.exception.code, "ACTION_IN_FLIGHT")
        finished = await connection.session.call_tool("finish")
        self.assertEqual(content(finished), {"count": 20, "peak": 20})
        await asyncio.gather(*(self.host.runs.result(run.id, timeout=15) for run in runs[1:]))
        for task in tuple(self.host.maintenance):
            await task
        receipt = await self.host.tools.inspect(pending[0]["id"])
        self.assertEqual(receipt["state"], "completed")
        self.assertEqual(content(json.loads(receipt["result_json"]))["value"], 37)
        self.assertEqual((await self.host.runs.get(runs[0].id))["state"], "cancelled")
        if self.host.mcp.retirement is not None:
            await self.host.mcp.retirement
        self.assertTrue(connection.task.done())
        self.assertNotIn(("business", 1, "shared"), self.host.mcp.connections)

    async def test_repeated_configuration_changes_retire_actual_idle_connections(self):
        for revision in range(1, 4):
            await self.host.mcp.discover("business", "p")
            connection = await self.host.mcp.connection("business", revision)
            saved = await self.host.mcp.get("business")
            await self.host.mcp.save(MCPServer.model_validate(saved["definition"]).model_copy(update={"name": "Revision " + str(revision + 1)}), expected_revision=revision)
            self.assertTrue(connection.task.done())
            self.assertEqual(self.host.mcp.connections, {})

    async def test_default_consumer_sessions_isolate_instances_and_controller_conversations(self):
        self.assertEqual(MCPServer(id="default", command=("program",)).session_scope, "consumer")
        saved = await self.host.mcp.get("business")
        await self.host.mcp.save(MCPServer.model_validate(saved["definition"]).model_copy(update={"session_scope": "consumer"}), expected_revision=1)
        await self.configure()
        runs = await asyncio.gather(self.submit("First"), self.submit("Second"))
        await asyncio.gather(*(self.host.runs.result(run.id, timeout=15) for run in runs))
        for run in runs:
            receipt = await self.host.store.call(lambda db: one(db, "SELECT result_json FROM actions WHERE run_id=? AND name LIKE 'mcp_%'", (run.id,)))
            self.assertEqual(content(json.loads(receipt["result_json"]))["count"], 1)
        for consumer, key, expected in (("first", "one", 1), ("second", "one", 1), ("first", "two", 2)):
            observed = await self.host.mcp.call("business", "echo", {"value": 9}, key, project_id="p", consumer_id=consumer)
            self.assertEqual(content(observed["result"])["count"], expected)
        self.assertEqual(len(self.host.mcp.connections), 4)
        self.assertEqual(len({id(value.session) for value in self.host.mcp.connections.values()}), 4)
        self.assertTrue(all(key[2] != "shared" for key in self.host.mcp.connections))
        await self.host.mcp.disconnect("business", "p")
        self.assertEqual(self.host.mcp.connections, {})

    async def test_service_tool_grants_and_external_exposure_are_independent_of_agent_binding(self):
        saved = await self.host.mcp.get("business")
        await self.host.mcp.save(MCPServer.model_validate(saved["definition"]).model_copy(update={"allowed_tools": ("echo",), "expose_to_codex": False}), expected_revision=1)
        await self.host.mcp.discover("business", "p")
        with self.assertRaises(RuntimeFault) as denied:
            await self.host.mcp.call("business", "echo", {"value": 3}, "external-denied", project_id="p", consumer_id="controller")
        self.assertEqual(denied.exception.code, "PERMISSION_DENIED")
        self.assertEqual(await self.host.store.call(lambda db: one(db, "SELECT COUNT(*) AS count FROM tool_intents")["count"]), 0)
        await self.configure()
        run = await self.submit()
        self.assertEqual((await self.host.runs.result(run.id, timeout=10)).text, "Observed")
        saved = await self.host.mcp.get("business")
        await self.host.mcp.save(MCPServer.model_validate(saved["definition"]).model_copy(update={"expose_to_codex": True}), expected_revision=2)
        await self.host.mcp.discover("business", "p")
        with self.assertRaises(RuntimeFault) as excluded:
            await self.host.mcp.call("business", "hold", {"value": 3}, "tool-denied", project_id="p", consumer_id="controller")
        self.assertEqual(excluded.exception.code, "PERMISSION_DENIED")
        original = await self.host.mcp.call("business", "echo", {"value": 3}, "allowed", project_id="p", consumer_id="controller")
        saved = await self.host.mcp.get("business")
        await self.host.mcp.save(MCPServer.model_validate(saved["definition"]).model_copy(update={"expose_to_codex": False, "allowed_tools": ()}), expected_revision=3)
        replay = await self.host.mcp.call("business", "echo", {"value": 3}, "allowed", project_id="p", consumer_id="controller")
        self.assertEqual(replay["action_id"], original["action_id"])
        self.assertEqual(replay["result"], original["result"])
        self.assertEqual(await self.host.store.call(lambda db: one(db, "SELECT COUNT(*) AS count FROM tool_intents")["count"]), 1)

    async def test_directory_registration_resolves_paths_without_starting_service(self):
        directory = self.root / "registered-mcp"
        directory.mkdir()
        config = {"id": "registered", "name": "Registered tools", "description": "An explicitly configured directory.", "command": ["./runner.exe", "--stdio"], "cwd": "work", "environment_refs": {"API_KEY": "env:TEST_MCP_KEY"}}
        source = directory / "mcp.json"
        source.write_text(json.dumps(config), encoding="utf-8")
        saved = await self.host.mcp.register_directory(directory, "p")
        self.assertEqual(saved["definition"]["cwd"], str(directory / "work"))
        self.assertEqual(saved["definition"]["command"][0], str(directory / "runner.exe"))
        self.assertEqual(saved["definition"]["source_directory"], str(directory))
        self.assertEqual(self.host.mcp.connections, {})
        self.assertIsNone((await self.host.mcp.inspect("registered", "p"))["tool_count"])
        source.write_text(json.dumps({**config, "id": "unsafe", "headers": {"Authorization": "private-value"}}), encoding="utf-8")
        with self.assertRaises(RuntimeFault) as private:
            await self.host.mcp.register_directory(directory, "p")
        self.assertEqual(private.exception.code, "INVALID_REQUEST")
        self.assertEqual(len((await self.host.mcp.list("p"))["items"]), 2)

    async def test_disconnect_after_commit_requires_scoped_evidence_and_never_replays_the_write(self):
        await self.configure("disconnect")
        run = await self.submit()
        with self.assertRaises(RuntimeFault) as unknown:
            await self.host.runs.result(run.id, timeout=15)
        self.assertEqual(unknown.exception.code, "OUTCOME_UNCERTAIN")
        self.assertEqual((self.root / "effect.txt").read_text(encoding="utf-8"), "37")

        await asyncio.gather(*tuple(self.host.jobs.values()))
        receipt = (await self.host.runs.inspect(run.id))["actions"][0]
        self.assertEqual(receipt["state"], "needs_review")
        with self.assertRaises(RuntimeFault):
            await self.host.runs.resume(run.id, "unsafe-retry")
        with self.assertRaises(RuntimeFault) as wrong_project:
            await self.host.runs.confirm_action(receipt["id"], "another-project", "confirm", "Read the actual external value", "not_applied")
        self.assertEqual(wrong_project.exception.code, "PERMISSION_DENIED")
        with self.assertRaises(RuntimeFault) as invalid:
            await self.host.runs.confirm_action(receipt["id"], "p", "invalid", "Read the actual external value", "completed", result={"unrelated": True})
        self.assertEqual(invalid.exception.code, "TOOL_RESULT_INVALID")
        actual = {"isError": False, "content": [{"type": "text", "text": "37"}], "structured_content": {"value": 37}}
        settle_ready, settle_release, model_ready, model_release = [asyncio.Event() for _ in range(4)]
        original_unresolved = self.host._unresolved
        async def delayed_settle(identity):
            pending = await original_unresolved(identity)
            if not pending and not settle_ready.is_set():
                settle_ready.set()
                await settle_release.wait()
            return pending
        async def paused_final_model():
            model_ready.set()
            await model_release.wait()
        self.host.model_api.after_tool = paused_final_model
        with patch.object(self.host, "_unresolved", side_effect=delayed_settle):
            try:
                confirmed = await self.host.runs.confirm_action(receipt["id"], "p", "confirmed-once", "Independent file read verified the external value is 37", "completed", result=actual)
                self.assertFalse(confirmed["reused"])
                await asyncio.wait_for(settle_ready.wait(), 5)
                replay = await self.host.runs.confirm_action(receipt["id"], "p", "confirmed-once", "Independent file read verified the external value is 37", "completed", result=actual)
                self.assertTrue(replay["reused"])
                self.assertEqual(self.host.model_api.calls, 3)
                await self.host.runs.resume(run.id, "continue-after-verification")
                await asyncio.wait_for(model_ready.wait(), 5)
                writer = self.host.active[run.id]
                settle_release.set()
                await asyncio.gather(*tuple(self.host.maintenance))
                self.assertEqual((await self.host.runs.get(run.id))["state"], "running")
                self.assertIs(self.host.active[run.id], writer)
            finally:
                settle_release.set()
                model_release.set()
        self.assertEqual((await self.host.runs.result(run.id, timeout=10)).text, "Observed")
        receipts = await self.host.store.call(lambda db: rows(db, "SELECT id,state FROM actions WHERE run_id=? AND name LIKE 'mcp_%'", (run.id,)))
        self.assertEqual(receipts, [{"id": receipt["id"], "state": "completed"}])
        self.assertEqual((self.root / "effect.txt").read_text(encoding="utf-8"), "37")

    async def test_streamable_http_uses_secret_reference_and_the_same_tool_receipt(self):
        import uvicorn
        from mcp.server.fastmcp import FastMCP
        from starlette.responses import JSONResponse

        remote = FastMCP("business-http-fixture", json_response=True)
        @remote.tool()
        async def echo(value: int) -> dict:
            return {"value": value, "transport": "http"}
        upstream = remote.streamable_http_app()
        token, observations = "Bearer public-fixture-credential", []
        async def authenticated(scope, receive, send):
            if scope["type"] == "http":
                authorized = dict(scope["headers"]).get(b"authorization") == token.encode("ascii")
                observations.append(authorized)
                if not authorized:
                    await JSONResponse({"error": "Unauthorized"}, status_code=401)(scope, receive, send)
                    return
            await upstream(scope, receive, send)
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(authenticated, log_level="warning", access_log=False, proxy_headers=False, timeout_graceful_shutdown=2))
        serving = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(5):
                while not server.started:
                    if serving.done():
                        await serving
                    await asyncio.sleep(0.01)
            saved = await self.host.mcp.save(MCPServer(id="http", project_id="p", transport="streamable_http", url="http://127.0.0.1:" + str(port) + "/mcp",
                timeout_seconds=10))
            credential = await self.host.registry.call("mcp.set_credential", {"server_id": "http", "project_id": "p", "target": "header", "name": "Authorization", "value": token, "expected_revision": saved["revision"]})
            self.assertTrue(credential["configured"])
            self.assertNotIn(token, json.dumps(credential))
            self.assertNotIn(token, json.dumps(await self.host.mcp.get("http")))
            self.assertNotIn(token, json.dumps(saved))
            await self.configure(server_id="http")
            run = await self.submit()
            with patch.dict(os.environ, {"HTTP_PROXY": "http://127.0.0.1:1", "HTTPS_PROXY": "http://127.0.0.1:1", "ALL_PROXY": "http://127.0.0.1:1", "NO_PROXY": ""}):
                await self.host.runs.result(run.id, timeout=10)
            receipt = await self.host.store.call(lambda db: rows(db, "SELECT result_json FROM actions WHERE run_id=? AND name LIKE 'mcp_%'", (run.id,)))
            self.assertEqual(content(json.loads(receipt[0]["result_json"])), {"value": 37, "transport": "http"})
            self.assertTrue(observations)
            self.assertTrue(all(observations))
        finally:
            await self.host.mcp.aclose()
            server.should_exit = True
            await asyncio.wait_for(serving, 5)

    async def test_actual_control_mcp_identity_is_rejected_before_tool_discovery(self):
        control_script = self.root / "control_service.py"
        control_script.write_text(SERVER.replace('FastMCP("business-fixture"', 'FastMCP("oea"'), encoding="utf-8")
        await self.host.mcp.save(MCPServer(id="control", project_id="p", command=(sys.executable, "-B", str(control_script)), timeout_seconds=5))
        with self.assertRaises(RuntimeFault) as rejected:
            await self.host.mcp.discover("control", "p")
        self.assertEqual(rejected.exception.code, "CONTROL_BRIDGE_FORBIDDEN")

    async def test_known_credential_reflection_is_not_saved_as_public_tool_metadata(self):
        secret = "fixture-mcp-private-key"
        script = self.root / "credential_metadata.py"
        script.write_text('import os\nfrom mcp.server.fastmcp import FastMCP\nmcp=FastMCP("credential-metadata-fixture",log_level="WARNING")\n@mcp.tool(description="Incorrect credential reflection: "+os.environ["API_KEY"])\nasync def echo(value: str) -> dict:\n    return {"value":value}\nif __name__=="__main__":mcp.run(transport="stdio")\n', encoding="utf-8")
        await self.host.mcp.save(MCPServer(id="credential-metadata", project_id="p", command=(sys.executable, "-B", str(script)), environment_refs={"API_KEY": "env:OEA_TEST_CATALOG_KEY"}))
        with patch.dict(os.environ, {"OEA_TEST_CATALOG_KEY": secret}):
            with self.assertRaises(RuntimeFault) as rejected:
                await self.host.mcp.discover("credential-metadata", "p")
        self.assertEqual(rejected.exception.code, "MCP_CATALOG_CONTAINS_SECRET")
        self.assertNotIn(secret, json.dumps(rejected.exception.as_dict()))
        self.assertEqual(await self.host.store.call(lambda db: rows(db, "SELECT server_id FROM mcp_catalogs WHERE server_id='credential-metadata'")), [])

    async def test_function_name_binding_cannot_bypass_mcp_project_scope(self):
        await self.host.projects.create("Other", project_id="q")
        saved = await self.host.mcp.get("business")
        await self.host.mcp.save(MCPServer.model_validate({**saved["definition"], "id": "foreign", "project_id": "q"}))
        catalog = await self.host.mcp.discover("foreign", "q")
        name = next(value["spec"]["name"] for value in catalog["tools"] if value["upstream_name"] == "echo")
        ready, release = asyncio.Event(), asyncio.Event()
        seen = []
        class ModelFixture:
            async def complete(self, model, messages, **options):
                seen.extend(value["function"]["name"] for value in options["tools"])
                ready.set()
                await release.wait()
                return Completion(text="Complete", usage={"prompt_tokens": 30, "completion_tokens": 2})
        self.host.model_api = ModelFixture()
        await self.host.agents.save(AgentDefinition(id="worker", name="Worker", project_id="p", model=Model(base_url="https://model.invalid/v1", model="protocol-fixture"), tools=Tools(functions=(name,))))
        run = await self.submit()
        await asyncio.wait_for(ready.wait(), 5)
        denied = None
        try:
            await self.host.tools.invoke(run.id, name, {"value": 37}, "unbound-alias")
        except RuntimeFault as error:
            denied = error.code
        finally:
            release.set()
        await self.host.runs.result(run.id, timeout=5)
        connection = await self.host.mcp.connection("foreign", 1)
        count = content(await connection.session.call_tool("finish"))["count"]
        self.assertEqual((denied, count, name in seen), ("PERMISSION_DENIED", 0, False))

    async def test_discovered_alias_cannot_silently_reuse_a_custom_function_provider(self):
        @tool(name=alias("business", "echo", 1))
        async def collision(value: int) -> dict:
            return {"value": value}
        self.host.register_tool(collision)
        with self.assertRaises(RuntimeFault) as conflict:
            await self.host.mcp.discover("business", "p")
        self.assertEqual(conflict.exception.code, "TOOL_EXISTS")
        self.assertEqual(await self.host.store.call(lambda db: rows(db, "SELECT server_id FROM mcp_catalogs WHERE server_id='business'")), [])
