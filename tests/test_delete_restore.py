"""Deletion is confirmed, recoverable and never removes project files."""
import asyncio
import tempfile
import unittest
from pathlib import Path

import httpx
from ma_runtime import AgentConfig, Runtime
from ma_runtime.errors import RuntimeFault
from ma_runtime.web.app import create_app


class DeleteRestoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ma-delete-tests-")
        self.root = Path(self.temp.name)
        self.folder = self.root / "files"
        self.folder.mkdir()
        self.keep = self.folder / "keep.txt"
        self.keep.write_text("keep local files", encoding="utf-8")
        self.rt = await Runtime.open(self.root / "state").__aenter__()
        await self.rt.projects.update("default", name="Disposable project", local_folder=str(self.folder))
        await self.rt.agents.configure(AgentConfig(id="worker", name="Worker", kind="mock"),
                                       api_key="synthetic-test-key")
        self.session = await self.rt.sessions.create(title="Disposable session")
        self.thread = await self.rt.threads.create(self.session.id, "worker")

    async def asyncTearDown(self):
        await self.rt.aclose()
        self.temp.cleanup()

    async def task(self, **mock):
        return await self.rt.tasks.submit({
            "session_id": self.session.id, "thread_id": self.thread.id, "agent_id": "worker",
            "payload": {"input": "synthetic", "_mock": mock}, "idempotency_key": "test"})

    async def state(self, task, expected):
        async with asyncio.timeout(5):
            while (await self.rt.tasks.get(task.id)).state != expected:
                await asyncio.sleep(.01)

    async def test_confirmation_is_required_and_cancel_has_no_effect(self):
        for fn, identity in ((self.rt.projects.delete, "default"), (self.rt.sessions.delete, self.session.id)):
            for confirmation in (False, None, "true", 1):
                with self.subTest(confirmation=confirmation), self.assertRaises(RuntimeFault) as error:
                    await fn(identity, confirmed=confirmation)
                self.assertEqual(error.exception.code, "CONFIRMATION_REQUIRED")
        self.assertEqual((await self.rt.projects.get("default")).state, "OPEN")
        self.assertEqual((await self.rt.sessions.get(self.session.id)).state, "OPEN")

    async def test_session_delete_hides_it_and_restore_keeps_history_and_artifact(self):
        task = await self.task(delay=0)
        await self.rt.tasks.result(task.id, timeout=5)
        artifact = await self.rt.artifacts.put_bytes(self.session.id, "sample.md", b"retained output")
        before = await self.rt.threads.conversation(self.thread.id)
        await self.rt.sessions.delete(self.session.id, confirmed=True)
        self.assertEqual((await self.rt.sessions.list()).items, [])
        self.assertEqual((await self.rt.sessions.list(deleted_only=True)).items[0].id, self.session.id)
        self.assertEqual(len(await self.rt.agents.list()), 1)
        with self.assertRaises(RuntimeFault):
            await self.rt.threads.turn(self.thread.id, "must not run", "after-delete")
        with self.assertRaises(RuntimeFault):
            await self.rt.artifacts.put_bytes(self.session.id, "bad.txt", b"not allowed")
        await self.rt.sessions.restore(self.session.id)
        after = await self.rt.threads.conversation(self.thread.id)
        self.assertEqual(before, after)
        self.assertEqual((await self.rt.artifacts.get(artifact.id)).name, "sample.md")
        self.assertEqual(self.keep.read_text(encoding="utf-8"), "keep local files")

    async def test_project_delete_cascades_and_restore_keeps_agent_key_and_prior_states(self):
        closed = await self.rt.sessions.create(title="Closed")
        await self.rt.sessions.close(closed.id)
        separate = await self.rt.sessions.create(title="Previously deleted")
        await self.rt.sessions.delete(separate.id, confirmed=True)
        await self.rt.projects.delete("default", confirmed=True)
        self.assertEqual(await self.rt.projects.list(), [])
        self.assertEqual(await self.rt.agents.list(), [])
        self.assertEqual((await self.rt.sessions.list()).items, [])
        self.assertEqual((await self.rt.sessions.list(deleted_only=True)).items, [])
        self.assertEqual(self.rt.vault.get("worker"), "synthetic-test-key")
        self.assertTrue(self.keep.is_file())
        with self.assertRaises(RuntimeFault):
            await self.rt.sessions.restore(self.session.id)
        with self.assertRaises(RuntimeFault):
            await self.rt.projects.archive("default", False)
        await self.rt.projects.restore("default")
        self.assertEqual((await self.rt.sessions.get(self.session.id)).state, "OPEN")
        self.assertEqual((await self.rt.sessions.get(closed.id)).state, "CLOSED")
        self.assertEqual((await self.rt.sessions.get(separate.id)).state, "DELETED")
        self.assertTrue((await self.rt.agents.describe("worker"))["has_api_key"])
        self.assertEqual(len((await self.rt.sessions.list()).items), 2)

    async def test_repeated_delete_and_restore_are_idempotent(self):
        for _ in range(2):
            await self.rt.projects.delete("default", confirmed=True)
        for _ in range(2):
            await self.rt.projects.restore("default")
        for _ in range(2):
            await self.rt.sessions.delete(self.session.id, confirmed=True)
        for _ in range(2):
            await self.rt.sessions.restore(self.session.id)
        self.assertEqual((await self.rt.sessions.get(self.session.id)).state, "OPEN")

    async def test_running_work_blocks_session_and_project_deletion(self):
        task = await self.task(delay=30)
        await self.state(task, "RUNNING")
        for fn, identity in ((self.rt.projects.delete, "default"), (self.rt.sessions.delete, self.session.id)):
            with self.assertRaises(RuntimeFault) as error:
                await fn(identity, confirmed=True)
            self.assertEqual(error.exception.code, "RESOURCE_BUSY")
        await self.rt.tasks.cancel(task.id)
        await self.state(task, "CANCELLED")

    async def test_waiting_and_uncertain_work_block_deletion(self):
        task = await self.task(delay=0, wait_for_input=True)
        await self.state(task, "WAITING")
        with self.assertRaises(RuntimeFault):
            await self.rt.sessions.delete(self.session.id, confirmed=True)
        # Represent an unresolved external outcome in this isolated fixture.
        await self.rt.store.call(lambda db: db.execute("UPDATE tasks SET state='UNCERTAIN' WHERE id=?", (task.id,)),
                                 transaction=True)
        with self.assertRaises(RuntimeFault):
            await self.rt.projects.delete("default", confirmed=True)
        self.assertEqual((await self.rt.projects.get("default")).state, "OPEN")
        await self.rt.store.call(lambda db: db.execute("UPDATE tasks SET state='WAITING' WHERE id=?", (task.id,)),
                                 transaction=True)
        await self.rt.tasks.cancel(task.id)

    async def test_archived_project_recovers_as_archived(self):
        await self.rt.projects.archive("default")
        await self.rt.projects.delete("default", confirmed=True)
        restored = await self.rt.projects.restore("default")
        self.assertEqual(restored.state, "ARCHIVED")

    async def test_http_delete_confirmation_trash_and_restore(self):
        app = create_app(runtime=self.rt)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(base_url="http://127.0.0.1", transport=httpx.ASGITransport(app=app),
                headers={"Authorization": "Bearer " + app.state.token}) as client:
                path = f"/api/sessions/{self.session.id}"
                denied = await client.request("DELETE", path, json={})
                self.assertEqual(denied.json()["error"]["code"], "CONFIRMATION_REQUIRED")
                self.assertEqual((await client.request("DELETE", path, json={"confirmed": True})).status_code, 200)
                trash = (await client.get("/api/trash")).json()
                self.assertEqual(trash["sessions"]["items"][0]["id"], self.session.id)
                self.assertEqual((await client.post(path + "/restore", json={})).status_code, 200)
                self.assertEqual((await client.request("DELETE", "/api/projects/default",
                    json={"confirmed": True})).status_code, 200)
                trash = (await client.get("/api/trash")).json()
                self.assertEqual(trash["projects"][0]["id"], "default")
                self.assertFalse(trash["sessions"]["items"])
                self.assertEqual((await client.post("/api/projects/default/restore", json={})).status_code, 200)
                self.assertEqual((await client.get("/api/trash")).json()["projects"], [])
