"""Purposeful, atomic inbox delivery; messages never infer their own authority."""

import hashlib
import json
from typing import Literal

from oea.catalog.service import need
from oea.contracts.configuration import AgentDefinition
from oea.context.store import ContextStore
from oea.contracts.domain import MessageSpec, RunSpec
from oea.catalog import reading
from oea.interfaces.registry import operation_context
from oea.contracts.errors import require
from oea.storage.persistence import encode, event, new_id, now_ms, one, rows


class Messages:
    def __init__(self, host):
        self.host = host

    @staticmethod
    def source(db, run_id, session):
        run = need(db, "runs", run_id)
        instance = need(db, "instances", run["instance_id"])
        require(instance["session_id"] == session["id"] and run["state"] == "running" and run["cancel_requested_at"] is None,
                "PERMISSION_DENIED", "Sender does not own an active run in this session")
        before = AgentDefinition.model_validate_json(run["config_json"])
        current = AgentDefinition.model_validate_json(need(db, "agent_definitions", instance["agent_id"])["config_json"])
        require(current.enabled, "AGENT_DISABLED", "Sender is disabled")
        if instance["role"] == "manager":
            require(session["mode"] == "collaborative" and current.manager is not None, "MODE_CHANGED", "Manager coordination is disabled")
            communication = current.manager.communication
        else:
            communication = current.communication
        if run["root_run_id"]:
            require(need(db, "runs", run["root_run_id"])["cancel_requested_at"] is None, "RUN_CANCELLED", "Root intent was cancelled")
        else:
            require(need(db, "workflow_runs", run["root_workflow_run_id"])["cancel_requested_at"] is None, "RUN_CANCELLED", "Root intent was cancelled")
        departments = {instance["agent_id"]} | (set(before.communication.departments) & set(communication.departments))
        can_request = before.communication.can_request and communication.can_request
        return run, instance, departments, can_request

    def _recipients(self, db, spec, session, *, departments=None):
        selected = {}
        for target in spec.recipients:
            if target == "all":
                query, params = "SELECT * FROM instances WHERE session_id=?", [session["id"]]
                if departments is not None:
                    query += " AND agent_id IN (" + ",".join("?" for _ in departments) + ")"
                    params.extend(departments)
                found = rows(db, query + " ORDER BY rowid LIMIT 257", params)
            elif target.startswith("department:"):
                agent_id = target.removeprefix("department:")
                found = [self.host.instances._create(db, session["id"], agent_id, name="manager", role="manager")]
            else:
                found = [need(db, "instances", target.removeprefix("instance:"))]
            for instance in found:
                require(instance["session_id"] == session["id"] and instance["project_id"] == session["project_id"],
                        "PERMISSION_DENIED", "Recipient belongs to another collaboration space")
                selected[instance["id"]] = instance
        require(0 < len(selected) <= 256, "INVALID_RECIPIENTS", "Choose 1 to 256 actual recipients")
        return list(selected.values())

    def _send(self, db, spec, *, source_run_id=None):
        session = need(db, "sessions", spec.session_id)
        source = self.source(db, source_run_id, session) if source_run_id else None
        fingerprint = hashlib.sha256(encode({"spec": spec.model_dump(mode="json", exclude_defaults=True), "source_run_id": source_run_id}).encode("utf-8")).hexdigest()
        previous = one(db, "SELECT * FROM messages WHERE session_id=? AND idempotency_key=?", (spec.session_id, spec.idempotency_key))
        if previous:
            require(previous["input_hash"] == fingerprint, "IDEMPOTENCY_CONFLICT", "Message key has different content, purpose, or ownership")
            return previous, True, []
        require(spec.purpose == "reply" or spec.target_run_id is None, "INVALID_REQUEST", "Only replies name an existing target run")
        require(spec.purpose == "request" or spec.budget_id is None, "INVALID_REQUEST", "Only a work request chooses a cost budget")
        recipients = self._recipients(db, spec, session, departments=source[2] if source else None)
        root, root_workflow = spec.root_run_id, spec.root_workflow_run_id
        parent = None
        if source:
            sender, sender_instance, departments, can_request = source
            require(root in {None, sender["root_run_id"]} and root_workflow in {None, sender["root_workflow_run_id"]}, "PERMISSION_DENIED", "Message cannot forge another root intent")
            root, root_workflow, parent = sender["root_run_id"], sender["root_workflow_run_id"], sender["id"]
            for recipient in recipients:
                require(recipient["agent_id"] in departments, "PERMISSION_DENIED", "Department is not currently granted")
                if spec.purpose == "request":
                    own_employee = sender_instance["role"] == "manager" and recipient["agent_id"] == sender_instance["agent_id"] and recipient["role"] == "main"
                    require(own_employee or can_request and recipient["id"] != sender_instance["id"], "PERMISSION_DENIED", "Sender cannot request this work")
        else:
            require(not (root and root_workflow), "INVALID_REQUEST", "A message has one root intent")
            if root:
                parent = need(db, "runs", root)
                owner = need(db, "instances", parent["instance_id"])
                require(owner["session_id"] == spec.session_id, "PERMISSION_DENIED", "Root belongs to another session")
                parent, root, root_workflow = parent["id"], parent["root_run_id"], parent["root_workflow_run_id"]
            if root_workflow:
                workflow = need(db, "workflow_runs", root_workflow)
                require(workflow["session_id"] == spec.session_id and workflow["cancel_requested_at"] is None, "PERMISSION_DENIED", "Root belongs to another active workflow")
                root, root_workflow = workflow["root_run_id"], workflow["root_workflow_run_id"]
        original = need(db, "messages", spec.reply_to) if spec.reply_to else None
        require(original is None or original["session_id"] == spec.session_id, "PERMISSION_DENIED", "Reply belongs to another session")
        if spec.topic_id:
            topic = need(db, "messages", spec.topic_id)
            require(topic["session_id"] == spec.session_id, "PERMISSION_DENIED", "Topic belongs to another session")
        identity = new_id("message")
        origin = {"kind": "leader", "name": self.host.redact((operation_context.get() or {}).get("agent_name") or "leader"), "mode_epoch": session["epoch"]} if not source else {
            "kind": "manager" if source[1]["role"] == "manager" else "instance", "instance_id": source[1]["id"],
            "name": source[1]["name"], "agent_id": source[1]["agent_id"], "run_id": source[0]["id"], "mode_epoch": session["epoch"]}
        db.execute("INSERT INTO messages(id,project_id,session_id,source_json,purpose,body,root_run_id,root_workflow_run_id,reply_to,topic_id,idempotency_key,input_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (identity, session["project_id"], spec.session_id, encode(origin), spec.purpose, self.host.redact(spec.content), root, root_workflow,
             spec.reply_to, spec.topic_id or (original["topic_id"] or original["id"] if original else None), spec.idempotency_key, fingerprint, now_ms()))
        for reference in dict.fromkeys(spec.context_refs):
            evidence = need(db, "context_sources", reference)
            require(evidence["project_id"] == session["project_id"], "PERMISSION_DENIED", "Context reference belongs to another project")
            if source:
                ContextStore.check_access(db, source[1], evidence)
            db.execute("INSERT INTO message_context_refs VALUES(?,?)", (identity, reference))
            for recipient in recipients:
                db.execute("INSERT OR IGNORE INTO context_grants VALUES(?,?,?)", (reference, recipient["id"], identity))
        launched = []
        for recipient in recipients:
            run_id, rejected, reason = None, None, None
            if spec.purpose == "request":
                run, _ = self.host.runs._submit(db, RunSpec(instance_id=recipient["id"], input={}, idempotency_key="message:" + identity, parent_run_id=parent, budget_id=spec.budget_id),
                    workflow_run_id=source[0]["workflow_run_id"] if source else None if parent else root_workflow, source_message_id=identity)
                run_id = run["id"]
                if root is None and root_workflow is None:
                    root, parent = run["root_run_id"], run["id"]
                launched.append(run_id)
            elif spec.purpose == "reply":
                delivery = one(db, "SELECT run_id FROM deliveries WHERE message_id=? AND instance_id=?", (spec.reply_to, recipient["id"])) if spec.reply_to else None
                run_id = spec.target_run_id or (delivery["run_id"] if delivery else None)
                require(run_id is not None, "RUN_REQUIRED", "Reply must identify the original request or waiting run")
                target = need(db, "runs", run_id)
                require(target["instance_id"] == recipient["id"], "PERMISSION_DENIED", "Reply targets another instance")
                require(not source or (target["root_run_id"], target["root_workflow_run_id"]) == (root, root_workflow), "PERMISSION_DENIED", "Reply belongs to another root intent")
                if target["state"] in {"completed", "cancelled"} or target["cancel_requested_at"] is not None:
                    rejected, reason = now_ms(), "RUN_FINISHED"
                elif target["state"] in {"waiting", "failed", "needs_review"}:
                    self.host.runs._resume(db, run_id, "message:" + identity, source_message_id=identity, refresh_model=False, refresh_capabilities=False)
                    launched.append(run_id)
                root, root_workflow = target["root_run_id"], target["root_workflow_run_id"]
            db.execute("INSERT INTO deliveries(message_id,instance_id,run_id,accepted_at,rejected_at,reason) VALUES(?,?,?,?,?,?)",
                (identity, recipient["id"], run_id, now_ms(), rejected, reason))
        db.execute("UPDATE messages SET root_run_id=?,root_workflow_run_id=? WHERE id=?", (root, root_workflow, identity))
        event(db, "message.accepted", project_id=session["project_id"], session_id=spec.session_id, data={"message_id": identity, "purpose": spec.purpose, "recipient_count": len(recipients)})
        return need(db, "messages", identity), False, launched

    async def send(self, spec: MessageSpec):
        self.host.assert_open()
        return await self._accept(MessageSpec.model_validate(spec))

    async def send_from(self, run_id, spec, *, action_id=None):
        context = self.host.active.get(run_id)
        require(context is not None, "STALE_RUN", "Sender no longer owns execution")
        spec = MessageSpec.model_validate(spec)
        for reference in spec.context_refs:
            await self.host.contexts.authorize_reference(context.run, context.instance, reference, self.host.authorize)
        return await self._accept(spec, source_run_id=run_id, action_id=action_id)

    async def _accept(self, spec, *, source_run_id=None, action_id=None):
        def accept(db):
            row, reused, launched = self._send(db, spec, source_run_id=source_run_id)
            receipt = {**self._view(row), "deliveries": rows(db, "SELECT * FROM deliveries WHERE message_id=? ORDER BY rowid", (row["id"],)),
                "context_refs": [item["source_id"] for item in rows(db, "SELECT source_id FROM message_context_refs WHERE message_id=?", (row["id"],))], "reused": reused}
            if action_id:
                receipt = self.host.tools.commit_owned(db, action_id, source_run_id, receipt)
            return receipt, launched
        receipt, launched = await self.host.store.call(accept, transaction=True)
        for identity in launched:
            self.host.launch(identity)
        self.host.notify()
        return receipt

    @staticmethod
    def _view(row):
        return {key: row[key] for key in ("id", "position", "project_id", "session_id", "purpose", "root_run_id", "root_workflow_run_id", "reply_to", "topic_id", "created_at")} | {
            "content": row["body"], "source": json.loads(row["source_json"])}

    async def get(self, message_id: str, *, project_id: str):
        def read(db):
            row = need(db, "messages", message_id)
            require(row["project_id"] == project_id, "PERMISSION_DENIED", "Message belongs to another project")
            return self._view(row) | {"deliveries": rows(db, "SELECT * FROM deliveries WHERE message_id=? ORDER BY rowid", (message_id,)),
                "context_refs": [item["source_id"] for item in rows(db, "SELECT source_id FROM message_context_refs WHERE message_id=?", (message_id,))]}
        return await self.host.store.call(read)

    async def list(self, session_id: str, *, project_id: str, after: int = 0, limit: int = 25, topic_id: str | None = None,
                   order: Literal["asc", "desc"] = "asc", before: int | None = None):
        require(0 <= after < 2**63 and (before is None or 0 <= before < 2**63) and 1 <= limit <= 100 and order in {"asc", "desc"}, "INVALID_REQUEST", "Read a bounded message page")
        def read(db):
            require(need(db, "sessions", session_id)["project_id"] == project_id, "PERMISSION_DENIED", "Session belongs to another project")
            query = "SELECT m.*,COUNT(d.instance_id) AS accepted_count,COUNT(d.started_at) AS started_count,COUNT(d.applied_at) AS applied_count,COUNT(d.completed_at) AS completed_count,COUNT(d.rejected_at) AS rejected_count,json_group_array(d.instance_id) FILTER (WHERE d.instance_id IS NOT NULL) AS recipient_instance_ids FROM messages m LEFT JOIN deliveries d ON d.message_id=m.id WHERE m.session_id=? AND m.position>?"
            params = [session_id, after]
            if before is not None:
                query += " AND m.position<?"
                params.append(before)
            if topic_id:
                query += " AND (m.topic_id=? OR m.id=?)"
                params.extend((topic_id, topic_id))
            return rows(db, query + " GROUP BY m.position ORDER BY m.position " + ("DESC" if order == "desc" else "ASC") + " LIMIT ?", (*params, limit + 1))
        values = await self.host.store.call(read)
        return {"items": [self._view(row) | {"recipient_instance_ids": json.loads(row["recipient_instance_ids"]), "delivery_summary": {field: row[field + "_count"] for field in ("accepted", "started", "applied", "completed", "rejected")}} for row in values[:limit]],
            "cursor": values[limit - 1]["position"] if len(values) > limit else None}

    async def mark_read(self, session_id: str, consumer_id: str, position: int, *, project_id: str):
        consumer_id = reading.reader(consumer_id)
        require(type(position) is int and 0 <= position < 2**63, "INVALID_REQUEST", "Use a saved message position")
        def write(db):
            require(need(db, "sessions", session_id)["project_id"] == project_id, "PERMISSION_DENIED", "Session belongs to another project")
            require(position == 0 or one(db, "SELECT 1 FROM messages WHERE session_id=? AND position=?", (session_id, position)), "INVALID_REQUEST", "Read position belongs to another session")
            previous = one(db, "SELECT position FROM read_positions WHERE consumer_id=? AND session_id=?", (consumer_id, session_id))
            if previous and previous["position"] >= position:
                return {"position": previous["position"], "summary": reading.summary(db, session_id, project_id, consumer_id)}, False
            db.execute("INSERT INTO read_positions VALUES(?,?,?,?) ON CONFLICT(consumer_id,session_id) DO UPDATE SET position=excluded.position,updated_at=excluded.updated_at", (consumer_id, session_id, position, now_ms()))
            event(db, "message.read", project_id=project_id, session_id=session_id, data={"consumer_id": consumer_id, "position": position})
            return {"position": position, "summary": reading.summary(db, session_id, project_id, consumer_id)}, True
        value, changed = await self.host.store.call(write, transaction=True)
        if changed:
            self.host.notify()
        return value

    async def unread(self, session_id: str, consumer_id: str, *, project_id: str):
        consumer_id = reading.reader(consumer_id)
        def read(db):
            require(need(db, "sessions", session_id)["project_id"] == project_id, "PERMISSION_DENIED", "Session belongs to another project")
            cursor = one(db, "SELECT position FROM read_positions WHERE consumer_id=? AND session_id=?", (consumer_id, session_id))
            position = cursor["position"] if cursor else 0
            return {"position": position, "count": one(db, "SELECT COUNT(*) AS count FROM messages WHERE session_id=? AND position>?", (session_id, position))["count"]}
        return await self.host.store.call(read)
