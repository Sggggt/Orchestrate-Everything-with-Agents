"""Public SDK lifecycle, sync/async parity, stream delivery, and import behavior."""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from oea import Agent, AgentDefinition, Completion, Model, ToolKit, ContextPolicy, Instructions
from oea.runtime.host import Host
from oea.contracts.errors import RuntimeFault
from oea.storage.persistence import one


class EchoModel:
    def __init__(self):
        self.calls = 0

    async def complete(self, model, messages, **options):
        self.calls += 1
        if options.get("emit"):
            await options["emit"]({"kind": "model.delta", "text": "Done"})
        return Completion(text="Done", usage={"prompt_tokens": 30, "completion_tokens": 3})


class SyncSDKTests(unittest.TestCase):
    def test_import_and_configuration_do_not_load_legacy_or_start_host_threads(self):
        script = """import json,sys,threading
from oea import Agent,Model,ToolKit
agent=Agent(name='Reader',model=Model(model='example'),tools=ToolKit.local(files=False))
saved=Agent.from_saved('reader',state_dir='/unused-sdk-state')
print(json.dumps({'threads':len(threading.enumerate()),'legacy':any(name in sys.modules for name in ['ma_runtime','agent']),'host':'oea.runtime.host' in sys.modules,'graph':'langgraph' in sys.modules,'ready':agent._ready}))
agent.close()
saved.close()
"""
        environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
        result = subprocess.run([sys.executable, "-B", "-c", script], env=environment, capture_output=True, text=True, check=True)
        value = json.loads(result.stdout)
        self.assertEqual(value, {"threads": 1, "legacy": False, "host": False, "graph": False, "ready": False})

    def test_standalone_sync_run_stream_and_close_share_one_owned_host(self):
        with tempfile.TemporaryDirectory(prefix="oea-v2-sdk-") as directory:
            model = EchoModel()
            with patch("oea.runtime.host.ModelAPI", return_value=model):
                with Agent(name="Reader", model=Model(base_url="https://example.invalid/v1", model="fixture"),
                           tools=ToolKit.local(files=False), state_dir=Path(directory) / "state") as agent:
                    instance = agent.instance("persistent")
                    self.assertEqual(instance.run("First", idempotency_key="one").text, "Done")
                    identity = instance.id
                    self.assertEqual(instance.run("Second", idempotency_key="two").text, "Done")
                    self.assertEqual(instance.id, identity)
                    frames = list(instance.stream("Third", idempotency_key="three"))
                    self.assertEqual(frames[-1]["kind"], "run.completed")
                    self.assertEqual(frames[-1]["result"]["text"], "Done")
                    backend = agent.backend
                self.assertFalse(backend.opened)
                self.assertFalse(backend.jobs)
            self.assertEqual(model.calls, 3)
            self.assertFalse(any(thread.name.startswith("oea-store") for thread in threading.enumerate()))


class AsyncSDKTests(unittest.IsolatedAsyncioTestCase):
    async def test_saved_definition_is_not_overwritten_and_new_runs_use_gui_changes(self):
        class SavedModel:
            def __init__(self):
                self.models, self.messages = [], []
            async def complete(self, model, messages, **options):
                self.models.append(model.model)
                self.messages.append(messages)
                return Completion(text="Done", usage={"prompt_tokens": 30, "completion_tokens": 3})
        with tempfile.TemporaryDirectory(prefix="oea-saved-sdk-") as directory:
            model = SavedModel()
            async with Host(Path(directory) / "state", model_api=model) as host:
                await host.projects.create("Project", project_id="p")
                definition = AgentDefinition(id="saved", name="Saved", project_id="p", model=Model(base_url="https://example.invalid/v1", model="saved-v1"),
                    instructions=Instructions(system="Preserved GUI instruction"), context=ContextPolicy(memory_tokens=1536))
                await host.registry.call("agents.save", {"definition": definition.model_dump(mode="json"), "expected_revision": 0})
                session = await host.sessions.create("p")
                async with Agent.from_saved("saved", host=host, session_id=session["id"]) as agent:
                    self.assertEqual((await agent.arun("First", idempotency_key="first")).text, "Done")
                    identity = agent.instance().id
                    self.assertEqual((await host.agents.get("saved"))["revision"], 1)
                    definition.name = "Renamed in GUI"
                    definition.model.model = "saved-v2"
                    await host.registry.call("agents.save", {"definition": definition.model_dump(mode="json"), "expected_revision": 1})
                    self.assertEqual((await agent.arun("Second", idempotency_key="second")).text, "Done")
                    self.assertEqual(agent.instance().id, identity)
                self.assertEqual(await host.agents.get("saved"), {"definition": definition.model_dump(mode="json"), "revision": 2})
                self.assertTrue(host.opened)
                self.assertEqual(model.models, ["saved-v1", "saved-v2"])
                self.assertIn("Preserved GUI instruction", model.messages[0][0]["content"])

    async def test_global_saved_agent_requires_scope_and_keeps_project_instances_separate(self):
        with tempfile.TemporaryDirectory(prefix="oea-global-saved-") as directory:
            async with Host(Path(directory) / "state", model_api=EchoModel()) as host:
                for project in ("p", "q"):
                    await host.projects.create(project, project_id=project)
                definition = AgentDefinition(id="global", name="Global", model=Model(base_url="https://example.invalid/v1", model="fixture"))
                await host.agents.save(definition)
                async with Agent.from_saved("global", host=host) as missing:
                    with self.assertRaises(RuntimeFault) as scope:
                        await missing.arun("No implicit project")
                self.assertEqual(scope.exception.code, "PROJECT_REQUIRED")
                identities = []
                for project in ("p", "q"):
                    async with Agent.from_saved("global", host=host, project_id=project) as agent:
                        await agent.arun("Scoped work")
                        identities.append(agent.instance().id)
                self.assertNotEqual(*identities)
                self.assertEqual((await host.agents.get("global"))["definition"]["project_id"], None)
                self.assertEqual((await host.agents.get("global"))["revision"], 1)

    async def test_reopening_saved_sdk_state_keeps_instance_after_display_name_change(self):
        with tempfile.TemporaryDirectory(prefix="oea-saved-reopen-") as directory:
            root = Path(directory) / "state"
            model = EchoModel()
            async with Host(root, model_api=model) as host:
                async with Agent(name="Reader", model=Model(base_url="https://example.invalid/v1", model="fixture"), tools=ToolKit.local(files=False), host=host) as created:
                    await created.arun("First")
                    original = created.instance().id
                    original_workspace = (await host.instances.get(original))["workspace"]
                saved = await host.agents.get("reader")
                definition = AgentDefinition.model_validate(saved["definition"])
                definition.name = "Changed display name"
                await host.agents.save(definition, expected_revision=1)
            with patch("oea.runtime.host.ModelAPI", return_value=model):
                async with Agent.from_saved("reader", state_dir=root) as reopened:
                    await reopened.arun("Continue")
                    self.assertEqual(reopened.instance().id, original)
                    reopened_workspace = (await reopened.backend.instances.get(original))["workspace"]
                    self.assertEqual(reopened_workspace["work_dir"], original_workspace["work_dir"])
                    self.assertNotEqual(reopened_workspace["output_dir"], original_workspace["output_dir"])
                    self.assertEqual(reopened.definition.name, "Changed display name")
                    self.assertEqual((await reopened.backend.agents.get("reader"))["revision"], 2)
                    backend = reopened.backend
                self.assertFalse(backend.opened)

    async def test_async_agent_uses_existing_host_and_does_not_close_it(self):
        with tempfile.TemporaryDirectory(prefix="oea-v2-sdk-") as directory:
            model = EchoModel()
            async with Host(Path(directory) / "state", model_api=model) as host:
                await host.projects.create("Project", project_id="p")
                async with Agent(name="Reader", project_id="p", model=Model(base_url="https://example.invalid/v1", model="fixture"),
                                 tools=ToolKit.local(files=False), host=host) as agent:
                    values = await asyncio.gather(*(agent.instance("instance-" + str(index)).arun("Read") for index in range(20)))
                    self.assertEqual([value.text for value in values], ["Done"] * 20)
                    self.assertEqual((await host.agents.get("reader"))["revision"], 1)
                    self.assertIs(agent.backend, host)
                self.assertTrue(host.opened)
            self.assertEqual(model.calls, 20)

    async def test_sync_calls_in_async_context_explain_the_correct_entry(self):
        agent = Agent(name="Reader", tools=ToolKit.local(files=False))
        with self.assertRaisesRegex(RuntimeError, "arun/astream/aclose"):
            agent.run("Read")
        self.assertIsNone(agent._runner)
        await agent.aclose()

    async def test_owned_host_is_released_when_saved_initialization_fails(self):
        with tempfile.TemporaryDirectory(prefix="oea-sdk-init-failure-") as directory:
            root = Path(directory) / "state"
            async with Host(root, model_api=EchoModel()):
                pass
            agent = Agent.from_saved("missing", state_dir=root)
            try:
                with self.assertRaises(RuntimeFault) as missing:
                    await agent.arun("No saved definition")
                self.assertEqual(missing.exception.code, "NOT_FOUND")
                self.assertFalse(agent.backend.opened)
                async with Host(root, model_api=EchoModel()) as reopened:
                    self.assertTrue(reopened.opened)
                with self.assertRaisesRegex(RuntimeError, "aclose"):
                    agent.close()
            finally:
                await agent.aclose()

    async def test_cancelled_submission_retains_original_receipt_and_shared_host(self):
        with tempfile.TemporaryDirectory(prefix="oea-sdk-submit-cancel-") as directory:
            model = EchoModel()
            async with Host(Path(directory) / "state", model_api=model) as host:
                accepted, deliver = asyncio.Event(), asyncio.Event()
                original_call = host.registry.call
                receipt = {}
                async def delay_receipt(operation, params, **options):
                    result = await original_call(operation, params, **options)
                    if operation == "runs.submit":
                        receipt.update(result.model_dump(mode="json"))
                        accepted.set()
                        await deliver.wait()
                    return result
                agent = Agent(name="Reader", model=Model(base_url="https://example.invalid/v1", model="fixture"), tools=ToolKit.local(files=False), host=host)
                instance = agent.instance("persistent")
                with patch.object(host.registry, "call", side_effect=delay_receipt):
                    task = asyncio.create_task(instance.submit("Accepted once", idempotency_key="stable"))
                    try:
                        await asyncio.wait_for(accepted.wait(), timeout=10)
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await task
                    finally:
                        deliver.set()
                        await agent.aclose()
                self.assertEqual(instance.last_run_id, receipt["id"])
                self.assertTrue(host.opened)
                self.assertEqual((await host.runs.result(instance.last_run_id, timeout=10)).text, "Done")
                self.assertEqual(await host.store.call(lambda db: one(db, "SELECT COUNT(*) AS count FROM runs")["count"]), 1)
                self.assertEqual(model.calls, 1)

    async def test_closing_stream_releases_observer_without_cancelling_run(self):
        with tempfile.TemporaryDirectory(prefix="oea-sdk-stream-close-") as directory:
            entered, release = asyncio.Event(), asyncio.Event()
            class WaitingModel:
                async def complete(self, *args, **options):
                    entered.set()
                    await release.wait()
                    return Completion(text="Done", usage={"prompt_tokens": 1, "completion_tokens": 1})
            async with Host(Path(directory) / "state", model_api=WaitingModel()) as host:
                async with Agent(name="Reader", model=Model(base_url="https://example.invalid/v1", model="fixture"), tools=ToolKit.local(files=False), host=host) as agent:
                    stream = agent.astream("Existing work", idempotency_key="stream")
                    try:
                        frame = await anext(stream)
                        identity = frame["run_id"]
                        await asyncio.wait_for(entered.wait(), timeout=10)
                        self.assertIn(identity, host.subscribers)
                        await stream.aclose()
                        self.assertNotIn(identity, host.subscribers)
                        self.assertIsNone((await host.runs.get(identity))["cancel_requested_at"])
                    finally:
                        release.set()
                        await stream.aclose()
                    self.assertEqual((await host.runs.result(identity, timeout=10)).text, "Done")

    async def test_explicit_resume_retains_original_run_for_following_input(self):
        class TwoWaitsModel:
            def __init__(self):
                self.calls = 0
            async def complete(self, *args, **options):
                self.calls += 1
                if self.calls <= 2:
                    return Completion(tool_calls=({"id": "wait-" + str(self.calls), "type": "function", "function": {"name": "wait_for_input", "arguments": json.dumps({"prompt": "Reply " + str(self.calls)})}},), finish_reason="tool_calls", usage={"prompt_tokens": 1, "completion_tokens": 1})
                return Completion(text="Both replies retained", usage={"prompt_tokens": 1, "completion_tokens": 1})
        with tempfile.TemporaryDirectory(prefix="oea-sdk-resume-") as directory:
            model = TwoWaitsModel()
            async with Host(Path(directory) / "state", model_api=model) as host:
                async with Agent(name="Reader", model=Model(base_url="https://example.invalid/v1", model="fixture"), tools=ToolKit.local(files=False), host=host) as original:
                    receipt = await original.instance().submit("Ask for two replies", idempotency_key="work")
                    frames = [frame async for frame in host.events(receipt["id"])]
                    self.assertEqual(frames[-1]["kind"], "run.waiting")
                async with Agent.from_saved("reader", host=host) as reopened:
                    instance = reopened.instance()
                    with self.assertRaises(RuntimeFault) as pending:
                        await instance.aresume("First reply", run_id=receipt["id"], idempotency_key="reply-one", timeout=0)
                    self.assertEqual(pending.exception.code, "RESULT_PENDING")
                    frames = [frame async for frame in host.events(receipt["id"])]
                    self.assertEqual(frames[-1]["kind"], "run.waiting")
                    self.assertEqual(instance.last_run_id, receipt["id"])
                    result = await instance.aresume("Second reply", idempotency_key="reply-two", timeout=10)
                    self.assertEqual(result.text, "Both replies retained")
                    self.assertEqual(instance.last_run_id, receipt["id"])
                self.assertEqual(await host.store.call(lambda db: one(db, "SELECT COUNT(*) AS count FROM runs")["count"]), 1)
                self.assertEqual(model.calls, 3)
