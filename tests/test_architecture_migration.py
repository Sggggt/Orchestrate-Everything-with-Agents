"""Recognized earlier schema is copied offline; no prior position is replayed."""

import asyncio
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from oea import AgentDefinition, Completion, Model, RunSpec
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
            async def create():
                async with Host(root, model_api=Fixture()) as host:
                    await host.projects.create("Example", project_id="p")
                    await host.agents.save(AgentDefinition(id="worker", name="Worker", project_id="p", model=Model(base_url="http://model.invalid", model="test")))
                    session = await host.sessions.create("p")
                    instance = await host.instances.create(session["id"], "worker")
                    run = await host.runs.submit(RunSpec(instance_id=instance["id"], input="Save evidence", idempotency_key="original"))
                    await host.runs.result(run.id, timeout=10)
                    return run.id, instance["id"]
            run_id, instance_id = asyncio.run(create())
            with closing(sqlite3.connect(root / "oea.sqlite")) as db, db:
                for table in ("architecture_steps", "architecture_versions", "agent_architectures"):
                    db.execute("DROP TABLE " + table)
                db.execute("UPDATE metadata SET value=? WHERE key='schema_sha256'", (INITIAL_V3_SCHEMA,))
                db.execute("UPDATE runs SET state='waiting' WHERE id=?", (run_id,))
                db.execute("UPDATE instances SET active_run_id=? WHERE id=?", (run_id, instance_id))
                db.execute("UPDATE actions SET state='needs_review' WHERE run_id=?", (run_id,))
            original = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
            report = upgrade_state(root, target=target)
            self.assertFalse(report["applied"])
            self.assertFalse(target.exists())
            report = upgrade_state(root, target=target, apply=True)
            self.assertEqual(report["review_runs"], [run_id])
            self.assertEqual(original, {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()})
            self.assertEqual((target / "imported/protocol-3-initial/graphs.sqlite").read_bytes(), original[Path("graphs.sqlite")])
            self.assertFalse((target / "graphs.sqlite").exists())
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
