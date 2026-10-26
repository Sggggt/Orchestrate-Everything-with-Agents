"""The existing-host client and browser gateway use the same v2 services."""

import json
import hashlib
import tempfile
import unittest
from pathlib import Path

import httpx

from oea import Agent, AgentDefinition, Completion, Model, PlatformClient, RunSpec
from oea.contracts.errors import RuntimeFault
from oea.runtime.host import Host
from oea.web.app import create_app


class ModelFixture:
    def __init__(self):
        self.calls = 0

    async def complete(self, model, messages, **options):
        self.calls += 1
        return Completion(text="Verified delivery", usage={"prompt_tokens": 40, "completion_tokens": 4})


class HTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="oea-http-")
        self.root = Path(self.temporary.name)
        self.model = ModelFixture()
        self.host = await Host(self.root / "state", model_api=self.model).__aenter__()
        self.app = create_app(host=self.host, client_url="http://127.0.0.1:8899")
        self.lifespan = self.app.router.lifespan_context(self.app)
        await self.lifespan.__aenter__()
        self.transport = httpx.ASGITransport(app=self.app)
        self.client = await PlatformClient("http://127.0.0.1:8899", self.app.state.token, transport=self.transport).__aenter__()

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.lifespan.__aexit__(None, None, None)
        await self.host.aclose()
        self.temporary.cleanup()

    async def seed(self):
        await self.client.call("projects.create", {"name": "Project", "project_id": "p"})
        definition = AgentDefinition(id="review", name="Review", project_id="p", model=Model(base_url="https://example.invalid/v1", model="protocol-fixture"))
        await self.client.call("agents.save", {"definition": definition.model_dump(mode="json")})
        session = await self.client.call("sessions.create", {"project_id": "p"})
        instance = await self.client.call("instances.create", {"session_id": session["id"], "agent_id": "review"})
        return session, instance

    async def test_streamed_upload_exceeds_rpc_body_limit_and_keeps_original_source(self):
        await self.client.call("projects.create", {"name": "Project", "project_id": "p"})
        session = await self.client.call("sessions.create", {"project_id": "p"})
        source = self.root / "中文 原文件.txt"
        body = ("bounded evidence\n" * 200000).encode("utf-8")
        source.write_bytes(body)
        digest = hashlib.sha256(body).hexdigest()
        uploaded = await self.client.upload_artifact(source, project_id="p", session_id=session["id"], idempotency_key="selected-file")
        self.assertGreater(uploaded["bytes"], 2 * 1024 * 1024)
        self.assertEqual(uploaded["sha256"], digest)
        self.assertEqual(uploaded["origin"], "upload")
        self.assertIsNone(uploaded["run_id"])
        self.assertIsNone(uploaded["instance_id"])
        replay = await self.client.upload_artifact(source, project_id="p", session_id=session["id"], idempotency_key="selected-file")
        self.assertEqual(replay["id"], uploaded["id"])
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), digest)
        response = await self.client.client.get("/api/artifacts/" + uploaded["id"] + "/content", params={"project_id": "p"})
        self.assertEqual(response.content, body)
        spoofed = await self.client.client.put("/api/artifact-uploads/" + uploaded["id"] + "/content", params={"project_id": "p"},
            headers={"X-OEA-Consumer-ID": "another-consumer"}, content=body)
        self.assertEqual(spoofed.status_code, 403)
        self.assertEqual(self.model.calls, 0)
        self.assertEqual((await self.client.call("instances.list", {"session_id": session["id"]}))["items"], [])

    async def test_sdk_client_receipts_and_http_errors_match_local_service(self):
        session, instance = await self.seed()
        run = await self.client.call("runs.submit", {"spec": RunSpec(instance_id=instance["id"], input="Deliver", idempotency_key="intent").model_dump(mode="json")})
        result = await self.client.call("runs.result", {"run_id": run["id"], "timeout": 10})
        self.assertEqual(result["text"], "Verified delivery")
        replay = await self.client.call("runs.submit", {"spec": {"instance_id": instance["id"], "input": "Deliver", "idempotency_key": "intent"}})
        self.assertTrue(replay["reused"])
        self.assertEqual(self.model.calls, 1)
        with self.assertRaises(RuntimeFault) as error:
            await self.client.call("removed.operation", {})
        self.assertEqual(error.exception.code, "METHOD_NOT_FOUND")
        with self.assertRaises(RuntimeFault) as local_error:
            await self.host.registry.call("removed.operation", {})
        self.assertEqual(error.exception.as_dict()["diagnostic"], local_error.exception.as_dict()["diagnostic"])
        listing = await self.client.call("runs.list", {"session_id": session["id"], "project_id": "p"})
        self.assertEqual([item["id"] for item in listing["items"]], [run["id"]])

    async def test_agent_from_saved_executes_rpc_configuration_without_a_save(self):
        session, _ = await self.seed()
        before = await self.client.call("agents.get", {"agent_id": "review"})
        async with Agent.from_saved("review", client=self.client, session_id=session["id"]) as agent:
            result = await agent.arun("Use saved configuration", idempotency_key="saved-sdk")
        self.assertEqual(result.text, "Verified delivery")
        self.assertEqual(await self.client.call("agents.get", {"agent_id": "review"}), before)
        self.assertTrue(self.client.opened)
        self.assertTrue(self.host.opened)

    async def test_auth_same_origin_and_explicit_protocol_reject_invalid_clients(self):
        async with httpx.AsyncClient(transport=self.transport, base_url=self.client.url) as raw:
            self.assertEqual((await raw.get("/api/protocol")).status_code, 401)
            self.assertEqual((await raw.get("/api/protocol", headers={"Authorization": "Bearer " + self.app.state.token, "Origin": "https://example.invalid"})).status_code, 403)
            self.assertEqual((await raw.get("/api/protocol", headers={"Authorization": "Bearer " + self.app.state.token, "Host": "example.invalid"})).status_code, 403)
            self.assertEqual((await raw.get("/api/protocol", headers={"Authorization": "Bearer " + self.app.state.token, "X-OEA-Protocol-Version": "1.0"})).status_code, 409)
            index = await raw.get("/")
            self.assertEqual(index.status_code, 200)
            self.assertIn("HttpOnly", index.headers["set-cookie"])
            self.assertIn("frame-ancestors 'none'", index.headers["content-security-policy"])
            self.assertEqual((await raw.get("/api/protocol")).status_code, 200)

    async def test_sse_reconnect_reads_saved_cursor_without_reexecuting_work(self):
        session, instance = await self.seed()
        run = await self.client.call("runs.submit", {"spec": {"instance_id": instance["id"], "input": "Deliver", "idempotency_key": "intent"}})
        await self.client.call("runs.result", {"run_id": run["id"], "timeout": 10})
        first = [frame async for frame in self.client.events(run["id"])]
        facts = [frame for frame in first if frame["kind"] == "event"]
        self.assertTrue(any(frame["event"]["kind"] == "model.completed" for frame in facts))
        cursor = max(frame["position"] for frame in facts)
        second = [frame async for frame in self.client.events(run["id"], after=cursor)]
        self.assertFalse(any(frame["kind"] == "event" for frame in second))
        self.assertEqual(second[-1]["result"]["text"], "Verified delivery")
        self.assertEqual(self.model.calls, 1)
        local = await self.host.events_service.list("p", session_id=session["id"], run_id=run["id"])
        self.assertEqual(local["position"], cursor)

    async def test_model_secret_is_write_only_and_rotates_the_configuration_reference(self):
        await self.seed()
        value = "test-only-value-" + "x" * 24
        result = await self.client.call("agents.set_credential", {"agent_id": "review", "project_id": "p", "role": "main", "value": value, "expected_revision": 1})
        saved = await self.client.call("agents.get", {"agent_id": "review"})
        self.assertTrue(result["configured"])
        self.assertNotIn(value, json.dumps(saved))
        self.assertNotIn(value, json.dumps(result))
        self.assertEqual(self.host.secret(result["credential_ref"]), value)
        changed = await self.client.call("agents.set_credential", {"agent_id": "review", "project_id": "p", "role": "main", "value": value + "y", "expected_revision": 2})
        self.assertNotEqual(changed["credential_ref"], result["credential_ref"])
        events = await self.client.call("events.list", {"project_id": "p"})
        self.assertNotIn(value, json.dumps(events))
