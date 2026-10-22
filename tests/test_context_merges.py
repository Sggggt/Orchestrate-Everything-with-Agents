"""Immutable evidence merges, branch provenance and revocable source access."""

import json
import tempfile
import unittest
from pathlib import Path

from ma_runtime import AgentConfig, Runtime, RuntimeFault
from ma_runtime.models import json_dump
from ma_runtime.store import one
from ma_runtime.tools import ToolSpec
from test_skill_system import PolicyLLM


class ContextMergeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="oea-context-merge-")
        self.rt = await Runtime.open(Path(self.temp.name)).__aenter__()
        self.config = AgentConfig(id="worker", name="Worker", kind="llm", base_url="http://model.invalid/v1", model="test")
        await self.rt.agents.configure(self.config)
        self.adapter = PolicyLLM(self.config)
        self.rt.adapters["worker"] = self.adapter
        self.session = await self.rt.sessions.create()
        self.target = await self.rt.threads.create(self.session.id, "worker")
        self.references, self.reads = {}, []
        self.original = self.adapter._completion
        async def complete(messages, context, tools=None):
            payload = context.request.payload
            if payload.get("source"):
                reference = await self.rt.contexts.register(context, payload["source"], payload.get("origin", "history:" + context.request.thread_id))
                artifact = await context.put_bytes("evidence.txt", payload["source"].encode())
                self.references[context.request.thread_id] = (reference, artifact)
            if payload.get("read"):
                for reference in context.request.context_refs:
                    manifest = json.loads((await context.read_context(reference, limit=16384))["text"])
                    self.reads.append(manifest)
                    for item in manifest["evidence"]:
                        self.reads.append(await context.read_context(item["reference"]))
            async for item in self.original(messages, context, tools):
                yield item
        self.adapter._completion = complete

    async def asyncTearDown(self):
        await self.rt.aclose()
        self.temp.cleanup()

    async def turn(self, thread, key, **payload):
        from ma_runtime import TaskSpec
        task = await self.rt.tasks.submit(TaskSpec(session_id=self.session.id, thread_id=thread.id, agent_id="worker",
            payload={"input": key, **payload}, idempotency_key=key))
        await self.rt.tasks.result(task.id, timeout=5)
        return await self.rt.threads.get(thread.id)

    async def source(self, text, key, origin=None):
        thread = await self.rt.threads.create(self.session.id, "worker")
        return await self.turn(thread, key, source=text, **({"origin": origin} if origin else {}))

    async def selection(self, source, name="evidence", artifact=False):
        ref, file = self.references[source.id]
        value = file if artifact else await self.rt.artifacts.get(ref)
        return {"thread_id": source.id, "revision": source.revision, "checkpoint_id": source.latest_checkpoint_id,
                "evidence": [{"name": name, "kind": "artifact" if artifact else "context", "reference": value.id, "sha256": value.sha256}]}

    async def test_merge_retains_main_checkpoint_and_only_selected_evidence(self):
        base = await self.turn(self.target, "Target mainline")
        source = await self.source("Source-only evidence", "Source conversation must stay separate")
        selected = await self.selection(source)
        selected["evidence"].extend((await self.selection(source, "file", artifact=True))["evidence"])
        spec = {"thread_id": base.id, "expected_revision": base.revision, "sources": [selected], "idempotency_key": "merge"}
        merged = await self.rt.contexts.merge(spec)
        unchanged = await self.rt.threads.get(base.id)
        self.assertEqual(unchanged.latest_checkpoint_id, base.latest_checkpoint_id)
        self.assertEqual(unchanged.revision, base.revision + 1)
        self.assertIsNone((await self.rt.threads.ownership(base.id))["workflow_run_id"])
        old_branch = await self.rt.threads.fork(base.id, checkpoint_id=base.latest_checkpoint_id, idempotency_key="before-merge")
        self.assertIsNone(await self.rt.store.call(lambda db: self.rt.context_merges.head(db, old_branch.id)))
        continued = await self.turn(base, "Read selected evidence", read=True)
        self.assertEqual([entry["text"] for entry in self.reads[1:]], ["Source-only evidence"] * 2)
        model_history = json_dump(self.adapter.captured[-1]["messages"])
        self.assertIn("Target mainline", model_history)
        self.assertNotIn("Source conversation must stay separate", model_history)
        branch = await self.rt.threads.fork(base.id, idempotency_key="after-merge")
        self.assertEqual((await self.rt.store.call(lambda db: self.rt.context_merges.head(db, branch.id))).id, merged["id"])
        await self.turn(source, "Advance source")
        self.assertTrue((await self.rt.contexts.merge(spec))["reused"])
        self.assertEqual((await self.rt.contexts.get_merge(merged["id"]))["sha256"], merged["sha256"])
        self.assertEqual((await self.rt.threads.get(base.id)).latest_checkpoint_id, continued.latest_checkpoint_id)

    async def test_conflicts_need_a_named_choice_and_versions_are_checked(self):
        left = await self.source("Value A", "Left")
        right = await self.source("Value B", "Right")
        a, b = await self.selection(left), await self.selection(right)
        spec = {"thread_id": self.target.id, "expected_revision": 0, "sources": [a, b], "idempotency_key": "conflict"}
        with self.assertRaises(RuntimeFault) as fault:
            await self.rt.contexts.merge(spec)
        self.assertEqual(fault.exception.code, "CONTEXT_MERGE_CONFLICT")
        spec["resolutions"] = {"evidence": {"reference": b["evidence"][0]["reference"], "reason": "Use the explicitly reviewed source."}}
        merged = await self.rt.contexts.merge(spec)
        self.assertEqual(len(merged["evidence"]), 1)
        self.assertEqual(merged["evidence"][0]["reference"], b["evidence"][0]["reference"])
        with self.assertRaises(RuntimeFault) as conflict:
            await self.rt.contexts.merge({**spec, "expected_revision": 1})
        self.assertEqual(conflict.exception.code, "IDEMPOTENCY_CONFLICT")
        with self.assertRaises(RuntimeFault) as version:
            await self.rt.contexts.merge({**spec, "idempotency_key": "stale-target"})
        self.assertEqual(version.exception.code, "REVISION_CONFLICT")
        await self.turn(left, "New source head")
        with self.assertRaises(RuntimeFault) as version:
            await self.rt.contexts.merge({**spec, "expected_revision": 1, "idempotency_key": "stale-source"})
        self.assertEqual(version.exception.code, "SOURCE_VERSION_CONFLICT")

    async def test_revoked_evidence_and_foreign_projects_cannot_be_merged(self):
        class SourceTool:
            def describe(self):
                return ToolSpec(name="evidence_tool")
        self.rt.tools.register(SourceTool(), allowed_agent_ids=["worker"])
        source = await self.source("Restricted evidence", "Authorized source", "tool:evidence_tool")
        spec = {"thread_id": self.target.id, "expected_revision": 0, "sources": [await self.selection(source)], "idempotency_key": "restricted"}
        self.rt.tools.allowed["evidence_tool"] = set()
        with self.assertRaises(RuntimeFault) as permission:
            await self.rt.contexts.merge(spec)
        self.assertEqual(permission.exception.code, "PERMISSION_DENIED")
        self.rt.tools.allowed["evidence_tool"] = {"worker"}
        await self.rt.contexts.merge(spec)
        self.rt.tools.allowed["evidence_tool"] = set()
        with self.assertRaises(RuntimeFault) as revoked:
            await self.turn(self.target, "Read after revocation", read=True)
        self.assertEqual(revoked.exception.code, "PERMISSION_DENIED")
        project = await self.rt.projects.create("Other project", str(Path(self.temp.name).parent), "other")
        await self.rt.agents.configure(self.config.model_copy(update={"id": "other", "project_id": project.id}))
        session = await self.rt.sessions.create(project_id=project.id)
        target = await self.rt.threads.create(session.id, "other")
        with self.assertRaises(RuntimeFault) as foreign:
            await self.rt.contexts.merge({**spec, "thread_id": target.id})
        self.assertEqual(foreign.exception.code, "PROJECT_MISMATCH")
