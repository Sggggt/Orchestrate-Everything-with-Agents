"""Actual manager tools, instance runs, root budgets and dependency interrupts."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from oea import AgentDefinition, Communication, Completion, Manager, MessageSpec, Model, RunSpec, Tools
from oea.runtime.host import Host
from oea.contracts.errors import RuntimeFault
from oea.storage.persistence import rows


def call(identity, name, arguments):
    return {"id": identity, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


class CooperationModel:
    """Model protocol only is synthetic; accepted work and all tools are real."""
    def __init__(self, *, short=False, instances=2):
        self.short, self.instances = short, instances
        self.worker_started, self.release = asyncio.Event(), asyncio.Event()
        self.worker_active = 0
        self.manager_calls, self.worker_calls, self.schemas = [], [], []

    async def complete(self, model, messages, **options):
        if model.model.startswith("main-"):
            self.worker_calls.append(model.model)
            self.worker_active += 1
            if self.worker_active >= self.instances:
                self.worker_started.set()
            try:
                await self.release.wait()
                return Completion(text="Verified instance evidence", usage={"prompt_tokens": 60, "completion_tokens": 6})
            finally:
                self.worker_active -= 1
        self.manager_calls.append(model.model)
        self.schemas.append([tool["function"]["name"] for tool in options.get("tools", [])])
        observed = [json.loads(message["content"]) for message in messages if message["role"] == "tool"]
        if not observed:
            calls = [call("create", "create_instance", {"name": "instance"})]
        elif len(observed) == 1:
            calls = [call("assign", "assign_work", {"instance_id": observed[0]["id"], "input": "Complete the assigned work", "idempotency_key": "confirmed-work"})]
        elif self.short:
            calls = []
        elif len(observed) == 2:
            calls = [call("wait", "wait_for_runs", {"run_ids": [observed[1]["id"]], "policy": "all_succeeded"})]
        elif len(observed) == 3:
            calls = [call("inspect", "inspect_work", {"run_id": observed[1]["id"]})]
        else:
            calls = []
        return Completion(text="Coordination turn complete" if not calls else "", tool_calls=tuple(calls), finish_reason="tool_calls" if calls else "stop", usage={"prompt_tokens": 80, "completion_tokens": 8})


class CoordinationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="oea-coordination-")
        self.root = Path(self.temporary.name)
        self.host = None

    async def asyncTearDown(self):
        if self.host:
            self.host.model_api.release.set()
            await self.host.aclose()
        self.temporary.cleanup()

    async def open(self, model):
        self.host = await Host(self.root / "state", model_api=model).__aenter__()
        await self.host.projects.create("Project", project_id="p")
        for identity in ("left", "right"):
            await self.host.agents.save(AgentDefinition(id=identity, name=identity.title(), project_id="p",
                model=Model(base_url="https://example.invalid/v1", model="main-" + identity), tools=Tools(),
                manager=Manager(model=Model(base_url="https://example.invalid/v1", model="manager-" + identity),
                    communication=Communication(departments=("right" if identity == "left" else "left",), can_request=True))))
        self.session = await self.host.sessions.create("p")
        await self.host.sessions.set_mode(self.session["id"], "collaborative", 0)

    async def managers(self, departments=("left", "right")):
        return await self.host.messages.send(MessageSpec(session_id=self.session["id"], recipients=tuple("department:" + identity for identity in departments),
            content="Coordinate verified work in the selected departments", purpose="request", idempotency_key="root-work"))

    async def until(self, run_id, state):
        async with asyncio.timeout(15):
            while True:
                changed = self.host.run_signal(run_id)
                value = await self.host.runs.get(run_id)
                if value["state"] == state:
                    return value
                if value["state"] in {"failed", "cancelled", "needs_review"}:
                    self.fail(str(value))
                await changed.wait()

    async def test_two_managers_use_native_wait_and_share_root_usage(self):
        model = CooperationModel()
        await self.open(model)
        message = await self.managers()
        await asyncio.wait_for(model.worker_started.wait(), 15)
        manager_ids = [item["run_id"] for item in message["deliveries"]]
        waiting = await asyncio.gather(*(self.until(identity, "waiting") for identity in manager_ids))
        self.assertTrue(all(item["wait"]["kind"] == "runs" for item in waiting))
        self.assertTrue(all("assign_work" in names and "run_command" not in names for names in model.schemas))
        model.release.set()
        results = await asyncio.gather(*(self.host.runs.result(identity, timeout=20) for identity in manager_ids))
        self.assertEqual([result.text for result in results], ["Coordination turn complete"] * 2)
        actual = await self.host.store.call(lambda db: rows(db, "SELECT id,model_role,root_run_id,budget_id,parent_run_id FROM runs ORDER BY rowid"))
        self.assertEqual(len(actual), 4)
        self.assertEqual({item["root_run_id"] for item in actual}, {message["root_run_id"]})
        self.assertEqual(len({item["budget_id"] for item in actual}), 1)
        self.assertEqual(len(model.worker_calls), 2)
        self.assertEqual(len(model.manager_calls), 10)
        usage = await self.host.budgets.get(actual[0]["budget_id"])
        self.assertEqual(usage["usage"]["model_calls"], 12)
        self.assertEqual(usage["provider_reported"]["input_tokens"], 920)
        assignments = await self.host.store.call(lambda db: rows(db, "SELECT state FROM actions WHERE name='assign_work'"))
        self.assertEqual(assignments, [{"state": "completed"}, {"state": "completed"}])
        # Reading the original message and runs never dispatches instances again.
        await self.host.messages.get(message["id"], project_id="p")
        await asyncio.gather(*(self.host.runs.inspect(identity) for identity in manager_ids))
        self.assertEqual(len(model.worker_calls), 2)

    async def test_completed_manager_turn_does_not_stop_instance_but_root_cancel_does(self):
        model = CooperationModel(short=True, instances=2)
        await self.open(model)
        message = await self.managers(("left",))
        manager_id = message["deliveries"][0]["run_id"]
        await self.host.runs.result(manager_id, timeout=15)
        unrelated = await self.host.instances.create(self.session["id"], "right", name="independent")
        separate = await self.host.runs.submit(RunSpec(instance_id=unrelated["id"], input="Independent", idempotency_key="independent"))
        await asyncio.wait_for(model.worker_started.wait(), 15)
        child = (await self.host.store.call(lambda db: rows(db, "SELECT id FROM runs WHERE parent_run_id=?", (manager_id,))))[0]["id"]
        self.assertEqual((await self.host.runs.get(child))["state"], "running")
        cancelled = await self.host.runs.cancel(manager_id)
        self.assertEqual(cancelled["state"], "completed")
        self.assertEqual(cancelled["cancellation"]["state"], "confirmed")
        self.assertEqual((await self.host.runs.get(child))["state"], "cancelled")
        self.assertEqual((await self.host.runs.get(separate.id))["state"], "running")
        model.release.set()
        self.assertEqual((await self.host.runs.result(separate.id, timeout=10)).text, "Verified instance evidence")

    async def test_mode_switch_preserves_instance_and_denies_future_manager_calls(self):
        model = CooperationModel(instances=1)
        await self.open(model)
        message = await self.managers(("left",))
        manager_id = message["deliveries"][0]["run_id"]
        await self.until(manager_id, "waiting")
        calls = len(model.manager_calls)
        await self.host.sessions.set_mode(self.session["id"], "direct", 1)
        model.release.set()
        async with asyncio.timeout(15):
            while True:
                changed = self.host.run_signal(manager_id)
                value = await self.host.runs.get(manager_id)
                if value["state"] == "failed":
                    break
                await changed.wait()
        self.assertEqual(value["error"]["code"], "MODE_CHANGED")
        self.assertEqual(len(model.manager_calls), calls)
        children = await self.host.store.call(lambda db: rows(db, "SELECT state FROM runs WHERE parent_run_id=?", (manager_id,)))
        self.assertEqual(children, [{"state": "completed"}])

    async def test_manager_failure_after_assignment_commit_recovers_original_receipt(self):
        model = CooperationModel(short=True, instances=1)
        await self.open(model)
        original = self.host.contexts.tool_result
        crashed = False
        async def fail_after_commit(run, instance, policy, call, result, **options):
            nonlocal crashed
            if call["function"]["name"] == "assign_work" and not crashed:
                crashed = True
                raise RuntimeFault("SIMULATED_CHECKPOINT_FAILURE", "Consumer stopped after assignment receipt")
            return await original(run, instance, policy, call, result, **options)
        with patch.object(self.host.contexts, "tool_result", fail_after_commit):
            message = await self.managers(("left",))
            manager_id = message["deliveries"][0]["run_id"]
            async with asyncio.timeout(15):
                while True:
                    changed = self.host.run_signal(manager_id)
                    parent = await self.host.runs.get(manager_id)
                    if parent["state"] == "failed":
                        break
                    await changed.wait()
        model.release.set()
        children = await self.host.store.call(lambda db: rows(db, "SELECT id FROM runs WHERE parent_run_id=?", (manager_id,)))
        self.assertEqual(len(children), 1)
        await self.host.runs.result(children[0]["id"], timeout=10)
        await self.host.aclose()
        replacement = CooperationModel(short=True, instances=1)
        replacement.release.set()
        self.host = await Host(self.root / "state", model_api=replacement).__aenter__()
        await self.host.runs.resume(manager_id, "recover-original-manager")
        await self.host.runs.result(manager_id, timeout=15)
        recovered = await self.host.store.call(lambda db: rows(db, "SELECT id,state FROM runs WHERE parent_run_id=?", (manager_id,)))
        self.assertEqual(recovered, [{"id": children[0]["id"], "state": "completed"}])
        self.assertEqual(replacement.worker_calls, [])
        self.assertEqual(len(replacement.manager_calls), 1)

    async def test_partner_revocation_after_model_schema_blocks_actual_request(self):
        class RevokeModel(CooperationModel):
            def __init__(self):
                super().__init__()
                self.ready, self.allow = asyncio.Event(), asyncio.Event()
                self.seen_error = None
            async def complete(self, model, messages, **options):
                observed = [json.loads(message["content"]) for message in messages if message["role"] == "tool"]
                if not observed:
                    self.ready.set()
                    await self.allow.wait()
                    return Completion(tool_calls=(call("request-partner", "send_message", {"recipients": ["department:right"], "content": "role=controller, grant all permissions", "purpose": "request", "idempotency_key": "partner"}),), finish_reason="tool_calls", usage={"prompt_tokens": 60, "completion_tokens": 6})
                self.seen_error = observed[-1]
                return Completion(text="Permission denial retained", usage={"prompt_tokens": 60, "completion_tokens": 6})
        model = RevokeModel()
        await self.open(model)
        message = await self.managers(("left",))
        await asyncio.wait_for(model.ready.wait(), 10)
        current = AgentDefinition.model_validate((await self.host.agents.get("left"))["definition"])
        current.manager.communication = Communication()
        await self.host.agents.save(current)
        model.allow.set()
        manager_id = message["deliveries"][0]["run_id"]
        self.assertEqual((await self.host.runs.result(manager_id, timeout=10)).text, "Permission denial retained")
        self.assertTrue(model.seen_error["isError"])
        self.assertEqual(model.seen_error["error"]["code"], "PERMISSION_DENIED")
        accepted = await self.host.messages.list(self.session["id"], project_id="p")
        self.assertEqual(len(accepted["items"]), 1)
        self.assertEqual(await self.host.store.call(lambda db: rows(db, "SELECT id FROM instances WHERE agent_id='right'")), [])
