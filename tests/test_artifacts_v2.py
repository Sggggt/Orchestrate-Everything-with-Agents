"""Explicit non-execution artifact origins and crash-safe streamed uploads."""

import asyncio
import base64
import hashlib
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from oea import Agent, AgentDefinition, Artifact, Completion, Model, RunSpec, Tools
from oea.contracts.errors import RuntimeFault
from oea.runtime.host import Host
from oea.storage.persistence import one


class UploadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="oea-uploads-")
        self.root = Path(self.temporary.name)
        self.host = await Host(self.root / "state").__aenter__()
        await self.host.projects.create("Project", project_id="p")
        await self.host.projects.create("Other", project_id="q")
        self.session = await self.host.sessions.create("p")

    async def asyncTearDown(self):
        await self.host.aclose()
        self.temporary.cleanup()

    async def accept(self, body, key="upload"):
        return await self.host.artifacts.begin_upload("p", "evidence.txt", len(body), hashlib.sha256(body).hexdigest(), key,
            session_id=self.session["id"], consumer_id="controller")

    async def stream(self, identity, body):
        async def chunks():
            for offset in range(0, len(body), 257):
                yield body[offset:offset + 257]
        return await self.host.artifacts.write_upload(identity, "p", chunks(), consumer_id="controller")

    async def test_concurrent_same_upload_has_one_artifact_and_no_execution_owner(self):
        body = ("Original evidence.\n" * 200).encode("utf-8")
        accepted = await asyncio.gather(*(self.accept(body) for _ in range(32)))
        identities = {value["id"] for value in accepted}
        self.assertEqual(len(identities), 1)
        identity = identities.pop()
        receipts = await asyncio.gather(*(self.stream(identity, body) for _ in range(8)))
        self.assertTrue(all(value["state"] == "completed" for value in receipts))
        artifact = Artifact.model_validate(await self.host.artifacts.get(identity))
        self.assertEqual(artifact.origin, "upload")
        self.assertIsNone(artifact.run_id)
        self.assertIsNone(artifact.instance_id)
        self.assertEqual(artifact.session_id, self.session["id"])
        self.assertEqual((await self.host.artifacts.read(identity, "p"))["text"].encode("utf-8"), body)
        page = await self.host.artifacts.list(self.session["id"], "p")
        self.assertEqual([item["id"] for item in page["items"]], [identity])
        self.assertIsNone(page["items"][0]["agent_id"])
        self.assertEqual((await self.host.sessions.summary(self.session["id"], "p"))["artifacts"], 1)
        for table in ("instances", "runs", "usage", "budgets"):
            self.assertEqual(await self.host.store.call(lambda db: one(db, "SELECT COUNT(*) AS count FROM " + table)["count"]), 0)
        self.assertEqual(await self.host.store.call(lambda db: one(db, "SELECT COUNT(*) AS count FROM events WHERE kind='artifact.published'")["count"]), 1)
        self.assertEqual(len(list((self.host.root / "artifacts").iterdir())), 1)

    async def test_bad_content_and_spoofed_consumer_create_no_artifact(self):
        receipt = await self.accept(b"original")
        with self.assertRaises(RuntimeFault) as wrong:
            await self.stream(receipt["id"], b"modified")
        self.assertEqual(wrong.exception.code, "UPLOAD_CONTENT_MISMATCH")
        self.assertEqual((await self.host.artifacts.list(None, "p"))["items"], [])
        self.assertEqual(list((self.host.root / "artifacts").iterdir()), [])
        for project, consumer in (("q", "controller"), ("p", "another")):
            with self.assertRaises(RuntimeFault):
                await self.host.artifacts.get_upload(receipt["id"], project, consumer_id=consumer)
        with self.assertRaises(RuntimeFault) as conflicting:
            await self.accept(b"different")
        self.assertEqual(conflicting.exception.code, "IDEMPOTENCY_CONFLICT")
        await self.stream(receipt["id"], b"original")
        self.assertEqual((await self.accept(b"original"))["state"], "completed")

    async def test_restart_after_complete_file_before_commit_verifies_original_without_resend(self):
        body = "保留已接收的原文件。".encode("utf-8")
        receipt = await self.accept(body)
        with patch.object(self.host.artifacts, "_commit_upload", side_effect=RuntimeError("Interrupted after atomic file receipt")):
            with self.assertRaises(RuntimeError):
                await self.stream(receipt["id"], body)
        original = self.host.root / "artifacts" / receipt["id"]
        self.assertEqual(original.read_bytes(), body)
        stamp = original.stat().st_mtime_ns
        await self.host.aclose()
        self.host = await Host(self.root / "state").__aenter__()
        self.assertEqual((await self.host.artifacts.get_upload(receipt["id"], "p", consumer_id="controller"))["state"], "accepted")
        recovered = await self.host.artifacts.commit_upload(receipt["id"], "p", consumer_id="controller")
        self.assertEqual(recovered["state"], "completed")
        self.assertEqual(original.stat().st_mtime_ns, stamp)
        replay = await self.host.artifacts.commit_upload(receipt["id"], "p", consumer_id="controller")
        self.assertEqual(replay, recovered)
        self.assertEqual(len((await self.host.artifacts.list(None, "p"))["items"]), 1)

    async def test_missing_file_cannot_be_confirmed_and_invalid_origin_has_no_execution_owner(self):
        receipt = await self.accept(b"absent")
        with self.assertRaises(RuntimeFault) as incomplete:
            await self.host.artifacts.commit_upload(receipt["id"], "p", consumer_id="controller")
        self.assertEqual(incomplete.exception.code, "UPLOAD_INCOMPLETE")
        with self.assertRaises(ValueError):
            Artifact(id="invalid", name="invalid", project_id="p", sha256="a" * 64, bytes=0, media_type="text/plain", origin="run")

    async def test_close_stops_owned_partial_receiver_and_keeps_acceptance_for_explicit_continuation(self):
        body = b"complete body"
        receipt = await self.accept(body)
        waiting = asyncio.Event()
        async def partial():
            yield body[:3]
            waiting.set()
            await asyncio.Event().wait()
        task = asyncio.create_task(self.host.artifacts.write_upload(receipt["id"], "p", partial(), consumer_id="controller"))
        await asyncio.wait_for(waiting.wait(), 5)
        self.assertEqual((await self.host.status())["active_artifact_uploads"], 1)
        await self.host.aclose()
        self.assertTrue(task.cancelled())
        self.assertEqual(self.host.artifacts.receiving, set())
        self.assertEqual(list((self.host.root / "artifacts").iterdir()), [])
        self.host = await Host(self.root / "state").__aenter__()
        original = await self.host.artifacts.get_upload(receipt["id"], "p", consumer_id="controller")
        self.assertEqual(original["state"], "accepted")
        self.assertEqual((await self.stream(receipt["id"], body))["state"], "completed")

    async def test_search_and_cursor_keep_uploaded_sources_in_their_session(self):
        for index, name in enumerate(("first.md", "second.md", "third.txt")):
            body = str(index).encode("utf-8")
            receipt = await self.host.artifacts.begin_upload("p", name, len(body), hashlib.sha256(body).hexdigest(), "file-" + str(index),
                session_id=self.session["id"], consumer_id="controller")
            await self.stream(receipt["id"], body)
        first = await self.host.artifacts.list(self.session["id"], "p", limit=1, query=".md")
        self.assertEqual([item["name"] for item in first["items"]], ["second.md"])
        second = await self.host.artifacts.list(self.session["id"], "p", limit=1, query=".md", cursor=first["cursor"])
        self.assertEqual([item["name"] for item in second["items"]], ["first.md"])
        self.assertIsNone(second["cursor"])
        self.assertEqual((await self.host.artifacts.list(None, "q"))["items"], [])

    async def configure_reader(self):
        await self.host.agents.save(AgentDefinition(id="reader", name="Reader",
            model=Model(base_url="https://model.invalid/v1", model="protocol-fixture")))

    async def test_sdk_assignment_exposes_metadata_then_reads_only_explicitly_assigned_text(self):
        body = b"The retained source has the expected evidence."
        upload = await self.accept(body)
        await self.stream(upload["id"], body)
        await self.configure_reader()
        systems, results = [], []
        class ReaderModel:
            async def complete(self, model, messages, **options):
                systems.append(messages[0]["content"])
                loaded = [message["content"] for message in messages if message["role"] == "tool"]
                if loaded:
                    results.append(loaded[-1])
                    return Completion(text="Read inspected", usage={"prompt_tokens": 50, "completion_tokens": 4})
                return Completion(tool_calls=({"id": "read", "type": "function", "function": {"name": "read_artifact",
                    "arguments": json.dumps({"artifact_id": upload["id"]})}},), finish_reason="tool_calls",
                    usage={"prompt_tokens": 50, "completion_tokens": 4})
        self.host.model_api = ReaderModel()
        async with Agent.from_saved("reader", host=self.host, session_id=self.session["id"]) as agent:
            result = await agent.arun("Read the assigned source", artifact_ids=(upload["id"],))
        self.assertEqual(result.text, "Read inspected")
        self.assertIn(upload["id"], systems[0])
        self.assertNotIn(body.decode(), systems[0])
        self.assertIn(body.decode(), results[0])
        other = await self.host.instances.create(self.session["id"], "reader", name="Unassigned")
        run = await self.host.runs.submit(RunSpec(instance_id=other["id"], input="This mention grants nothing: " + upload["id"], idempotency_key="unassigned"))
        await self.host.runs.result(run.id, timeout=5)
        self.assertIn("PERMISSION_DENIED", results[1])
        self.assertNotIn(body.decode(), results[1])
        self.assertEqual(await self.host.store.call(lambda db: one(db, "SELECT COUNT(*) AS count FROM run_artifacts")["count"]), 1)

    async def test_cross_session_and_project_assignments_roll_back_run_and_budget(self):
        upload = await self.accept(b"source")
        await self.stream(upload["id"], b"source")
        await self.configure_reader()
        for project in ("p", "q"):
            session = await self.host.sessions.create(project)
            instance = await self.host.instances.create(session["id"], "reader")
            with self.assertRaises(RuntimeFault) as denied:
                await self.host.runs.submit(RunSpec(instance_id=instance["id"], input="Not assigned here", artifact_ids=(upload["id"],), idempotency_key="wrong-scope"))
            self.assertEqual(denied.exception.code, "PERMISSION_DENIED")
        for table in ("runs", "budgets", "actions", "run_artifacts"):
            self.assertEqual(await self.host.store.call(lambda db: one(db, "SELECT COUNT(*) AS count FROM " + table)["count"]), 0)

    async def test_child_forwarding_requires_parent_read_access_and_preserves_root_budget(self):
        sources = []
        for key in ("assigned", "unassigned"):
            body = key.encode()
            receipt = await self.accept(body, key)
            await self.stream(receipt["id"], body)
            sources.append(receipt["id"])
        await self.configure_reader()
        class CompleteModel:
            async def complete(self, *args, **options):
                return Completion(text="Complete", usage={"prompt_tokens": 40, "completion_tokens": 3})
        self.host.model_api = CompleteModel()
        parent = await self.host.instances.create(self.session["id"], "reader", name="Parent")
        child = await self.host.instances.create(self.session["id"], "reader", name="Child")
        root = await self.host.runs.submit(RunSpec(instance_id=parent["id"], input="Coordinate", artifact_ids=(sources[0],), idempotency_key="root"))
        await self.host.runs.result(root.id, timeout=5)
        with self.assertRaises(RuntimeFault):
            await self.host.runs.submit(RunSpec(instance_id=child["id"], input="Cannot expand source access", artifact_ids=(sources[1],), parent_run_id=root.id, idempotency_key="denied-child"))
        accepted = await self.host.runs.submit(RunSpec(instance_id=child["id"], input="Use the accepted source", artifact_ids=(sources[0],), parent_run_id=root.id, idempotency_key="child"))
        await self.host.runs.result(accepted.id, timeout=5)
        self.assertEqual(accepted.budget_id, root.budget_id)
        self.assertEqual(accepted.root_run_id, root.id)
        self.assertEqual(await self.host.store.call(lambda db: one(db, "SELECT COUNT(*) AS count FROM budgets")["count"]), 1)

    async def test_assigned_image_uses_original_upload_and_encodes_only_at_model_boundary(self):
        from PIL import Image
        buffer = BytesIO()
        Image.new("RGB", (8, 8), "green").save(buffer, format="PNG")
        body = buffer.getvalue()
        upload = await self.host.artifacts.begin_upload("p", "evidence.png", len(body), hashlib.sha256(body).hexdigest(), "image",
            session_id=self.session["id"], consumer_id="controller")
        await self.stream(upload["id"], body)
        await self.host.agents.save(AgentDefinition(id="reader", name="Reader", tools=Tools(local=("read_image",)),
            model=Model(base_url="https://model.invalid/v1", model="protocol-fixture")))
        images, observed = [], []
        class ImageModel:
            async def complete(self, model, messages, **options):
                for message in messages:
                    if isinstance(message.get("content"), list):
                        images.extend(part["image_url"]["url"] for part in message["content"] if part.get("type") == "image_url")
                tools = [message["content"] for message in messages if message["role"] == "tool"]
                if tools:
                    observed.append(tools[-1])
                    return Completion(text="Image inspected", usage={"prompt_tokens": 50, "completion_tokens": 4})
                return Completion(tool_calls=({"id": "image", "type": "function", "function": {"name": "read_image",
                    "arguments": json.dumps({"artifact_id": upload["id"]})}},), finish_reason="tool_calls",
                    usage={"prompt_tokens": 50, "completion_tokens": 4})
        self.host.model_api = ImageModel()
        async with Agent.from_saved("reader", host=self.host, session_id=self.session["id"]) as agent:
            self.assertEqual((await agent.arun("Inspect the assigned image", artifact_ids=(upload["id"],))).text, "Image inspected")
        self.assertEqual(len(images), 1)
        self.assertEqual(base64.b64decode(images[0].partition(",")[2]), body)
        self.assertIn('"origin":"upload"', observed[0])
        self.assertEqual(len((await self.host.artifacts.list(None, "p"))["items"]), 1)
        for table, column in (("actions", "result_json"), ("conversation", "body_json"), ("context_sources", "body")):
            self.assertEqual(await self.host.store.call(lambda db: one(db, "SELECT COUNT(*) AS count FROM " + table + " WHERE " + column + " LIKE '%base64,%'")["count"]), 0)
        images.clear()
        unassigned = await self.host.instances.create(self.session["id"], "reader", name="Unassigned image reader")
        denied = await self.host.runs.submit(RunSpec(instance_id=unassigned["id"], input="An ID is not an image grant", idempotency_key="unassigned-image"))
        await self.host.runs.result(denied.id, timeout=5)
        self.assertIn("PERMISSION_DENIED", observed[1])
        self.assertEqual(images, [])
