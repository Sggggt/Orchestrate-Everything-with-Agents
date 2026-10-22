"""Generation-fenced logical channels over the existing execution ownership."""

import json
from contextlib import contextmanager

from .errors import require
from .models import json_dump, new_id, now_ms
from .store import emit, one, rows


class ExecutionChannels:
    def __init__(self, store, *, check_run, delivery):
        self.store, self.check_run, self.delivery = store, check_run, delivery
        self.current = {}
        self.inflight = {}
        self.binding = set()

    async def open(self):
        await self.store.call(lambda db: db.execute("UPDATE execution_channels SET state='DISCONNECTED' WHERE state='CONNECTED'"), transaction=True)

    def view(self, row):
        value = dict(row)
        value["capabilities"] = json.loads(value.pop("capabilities_json"))
        return value

    async def get(self, thread_id):
        row = await self.store.call(lambda db: one(db, "SELECT * FROM execution_channels WHERE thread_id=?", (thread_id,)))
        return self.view(row) if row else None

    async def bind(self, run_id, thread_id, connection_id, *, protocol_version="1.0", capabilities=(), expected_generation=None):
        require(protocol_version == "1.0", "UNSUPPORTED_CAPABILITY", "Unsupported channel protocol")
        require(isinstance(connection_id, str) and 0 < len(connection_id) <= 256 and
                len(capabilities) <= 16 and set(capabilities) <= {"inbox", "receipts", "generation", "reconnect"} and
                (expected_generation is None or type(expected_generation) is int and expected_generation > 0),
                "INVALID_REQUEST", "Invalid channel identity or capabilities")
        require(thread_id not in self.binding and not self.inflight.get(thread_id),
                "CHANNEL_BUSY", "A channel cannot transfer while a callback is in flight")
        self.binding.add(thread_id)
        try:
            def commit(db):
                task, _ = self.check_run(db, run_id)
                require(task.thread_id == thread_id, "PERMISSION_DENIED", "Channel does not own this thread")
                prior = one(db, "SELECT * FROM execution_channels WHERE thread_id=?", (thread_id,))
                if prior and prior.run_id == run_id and prior.connection_id == connection_id and prior.state == "CONNECTED":
                    require(prior.protocol_version == protocol_version and json.loads(prior.capabilities_json) == list(capabilities),
                            "IDEMPOTENCY_CONFLICT", "Channel identity was already bound with different capabilities")
                    return self.view(prior)
                if prior and prior.run_id == run_id:
                    require(expected_generation == prior.generation, "STALE_CHANNEL", "Reconnect requires the current channel generation")
                identity = prior.id if prior else new_id("channel")
                generation = prior.generation + 1 if prior else 1
                db.execute("INSERT INTO execution_channels(id,thread_id,run_id,connection_id,generation,protocol_version,capabilities_json,state,updated_at) VALUES(?,?,?,?,?,?,?,'CONNECTED',?) "
                    "ON CONFLICT(thread_id) DO UPDATE SET run_id=excluded.run_id,connection_id=excluded.connection_id,generation=excluded.generation,protocol_version=excluded.protocol_version,capabilities_json=excluded.capabilities_json,state='CONNECTED',updated_at=excluded.updated_at",
                    (identity, thread_id, run_id, connection_id, generation, protocol_version, json_dump(capabilities), now_ms()))
                emit(db, "channel.bound", session_id=task.session_id, thread_id=thread_id, task_id=task.id, run_id=run_id,
                     payload={"channel_id": identity, "generation": generation, "connection_id": connection_id})
                return self.view(one(db, "SELECT * FROM execution_channels WHERE id=?", (identity,)))
            value = await self.store.call(commit, transaction=True)
            self.current[thread_id] = value
            return dict(value)
        finally:
            self.binding.discard(thread_id)

    @contextmanager
    def use(self, identity):
        require(isinstance(identity, dict), "STALE_CHANNEL", "Channel identity is required")
        thread_id = identity.get("thread_id")
        current = self.current.get(thread_id)
        require(current and thread_id not in self.binding and current["state"] == "CONNECTED" and
                all(identity.get(key) == current[key] for key in ("id", "thread_id", "run_id", "connection_id", "generation")),
                "STALE_CHANNEL", "Channel identity or generation no longer has write authority")
        self.inflight[thread_id] = self.inflight.get(thread_id, 0) + 1
        try:
            yield current
        finally:
            self.inflight[thread_id] -= 1
            if not self.inflight[thread_id]:
                self.inflight.pop(thread_id)

    async def disconnect(self, identity):
        current = self.current.get(identity["thread_id"])
        if not current or current["generation"] != identity["generation"]:
            return
        await self.store.call(lambda db: db.execute(
            "UPDATE execution_channels SET state='DISCONNECTED',updated_at=? WHERE id=? AND generation=?",
            (now_ms(), identity["id"], identity["generation"])), transaction=True)
        current["state"] = "DISCONNECTED"
        self.current.pop(identity["thread_id"], None)

    def ended(self, run_id, thread_id):
        current = self.current.get(thread_id)
        if current and current["run_id"] == run_id:
            self.current.pop(thread_id)

    def end_in(self, db, run_id, thread_id):
        db.execute("UPDATE execution_channels SET state='DISCONNECTED',updated_at=? WHERE thread_id=? AND run_id=?",
                   (now_ms(), thread_id, run_id))

    async def inbox(self, identity, *, after_position=0, limit=32):
        require(isinstance(limit, int) and 1 <= limit <= 100 and isinstance(after_position, int) and after_position >= 0,
                "INVALID_REQUEST", "Invalid inbox range")
        with self.use(identity):
            def offer(db):
                task, _ = self.check_run(db, identity["run_id"])
                found = rows(db, "SELECT m.*,d.position FROM messages m JOIN message_delivery d ON d.message_id=m.id "
                    "WHERE m.target_thread_id=? AND d.position>? AND d.state IN ('DELIVERED','OFFERED') "
                    "AND (m.state='PENDING' OR m.target_run_id=?) ORDER BY d.position LIMIT ?",
                    (task.thread_id, after_position, identity["run_id"], limit + 1))
                result, total = [], 0
                for row in found[:limit]:
                    size = len(row.envelope_json.encode())
                    if result and total + size > 262144:
                        break
                    total += size
                    if row.state == "PENDING":
                        self.delivery.offer(db, task, identity["run_id"], row.id)
                    result.append({"position": row.position, "envelope": json.loads(row.envelope_json)})
                return {"items": result, "cursor": result[-1]["position"] if result else after_position,
                        "has_more": len(found) > len(result)}
            return await self.store.call(offer, transaction=True)

    async def acknowledge(self, identity, positions):
        require(isinstance(positions, (list, tuple)) and 0 < len(positions) <= 100 and
                all(isinstance(value, int) and value > 0 for value in positions) and
                list(positions) == sorted(set(positions)), "INVALID_REQUEST", "Acknowledge ordered unique positions")
        with self.use(identity):
            def commit(db):
                task, _ = self.check_run(db, identity["run_id"])
                channel = one(db, "SELECT * FROM execution_channels WHERE id=?", (identity["id"],))
                owned = rows(db, "SELECT d.position FROM message_delivery d JOIN messages m ON m.id=d.message_id "
                    "WHERE m.target_thread_id=? AND d.position IN (" + ",".join("?" for _ in positions) + ")",
                    (task.thread_id, *positions))
                require(len(owned) == len(positions), "ACK_GAP", "Receipt includes a message outside this channel")
                remaining = [position for position in positions if position > channel.received_position]
                if not remaining:
                    return channel.received_position
                expected = rows(db, "SELECT d.position,d.state,m.target_run_id FROM message_delivery d JOIN messages m ON m.id=d.message_id "
                    "WHERE m.target_thread_id=? AND d.position>? ORDER BY d.position LIMIT ?",
                    (task.thread_id, channel.received_position, len(remaining)))
                require([row.position for row in expected] == remaining and all(row.state in {'APPLIED','REJECTED','EXPIRED'} or
                        (row.state == 'OFFERED' and row.target_run_id == identity['run_id']) for row in expected),
                        "ACK_GAP", "A receipt cannot skip an unreceived message")
                self.delivery.received(db, remaining)
                db.execute("UPDATE execution_channels SET received_position=?,updated_at=? WHERE id=?",
                           (remaining[-1], now_ms(), channel.id))
                emit(db, "channel.received", session_id=task.session_id, thread_id=task.thread_id, run_id=identity["run_id"],
                     payload={"channel_id": channel.id, "received_position": remaining[-1]})
                return remaining[-1]
            position = await self.store.call(commit, transaction=True)
            self.current[identity["thread_id"]]["received_position"] = position
            return {"received_position": position, "application_confirmed": False}
