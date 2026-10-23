"""Context projection and evidence access using temporary state and real receipts."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from ma_runtime import (AdapterEvent, AgentConfig, AgentDescriptor, LLMResponse, PlatformClient,
                        ProbeResult, Runtime, RuntimeFault, TaskResult)
from ma_runtime.context_projection import READ_CONTEXT_TOOL, estimated_tokens, prepare_context
from ma_runtime.llm import ConfiguredLLM
from ma_runtime.models import TaskSpec, content_hash, json_dump
from ma_runtime.tools import ToolSpec
from ma_runtime.web.app import create_app


class EchoSource:
    def describe(self):
        return ToolSpec(name="echo")


class ContextAgent:
    def __init__(self, identity="worker"):
        self.identity = identity
        self.ready, self.release = asyncio.Event(), asyncio.Event()
        self.context = None

    def describe(self):
        return AgentDescriptor(agent_id=self.identity, max_concurrency=4)

    async def execute(self, request, context):
        self.context = context
        mode = request.payload.get("mode", "source")
        if mode == "read":
            try:
                output = await context.read_context(request.payload["reference"])
            except RuntimeFault as exc:
                output = {"error": exc.code}
        elif mode == "project_data":
            output = {"view": await context.project_context(request.payload["value"])}
        elif mode == "walk_history":
            reference, segments = request.payload["reference"], []
            while reference:
                part = await context.read_context(reference, limit=16384)
                assert part["next_offset"] is None
                segments.append(json.loads(part["text"]))
                reference = part.get("previous_reference")
            output = {"messages": [message for segment in reversed(segments) for message in segment]}
        elif mode == "project":
            config = AgentConfig(id=self.identity, name="Context worker", kind="llm", model="test", base_url="http://model.invalid/v1",
                                 context_budget_tokens=request.payload.get("budget", 6000), max_output_tokens=1024)
            original = request.payload["messages"]
            tools = [READ_CONTEXT_TOOL] if request.payload.get("reader", True) else []
            view, plan = await prepare_context(original, tools, context, ConfiguredLLM(config))
            output = {"view": view, "plan": plan, "original": original}
        else:
            reference = await context._runtime.contexts.register(context, request.payload.get("text", "Source evidence"),
                request.payload.get("origin", "history:" + request.thread_id))
            output = {"reference": reference}
            if mode == "revoke":
                self.ready.set()
                await self.release.wait()
                try:
                    await context.read_context(reference)
                except RuntimeFault as exc:
                    output["error"] = exc.code
        yield AdapterEvent(type="completed", result=TaskResult(output=output))

    async def probe(self, run_ref):
        return ProbeResult(state="STOPPED", confirmed_stopped=True)

    async def aclose(self):
        self.release.set()


class ContextProjectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_complete_tool_schema_with_type_union_and_type_property_reaches_context_preparation(self):
        config = AgentConfig(id="worker", name="Worker", context_budget_tokens=6000)
        context = type("Context", (), {"request": None})()
        schema = {"type": "object", "properties": {"type": {"type": ["string", "null"]},
                  "node": {"$ref": "#/$defs/node"}},
                  "$defs": {"node": {"type": "object", "properties": {"next": {"$ref": "#/$defs/node"}}}}}
        tools = [{"type": "function", "function": {"name": "query", "parameters": schema}}]
        messages = [{"role": "user", "content": "Read a bounded query"}]
        projected, _ = await prepare_context(messages, tools, context, ConfiguredLLM(config))
        self.assertEqual(projected, messages)
        self.assertEqual(tools[0]["function"]["parameters"], schema)
        self.assertGreater(estimated_tokens(tools), 0)

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="oea-context-")
        self.root = Path(self.temp.name)
        self.worker = ContextAgent()
        self.rt = await Runtime.open(self.root / "state", adapters={"worker": self.worker}).__aenter__()
        self.rt.tools.register(EchoSource(), allowed_agent_ids=["worker"])
        self.session = await self.rt.sessions.create()
        self.thread = await self.rt.threads.create(self.session.id, "worker")
        self.sequence = 0

    async def asyncTearDown(self):
        await self.rt.aclose()
        self.temp.cleanup()

    async def submit(self, payload, *, thread=None, **options):
        self.sequence += 1
        target = thread or self.thread
        task = await self.rt.tasks.submit({"session_id": target.session_id, "thread_id": target.id,
            "agent_id": target.agent_id, "idempotency_key": str(self.sequence), "payload": payload, **options})
        return task, await self.rt.tasks.result(task.id, timeout=5)

    async def test_sources_are_immutable_deduplicated_and_hidden_from_deliverables(self):
        text = "中文 evidence\n" * 300
        _, first = await self.submit({"text": text, "origin": "tool:echo"})
        _, second = await self.submit({"text": text, "origin": "tool:echo"})
        reference = first.output["reference"]
        self.assertEqual(reference, second.output["reference"])
        parts, offset = [], 0
        while offset is not None:
            value = await self.rt.contexts.read(reference, "default", offset=offset, limit=256)
            parts.append(value["text"])
            offset = value["next_offset"]
        self.assertEqual("".join(parts), text)
        self.assertEqual((await self.rt.sessions.snapshot(self.session.id))["artifacts"], [])
        _, changed = await self.submit({"text": text + "changed", "origin": "tool:echo"})
        self.assertNotEqual(reference, changed.output["reference"])
        self.assertEqual((await self.rt.contexts.read(reference, "default"))["text"], text)

    async def test_cross_thread_access_requires_explicit_assignment_and_cross_project_is_rejected(self):
        _, saved = await self.submit({"text": "Private working evidence"})
        reference = saved.output["reference"]
        other = await self.rt.threads.create(self.session.id, "worker")
        _, denied = await self.submit({"mode": "read", "reference": reference}, thread=other)
        self.assertEqual(denied.output["error"], "PERMISSION_DENIED")
        _, allowed = await self.submit({"mode": "read", "reference": reference}, thread=other, context_refs=[reference])
        self.assertEqual(allowed.output["text"], "Private working evidence")
        other_folder = self.root / "other-documents"
        other_folder.mkdir()
        project = await self.rt.projects.create("Other project", str(other_folder), "other")
        with self.assertRaises(RuntimeFault) as fault:
            await self.rt.contexts.read(reference, project.id)
        self.assertEqual(fault.exception.code, "PROJECT_MISMATCH")

    async def test_tool_provenance_survives_cache_eviction_and_rejects_a_different_call(self):
        from ma_runtime.tracing import RunStage
        original = self.worker.execute
        async def execute(request, context):
            source = await self.rt.contexts.register(context, "Recorded evidence", "history:" + request.thread_id)
            stage = RunStage(context, "model", tool_calls=[{"id": "reader", "name": "read_context"}])
            await stage.record("SUCCEEDED")
            context.model_tool_causes = {}
            value = await context.invoke_tool("read_context", {"reference": source}, "read", model_call_id=stage.id, tool_call_id="reader")
            with self.assertRaises(RuntimeFault) as fault:
                await context.invoke_tool("read_context", {"reference": source}, "bad", model_call_id=stage.id, tool_call_id="another")
            self.assertEqual(fault.exception.code, "ADAPTER_PROTOCOL_ERROR")
            yield AdapterEvent(type="completed", result=TaskResult(output=value))
        self.worker.execute = execute
        task, result = await self.submit({})
        self.assertEqual(result.output["text"], "Recorded evidence")
        stages = (await self.rt.tasks.trace(task.id))["items"]
        reader = next(stage for stage in stages if stage["stage"] == "context_read")
        model = next(stage for stage in stages if stage["stage"] == "model")
        self.assertEqual(reader["attributes"]["parent_stage_id"], model["id"])
        self.worker.execute = original

    async def test_source_access_rechecks_current_workbench_grants(self):
        await self.rt.environments.configure({"id": "files", "project_id": "default"})
        config = AgentConfig(id="worker", name="Worker", kind="external", project_id="default",
            workbench={"environment_id": "files", "tools": ["read_file"]})
        await self.rt.agents.configure(config)
        self.rt.adapters["worker"] = self.worker
        task = await self.rt.tasks.submit({"session_id": self.session.id, "thread_id": self.thread.id,
            "agent_id": "worker", "idempotency_key": "revoke", "payload": {"mode": "revoke", "origin": "tool:read_file"}})
        await asyncio.wait_for(self.worker.ready.wait(), 5)
        await self.rt.agents.configure(config.model_copy(update={"workbench": config.workbench.model_copy(update={"tools": ()})}))
        self.worker.release.set()
        result = await self.rt.tasks.result(task.id, timeout=5)
        self.assertEqual(result.output["error"], "PERMISSION_DENIED")

    async def test_large_tool_result_is_referenced_without_mutating_original_or_splitting_pairs(self):
        text = "Evidence line\n" * 1400
        messages = [{"role": "user", "content": "Inspect the evidence"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "call", "type": "function", "function": {"name": "echo", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "call", "content": text}]
        _, result = await self.submit({"mode": "project", "messages": messages})
        self.assertEqual(result.output["original"], messages)
        view, plan = result.output["view"], result.output["plan"]
        self.assertLess(plan["input_tokens"], plan["input_tokens_before"])
        self.assertEqual([item["role"] for item in view], ["user", "assistant", "tool"])
        self.assertEqual((await self.rt.contexts.read(plan["source_refs"][0], "default", limit=16384))["text"], text[:16384])

    async def test_working_memory_reuses_prefix_after_restart_and_keeps_raw_history(self):
        summaries = []
        async def summarize(client, messages, context, **options):
            summaries.append(messages)
            return LLMResponse(model="test", text="Confirmed: review is in progress. Keep the source references and verify the remaining section.", finish_reason="stop")
        messages = [{"role": "system", "content": "Keep explicit evidence."}]
        with patch("ma_runtime.llm.ConfiguredLLM.complete", new=summarize):
            for number in range(1, 25):
                messages.append({"role": "user" if number % 2 else "assistant", "content": str(number) + " " + "detail " * 120})
                _, result = await self.submit({"mode": "project", "messages": messages, "budget": 4000})
            plan = result.output["plan"]
            self.assertIn("memory_ref", plan)
            self.assertLessEqual(plan["input_tokens"], 4000)
            self.assertLess(len(summaries), len(messages) // 2)
            archive = await self.rt.contexts.read(plan["history_ref"], "default", limit=16384)
            self.assertIn("detail", archive["text"])
            count = len(summaries)
            await self.rt.aclose()
            self.rt = await Runtime.open(self.root / "state", adapters={"worker": self.worker}).__aenter__()
            self.rt.tools.register(EchoSource(), allowed_agent_ids=["worker"])
            _, restored = await self.submit({"mode": "project", "messages": messages, "budget": 4000})
            self.assertEqual(len(summaries), count)
            self.assertEqual(restored.output["plan"]["memory_ref"], plan["memory_ref"])

    async def test_mandatory_oversize_and_orphaned_tool_result_fail_before_summary(self):
        for messages, code in [([{ "role": "user", "content": "x" * 20000}], "MODEL_CONTEXT_BUDGET"),
                               ([{"role": "tool", "tool_call_id": "orphan", "content": "result"}], "MODEL_CONTEXT_INVALID")]:
            with self.assertRaises(RuntimeFault):
                await self.submit({"mode": "project", "messages": messages, "budget": 2000})
            task = (await self.rt.tasks.list(self.session.id)).items[-1]
            self.assertEqual(task.error["code"], code)

    async def test_growing_projected_memory_archives_new_groups_before_reuse(self):
        summaries = []
        async def summarize(client, messages, context, **options):
            summaries.append(messages)
            return LLMResponse(model="test", text="Known constraint. " * 210, finish_reason="stop")
        messages = [{"role": "system", "content": "Keep evidence. " * 6},
                    {"role": "user", "content": "a" * 7200}, {"role": "user", "content": "b" * 1400}]
        with patch("ma_runtime.llm.ConfiguredLLM.complete", new=summarize):
            _, first = await self.submit({"mode": "project", "messages": messages, "budget": 3000})
            self.assertEqual(len(summaries), 1)
            messages.extend([{"role": "assistant", "content": "c" * 2400}, {"role": "user", "content": "d" * 1100}])
            _, second = await self.submit({"mode": "project", "messages": messages, "budget": 3000})
        self.assertEqual(len(summaries), 2)
        self.assertLessEqual(second.output["plan"]["input_tokens"], 3000)
        self.assertEqual(second.output["original"], messages)
        archive = await self.rt.contexts.read(second.output["plan"]["history_ref"], "default")
        self.assertEqual(archive["previous_reference"], first.output["plan"]["history_ref"])
        self.assertEqual(json.loads(archive["text"]), [{"role": "user", "content": "b" * 1400}])

    async def test_source_reads_use_the_shared_http_sdk(self):
        _, saved = await self.submit({"text": "Source"})
        app = create_app(runtime=self.rt)
        async with app.router.lifespan_context(app):
            async with PlatformClient("http://127.0.0.1", app.state.token, transport=httpx.ASGITransport(app=app)) as client:
                value = await client.contexts.read(saved.output["reference"], "default")
                self.assertEqual(value["text"], "Source")

    async def test_large_dependency_values_use_individual_references_and_tolerate_arbitrary_json(self):
        values = {"first": {"output": "x" * 5000}, "second": {"result": "y" * 5000}}
        _, result = await self.submit({"mode": "project_data", "value": values})
        for key, item in values.items():
            reference = result.output["view"][key]["reference"]
            source = await self.rt.contexts.read(reference, "default")
            self.assertEqual(json.loads(source["text"]), item)

    async def test_assigned_incremental_history_can_be_walked_without_copying_the_prefix(self):
        async def summarize(client, messages, context, **options):
            return LLMResponse(model="test", text="Work is in progress; retain original sources.", finish_reason="stop")
        messages = []
        with patch("ma_runtime.llm.ConfiguredLLM.complete", new=summarize):
            for index in range(1, 31):
                messages.append({"role": "user" if index % 2 else "assistant", "content": f"{index}: " + "evidence " * 100})
                _, result = await self.submit({"mode": "project", "messages": messages, "budget": 3000})
        reference = result.output["plan"]["history_ref"]
        other = await self.rt.threads.create(self.session.id, "worker")
        _, history = await self.submit({"mode": "walk_history", "reference": reference}, thread=other, context_refs=[reference])
        self.assertEqual(history.output["messages"], messages[:len(history.output["messages"])])
        from ma_runtime.store import one
        size = await self.rt.store.call(lambda db: one(db, "SELECT SUM(a.size) AS total FROM context_sources c JOIN artifacts a ON a.id=c.reference WHERE c.origin LIKE 'history:%'"))
        self.assertLess(size.total, len(json_dump(messages).encode("utf-8")))
