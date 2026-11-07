"""OAP HTTP and SDK boundaries use the real database, graphs and artifact store."""

import asyncio
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import httpx

from oea import AgentDefinition, ApplicationBinding, ApplicationClient, ApplicationGrant, Instructions, Model, PlatformClient
from oea.contracts.applications import OAP_PREFIX
from oea.contracts.errors import RuntimeFault
from oea.runtime.host import Host
from oea.storage.persistence import one, rows
from oea.web.app import create_app
from test_http import ModelFixture


class OAPHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="oap-http-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.model = ModelFixture()
        self.host = await Host(self.root / "state", model_api=self.model).__aenter__()
        self.addAsyncCleanup(self.host.aclose)
        self.app = create_app(host=self.host)
        self.lifespan = self.app.router.lifespan_context(self.app)
        await self.lifespan.__aenter__()
        self.addAsyncCleanup(self.lifespan.__aexit__, None, None, None)
        self.transport = httpx.ASGITransport(app=self.app)
        self.platform = await PlatformClient("http://127.0.0.1:8899", self.app.state.token, transport=self.transport).__aenter__()
        self.addAsyncCleanup(self.platform.aclose)
        await self.host.projects.create("Example", project_id="p")
        await self.host.agents.save(AgentDefinition(id="worker", name="Worker", project_id="p",
            model=Model(base_url="https://model.invalid", model="fixture"), instructions=Instructions(system="Internal role", agent_md="Private project guide")))
        self.clients, self.registrations, self.conversations = [], [], []
        for name in ("First", "Second"):
            registered = await self.platform.call("applications.create", {"name": name})
            identity = registered["application"]["id"]
            binding = await self.platform.call("applications.bind", {"binding": ApplicationBinding(app_id=identity, project_id="p", agent_id="worker").model_dump(mode="json")})
            client = await ApplicationClient("http://127.0.0.1:8899", registered["credential"], transport=self.transport).__aenter__()
            self.addAsyncCleanup(client.aclose)
            self.clients.append(client)
            self.registrations.append(registered)
            self.conversations.append(await client.create_conversation(binding["id"], idempotency_key="conversation"))

    async def complete(self, index=0, *, key="work", artifact_ids=()):
        client, conversation = self.clients[index], self.conversations[index]
        handle = await client.submit(conversation["id"], "Original application request", idempotency_key=key, artifact_ids=artifact_ids)
        await self.host.runs.result(handle["id"], timeout=10)
        return handle

    async def test_separate_authentication_and_typed_request_identity(self):
        client = self.clients[0]
        agents = await client.agents()
        self.assertEqual(len(agents["items"]), 1)
        self.assertNotIn("credential", json.dumps(agents))
        management = await client.client.post("/api/rpc", json={"protocol_version": "3.0", "operation": "projects.list", "params": {}, "request_id": "forged"})
        self.assertEqual(management.status_code, 401)
        landing = await client.client.get("/")
        self.assertNotIn("set-cookie", landing.headers)
        self.assertEqual((await client.client.post("/api/browser-login")).status_code, 401)
        rejected = await client.client.post("/api/browser-login/redeem", json={"ticket": self.registrations[0]["credential"]})
        self.assertEqual(rejected.status_code, 403)
        self.assertNotIn("set-cookie", rejected.headers)
        async with httpx.AsyncClient(base_url="http://127.0.0.1:8899", transport=self.transport) as raw:
            for headers in ({"Authorization": "Bearer " + self.app.state.token}, {"Cookie": "oea-session-8899=" + self.app.state.token}):
                response = await raw.get(OAP_PREFIX + "/capabilities", headers=headers)
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.json()["protocol_version"], "1.0")
        response = await client.client.post(OAP_PREFIX + "/conversations", headers={"Idempotency-Key": "forged", "X-Request-ID": "request-id"},
            json={"binding_id": agents["items"][0]["id"], "app_id": self.registrations[1]["application"]["id"]})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["request_id"], "request-id")
        mismatch = await client.client.get(OAP_PREFIX + "/capabilities", headers={"X-OAP-Version": "2.0"})
        self.assertEqual(mismatch.status_code, 409)
        remote = httpx.ASGITransport(app=self.app, client=("192.0.2.20", 10000))
        async with httpx.AsyncClient(base_url="http://127.0.0.1:8899", transport=remote) as raw:
            response = await raw.get(OAP_PREFIX + "/capabilities", headers={"Authorization": "Bearer " + self.registrations[0]["credential"]})
            self.assertEqual(response.status_code, 403)

    async def test_acceptance_history_events_and_reconnect_do_not_reexecute(self):
        run = await self.complete()
        client = self.clients[0]
        retry = await client.submit(self.conversations[0]["id"], "Original application request", idempotency_key="work")
        self.assertEqual(retry["id"], run["id"])
        self.assertTrue(retry["reused"])
        with self.assertRaises(RuntimeFault) as fault:
            await client.submit(self.conversations[0]["id"], "Changed input", idempotency_key="work")
        self.assertEqual(fault.exception.code, "IDEMPOTENCY_CONFLICT")
        history = await client.history(self.conversations[0]["id"])
        self.assertIn("Original application request", json.dumps(history))
        self.assertIn("Verified delivery", json.dumps(history))
        self.assertNotIn("Internal role", json.dumps(history))
        self.assertNotIn("Private project guide", json.dumps(history))
        events = [value async for value in client.events(run["id"])]
        positions = [value["position"] for value in events if value["kind"] == "event"]
        self.assertEqual(positions, sorted(set(positions)))
        self.assertEqual(events[-1]["kind"], "run.completed")
        reconnected = [value async for value in client.events(run["id"], after=max(positions))]
        self.assertFalse(any(value["kind"] == "event" for value in reconnected))
        self.assertEqual(self.model.calls, 1)
        self.assertEqual((await client.result(run["id"]))["text"], "Verified delivery")
        page = await client.result(run["id"], offset=3, limit=4)
        self.assertEqual(page["text"], "ifie")
        self.assertEqual(page["content_view"]["next_offset"], 7)

    async def test_large_upload_hash_range_download_and_managed_assignment(self):
        client = self.clients[0]
        data = b"bounded input\n" * 180000
        digest = hashlib.sha256(data).hexdigest()
        upload = await client.begin_upload(self.conversations[0]["id"], "input.txt", len(data), digest, idempotency_key="upload")
        async def chunks():
            for offset in range(0, len(data), 65536):
                yield data[offset:offset + 65536]
        completed = await client.upload_content(upload["id"], chunks())
        self.assertEqual(completed["state"], "completed")
        committed = await client.commit_upload(upload["id"], idempotency_key="commit")
        self.assertEqual(committed["artifact_id"], upload["id"])
        run = await self.complete(artifact_ids=(upload["id"],))
        destination = await client.download(upload["id"], self.root / "download.txt")
        self.assertEqual(hashlib.sha256(destination.read_bytes()).hexdigest(), digest)
        partial = await client.client.get(OAP_PREFIX + "/artifacts/" + upload["id"] + "/content", headers={"Range": "bytes=5-19"})
        self.assertEqual(partial.status_code, 206)
        self.assertEqual(partial.content, data[5:20])
        instance = await self.host.instances.get(run["instance_id"])
        inputs = Path(instance["workspace"]["input_dirs"][0])
        self.assertEqual((inputs / upload["id"] / "input.txt").read_bytes(), data)
        with self.assertRaises(RuntimeFault):
            await self.clients[1].upload_status(upload["id"])
        cross = await self.clients[1].client.get(OAP_PREFIX + "/artifacts/" + upload["id"] + "/content")
        self.assertEqual(cross.status_code, 403)
        self.assertEqual((await self.platform.call("artifacts.list", {"session_id": None, "project_id": "p"}))["items"], [])
        for operation, params in (("artifacts.get", {"artifact_id": upload["id"]}),
                                  ("artifacts.get_upload", {"upload_id": upload["id"], "project_id": "p"})):
            with self.assertRaises(RuntimeFault) as denied:
                await self.platform.call(operation, params)
            self.assertEqual(denied.exception.code, "PERMISSION_DENIED")
        public_download = await self.platform.client.get("/api/artifacts/" + upload["id"] + "/content", params={"project_id": "p"})
        self.assertEqual(public_download.status_code, 403)

    async def test_platform_lists_and_guessed_objects_cannot_read_application_work(self):
        run = await self.complete()
        instance = await self.host.instances.get(run["instance_id"])
        session_id = instance["session_id"]
        self.assertEqual((await self.platform.call("sessions.list", {"project_id": "p"}))["items"], [])
        self.assertEqual((await self.platform.call("events.list", {"project_id": "p"}))["items"][-1]["kind"], "application.binding_saved")
        private = await self.host.store.call(lambda db: rows(db, "SELECT position FROM events WHERE source='application'"))
        public = await self.platform.call("events.list", {"project_id": "p", "limit": 250})
        self.assertFalse({row["position"] for row in private} & {row["position"] for row in public["items"]})
        checks = [("runs.get", {"run_id": run["id"]}), ("runs.result", {"run_id": run["id"]}),
                  ("instances.get", {"instance_id": run["instance_id"]}), ("sessions.get", {"session_id": session_id}),
                  ("instances.history", {"instance_id": run["instance_id"], "project_id": "p"}),
                  ("sessions.unread", {"session_id": session_id, "project_id": "p"}),
                  ("budgets.get", {"budget_id": run["budget_id"]}),
                  ("runs.submit", {"spec": {"instance_id": run["instance_id"], "input": "forged", "idempotency_key": "forged"}})]
        for operation, params in checks:
            with self.subTest(operation=operation), self.assertRaises(RuntimeFault) as fault:
                await self.platform.call(operation, params)
            self.assertEqual(fault.exception.code, "PERMISSION_DENIED")
        with self.assertRaises(RuntimeFault):
            await self.clients[1].run(run["id"])
        response = await self.platform.client.get("/api/events", params={"project_id": "p", "run_id": run["id"]})
        self.assertEqual(response.status_code, 403)
        command = await self.platform.client.get("/api/commands/unknown/stdout", params={"project_id": "p", "run_id": run["id"]})
        self.assertEqual(command.status_code, 403)
        history = await self.host.instances.history(run["instance_id"], "p")
        reference = await self.host.results.issue("instances.history", {"instance_id": run["instance_id"], "project_id": "p"}, history, self.platform.consumer_id, characters=len(json.dumps(history)))
        with self.assertRaises(RuntimeFault) as denied:
            await self.platform.call("results.read", {"result_ref": reference["result_ref"], "project_id": "p"})
        self.assertEqual(denied.exception.code, "PERMISSION_DENIED")

    async def test_project_deletion_removes_only_its_application_bindings_and_facts(self):
        first, second = await self.complete(0), await self.complete(1)
        await self.host.projects.create("Other", project_id="q")
        await self.host.agents.save(AgentDefinition(id="other", name="Other", project_id="q",
            model=Model(base_url="https://model.invalid", model="fixture")))
        app_id = self.registrations[0]["application"]["id"]
        binding = await self.host.applications.bind(ApplicationBinding(app_id=app_id, project_id="q", agent_id="other"))
        conversation = await self.clients[0].create_conversation(binding["id"], idempotency_key="other-conversation")
        retained = await self.clients[0].submit(conversation["id"], "Retained work", idempotency_key="other-work")
        await self.host.runs.result(retained["id"], timeout=10)
        receipt = await self.platform.call("projects.delete", {"project_id": "p", "confirm_name": "Example", "idempotency_key": "delete"})
        self.assertTrue(receipt["deleted"])
        self.assertEqual((await self.clients[0].agents())["items"][0]["id"], binding["id"])
        self.assertEqual([item["id"] for item in (await self.clients[0].conversations())["items"]], [conversation["id"]])
        self.assertEqual((await self.clients[0].result(retained["id"]))["text"], "Verified delivery")
        self.assertEqual((await self.clients[1].agents())["items"], [])
        self.assertEqual((await self.clients[1].conversations())["items"], [])
        for client, run in zip(self.clients, (first, second)):
            with self.assertRaises(RuntimeFault) as denied:
                await client.run(run["id"])
            self.assertEqual(denied.exception.code, "PERMISSION_DENIED")
        self.assertEqual(await self.host.applications.authenticate(self.registrations[0]["credential"]), app_id)
        self.assertEqual(await self.host.store.call(lambda db: list(db.execute("PRAGMA foreign_key_check"))), [])
        remaining = await self.host.store.call(lambda db: rows(db, "SELECT target FROM application_requests"))
        self.assertEqual({item["target"] for item in remaining}, {binding["id"], conversation["id"]})

    async def test_interrupted_and_invalid_uploads_reuse_the_original_intent(self):
        client, conversation = self.clients[0], self.conversations[0]
        data = b"original upload bytes"
        upload = await client.begin_upload(conversation["id"], "input.txt", len(data), hashlib.sha256(data).hexdigest(), idempotency_key="upload")
        async def interrupted():
            yield data[:7]
            raise ConnectionError("Synthetic upload disconnect")
        interrupted_seen = False
        try:
            await client.upload_content(upload["id"], interrupted())
        except* ConnectionError:
            interrupted_seen = True
        self.assertTrue(interrupted_seen)
        self.assertEqual((await client.upload_status(upload["id"]))["state"], "accepted")
        self.assertEqual(list((self.host.root / "artifacts").iterdir()), [])
        with self.assertRaises(RuntimeFault) as incomplete:
            await client.commit_upload(upload["id"], idempotency_key="commit")
        self.assertEqual(incomplete.exception.code, "UPLOAD_INCOMPLETE")
        async def wrong():
            yield b"x" * len(data)
        with self.assertRaises(RuntimeFault) as mismatch:
            await client.upload_content(upload["id"], wrong())
        self.assertEqual(mismatch.exception.code, "UPLOAD_CONTENT_MISMATCH")
        async def complete():
            yield data
        await client.upload_content(upload["id"], complete())
        again = await client.begin_upload(conversation["id"], "input.txt", len(data), hashlib.sha256(data).hexdigest(), idempotency_key="upload")
        self.assertEqual(again["id"], upload["id"])
        for _ in range(2):
            self.assertEqual((await client.commit_upload(upload["id"], idempotency_key="commit"))["artifact_id"], upload["id"])
        counts = await self.host.store.call(lambda db: one(db,
            "SELECT (SELECT COUNT(*) FROM artifacts) AS artifacts,(SELECT COUNT(*) FROM events WHERE kind='artifact.published') AS published,(SELECT COUNT(*) FROM runs) AS runs"))
        self.assertEqual(counts, {"artifacts": 1, "published": 1, "runs": 0})

    async def test_inputs_cannot_move_between_conversations_or_change_after_assignment(self):
        client, conversation = self.clients[0], self.conversations[0]
        data = b"assigned input"
        upload = await client.begin_upload(conversation["id"], "input.txt", len(data), hashlib.sha256(data).hexdigest(), idempotency_key="upload")
        async def chunks():
            yield data
        await client.upload_content(upload["id"], chunks())
        other = await client.create_conversation(conversation["binding_id"], idempotency_key="other-conversation")
        for owner, target in ((client, other), (self.clients[1], self.conversations[1])):
            with self.assertRaises(RuntimeFault) as denied:
                await owner.submit(target["id"], "Use input", idempotency_key="forged", artifact_ids=(upload["id"],))
            self.assertEqual(denied.exception.code, "PERMISSION_DENIED")
        run = await self.complete(artifact_ids=(upload["id"],))
        instance = await self.host.instances.get(run["instance_id"])
        copy = Path(instance["workspace"]["input_dirs"][0]) / upload["id"] / "input.txt"
        copy.write_bytes(b"changed bytes")
        with self.assertRaises(RuntimeFault) as changed:
            await client.submit(conversation["id"], "Use input again", idempotency_key="next", artifact_ids=(upload["id"],))
        self.assertEqual(changed.exception.code, "ARTIFACT_CHANGED")
        downloaded = await client.download(upload["id"], self.root / "original.txt")
        self.assertEqual(downloaded.read_bytes(), data)

    async def test_active_stream_observes_rotation_without_replaying_model(self):
        from test_host import BarrierModel
        from oea.web.oap import progress
        model = self.host.model_api = BarrierModel()
        client = self.clients[0]
        run = await client.submit(self.conversations[0]["id"], "Wait", idempotency_key="wait")
        await asyncio.wait_for(model.ready.wait(), 5)
        stream = progress(self.host, self.registrations[0]["application"]["id"], run["id"], 1)
        self.assertEqual((await anext(stream))["kind"], "run.snapshot")
        await self.host.applications.rotate_credential(self.registrations[0]["application"]["id"], 1)
        with self.assertRaises(RuntimeFault):
            await anext(stream)
        await stream.aclose()
        self.assertNotIn(run["id"], self.host.subscribers)
        with self.assertRaises(RuntimeFault):
            await client.run(run["id"])
        model.release.set()
        await self.host.runs.result(run["id"], timeout=5)
        self.assertEqual(model.calls, 1)

    async def test_json_boundaries_reject_malformed_unicode_nonfinite_and_oversized_inputs(self):
        client = self.clients[0]
        path = OAP_PREFIX + "/conversations/" + self.conversations[0]["id"] + "/runs"
        for body in (b'{"input":NaN}', b'{"input":"\\ud800"}', b'{"input":"\xff"}', b'{"input":"x","instance_id":"forged"}'):
            response = await client.client.post(path, content=body, headers={"Content-Type": "application/json", "Idempotency-Key": "invalid"})
            self.assertEqual(response.status_code, 400, response.text)
        response = await client.client.post(path, json={"input": "x" * (1024 * 1024 + 1)}, headers={"Idempotency-Key": "large-input"})
        self.assertEqual(response.status_code, 400)
        response = await client.client.post(path, content=b" " * (2 * 1024 * 1024 + 1), headers={"Content-Type": "application/json", "Idempotency-Key": "large-json"})
        self.assertEqual(response.status_code, 413)
        self.assertEqual((await self.host.store.call(lambda db: one(db, "SELECT COUNT(*) AS n FROM runs")))["n"], 0)
        run = await client.submit(self.conversations[0]["id"], "你好 🌍", idempotency_key="unicode")
        await self.host.runs.result(run["id"], timeout=10)
        self.assertEqual((await client.history(self.conversations[0]["id"]))["items"][0]["body"]["content"], "你好 🌍")

    async def test_history_boundary_and_action_ownership_exclude_private_context(self):
        run = await self.complete()
        client, conversation = self.clients[0], self.conversations[0]
        original, instance = await self.host.store.call(lambda db: (one(db, "SELECT * FROM runs WHERE id=?", (run["id"],)), one(db, "SELECT * FROM instances WHERE id=?", (run["instance_id"],))))
        await self.host.contexts.append(original, instance, {"role": "user", "content": "Private runtime instruction"}, run["id"] + ":internal")
        first = await client.history(conversation["id"], limit=1)
        await self.complete(key="second")
        rest = await client.history(conversation["id"], after=first["cursor"], version=first["version"])
        self.assertEqual(len(rest["items"]), 1)
        latest = await client.history(conversation["id"])
        self.assertNotIn("Private runtime instruction", json.dumps(latest))
        self.assertGreater(latest["version"], first["version"])
        with self.assertRaises(RuntimeFault):
            await self.clients[1].history(self.conversations[1]["id"], version=first["version"])
        other = await self.complete(1)
        action = (await self.clients[1].actions(other["id"]))["items"][0]
        with self.assertRaises(RuntimeFault) as failure:
            await client.action(run["id"], action["id"])
        self.assertEqual(failure.exception.code, "PERMISSION_DENIED")
        with self.assertRaises(RuntimeFault) as failure:
            await client.confirm_action(run["id"], action["id"], idempotency_key="forged", evidence="Foreign action", outcome="completed", result={})
        self.assertEqual(failure.exception.code, "PERMISSION_DENIED")

    async def test_exact_json_and_input_size_boundaries_are_accepted(self):
        client = self.clients[0]
        request = json.dumps({"binding_id": self.conversations[0]["binding_id"]}).encode()
        request += b" " * (2 * 1024 * 1024 - len(request))
        response = await client.client.post(OAP_PREFIX + "/conversations", content=request,
            headers={"Content-Type": "application/json", "Idempotency-Key": "maximum-json"})
        self.assertEqual(response.status_code, 200)
        conversation = response.json()["result"]
        payload = "x" * (1024 * 1024)
        run = await client.submit(conversation["id"], payload, idempotency_key="maximum-input")
        saved = await self.host.store.call(lambda db: one(db, "SELECT input_json FROM runs WHERE id=?", (run["id"],)))
        self.assertEqual(json.loads(saved["input_json"]), payload)
        await client.cancel(run["id"], idempotency_key="finish-boundary-test")
