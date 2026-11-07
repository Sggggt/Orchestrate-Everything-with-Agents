"""Registered Skill versions, source management, and current execution grants."""

import asyncio
import json
from pathlib import Path
import tempfile
import unittest

from oea import AgentDefinition, Completion, Model, RunSpec, SkillBinding, SkillSource
from oea.contracts.errors import RuntimeFault
from oea.runtime.host import Host
from oea.storage.persistence import one


class SkillModel:
    def __init__(self):
        self.inputs = []

    async def complete(self, model, messages, **options):
        self.inputs.append(messages[0]["content"])
        return Completion(text="Complete", usage={"prompt_tokens": 30, "completion_tokens": 3})


class SkillTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="oea-skills-")
        self.root = Path(self.temporary.name)
        self.source = self.root / "skill-source"
        self.source.mkdir()
        (self.source / "SKILL.md").write_text("Instructions version one.", encoding="utf-8")
        (self.source / "reference.md").write_text("Original reference.", encoding="utf-8")
        self.model = SkillModel()
        self.host = await Host(self.root / "state", model_api=self.model).__aenter__()
        await self.host.projects.create("Project", project_id="p")
        await self.host.projects.create("Other", project_id="q")
        self.session = await self.host.sessions.create("p")
        await self.host.skills.register(SkillSource(id="review", path=self.source, project_id="p"), expected_revision=0)

    async def asyncTearDown(self):
        await self.host.aclose()
        self.temporary.cleanup()

    async def configure(self, *, required=True, project_id="p"):
        await self.host.agents.save(AgentDefinition(id="worker", name="Worker", project_id=project_id,
            model=Model(base_url="https://model.invalid/v1", model="protocol-fixture"), skills=(SkillBinding(id="review", required=required),)))

    async def test_directory_registration_reads_metadata_once_and_preserves_references(self):
        directory = self.root / "evidence-guide"
        directory.mkdir()
        body = "---\nname: evidence-guide\ndescription: Check original receipts.\nlicense: MIT\ncompatibility: Local files\n---\n\n# Guide\n\nRead original evidence.\n"
        (directory / "SKILL.md").write_text(body, encoding="utf-8")
        (directory / "reference.md").write_text("Reference kept in place.", encoding="utf-8")
        registered = await self.host.skills.register_directory(directory, "p")
        self.assertEqual(registered["definition"]["id"], "evidence-guide")
        self.assertEqual(registered["definition"]["description"], "Check original receipts.")
        await self.host.agents.save(AgentDefinition(id="reader", name="Reader", project_id="p", skills=(SkillBinding(id="evidence-guide"),)))
        (directory / "SKILL.md").unlink()
        detail = await self.host.skills.describe("evidence-guide", "p")
        self.assertEqual(detail["metadata"]["license"], "MIT")
        self.assertEqual(detail["bound_agents"], ["reader"])
        self.assertEqual((await self.host.skills.list("p"))["items"][-1]["bound_agents"], ["reader"])
        self.assertEqual((await self.host.skills.read("evidence-guide", "p"))["text"], body)
        self.assertEqual((await self.host.skills.read("evidence-guide", "p", path="reference.md"))["text"], "Reference kept in place.")
        with self.assertRaises(RuntimeFault) as foreign:
            await self.host.skills.describe("evidence-guide", "q")
        self.assertEqual(foreign.exception.code, "PERMISSION_DENIED")

    async def test_invalid_directory_metadata_does_not_create_registration(self):
        with self.assertRaises(RuntimeFault) as missing:
            await self.host.skills.register_directory(self.source, "p")
        self.assertEqual(missing.exception.code, "INVALID_SKILL")
        (self.source / "SKILL.md").write_text("---\nname: invalid\ndescription: [wrong, type]\n---\n", encoding="utf-8")
        with self.assertRaises(RuntimeFault) as invalid:
            await self.host.skills.register_directory(self.source, "p")
        self.assertEqual(invalid.exception.code, "INVALID_SKILL")
        self.assertEqual(await self.host.store.call(lambda db: one(db, "SELECT COUNT(*) AS count FROM skills")["count"]), 1)

    async def test_registered_body_is_versioned_and_does_not_read_the_source_on_each_run(self):
        await self.configure()
        instance = await self.host.instances.create(self.session["id"], "worker")
        (self.source / "SKILL.md").unlink()
        run = await self.host.runs.submit(RunSpec(instance_id=instance["id"], input="Use saved instructions", idempotency_key="first"))
        await self.host.runs.result(run.id, timeout=5)
        self.assertIn("Instructions version one.", self.model.inputs[0])
        self.assertEqual(self.model.inputs[0].count("Instructions version one."), 1)
        (self.source / "SKILL.md").write_text("Instructions version two.", encoding="utf-8")
        refreshed = await self.host.skills.refresh("review", "p", 1)
        self.assertEqual(refreshed["revision"], 2)
        self.assertEqual((await self.host.skills.read("review", "p", revision=1))["text"], "Instructions version one.")
        await self.host.aclose()
        self.host = await Host(self.root / "state", model_api=self.model).__aenter__()
        second = await self.host.runs.submit(RunSpec(instance_id=instance["id"], input="Use new instructions", idempotency_key="second"))
        await self.host.runs.result(second.id, timeout=5)
        self.assertIn("Instructions version two.", self.model.inputs[1])
        self.assertNotIn("Instructions version one.", self.model.inputs[1])
        self.assertEqual(await self.host.store.call(lambda db: one(db, "SELECT COUNT(*) AS count FROM skill_bodies")["count"]), 2)

    async def test_discovery_pins_metadata_with_body_and_loads_optional_body_on_demand(self):
        first = "---\nname: Receipt guide\ndescription: Inspect the original receipt.\n---\nFirst version body."
        second = "---\nname: Updated guide\ndescription: Compare the retained evidence.\n---\nSecond version body."
        (self.source / "SKILL.md").write_text(first, encoding="utf-8")
        await self.host.skills.register(SkillSource(id="review", path=self.source, project_id="p"), expected_revision=1)
        (self.source / "SKILL.md").write_text(second, encoding="utf-8")
        refreshed = await self.host.skills.refresh("review", "p", 2)
        self.assertEqual(refreshed["definition"]["id"], "review")
        self.assertEqual(refreshed["definition"]["name"], "Updated guide")
        (self.source / "SKILL.md").unlink()
        await self.host.aclose()
        self.host = await Host(self.root / "state", model_api=self.model).__aenter__()
        inputs, bodies = [], []
        class ChoosingModel:
            async def complete(self, model, messages, **options):
                inputs.append(messages[0]["content"])
                loaded = [message["content"] for message in messages if message["role"] == "tool"]
                if loaded:
                    bodies.extend(loaded)
                    return Completion(text="Evidence inspected", usage={"prompt_tokens": 40, "completion_tokens": 4})
                return Completion(tool_calls=({"id": "read", "type": "function", "function": {
                    "name": "read_skill", "arguments": '{"skill_id":"review"}'}},),
                    finish_reason="tool_calls", usage={"prompt_tokens": 40, "completion_tokens": 4})
        self.host.model_api = ChoosingModel()
        for index, revision in enumerate((2, None)):
            await self.host.agents.save(AgentDefinition(id="worker", name="Worker", project_id="p",
                model=Model(base_url="https://model.invalid/v1", model="protocol-fixture"),
                skills=(SkillBinding(id="review", revision=revision, name="Untrusted stale label", description="Wrong description"),)))
            instance = await self.host.instances.create(self.session["id"], "worker", name="Reader " + str(index))
            run = await self.host.runs.submit(RunSpec(instance_id=instance["id"], input="Inspect evidence", idempotency_key="metadata-" + str(index)))
            self.assertEqual((await self.host.runs.result(run.id, timeout=5)).text, "Evidence inspected")
        self.assertIn("Receipt guide", inputs[0])
        self.assertIn("Inspect the original receipt.", inputs[0])
        self.assertNotIn("Updated guide", inputs[0])
        self.assertIn("Updated guide", inputs[2])
        self.assertIn("Compare the retained evidence.", inputs[2])
        for value in inputs:
            self.assertNotIn("First version body.", value)
            self.assertNotIn("Second version body.", value)
            self.assertNotIn("Wrong description", value)
        self.assertIn("First version body.", bodies[0])
        self.assertIn("Second version body.", bodies[1])
        self.assertEqual(await self.host.store.call(lambda db: one(db, "SELECT COUNT(*) AS count FROM skill_loads")["count"]), 2)

    async def test_direct_binding_metadata_discovery_does_not_open_the_source(self):
        absent = self.root / "unopened-source"
        self.assertFalse(absent.exists())
        await self.host.agents.save(AgentDefinition(id="worker", name="Worker", project_id="p",
            model=Model(base_url="https://model.invalid/v1", model="protocol-fixture"),
            skills=(SkillBinding(id="direct", path=absent, name="Direct guide", description="Read it only for a relevant task."),)))
        instance = await self.host.instances.create(self.session["id"], "worker")
        run = await self.host.runs.submit(RunSpec(instance_id=instance["id"], input="No guide needed", idempotency_key="direct-metadata"))
        self.assertEqual((await self.host.runs.result(run.id, timeout=5)).text, "Complete")
        self.assertIn("Direct guide", self.model.inputs[0])
        self.assertIn("Read it only for a relevant task.", self.model.inputs[0])
        self.assertFalse(absent.exists())
        self.assertEqual(await self.host.store.call(lambda db: one(db, "SELECT COUNT(*) AS count FROM skill_loads")["count"]), 0)

    async def test_project_scope_and_source_paths_are_checked_before_accepting_work(self):
        with self.assertRaises(RuntimeFault):
            await self.host.skills.read("review", "q")
        await self.configure(project_id="q")
        session = await self.host.sessions.create("q")
        instance = await self.host.instances.create(session["id"], "worker")
        with self.assertRaises(RuntimeFault) as denied:
            await self.host.runs.submit(RunSpec(instance_id=instance["id"], input="Not granted", idempotency_key="denied"))
        self.assertEqual(denied.exception.code, "PERMISSION_DENIED")
        self.assertEqual(await self.host.store.call(lambda db: one(db, "SELECT COUNT(*) AS count FROM budgets")["count"]), 0)
        with self.assertRaises(RuntimeFault):
            await self.host.skills.read("review", "p", path="../other.md")
        with self.assertRaises(RuntimeFault):
            await self.host.skills.register(SkillSource(id="unsafe", path=self.host.root, project_id="p"))

    async def test_mid_run_disable_revokes_the_loaded_tool_schema_and_remove_keeps_files(self):
        await self.configure(required=False)
        host, observations = self.host, []
        class RevokeModel:
            async def complete(self, model, messages, **options):
                observed = [message for message in messages if message["role"] == "tool"]
                if observed:
                    observations.append(observed[0]["content"])
                    self_tools = {value["function"]["name"] for value in options["tools"]}
                    observations.append("read_skill" in self_tools)
                    return Completion(text="Permission explained", usage={"prompt_tokens": 30, "completion_tokens": 3})
                await host.skills.set_enabled("review", "p", False, 1)
                return Completion(tool_calls=({"id": "read", "type": "function", "function": {"name": "read_skill", "arguments": '{"skill_id":"review"}'}},),
                    finish_reason="tool_calls", usage={"prompt_tokens": 30, "completion_tokens": 3})
        self.host.model_api = RevokeModel()
        instance = await self.host.instances.create(self.session["id"], "worker")
        run = await self.host.runs.submit(RunSpec(instance_id=instance["id"], input="Read optional Skill", idempotency_key="revoke"))
        await self.host.runs.result(run.id, timeout=5)
        self.assertIn("PERMISSION_DENIED", observations[0])
        self.assertFalse(observations[1])
        self.assertEqual(await self.host.store.call(lambda db: one(db, "SELECT COUNT(*) AS count FROM skill_loads")["count"]), 0)
        removed = await self.host.skills.remove("review", "p", 2)
        self.assertTrue(removed["removed"])
        self.assertTrue((self.source / "SKILL.md").is_file())
        self.assertEqual((await self.host.skills.list("p"))["items"], [])
        self.assertEqual(await self.host.store.call(lambda db: one(db, "SELECT COUNT(*) AS count FROM skill_bodies")["count"]), 1)
        self.assertEqual(await self.host.store.call(lambda db: one(db, "SELECT COUNT(*) AS count FROM skill_versions")["count"]), 3)

    async def test_explicit_source_change_loads_new_body_and_denies_old_source_reference(self):
        await self.configure()
        calls, inputs, original_reference, observations = 0, [], None, []
        class ChangingModel:
            async def complete(self, model, messages, **options):
                nonlocal calls
                calls += 1
                inputs.append(messages[0]["content"])
                if calls == 1:
                    name, arguments = "wait_for_input", {"prompt": "Confirm the next source"}
                elif calls == 2:
                    name, arguments = "read_context", {"reference": original_reference}
                else:
                    observations.extend(message["content"] for message in messages if message["role"] == "tool")
                    return Completion(text="Complete", usage={"prompt_tokens": 30, "completion_tokens": 3})
                return Completion(tool_calls=({"id": "call-" + str(calls), "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}},),
                    finish_reason="tool_calls", usage={"prompt_tokens": 30, "completion_tokens": 3})
        self.host.model_api = ChangingModel()
        instance = await self.host.instances.create(self.session["id"], "worker")
        run = await self.host.runs.submit(RunSpec(instance_id=instance["id"], input="Use the permitted source", idempotency_key="source-change"))
        async with asyncio.timeout(5):
            while True:
                changed = self.host.run_signal(run.id)
                if (await self.host.runs.get(run.id))["state"] == "waiting":
                    break
                await changed.wait()
        original_reference = await self.host.store.call(lambda db: one(db, "SELECT source_id FROM skill_loads WHERE run_id=?", (run.id,))["source_id"])
        new_source = self.root / "new-source"
        new_source.mkdir()
        (new_source / "SKILL.md").write_text("Newly permitted instructions.", encoding="utf-8")
        saved = await self.host.agents.get("worker")
        changed = saved["definition"]
        changed["skills"] = [{"id": "review", "path": str(new_source), "required": True}]
        await self.host.agents.save(changed, expected_revision=saved["revision"])
        await self.host.runs.resume(run.id, "new-source", input="Continue with the new source")
        await self.host.runs.result(run.id, timeout=5)
        self.assertIn("Newly permitted instructions.", inputs[1])
        self.assertNotIn("Instructions version one.", inputs[1])
        self.assertTrue(any("PERMISSION_DENIED" in value for value in observations))
        self.assertFalse(any("Instructions version one." in value for value in observations))
        self.assertEqual(await self.host.store.call(lambda db: one(db, "SELECT COUNT(*) AS count FROM skill_loads WHERE run_id=?", (run.id,))["count"]), 2)
