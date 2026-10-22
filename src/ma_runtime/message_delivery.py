"""Delivery evidence is independent of browser read state and execution status."""

from .errors import require
from .models import content_hash, now_ms
from .store import emit, one, rows


class MessageDelivery:
    def __init__(self, store, *, check_run, notify):
        self.store, self.check_run, self.notify = store, check_run, notify

    def offer(self, db, task, run_id, message_id):
        db.execute("UPDATE messages SET state='OFFERED',target_run_id=?,updated_at=? WHERE id=?",
                   (run_id, now_ms(), message_id))
        emit(db, "message.offered", session_id=task.session_id, thread_id=task.thread_id, task_id=task.id,
             run_id=run_id, payload={"message_id": message_id, "state": "OFFERED"})

    def offer_pending(self, db, task, run_id):
        pending = rows(db, "SELECT m.* FROM messages m JOIN message_delivery d ON d.message_id=m.id "
                       "WHERE target_thread_id=? AND ((m.state='PENDING' AND d.state='DELIVERED') OR "
                       "(m.state='OFFERED' AND d.state='OFFERED' AND EXISTS(SELECT 1 FROM runs r WHERE r.id=m.target_run_id "
                       "AND r.state IN ('SUCCEEDED','FAILED','WAITING') AND r.boundary_receipt_json IS NOT NULL))) "
                       "ORDER BY m.created_at,m.rowid LIMIT 100",
                       (task.thread_id,))
        for message in pending:
            self.offer(db, task, run_id, message.id)
        return pending

    def validate_application(self, db, run_id, identities):
        if not identities:
            return
        offered = {row.id for row in rows(db, "SELECT m.id FROM messages m JOIN message_delivery d ON d.message_id=m.id "
                                         "WHERE m.target_run_id=? AND d.state='OFFERED'", (run_id,))}
        require(set(identities) <= offered, "ADAPTER_PROTOCOL_ERROR", "Receipt contains messages not offered to this run")

    def apply(self, db, task, run_id, checkpoint_id, identities):
        for identity in identities:
            db.execute("UPDATE messages SET state='APPLIED',applied_checkpoint_id=?,updated_at=? WHERE id=?",
                       (checkpoint_id, now_ms(), identity))
            emit(db, "message.applied", session_id=task.session_id, thread_id=task.thread_id, task_id=task.id,
                 run_id=run_id, payload={"message_id": identity, "state": "APPLIED"})

    def reset_offers(self, db, run_id):
        db.execute("UPDATE messages SET state='PENDING',target_run_id=NULL WHERE target_run_id=? AND state='OFFERED'", (run_id,))

    def received(self, db, positions):
        db.executemany("UPDATE message_delivery SET received_at=COALESCE(received_at,?) WHERE position=?",
                       ((now_ms(), position) for position in positions))

    async def get(self, message_id):
        row = await self.store.call(lambda db: one(db,
            "SELECT d.*,m.target_thread_id,m.target_run_id,m.applied_checkpoint_id FROM message_delivery d JOIN messages m ON m.id=d.message_id WHERE message_id=?", (message_id,)))
        require(row is not None, "NOT_FOUND", "Message delivery does not exist")
        return {key: value for key, value in row.items() if key not in {"disposition_key", "disposition_hash"}}

    async def dispose(self, message_id, state, reason, key, *, expires_at_ms=None, caller_run_id=None):
        require(isinstance(reason, str) and 0 < len(reason.strip()) <= 4096 and
                isinstance(key, str) and 0 < len(key) <= 256, "INVALID_REQUEST", "A bounded reason and idempotency key are required")
        if state == "EXPIRED":
            require(isinstance(expires_at_ms, int) and 0 < expires_at_ms <= now_ms(),
                    "INVALID_REQUEST", "Expiration requires an elapsed deadline")
        digest = content_hash({"state": state, "reason": reason, "expires_at_ms": expires_at_ms})
        def commit(db):
            row = one(db, "SELECT d.*,m.session_id,m.target_thread_id,m.target_run_id,m.state AS legacy_state FROM message_delivery d JOIN messages m ON m.id=d.message_id WHERE message_id=?", (message_id,))
            require(row is not None, "NOT_FOUND", "Message delivery does not exist")
            if row.disposition_key:
                require(row.disposition_key == key and row.disposition_hash == digest,
                        "IDEMPOTENCY_CONFLICT", "Message disposition is immutable")
                return
            require(row.state != "APPLIED", "MESSAGE_ALREADY_APPLIED", "An applied message cannot be rejected or expired")
            if caller_run_id:
                task, _ = self.check_run(db, caller_run_id)
                require(task.thread_id == row.target_thread_id, "PERMISSION_DENIED", "Message belongs to another thread")
            if row.state == "OFFERED":
                require(caller_run_id is not None and caller_run_id == row.target_run_id,
                        "MESSAGE_ALREADY_OFFERED", "Only the receiving execution can decline an offered message")
            db.execute("UPDATE message_delivery SET state=?,disposition_key=?,disposition_hash=?,reason=?,ended_at=? WHERE message_id=?",
                       (state, key, digest, reason, now_ms(), message_id))
            emit(db, "message." + state.lower(), session_id=row.session_id, thread_id=row.target_thread_id,
                 payload={"message_id": message_id, "state": state, "reason": reason})
        await self.store.call(commit, transaction=True)
        self.notify()
        return await self.get(message_id)
