"""Offline Box migration retains old facts and removes only unreferenced secrets."""

import hashlib
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from oea.contracts.configuration import Model
from oea.migration.configuration_v3 import AgentDefinition, LegacyManager
from oea.migration.upgrade import upgrade_state
from oea.runtime.host import Host
from oea.security.secrets import SecretVault


class APIBoxMigrationTests(unittest.IsolatedAsyncioTestCase):
    def source(self, root):
        root.mkdir()
        vault = SecretVault(root)
        vault.set("shared", "fixture-value")
        vault.set("unused", "unused-fixture")
        vault.set("mcp", "mcp-fixture")
        model = Model(base_url="https://example.invalid/v1", model="example", credential_ref="vault:shared")
        definition = AgentDefinition(id="worker", name="Worker", project_id="p", model=model,
            summary_model=model.model_copy(update={"model": "summary"}),
            manager=LegacyManager(model=model.model_copy(update={"model": "manager"})))
        schema = Path(__file__).parents[1].joinpath("src/oea/storage/schema-v3.sql").read_text(encoding="utf-8")
        with closing(sqlite3.connect(root / "oea.sqlite")) as db, db:
            db.executescript(schema)
            db.executemany("INSERT INTO metadata VALUES(?,?)", [("protocol_version", "3.0"), ("schema_revision", "5"), ("schema_sha256", hashlib.sha256(schema.encode()).hexdigest())])
            db.execute("INSERT INTO projects(id,name,created_at) VALUES('p','Example',1)")
            db.execute("INSERT INTO agent_definitions VALUES('worker','p',?,1,1)", (definition.model_dump_json(),))
            db.execute("INSERT INTO sessions(id,project_id,name,created_at) VALUES('session','p','Example',1)")
            db.execute("INSERT INTO instances(id,project_id,session_id,agent_id,name,role,config_json,workspace_json,created_at) VALUES('instance','p','session','worker','Example','main',?,'{}',1)", (definition.model_dump_json(),))
            db.execute("INSERT INTO budgets(id,project_id,root_run_id,policy_json,created_at) VALUES('budget','p','run','{}',1)")
            db.execute("INSERT INTO runs(id,instance_id,root_run_id,budget_id,state,input_json,idempotency_key,input_hash,model_role,created_at,updated_at,config_json) VALUES('run','instance','run','budget','needs_review','{}','work','hash','main',1,1,?)", (definition.model_dump_json(),))
            db.execute("INSERT INTO actions(id,run_id,operation_key,name,input_hash,input_json,side_effects,state,created_at,updated_at) VALUES('action','run','original','model.complete','hash','{}','external','needs_review',1,1)")
            mcp = {"id": "service", "project_id": "p", "command": ["example"], "environment_refs": {"KEY": "vault:mcp"}}
            db.execute("INSERT INTO mcp_servers VALUES('service','p',?,1,1)", (json.dumps(mcp),))
        return hashlib.sha256((root / "oea.sqlite").read_bytes()).hexdigest()

    async def test_copy_imports_all_roles_and_preserves_pending_facts(self):
        with tempfile.TemporaryDirectory(prefix="oea-box-migration-") as temporary:
            root = Path(temporary)
            source, target = root / "source", root / "target"
            original_hash = self.source(source)
            dry = upgrade_state(source)
            self.assertFalse(dry["applied"])
            self.assertEqual(dry["models_imported"], 3)
            result = upgrade_state(source, target=target, apply=True)
            self.assertTrue(result["applied"])
            self.assertEqual(result["orphan_credentials_removed"], 1)
            self.assertEqual(hashlib.sha256((source / "oea.sqlite").read_bytes()).hexdigest(), original_hash)
            self.assertEqual(set(SecretVault(target).values), {"shared", "mcp"})
            with closing(sqlite3.connect(target / "oea.sqlite")) as db:
                declaration = json.loads(db.execute("SELECT config_json FROM agent_definitions").fetchone()[0])
                self.assertNotIn("model", declaration)
                self.assertNotIn("summary_model", declaration)
                self.assertNotIn("model", declaration["manager"])
                self.assertTrue(declaration["model_ref"])
                self.assertTrue(declaration["summary_model_ref"])
                run = db.execute("SELECT state,execution_version,config_json FROM runs").fetchone()
                self.assertEqual(run[:2], ("needs_review", "4.0"))
                self.assertEqual(json.loads(run[2])["model"]["credential_ref"], "vault:shared")
                self.assertEqual(db.execute("SELECT COUNT(*) FROM budgets").fetchone()[0], 1)
            async with Host(target) as host:
                self.assertEqual((await host.agents.get("worker"))["model_summary"]["manager"]["model_id"], "manager")
                self.assertEqual((await host.runs.get("run"))["state"], "needs_review")

    async def test_missing_credential_prevents_cleanup_and_target_activation(self):
        with tempfile.TemporaryDirectory(prefix="oea-box-migration-") as temporary:
            source = Path(temporary) / "source"
            self.source(source)
            SecretVault(source).set("shared", "")
            from oea.contracts.errors import RuntimeFault
            with self.assertRaises(RuntimeFault):
                upgrade_state(source)
            self.assertTrue(SecretVault(source).get("unused"))
