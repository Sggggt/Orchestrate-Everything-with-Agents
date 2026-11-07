"""Offline conversion boundaries; all inputs are generated outside the source tree."""

import asyncio
import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from oea.contracts.configuration import AgentDefinition
from oea.contracts.errors import RuntimeFault
from oea.migration.history import ImportedHistory
from oea.migration.offline import migrate_legacy
from oea.security.secrets import SecretVault
from oea.security.lock import WorkspaceLock
from oea.workspace.service import WorkspacePaths
from oea.storage.persistence import Store


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "legacy"
        self.source.mkdir()
        self.target = self.root / "converted"
        db = sqlite3.connect(self.source / "runtime.sqlite")
        db.executescript("""
            CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY);
            INSERT INTO schema_migrations VALUES(29);
            CREATE TABLE projects(id TEXT PRIMARY KEY,name TEXT,local_folder TEXT,state TEXT,created_at INTEGER);
            INSERT INTO projects VALUES('p','Example','','OPEN',1);
            CREATE TABLE sessions(id TEXT PRIMARY KEY,project_id TEXT,title TEXT,state TEXT,created_at INTEGER);
            INSERT INTO sessions VALUES('s','p','Example','CLOSED',2);
            CREATE TABLE agent_configs(id TEXT PRIMARY KEY,config_json TEXT,updated_at INTEGER);
            CREATE TABLE threads(id TEXT PRIMARY KEY,session_id TEXT,agent_id TEXT,title TEXT,created_at INTEGER);
            INSERT INTO threads VALUES('t','s','example','Duplicate title',3),('manager-t','s','example','Duplicate title',4);
            CREATE TABLE tasks(id TEXT PRIMARY KEY,session_id TEXT,thread_id TEXT,state TEXT,created_at INTEGER);
            INSERT INTO tasks VALUES('task','s','t','RUNNING',4);
            CREATE TABLE runs(id TEXT PRIMARY KEY,task_id TEXT,state TEXT,started_at INTEGER);
            INSERT INTO runs VALUES('run','task','WAITING',5);
            CREATE TABLE collaboration_participants(id TEXT PRIMARY KEY,thread_id TEXT,kind TEXT);
            INSERT INTO collaboration_participants VALUES('manager','manager-t','manager');
            CREATE TABLE conversation(seq INTEGER PRIMARY KEY,id TEXT,session_id TEXT,thread_id TEXT,task_id TEXT,run_id TEXT,role TEXT,content TEXT,created_at INTEGER,summary_markdown TEXT);
            INSERT INTO conversation VALUES(1,'c','s','t','task','run','assistant','Archived answer',6,'Short answer');
            CREATE TABLE thread_activity_items(thread_id TEXT,kind TEXT,source_id TEXT,read_at INTEGER);
            INSERT INTO thread_activity_items VALUES('t','conversation','c',7);
            CREATE TABLE messages(id TEXT PRIMARY KEY,session_id TEXT,source_key TEXT,envelope_json TEXT,created_at INTEGER);
            INSERT INTO messages VALUES('m','s','controller','{"payload":{"text":"Historical instruction"}}',8);
            CREATE TABLE artifacts(id TEXT PRIMARY KEY,session_id TEXT,relative_path TEXT,sha256 TEXT,size INTEGER,name TEXT,media_type TEXT,created_at INTEGER);
            CREATE TABLE budget_entries(id TEXT PRIMARY KEY,task_id TEXT,run_id TEXT,kind TEXT,state TEXT,input_tokens INTEGER,output_tokens INTEGER,created_at INTEGER);
            INSERT INTO budget_entries VALUES('usage','task','run','model','SETTLED',17,23,9);
            CREATE TABLE opaque(key BLOB PRIMARY KEY,value BLOB);
            INSERT INTO opaque VALUES(X'01',X'00FF01');
        """)
        config = {"id": "example", "project_id": "p", "name": "Example", "kind": "llm", "base_url": "https://example.invalid/v1", "model": "example-model",
            "manager_model": {"base_url": "https://example.invalid/v1", "model": "manager-model"}}
        db.execute("INSERT INTO agent_configs VALUES(?,?,?)", ("example", json.dumps(config), 1))
        blob = self.source / "artifacts" / "sha256" / "example"
        blob.parent.mkdir(parents=True)
        blob.write_bytes(b"example artifact\x00\xff")
        self.blob_hash = hashlib.sha256(blob.read_bytes()).hexdigest()
        db.execute("INSERT INTO artifacts VALUES(?,?,?,?,?,?,?,?)", ("artifact", "s", "sha256/example", self.blob_hash, blob.stat().st_size, "example.bin", "application/octet-stream", 10))
        db.commit()
        db.close()
        vault = SecretVault(self.source)
        vault.set("example", "main-example-secret")
        vault.set("manager:example", "manager-example-secret")

    def test_dry_run_does_not_change_source_or_create_target(self):
        before = {path.relative_to(self.source): path.read_bytes() for path in self.source.rglob("*") if path.is_file()}
        report = migrate_legacy(self.source, self.target)
        self.assertFalse(self.target.exists())
        self.assertFalse(report["applied"])
        self.assertTrue(report["ready_for_apply"])
        self.assertEqual(report["usage"]["input_tokens"], 17)
        self.assertEqual(report["secrets"]["entry_count"], 2)
        after = {path.relative_to(self.source): path.read_bytes() for path in self.source.rglob("*") if path.is_file()}
        self.assertEqual(before, after)

    def test_apply_preserves_history_and_separate_credentials_without_runs(self):
        report = migrate_legacy(self.source, self.target, apply=True)
        self.assertTrue(report["applied"])
        self.assertEqual(report["needs_review"]["count"], 2)
        db = sqlite3.connect(self.target / "oea.sqlite")
        self.addCleanup(db.close)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 0)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM conversation").fetchone()[0], 0)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0], 0)
        self.assertEqual(db.execute("SELECT archived FROM sessions").fetchone()[0], 1)
        self.assertEqual(db.execute("SELECT origin,run_id,instance_id FROM artifacts").fetchone(), ("import", None, None))
        config = json.loads(db.execute("SELECT config_json FROM agent_definitions").fetchone()[0])
        self.assertEqual(config["model"]["credential_ref"], "vault:example")
        self.assertEqual(config["manager"]["model"]["credential_ref"], "vault:manager:example")
        self.assertEqual(SecretVault(self.target).get("manager:example"), "manager-example-secret")
        archived = json.loads(db.execute("SELECT row_json FROM imported_history WHERE table_name='opaque'").fetchone()[0])
        self.assertEqual(archived["value"], {"$sqlite_blob": "AP8B"})
        self.assertEqual(db.execute("SELECT COUNT(*) FROM imported_history").fetchone()[0], sum(report["table_counts"].values()))
        self.assertEqual(hashlib.sha256((self.target / "imported/legacy.sqlite").read_bytes()).hexdigest(), report["source_sha256"])
        self.assertEqual(list(db.execute("PRAGMA foreign_key_check")), [])

    def test_target_and_wal_are_rejected(self):
        self.target.mkdir()
        with self.assertRaises(RuntimeFault) as occupied:
            migrate_legacy(self.source, self.target, apply=True)
        self.assertEqual(occupied.exception.code, "MIGRATION_TARGET_EXISTS")
        (self.source / "runtime.sqlite-wal").write_bytes(b"not checkpointed")
        with self.assertRaises(RuntimeFault) as active:
            migrate_legacy(self.source)
        self.assertEqual(active.exception.code, "MIGRATION_SOURCE_ACTIVE")

    def test_missing_artifact_is_reported_and_apply_is_not_published(self):
        (self.source / "artifacts/sha256/example").unlink()
        report = migrate_legacy(self.source)
        self.assertFalse(report["ready_for_apply"])
        self.assertEqual(report["artifacts"]["verified"], 0)
        with self.assertRaises(RuntimeFault) as invalid:
            migrate_legacy(self.source, self.target, apply=True)
        self.assertEqual(invalid.exception.code, "MIGRATION_REVIEW_REQUIRED")
        db = sqlite3.connect(self.target / "oea.sqlite")
        self.addCleanup(db.close)
        self.assertIsNone(db.execute("SELECT value FROM metadata WHERE key='protocol_version'").fetchone())

    def test_history_reads_are_scoped_and_usage_is_aggregated(self):
        migrate_legacy(self.source, self.target, apply=True)
        async def check():
            store = Store(self.target)
            await store.open()
            try:
                history = ImportedHistory(SimpleNamespace(store=store))
                usage = await history.usage("p")
                self.assertEqual((usage["entries"], usage["input_tokens"], usage["output_tokens"]), (1, 17, 23))
                transcript = await history.conversation("p", instance_id="t", limit=1)
                self.assertEqual(transcript["items"][0]["content"], "Archived answer")
                self.assertEqual(transcript["items"][0]["read_at"], 7)
                self.assertEqual((await history.conversation("other", instance_id="t"))["items"], [])
                with self.assertRaises(RuntimeFault):
                    await history.get("other", transcript["items"][0]["position"])
                self.assertEqual((await history.list("p", "tasks", needs_review=True))["items"][0]["review_state"], "needs_review")
            finally:
                await store.close()
        asyncio.run(check())


    def test_active_host_is_rejected_and_stale_lock_is_unchanged(self):
        lock = WorkspaceLock(self.source)
        lock.acquire()
        try:
            with self.assertRaises(RuntimeFault) as active:
                migrate_legacy(self.source, self.target, apply=True)
            self.assertEqual(active.exception.code, "MIGRATION_SOURCE_ACTIVE")
            self.assertFalse(self.target.exists())
        finally:
            lock.release()
        before = (self.source / "host.lock").read_bytes()
        self.assertTrue(migrate_legacy(self.source)["ready_for_apply"])
        self.assertEqual((self.source / "host.lock").read_bytes(), before)

    def test_rollback_journal_and_nested_target_are_rejected(self):
        journal = self.source / "runtime.sqlite-journal"
        journal.write_bytes(b"unresolved transaction")
        with self.assertRaises(RuntimeFault) as active:
            migrate_legacy(self.source)
        self.assertEqual(active.exception.code, "MIGRATION_SOURCE_ACTIVE")
        journal.unlink()
        with self.assertRaises(RuntimeFault) as nested:
            migrate_legacy(self.source, self.source / "nested")
        self.assertEqual(nested.exception.code, "MIGRATION_TARGET_INVALID")
        with self.assertRaises(RuntimeFault) as missing:
            migrate_legacy(self.source, apply=True)
        self.assertEqual(missing.exception.code, "MIGRATION_TARGET_REQUIRED")

    def test_source_material_changes_prevent_publication(self):
        from oea.migration.offline import Converter
        material = self.source / "original.txt"
        material.write_text("before", encoding="utf-8")
        archive = Converter.archive
        def changing(converter):
            archive(converter)
            material.write_text("changed during migration", encoding="utf-8")
        with patch.object(Converter, "archive", changing):
            with self.assertRaises(RuntimeFault) as changed:
                migrate_legacy(self.source, self.target, apply=True)
        self.assertEqual(changed.exception.code, "MIGRATION_SOURCE_CHANGED")
        db = sqlite3.connect(self.target / "oea.sqlite")
        self.addCleanup(db.close)
        self.assertIsNone(db.execute("SELECT value FROM metadata WHERE key='protocol_version'").fetchone())

    def test_appearing_source_file_is_detected(self):
        from oea.migration.offline import Converter
        archive = Converter.archive
        def changing(converter):
            archive(converter)
            (self.source / "new.txt").write_text("new fact", encoding="utf-8")
        with patch.object(Converter, "archive", changing):
            with self.assertRaises(RuntimeFault) as changed:
                migrate_legacy(self.source)
        self.assertEqual(changed.exception.code, "MIGRATION_SOURCE_CHANGED")
        self.assertFalse(self.target.exists())

    def test_auxiliary_material_is_preserved_in_full(self):
        material = self.source / "secondary-wal"
        material.write_bytes(b"auxiliary evidence")
        report = migrate_legacy(self.source, self.target, apply=True)
        self.assertEqual((self.target / "imported/source-files/secondary-wal").read_bytes(), material.read_bytes())
        self.assertTrue(report["secrets"]["copied_verified"])

    def test_original_work_directory_is_bound_without_legacy_environment(self):
        work = self.root / "work"
        work.mkdir()
        reference = self.root / "reference"
        reference.mkdir()
        db = sqlite3.connect(self.source / "runtime.sqlite")
        db.execute("CREATE TABLE task_workbenches(task_id TEXT PRIMARY KEY,snapshot_json TEXT)")
        db.execute("INSERT INTO task_workbenches VALUES(?,?)", ("task", json.dumps({"directory": str(work),
            "environment": {"runner": "local", "variables": {"EXAMPLE": "value"}, "resources": {"/reference": str(reference)}}})))
        db.commit()
        db.close()
        report = migrate_legacy(self.source, self.target, apply=True)
        db = sqlite3.connect(self.target / "oea.sqlite")
        self.addCleanup(db.close)
        config_json, binding_json = db.execute("SELECT config_json,workspace_json FROM instances WHERE id='t'").fetchone()
        definition = AgentDefinition.model_validate_json(config_json)
        self.assertEqual(definition.workspace.environment, {})
        paths = WorkspacePaths.for_instance(definition.workspace, "t", "new-run", self.target, binding=json.loads(binding_json))
        self.assertEqual(paths.work_dir, work)
        self.assertEqual(report["directories"]["moved"], 0)
        self.assertEqual(report["directories"]["copied"], 0)
        self.assertTrue(any(item["path"] == str(reference) for item in report["directories"]["items"]))
        manager = AgentDefinition.model_validate_json(db.execute("SELECT config_json FROM instances WHERE id='manager-t'").fetchone()[0])
        self.assertEqual(manager.model.credential_ref, "vault:manager:example")
        self.assertEqual(manager.tools.local, ())

    def test_thread_work_directory_takes_precedence_over_task_workbench(self):
        work = self.root / "stable-work"
        work.mkdir()
        db = sqlite3.connect(self.source / "runtime.sqlite")
        db.executescript("CREATE TABLE task_workbenches(task_id TEXT PRIMARY KEY,snapshot_json TEXT); CREATE TABLE thread_workdirs(thread_id TEXT PRIMARY KEY,path TEXT);")
        db.execute("INSERT INTO task_workbenches VALUES(?,?)", ("task", json.dumps({"directory": str(self.root / "task-work")})))
        db.execute("INSERT INTO thread_workdirs VALUES(?,?)", ("t", str(work)))
        db.commit()
        db.close()
        migrate_legacy(self.source, self.target, apply=True)
        db = sqlite3.connect(self.target / "oea.sqlite")
        self.addCleanup(db.close)
        binding = json.loads(db.execute("SELECT workspace_json FROM instances WHERE id='t'").fetchone()[0])
        self.assertEqual(binding["work_dir"], str(work))

    def _add_cumulative_budget(self, *, incoming=17, reserved=False):
        db = sqlite3.connect(self.source / "runtime.sqlite")
        db.executescript("CREATE TABLE execution_budgets(id TEXT PRIMARY KEY,session_id TEXT,model_calls INTEGER,tool_calls INTEGER,input_tokens INTEGER,output_tokens INTEGER); ALTER TABLE budget_entries ADD COLUMN budget_id TEXT;")
        db.execute("INSERT INTO execution_budgets VALUES('budget','s',?,0,?,?)", (2 if reserved else 1, incoming + (11 if reserved else 0), 23 + (13 if reserved else 0)))
        db.execute("UPDATE budget_entries SET budget_id='budget'")
        if reserved:
            db.execute("INSERT INTO budget_entries VALUES('reserved','task','run','model','RESERVED',11,13,10,'budget')")
        db.commit()
        db.close()

    def test_cumulative_usage_and_reservations_are_reconciled(self):
        self._add_cumulative_budget(reserved=True)
        report = migrate_legacy(self.source, self.target, apply=True)
        self.assertEqual(report["usage"]["cumulative"], {"model_calls": 2, "tool_calls": 0, "input_tokens": 28, "output_tokens": 36})
        self.assertEqual(report["usage"]["reconciliation"]["matched"], 1)
        by_state = {item["state"]: item for item in report["usage"]["by_state"]}
        self.assertEqual(by_state["SETTLED"]["input_tokens"], 17)
        self.assertEqual(by_state["RESERVED"]["input_tokens"], 11)
        self.assertEqual(report["needs_review"]["by_table"]["budget_entries"], 1)

    def test_usage_mismatch_blocks_publication_and_preserves_review_report(self):
        self._add_cumulative_budget(incoming=18)
        report = migrate_legacy(self.source)
        self.assertFalse(report["ready_for_apply"])
        self.assertTrue(any(item["code"] == "USAGE_TOTAL_MISMATCH" for item in report["issues"]))
        with self.assertRaises(RuntimeFault) as mismatch:
            migrate_legacy(self.source, self.target, apply=True)
        self.assertEqual(mismatch.exception.code, "MIGRATION_REVIEW_REQUIRED")
        persisted = json.loads((self.target / "migration-report.json").read_text(encoding="utf-8"))
        self.assertFalse(persisted["applied"])
        db = sqlite3.connect(self.target / "oea.sqlite")
        self.addCleanup(db.close)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM imported_history").fetchone()[0], sum(report["table_counts"].values()))
        self.assertIsNone(db.execute("SELECT value FROM metadata WHERE key='protocol_version'").fetchone())
        async def cannot_open():
            store = Store(self.target)
            with self.assertRaises(RuntimeFault) as incomplete:
                await store.open()
            self.assertEqual(incomplete.exception.code, "SCHEMA_MISMATCH")
        asyncio.run(cannot_open())

    def test_http_mcp_transport_and_vault_references_are_preserved(self):
        reference = "mcp:service:Authorization"
        SecretVault(self.source).set(reference, "example-header-secret")
        config = {"id": "service", "name": "Service", "transport": "streamable-http", "url": "https://example.invalid/mcp", "cwd": "", "command": ""}
        db = sqlite3.connect(self.source / "runtime.sqlite")
        db.execute("CREATE TABLE mcp_servers(id TEXT PRIMARY KEY,config_json TEXT,credentials_json TEXT,updated_at INTEGER)")
        db.execute("INSERT INTO mcp_servers VALUES(?,?,?,1)", ("service", json.dumps(config), json.dumps({"headers": {"Authorization": reference}})))
        db.commit()
        db.close()
        report = migrate_legacy(self.source, self.target, apply=True)
        self.assertEqual(report["secrets"]["missing_references"], 0)
        db = sqlite3.connect(self.target / "oea.sqlite")
        self.addCleanup(db.close)
        imported = json.loads(db.execute("SELECT config_json FROM mcp_servers").fetchone()[0])
        self.assertEqual(imported["transport"], "streamable_http")
        self.assertEqual(imported["header_refs"], {"Authorization": "vault:" + reference})
        self.assertIsNone(imported["cwd"])
        self.assertEqual(SecretVault(self.target).get(reference), "example-header-secret")

    def test_unavailable_vault_blocks_apply_without_modifying_source(self):
        (self.source / "secrets.enc").write_bytes(b"invalid encrypted vault")
        before = (self.source / "secrets.enc").read_bytes()
        report = migrate_legacy(self.source)
        self.assertFalse(report["ready_for_apply"])
        self.assertFalse(report["secrets"]["available"])
        with self.assertRaises(RuntimeFault) as unavailable:
            migrate_legacy(self.source, self.target, apply=True)
        self.assertEqual(unavailable.exception.code, "MIGRATION_REVIEW_REQUIRED")
        self.assertEqual((self.source / "secrets.enc").read_bytes(), before)

    def test_history_pages_preserve_source_order_and_do_not_launch_work(self):
        from oea.runtime.host import Host
        self._add_cumulative_budget()
        db = sqlite3.connect(self.source / "runtime.sqlite")
        db.execute("INSERT INTO conversation VALUES(2,'c2','s','t','task','run','user','Second',10,NULL)")
        db.execute("INSERT INTO conversation VALUES(3,'c3','s','t','task','run','assistant','Third',11,NULL)")
        db.commit()
        db.close()
        migrate_legacy(self.source, self.target, apply=True)
        async def check():
            async with Host(self.target) as host:
                for name in ("summary", "list", "get", "conversation", "usage"):
                    self.assertTrue(host.registry.describe("history." + name)["read_only"])
                first = await host.registry.call("history.conversation", {"project_id": "p", "instance_id": "t", "limit": 1})
                second = await host.registry.call("history.conversation", {"project_id": "p", "instance_id": "t", "before": first["cursor"], "limit": 1})
                third = await host.registry.call("history.conversation", {"project_id": "p", "instance_id": "t", "before": second["cursor"], "limit": 1})
                self.assertEqual([first["items"][0]["content"], second["items"][0]["content"], third["items"][0]["content"]], ["Third", "Second", "Archived answer"])
                self.assertIsNone(third["cursor"])
                first_page = await host.history.list("p", "conversation", limit=1)
                next_page = await host.history.list("p", "conversation", cursor=first_page["cursor"], limit=1)
                self.assertNotEqual(first_page["items"][0]["position"], next_page["items"][0]["position"])
                usage = await host.registry.call("history.usage", {"project_id": "p", "session_id": "s"})
                self.assertEqual(usage["cumulative"]["input_tokens"], 17)
                self.assertEqual(usage["model_calls"], 1)
                self.assertEqual((await host.registry.call("history.summary", {"project_id": "p"}))["project_id"], "p")
                for limit in (0, 101, True):
                    with self.assertRaises(RuntimeFault):
                        await host.history.list("p", "tasks", limit=limit)
                with self.assertRaises(RuntimeFault):
                    await host.history.get("p", 0)
                self.assertEqual(await host.store.call(lambda db: db.execute("SELECT COUNT(*) FROM runs").fetchone()["COUNT(*)"]), 0)
                self.assertEqual(host.active, {})
        asyncio.run(check())


    def test_workflow_and_checkpoint_history_retains_project_scope(self):
        db = sqlite3.connect(self.source / "runtime.sqlite")
        db.executescript("""
            CREATE TABLE workflows(id TEXT PRIMARY KEY,project_id TEXT,graph_json TEXT,created_at INTEGER);
            INSERT INTO workflows VALUES('graph','p','{}',11);
            CREATE TABLE workflow_runs(id TEXT PRIMARY KEY,workflow_id TEXT,session_id TEXT,state TEXT,created_at INTEGER);
            INSERT INTO workflow_runs VALUES('graph-run','graph','s','WAITING',12);
            CREATE TABLE workflow_node_runs(run_id TEXT,node_id TEXT,state TEXT,PRIMARY KEY(run_id,node_id));
            INSERT INTO workflow_node_runs VALUES('graph-run','node','UNCERTAIN');
            CREATE TABLE checkpoints(id TEXT PRIMARY KEY,thread_id TEXT);
            INSERT INTO checkpoints VALUES('checkpoint','t');
            CREATE TABLE checkpoint_windows(checkpoint_id TEXT PRIMARY KEY,through_seq INTEGER);
            INSERT INTO checkpoint_windows VALUES('checkpoint',1);
        """)
        db.commit()
        db.close()
        report = migrate_legacy(self.source, self.target, apply=True)
        self.assertEqual(report["needs_review"]["by_table"]["workflows"], 1)
        self.assertEqual(report["needs_review"]["by_table"]["workflow_node_runs"], 1)
        db = sqlite3.connect(self.target / "oea.sqlite")
        self.addCleanup(db.close)
        self.assertEqual(db.execute("SELECT project_id,session_id FROM imported_history WHERE table_name='workflow_node_runs'").fetchone(), ("p", "s"))
        self.assertEqual(db.execute("SELECT project_id,instance_id FROM imported_history WHERE table_name='checkpoint_windows'").fetchone(), ("p", "t"))
        self.assertEqual(db.execute("SELECT instance_id FROM imported_history WHERE table_name='threads' AND json_extract(row_json,'$.id')='t'").fetchone()[0], "t")
        self.assertEqual(db.execute("SELECT COUNT(*) FROM workflows").fetchone()[0], 0)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM workflow_runs").fetchone()[0], 0)

    def test_invalid_source_json_and_corrupt_database_have_structured_errors(self):
        db = sqlite3.connect(self.source / "runtime.sqlite")
        db.execute("UPDATE agent_configs SET config_json='[]'")
        db.commit()
        db.close()
        with self.assertRaises(RuntimeFault) as malformed:
            migrate_legacy(self.source)
        self.assertEqual(malformed.exception.code, "MIGRATION_RECORD_INVALID")
        (self.source / "runtime.sqlite").write_bytes(b"corrupt synthetic database")
        with self.assertRaises(RuntimeFault) as corrupt:
            migrate_legacy(self.source)
        self.assertEqual(corrupt.exception.code, "MIGRATION_SOURCE_CORRUPT")
        self.assertFalse(self.target.exists())


    def test_unsupported_version_and_broken_source_references_create_no_target(self):
        db = sqlite3.connect(self.source / "runtime.sqlite")
        db.execute("UPDATE schema_migrations SET version=28")
        db.commit()
        db.close()
        with self.assertRaises(RuntimeFault) as version:
            migrate_legacy(self.source, self.target, apply=True)
        self.assertEqual(version.exception.code, "MIGRATION_VERSION_UNSUPPORTED")
        self.assertFalse(self.target.exists())
        db = sqlite3.connect(self.source / "runtime.sqlite")
        db.executescript("UPDATE schema_migrations SET version=29; CREATE TABLE referenced_material(id TEXT PRIMARY KEY,project_id TEXT REFERENCES projects(id)); INSERT INTO referenced_material VALUES('broken','missing');")
        db.commit()
        db.close()
        with self.assertRaises(RuntimeFault) as reference:
            migrate_legacy(self.source, self.target, apply=True)
        self.assertEqual(reference.exception.code, "MIGRATION_REFERENCE_INVALID")
        self.assertFalse(self.target.exists())

    def test_missing_mcp_credential_is_a_blocking_reference_issue(self):
        config = {"id": "service", "transport": "stdio", "command": "example-server"}
        db = sqlite3.connect(self.source / "runtime.sqlite")
        db.execute("CREATE TABLE mcp_servers(id TEXT PRIMARY KEY,config_json TEXT,credentials_json TEXT,updated_at INTEGER)")
        db.execute("INSERT INTO mcp_servers VALUES(?,?,?,1)", ("service", json.dumps(config), json.dumps({"env": {"EXAMPLE_TOKEN": "missing-reference"}})))
        db.commit()
        db.close()
        report = migrate_legacy(self.source)
        self.assertEqual(report["secrets"]["missing_references"], 1)
        self.assertFalse(report["ready_for_apply"])
        self.assertTrue(any(issue["code"] == "SECRET_REFERENCE_MISSING" and issue["blocking"] for issue in report["issues"]))


if __name__ == "__main__":
    unittest.main()
