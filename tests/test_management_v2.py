"""Management boundaries use temporary local configuration and retained receipts."""

import asyncio
import json
from pathlib import Path
import tempfile
import unittest
import uuid

from oea import (AgentDefinition, Budget, Communication, Completion, ContextPolicy, Delivery,
                 Execution, Instructions, Manager, MCPBinding, MCPServer, Model, RunSpec,
                 SkillBinding, Tools, Workspace)
from oea.contracts.errors import RuntimeFault
from oea.runtime.host import Host
from oea.storage.persistence import one


class ManagementModel:
    """Protocol fixture; management checks must never use a remote model."""
    async def complete(self, *args, **options):
        return Completion(text="Completed", usage={"prompt_tokens": 1, "completion_tokens": 1})


class ManagementTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="oea-management-")
        self.root = Path(self.temporary.name)
        self.host = await Host(self.root / "state", model_api=ManagementModel()).__aenter__()
        await self.host.projects.create("Project", project_id="p")
        await self.host.projects.create("Other", project_id="q")

    async def asyncTearDown(self):
        await self.host.aclose()
        self.temporary.cleanup()

    async def test_selected_codex_import_keeps_credentials_private_and_does_not_connect(self):
        secret = uuid.uuid4().hex
        source = self.root / "config.toml"
        source.write_text('[mcp_servers.business]\ncommand = "python"\nargs = ["-m", "business"]\n[mcp_servers.business.env]\nAPI_KEY = ' + json.dumps(secret) + '\nEMPTY = ""\n[mcp_servers.other]\nurl = "https://business.invalid/mcp"\n[mcp_servers.control]\ncommand = "oea"\nargs = ["mcp"]\n', encoding="utf-8")
        page = await self.host.mcp.codex_candidates("p", source, limit=2)
        self.assertNotIn(secret, json.dumps(page))
        self.assertEqual(len(page["items"]), 2)
        self.assertEqual(page["cursor"], 2)
        last = await self.host.mcp.codex_candidates("p", source, cursor=page["cursor"], limit=2)
        self.assertFalse(last["items"][0]["importable"])
        result = await self.host.mcp.import_codex("p", source, ("business", "control"))
        self.assertEqual(len(result["imported"]), 1)
        self.assertEqual(result["errors"][0]["error"]["code"], "CONTROL_BRIDGE_FORBIDDEN")
        self.assertNotIn(secret, json.dumps(result))
        definition = result["imported"][0]["definition"]
        self.assertEqual(self.host.secret(definition["environment_refs"]["API_KEY"]), secret)
        self.assertEqual(definition["environment"], {"EMPTY": ""})
        self.assertEqual(self.host.mcp.connections, {})
        self.assertEqual(len((await self.host.mcp.list("p"))["items"]), 1)
        again = await self.host.mcp.import_codex("p", source, ("business",))
        self.assertEqual(again["skipped"], [{"name": "business", "id": "business"}])

    async def test_soft_removal_preserves_source_catalog_and_credentials(self):
        directory = self.root / "source"
        directory.mkdir()
        marker = directory / "source.txt"
        marker.write_text("retained", encoding="utf-8")
        await self.host.mcp.save(MCPServer(id="business", project_id="p", command=("python",), source_directory=directory))
        await self.host.store.call(lambda db: db.execute("INSERT INTO mcp_catalogs VALUES(?,?,?,?)", ("business", 1, "[]", 1)), transaction=True)
        credential = await self.host.mcp.set_credential("business", "p", "environment", "API_KEY", uuid.uuid4().hex, 1)
        secret = self.host.secret(credential["credential_ref"])
        removed = await self.host.mcp.remove("business", "p", credential["revision"])
        self.assertTrue(removed["definition"]["removed"])
        self.assertFalse(removed["definition"]["enabled"])
        self.assertEqual((await self.host.mcp.list("p"))["items"], [])
        self.assertEqual(len((await self.host.mcp.list("p", include_removed=True))["items"]), 1)
        self.assertEqual((await self.host.mcp.tools("business", "p", revision=1))["tools"], [])
        self.assertEqual(self.host.secret(credential["credential_ref"]), secret)
        self.assertEqual(marker.read_text(encoding="utf-8"), "retained")
        with self.assertRaises(RuntimeFault) as denied:
            await self.host.mcp.remove("business", "q", removed["revision"])
        self.assertEqual(denied.exception.code, "PERMISSION_DENIED")

    async def test_session_archive_rejects_new_instances_and_preserves_existing_identity(self):
        session = await self.host.sessions.create("p", "Original")
        await self.host.agents.save(AgentDefinition(id="worker", name="Worker", project_id="p"))
        instance = await self.host.instances.create(session["id"], "worker")
        self.assertEqual((await self.host.projects.update("p", "Renamed"))["name"], "Renamed")
        self.assertEqual((await self.host.sessions.update(session["id"], "p", "Renamed session"))["name"], "Renamed session")
        archived = await self.host.sessions.set_archived(session["id"], "p", True)
        self.assertTrue(archived["archived"])
        with self.assertRaises(RuntimeFault) as denied:
            await self.host.instances.create(session["id"], "worker")
        self.assertEqual(denied.exception.code, "SESSION_ARCHIVED")
        self.assertEqual((await self.host.instances.get(instance["id"]))["id"], instance["id"])
        with self.assertRaises(RuntimeFault) as denied:
            await self.host.sessions.set_archived(session["id"], "q", False)
        self.assertEqual(denied.exception.code, "PERMISSION_DENIED")
        restored = await self.host.sessions.set_archived(session["id"], "p", False)
        self.assertFalse(restored["archived"])
        await self.host.instances.create(session["id"], "worker")

    async def test_codex_rejects_public_credential_reflection_and_denylist_only_configuration(self):
        secret = uuid.uuid4().hex
        source = self.root / "config.toml"
        source.write_text('[mcp_servers.reflection]\ncommand = "python"\nargs = [' + json.dumps(secret) + ']\n[mcp_servers.reflection.env]\nAPI_KEY = ' + json.dumps(secret) + '\n[mcp_servers.denied]\ncommand = "python"\ndisabled_tools = ["write"]\n', encoding="utf-8")
        result = await self.host.mcp.import_codex("p", source, ("reflection", "denied"))
        self.assertEqual(result["imported"], [])
        self.assertEqual(len(result["errors"]), 2)
        self.assertNotIn(secret, json.dumps(result))
        self.assertEqual(self.host.mcp.connections, {})

    async def test_six_elements_round_trip_with_revision_conflicts_and_project_ownership(self):
        project = self.root / "project"
        project.mkdir()
        skill = self.root / "skill"
        skill.mkdir()
        (skill / "SKILL.md").write_text("A public guide.", encoding="utf-8")
        definition = AgentDefinition(id="worker", name="Worker", project_id="p",
            model=Model(base_url="https://model.invalid/v1", model="main", temperature=0.2),
            instructions=Instructions(system="Review assigned inputs.", agent_md="Retain evidence."),
            tools=Tools(local=("read_file", "write_file"), functions=("review",), mcp=(MCPBinding(server_id="business", tools=("inspect",)),)),
            skills=(SkillBinding(id="guide", path=skill, name="Guide", description="Review guidance", required=True),),
            context=ContextPolicy(input_tokens=8192, recent_turns=3),
            workspace=Workspace(project=project, work=self.root / "work", references=(skill,), writable_project_paths=("output",), environment={"REVIEW_MODE": "local"}),
            execution=Execution(budget=Budget(max_model_calls=12), delivery=Delivery(min_artifacts=1)),
            communication=Communication(departments=("review",), can_request=True),
            manager=Manager(model=Model(base_url="https://manager.invalid/v1", model="manager"), instructions=Instructions(system="Coordinate reviewers.")))
        saved = await self.host.registry.call("agents.save", {"definition": definition.model_dump(mode="json"), "expected_revision": 0})
        self.assertEqual(saved["definition"], definition.model_dump(mode="json"))
        renamed = definition.model_copy(update={"name": "Renamed worker"})
        updated = await self.host.registry.call("agents.save", {"definition": renamed.model_dump(mode="json"), "expected_revision": 1})
        self.assertEqual(updated["revision"], 2)
        self.assertEqual((await self.host.agents.get("worker"))["definition"], renamed.model_dump(mode="json"))
        with self.assertRaises(RuntimeFault) as conflict:
            await self.host.agents.save(definition, expected_revision=1)
        self.assertEqual(conflict.exception.code, "REVISION_CONFLICT")
        with self.assertRaises(RuntimeFault) as foreign:
            await self.host.agents.save(renamed.model_copy(update={"project_id": "q"}), expected_revision=2)
        self.assertEqual(foreign.exception.code, "PROJECT_MISMATCH")
        self.assertEqual((await self.host.agents.get("worker"))["revision"], 2)
        self.assertFalse((self.root / "work").exists())

    async def test_catalog_pages_keep_project_scope_and_stable_instance_identity(self):
        first = await self.host.sessions.create("p", "First")
        second = await self.host.sessions.create("p", "Second")
        await self.host.sessions.create("q", "Foreign")
        for identity, project in (("worker", "p"), ("reviewer", "p"), ("foreign", "q"), ("global", None)):
            await self.host.agents.save(AgentDefinition(id=identity, name=identity, project_id=project))
        same = await asyncio.gather(*(self.host.instances.create(first["id"], "worker", name="stable") for _ in range(12)))
        self.assertEqual(len({item["id"] for item in same}), 1)
        other = await self.host.instances.create(first["id"], "worker", name="other")
        await self.host.instances.create(second["id"], "worker", name="stable")
        await self.host.agents.save(AgentDefinition(id="worker", name="Renamed", project_id="p"), expected_revision=1)
        self.assertEqual((await self.host.instances.create(first["id"], "worker", name="stable"))["id"], same[0]["id"])
        async def page_ids(call, key):
            cursor, found = 0, []
            while cursor is not None:
                page = await call(cursor=cursor, limit=1)
                found.extend(key(item) for item in page["items"])
                cursor = page["cursor"]
            self.assertEqual(len(found), len(set(found)))
            return set(found)
        self.assertEqual(await page_ids(self.host.projects.list, lambda item: item["id"]), {"p", "q"})
        self.assertEqual(await page_ids(lambda **page: self.host.sessions.list("p", **page), lambda item: item["id"]), {first["id"], second["id"]})
        self.assertEqual(await page_ids(lambda **page: self.host.agents.list("p", **page), lambda item: item["definition"]["id"]), {"worker", "reviewer", "global"})
        self.assertEqual(await page_ids(lambda **page: self.host.instances.list(first["id"], agent_id="worker", **page), lambda item: item["id"]), {same[0]["id"], other["id"]})
        with self.assertRaises(RuntimeFault) as foreign:
            await self.host.instances.create(first["id"], "foreign", name="forbidden")
        self.assertEqual(foreign.exception.code, "PROJECT_MISMATCH")

    async def test_catalog_pagination_rejects_non_integer_and_overflow_boundaries(self):
        session = await self.host.sessions.create("p")
        await self.host.agents.save(AgentDefinition(id="worker", name="Worker", project_id="p"))
        instance = await self.host.instances.create(session["id"], "worker", name="stable")
        calls = (self.host.projects.list,
                 lambda **page: self.host.agents.list("p", **page),
                 lambda **page: self.host.sessions.list("p", **page),
                 lambda **page: self.host.instances.list(session["id"], **page))
        for call in calls:
            for params in ({"cursor": -1}, {"cursor": 2**63}, {"cursor": True}, {"cursor": 0.5}, {"limit": 0}, {"limit": 101}, {"limit": True}, {"limit": 1.5}):
                with self.subTest(call=call, params=params), self.assertRaises(RuntimeFault) as invalid:
                    await call(**params)
                self.assertEqual(invalid.exception.code, "INVALID_REQUEST")
            self.assertEqual((await call(cursor=2**63 - 1))["items"], [])
        for params in ({"before": 0}, {"before": 2**63}, {"before": True}, {"before": 1.5}, {"limit": False}):
            with self.subTest(params=params), self.assertRaises(RuntimeFault) as invalid:
                await self.host.instances.history(instance["id"], "p", **params)
            self.assertEqual(invalid.exception.code, "INVALID_REQUEST")

    async def test_session_archive_preserves_completed_work_until_restored(self):
        session = await self.host.sessions.create("p", "Work")
        await self.host.agents.save(AgentDefinition(id="worker", name="Worker", project_id="p", model=Model(base_url="https://fixture.invalid/v1", model="fixture")))
        instance = await self.host.instances.create(session["id"], "worker", name="stable")
        original = RunSpec(instance_id=instance["id"], input="Retain this result.", idempotency_key="original")
        accepted = await self.host.runs.submit(original)
        result = await self.host.runs.result(accepted.id, timeout=10)
        self.assertEqual((await self.host.runs.get(accepted.id))["state"], "completed")
        workspace = (await self.host.instances.get(instance["id"]))["workspace"]
        await self.host.registry.call("sessions.update", {"session_id": session["id"], "project_id": "p", "name": "Retained work"})
        await self.host.registry.call("sessions.set_archived", {"session_id": session["id"], "project_id": "p", "archived": True})
        with self.assertRaises(RuntimeFault) as archived:
            await self.host.runs.submit(RunSpec(instance_id=instance["id"], input="New work", idempotency_key="new"))
        self.assertEqual(archived.exception.code, "SESSION_ARCHIVED")
        self.assertTrue((await self.host.runs.submit(original)).reused)
        self.assertEqual((await self.host.runs.result(accepted.id)).text, result.text)
        self.assertEqual(len((await self.host.instances.history(instance["id"], "p"))["items"]), 2)
        self.assertEqual((await self.host.sessions.summary(session["id"], "p"))["runs"], {"completed": 1})
        await self.host.sessions.set_archived(session["id"], "p", False)
        await self.host.registry.call("projects.update", {"project_id": "p", "name": "Renamed project"})
        self.assertNotIn("archived", (await self.host.projects.list())["items"][0])
        continued = await self.host.runs.submit(RunSpec(instance_id=instance["id"], input="New work", idempotency_key="new"))
        self.assertEqual(continued.instance_id, instance["id"])
        self.assertEqual((await self.host.runs.result(continued.id, timeout=10)).text, "Completed")
        continued_workspace = (await self.host.instances.get(instance["id"]))["workspace"]
        self.assertEqual(continued_workspace["work_dir"], workspace["work_dir"])
        self.assertNotEqual(continued_workspace["output_dir"], workspace["output_dir"])

    async def test_empty_names_are_rejected_before_catalog_writes(self):
        session = await self.host.sessions.create("p")
        await self.host.agents.save(AgentDefinition(id="worker", name="Worker", project_id="p"))
        for name in ("", "   "):
            for call in (lambda: self.host.projects.create(name),
                         lambda: self.host.projects.update("p", name),
                         lambda: self.host.sessions.create("p", name),
                         lambda: self.host.sessions.update(session["id"], "p", name),
                         lambda: self.host.instances.create(session["id"], "worker", name=name)):
                with self.subTest(name=name, call=call), self.assertRaises(RuntimeFault) as invalid:
                    await call()
                self.assertEqual(invalid.exception.code, "INVALID_REQUEST")
        self.assertEqual(await self.host.store.call(lambda db: one(db, "SELECT COUNT(*) AS count FROM instances")["count"]), 0)

    async def test_session_archiving_does_not_cancel_existing_in_flight_work(self):
        entered, release = asyncio.Event(), asyncio.Event()
        class InFlightModel:
            async def complete(self, *args, **options):
                entered.set()
                await release.wait()
                return Completion(text="Retained in-flight result", usage={"prompt_tokens": 1, "completion_tokens": 1})
        self.host.model_api = InFlightModel()
        session = await self.host.sessions.create("p")
        await self.host.agents.save(AgentDefinition(id="worker", name="Worker", project_id="p", model=Model(base_url="https://fixture.invalid/v1", model="fixture")))
        instance = await self.host.instances.create(session["id"], "worker", name="stable")
        accepted = await self.host.runs.submit(RunSpec(instance_id=instance["id"], input="Existing work", idempotency_key="in-flight"))
        try:
            await asyncio.wait_for(entered.wait(), timeout=10)
            await self.host.sessions.set_archived(session["id"], "p", True)
            active = await self.host.runs.get(accepted.id)
            self.assertEqual(active["state"], "running")
            self.assertIsNone(active["cancel_requested_at"])
        finally:
            release.set()
        self.assertEqual((await self.host.runs.result(accepted.id, timeout=10)).text, "Retained in-flight result")
