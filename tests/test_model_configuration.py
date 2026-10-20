"""Unified native/process model API behavior against a local streaming provider."""

import asyncio
import json
import socket
import sys
import tempfile
import unittest
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ma_runtime import (
    AgentConfig,
    AgentDescriptor,
    PlatformClient,
    Runtime,
    RuntimeConfig,
)
from ma_runtime.models import json_dump
from ma_runtime.web.app import create_app

FIXTURE = Path(__file__).parent / "fixtures/model_agent.py"


class ModelConfigurationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ma-model-config-")
        self.root = Path(self.temp.name)
        self.rt = await Runtime.open(
            self.root, RuntimeConfig(cancel_grace_seconds=0.5)
        ).__aenter__()
        self.captured = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        provider = FastAPI()

        @provider.post("/{version}/chat/completions")
        async def completion(version: str, request: Request):
            body = await request.json()
            auth = request.headers.get("authorization", "")
            self.captured.append(
                {"version": version, "body": body, "authorization": auth,
                 "client_port": request.client.port, "cookie": request.headers.get("cookie", "")}
            )
            self.started.set()
            content = body["messages"][-1].get("content", "")
            reasoning_summary = (body["messages"][0].get("content", "").startswith("Summarize previous work")
                                 and "reasoning-heavy-summary-case" in content)
            output_cap = body.get("max_tokens", body.get("max_completion_tokens", 4096))
            context_request = next((message["content"].partition(":")[2].strip() for message in body["messages"]
                if message.get("role") == "user" and isinstance(message.get("content"), str) and message["content"].startswith("read context:")), None)
            context_result = next((message["content"] for message in body["messages"]
                if message.get("role") == "tool" and message.get("tool_call_id") == "context-source"), None)
            if content == "http failure":
                return JSONResponse({"error": "Provider unavailable"}, status_code=503)

            async def stream():
                if content == "hold first node":
                    await self.release.wait()
                text = (
                    auth
                    if content == "echo credential"
                    else body["model"] + ":" + version
                )
                if content in {"empty limit", "empty stop"}:
                    text = ""
                if content == "partial then stop":
                    text = "First part. "
                if reasoning_summary:
                    text = "Retained evidence constraints and source references; review remains in progress." if output_cap >= 3500 else ""
                if context_result is not None:
                    text = json.loads(context_result).get("text", "tool-error")
                if content == "structured instruction conflict":
                    from ma_runtime.presentation import OUTPUT_GUIDANCE
                    text = json.dumps({"items": ["evidence"], "covered_items": ["scope"]})
                    if any(OUTPUT_GUIDANCE in message.get("content", "") for message in body["messages"] if isinstance(message.get("content"), str)):
                        text = "Summary\n```json\n" + text + "\n```"
                used_output = (3500 if output_cap >= 3500 else output_cap) if reasoning_summary else 3
                finish = ("stop" if output_cap >= 3500 else "length") if reasoning_summary else "tool_calls" if content == "read source" else "length" if content in {"empty limit", "partial limit", "partial then stop"} else "stop"
                read_call = content == "read source"
                delta = {"tool_calls": [{"index": 0, "id": "read-source", "type": "function", "function": {
                    "name": "read_file", "arguments": json.dumps({"path": "/project/source.txt", "limit": 16384})}}]} if read_call else {"content": text}
                if context_request and context_result is None:
                    delta = {"tool_calls": [{"index": 0, "id": "context-source", "type": "function", "function": {
                        "name": "read_context", "arguments": json.dumps({"reference": context_request, "limit": 512})}}]}
                    finish = "tool_calls"
                yield (
                    "data: "
                    + json.dumps(
                        {
                            "choices": [
                                {"delta": delta, "finish_reason": None}
                            ]
                        }
                    )
                    + "\n\n"
                )
                if content == "hold after text":
                    await self.release.wait()
                yield (
                    "data: "
                    + json.dumps(
                        {
                            "choices": [{"delta": {}, "finish_reason": finish}],
                            "usage": {
                                "prompt_tokens": 2,
                                "completion_tokens": used_output,
                                "total_tokens": 2 + used_output,
                                **({"completion_tokens_details": {"reasoning_tokens": used_output - (20 if text else 0)}} if reasoning_summary else {}),
                            },
                        }
                    )
                    + "\n\n"
                )
                yield "data: [DONE]\n\n"
                if content == "hold after done":
                    await self.release.wait()

            return StreamingResponse(stream(), media_type="text/event-stream", headers={"Set-Cookie": "provider_session=private; Path=/"})

        self.socket = socket.socket()
        self.socket.bind(("127.0.0.1", 0))
        self.base = f"http://127.0.0.1:{self.socket.getsockname()[1]}"
        self.server = uvicorn.Server(
            uvicorn.Config(provider, access_log=False, log_level="critical")
        )
        self.hosting = asyncio.create_task(self.server.serve(sockets=[self.socket]))
        async with asyncio.timeout(10):
            while not self.server.started:
                await asyncio.sleep(0.01)

    async def asyncTearDown(self):
        self.release.set()
        await self.rt.aclose()
        self.server.should_exit = True
        await asyncio.wait_for(self.hosting, 10)
        self.socket.close()
        self.temp.cleanup()

    def config(self, **changes):
        return AgentConfig(
            **{
                "id": "model-worker",
                "name": "Model worker",
                "kind": "process",
                "llm_required": True,
                "base_url": self.base + "/v1",
                "model": "first-model",
                "command": (sys.executable, "-B", str(FIXTURE.resolve())),
                "agent_md": "Keep the Agent's rules.",
                "system_prompt": "Be specific.",
                "temperature": 0.2,
                "max_output_tokens": 80,
                "token_parameter": "max_tokens",
                **changes,
                "process_descriptor": (
                    AgentDescriptor(
                        agent_id="model-worker",
                        execution_mode="subprocess",
                        cancellation="terminate_owned_process",
                        message_receipts="unsupported",
                        tool_mediation="full",
                    )
                    if changes.get("kind", "process") == "process"
                    else None
                ),
            }
        )

    async def submit(self, payload=None, **options):
        session = await self.rt.sessions.create(title="Model configuration test")
        thread = await self.rt.threads.create(session.id, "model-worker")
        task = await self.rt.tasks.submit(
            {
                "session_id": session.id,
                "thread_id": thread.id,
                "agent_id": "model-worker",
                "payload": payload or {"input": "test"},
                "idempotency_key": "one-task",
                **options,
            }
        )
        return task

    async def test_process_uses_ui_saved_model_and_keeps_credentials_in_host(self):
        await self.rt.agents.configure(
            self.config(), api_key="fixture-private-credential"
        )
        task = await self.submit({"input": "echo credential"})
        result = await self.rt.tasks.result(task.id, timeout=15)
        self.assertEqual(result.output["model"], "first-model")
        self.assertEqual(result.output["text"], "Bearer [redacted]")
        self.assertEqual(result.output["usage"]["total_tokens"], 5)
        body = self.captured[0]["body"]
        self.assertEqual(
            self.captured[0]["authorization"], "Bearer fixture-private-credential"
        )
        self.assertEqual(body["temperature"], 0.2)
        self.assertEqual(body["max_tokens"], 80)
        self.assertIn("Keep the Agent's rules.", body["messages"][0]["content"])
        self.assertIn("Be specific.", body["messages"][0]["content"])
        self.assertNotIn("api_key", result.output["info"])
        self.assertNotIn("fixture-private-credential", json_dump(result))
        self.assertNotIn(
            "fixture-private-credential", json_dump(await self.rt.tasks.get(task.id))
        )
        self.assertNotIn(
            "fixture-private-credential",
            json_dump(await self.rt.agents.describe("model-worker")),
        )

    async def test_model_changes_apply_to_new_runs_and_not_running_nodes(self):
        await self.rt.agents.configure(self.config(), api_key="old-fixture-key")
        task = await self.submit({"input": "hold first node", "twice": True})
        await asyncio.wait_for(self.started.wait(), 10)
        await self.rt.agents.configure(
            self.config(base_url=self.base + "/v2", model="next-model"),
            api_key="new-fixture-key",
        )
        self.release.set()
        first = await self.rt.tasks.result(task.id, timeout=15)
        self.assertEqual(first.output["second_model"], "first-model")
        next_task = await self.submit()
        second = await self.rt.tasks.result(next_task.id, timeout=15)
        self.assertEqual(second.output["model"], "next-model")
        self.assertEqual([c["version"] for c in self.captured], ["v1", "v1", "v2"])
        self.assertEqual(
            [c["authorization"] for c in self.captured],
            [
                "Bearer old-fixture-key",
                "Bearer old-fixture-key",
                "Bearer new-fixture-key",
            ],
        )

    async def test_missing_configuration_waits_and_http_save_releases_task(self):
        config = self.config(base_url="", model="")
        saved = await self.rt.agents.configure(config)
        self.assertEqual(saved["model_setup"]["state"], "needs_configuration")
        task = await self.submit()
        async with asyncio.timeout(5):
            while (
                await self.rt.tasks.get(task.id)
            ).queue_reason != "MODEL_CONFIGURATION":
                await asyncio.sleep(0.02)
        self.assertEqual(len(self.captured), 0)
        app = create_app(runtime=self.rt)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                base_url="http://127.0.0.1",
                transport=httpx.ASGITransport(app=app),
                headers={"Authorization": "Bearer " + app.state.token},
            ) as web:
                response = await web.put(
                    "/api/agents/model-worker",
                    json={
                        **self.config().model_dump(mode="json"),
                        "api_key": "ui-fixture-key",
                    },
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["model_setup"]["state"], "configured")
                self.assertNotIn("ui-fixture-key", response.text)
        result = await self.rt.tasks.result(task.id, timeout=15)
        self.assertEqual(result.output["text"], "first-model:v1")
        self.assertEqual(self.captured[0]["authorization"], "Bearer ui-fixture-key")

    async def test_node_cannot_override_model_credentials_or_use_disabled_model(self):
        await self.rt.agents.configure(self.config())
        task = await self.submit({"tamper": True, "capture_error": True})
        self.assertEqual(
            (await self.rt.tasks.result(task.id, timeout=15)).output["error_code"],
            "INVALID_REQUEST",
        )
        await self.rt.agents.configure(
            self.config(llm_required=False, base_url="", model="")
        )
        task = await self.submit({"capture_error": True})
        self.assertEqual(
            (await self.rt.tasks.result(task.id, timeout=15)).output["error_code"],
            "MODEL_NOT_ENABLED",
        )
        self.assertEqual(self.captured, [])

    async def test_cancellation_ends_pending_model_request(self):
        await self.rt.agents.configure(self.config())
        task = await self.submit({"input": "hold first node"})
        await asyncio.wait_for(self.started.wait(), 10)
        await self.rt.tasks.cancel(task.id)
        async with asyncio.timeout(5):
            while (await self.rt.tasks.get(task.id)).state != "CANCELLED":
                await asyncio.sleep(0.02)
        self.assertEqual(len(self.captured), 1)

    async def test_required_tool_choice_reaches_provider_and_text_is_rejected(self):
        from ma_runtime.errors import RuntimeFault
        config = self.config(kind="llm", command=(), llm_required=False, collaboration=True)
        await self.rt.agents.configure(config)
        session = await self.rt.sessions.create(title="Required tool transport")
        thread = await self.rt.threads.create(session.id, config.id)
        task = await self.rt.threads.turn(thread.id, "Verify", "required-transport",
            tool_policy={"allowed": ["list_threads"], "required": ["list_threads"]})
        with self.assertRaises(RuntimeFault):
            await self.rt.tasks.result(task.id, timeout=10)
        self.assertEqual(self.captured[-1]["body"]["tool_choice"], "required")
        state = await self.rt.tasks.get(task.id)
        self.assertEqual(state.state, "FAILED")
        self.assertEqual(state.error["code"], "EXECUTION_REQUIREMENT_UNMET")

    async def test_native_agent_and_model_probe_use_the_shared_transport(self):
        config = self.config(
            kind="llm", command=(), llm_required=False, collaboration=False
        )
        await self.rt.agents.configure(config)
        task = await self.submit()
        result = await self.rt.tasks.result(task.id, timeout=10)
        self.assertEqual(result.output["text"], "first-model:v1")
        app = create_app(runtime=self.rt)
        async with app.router.lifespan_context(app):
            async with PlatformClient(
                "http://127.0.0.1",
                app.state.token,
                transport=httpx.ASGITransport(app=app),
            ) as client:
                await client.agents.update("model-worker", model="probe-model")
                result = await client.agents.test_model("model-worker")
                self.assertTrue(result["connected"])
                self.assertEqual(result["model"], "probe-model")
        self.assertEqual(self.captured[-1]["body"]["max_tokens"], 16)
        self.assertEqual(
            self.captured[-1]["body"]["messages"],
            [{"role": "user", "content": "Reply with OK."}],
        )

    async def test_model_trace_is_durable_bounded_and_shared_with_http_sdk(self):
        await self.rt.agents.configure(self.config(kind="llm", command=(), llm_required=False), api_key="fixture-trace-secret")
        task = await self.submit({"input": "trace request"})
        await self.rt.tasks.result(task.id, timeout=10)
        first = await self.rt.tasks.trace(task.id, limit=1)
        self.assertTrue(first["has_more"])
        self.assertEqual(first["items"][0]["state"], "RUNNING")
        second = await self.rt.tasks.trace(task.id, cursor=first["cursor"], limit=1)
        stage = second["items"][0]
        self.assertEqual(stage["state"], "SUCCEEDED")
        self.assertGreater(stage["seq"], first["items"][0]["seq"])
        self.assertLessEqual(stage["attributes"]["response_headers_ms"], stage["attributes"]["first_content_ms"])
        self.assertGreaterEqual(stage["duration_ms"], stage["attributes"]["first_content_ms"])
        self.assertEqual(stage["attributes"]["usage"]["total_tokens"], 5)
        self.assertGreaterEqual(first["runs"][0]["timings"]["validation_ms"], 0)
        self.assertNotIn("trace request", json_dump(first) + json_dump(second))
        self.assertNotIn("fixture-trace-secret", json_dump(first) + json_dump(second))
        app = create_app(runtime=self.rt)
        async with app.router.lifespan_context(app):
            async with PlatformClient("http://127.0.0.1", app.state.token, transport=httpx.ASGITransport(app=app)) as client:
                remote = await client.tasks.trace(task.id, limit=1)
                self.assertEqual(remote["items"], first["items"])
                response = await client.http.get(f"/api/tasks/{task.id}/trace?limit=1")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["items"], first["items"])

    async def test_real_transport_limit_keeps_usage_and_partial_output(self):
        await self.rt.agents.configure(self.config(kind="llm", command=(), llm_required=False))
        for content in ("empty limit", "partial limit"):
            task = await self.submit({"input": content})
            from ma_runtime.errors import RuntimeFault
            with self.assertRaises(RuntimeFault):
                await self.rt.tasks.result(task.id, timeout=10)
            value = await self.rt.tasks.get(task.id)
            self.assertEqual(value.error["code"], "OUTPUT_TRUNCATED")
            self.assertEqual(value.result.metadata["model_completion"]["usage"]["total_tokens"], 5)
            self.assertEqual(bool(value.result.output["text"]), content == "partial limit")
            trace = await self.rt.tasks.trace(task.id)
            self.assertEqual(trace["items"][-1]["state"], "INCOMPLETE")

    async def test_provider_http_failure_and_response_timeout_remain_distinct(self):
        from ma_runtime.errors import RuntimeFault
        await self.rt.agents.configure(self.config(kind="llm", command=(), llm_required=False))
        task = await self.submit({"input": "http failure"})
        with self.assertRaises(RuntimeFault):
            await self.rt.tasks.result(task.id, timeout=10)
        value = await self.rt.tasks.get(task.id)
        self.assertEqual(value.error["code"], "MODEL_HTTP_ERROR")
        trace = await self.rt.tasks.trace(task.id)
        self.assertEqual(trace["items"][-1]["error"]["details"]["http_status"], 503)
        await self.rt.agents.configure(self.config(kind="llm", command=(), llm_required=False, timeout_seconds=0.05))
        task = await self.submit({"input": "hold first node"})
        with self.assertRaises(RuntimeFault):
            await self.rt.tasks.result(task.id, timeout=10)
        value = await self.rt.tasks.get(task.id)
        self.assertEqual(value.error["code"], "MODEL_READ_TIMEOUT")
        trace = await self.rt.tasks.trace(task.id)
        self.assertEqual(trace["items"][-1]["error"]["details"]["operation"], "read")
        task = await self.submit({"input": "hold after text"})
        with self.assertRaises(RuntimeFault):
            await self.rt.tasks.result(task.id, timeout=10)
        value = await self.rt.tasks.get(task.id)
        self.assertEqual(value.error["code"], "MODEL_READ_TIMEOUT")
        self.assertEqual(value.result.output["text"], "first-model:v1")
        self.assertEqual(value.result.metadata["acceptance"]["state"], "FAILED")

    async def test_host_reuses_connections_without_reusing_credentials_or_provider_cookies(self):
        await self.rt.agents.configure(self.config(kind="llm", command=(), llm_required=False), api_key="first-credential")
        first = await self.submit({"input": "first"})
        await self.rt.tasks.result(first.id, timeout=10)
        await self.rt.agents.configure(self.config(kind="llm", command=(), llm_required=False), api_key="second-credential")
        second = await self.submit({"input": "second"})
        await self.rt.tasks.result(second.id, timeout=10)
        self.assertEqual(self.captured[0]["client_port"], self.captured[1]["client_port"])
        self.assertNotEqual(self.captured[0]["authorization"], self.captured[1]["authorization"])
        self.assertEqual([call["cookie"] for call in self.captured], ["", ""])
        clients = list(self.rt.model_transport.clients.values())
        await self.rt.aclose()
        self.assertTrue(all(client.is_closed for client in clients))

    async def test_terminal_marker_does_not_wait_for_a_provider_to_close_its_tail(self):
        await self.rt.agents.configure(self.config(kind="llm", command=(), llm_required=False))
        task = await self.submit({"input": "hold after done"})
        result = await self.rt.tasks.result(task.id, timeout=5)
        self.assertEqual(result.output["text"], "first-model:v1")
        trace = await self.rt.tasks.trace(task.id)
        self.assertFalse(trace["items"][-1]["attributes"]["stream_drained"])

    async def test_process_cannot_commit_an_unrecovered_incomplete_model_response(self):
        from ma_runtime.errors import RuntimeFault
        await self.rt.agents.configure(self.config())
        for options in ({}, {"allow_incomplete": True}, {"capture_error": True}):
            task = await self.submit({"input": "partial limit", **options})
            with self.assertRaises(RuntimeFault):
                await self.rt.tasks.result(task.id, timeout=10)
            state = await self.rt.tasks.get(task.id)
            self.assertEqual(state.state, "FAILED")
            self.assertIsNotNone(state.result)
            self.assertIn("first-model:v1", json_dump(state.result))

    async def test_native_context_pressure_records_real_summary_requests_and_bounded_model_input(self):
        from ma_runtime.context_projection import estimated_tokens
        await self.rt.agents.configure(self.config(kind="llm", command=(), llm_required=False,
                                                   collaboration=False, context_budget_tokens=4000, max_output_tokens=1024))
        session = await self.rt.sessions.create(title="Context pressure")
        thread = await self.rt.threads.create(session.id, "model-worker")
        plans = []
        for index in range(18):
            task = await self.rt.threads.turn(thread.id, f"Section {index}: " + "evidence " * 100, f"turn-{index}")
            await self.rt.tasks.result(task.id, timeout=10)
            trace = await self.rt.tasks.trace(task.id)
            plans.extend(item for item in trace["items"] if item["state"] == "SUCCEEDED")
        self.assertTrue(any(item["stage"] == "summary" for item in plans))
        self.assertTrue(any(item["attributes"].get("context", {}).get("memory_ref") for item in plans))
        self.assertLess(sum(item["stage"] == "summary" for item in plans), 18)
        for call in self.captured:
            self.assertLessEqual(estimated_tokens(call["body"]["messages"]) + estimated_tokens(call["body"].get("tools", [])), 4000)
        self.assertEqual(sum(item["attributes"]["usage"]["total_tokens"] for item in plans), len(self.captured) * 5)

    async def test_summary_reasoning_allowance_is_separate_from_retained_input_space(self):
        from ma_runtime.context_projection import estimated_tokens
        await self.rt.agents.configure(self.config(kind="llm", command=(), llm_required=False,
            collaboration=False, context_budget_tokens=2500, max_output_tokens=4096))
        session = await self.rt.sessions.create(title="Reasoning summary budget")
        thread = await self.rt.threads.create(session.id, "model-worker")
        stages = []
        for index in range(12):
            task = await self.rt.threads.turn(thread.id,
                f"reasoning-heavy-summary-case {index}: " + "evidence " * 100, f"reasoning-{index}")
            await self.rt.tasks.result(task.id, timeout=10)
            stages.extend(item for item in (await self.rt.tasks.trace(task.id))["items"] if item["state"] == "SUCCEEDED")
        summaries = [item for item in stages if item["stage"] == "summary"]
        self.assertTrue(summaries)
        self.assertTrue(any(item["attributes"].get("context", {}).get("memory_ref") for item in stages))
        self.assertTrue(all(item["attributes"]["max_output_tokens"] == 4096 for item in summaries))
        self.assertTrue(all(item["attributes"]["usage"]["completion_tokens_details"]["reasoning_tokens"] > 3000 for item in summaries))
        self.assertEqual(len(self.captured), 12 + len(summaries))
        for request in self.captured:
            self.assertLessEqual(estimated_tokens(request["body"]["messages"]) + estimated_tokens(request["body"].get("tools", [])), 2500)

    async def test_native_file_evidence_is_reused_by_version_without_becoming_a_deliverable(self):
        documents = tempfile.TemporaryDirectory(prefix="oea-documents-")
        self.addCleanup(documents.cleanup)
        folder = Path(documents.name)
        path = folder / "source.txt"
        path.write_text("Version A\n" + "evidence\n" * 1800, encoding="utf-8")
        await self.rt.projects.update("default", local_folder=str(folder))
        await self.rt.environments.configure({"id": "files", "project_id": "default"})
        await self.rt.agents.configure(self.config(kind="llm", command=(), llm_required=False, collaboration=False,
            workbench={"environment_id": "files", "tools": ["read_file"]}, context_budget_tokens=5000))
        references = []
        for index in range(3):
            if index == 2:
                path.write_text("Version B\n" + "evidence\n" * 1800, encoding="utf-8")
            task = await self.submit({"input": "read source"})
            await self.rt.tasks.result(task.id, timeout=10)
            trace = await self.rt.tasks.trace(task.id)
            refs = [reference for item in trace["items"] if item["state"] == "SUCCEEDED"
                    for reference in item["attributes"].get("context", {}).get("source_refs", [])]
            self.assertTrue(refs)
            references.append(refs[-1])
            saved = await self.rt.tasks.get(task.id)
            self.assertEqual((await self.rt.sessions.snapshot(saved.session_id))["artifacts"], [])
        self.assertEqual(references[0], references[1])
        self.assertNotEqual(references[1], references[2])
        old = await self.rt.contexts.read(references[0], "default", limit=16384)
        self.assertIn("Version A", old["text"])

    async def test_native_agent_dispatches_assigned_context_reader_through_shared_runtime(self):
        documents = tempfile.TemporaryDirectory(prefix="oea-assigned-source-")
        self.addCleanup(documents.cleanup)
        folder = Path(documents.name)
        (folder / "source.txt").write_text("Version A\n" + "evidence\n" * 1800, encoding="utf-8")
        await self.rt.projects.update("default", local_folder=str(folder))
        await self.rt.environments.configure({"id": "files", "project_id": "default"})
        await self.rt.agents.configure(self.config(kind="llm", command=(), llm_required=False, collaboration=False,
            workbench={"environment_id": "files", "tools": ["read_file"]}, context_budget_tokens=5000))
        source = await self.submit({"input": "read source"})
        await self.rt.tasks.result(source.id, timeout=10)
        trace = await self.rt.tasks.trace(source.id)
        reference = next(ref for item in trace["items"] if item["state"] == "SUCCEEDED"
                         for ref in item["attributes"].get("context", {}).get("source_refs", []))
        original = await self.rt.tasks.get(source.id)
        thread = await self.rt.threads.create(original.session_id, "model-worker")
        task = await self.rt.threads.turn(thread.id, "read context:" + reference, "assigned-context",
                                         context_refs=[reference], tool_policy={"allowed": ["read_file"]})
        result = await self.rt.tasks.result(task.id, timeout=10)
        self.assertIn("Version A", result.output["text"])
        stages = (await self.rt.tasks.trace(task.id))["items"]
        read = [item for item in stages if item["stage"] == "context_read" and item["state"] == "SUCCEEDED"]
        self.assertEqual(len(read), 1)
        self.assertEqual(read[0]["attributes"]["reference"], reference)
        parent = next(item for item in stages if item["id"] == read[0]["attributes"]["parent_stage_id"])
        self.assertEqual(parent["stage"], "model")
        self.assertEqual((await self.rt.workbench.calls(task.id))["items"], [])

    async def test_structured_native_delivery_does_not_request_a_markdown_preamble(self):
        await self.rt.agents.configure(self.config(kind="llm", command=(), llm_required=False))
        task = await self.submit({"input": "structured instruction conflict"}, result_policy={"kind": "structured",
            "output_schema": {"type": "object", "required": ["items"], "properties": {"items": {"type": "array", "minItems": 1}}},
            "required_items": ["scope"]})
        result = await self.rt.tasks.result(task.id, timeout=10)
        self.assertEqual(result.output, {"items": ["evidence"], "covered_items": ["scope"]})
        self.assertEqual(result.metadata["acceptance"]["state"], "PASSED")
        self.assertEqual(len(self.captured), 1)

    async def test_delivery_continuation_reuses_task_and_receipt_and_joins_partial_text(self):
        from ma_runtime.errors import RuntimeFault
        await self.rt.agents.configure(self.config(kind="llm", command=(), llm_required=False))
        task = await self.submit({"input": "partial then stop"})
        with self.assertRaises(RuntimeFault):
            await self.rt.tasks.result(task.id, timeout=10)
        first = await self.rt.tasks.get(task.id)
        original_hash = first.request_hash
        accepted = await self.rt.tasks.continue_result(task.id, "continue")
        self.assertEqual(accepted.id, task.id)
        result = await self.rt.tasks.result(task.id, timeout=10)
        self.assertEqual(result.output["text"], "First part. first-model:v1")
        repeated = await self.rt.tasks.continue_result(task.id, "continue")
        self.assertTrue(repeated.reused)
        final = await self.rt.tasks.get(task.id)
        self.assertEqual(final.request_hash, original_hash)
        self.assertEqual(len(final.runs), 2)
        self.assertEqual(final.continuation["calls_used"], 1)
        self.assertEqual(final.continuation["runs_used"], 1)
        with self.assertRaises(RuntimeFault) as conflict:
            await self.rt.tasks.continue_result(task.id, "continue", "Different guidance")
        self.assertEqual(conflict.exception.code, "IDEMPOTENCY_CONFLICT")

    async def test_delivery_continuation_rejects_changed_conversation_and_exhausted_budget(self):
        from ma_runtime.errors import RuntimeFault
        await self.rt.agents.configure(self.config(kind="llm", command=(), llm_required=False))
        task = await self.submit({"input": "partial limit"}, continuation_policy={"max_runs": 1, "max_model_calls": 1, "max_output_tokens": 128})
        with self.assertRaises(RuntimeFault):
            await self.rt.tasks.result(task.id, timeout=10)
        await self.rt.tasks.continue_result(task.id, "continue", "partial limit")
        with self.assertRaises(RuntimeFault):
            await self.rt.tasks.result(task.id, timeout=10)
        count = len(self.captured)
        with self.assertRaises(RuntimeFault) as exhausted:
            await self.rt.tasks.continue_result(task.id, "again")
        self.assertEqual(exhausted.exception.code, "CONTINUATION_BUDGET_EXCEEDED")
        self.assertEqual(len(self.captured), count)
        other = await self.submit({"input": "partial then stop"})
        with self.assertRaises(RuntimeFault):
            await self.rt.tasks.result(other.id, timeout=10)
        state = await self.rt.tasks.get(other.id)
        later = await self.rt.threads.turn(state.thread_id, "Later goal", "later")
        await self.rt.tasks.result(later.id, timeout=10)
        with self.assertRaises(RuntimeFault) as changed:
            await self.rt.tasks.continue_result(other.id, "moved")
        self.assertEqual(changed.exception.code, "REVISION_CONFLICT")

    async def test_artifact_process_delivery_does_not_require_model_prose(self):
        await self.rt.agents.configure(self.config())
        task = await self.submit({"input": "empty stop", "publish_empty": True}, result_policy={"kind": "artifacts", "required_artifacts": ["report.txt"]})
        result = await self.rt.tasks.result(task.id, timeout=10)
        self.assertEqual(result.output["text"], "")
        self.assertEqual(len(result.artifact_ids), 1)

    async def test_process_model_failure_keeps_the_safe_cause_without_claiming_completion(self):
        from ma_runtime import RuntimeFault
        await self.rt.agents.configure(self.config(), api_key="fixture-process-error-secret")
        task = await self.submit({"input": "http failure"})
        with self.assertRaises(RuntimeFault):
            await self.rt.tasks.result(task.id, timeout=10)
        final = await self.rt.tasks.get(task.id)
        self.assertNotEqual(final.state, "SUCCEEDED")
        self.assertEqual(final.error["code"], "MODEL_HTTP_ERROR")
        self.assertEqual(final.error["details"]["http_status"], 503)
        self.assertTrue(final.error["retryable"])
        self.assertNotIn("fixture-process-error-secret", json_dump(final))
        self.assertIsNone(final.runs[-1].boundary_receipt)

    async def test_continuation_counts_tool_followups_and_clamps_the_actual_model_request(self):
        from ma_runtime import RuntimeFault
        (self.root / "documents").mkdir()
        (self.root / "documents/source.txt").write_text("Read only evidence", encoding="utf-8")
        project = await self.rt.projects.create("Documents", str(self.root / "documents"), "documents")
        await self.rt.environments.configure({"id": "files", "project_id": project.id})
        await self.rt.agents.configure(self.config(kind="llm", command=(), llm_required=False, project_id=project.id,
            max_output_tokens=512, workbench={"environment_id": "files", "tools": ["read_file"]}))
        session = await self.rt.sessions.create(project_id=project.id)
        thread = await self.rt.threads.create(session.id, "model-worker")
        task = await self.rt.threads.turn(thread.id, "partial then stop", "bounded",
            continuation_policy={"max_model_calls": 1, "max_output_tokens": 128})
        with self.assertRaises(RuntimeFault):
            await self.rt.tasks.result(task.id, timeout=10)
        app = create_app(runtime=self.rt)
        async with app.router.lifespan_context(app):
            async with PlatformClient("http://localhost", app.state.token, transport=httpx.ASGITransport(app=app)) as client:
                response = await client.http.post(f"/api/tasks/{task.id}/continue", json={"idempotency_key": "read", "content": "read source"})
                self.assertEqual(response.status_code, 200)
                with self.assertRaises(RuntimeFault):
                    await client.tasks.result(task.id, timeout=10)
                replay = await client.tasks.continue_result(task.id, "read", "read source")
                self.assertTrue(replay["reused"])
        final = await self.rt.tasks.get(task.id)
        self.assertEqual(final.error["code"], "CONTINUATION_BUDGET_EXCEEDED")
        self.assertEqual(final.continuation["calls_used"], 1)
        self.assertEqual(final.continuation["tokens_reserved"], 3)
        self.assertEqual(self.captured[-1]["body"]["max_tokens"], 128)
        calls = (await self.rt.tasks.trace(task.id))["tools"]
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0]["parent_stage_id"])


if __name__ == "__main__":
    unittest.main()
