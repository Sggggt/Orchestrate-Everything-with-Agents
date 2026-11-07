"""Application identity and authorization use real SQLite transactions."""

import asyncio
from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3
import tempfile
import unittest

from pydantic import ValidationError

from oea import AgentDefinition
from oea.contracts.applications import ApplicationBinding, ApplicationGrant, ExecutionScope
from oea.contracts.configuration import Workspace
from oea.contracts.errors import RuntimeFault
from oea.migration.upgrade import upgrade_state
from oea.runtime.host import Host


class ApplicationIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="oea-applications-")
        self.addCleanup(self.temporary.cleanup)
        self.host = await Host(Path(self.temporary.name) / "state").__aenter__()
        self.addAsyncCleanup(self.host.aclose)
        await self.host.projects.create("Example", project_id="p")
        await self.host.agents.save(AgentDefinition(id="worker", name="Worker", project_id="p"))

    async def test_credentials_are_application_specific_rotatable_and_not_in_metadata(self):
        first = await self.host.applications.create("First")
        second = await self.host.applications.create("Second")
        identity = first["application"]["id"]
        self.assertEqual(await self.host.applications.authenticate(first["credential"]), identity)
        forged = first["credential"].replace(identity, second["application"]["id"])
        for credential in (forged, "management-token", identity, "", "oap." + identity + ".wrong"):
            with self.assertRaises(RuntimeFault) as fault:
                await self.host.applications.authenticate(credential)
            self.assertEqual(fault.exception.code, "PERMISSION_DENIED")
        rotated = await self.host.applications.rotate_credential(identity, 1)
        with self.assertRaises(RuntimeFault):
            await self.host.applications.authenticate(first["credential"])
        self.assertEqual(await self.host.applications.authenticate(rotated["credential"]), identity)
        self.assertNotIn("token_hash", await self.host.applications.get(identity))
        self.assertNotIn("credential", str(await self.host.applications.bindings(identity)))
        saved = await self.host.store.call(lambda db: list(db.execute("SELECT token_hash FROM applications")))
        self.assertTrue(all(len(row["token_hash"]) == 64 for row in saved))
        await self.host.applications.set_enabled(identity, False, 2)
        with self.assertRaises(RuntimeFault):
            await self.host.applications.authenticate(rotated["credential"])

    async def test_binding_ownership_and_optimistic_revision(self):
        application = await self.host.applications.create("Example")
        identity = application["application"]["id"]
        binding = ApplicationBinding(app_id=identity, project_id="p", agent_id="worker")
        saved = await self.host.applications.bind(binding)
        changed = binding.model_copy(update={"enabled": False})
        with self.assertRaises(RuntimeFault) as fault:
            await self.host.applications.bind(changed, binding_id=saved["id"], expected_revision=0)
        self.assertEqual(fault.exception.code, "REVISION_CONFLICT")
        saved = await self.host.applications.bind(changed, binding_id=saved["id"], expected_revision=1)
        self.assertEqual(saved["revision"], 2)
        self.assertFalse(saved["enabled"])
        await self.host.projects.create("Other", project_id="q")
        with self.assertRaises(RuntimeFault):
            await self.host.applications.bind(binding.model_copy(update={"project_id": "q"}))

    async def test_removed_identity_cannot_be_reenabled_and_metadata_is_paginated(self):
        application = await self.host.applications.create("Example")
        identity = application["application"]["id"]
        await self.host.applications.create("Another")
        page = await self.host.applications.list(limit=1)
        self.assertEqual(len(page["items"]), 1)
        self.assertIsNotNone(page["cursor"])
        self.assertEqual(len((await self.host.applications.list(cursor=page["cursor"], limit=1))["items"]), 1)
        await self.host.applications.remove(identity, 1)
        with self.assertRaises(RuntimeFault):
            await self.host.applications.set_enabled(identity, True, 2)
        with self.assertRaises(RuntimeFault):
            await self.host.applications.authenticate(application["credential"])

    async def test_shared_management_registry_exposes_typed_binding_contract(self):
        schema = self.host.registry.describe("applications.bind")["parameters_schema"]
        self.assertIn("ApplicationBinding", schema["$defs"])
        created = await self.host.registry.call("applications.create", {"name": "Example"})
        self.assertEqual(created["application"]["name"], "Example")
        with self.assertRaises(RuntimeFault):
            await self.host.registry.call("applications.create", {"name": "Example", "app_id": "forged"})


class ApplicationContractTests(unittest.TestCase):
    def test_application_scope_is_complete_and_cannot_carry_platform_identity(self):
        with self.assertRaises(ValidationError):
            ExecutionScope(source="application", app_id="app")
        with self.assertRaises(ValidationError):
            ExecutionScope(app_id="app")
        scope = ExecutionScope(source="application", app_id="app", binding_id="binding", conversation_id="conversation")
        with self.assertRaises(ValidationError):
            scope.app_id = "other"

    def test_grants_do_not_inherit_project_directories_or_trusted_execution(self):
        for workspace in (Workspace(), Workspace(inherit_project=False, work=Path("work")),
                          Workspace(inherit_project=False, project=Path("project")),
                          Workspace(inherit_project=False, sandbox="trusted")):
            with self.assertRaises(ValidationError):
                ApplicationGrant(workspace=workspace)


class ApplicationMigrationTests(unittest.TestCase):
    def test_upgrade_preserves_checkpoints_paths_and_original_source(self):
        schema = Path(__file__).parents[1].joinpath("src/oea/migration/schema-v3-pre-oap.sql").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory(prefix="oea-application-upgrade-") as temporary:
            source, target = Path(temporary) / "source", Path(temporary) / "target"
            source.mkdir()
            artifact = source / "artifacts" / "artifact_saved"
            artifact.parent.mkdir()
            artifact.write_bytes(b"Retained published evidence")
            with closing(sqlite3.connect(source / "oea.sqlite")) as db:
                db.executescript(schema)
                db.executemany("INSERT INTO metadata VALUES(?,?)", [("protocol_version", "3.0"), ("schema_revision", "1"), ("schema_sha256", hashlib.sha256(schema.encode()).hexdigest())])
                db.execute("INSERT INTO projects(id,name,folder,created_at) VALUES('p','Example',?,1)", (str(source / "existing-project"),))
                db.execute("INSERT INTO sessions(id,project_id,name,created_at) VALUES('s','p','Existing',1)")
                db.execute("INSERT INTO artifacts(id,project_id,session_id,name,path,sha256,bytes,media_type,created_at,origin) VALUES('artifact_saved','p','s','evidence.txt',?,?,?,'text/plain',1,'upload')",
                           (str(artifact), hashlib.sha256(artifact.read_bytes()).hexdigest(), artifact.stat().st_size))
                db.commit()
            with closing(sqlite3.connect(source / "graphs.sqlite")) as db:
                db.execute("CREATE TABLE checkpoint_evidence(id TEXT PRIMARY KEY,value TEXT)")
                db.execute("INSERT INTO checkpoint_evidence VALUES('original','retained')")
                db.commit()
            (source / "client-token").write_text("previous-local-control-credential", encoding="utf-8")
            before = {path.relative_to(source).as_posix(): path.read_bytes() for path in source.rglob("*") if path.is_file()}
            self.assertFalse(upgrade_state(source)["applied"])
            preview = upgrade_state(source, target=target)
            self.assertFalse(preview["applied"])
            self.assertFalse(target.exists())
            result = upgrade_state(source, target=target, apply=True)
            self.assertTrue(result["applied"])
            self.assertEqual(before, {path.relative_to(source).as_posix(): path.read_bytes() for path in source.rglob("*") if path.is_file()})
            self.assertEqual(before["graphs.sqlite"], (target / "graphs.sqlite").read_bytes())
            self.assertNotEqual(before["client-token"], (target / "client-token").read_bytes())
            with closing(sqlite3.connect(target / "oea.sqlite")) as db:
                self.assertEqual(db.execute("SELECT source FROM sessions WHERE id='s'").fetchone()[0], "platform")
                self.assertEqual(db.execute("SELECT folder FROM projects WHERE id='p'").fetchone()[0], str(source / "existing-project"))
                self.assertEqual(db.execute("SELECT path FROM artifacts WHERE id='artifact_saved'").fetchone()[0], str(target / "artifacts" / "artifact_saved"))
                self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
                self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            async def verify_store():
                from oea.storage.persistence import Store
                store = Store(target)
                await store.open()
                await store.close()
                async with Host(target) as host:
                    self.assertEqual((await host.artifacts.read("artifact_saved", "p"))["text"], "Retained published evidence")
            asyncio.run(verify_store())
