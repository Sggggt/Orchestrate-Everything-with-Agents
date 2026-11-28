"""API Box and plugin contracts with runtime-generated isolated inputs."""

import asyncio
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from oea import AgentDefinition, Completion, Manager, Model, RunSpec, SkillBinding
from oea.skills.service import SkillSource
from oea.contracts.configuration import AgentCreation, RunConfiguration
from oea.contracts.errors import RuntimeFault
from oea.mcp.upstream import MCPServer
from oea.plugins.service import PLUGIN_SCHEMA, MCP_SCHEMA, NAMESPACE
from oea.runtime.host import Host
from oea.storage.persistence import one


class GateModel:
    def __init__(self):
        self.started, self.release = asyncio.Event(), asyncio.Event()
        self.requests = []

    async def complete(self, model, messages, **kwargs):
        self.requests.append(model.model)
        self.started.set()
        await self.release.wait()
        return Completion(text="Complete", usage={"prompt_tokens": 30, "completion_tokens": 3})


class APIBoxPluginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="oea-box-")
        self.root = Path(self.temporary.name)
        self.model = GateModel()
        self.host = await Host(self.root / "state", model_api=self.model).__aenter__()
        await self.host.projects.create("Example", project_id="p")
        self.first = await self.host.model_profiles.save("First", Model(base_url="https://example.invalid/v1", model="first"))
        self.second = await self.host.model_profiles.save("Second", Model(base_url="https://example.invalid/v1", model="second"))
        self.definition = AgentDefinition(id="worker", name="Worker", project_id="p", model_ref=self.first["id"])
        self.saved = await self.host.agents.save(self.definition)
        self.session = await self.host.sessions.create("p")
        self.instance = await self.host.instances.create(self.session["id"], "worker")

    async def asyncTearDown(self):
        self.model.release.set()
        await self.host.aclose()
        self.temporary.cleanup()

    async def active(self):
        run = await self.host.runs.submit(RunSpec(instance_id=self.instance["id"], input="Work", idempotency_key="work"))
        await asyncio.wait_for(self.model.started.wait(), 10)
        return run

    async def test_declarations_have_references_and_no_inline_credentials(self):
        schema = await self.host.agents.schema()
        self.assertNotIn("model", schema["properties"])
        self.assertNotIn("summary_model", schema["properties"])
        with self.assertRaises(ValidationError):
            AgentDefinition(id="old", name="Old", model=Model())
        self.assertNotIn("agents.set_credential", self.host.registry.handlers)
        self.assertEqual(self.saved["definition"]["model_ref"], self.first["id"])

    async def test_unique_name_and_rename_keep_existing_references(self):
        with self.assertRaises(RuntimeFault):
            await self.host.model_profiles.save("First", Model(model="other"))
        changed = await self.host.model_profiles.save("Renamed", Model(base_url="https://example.invalid/v1", model="first"), id=self.first["id"], expected_revision=1)
        self.assertEqual(changed["id"], self.first["id"])
        self.assertEqual((await self.host.agents.get("worker"))["model_summary"]["main"]["name"], "Renamed")

    async def test_credentials_are_write_only_and_versions_are_immutable(self):
        result = await self.host.model_profiles.set_credential(self.first["id"], "test-credential", 1)
        self.assertTrue(result["has_credential"])
        self.assertNotIn("credential_ref", result["model"])
        self.assertNotIn("test-credential", json.dumps(result))
        self.assertEqual(await self.host.store.call(lambda db: one(db, "SELECT COUNT(*) AS n FROM model_profile_versions WHERE profile_id=?", (self.first["id"],))["n"]), 2)

    async def test_instance_selection_does_not_replace_accepted_run_or_sibling(self):
        sibling = await self.host.instances.create(self.session["id"], "worker", name="sibling")
        run = await self.active()
        await self.host.instances.set_model(self.instance["id"], "p", self.second["id"], 0)
        self.assertEqual((await self.host.runs.get(run.id))["model_summary"]["id"], self.first["id"])
        self.assertEqual((await self.host.instances.get(sibling["id"]))["model_summary"]["id"], self.first["id"])
        self.model.release.set()
        await self.host.runs.result(run.id, timeout=10)
        following = await self.host.runs.submit(RunSpec(instance_id=self.instance["id"], input="Next", idempotency_key="next"))
        await self.host.runs.result(following.id, timeout=10)
        self.assertEqual(self.model.requests, ["first", "second"])

    async def test_manager_rejects_instance_override(self):
        await self.host.agents.save(self.definition.model_copy(update={"manager": Manager(model_ref=self.first["id"])}), expected_revision=1)
        await self.host.sessions.set_mode(self.session["id"], "collaborative", 0)
        manager = await self.host.instances.create(self.session["id"], "worker", role="manager")
        with self.assertRaises(RuntimeFault):
            await self.host.instances.set_model(manager["id"], "p", self.second["id"], 0)
        with self.assertRaises(RuntimeFault):
            await self.host.instances.create(self.session["id"], "worker", role="manager", name="other", model_ref=self.second["id"])

    async def test_global_events_contain_only_public_configuration_facts(self):
        page = await self.host.events_service.list()
        self.assertTrue(page["items"])
        self.assertTrue(all(item["project_id"] is None and set(item["data"]) <= {"id", "revision"} for item in page["items"]))

    async def test_plugin_connection_edit_copies_shared_registration(self):
        original = MCPServer(id="example", project_id="p", command=(sys.executable, "-c", "pass"))
        await self.host.mcp.save(original)
        package = self.root / "package"
        package.mkdir()
        text = json.dumps({"$schema": PLUGIN_SCHEMA, "name": "example-plugin", "version": "1.0.0", "extensions": {NAMESPACE: {"mcp_refs": ["example"]}}})
        (package / "plugin.json").write_text(text, encoding="utf-8")
        plugin = await self.host.plugins.install(str(package), "p", "install")
        repeated = await self.host.plugins.install(str(package), "p", "install")
        self.assertEqual(repeated, plugin)
        updated = await self.host.plugins.configure_component(plugin["id"], "mcp:example", original.model_copy(update={"name": "Private copy"}), plugin["revision"])
        self.assertNotEqual(updated["components"][0]["server_id"], "example")
        self.assertEqual(updated["components"][0]["ownership"], "owned")
        self.assertEqual((await self.host.mcp.get("example"))["revision"], 1)
        self.assertEqual((package / "plugin.json").read_text(encoding="utf-8"), text)
        with self.assertRaises(RuntimeFault):
            await self.host.plugins.configure_component(plugin["id"], "mcp:example", original, plugin["revision"])

    async def test_agent_factory_creates_definition_once_without_new_budget(self):
        parent = self.definition.model_copy(update={"plugins": ("agent-factory",), "creation": AgentCreation(enabled=True, model_refs=(self.first["id"],))})
        await self.host.agents.save(parent, expected_revision=1)
        run = await self.active()
        options = await self.host.tools.invoke(run.id, "plugin_options", {}, "options")
        self.assertEqual(options["models"][0]["id"], self.first["id"])
        self.assertNotIn("base_url", json.dumps(options))
        before = await self.host.store.call(lambda db: one(db, "SELECT COUNT(*) AS n FROM budgets")["n"])
        child = AgentDefinition(id="child", name="Child", project_id="p", model_ref=self.first["id"])
        args = {"definition": child.model_dump(mode="json")}
        result = await self.host.tools.invoke(run.id, "create_agent", args, "create")
        self.assertEqual(result.get("agent_id"), "child", result)
        self.assertEqual(await self.host.tools.invoke(run.id, "create_agent", args, "create"), result)
        self.assertEqual(await self.host.store.call(lambda db: one(db, "SELECT COUNT(*) AS n FROM budgets")["n"]), before)
        instance = await self.host.tools.invoke(run.id, "create_instance", {"agent_id": "child", "name": "child-one"}, "instance")
        self.assertEqual(instance.get("agent_id"), "child", instance)

    async def test_factory_rejects_ungranted_model_and_current_revocation(self):
        parent = self.definition.model_copy(update={"plugins": ("agent-factory",), "creation": AgentCreation(enabled=True, model_refs=(self.first["id"],))})
        await self.host.agents.save(parent, expected_revision=1)
        run = await self.active()
        child = AgentDefinition(id="child", name="Child", project_id="p", model_ref=self.second["id"])
        result = await self.host.tools.invoke(run.id, "create_agent", {"definition": child.model_dump(mode="json")}, "denied")
        self.assertTrue(result.get("isError"), result)
        await self.host.agents.save(parent.model_copy(update={"creation": AgentCreation()}), expected_revision=2)
        self.assertNotIn("create_agent", self.host.active[run.id].tool_names())

    async def test_creation_checks_persisted_revocation_before_committing(self):
        parent = self.definition.model_copy(update={"plugins": ("agent-factory",), "creation": AgentCreation(enabled=True, model_refs=(self.first["id"],))})
        await self.host.agents.save(parent, expected_revision=1)
        run = await self.active()
        revoked = parent.model_copy(update={"creation": AgentCreation()})
        # Model a committed configuration update whose cache publication is pending.
        await self.host.store.call(lambda db: db.execute("UPDATE agent_definitions SET config_json=? WHERE id='worker'", (revoked.model_dump_json(),)), transaction=True)
        child = AgentDefinition(id="child", name="Child", project_id="p", model_ref=self.first["id"])
        result = await self.host.tools.invoke(run.id, "create_agent", {"definition": child.model_dump(mode="json")}, "revoked-in-store")
        self.assertTrue(result.get("isError"), result)
        self.assertIsNone(await self.host.store.call(lambda db: one(db, "SELECT id FROM agent_definitions WHERE id='child'")))

    async def test_model_switch_projects_vendor_state_and_preserves_original_history(self):
        from oea import Tools
        from oea.tools.functions import FunctionTool
        def echo(value: str):
            return value
        self.host.register_tool(FunctionTool(echo))
        await self.host.agents.save(self.definition.model_copy(update={"tools": Tools(functions=("echo",))}), expected_revision=1)
        requests = []
        class Fixture:
            async def complete(inner, model, messages, **kwargs):
                requests.append((model.model, messages))
                if len(requests) == 1:
                    return Completion(tool_calls=({"id": "call", "type": "function", "function": {"name": "echo", "arguments": '{"value":"evidence"}'}, "extra_content": {"signature": "opaque"}},),
                                      continuation={"reasoning_content": "opaque", "reasoning_details": [{"signature": "opaque"}]},
                                      usage={"prompt_tokens": 30, "completion_tokens": 3})
                return Completion(text="Done", usage={"prompt_tokens": 30, "completion_tokens": 3})
        self.host.model_api = Fixture()
        first = await self.host.runs.submit(RunSpec(instance_id=self.instance["id"], input="First", idempotency_key="vendor-one"))
        await self.host.runs.result(first.id, timeout=10)
        self.assertTrue(any(message.get("reasoning_content") == "opaque" for message in requests[1][1]))
        await self.host.instances.set_model(self.instance["id"], "p", self.second["id"], 0)
        second = await self.host.runs.submit(RunSpec(instance_id=self.instance["id"], input="Second", idempotency_key="vendor-two"))
        await self.host.runs.result(second.id, timeout=10)
        self.assertNotIn("opaque", json.dumps(requests[-1][1]))
        self.assertTrue(any(message.get("tool_calls") for message in requests[-1][1]))
        history = await self.host.contexts.history(self.instance["id"], limit=20)
        self.assertTrue(any(item["message"].get("reasoning_content") == "opaque" for item in history["items"]))

    async def test_local_wrapping_and_portable_export_never_copy_connection_credentials(self):
        original = MCPServer(id="example", project_id="p", command=(sys.executable, "-c", "pass"))
        await self.host.mcp.save(original)
        wrapped = await self.host.plugins.build("wrapped-example", ["example"], [], "p", "wrap")
        plugin = await self.host.plugins.install(wrapped["path"], "p", "install-wrap")
        self.assertEqual(plugin["components"][0]["server_id"], "example")
        self.assertEqual(plugin["components"][0]["ownership"], "reference")
        exported = await self.host.plugins.export(plugin["id"], "p", "portable")
        package = Path(exported["path"])
        content = "\n".join(path.read_text(encoding="utf-8") for path in package.rglob("*") if path.is_file())
        self.assertNotIn(str(self.root), content)
        self.assertNotIn(str(Path(sys.executable).parent), content)
        self.assertFalse((package / "export-receipt.json").exists())
        self.assertTrue(exported["connection_configuration_required"])

    async def test_official_plugin_can_be_removed_and_reinstalled(self):
        plugin = await self.host.plugins.get("agent-factory", "p")
        await self.host.plugins.remove(plugin["id"], plugin["revision"])
        self.assertFalse(next(item for item in (await self.host.plugins.catalog())["items"] if item["id"] == "agent-factory")["installed"])
        restored = await self.host.plugins.install("official:agent-factory", "p", "restore")
        self.assertTrue(restored["enabled"])
        self.assertFalse(restored["removed"])

    async def test_catalog_refresh_is_explicit_and_removal_preserves_plugins(self):
        package = self.root / "catalog-plugin"
        package.mkdir()
        (package / "plugin.json").write_text(json.dumps({"$schema": PLUGIN_SCHEMA, "name": "catalog-plugin"}), encoding="utf-8")
        catalog = self.root / "catalog.json"
        catalog.write_text(json.dumps({"plugins": [{"name": "catalog-plugin", "source": "./catalog-plugin"}]}), encoding="utf-8")
        source = await self.host.plugins.add_source(str(catalog), "Local catalog")
        self.assertEqual(source["entry_count"], 1)
        item = next(item for item in (await self.host.plugins.catalog())["items"] if item["id"] == "catalog-plugin")
        installed = await self.host.plugins.install(item["source"], "p", "from-catalog")
        catalog.write_text(json.dumps({"plugins": []}), encoding="utf-8")
        self.assertEqual((await self.host.plugins.sources())["items"][0]["entry_count"], 1)
        await self.host.plugins.refresh_source(source["id"])
        self.assertEqual((await self.host.plugins.sources())["items"][0]["entry_count"], 0)
        await self.host.plugins.remove_source(source["id"])
        self.assertFalse((await self.host.plugins.get(installed["id"], "p"))["removed"])

    async def test_component_registration_rolls_back_with_failed_installation(self):
        package = self.root / "atomic-package"
        package.mkdir()
        (package / "plugin.json").write_text(json.dumps({"$schema": PLUGIN_SCHEMA, "name": "atomic-package"}), encoding="utf-8")
        (package / "mcp.json").write_text(json.dumps({"$schema": MCP_SCHEMA, "mcpServers": {
            "one": {"type": "stdio", "command": "python"}, "two": {"type": "stdio", "command": "python"}}}), encoding="utf-8")
        save = self.host.mcp._save
        def failure(db, definition, expected_revision):
            if definition.name == "two":
                raise RuntimeFault("FIXTURE_FAILURE", "Stop after preparing the first component")
            return save(db, definition, expected_revision)
        with patch.object(self.host.mcp, "_save", side_effect=failure), self.assertRaises(RuntimeFault):
            await self.host.plugins.install(str(package), "p", "atomic")
        self.assertNotIn("atomic-package", self.host.plugins.current)
        self.assertEqual(self.host.mcp.configurations, {})
        self.assertEqual(await self.host.store.call(lambda db: one(db, "SELECT COUNT(*) AS n FROM mcp_servers")["n"]), 0)

    async def test_plugin_update_keeps_accepted_components_and_revokes_both_versions(self):
        package = self.root / "versioned-package"
        package.mkdir()
        manifest = {"$schema": PLUGIN_SCHEMA, "name": "versioned-package", "version": "1.0.0"}
        (package / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
        (package / "mcp.json").write_text(json.dumps({"$schema": MCP_SCHEMA, "mcpServers": {"server": {"type": "stdio", "command": "python"}}}), encoding="utf-8")
        plugin = await self.host.plugins.install(str(package), "p", "version-one")
        definition = self.definition.model_copy(update={"plugins": (plugin["id"],)})
        await self.host.agents.save(definition, expected_revision=1)
        def accept(db, key):
            row, _ = self.host.runs._submit(db, RunSpec(instance_id=self.instance["id"], input="Work", idempotency_key=key))
            return RunConfiguration.model_validate_json(row["config_json"])
        old = await self.host.store.call(lambda db: accept(db, "old"), transaction=True)
        original = old.tools.mcp[0].server_id
        configured, component_revision = self.host.mcp.configurations[original]
        await self.host.mcp.save(configured.model_copy(update={"environment": {**configured.environment, "USER_VALUE": "custom"},
                                                             "environment_refs": {"KEY": "vault:connection"}}), expected_revision=component_revision)
        manifest["version"] = "2.0.0"
        (package / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
        (package / "mcp.json").write_text(json.dumps({"$schema": MCP_SCHEMA, "mcpServers": {"server": {"type": "stdio", "command": "node", "args": ["--version"], "env": {"DEFAULT_VALUE": "new"}}}}), encoding="utf-8")
        updated = await self.host.plugins.update(plugin["id"], str(package), plugin["revision"], "version-two")
        self.assertEqual(await self.host.plugins.update(plugin["id"], str(package), plugin["revision"], "version-two"), updated)
        current = self.host.plugins.current_definition(definition, "p", old)
        self.assertEqual(current.tools.mcp[0].server_id, original)
        new = await self.host.store.call(lambda db: accept(db, "new"), transaction=True)
        self.assertNotEqual(new.tools.mcp[0].server_id, original)
        effective = self.host.mcp.configurations[new.tools.mcp[0].server_id][0]
        self.assertEqual(effective.command, ("node", "--version"))
        self.assertEqual(effective.environment["DEFAULT_VALUE"], "new")
        self.assertEqual(effective.environment["USER_VALUE"], "custom")
        self.assertEqual(effective.environment_refs, {"KEY": "vault:connection"})
        await self.host.plugins.set_enabled(plugin["id"], False, updated["revision"])
        self.assertFalse(self.host.plugins.bindings.available("mcp", original))
        self.assertFalse(self.host.plugins.bindings.available("mcp", new.tools.mcp[0].server_id))

    async def test_factory_capabilities_are_not_added_to_a_run_accepted_while_disabled(self):
        for identity in ("agent-factory", "plugin-creator"):
            row = await self.host.plugins.get(identity, "p")
            await self.host.plugins.set_enabled(identity, False, row["revision"])
        parent = self.definition.model_copy(update={"plugins": ("agent-factory", "plugin-creator"), "creation": AgentCreation(enabled=True, model_refs=(self.first["id"],))})
        await self.host.agents.save(parent, expected_revision=1)
        run = await self.active()
        for identity in parent.plugins:
            row = await self.host.plugins.get(identity, "p")
            await self.host.plugins.set_enabled(identity, True, row["revision"])
        self.assertNotIn("create_agent", self.host.active[run.id].tool_names())
        self.assertNotIn("build_plugin", self.host.active[run.id].tool_names())

    async def test_child_skill_sources_and_revisions_are_bounded_for_main_and_manager(self):
        approved = self.root / "approved"
        rejected = self.root / "rejected"
        for directory in (approved, rejected):
            directory.mkdir()
            (directory / "SKILL.md").write_text("Approved instruction source.", encoding="utf-8")
        await self.host.skills.register(SkillSource(id="approved", path=approved, project_id="p", name="Approved", description="Example"))
        parent = self.definition.model_copy(update={"skills": (SkillBinding(id="approved"),), "plugins": ("agent-factory",), "creation": AgentCreation(enabled=True, model_refs=(self.first["id"],))})
        await self.host.agents.save(parent, expected_revision=1)
        run = await self.active()
        forged = (SkillBinding(id="approved", path=rejected), SkillBinding(id="approved", revision=99))
        for index, binding in enumerate(forged):
            for role in ("main", "manager"):
                child = AgentDefinition(id="rejected-child", name="Rejected", project_id="p", model_ref=self.first["id"],
                    skills=(binding,) if role == "main" else (), manager=Manager(model_ref=self.first["id"], skills=(binding,)) if role == "manager" else None)
                result = await self.host.tools.invoke(run.id, "create_agent", {"definition": child.model_dump(mode="json")}, f"forged-{index}-{role}")
                self.assertTrue(result.get("isError"), result)
        child = AgentDefinition(id="approved-child", name="Approved", project_id="p", model_ref=self.first["id"], skills=(SkillBinding(id="approved"),))
        result = await self.host.tools.invoke(run.id, "create_agent", {"definition": child.model_dump(mode="json")}, "canonical-skill")
        self.assertFalse(result.get("isError"), result)
        self.assertEqual(result["definition"]["skills"][0]["revision"], 1)
        self.assertIsNone(result["definition"]["skills"][0]["path"])
