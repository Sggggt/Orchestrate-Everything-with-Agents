"""Frozen initial protocol-three state is copied without replaying prior work."""

import asyncio
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from oea import Completion, RunSpec
from oea.contracts.errors import RuntimeFault
from oea.migration.architectures import INITIAL_V3_SCHEMA
from oea.migration.upgrade import upgrade_state
from oea.runtime.host import Host


class ArchitectureMigrationTests(unittest.TestCase):
    def test_read_only_transition_retains_history_and_does_not_replay_old_work(self):
        script = Path(__file__).parents[1].joinpath("src/oea/migration/schema-v3-initial.sql").read_text(encoding="utf-8")
        self.assertEqual(hashlib.sha256(script.encode()).hexdigest(), INITIAL_V3_SCHEMA)
        class Fixture:
            calls = 0
            async def complete(inner, *args, **kwargs):
                inner.calls += 1
                return Completion(text="Saved result", usage={"prompt_tokens": 20, "completion_tokens": 5})
        with tempfile.TemporaryDirectory(prefix="oea-architecture-migration-") as directory:
            root, target = Path(directory) / "source", Path(directory) / "target"
            work = root / "workspaces" / "instance"
            work.mkdir(parents=True)
            (work / "partial.txt").write_text("Retained file", encoding="utf-8")
            run_id, instance_id = "original", "instance"
            config = json.dumps({"id": "worker", "name": "Worker", "project_id": "p",
                                 "model": {"base_url": "http://model.invalid", "model": "test"}})
            with closing(sqlite3.connect(root / "oea.sqlite")) as db, db:
                db.executescript(script)
                db.executemany("INSERT INTO metadata VALUES(?,?)", [("protocol_version", "3.0"), ("schema_sha256", INITIAL_V3_SCHEMA), ("schema_revision", "1")])
                db.execute("INSERT INTO projects(id,name,created_at) VALUES('p','Example',1)")
                db.execute("INSERT INTO sessions(id,project_id,name,created_at) VALUES('session','p','Session',1)")
                db.execute("INSERT INTO agent_definitions VALUES('worker','p',?,1,1)", (config,))
                db.execute("INSERT INTO instances VALUES(?,'p','session','worker','Worker','main',?,?,NULL,1)",
                           (instance_id, config, json.dumps({"work_dir": str(work)})))
                db.execute("INSERT INTO budgets(id,project_id,session_id,root_run_id,policy_json,created_at) VALUES('budget','p','session',?,'{}',1)", (run_id,))
                db.execute("INSERT INTO runs(id,instance_id,root_run_id,budget_id,state,input_json,idempotency_key,input_hash,model_role,created_at,updated_at,config_json) VALUES(?,?,?,'budget','waiting','\"Save evidence\"','original','saved-input','main',1,1,?)",
                           (run_id, instance_id, run_id, config))
                db.execute("UPDATE instances SET active_run_id=? WHERE id=?", (run_id, instance_id))
                db.execute("INSERT INTO actions(id,run_id,operation_key,name,input_hash,input_json,side_effects,state,result_json,created_at,updated_at) VALUES('model',?,'model','model.complete','saved-model','{}','none','completed',?,1,1)",
                           (run_id, json.dumps({"text": "Saved partial result", "message": {"role": "assistant", "content": "Saved partial result"}})))
                db.execute("INSERT INTO actions(id,run_id,operation_key,name,input_hash,input_json,side_effects,state,created_at,updated_at) VALUES('pending',?,'pending','exec_command','saved-command','{}','local','dispatched',1,1)", (run_id,))
                db.execute("INSERT INTO usage(id,budget_id,run_id,action_id,purpose,kind,state,input_tokens,output_tokens,created_at) VALUES('usage','budget',?,'model','main','model','settled',20,5,1)", (run_id,))
                db.execute("INSERT INTO conversation(id,instance_id,run_id,role,input_run_id,created_at) VALUES('input',?,?,'user',?,1)", (instance_id, run_id, run_id))
                db.execute("INSERT INTO conversation(id,instance_id,run_id,role,action_id,created_at) VALUES('partial',?,?,'assistant','model',2)", (instance_id, run_id))
                db.execute("INSERT INTO memories VALUES('memory',?,?,1,'[]','Retained stage memory',1)", (instance_id, run_id))
                db.execute("INSERT INTO events(kind,project_id,session_id,instance_id,run_id,data_json,created_at) VALUES('run.waiting','p','session',?,?,'{}',1)", (instance_id, run_id))
                self.assertNotIn("source", {row[1] for row in db.execute("PRAGMA table_info(sessions)")})
                self.assertNotIn("applications", {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")})
                self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
            with closing(sqlite3.connect(root / "graphs.sqlite")) as db, db:
                db.execute("CREATE TABLE checkpoint_evidence(id TEXT PRIMARY KEY,value TEXT)")
                db.execute("INSERT INTO checkpoint_evidence VALUES('original','archived position')")
            (root / "client-token").write_text("earlier-browser-credential", encoding="utf-8")
            original = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
            report = upgrade_state(root, target=target)
            self.assertFalse(report["applied"])
            self.assertFalse(target.exists())
            report = upgrade_state(root, target=target, apply=True)
            self.assertEqual(report["review_runs"], [run_id])
            self.assertNotEqual((target / "client-token").read_bytes(), original[Path("client-token")])
            self.assertEqual(original, {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()})
            self.assertEqual((target / "imported/protocol-3-initial/graphs.sqlite").read_bytes(), original[Path("graphs.sqlite")])
            self.assertFalse((target / "graphs.sqlite").exists())
            self.assertEqual((target / "workspaces/instance/partial.txt").read_text(encoding="utf-8"), "Retained file")
            with closing(sqlite3.connect(target / "oea.sqlite")) as db:
                self.assertEqual(db.execute("SELECT source FROM sessions WHERE id='session'").fetchone()[0], "platform")
                self.assertEqual(db.execute("SELECT source FROM events").fetchone()[0], "platform")
                self.assertEqual(db.execute("SELECT state FROM actions WHERE id='pending'").fetchone()[0], "needs_review")
                self.assertEqual(db.execute("SELECT state,input_tokens,output_tokens FROM usage").fetchone(), ("settled", 20, 5))
                self.assertEqual(db.execute("SELECT COUNT(*) FROM conversation").fetchone()[0], 2)
                self.assertTrue(any("Retained stage memory" in body for body, in db.execute("SELECT body FROM memories")))
                self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
            async def inspect():
                fixture = Fixture()
                async with Host(target, model_api=fixture) as host:
                    self.assertEqual(fixture.calls, 0)
                    self.assertEqual((await host.runs.get(run_id))["state"], "needs_review")
                    with self.assertRaises(RuntimeFault) as fault:
                        await host.runs.resume(run_id, "resume")
                    self.assertEqual(fault.exception.code, "EXECUTION_VERSION_UNSUPPORTED")
                    self.assertEqual((await host.architectures.list("p"))["items"], [])
                    fresh = await host.runs.submit(RunSpec(instance_id=instance_id, input="New independent work", idempotency_key="new"))
                    self.assertEqual((await host.runs.result(fresh.id, timeout=10)).text, "Saved result")
                    self.assertEqual(fixture.calls, 1)
            asyncio.run(inspect())
