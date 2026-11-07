"""Offline protocol conversion preserves source bytes, ownership and unknown work."""

import asyncio
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from oea import Completion, RunSpec
from oea.contracts.errors import RuntimeFault
from oea.migration.upgrade import upgrade_state, LATEST_V2_SCHEMA
from oea.runtime.host import Host
from oea.security.lock import WorkspaceLock
from oea.security.secrets import SecretVault


class UpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="oea-upgrade-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "source"
        self.target = self.root.parent / "converted"
        self.root.mkdir()
        self.database = self.root / "oea.sqlite"
        script = Path(__file__).parents[1].joinpath("src/oea/migration/schema-v2.sql").read_text(encoding="utf-8")
        self.assertEqual(hashlib.sha256(script.encode()).hexdigest(), LATEST_V2_SCHEMA)
        self.config = {"id": "worker", "name": "Worker", "project_id": "p", "model": {"base_url": "https://model.invalid/v1", "model": "example", "credential_ref": "vault:example"},
            "tools": {"local": ["read_file", "write_file", "run_command"]}, "manager": {"model": {"model": "optional"}},
            "execution": {"budget": {"max_input_tokens": 123456}}, "workspace": {"work": str(self.root / "work")}}
        with closing(sqlite3.connect(self.database)) as db, db:
            db.executescript(script)
            db.executemany("INSERT INTO metadata VALUES(?,?)", [("protocol_version", "2.0"), ("schema_sha256", LATEST_V2_SCHEMA)])
            db.execute("INSERT INTO projects VALUES('p','Project',NULL,0,1)")
            db.execute("INSERT INTO sessions(id,project_id,name,created_at) VALUES('s','p','Session',1)")
            db.execute("INSERT INTO agent_definitions VALUES('worker','p',?,1,1)", (json.dumps(self.config),))
            for identity in ('i','j'):
                db.execute("INSERT INTO instances VALUES(?,'p','s','worker',?,'main',?,'{}',NULL,1)", (identity, identity, json.dumps(self.config)))
            db.execute("INSERT INTO budgets(id,project_id,root_run_id,policy_json,created_at) VALUES('b','p','r',?,1)", (json.dumps({"max_input_tokens": 123456}),))
            db.execute("INSERT INTO runs(id,instance_id,root_run_id,budget_id,state,input_json,idempotency_key,input_hash,model_role,created_at,updated_at,config_json) VALUES('r','i','r','b','running','\"Original goal\"','r','hash','main',1,1,?)", (json.dumps(self.config),))
            db.execute("UPDATE instances SET active_run_id='r' WHERE id='i'")
            db.execute("INSERT INTO actions(id,run_id,operation_key,name,input_hash,input_json,side_effects,state,created_at,updated_at) VALUES('a','r','a','run_command','hash','{}','local','dispatched',1,1)")
            db.execute("INSERT INTO usage(id,budget_id,run_id,action_id,purpose,kind,state,input_tokens,output_tokens,created_at) VALUES('u','b','r','a','model','model','reserved',90,10,1)")
            db.execute("INSERT INTO messages(id,project_id,session_id,source_json,purpose,body,idempotency_key,input_hash,created_at) VALUES('m','p','s',?,'request','Saved body with wrong business arguments','m','hash',1)", (json.dumps({"kind":"controller","name":"Saved name"}),))
            db.execute("INSERT INTO deliveries(message_id,instance_id,run_id,accepted_at) VALUES('m','i','r',1)")
            db.execute("INSERT INTO read_positions VALUES('reader','s',1,1)")
            db.execute("INSERT INTO conversation(id,instance_id,run_id,role,body_json,created_at) VALUES('c1','i','r','user','{\"role\":\"user\",\"content\":\"Earlier fact\"}',1)")
            db.execute("INSERT INTO memories VALUES('memory','i','r',1,'[]','Retained stage memory',1)")
            db.execute("INSERT INTO conversation(id,instance_id,run_id,role,body_json,created_at) VALUES('c2','i','r','assistant',?,2)", (json.dumps({"role":"assistant","content":"Partial progress","tool_calls":[{"id":"pending","type":"function","function":{"name":"run_command","arguments":"{}"}}]}),))
        (self.root / "graphs.sqlite").write_bytes(b"archived checkpoint fixture")
        (self.root / "work").mkdir()
        (self.root / "work/partial.txt").write_text("Completed partial evidence", encoding="utf-8")
        SecretVault(self.root).set("example", "fixture-secret")
        (self.root / "client-token").write_text("earlier-browser-credential", encoding="utf-8")

    def snapshot(self):
        return {path.relative_to(self.root).as_posix(): path.read_bytes() for path in self.root.rglob('*') if path.is_file()}

    def test_dry_run_does_not_mutate_source_or_create_target(self):
        original = self.snapshot()
        report = upgrade_state(self.root, target=self.target)
        self.assertFalse(report["applied"])
        self.assertTrue(report["ready_for_apply"])
        self.assertFalse(self.target.exists())
        self.assertEqual(self.snapshot(), original)
        self.assertEqual(report["review_runs"], ["r"])
        self.assertEqual(report["usage_totals"], [1,90,10,None])

    def test_conversion_preserves_usage_secrets_history_and_private_readers(self):
        original = self.snapshot()
        report = upgrade_state(self.root, target=self.target, apply=True)
        self.assertTrue(report["applied"])
        self.assertEqual(self.snapshot(), original)
        self.assertEqual((self.target / "imported/protocol-2/oea.sqlite").read_bytes(), original["oea.sqlite"])
        self.assertNotEqual((self.target / "client-token").read_bytes(), original["client-token"])
        self.assertEqual((self.target / "imported/protocol-2/graphs.sqlite").read_bytes(), original["graphs.sqlite"])
        self.assertFalse((self.target / "graphs.sqlite").exists())
        self.assertEqual((self.target / "work/partial.txt").read_text(), "Completed partial evidence")
        self.assertEqual(SecretVault(self.target).get("example"), "fixture-secret")
        with closing(sqlite3.connect(self.target / 'oea.sqlite')) as db:
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(db.execute("SELECT value FROM metadata WHERE key='protocol_version'").fetchone()[0], '3.0')
            config = json.loads(db.execute("SELECT config_json FROM agent_definitions").fetchone()[0])
            self.assertEqual(config['tools']['local'], ['exec_command','write_stdin','apply_patch'])
            self.assertEqual(config['manager']['tools']['local'], [])
            self.assertEqual(config['execution']['budget']['max_total_input_tokens'],123456)
            self.assertIsNone(config['execution']['budget']['max_uncached_input_tokens'])
            self.assertEqual(config['workspace']['work'], str(self.target / 'work'))
            self.assertEqual(db.execute("SELECT state,execution_version FROM runs").fetchone(), ('needs_review','2.0'))
            self.assertEqual(db.execute("SELECT state,name FROM actions").fetchone(), ('needs_review','run_command'))
            self.assertEqual(db.execute("SELECT state,input_tokens,output_tokens,cached_input_tokens FROM usage").fetchone(), ('needs_review',90,10,None))
            self.assertEqual(db.execute("SELECT cost_micros,cost_state FROM usage_measurements").fetchone(), (None,'unknown'))
            memory = db.execute("SELECT body FROM memories ORDER BY rowid DESC LIMIT 1").fetchone()[0]
            self.assertIn('Retained stage memory',memory)
            self.assertIn('c2',memory)
            self.assertEqual(db.execute("SELECT body FROM messages").fetchone()[0], 'Saved body with wrong business arguments')
            board = db.execute("SELECT board_id FROM messages").fetchone()[0]
            self.assertEqual(db.execute("SELECT instance_id FROM board_readers WHERE board_id=?",(board,)).fetchall(),[('i',)])
            self.assertEqual(db.execute("SELECT position FROM board_read_positions WHERE board_id=?",(board,)).fetchone()[0],1)

    def test_new_host_never_replays_old_checkpoint_and_new_work_remains_possible(self):
        upgrade_state(self.root, target=self.target, apply=True)
        class Fixture:
            calls = 0
            async def complete(inner,*args,**options):
                inner.calls += 1
                return Completion(text='New work only',usage={'prompt_tokens':40,'completion_tokens':5})
        async def verify():
            model = Fixture()
            async with Host(self.target,model_api=model) as host:
                self.assertEqual(model.calls,0)
                self.assertEqual((await host.runs.get('r'))['state'],'needs_review')
                with self.assertRaises(RuntimeFault) as denied:
                    await host.runs.resume('r','resume')
                self.assertEqual(denied.exception.code,'EXECUTION_VERSION_UNSUPPORTED')
                fresh = await host.runs.submit(RunSpec(instance_id='i',input='New independent work',idempotency_key='new'))
                self.assertEqual((await host.runs.result(fresh.id,timeout=10)).text,'New work only')
                self.assertEqual(model.calls,1)
                self.assertEqual((await host.budgets.get('b'))['needs_review']['input_tokens'],90)
        asyncio.run(verify())

    def test_active_source_existing_target_and_unknown_schema_are_rejected(self):
        lock=WorkspaceLock(self.root)
        lock.acquire()
        try:
            with self.assertRaises(RuntimeFault) as error:upgrade_state(self.root)
            self.assertEqual(error.exception.code,'MIGRATION_SOURCE_ACTIVE')
        finally:lock.release()
        self.target.mkdir()
        with self.assertRaises(RuntimeFault):upgrade_state(self.root,target=self.target,apply=True)
        self.assertEqual(list(self.target.iterdir()),[])
        with closing(sqlite3.connect(self.database)) as db,db:
            db.execute("UPDATE metadata SET value='unknown' WHERE key='schema_sha256'")
        with self.assertRaises(RuntimeFault):upgrade_state(self.root)

    def test_changed_source_prevents_publishing_target(self):
        import shutil
        copy=shutil.copyfile
        changed=False
        def copying(source,target,*args,**kwargs):
            nonlocal changed
            result=copy(source,target,*args,**kwargs)
            if not changed:
                changed=True
                (self.root/'unexpected.txt').write_text('Appeared during migration')
            return result
        with patch('oea.migration.upgrade.shutil.copyfile',side_effect=copying),self.assertRaises(RuntimeFault) as failure:
            upgrade_state(self.root,target=self.target,apply=True)
        self.assertEqual(failure.exception.code,'MIGRATION_SOURCE_CHANGED')
        with closing(sqlite3.connect(self.target/'oea.sqlite')) as db:
            self.assertEqual(db.execute('SELECT * FROM metadata').fetchall(),[])
