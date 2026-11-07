"""Attempt-scoped live pages, durable fallback and stale callback isolation."""

import asyncio
import json
from pathlib import Path
import tempfile
import unittest

from oea import AgentDefinition, Completion, Model, RunSpec
from oea.contracts.errors import RuntimeFault
from oea.runtime.host import Host
from oea.storage.persistence import one


class ResponseTests(unittest.IsolatedAsyncioTestCase):
    def test_transient_snapshot_cache_is_bounded_and_reports_eviction(self):
        from oea.models.stream import ResponseStreams
        stream = ResponseStreams()
        active = stream.start("r", "a", "u", 1)
        stream.append(active, "x" * (2 * 1024 * 1024))
        first = stream.read("r", "a", "u", None)
        for _ in range(10):
            stream.append(active, "x")
            stream.read("r", "a", "u", None)
        self.assertLessEqual(stream.snapshot_bytes, 16 * 1024 * 1024)
        with self.assertRaises(RuntimeFault) as caught:
            stream.read("r", "a", "u", first["version"])
        self.assertEqual(caught.exception.code, "SOURCE_VERSION_CHANGED")

    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="oea-response-")
        self.root = Path(self.temporary.name) / "state"
        self.host = await Host(self.root).__aenter__()
        await self.host.projects.create("Project", project_id="p")
        await self.host.agents.save(AgentDefinition(id="worker", name="Worker", project_id="p",
            model=Model(base_url="https://model.invalid/v1", model="fixture")))
        session = await self.host.sessions.create("p")
        self.instance = await self.host.instances.create(session["id"], "worker")

    async def asyncTearDown(self):
        await self.host.aclose()
        self.temporary.cleanup()

    async def submit(self, key="work"):
        return await self.host.runs.submit(RunSpec(instance_id=self.instance["id"], input="Finish", idempotency_key=key))

    async def test_live_unicode_pages_remain_fixed_while_new_deltas_arrive(self):
        first,more,second,finish = (asyncio.Event() for _ in range(4))
        text = "正文🙂𐐀" * 5000
        class ModelFixture:
            async def complete(inner, model, messages, **options):
                await options["emit"]({"kind": "model.delta", "text": text})
                first.set();await more.wait()
                await options["emit"]({"kind": "model.delta", "text": "tail"})
                second.set();await finish.wait()
                return Completion(text=text + "tail", usage={"prompt_tokens": 12, "completion_tokens": 4})
        self.host.model_api = ModelFixture()
        run = await self.submit()
        await asyncio.wait_for(first.wait(), 5)
        page = await self.host.registry.call("runs.response", {"run_id": run.id, "limit": 31})
        self.assertEqual(page["sequence"], 1)
        more.set();await asyncio.wait_for(second.wait(), 5)
        args = {"run_id": run.id, "action_id": page["action_id"], "attempt_id": page["attempt_id"], "version": page["version"]}
        pieces = [page["text"]]
        while page["next_offset"] is not None:
            page = await self.host.runs.response(**args, offset=page["next_offset"], limit=2048)
            pieces.append(page["text"])
        self.assertEqual("".join(pieces), text)
        current = await self.host.runs.response(run.id)
        self.assertEqual(current["sequence"], 2)
        self.assertNotEqual(current["version"], args["version"])
        finish.set();await self.host.runs.result(run.id, timeout=5)
        saved = await self.host.runs.response(run.id, action_id=args["action_id"], attempt_id=args["attempt_id"])
        self.assertEqual(saved["status"], "saved")
        await self.host.aclose()
        self.host = await Host(self.root).__aenter__()
        reloaded = await self.host.runs.response(run.id, action_id=args["action_id"], attempt_id=args["attempt_id"], version=saved["version"])
        self.assertEqual(saved, reloaded)
        with self.assertRaises(RuntimeFault) as caught:
            await self.host.runs.response(**args)
        self.assertEqual(caught.exception.code, "SOURCE_VERSION_CHANGED")

    async def test_late_delta_and_fact_do_not_replace_a_new_attempt(self):
        calls,callbacks = [],[]
        ready,finish = asyncio.Event(),asyncio.Event()
        class ModelFixture:
            async def complete(inner, model, messages, **options):
                calls.append(1);callbacks.append(options)
                if len(calls) == 1:
                    raise RuntimeFault("MODEL_FAILED", "Known local failure", details={"request_sent": False})
                await options["emit"]({"kind": "model.delta", "text": "new"})
                ready.set();await finish.wait()
                return Completion(text="new", usage={"prompt_tokens": 12, "completion_tokens": 1})
        self.host.model_api = ModelFixture()
        run = await self.submit()
        with self.assertRaises(RuntimeFault):
            await self.host.runs.result(run.id, timeout=5)
        original = await self.host.runs.response(run.id)
        receipt = await self.host.runs.action(original["action_id"], attempt_id=original["attempt_id"])
        reference = await self.host.results.issue("runs.action", {"action_id": original["action_id"], "attempt_id": original["attempt_id"]}, receipt, "reader", characters=0)
        await self.host.runs.resume(run.id, "continue")
        await asyncio.wait_for(ready.wait(), 5)
        current = await self.host.runs.response(run.id)
        await callbacks[0]["emit"]({"kind": "model.delta", "text": "stale"})
        await callbacks[0]["on_fact"]("response_received", {"response_received": True}, {})
        unchanged = await self.host.runs.response(run.id)
        self.assertEqual(unchanged["text"], "new")
        self.assertEqual(unchanged["attempt_id"], current["attempt_id"])
        row = await self.host.store.call(lambda db: one(db, "SELECT input_json FROM actions WHERE id=?", (current["action_id"],)))
        self.assertEqual(json.loads(row["input_json"])["attempt"]["attempt_id"], current["attempt_id"])
        finish.set();await self.host.runs.result(run.id, timeout=5)
        old = await self.host.runs.response(run.id, action_id=original["action_id"], attempt_id=original["attempt_id"])
        self.assertEqual(old["text"], "")
        self.assertNotEqual(old["attempt_id"], current["attempt_id"])
        retained = await self.host.results.read(reference["result_ref"], project_id="p", consumer_id="reader", json_pointer="/is_current")
        self.assertTrue(retained["value"], "The original response envelope stays fixed after archival")
        self.assertFalse((await self.host.runs.action(original["action_id"], attempt_id=original["attempt_id"]))["is_current"])
        self.assertEqual(len(calls), 2)
