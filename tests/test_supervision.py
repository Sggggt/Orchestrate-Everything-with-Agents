"""Read markers persist independently of execution delivery and guidance."""

import tempfile
import unittest
from pathlib import Path

from oea import AgentConfig, MessageSpec, Runtime, RuntimeFault


class SupervisionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="supervision-")
        self.root = Path(self.temp.name)
        self.rt = await Runtime.open(self.root).__aenter__()
        await self.rt.agents.configure(AgentConfig(id="worker", name="Worker", kind="mock"))
        self.session = await self.rt.sessions.create()
        self.threads = [await self.rt.threads.create(self.session.id, "worker") for _ in range(2)]

    async def asyncTearDown(self):
        await self.rt.aclose()
        self.temp.cleanup()

    async def message(self, thread, key):
        return await self.rt.messages.send(MessageSpec(session_id=self.session.id, target_thread_id=thread.id,
            payload={"text": key}, idempotency_key=key))

    async def test_exact_visible_ranges_leave_holes_and_other_instances_unread(self):
        first, second = self.threads
        handles = [await self.message(first, f"message-{index}") for index in range(3)]
        await self.message(second, "other")
        page = await self.rt.threads.activity(first.id)
        positions = [item["position"] for item in page.items]
        read = await self.rt.threads.mark_read(first.id, [positions[0], positions[2]])
        self.assertEqual(read["unread_count"], 1)
        self.assertEqual((await self.rt.threads.mark_read(first.id, [positions[0]]))["changed"], 0)
        view = {item["thread_id"]: item for item in (await self.rt.threads.instances(self.session.id)).items}
        self.assertEqual(view[first.id]["unread_count"], 1)
        self.assertEqual(view[second.id]["unread_count"], 1)
        self.assertEqual((await self.rt.messages.get(handles[0].id)).state, "PENDING")
        with self.assertRaises(RuntimeFault) as error:
            await self.rt.threads.mark_read(second.id, [positions[0]])
        self.assertEqual(error.exception.code, "PERMISSION_DENIED")
        await self.rt.aclose()
        self.rt = await Runtime.open(self.root).__aenter__()
        resumed = await self.rt.threads.activity(first.id)
        self.assertEqual([item["read"] for item in resumed.items], [True, False, True])
        snapshot = await self.rt.sessions.snapshot(self.session.id, presentation=True)
        self.assertEqual({item["thread_id"]: item["unread_count"] for item in snapshot["thread_activity"]},
                         {first.id: 1, second.id: 1})

    async def test_activity_pagination_and_idempotent_message_do_not_reorder(self):
        first, second = self.threads
        one = await self.message(first, "first")
        two = await self.message(second, "second")
        page = await self.rt.threads.instances(self.session.id, "worker", limit=1)
        self.assertEqual(page.items[0]["thread_id"], second.id)
        rest = await self.rt.threads.instances(self.session.id, "worker", cursor=page.cursor, limit=1)
        self.assertEqual(rest.items[0]["thread_id"], first.id)
        self.assertEqual((await self.message(first, "first")).id, one.id)
        self.assertEqual((await self.rt.threads.instances(self.session.id)).items[0]["thread_id"], second.id)
        newer = await self.message(first, "newer")
        self.assertNotEqual(newer.id, two.id)
        self.assertEqual((await self.rt.threads.instances(self.session.id)).items[0]["thread_id"], first.id)
        page = await self.rt.threads.activity(first.id, limit=1)
        older = await self.rt.threads.activity(first.id, cursor=page.cursor, limit=1)
        self.assertTrue(page.has_more)
        self.assertEqual(older.items[0]["source_id"], one.id)
        self.assertFalse(older.has_more)
