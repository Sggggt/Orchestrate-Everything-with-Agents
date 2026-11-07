"""Durable free-form board entries; notifications never create work or read grants."""

import hashlib
import json
from typing import Literal

from oea.catalog.service import need
from oea.catalog import reading
from oea.collaboration.boards import Boards
from oea.contracts.configuration import AgentDefinition, manager_snapshot
from oea.contracts.domain import MessageSpec
from oea.contracts.errors import RuntimeFault, require
from oea.context.store import ContextStore
from oea.interfaces.registry import operation_context
from oea.storage.persistence import encode, event, new_id, now_ms, one, rows


class Messages:
    def __init__(self, host):
        self.host = host

    @staticmethod
    def source(db, run_id, session):
        require(session["source"] == "platform", "PERMISSION_DENIED", "Application execution has no platform communication capability")
        run = need(db, "runs", run_id)
        instance = need(db, "instances", run["instance_id"])
        require(instance["session_id"] == session["id"] and run["state"] == "running" and run["cancel_requested_at"] is None,
                "PERMISSION_DENIED", "Sender does not own an active run in this session")
        before = AgentDefinition.model_validate_json(run["config_json"])
        current = AgentDefinition.model_validate_json(need(db, "agent_definitions", instance["agent_id"])["config_json"])
        require(current.enabled, "AGENT_DISABLED", "Sender is disabled")
        if instance["role"] == "manager":
            require(session["mode"] == "collaborative" and current.manager is not None, "MODE_CHANGED", "Manager coordination is disabled")
            current = manager_snapshot(current)
        if run["root_run_id"]:
            require(need(db, "runs", run["root_run_id"])["cancel_requested_at"] is None, "RUN_CANCELLED", "Root intent was cancelled")
        else:
            require(need(db, "workflow_runs", run["root_workflow_run_id"])["cancel_requested_at"] is None, "RUN_CANCELLED", "Root intent was cancelled")
        departments = {instance["agent_id"]} | (set(before.communication.departments) & set(current.communication.departments))
        return run, instance, departments, before.communication.can_request and current.communication.can_request

    @staticmethod
    def visible(db, board, source=None):
        return Boards.check(db, board["id"], project_id=board["project_id"], instance=source[1] if source else None)

    def _send(self, db, spec, *, source_run_id=None, unavailable_refs=()):
        board = need(db, "boards", spec.board_id)
        source = None
        if source_run_id:
            run = need(db, "runs", source_run_id)
            sender = need(db, "instances", run["instance_id"])
            source = self.source(db, source_run_id, need(db, "sessions", sender["session_id"]))
        Boards.check(db, spec.board_id, project_id=source[1]["project_id"] if source else board["project_id"],
                     instance=source[1] if source else None, write=True, departments=source[2] if source else None)
        fingerprint = hashlib.sha256(encode({"spec": spec.model_dump(mode="json", exclude_defaults=True), "source_run_id": source_run_id}).encode("utf-8")).hexdigest()
        previous = one(db, "SELECT * FROM messages WHERE board_id=? AND idempotency_key=?", (spec.board_id, spec.idempotency_key))
        if previous:
            require(previous["input_hash"] == fingerprint, "IDEMPOTENCY_CONFLICT", "Message key has different content or ownership")
            if source:
                recipients = rows(db, "SELECT i.agent_id FROM deliveries d JOIN instances i ON i.id=d.instance_id WHERE d.message_id=?", (previous["id"],))
                require(all(item["agent_id"] in source[2] for item in recipients), "PERMISSION_DENIED", "An original recipient is no longer reachable")
            return previous, True
        identity = new_id("message")
        origin = {"kind": "leader", "name": self.host.redact((operation_context.get() or {}).get("agent_name") or "leader")} if not source else {
            "kind": "instance", "instance_id": source[1]["id"], "name": source[1]["name"], "agent_id": source[1]["agent_id"], "run_id": source[0]["id"]}
        db.execute("INSERT INTO messages(id,project_id,session_id,board_id,source_json,body,root_run_id,root_workflow_run_id,idempotency_key,input_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (identity, board["project_id"], board["session_id"], board["id"], encode(origin), self.host.redact(spec.content),
             source[0]["root_run_id"] if source else None, source[0]["root_workflow_run_id"] if source else None, spec.idempotency_key, fingerprint, now_ms()))
        # The outer envelope has been accepted. Optional references and mentions
        # report their own failures without losing the original body.
        issues, accepted_refs, accepted_artifacts = [], [], []
        for field in ("reply_to", "topic_id"):
            target = getattr(spec, field)
            if not target:
                continue
            previous = one(db, "SELECT board_id FROM messages WHERE id=?", (target,))
            if previous and previous["board_id"] == board["id"]:
                db.execute("UPDATE messages SET " + field + "=? WHERE id=?", (target, identity))
            else:
                issues.append({"field": field, "target": target, "reason": "REFERENCE_UNAVAILABLE"})
        for reference in dict.fromkeys(spec.context_refs):
            evidence = one(db, "SELECT * FROM context_sources WHERE id=?", (reference,))
            try:
                require(reference not in unavailable_refs and evidence and evidence["project_id"] == board["project_id"], "REFERENCE_UNAVAILABLE", "Reference is unavailable")
                if source:
                    ContextStore.check_access(db, source[1], evidence)
                db.execute("INSERT INTO message_context_refs VALUES(?,?)", (identity, reference))
                accepted_refs.append(reference)
            except RuntimeFault:
                issues.append({"field": "context_refs", "target": reference, "reason": "REFERENCE_UNAVAILABLE"})
        for artifact_id in dict.fromkeys(spec.artifact_ids):
            artifact = one(db, "SELECT project_id,instance_id FROM artifacts WHERE id=?", (artifact_id,))
            if artifact and artifact["project_id"] == board["project_id"] and (not source or artifact["instance_id"] == source[1]["id"] or one(db, "SELECT 1 FROM run_artifacts WHERE run_id=? AND artifact_id=?", (source_run_id, artifact_id))):
                db.execute("INSERT INTO message_artifacts VALUES(?,?)", (identity, artifact_id))
                accepted_artifacts.append(artifact_id)
            else:
                issues.append({"field": "artifact_ids", "target": artifact_id, "reason": "REFERENCE_UNAVAILABLE"})
        targets = list(dict.fromkeys(([board["owner_instance_id"]] if board["scope"] == "inbox" else []) + list(spec.mentions)))
        for target in targets:
            recipient = one(db, "SELECT * FROM instances WHERE id=?", (target,))
            available = recipient and Boards.can_read(db, board, recipient)
            if source and available:
                available = recipient["session_id"] == source[1]["session_id"] and recipient["agent_id"] in source[2]
            if available:
                config = one(db, "SELECT config_json FROM agent_definitions WHERE id=?", (recipient["agent_id"],))
                available = config and json.loads(config["config_json"])["enabled"]
            if not available:
                issues.append({"field": "mentions", "target": target, "reason": "NOT_DELIVERED"})
                continue
            db.execute("INSERT INTO deliveries(message_id,instance_id,delivered_at) VALUES(?,?,?)", (identity, target, now_ms()))
            for reference in accepted_refs:
                db.execute("INSERT OR IGNORE INTO context_grants VALUES(?,?,?)", (reference, target, identity))
        db.execute("UPDATE messages SET issues_json=? WHERE id=?", (encode(issues), identity))
        event(db, "message.accepted", project_id=board["project_id"], session_id=board["session_id"], data={"message_id": identity, "board_id": board["id"], "notification_count": len(targets) - sum(issue["field"] == "mentions" for issue in issues)})
        return need(db, "messages", identity), False

    @staticmethod
    def _view(row):
        return {key: row[key] for key in ("id", "position", "project_id", "session_id", "board_id", "root_run_id", "root_workflow_run_id", "reply_to", "topic_id", "created_at")} | {
            "content": row["body"], "source": json.loads(row["source_json"]), "issues": json.loads(row["issues_json"])}

    def receipt(self, db, row):
        identity = row["id"]
        return self._view(row) | {"deliveries": rows(db, "SELECT d.*,i.name AS instance_name FROM deliveries d JOIN instances i ON i.id=d.instance_id WHERE d.message_id=? ORDER BY d.rowid", (identity,)),
            "context_refs": [item["source_id"] for item in rows(db, "SELECT source_id FROM message_context_refs WHERE message_id=?", (identity,))],
            "artifact_ids": [item["artifact_id"] for item in rows(db, "SELECT artifact_id FROM message_artifacts WHERE message_id=?", (identity,))]}

    async def send(self, spec: MessageSpec):
        self.host.assert_open()
        return await self._accept(MessageSpec.model_validate(spec))

    async def send_from(self, run_id, spec, *, action_id=None):
        require(run_id in self.host.active, "STALE_RUN", "Sender no longer owns execution")
        spec = MessageSpec.model_validate(spec)
        context, unavailable = self.host.active[run_id], []
        for reference in spec.context_refs:
            try:
                await self.host.contexts.authorize_reference(context.run, context.instance, reference, self.host.authorize)
            except RuntimeFault:
                unavailable.append(reference)
        return await self._accept(spec, source_run_id=run_id, action_id=action_id, unavailable_refs=unavailable)

    async def _accept(self, spec, *, source_run_id=None, action_id=None, unavailable_refs=()):
        def accept(db):
            row, reused = self._send(db, spec, source_run_id=source_run_id, unavailable_refs=unavailable_refs)
            receipt = self.receipt(db, row) | {"reused": reused}
            if action_id:
                receipt = self.host.tools.commit_owned(db, action_id, source_run_id, receipt)
            return receipt
        receipt = await self.host.store.call(accept, transaction=True)
        self.host.notify()
        return receipt

    async def get(self, message_id: str, *, project_id: str):
        def read(db):
            row = need(db, "messages", message_id)
            Boards.check(db, row["board_id"], project_id=project_id)
            return self.receipt(db, row)
        return await self.host.store.call(read)

    @staticmethod
    def scope(db, board_id, project_id, session_id=None, instance=None):
        require((board_id is None) != (session_id is None), "INVALID_REQUEST", "Choose one board or session message scope")
        if board_id is not None:
            return Boards.check(db, board_id, project_id=project_id, instance=instance)
        require(instance is None, "PERMISSION_DENIED", "Session-wide message supervision is a controller capability")
        require(need(db, "sessions", session_id)["project_id"] == project_id, "PERMISSION_DENIED", "Session belongs to another project")
        return None

    def page(self, db, board_id, *, project_id, after=0, limit=25, topic_id=None, order="asc", before=None, instance=None, session_id=None):
        require(type(after) is int and 0 <= after < 2**63 and (before is None or type(before) is int and 0 <= before < 2**63) and 1 <= limit <= 100 and order in {"asc", "desc"}, "INVALID_REQUEST", "Read a bounded message page")
        self.scope(db, board_id, project_id, session_id, instance)
        query = "SELECT m.*,b.scope AS board_scope,b.name AS board_name,b.owner_instance_id AS inbox_owner_id,i.name AS inbox_owner_name FROM messages m JOIN boards b ON b.id=m.board_id LEFT JOIN instances i ON i.id=b.owner_instance_id WHERE "
        query += "m.board_id=?" if board_id is not None else "m.session_id=?"
        query += " AND m.position>?"
        params = [board_id if board_id is not None else session_id, after]
        if before is not None:
            query += " AND m.position<?"
            params.append(before)
        if topic_id:
            query += " AND (m.topic_id=? OR m.id=?)"
            params.extend((topic_id, topic_id))
        values = rows(db, query + " ORDER BY m.position " + ("DESC" if order == "desc" else "ASC") + " LIMIT ?", (*params, limit + 1))
        return {"items": [self.receipt(db, row) | ({"board": {"id": row["board_id"], "name": row["board_name"], "scope": row["board_scope"],
                    "owner_instance_id": row["inbox_owner_id"], "owner_name": row["inbox_owner_name"]}} if session_id is not None else {}) for row in values[:limit]],
                "cursor": values[limit - 1]["position"] if len(values) > limit else None,
                "next_position": values[min(limit, len(values)) - 1]["position"] if values else after}

    async def list(self, board_id: str | None = None, *, project_id: str, after: int = 0, limit: int = 25, topic_id: str | None = None,
                   order: Literal["asc", "desc"] = "asc", before: int | None = None, session_id: str | None = None):
        return await self.host.store.call(lambda db: self.page(db, board_id, project_id=project_id, after=after, limit=limit, topic_id=topic_id, order=order, before=before, session_id=session_id), transaction="read")

    @staticmethod
    def save_position(db, board_id, consumer_id, position):
        require(type(position) is int and 0 <= position < 2**63, "INVALID_REQUEST", "Use a saved message position")
        require(position == 0 or one(db, "SELECT 1 FROM messages WHERE board_id=? AND position=?", (board_id, position)), "INVALID_REQUEST", "Read position belongs to another board")
        db.execute("INSERT INTO board_read_positions VALUES(?,?,?,?) ON CONFLICT(consumer_id,board_id) DO UPDATE SET position=MAX(position,excluded.position),updated_at=excluded.updated_at", (consumer_id, board_id, position, now_ms()))
        return one(db, "SELECT position FROM board_read_positions WHERE consumer_id=? AND board_id=?", (consumer_id, board_id))["position"]

    async def mark_read(self, board_id: str | None, consumer_id: str, position: int, *, project_id: str, session_id: str | None = None):
        consumer_id = reading.reader(consumer_id)
        def write(db):
            board = self.scope(db, board_id, project_id, session_id)
            if board is not None:
                saved = self.save_position(db, board_id, consumer_id, position)
                owner = board["session_id"]
                result = {"board_id": board_id, "position": saved}
            else:
                require(type(position) is int and 0 <= position < 2**63 and (position == 0 or one(db,
                    "SELECT 1 FROM messages WHERE session_id=? AND position=?", (session_id, position))), "INVALID_REQUEST", "Read position belongs to another session")
                db.execute("INSERT INTO board_read_positions(consumer_id,board_id,position,updated_at) "
                    "SELECT ?,board_id,MAX(position),? FROM messages WHERE session_id=? AND position<=? GROUP BY board_id "
                    "ON CONFLICT(consumer_id,board_id) DO UPDATE SET position=MAX(position,excluded.position),updated_at=excluded.updated_at",
                    (consumer_id, now_ms(), session_id, position))
                owner, saved = session_id, position
                result = {"session_id": session_id, "position": saved, "positions": self.session_positions(db, session_id, consumer_id)}
            event(db, "message.read", project_id=project_id, session_id=owner, data={"board_id": board_id, "consumer_id": consumer_id, "position": saved})
            return result | {"summary": reading.summary(db, owner, project_id, consumer_id) if owner else None}
        result = await self.host.store.call(write, transaction=True)
        self.host.notify()
        return result

    @staticmethod
    def session_positions(db, session_id, consumer_id):
        return {item["board_id"]: item["position"] for item in rows(db,
            "SELECT p.board_id,p.position FROM board_read_positions p JOIN boards b ON b.id=p.board_id WHERE p.consumer_id=? AND b.session_id=?", (consumer_id, session_id))}

    async def unread(self, board_id: str | None, consumer_id: str, *, project_id: str, session_id: str | None = None):
        consumer_id = reading.reader(consumer_id)
        def read(db):
            self.scope(db, board_id, project_id, session_id)
            if session_id is not None:
                positions = self.session_positions(db, session_id, consumer_id)
                count = one(db, "SELECT COUNT(*) AS count FROM messages m LEFT JOIN board_read_positions p ON p.board_id=m.board_id AND p.consumer_id=? WHERE m.session_id=? AND m.position>COALESCE(p.position,0)", (consumer_id, session_id))["count"]
                return {"session_id": session_id, "position": max(positions.values(), default=0), "positions": positions, "count": count}
            cursor = one(db, "SELECT position FROM board_read_positions WHERE consumer_id=? AND board_id=?", (consumer_id, board_id))
            position = cursor["position"] if cursor else 0
            return {"position": position, "count": one(db, "SELECT COUNT(*) AS count FROM messages WHERE board_id=? AND position>?", (board_id, position))["count"]}
        return await self.host.store.call(read)
