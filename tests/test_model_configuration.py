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
from fastapi.responses import StreamingResponse

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
                {"version": version, "body": body, "authorization": auth}
            )
            self.started.set()
            content = body["messages"][-1].get("content", "")

            async def stream():
                if content == "hold first node":
                    await self.release.wait()
                text = (
                    auth
                    if content == "echo credential"
                    else body["model"] + ":" + version
                )
                yield (
                    "data: "
                    + json.dumps(
                        {
                            "choices": [
                                {"delta": {"content": text}, "finish_reason": None}
                            ]
                        }
                    )
                    + "\n\n"
                )
                yield (
                    "data: "
                    + json.dumps(
                        {
                            "choices": [{"delta": {}, "finish_reason": "stop"}],
                            "usage": {
                                "prompt_tokens": 2,
                                "completion_tokens": 3,
                                "total_tokens": 5,
                            },
                        }
                    )
                    + "\n\n"
                )
                yield "data: [DONE]\n\n"

            return StreamingResponse(stream(), media_type="text/event-stream")

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

    async def submit(self, payload=None):
        session = await self.rt.sessions.create(title="Model configuration test")
        thread = await self.rt.threads.create(session.id, "model-worker")
        task = await self.rt.tasks.submit(
            {
                "session_id": session.id,
                "thread_id": thread.id,
                "agent_id": "model-worker",
                "payload": payload or {"input": "test"},
                "idempotency_key": "one-task",
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


if __name__ == "__main__":
    unittest.main()
