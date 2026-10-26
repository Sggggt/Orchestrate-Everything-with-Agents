"""Durable v2 facts preserve transactions and reject legacy state in place."""

import tempfile
import unittest
from pathlib import Path

from oea.contracts.errors import RuntimeFault
from oea.storage.persistence import Store, event, one, rows
from oea.interfaces.registry import OperationRegistry


class PersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="oea-v2-state-")
        self.root = Path(self.temporary.name)
        self.store = Store(self.root)
        await self.store.open()

    async def asyncTearDown(self):
        await self.store.close()
        self.temporary.cleanup()

    async def test_durable_fact_and_event_commit_together(self):
        registry = OperationRegistry()

        async def create(name):
            def write(db):
                db.execute("INSERT INTO projects VALUES('project',?,NULL,0,1)", (name,))
                event(db, "project.created", project_id="project")
            await self.store.call(write, transaction=True)
            return name

        registry.register("projects.create", create)
        self.assertEqual(await registry.call("projects.create", {"name": "Project"}, correlation_id="request"), "Project")
        await self.store.close()
        self.store = Store(self.root)
        await self.store.open()
        self.assertEqual((await self.store.call(lambda db: one(db, "SELECT * FROM projects")))["name"], "Project")
        saved = await self.store.call(lambda db: one(db, "SELECT * FROM events"))
        self.assertIn('"correlation_id":"request"', saved["data_json"])

    async def test_exception_rolls_back_both_fact_and_event(self):
        def fail(db):
            db.execute("INSERT INTO projects VALUES('project','Project',NULL,0,1)")
            event(db, "project.created", project_id="project")
            raise ValueError("Abort transaction")
        with self.assertRaises(ValueError):
            await self.store.call(fail, transaction=True)
        self.assertEqual(await self.store.call(lambda db: rows(db, "SELECT * FROM projects")), [])
        self.assertEqual(await self.store.call(lambda db: rows(db, "SELECT * FROM events")), [])

    async def test_new_state_has_no_resource_or_lease_tables(self):
        tables = await self.store.call(lambda db: rows(db, "SELECT name FROM sqlite_master WHERE type='table'"))
        self.assertFalse(any("resource" in table["name"] or "lease" in table["name"] for table in tables))

    async def test_legacy_source_is_left_untouched(self):
        with tempfile.TemporaryDirectory(prefix="oea-v2-legacy-") as directory:
            root = Path(directory)
            original = root / "runtime.sqlite"
            original.write_bytes(b"legacy database marker")
            store = Store(root)
            with self.assertRaises(RuntimeFault) as caught:
                await store.open()
            self.assertEqual(caught.exception.code, "MIGRATION_REQUIRED")
            self.assertEqual(original.read_bytes(), b"legacy database marker")
            self.assertFalse((root / "oea.sqlite").exists())

    async def test_registry_rejects_unknown_parameters_before_handler(self):
        registry = OperationRegistry()
        invoked = []

        async def create(name):
            invoked.append(name)

        registry.register("projects.create", create)
        with self.assertRaises(RuntimeFault):
            await registry.call("projects.create", {"name": "Project", "extra": True})
        self.assertEqual(invoked, [])
