"""A recognized offline schema upgrade preserves facts and has a new backup."""

from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from oea.contracts.errors import RuntimeFault
from oea.migration.upgrade import upgrade_state, PRE_READING_SCHEMA, PRE_DELIVERY_SCHEMA, ADDED_COLUMNS
from oea.security.lock import WorkspaceLock


class UpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="oea-upgrade-")
        self.root = Path(self.temporary.name) / "state"
        self.root.mkdir()
        script = (Path(__file__).parents[1] / "src/oea/storage/schema-v2.sql").read_text(encoding="utf-8")
        lines = []
        for line in script.splitlines():
            if line.startswith(("CREATE TABLE result_snapshots(", "CREATE INDEX result_snapshots_project ", "CREATE TABLE run_tool_state(")):
                continue
            for table, columns in ADDED_COLUMNS.items():
                if line.startswith("CREATE TABLE " + table + "("):
                    for column in columns:
                        line = line.replace("," + column, "")
            lines.append(line)
        self.assertEqual(hashlib.sha256(("\n".join(lines) + "\n").encode()).hexdigest(), PRE_DELIVERY_SCHEMA)
        previous = "\n".join(line for line in lines if not line.startswith(("CREATE TABLE instance_read_positions(", "CREATE INDEX instance_read_positions_instance "))) + "\n"
        self.assertEqual(hashlib.sha256(previous.encode()).hexdigest(), PRE_READING_SCHEMA)
        self.database = self.root / "oea.sqlite"
        with closing(sqlite3.connect(self.database)) as db, db:
            db.executescript(previous)
            db.executemany("INSERT INTO metadata VALUES(?,?)", [("protocol_version", "2.0"), ("schema_sha256", PRE_READING_SCHEMA)])
            db.execute("INSERT INTO projects VALUES('p','Project',NULL,0,1)")
            db.execute("INSERT INTO sessions(id,project_id,name,created_at) VALUES('s','p','Session',1)")
            for index, kind in enumerate(("controller", "employee")):
                db.execute("INSERT INTO messages(id,project_id,session_id,source_json,purpose,body,idempotency_key,input_hash,created_at) VALUES(?,'p','s',?,'inform','Saved message',?,'hash',1)", (str(index), json.dumps({"kind": kind, "name": "Saved name"}), str(index)))
            db.execute("INSERT INTO read_positions VALUES('reader','s',1,1)")

    def tearDown(self):
        self.temporary.cleanup()

    def test_dry_run_apply_and_replay_preserve_original_facts(self):
        old = self.database.read_bytes()
        self.assertFalse(upgrade_state(self.root)["changed"])
        self.assertEqual(self.database.read_bytes(), old)
        backup = Path(self.temporary.name) / "original.sqlite"
        result = upgrade_state(self.root, backup_file=backup, apply=True)
        self.assertTrue(result["changed"])
        with closing(sqlite3.connect(backup)) as db, db:
            self.assertEqual(db.execute("SELECT value FROM metadata WHERE key='schema_sha256'").fetchone()[0], PRE_READING_SCHEMA)
        with closing(sqlite3.connect(self.database)) as db, db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM instance_read_positions").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT position FROM read_positions").fetchone()[0], 1)
            sources = [json.loads(row[0]) for row in db.execute("SELECT source_json FROM messages ORDER BY position")]
            self.assertEqual([value["kind"] for value in sources], ["leader", "instance"])
            self.assertEqual([value["name"] for value in sources], ["Saved name", "Saved name"])
        self.assertFalse(upgrade_state(self.root, apply=True)["changed"])

    def test_active_host_and_existing_backup_are_not_modified(self):
        lock = WorkspaceLock(self.root)
        lock.acquire()
        try:
            with self.assertRaises(RuntimeFault) as error:
                upgrade_state(self.root)
            self.assertEqual(error.exception.code, "RUNTIME_ALREADY_OPEN")
        finally:
            lock.release()
        backup = Path(self.temporary.name) / "existing.sqlite"
        backup.write_bytes(b"preserve")
        with self.assertRaises(FileExistsError):
            upgrade_state(self.root, backup_file=backup, apply=True)
        self.assertEqual(backup.read_bytes(), b"preserve")
        self.assertEqual(upgrade_state(self.root)["state"], "ready")

    def test_old_tool_selection_becomes_an_unversioned_candidate(self):
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("INSERT INTO agent_definitions VALUES('worker','p','{}',1,1)")
            db.execute("INSERT INTO instances VALUES('i','p','s','worker','Worker','main','{}','{}',NULL,1)")
            db.execute("INSERT INTO budgets(id,project_id,root_run_id,policy_json,created_at) VALUES('b','p','r','{}',1)")
            db.execute("INSERT INTO runs(id,instance_id,root_run_id,budget_id,state,input_json,idempotency_key,input_hash,model_role,created_at,updated_at) VALUES('r','i','r','b','failed','{}','r','hash','main',1,1)")
            for index, loaded in enumerate((True, False, True)):
                db.execute("INSERT INTO actions(id,run_id,operation_key,name,input_hash,input_json,side_effects,state,result_json,created_at,updated_at) VALUES(?,'r',?,'tool_guide','hash','{}','none','completed',?,1,1)",
                           (str(index), str(index), json.dumps({"name": "calculate", "loaded": loaded})))
        upgrade_state(self.root, backup_file=Path(self.temporary.name) / "selection-backup.sqlite", apply=True)
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(db.execute("SELECT name,loaded,contract_id FROM run_tool_state").fetchall(), [("calculate", 1, None)])

    def test_unknown_schema_is_rejected_before_backup_or_mutation(self):
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE metadata SET value='unknown' WHERE key='schema_sha256'")
        backup = Path(self.temporary.name) / "backup.sqlite"
        with self.assertRaises(RuntimeFault):
            upgrade_state(self.root, backup_file=backup, apply=True)
        self.assertFalse(backup.exists())
