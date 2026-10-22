"""A2A mapping preserves local ownership, explicit intent and terminal evidence."""

import asyncio
import tempfile
import unittest
import os
import importlib.util
from pathlib import Path

from oea import Runtime, RuntimeFault, AgentConfig, AgentDescriptor
from oea.a2a import Message, Part, task, artifact, agent_card
from test_channels import ChannelWorker


class A2ATests(unittest.IsolatedAsyncioTestCase):
    @unittest.skipUnless(os.environ.get("OEA_A2A_SCHEMA_DIR"), "Set OEA_A2A_SCHEMA_DIR to compiled official v1.0.1 protobufs")
    def test_official_protojson_schema_accepts_mapped_objects(self):
        from google.protobuf.json_format import ParseDict
        source = Path(os.environ["OEA_A2A_SCHEMA_DIR"]) / "a2a_pb2.py"
        spec = importlib.util.spec_from_file_location("official_a2a_pb2", source)
        protocol = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(protocol)
        item = {"id": "artifact", "name": "result.txt", "media_type": "text/plain", "size": 1, "sha256": "a" * 64}
        ParseDict(artifact(item), protocol.Artifact())
        ParseDict(task({"id": "task", "thread_id": "thread", "session_id": "session", "state": "SUCCEEDED", "updated_at": 1,
                       "result": {"output": {"value": 1}}}, [item]), protocol.Task())
        ParseDict({"messageId": "message", "contextId": "thread", "role": "ROLE_AGENT", "parts": [{"text": "Ready"}]}, protocol.Message())
        ParseDict(agent_card(AgentConfig(id="worker", name="Worker").model_dump(mode="json"),
            AgentDescriptor(agent_id="worker").model_dump(mode="json"),
            [{"url": "https://agent.example/a2a", "protocolBinding": "HTTP+JSON", "protocolVersion": "1.0"}]), protocol.AgentCard())

    async def test_message_only_and_task_intents_share_existing_services(self):
        with tempfile.TemporaryDirectory() as temp:
            worker = ChannelWorker()
            async with Runtime.open(temp, adapters={"worker": worker}) as rt:
                session = await rt.sessions.create()
                thread = await rt.threads.create(session.id, "worker")
                message = {"messageId": "notify", "contextId": thread.id, "role": "ROLE_USER", "parts": [{"text": "Review the next task"}]}
                response = await rt.a2a.send(session.id, message)
                self.assertEqual(response["message"]["role"], "ROLE_AGENT")
                self.assertFalse(rt.active)
                self.assertEqual((await rt.tasks.list(session.id)).items, [])
                self.assertEqual(response, await rt.a2a.send(session.id, message))
                message = {**message, "messageId": "execute"}
                submitted = await rt.a2a.send(session.id, message, mode="task")
                self.assertEqual(submitted["task"]["contextId"], thread.id)
                request, _ = await asyncio.wait_for(worker.ready.get(), 3)
                worker.release.set()
                await rt.tasks.result(submitted["task"]["id"], timeout=5)
                complete = await rt.a2a.get_task(submitted["task"]["id"])
                self.assertEqual(complete["status"]["state"], "TASK_STATE_COMPLETED")
                self.assertEqual(complete["status"]["message"]["role"], "ROLE_AGENT")
                with self.assertRaises(RuntimeFault) as terminal:
                    await rt.a2a.send(session.id, {**message, "messageId": "repair", "taskId": submitted["task"]["id"]}, mode="task")
                self.assertEqual(terminal.exception.code, "NEW_TASK_REQUIRED")
                renewed = await rt.a2a.send(session.id, {**message, "messageId": "repair", "referenceTaskIds": [submitted["task"]["id"]]}, mode="task")
                self.assertNotEqual(renewed["task"]["id"], submitted["task"]["id"])
                with self.assertRaises(RuntimeFault):
                    await rt.a2a.send(session.id, {**message, "role": "ROLE_AGENT"})
                with self.assertRaises(RuntimeFault):
                    await rt.a2a.send(session.id, {**message, "extensions": ["urn:unknown"]})
                with self.assertRaises(RuntimeFault):
                    await rt.a2a.send(session.id, message, version="0.3")
                self.assertFalse((await rt.a2a.capabilities())["network_binding"])

    def test_parts_and_uncertain_task_do_not_imply_standard_success(self):
        Part.model_validate({"data": None})
        Part.model_validate({"raw": "YQ==", "filename": "letter.txt", "mediaType": "text/plain"})
        with self.assertRaises(ValueError):
            Part.model_validate({"text": "mixed", "data": {}})
        with self.assertRaises(ValueError):
            Message.model_validate({"messageId": "old", "role": "user", "parts": [{"kind": "text", "text": "old"}]})
        mapped = task({"id": "task", "thread_id": "thread", "session_id": "session", "state": "UNCERTAIN", "updated_at": 1})
        self.assertEqual(mapped["status"]["state"], "TASK_STATE_UNSPECIFIED")
        self.assertTrue(mapped["metadata"]["urn:oea:runtime"]["requires_reconciliation"])
