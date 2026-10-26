"""Graph, actual local tools, durable receipts, and lifecycle integration."""

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from oea.contracts.configuration import AgentDefinition, Budget, CapabilityUse, Execution, Manager, Model, SkillBinding, UsePolicy, Workspace
from oea.contracts.domain import Completion, RunSpec
from oea.contracts.errors import RuntimeFault
from oea.tools.functions import tool
from oea.runtime.host import Host
from oea.storage.persistence import one, rows
from oea.runtime.context import RunContext
from oea.stdio.process_tree import OwnedProcessTree
from oea.models.api import ModelFailure
from oea.tools import ToolKit


def call(identity, name, arguments):
    return {"id": identity, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}


class FileModel:
    """Protocol fixture: tools and process execution remain real."""
    def __init__(self):
        self.calls = 0

    async def complete(self, model, messages, **options):
        self.calls += 1
        system = messages[0]["content"]
        paths = json.loads(system.split("Real workspace paths: ", 1)[1].split("\n\n", 1)[0])
        observed = [message for message in messages if message["role"] == "tool"]
        if len(observed) == 0:
            calls = [call("read", "read_file", {"path": str(Path(paths["project_dir"]) / "source.json")})]
        elif len(observed) == 1:
            source = str(Path(paths["project_dir"]) / "source.json")
            script = "import json; from pathlib import Path; data=json.loads(Path(" + repr(source) + ").read_text(encoding='utf-8')); Path('result.txt').write_text(str(sum(data['values'])),encoding='utf-8')"
            calls = [call("compute", "run_command", {"argv": [sys.executable, "-B", "-c", script]})]
        elif len(observed) == 2:
            calls = [call("publish", "publish_artifact", {"path": "result.txt", "name": "result.txt"})]
        else:
            calls = []
        return Completion(text="Sum: 10" if not calls else "", tool_calls=tuple(calls), finish_reason="tool_calls" if calls else "stop",
                          usage={"prompt_tokens": 100, "completion_tokens": 10})


class BarrierModel:
    def __init__(self, target=1):
        self.target, self.active, self.peak, self.calls = target, 0, 0, 0
        self.ready, self.release = asyncio.Event(), asyncio.Event()

    async def complete(self, model, messages, **options):
        self.active += 1
        self.calls += 1
        self.peak = max(self.peak, self.active)
        if self.active >= self.target:
            self.ready.set()
        try:
            await self.release.wait()
            return Completion(text="Complete", usage={"prompt_tokens": 30, "completion_tokens": 2})
        finally:
            self.active -= 1


class WaitModel:
    def __init__(self):
        self.calls = 0

    async def complete(self, model, messages, **options):
        self.calls += 1
        answered = any("Guidance for this run:" in str(message.get("content", "")) for message in messages)
        return Completion(text="Answer: 37" if answered else "", tool_calls=() if answered else (call("wait", "wait_for_input", {"prompt": "Provide the confirmed value"}),),
                          finish_reason="stop" if answered else "tool_calls", usage={"prompt_tokens": 50, "completion_tokens": 5})


class ToolModel:
    def __init__(self):
        self.calls = 0

    async def complete(self, model, messages, **options):
        self.calls += 1
        observed = any(message["role"] == "tool" for message in messages)
        return Completion(text="Submitted once" if observed else "", tool_calls=() if observed else (call("stable-submission", "submit", {"value": 37}),),
                          finish_reason="stop" if observed else "tool_calls", usage={"prompt_tokens": 50, "completion_tokens": 5})


class CommandModel:
    async def complete(self, model, messages, **options):
        return Completion(tool_calls=(call("command", "run_command", {"argv": [sys.executable, "-B", "-c", "import time; print('started',flush=True); time.sleep(30)"]}),),
                          finish_reason="tool_calls", usage={"prompt_tokens": 40, "completion_tokens": 5})


class EmptyThenValidModel:
    def __init__(self):
        self.calls = 0
        self.messages = []

    async def complete(self, model, messages, **options):
        self.calls += 1
        self.messages.append(messages)
        return Completion(text="" if self.calls == 1 else "Recovered output", usage={"prompt_tokens": 40, "completion_tokens": 5})


class SkillModel:
    def __init__(self):
        self.messages = []

    async def complete(self, model, messages, **options):
        self.messages.append(messages)
        loaded = any(message["role"] == "tool" for message in messages)
        return Completion(text="Skill used" if loaded else "", tool_calls=() if loaded else (call("load-skill", "read_skill", {"skill_id": "review"}),),
                          finish_reason="stop" if loaded else "tool_calls", usage={"prompt_tokens": 60, "completion_tokens": 5})


class HostTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="oea-v2-host-")
        self.root = Path(self.temporary.name)
        self.host = None

    async def asyncTearDown(self):
        if self.host:
            await self.host.aclose()
        self.temporary.cleanup()

    async def test_secret_in_tool_metadata_is_rejected_without_masking_parameter_names(self):
        class ObservingModel:
            def __init__(self):
                self.calls, self.tools = 0, None
            async def complete(self, model, messages, **options):
                self.calls += 1
                self.tools = options["tools"]
                return Completion(text="Complete", usage={"prompt_tokens": 30, "completion_tokens": 2})
        model = ObservingModel()
        await self.open(model)
        secret = "fixture-protected-model-key"
        @tool(description="Public parameters")
        def safe_query(api_key: str) -> str:
            return api_key
        @tool(description="Incorrect metadata: " + secret)
        def unsafe_query(value: str) -> str:
            return value
        self.host.register_tool(safe_query)
        self.host.register_tool(unsafe_query)
        saved = await self.host.agents.get("worker")
        definition = AgentDefinition.model_validate(saved["definition"])
        definition.model.credential_ref = "env:OEA_TEST_SCHEMA_KEY"
        definition.tools.functions = ("unsafe_query",)
        with patch.dict("os.environ", {"OEA_TEST_SCHEMA_KEY": secret}):
            await self.host.agents.save(definition, expected_revision=1)
            instance = await self.host.instances.create(self.session["id"], "worker")
            rejected = await self.host.runs.submit(RunSpec(instance_id=instance["id"], input="Use the declared capability", idempotency_key="unsafe-metadata"))
            with self.assertRaises(RuntimeFault) as unsafe:
                await self.host.runs.result(rejected.id, timeout=5)
            self.assertEqual(unsafe.exception.code, "TOOL_SCHEMA_CONTAINS_SECRET")
            self.assertEqual(model.calls, 0)
            definition.tools.functions = ("safe_query",)
            await self.host.agents.save(definition, expected_revision=2)
            accepted = await self.host.runs.submit(RunSpec(instance_id=instance["id"], input="Use public metadata", idempotency_key="safe-metadata"))
            await self.host.runs.result(accepted.id, timeout=5)
        declared = next(value for value in model.tools if value["function"]["name"] == "safe_query")
        self.assertEqual(declared["function"]["parameters"]["properties"]["api_key"]["type"], "string")
        self.assertEqual(model.calls, 1)

    async def test_model_receives_delivery_schema_and_required_tool_evidence_contract(self):
        prompts, invoked = [], []
        class ContractModel:
            async def complete(self, model, messages, **options):
                prompts.append(messages[0]["content"])
                used = any(value["role"] == "tool" for value in messages)
                return Completion(text=json.dumps({"answer": 37}) if used else "", tool_calls=() if used else (call("probe", "required_probe", {}),),
                                  finish_reason="stop" if used else "tool_calls", usage={"prompt_tokens": 30, "completion_tokens": 2})
        await self.open(ContractModel())
        @tool
        def required_probe() -> int:
            invoked.append(1)
            return 37
        self.host.register_tool(required_probe)
        saved = await self.host.agents.get("worker")
        definition = AgentDefinition.model_validate(saved["definition"])
        definition.tools.functions = ("required_probe",)
        schema = {"type": "object", "properties": {"answer": {"type": "integer"}}, "required": ["answer"], "additionalProperties": False}
        definition.execution.delivery.output_schema = schema
        await self.host.agents.save(definition, expected_revision=1)
        instance = await self.host.instances.create(self.session["id"], "worker")
        run = await self.host.runs.submit(RunSpec(instance_id=instance["id"], input="Return the checked value", idempotency_key="contract", policy=UsePolicy(tools=CapabilityUse(required=("required_probe",)))))
        result = await self.host.runs.result(run.id, timeout=5)
        self.assertEqual(result.data, {"answer": 37})
        self.assertEqual(invoked, [1])
        self.assertIn("Successful tool receipts required before delivery", prompts[0])
        self.assertIn("required_probe", prompts[0])
        self.assertEqual(json.loads(prompts[0].split("Delivery JSON Schema (return a JSON object as the final response, without surrounding prose):\n", 1)[1].split("\n\n", 1)[0]), schema)

    async def open(self, model, *, tools=None, workspace=None):
        self.host = await Host(self.root / "state", model_api=model, allow_commands=True).__aenter__()
        await self.host.projects.create("Project", project_id="p")
        await self.host.agents.save(AgentDefinition(id="worker", name="Worker", project_id="p",
            model=Model(base_url="https://example.invalid/v1", model="protocol-fixture"),
            tools=(tools or ToolKit.local(files=False)).bindings, workspace=workspace or Workspace()))
        self.session = await self.host.sessions.create("p", session_id="session")

    async def submit(self, name="instance", key="work", text="Complete the assigned work"):
        instance = await self.host.instances.create(self.session["id"], "worker", name=name)
        return await self.host.runs.submit(RunSpec(instance_id=instance["id"], input=text, idempotency_key=key))

    async def until(self, run_id, target, *, allow_review=False):
        async with asyncio.timeout(10):
            while True:
                changed = self.host.changed
                run = await self.host.runs.get(run_id)
                if run["state"] == target:
                    return run
                if run["state"] in {"failed", "cancelled"} or run["state"] == "needs_review" and not allow_review:
                    self.fail(str(run))
                await changed.wait()

    async def test_complete_file_command_artifact_delivery_on_actual_graph(self):
        project = self.root / "项目 资料"
        project.mkdir()
        source = project / "source.json"
        source.write_text(json.dumps({"values": [2, 3, 5]}), encoding="utf-8")
        original = source.read_bytes()
        model = FileModel()
        await self.open(model, tools=ToolKit.local(commands=True), workspace=Workspace(project=project))
        handle = await self.submit()
        result = await self.host.runs.result(handle.id, timeout=10)
        self.assertEqual(result.text, "Sum: 10")
        self.assertEqual(model.calls, 4)
        self.assertEqual(len(result.artifacts), 1)
        artifact = await self.host.store.call(lambda db: one(db, "SELECT path FROM artifacts WHERE id=?", (result.artifacts[0].id,)))
        self.assertEqual(Path(artifact["path"]).read_text(), "10")
        self.assertEqual(source.read_bytes(), original)
        replay = await self.submit()
        self.assertTrue(replay.reused)
        self.assertEqual((await self.host.runs.result(replay.id)).text, "Sum: 10")
        self.assertEqual(model.calls, 4)

    async def test_run_policy_filters_tools_and_requires_actual_success_receipt(self):
        submissions = []
        @tool
        def submit(value: int) -> dict:
            submissions.append(value)
            return {"accepted": value}
        model = ToolModel()
        await self.open(model, tools=ToolKit.local(files=False).with_tools(submit))
        self.host.register_tool(submit)
        instance = await self.host.instances.create(self.session["id"], "worker", name="policy")
        policy = UsePolicy(tools=CapabilityUse(allowed=("submit",), required=("submit",)))
        run = await self.host.runs.submit(RunSpec(instance_id=instance["id"], input="Submit", idempotency_key="intent", policy=policy))
        await self.host.runs.result(run.id, timeout=10)
        self.assertEqual(submissions, [37])
        required = await self.host.store.call(lambda db: rows(db, "SELECT state FROM actions WHERE run_id=? AND name='submit'", (run.id,)))
        self.assertEqual(required, [{"state": "completed"}])

    async def test_missing_required_tool_fails_before_any_model_call(self):
        model = BarrierModel()
        model.release.set()
        await self.open(model)
        instance = await self.host.instances.create(self.session["id"], "worker", name="policy")
        policy = UsePolicy(tools=CapabilityUse(allowed=("unbound_tool",), required=("unbound_tool",)))
        run = await self.host.runs.submit(RunSpec(instance_id=instance["id"], input="Work", idempotency_key="intent", policy=policy))
        with self.assertRaises(RuntimeFault) as error:
            await self.host.runs.result(run.id, timeout=10)
        self.assertEqual(error.exception.code, "REQUIRED_TOOL_UNAVAILABLE")
        self.assertEqual(model.calls, 0)

    async def test_shared_budget_scope_and_concurrent_reservations(self):
        model = BarrierModel()
        await self.open(model)
        budget = await self.host.budgets.create("p", Budget(max_model_calls=1), "limit", session_id=self.session["id"])
        first = await self.host.instances.create(self.session["id"], "worker", name="first")
        second = await self.host.instances.create(self.session["id"], "worker", name="second")
        run = await self.host.runs.submit(RunSpec(instance_id=first["id"], input="First", idempotency_key="first", budget_id=budget["id"]))
        await asyncio.wait_for(model.ready.wait(), 10)
        other = await self.host.runs.submit(RunSpec(instance_id=second["id"], input="Second", idempotency_key="second", budget_id=budget["id"]))
        try:
            with self.assertRaises(RuntimeFault) as error:
                await self.host.runs.result(other.id, timeout=10)
            self.assertEqual(error.exception.code, "BUDGET_EXHAUSTED")
            in_flight = await self.host.budgets.get(budget["id"])
            self.assertIsNone(in_flight["provider_reported"]["input_tokens"])
            self.assertGreater(in_flight["reserved"]["input_tokens"], 0)
            different = await self.host.sessions.create("p")
            target = await self.host.instances.create(different["id"], "worker")
            with self.assertRaises(RuntimeFault) as error:
                await self.host.runs.submit(RunSpec(instance_id=target["id"], input="Wrong session", idempotency_key="invalid", budget_id=budget["id"]))
            self.assertEqual(error.exception.code, "BUDGET_MISMATCH")
        finally:
            model.release.set()
        await self.host.runs.result(run.id, timeout=10)
        self.assertEqual(model.calls, 1)
        settled = await self.host.budgets.get(budget["id"])
        self.assertEqual(settled["provider_reported"]["input_tokens"], 30)
        self.assertEqual(settled["reserved"]["input_tokens"], 0)

    async def test_twenty_independent_instances_are_actually_simultaneously_inflight(self):
        model = BarrierModel(target=20)
        await self.open(model)
        handles = [await self.submit(name="instance-" + str(index)) for index in range(20)]
        await asyncio.wait_for(model.ready.wait(), 10)
        self.assertEqual(model.active, 20)
        self.assertEqual(model.peak, 20)
        model.release.set()
        results = await asyncio.gather(*(self.host.runs.result(handle.id, timeout=10) for handle in handles))
        self.assertEqual(len(results), 20)

    async def test_one_instance_has_one_writer_and_queued_intents_keep_running(self):
        model = BarrierModel()
        await self.open(model)
        handles = [await self.submit(key="work-" + str(index)) for index in range(3)]
        await asyncio.wait_for(model.ready.wait(), 5)
        self.assertEqual(model.active, 1)
        model.release.set()
        await asyncio.gather(*(self.host.runs.result(handle.id, timeout=10) for handle in handles))
        self.assertEqual(model.peak, 1)
        self.assertEqual(model.calls, 3)

    async def test_wait_reply_and_response_loss_use_original_run_and_budget(self):
        model = WaitModel()
        await self.open(model)
        handle = await self.submit()
        await self.until(handle.id, "waiting")
        await self.host.runs.resume(handle.id, "reply-1", input={"value": 37})
        await self.host.runs.resume(handle.id, "reply-1", input={"value": 37})
        result = await self.host.runs.result(handle.id, timeout=10)
        self.assertEqual(result.text, "Answer: 37")
        self.assertEqual(model.calls, 2)
        self.assertEqual((await self.host.runs.get(handle.id))["budget_id"], handle.budget_id)
        replies = await self.host.store.call(lambda db: rows(db, "SELECT * FROM conversation WHERE id LIKE ?", (handle.id + ":reply:%",)))
        self.assertEqual(len(replies), 1)

    async def test_restart_after_tool_commit_does_not_repeat_the_external_action(self):
        model = ToolModel()
        calls = []

        @tool(side_effects="external")
        async def submit(value: int) -> dict:
            calls.append(value)
            return {"value": value}

        toolkit = ToolKit.local(files=False).with_tools(submit)
        await self.open(model, tools=toolkit)
        self.host.register_tool(submit)
        original = RunContext.execute_tools
        injected = False

        async def crash(context, state):
            nonlocal injected
            result = await original(context, state)
            if not injected:
                injected = True
                raise RuntimeError("Crash before graph checkpoint")
            return result

        with patch.object(RunContext, "execute_tools", crash):
            handle = await self.submit()
            with self.assertRaises(RuntimeFault):
                await self.host.runs.result(handle.id, timeout=10)
        await self.host.aclose()
        self.host = await Host(self.root / "state", model_api=model).__aenter__()
        self.host.register_tool(submit)
        await self.host.runs.resume(handle.id, "resume-original")
        result = await self.host.runs.result(handle.id, timeout=10)
        self.assertEqual(result.text, "Submitted once")
        self.assertEqual(calls, [37])
        self.assertEqual(model.calls, 2)

    async def test_second_host_cannot_open_the_same_state_directory(self):
        await self.open(WaitModel())
        with self.assertRaises(RuntimeFault) as caught:
            async with Host(self.root / "state"):
                pass
        self.assertEqual(caught.exception.code, "RUNTIME_ALREADY_OPEN")

    async def test_actual_command_cancel_confirms_stop_and_keeps_the_call_receipt(self):
        await self.open(CommandModel(), tools=ToolKit.local(commands=True))
        started = asyncio.Event()
        captured = []

        class ObservedTree(OwnedProcessTree):
            def __init__(self, process):
                super().__init__(process)
                captured.append(process)
                started.set()

        with patch("oea.workspace.service.OwnedProcessTree", ObservedTree):
            handle = await self.submit()
            await asyncio.wait_for(started.wait(), 10)
            status = await self.host.runs.cancel(handle.id)
        self.assertEqual(status["state"], "cancelled")
        self.assertIsNotNone(status["cancel_requested_at"])
        self.assertIsNotNone(status["stopped_at"])
        self.assertIsNotNone(captured[0].returncode)
        receipt = await self.host.store.call(lambda db: one(db, "SELECT * FROM actions WHERE run_id=? AND name='run_command'", (handle.id,)))
        self.assertEqual(receipt["state"], "completed")
        self.assertTrue(json.loads(receipt["result_json"])["process_stopped"])

    async def test_fixing_model_configuration_resumes_the_same_run_without_rebuilding_instance(self):
        model = WaitModel()
        await self.open(model)
        value = (await self.host.agents.get("worker"))["definition"]
        value["model"] = Model().model_dump()
        await self.host.agents.save(value)
        handle = await self.submit()
        with self.assertRaises(RuntimeFault) as caught:
            await self.host.runs.result(handle.id, timeout=10)
        self.assertEqual(caught.exception.code, "MODEL_NOT_CONFIGURED")
        before = await self.host.instances.get(handle.instance_id)
        value["model"] = Model(base_url="https://example.invalid/v1", model="configured").model_dump()
        await self.host.agents.save(value)
        await self.host.runs.resume(handle.id, "configured-model", input="The confirmed answer is 37")
        self.assertEqual((await self.host.runs.result(handle.id, timeout=10)).text, "Answer: 37")
        after = await self.host.instances.get(handle.instance_id)
        self.assertEqual(before["workspace"]["work_dir"], after["workspace"]["work_dir"])
        self.assertEqual((await self.host.runs.get(handle.id))["budget_id"], handle.budget_id)

    async def test_explicit_delivery_repair_continues_in_the_original_graph(self):
        model = EmptyThenValidModel()
        await self.open(model)
        handle = await self.submit()
        with self.assertRaises(RuntimeFault) as caught:
            await self.host.runs.result(handle.id, timeout=10)
        self.assertEqual(caught.exception.code, "EMPTY_OUTPUT")
        await self.host.runs.resume(handle.id, "repair-delivery")
        self.assertEqual((await self.host.runs.result(handle.id, timeout=10)).text, "Recovered output")
        self.assertEqual(model.calls, 2)
        self.assertIn("EMPTY_OUTPUT", json.dumps(model.messages[-1]))
        initial = await self.host.store.call(lambda db: rows(db, "SELECT id FROM conversation WHERE input_run_id=?", (handle.id,)))
        self.assertEqual(len(initial), 1)
        self.assertEqual((await self.host.runs.get(handle.id))["budget_id"], handle.budget_id)

    async def test_cached_source_read_cannot_bypass_a_revoked_origin_tool(self):
        model = BarrierModel()
        await self.open(model, tools=ToolKit.local())
        handle = await self.submit()
        await asyncio.wait_for(model.ready.wait(), 10)
        context = self.host.active[handle.id]
        reference = await self.host.contexts.source(context.run, context.instance, "retained evidence", "tool:read_file")
        arguments = {"reference": reference}
        first = await self.host.tools.invoke(handle.id, "read_context", arguments, "same-read")
        self.assertEqual(first["text"], "retained evidence")
        definition = (await self.host.agents.get("worker"))["definition"]
        definition["tools"]["local"] = []
        await self.host.agents.save(definition)
        with self.assertRaises(RuntimeFault) as caught:
            await self.host.tools.invoke(handle.id, "read_context", arguments, "same-read")
        self.assertEqual(caught.exception.code, "PERMISSION_DENIED")
        model.release.set()
        await self.host.runs.result(handle.id, timeout=10)

    async def test_required_skill_has_one_body_in_model_input_and_keeps_its_loaded_version(self):
        folder = self.root / "review"
        folder.mkdir()
        file = folder / "SKILL.md"
        file.write_text("---\nname: review\ndescription: Review evidence\n---\nREQUIRED_UNIQUE_MARKER", encoding="utf-8")
        model = SkillModel()
        await self.open(model)
        definition = (await self.host.agents.get("worker"))["definition"]
        definition["skills"] = [SkillBinding(id="review", path=folder, required=True).model_dump(mode="json")]
        await self.host.agents.save(definition)
        original = RunContext.execute_tools
        failed = False

        async def crash(context, state):
            nonlocal failed
            result = await original(context, state)
            if not failed:
                failed = True
                raise RuntimeError("Crash after Skill receipt")
            return result

        with patch.object(RunContext, "execute_tools", crash):
            handle = await self.submit()
            with self.assertRaises(RuntimeFault):
                await self.host.runs.result(handle.id, timeout=10)
        await self.host.aclose()
        file.write_text("---\nname: review\ndescription: Updated evidence\n---\nNEW_SKILL_MARKER", encoding="utf-8")
        self.host = await Host(self.root / "state", model_api=model).__aenter__()
        await self.host.runs.resume(handle.id, "resume-skill")
        self.assertEqual((await self.host.runs.result(handle.id, timeout=10)).text, "Skill used")
        self.assertEqual(len(model.messages), 2)
        for messages in model.messages:
            self.assertEqual(json.dumps(messages).count("REQUIRED_UNIQUE_MARKER"), 1)
            self.assertNotIn("NEW_SKILL_MARKER", json.dumps(messages))

    async def test_partial_model_result_is_readable_through_the_public_action_service(self):
        text = "retained partial evidence " * 1500

        class PartialModel:
            async def complete(self, model, messages, **options):
                raise ModelFailure("MODEL_STREAM_INTERRUPTED", "Stream interrupted", Completion(text=text))

        await self.open(PartialModel())
        handle = await self.submit()
        with self.assertRaises(RuntimeFault):
            await self.host.runs.result(handle.id, timeout=10)
        inspection = await self.host.runs.inspect(handle.id)
        action = next(item for item in inspection["actions"] if item["name"] == "model.complete")
        self.assertTrue(action["result_available"])
        body, offset = "", 0
        while True:
            page = await self.host.runs.action(action["id"], offset=offset, limit=8192)
            body += page["text"]
            if page["next_offset"] is None:
                break
            offset = page["next_offset"]
        self.assertEqual(json.loads(body)["message"]["content"], text)

    async def test_failed_model_partial_survives_recovery_and_restart_as_original_attempt(self):
        text = "原始部分证据 / retained evidence\n" * 700
        class RecoveringModel:
            def __init__(self):
                self.calls = 0
            async def complete(self, model, messages, **options):
                self.calls += 1
                if self.calls == 1:
                    raise ModelFailure("MODEL_STREAM_INTERRUPTED", "Original stream interrupted",
                        Completion(text=text, usage={"prompt_tokens": 70, "completion_tokens": 9}))
                return Completion(text="Recovered delivery", usage={"prompt_tokens": 80, "completion_tokens": 7})
        model = RecoveringModel()
        await self.open(model)
        handle = await self.submit()
        with self.assertRaises(RuntimeFault):
            await self.host.runs.result(handle.id, timeout=10)
        inspection = await self.host.runs.inspect(handle.id)
        action = next(value for value in inspection["actions"] if value["name"] == "model.complete")
        original = await self.host.runs.action(action["id"])
        attempt_id = original["attempt_id"]
        self.assertIsNotNone(attempt_id)
        await self.host.runs.resume(handle.id, "recover-original-stream")
        self.assertEqual((await self.host.runs.result(handle.id, timeout=10)).text, "Recovered delivery")
        self.assertEqual(model.calls, 2)
        await self.host.aclose()
        self.host = await Host(self.root / "state", model_api=model).__aenter__()
        body, offset = "", 0
        while True:
            page = await self.host.runs.action(action["id"], attempt_id=attempt_id, offset=offset, limit=4096)
            self.assertFalse(page["is_current"])
            body += page["text"] or ""
            if page["next_offset"] is None:
                break
            offset = page["next_offset"]
        self.assertEqual(json.loads(body)["message"]["content"], text)
        current = await self.host.runs.action(action["id"])
        self.assertEqual(json.loads(current["text"])["message"]["content"], "Recovered delivery")
        attempts = await self.host.runs.attempts(action["id"], "p")
        self.assertEqual([value["attempt_id"] for value in attempts["items"]], [attempt_id])
        self.assertEqual(attempts["items"][0]["error"]["code"], "MODEL_STREAM_INTERRUPTED")
        with self.assertRaises(RuntimeFault):
            await self.host.runs.attempts(action["id"], "other")
        self.assertEqual(model.calls, 2)
        budget = await self.host.budgets.get(handle.budget_id)
        self.assertEqual(budget["usage"]["model_calls"], 2)
        self.assertEqual(budget["provider_reported"]["input_tokens"], 150)

    async def test_resumption_does_not_allow_duplicate_inflight_model_step(self):
        class WaitingModel:
            def __init__(self):
                self.calls = 0
                self.ready, self.release = asyncio.Event(), asyncio.Event()
            async def complete(self, model, messages, **options):
                self.calls += 1
                if self.calls == 1:
                    return Completion(tool_calls=(call("wait", "wait_for_input", {"prompt": "Confirm"}),),
                        finish_reason="tool_calls", usage={"prompt_tokens": 50, "completion_tokens": 5})
                self.ready.set()
                await self.release.wait()
                return Completion(text="Continued once", usage={"prompt_tokens": 60, "completion_tokens": 6})
        model = WaitingModel()
        await self.open(model)
        handle = await self.submit()
        await self.until(handle.id, "waiting")
        await self.host.runs.resume(handle.id, "confirmed", input="Continue")
        try:
            await asyncio.wait_for(model.ready.wait(), 10)
            with self.assertRaises(RuntimeFault) as error:
                await asyncio.wait_for(self.host.active[handle.id].call_model({"step": 1}), 1)
            self.assertEqual(error.exception.code, "MODEL_IN_FLIGHT")
        finally:
            model.release.set()
        self.assertEqual((await self.host.runs.result(handle.id, timeout=10)).text, "Continued once")
        self.assertEqual(model.calls, 2)

    async def test_artifact_unicode_pages_and_manager_read_scope(self):
        text = ("边界证据 😀 / evidence\n" * 73)
        class ArtifactModel:
            async def complete(self, model, messages, **options):
                observed = [message for message in messages if message["role"] == "tool"]
                calls = [call("write", "write_file", {"path": "证据.txt", "content": text})] if not observed else [call("publish", "publish_artifact", {"path": "证据.txt", "name": "证据.txt"})] if len(observed) == 1 else []
                return Completion(text="Artifact published" if not calls else "", tool_calls=tuple(calls), finish_reason="tool_calls" if calls else "stop", usage={"prompt_tokens": 50, "completion_tokens": 5})
        await self.open(ArtifactModel(), tools=ToolKit.local())
        owner = await self.submit(name="source")
        result = await self.host.runs.result(owner.id, timeout=10)
        artifact = result.artifacts[0]
        pieces, offset = [], 0
        while True:
            page = await self.host.artifacts.read(artifact.id, "p", offset=offset, limit=64)
            pieces.append(page["text"])
            if page["next_offset"] is None:
                break
            self.assertGreater(page["next_offset"], offset)
            offset = page["next_offset"]
        self.assertEqual("".join(pieces), text)
        with self.assertRaises(RuntimeFault):
            await self.host.artifacts.read(artifact.id, "other")
        model = self.host.model_api = BarrierModel()
        unrelated = await self.submit(name="unrelated", key="unrelated")
        try:
            await asyncio.wait_for(model.ready.wait(), 10)
            with self.assertRaises(RuntimeFault) as error:
                await self.host.tools.invoke(unrelated.id, "read_artifact", {"artifact_id": artifact.id}, "wrong-root")
            self.assertEqual(error.exception.code, "PERMISSION_DENIED")
        finally:
            model.release.set()
        await self.host.runs.result(unrelated.id, timeout=10)
        definition = (await self.host.agents.get("worker"))["definition"]
        definition["manager"] = Manager(model=Model(base_url="https://example.invalid/v1", model="manager")).model_dump(mode="json")
        await self.host.agents.save(definition)
        await self.host.sessions.set_mode(self.session["id"], "collaborative", 0)
        manager = await self.host.instances.create(self.session["id"], "worker", name="manager", role="manager")
        model = self.host.model_api = BarrierModel()
        related = await self.host.runs.submit(RunSpec(instance_id=manager["id"], parent_run_id=owner.id, input="Read the related evidence", idempotency_key="manager"))
        try:
            await asyncio.wait_for(model.ready.wait(), 10)
            context = self.host.active[related.id]
            self.assertNotIn("write_file", context.tool_names())
            self.assertNotIn("run_command", context.tool_names())
            read = await self.host.tools.invoke(related.id, "read_artifact", {"artifact_id": artifact.id, "limit": 64}, "related-read")
            self.assertEqual(read["text"], text[:len(read["text"])])
            self.assertGreater(len(read["text"]), 0)
        finally:
            model.release.set()
        await self.host.runs.result(related.id, timeout=10)

    async def test_default_graph_uses_the_selected_root_budget_for_its_loop_boundary(self):
        observed = []
        @tool
        def measure(value: int) -> dict:
            observed.append(value)
            return {"observed": value}
        class StagesModel:
            def __init__(self):
                self.calls = 0
            async def complete(self, model, messages, **options):
                self.calls += 1
                calls = (call("stage-" + str(self.calls), "measure", {"value": self.calls}),) if self.calls <= 8 else ()
                return Completion(text="Eight stages complete" if not calls else "", tool_calls=calls, finish_reason="tool_calls" if calls else "stop", usage={"prompt_tokens": 50, "completion_tokens": 5})
        model = StagesModel()
        await self.open(model, tools=ToolKit.local(files=False).with_tools(measure))
        self.host.register_tool(measure)
        definition = AgentDefinition.model_validate((await self.host.agents.get("worker"))["definition"])
        await self.host.agents.save(definition.model_copy(update={"execution": Execution(budget=Budget(max_model_calls=1))}))
        budget = await self.host.budgets.create("p", Budget(max_model_calls=12), "root-policy", session_id=self.session["id"])
        instance = await self.host.instances.create(self.session["id"], "worker")
        run = await self.host.runs.submit(RunSpec(instance_id=instance["id"], input="Observe all eight stages", idempotency_key="stages", budget_id=budget["id"]))
        self.assertEqual((await self.host.runs.result(run.id, timeout=15)).text, "Eight stages complete")
        self.assertEqual(observed, list(range(1, 9)))
        self.assertEqual(model.calls, 9)
        self.assertEqual((await self.host.budgets.get(budget["id"]))["usage"]["model_calls"], 9)

    async def test_instance_work_directory_survives_default_workspace_configuration_changes(self):
        model = BarrierModel()
        await self.open(model, workspace=Workspace(work=self.root / "first-work"))
        handle = await self.submit()
        await asyncio.wait_for(model.ready.wait(), 10)
        first = await self.host.instances.get(handle.instance_id)
        work = Path(first["workspace"]["work_dir"])
        (work / "partial.txt").write_text("retained work", encoding="utf-8")
        model.release.set()
        await self.host.runs.result(handle.id, timeout=10)
        definition = (await self.host.agents.get("worker"))["definition"]
        definition["workspace"]["work"] = str(self.root / "new-default-work")
        await self.host.agents.save(definition)
        following = await self.submit(key="following")
        await self.host.runs.result(following.id, timeout=10)
        second = await self.host.instances.get(handle.instance_id)
        self.assertEqual(second["workspace"]["work_dir"], str(work))
        self.assertEqual((work / "partial.txt").read_text(), "retained work")
        self.assertFalse((self.root / "new-default-work" / handle.instance_id).exists())

    async def test_workspace_configuration_fix_does_not_require_a_new_run_or_instance(self):
        model = FileModel()
        await self.open(model, tools=ToolKit.local(commands=True), workspace=Workspace(project=self.root / "missing-input"))
        handle = await self.submit()
        with self.assertRaises(RuntimeFault) as caught:
            await self.host.runs.result(handle.id, timeout=10)
        self.assertEqual(caught.exception.code, "WORKSPACE_UNAVAILABLE")
        project = self.root / "actual-input"
        project.mkdir()
        (project / "source.json").write_text(json.dumps({"values": [2, 3, 5]}), encoding="utf-8")
        definition = (await self.host.agents.get("worker"))["definition"]
        definition["workspace"]["project"] = str(project)
        await self.host.agents.save(definition)
        await self.host.runs.resume(handle.id, "fixed-input-folder")
        self.assertEqual((await self.host.runs.result(handle.id, timeout=10)).text, "Sum: 10")
        self.assertEqual((await self.host.runs.get(handle.id))["instance_id"], handle.instance_id)
        self.assertEqual((await self.host.runs.get(handle.id))["budget_id"], handle.budget_id)

    async def test_manager_uses_its_own_model_and_does_not_inherit_the_main_program_or_skills(self):
        captured = []

        class ManagerModel:
            async def complete(self, model, messages, **options):
                captured.append((model, messages, options.get("tools", [])))
                return Completion(text="Manager response", usage={"prompt_tokens": 40, "completion_tokens": 5})

        await self.open(ManagerModel(), tools=ToolKit.local(commands=True))
        definition = (await self.host.agents.get("worker"))["definition"]
        definition["model"] = Model().model_dump()
        definition["instructions"] = {"system": "PRIVATE_MAIN_INSTRUCTION", "agent_md": ""}
        definition["execution"]["program"] = ["unavailable-business-program"]
        definition["skills"] = [SkillBinding(id="business", path=self.root / "private-skill", required=True).model_dump(mode="json")]
        definition["manager"] = Manager(model=Model(base_url="https://example.invalid/v1", model="manager-only")).model_dump(mode="json")
        await self.host.agents.save(definition)
        await self.host.store.call(lambda db: db.execute("UPDATE sessions SET mode='collaborative' WHERE id=?", (self.session["id"],)), transaction=True)
        instance = await self.host.instances.create(self.session["id"], "worker", name="manager", role="manager")
        handle = await self.host.runs.submit(RunSpec(instance_id=instance["id"], input="Coordinate this explicit request", idempotency_key="manager"))
        self.assertEqual((await self.host.runs.result(handle.id, timeout=10)).text, "Manager response")
        self.assertEqual(captured[0][0].model, "manager-only")
        self.assertNotIn("PRIVATE_MAIN_INSTRUCTION", json.dumps(captured[0][1]))
        self.assertNotIn("private-skill", json.dumps(captured[0][1]))
        self.assertNotIn("run_command", [item["function"]["name"] for item in captured[0][2]])

    async def test_non_cancellable_call_retains_original_result_then_waits_for_explicit_resume(self):
        model = ToolModel()
        started, release = asyncio.Event(), asyncio.Event()
        submitted = []

        @tool(side_effects="external", timeout_seconds=0.05)
        async def submit(value: int) -> dict:
            submitted.append(value)
            started.set()
            await release.wait()
            return {"value": value}

        await self.open(model, tools=ToolKit.local(files=False).with_tools(submit))
        self.host.register_tool(submit)
        try:
            handle = await self.submit()
            await asyncio.wait_for(started.wait(), 5)
            with self.assertRaises(RuntimeFault):
                await self.host.runs.result(handle.id, timeout=10)
            status = await self.host.runs.get(handle.id)
            self.assertEqual(status["state"], "needs_review")
            self.assertIsNone(status["stopped_at"])
            self.assertEqual(model.calls, 1)
            release.set()
            await self.until(handle.id, "waiting", allow_review=True)
            self.assertEqual(model.calls, 1)
            await self.host.runs.resume(handle.id, "continue-original")
            self.assertEqual((await self.host.runs.result(handle.id, timeout=10)).text, "Submitted once")
            self.assertEqual(submitted, [37])
        finally:
            release.set()

    async def test_cancel_of_non_cancellable_call_is_not_confirmed_until_it_actually_ends(self):
        model = ToolModel()
        started, release = asyncio.Event(), asyncio.Event()

        @tool(side_effects="external")
        async def submit(value: int) -> dict:
            started.set()
            await release.wait()
            return {"value": value}

        await self.open(model, tools=ToolKit.local(files=False).with_tools(submit))
        self.host.register_tool(submit)
        try:
            handle = await self.submit()
            await asyncio.wait_for(started.wait(), 5)
            status = await self.host.runs.cancel(handle.id)
            self.assertEqual(status["state"], "needs_review")
            self.assertIsNone(status["stopped_at"])
            release.set()
            await self.until(handle.id, "cancelled", allow_review=True)
            self.assertEqual(model.calls, 1)
        finally:
            release.set()

    async def test_elapsed_budget_stops_the_actual_owned_command(self):
        await self.open(CommandModel(), tools=ToolKit.local(commands=True))
        definition = (await self.host.agents.get("worker"))["definition"]
        definition["execution"]["budget"]["max_elapsed_seconds"] = 1
        await self.host.agents.save(definition)
        captured = []

        class ObservedTree(OwnedProcessTree):
            def __init__(self, process):
                super().__init__(process)
                captured.append(process)

        with patch("oea.workspace.service.OwnedProcessTree", ObservedTree):
            handle = await self.submit()
            with self.assertRaises(RuntimeFault) as caught:
                await self.host.runs.result(handle.id, timeout=10)
        self.assertEqual(caught.exception.code, "BUDGET_EXHAUSTED")
        self.assertEqual((await self.host.runs.get(handle.id))["state"], "failed")
        self.assertTrue(captured)
        self.assertIsNotNone(captured[0].returncode)
