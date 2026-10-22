"""Resource capacity, transactional leases and actual occupied phase boundaries."""

import asyncio
import tempfile
import unittest
from pathlib import Path

from oea import Runtime, RuntimeConfig, RuntimeFault, QuantityRequirement, ResourceRequirement
from ma_runtime.resources import ResourceCoordinator


class ResourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_atomic_quantities_and_phase_cancellation(self):
        resources = ResourceCoordinator()
        resources.define("a", 2)
        resources.define("b", 1)
        q = QuantityRequirement
        self.assertTrue(resources.acquire("first", (), (q(key="a"), q(key="b"))))
        self.assertFalse(resources.acquire("second", (ResourceRequirement(key="file"),), (q(key="a"), q(key="b"))))
        self.assertNotIn("file", resources.holders)
        self.assertEqual(resources.quantities["a"], {"first": 1})
        self.assertTrue(resources.acquire("first", (), (q(key="a"), q(key="b"))))
        resources.isolate("first", "Unknown operation")
        resources.release("first")
        self.assertFalse(resources.can_acquire("second", (), (q(key="b"),)))
        entered = asyncio.Event()
        async def blocked():
            async with resources.phase("waiting", (q(key="b"),), timeout=2):
                entered.set()
        waiter = asyncio.create_task(blocked())
        await asyncio.sleep(0)
        self.assertFalse(entered.is_set())
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        self.assertFalse(resources.phase_waiters)
        resources.resolve("first")
        await blocked()
        self.assertTrue(entered.is_set())
        self.assertFalse(resources.quantities)

    async def test_lease_queue_expiry_and_recovery_do_not_free_unknown_occupancy(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = RuntimeConfig(max_concurrency=2, reserved_external_slots=0)
            async with Runtime.open(root, config=config) as rt:
                await rt.resources.configure({"key": "service", "unit": "request", "limit": 1})
                original = await rt.resources.inspect()
                self.assertTrue((await rt.resources.preflight([{"key": "service"}]))["admissible"])
                self.assertEqual(original, await rt.resources.inspect())
                spec = {"owner": "owner", "scope": "scope", "phase": "call", "idempotency_key": "one",
                        "quantities": [{"key": "service"}], "ttl_seconds": .04}
                first = await rt.resources.request(spec)
                again = await rt.resources.request(spec)
                self.assertEqual(first["id"], again["id"])
                self.assertTrue(again["reused"])
                await rt.resources.tick()
                self.assertEqual((await rt.resources.get(first["id"]))["state"], "GRANTED")
                second = await rt.resources.request({**spec, "idempotency_key": "two", "ttl_seconds": 10})
                await rt.resources.tick()
                self.assertEqual((await rt.resources.get(second["id"]))["state"], "QUEUED")
                with self.assertRaises(RuntimeFault) as conflict:
                    await rt.resources.request({**spec, "phase": "changed"})
                self.assertEqual(conflict.exception.code, "IDEMPOTENCY_CONFLICT")
                await asyncio.sleep(.05)
                await rt.resources.tick()
                self.assertEqual((await rt.resources.get(first["id"]))["state"], "ISOLATED")
                with self.assertRaises(RuntimeFault):
                    await rt.resources.release_lease(first["id"])
            async with Runtime.open(root, config=config) as rt:
                self.assertEqual((await rt.resources.get(first["id"]))["state"], "ISOLATED")
                self.assertFalse((await rt.resources.preflight([{"key": "service"}]))["admissible"])
                await rt.resources.release_lease(first["id"], confirmed_stopped=True, evidence="Owner stopped")
                await rt.resources.release_lease(first["id"])
                await rt.resources.tick()
                self.assertEqual((await rt.resources.get(second["id"]))["state"], "GRANTED")
            async with Runtime.open(root, config=config) as rt:
                self.assertEqual((await rt.resources.get(second["id"]))["state"], "ISOLATED")
