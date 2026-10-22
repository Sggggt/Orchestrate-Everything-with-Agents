"""Logical channel generations and receipt evidence over managed execution."""

import asyncio
import tempfile
import unittest
import json
import sys
from pathlib import Path
from unittest.mock import patch

from oea import (AdapterEvent, AgentDescriptor, CheckpointProposal, Runtime, RuntimeFault,
                 TaskResult, TaskSpec, MessageSpec)
from ma_runtime.models import CommandEnvelope, now_ms
from ma_runtime.commands import CommandBus
from ma_runtime.store import one


class ChannelWorker:
    def __init__(self):
        self.ready = asyncio.Queue()
        self.release = asyncio.Event()
        self.apply = ()

    def describe(self):
        return AgentDescriptor(agent_id="worker", checkpoint_mode="opaque_json", max_concurrency=4)

    async def execute(self, request, context):
        self.ready.put_nowait((request, context))
        await self.release.wait()
        yield AdapterEvent(type="completed", result=TaskResult(output={"done": True}),
                           checkpoint=CheckpointProposal(ref={"saved": True}), applied_message_ids=self.apply)

    async def aclose(self):
        self.release.set()


class ChannelTests(unittest.IsolatedAsyncioTestCase):
    async def test_jsonl_reconnection_replays_receipts_and_rejects_delayed_old_generation(self):
        from oea import AgentConfig
        with tempfile.TemporaryDirectory() as temp:
            async with Runtime.open(temp) as rt:
                descriptor = AgentDescriptor(agent_id="channel-worker", execution_mode="subprocess",
                    cancellation="terminate_owned_process", checkpoint_mode="opaque_json")
                await rt.agents.configure(AgentConfig(id="channel-worker", name="Channel worker", kind="process",
                    command=(sys.executable, "-B", str(Path(__file__).parent / "fixtures/channel_agent.py")),
                    process_descriptor=descriptor))
                session = await rt.sessions.create()
                thread = await rt.threads.create(session.id, "channel-worker")
                messages = [await rt.messages.send(MessageSpec(session_id=session.id, target_thread_id=thread.id,
                    idempotency_key=str(index))) for index in range(2)]
                task = await rt.tasks.submit(TaskSpec(session_id=session.id, thread_id=thread.id,
                    agent_id="channel-worker", idempotency_key="run"))
                result = (await rt.tasks.result(task.id, timeout=10)).output
                self.assertEqual(result["gap"], "ACK_GAP")
                self.assertEqual(result["stale"], "STALE_CHANNEL")
                self.assertEqual(result["generation"], result["original_generation"] + 1)
                self.assertEqual(result["replay_generation"], result["generation"])
                self.assertEqual(result["remaining"], 2)
                self.assertFalse(result["receipt"]["application_confirmed"])
                self.assertEqual((await rt.tasks.get(task.id)).attempt_count, 1)
                for message in messages:
                    self.assertEqual((await rt.messages.delivery(message.id))["state"], "APPLIED")

    async def test_transaction_rollback_and_lost_wakeup_recover_from_durable_events(self):
        with tempfile.TemporaryDirectory() as temp:
            async with Runtime.open(temp, {"subscriber_buffer_size": 8}, adapters={"worker": ChannelWorker()}) as rt:
                session = await rt.sessions.create()
                thread = await rt.threads.create(session.id, "worker")
                spec = MessageSpec(session_id=session.id, target_thread_id=thread.id, idempotency_key="message")
                def fail_before_commit(db):
                    rt.messages.insert(db, spec)
                    raise RuntimeFault("INJECTED_FAILURE", "Before commit")
                with self.assertRaises(RuntimeFault):
                    await rt.store.call(fail_before_commit, transaction=True)
                self.assertEqual(await rt.store.call(lambda db: one(db, "SELECT COUNT(*) AS n FROM messages").n), 0)
                self.assertEqual(await rt.store.call(lambda db: one(db, "SELECT COUNT(*) AS n FROM message_delivery").n), 0)
                cursor = (await rt.events.list(session.id))["cursor"]
                streams = [rt.events.subscribe(session.id, cursor) for _ in range(2)]
                initial = [asyncio.create_task(anext(stream)) for stream in streams]
                for _ in range(20):
                    if len(rt.events.subscribers) == 2:
                        break
                    await asyncio.sleep(.005)
                for _ in range(24):
                    rt.events.progress(session.id, thread.id, "task", "run", "run.progress", {"delta": "progress"})
                with patch.object(rt, "notify", side_effect=ConnectionError("Wakeup lost")):
                    with self.assertRaises(ConnectionError):
                        await rt.messages.send(spec)
                recovered = await rt.messages.send(spec)
                self.assertTrue(recovered.reused)
                for stream, first in zip(streams, initial):
                    observed = [(await first)["type"]]
                    for _ in range(5):
                        if {"message.accepted", "stream.resync"} <= set(observed):
                            break
                        event = await asyncio.wait_for(anext(stream), 2)
                        observed.append(event["type"])
                    self.assertIn("message.accepted", observed)
                    self.assertIn("stream.resync", observed)
                    await stream.aclose()
                self.assertEqual(await rt.store.call(lambda db: one(db, "SELECT COUNT(*) AS n FROM messages").n), 1)
                self.assertEqual(await rt.store.call(lambda db: one(db, "SELECT COUNT(*) AS n FROM events WHERE type='message.accepted'").n), 1)

    async def test_contiguous_transport_receipts_do_not_apply_messages_and_stale_writer_is_fenced(self):
        with tempfile.TemporaryDirectory() as temp:
            worker = ChannelWorker()
            async with Runtime.open(temp, adapters={"worker": worker}) as rt:
                session = await rt.sessions.create()
                thread = await rt.threads.create(session.id, "worker")
                specs = [MessageSpec(session_id=session.id, target_thread_id=thread.id, payload={"n": index}, idempotency_key=str(index)) for index in range(3)]
                messages = [await rt.messages.send(spec) for spec in specs]
                self.assertEqual((await rt.messages.send(specs[0])).id, messages[0].id)
                delivered = await rt.messages.delivery(messages[0].id)
                self.assertEqual(delivered["accepted_at"], delivered["delivered_at"])
                self.assertEqual(delivered["state"], "DELIVERED")
                handle = await rt.tasks.submit(TaskSpec(session_id=session.id, thread_id=thread.id, agent_id="worker", idempotency_key="run"))
                request, context = await asyncio.wait_for(worker.ready.get(), 3)
                binding = await rt.channels.bind(request.run_id, thread.id, "connection-one", capabilities=("inbox", "receipts", "generation"))
                duplicate = await rt.channels.bind(request.run_id, thread.id, "connection-one", capabilities=("inbox", "receipts", "generation"))
                self.assertEqual(binding["generation"], duplicate["generation"])
                inbox = await rt.channels.inbox(binding)
                positions = [item["position"] for item in inbox["items"]]
                with self.assertRaises(RuntimeFault) as gap:
                    await rt.channels.acknowledge(binding, [positions[1]])
                self.assertEqual(gap.exception.code, "ACK_GAP")
                ack = await rt.channels.acknowledge(binding, positions[:2])
                self.assertFalse(ack["application_confirmed"])
                self.assertEqual((await rt.messages.delivery(messages[0].id))["state"], "OFFERED")
                with rt.channels.use(binding):
                    with self.assertRaises(RuntimeFault) as busy:
                        await rt.channels.bind(request.run_id, thread.id, "connection-two", expected_generation=binding["generation"])
                    self.assertEqual(busy.exception.code, "CHANNEL_BUSY")
                resumed = await rt.channels.bind(request.run_id, thread.id, "connection-two", expected_generation=binding["generation"])
                self.assertEqual(resumed["generation"], binding["generation"] + 1)
                self.assertEqual(resumed["received_position"], positions[1])
                with self.assertRaises(RuntimeFault) as stale:
                    with rt.channels.use(binding):
                        self.fail("Old connection admitted")
                self.assertEqual(stale.exception.code, "STALE_CHANNEL")
                self.assertEqual(len((await rt.channels.inbox(resumed))["items"]), 3)
                self.assertEqual(len(rt.active), 1)
                other = await rt.threads.create(session.id, "worker")
                with self.assertRaises(RuntimeFault):
                    await rt.channels.bind(request.run_id, other.id, "wrong-thread")
                foreign = await rt.messages.send(MessageSpec(session_id=session.id, target_thread_id=other.id, idempotency_key="foreign"))
                with self.assertRaises(RuntimeFault):
                    await context.reject_message(foreign.id, "Not mine", "decline")
                worker.apply = tuple(item.id for item in messages)
                worker.release.set()
                await rt.tasks.result(handle.id, timeout=5)
                for message in messages:
                    receipt = await rt.messages.delivery(message.id)
                    self.assertEqual(receipt["state"], "APPLIED")
                    self.assertEqual(receipt["boundary_run_id"], request.run_id)
                    self.assertIsNotNone(receipt["applied_checkpoint_id"])
                with self.assertRaises(RuntimeFault):
                    await rt.messages.expire(messages[0].id, now_ms(), "Too late", "late")

    async def test_rejected_and_expired_messages_preserve_legacy_shape_but_never_enter_execution(self):
        with tempfile.TemporaryDirectory() as temp:
            worker = ChannelWorker()
            async with Runtime.open(temp, adapters={"worker": worker}) as rt:
                session = await rt.sessions.create()
                thread = await rt.threads.create(session.id, "worker")
                first = await rt.messages.send(MessageSpec(session_id=session.id, target_thread_id=thread.id, idempotency_key="first"))
                second = await rt.messages.send(MessageSpec(session_id=session.id, target_thread_id=thread.id, idempotency_key="second"))
                declined = await rt.messages.reject(first.id, "Wrong destination", "decline")
                self.assertEqual(declined["state"], "REJECTED")
                self.assertEqual(declined, await rt.messages.reject(first.id, "Wrong destination", "decline"))
                expired = await rt.messages.expire(second.id, now_ms(), "Deadline elapsed", "expire")
                self.assertEqual(expired["state"], "EXPIRED")
                self.assertEqual((await rt.messages.get(first.id)).state, "PENDING")
                presentation = await rt.sessions.snapshot(session.id, presentation=True)
                self.assertEqual({item["state"] for item in presentation["messages"]}, {"REJECTED", "EXPIRED"})
                handle = await rt.tasks.submit(TaskSpec(session_id=session.id, thread_id=thread.id, agent_id="worker", idempotency_key="run"))
                request, _ = await asyncio.wait_for(worker.ready.get(), 3)
                self.assertFalse(request.messages)
                worker.release.set()
                await rt.tasks.result(handle.id, timeout=5)

    async def test_committed_boundary_keeps_unapplied_messages_available_to_the_next_turn(self):
        with tempfile.TemporaryDirectory() as temp:
            worker = ChannelWorker()
            async with Runtime.open(temp, adapters={"worker": worker}) as rt:
                session = await rt.sessions.create()
                thread = await rt.threads.create(session.id, "worker")
                messages = [await rt.messages.send(MessageSpec(session_id=session.id, target_thread_id=thread.id,
                    idempotency_key=str(index))) for index in range(2)]
                first = await rt.tasks.submit(TaskSpec(session_id=session.id, thread_id=thread.id,
                    agent_id="worker", idempotency_key="first"))
                request, _ = await asyncio.wait_for(worker.ready.get(), 3)
                worker.release.set()
                await rt.tasks.result(first.id, timeout=5)
                self.assertEqual((await rt.messages.delivery(messages[0].id))["state"], "DELIVERED")
                # Older hosts retained OFFERED after a committed, unacknowledged boundary.
                await rt.store.call(lambda db: db.execute(
                    "UPDATE messages SET state='OFFERED',target_run_id=? WHERE id=?",
                    (request.run_id, messages[1].id)), transaction=True)
                worker.apply = tuple(message.id for message in messages)
                second = await rt.tasks.submit(TaskSpec(session_id=session.id, thread_id=thread.id,
                    agent_id="worker", idempotency_key="second"))
                second_request, _ = await asyncio.wait_for(worker.ready.get(), 3)
                self.assertEqual({message["message_id"] for message in second_request.messages}, set(worker.apply))
                await rt.tasks.result(second.id, timeout=5)
                for message in messages:
                    delivery = await rt.messages.delivery(message.id)
                    self.assertEqual(delivery["state"], "APPLIED")
                    self.assertEqual(delivery["boundary_run_id"], second_request.run_id)

    async def test_single_command_handler_and_response_loss_reuses_domain_receipt(self):
        with tempfile.TemporaryDirectory() as temp:
            async with Runtime.open(temp) as rt:
                await rt.resources.configure({"key": "request", "limit": 1})
                spec = {"owner": "client", "scope": "session", "phase": "operation", "idempotency_key": "request", "quantities": [{"key": "request"}]}
                lost = True
                async def handler(spec):
                    nonlocal lost
                    result = await rt.resources.request(spec)
                    if lost:
                        lost = False
                        raise ConnectionError("Response lost after commit")
                    return result
                bus = CommandBus({"resource.request": handler})
                with self.assertRaises(RuntimeFault):
                    bus.register("resource.request", handler)
                command = CommandEnvelope(method="resource.request", params={"spec": spec}, correlation_id="correlation")
                with self.assertRaises(ConnectionError):
                    await bus.execute(command)
                result = await bus.execute(command)
                self.assertTrue(result["reused"])
                self.assertEqual(await rt.store.call(lambda db: one(db, "SELECT COUNT(*) AS n FROM resource_leases").n), 1)
                self.assertEqual(await rt.store.call(lambda db: one(db, "SELECT COUNT(*) AS n FROM events WHERE type='resource.requested'").n), 1)
                payload = await rt.store.call(lambda db: one(db, "SELECT payload_json FROM events WHERE type='resource.requested'").payload_json)
                self.assertEqual(json.loads(payload)["command"]["correlation_id"], "correlation")
