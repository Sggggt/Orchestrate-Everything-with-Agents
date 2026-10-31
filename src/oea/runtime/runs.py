"""Explicit work intents, inspection, waits, cancellation, and continuation."""

import asyncio
import hashlib
import json
from oea.contracts.execution import ResumeExecutionLimits, agent_steps, resume_limits
from oea.contracts.paging import ResultOffset, ResponseLimit
from pathlib import Path
from typing import Literal


from oea.catalog.service import need
from oea.contracts.configuration import AgentDefinition, Budget, ContextPolicy, Execution, Instructions, Tools, Workspace, manager_snapshot
from oea.contracts.domain import Artifact, RunHandle, RunResult, RunSpec
from oea.contracts.measurement import content_view
from oea.contracts.errors import RuntimeFault, require
from oea.storage.persistence import encode, event, new_id, now_ms, one, rows


class Runs:
    def __init__(self, host):
        self.host = host

    def _submit(self, db, spec, *, workflow_run_id=None, source_message_id=None, model_probe=False):
        """Join acceptance to an existing message or workflow transaction."""
        identity = new_id("run")
        intent = {"spec": spec.model_dump(mode="json", exclude_defaults=True, exclude={"display"}), "workflow_run_id": workflow_run_id, "source_message_id": source_message_id}
        if model_probe:
            intent["model_probe"] = True
        fingerprint = hashlib.sha256(encode(intent).encode("utf-8")).hexdigest()
        instance = need(db, "instances", spec.instance_id)
        previous = one(db, "SELECT * FROM runs WHERE instance_id=? AND idempotency_key=?", (spec.instance_id, spec.idempotency_key))
        if previous:
            require(previous["input_hash"] == fingerprint, "IDEMPOTENCY_CONFLICT", "Run key has different input or ownership")
            return previous, True
        need(db, "projects", instance["project_id"])
        self.host.assert_project_available(instance["project_id"])
        definition = AgentDefinition.model_validate_json(need(db, "agent_definitions", instance["agent_id"])["config_json"])
        require(definition.enabled, "AGENT_DISABLED", "Agent is disabled")
        session = need(db, "sessions", instance["session_id"])
        require(not session["archived"], "SESSION_ARCHIVED", "Restore the session before accepting new work")
        require(definition.project_id in {None, instance["project_id"]}, "PROJECT_MISMATCH", "Agent belongs to another project")
        workspace = definition.workspace
        if workspace.project is None:
            project = need(db, "projects", instance["project_id"])
            workspace = workspace.model_copy(update={"project": Path(project["folder"]) if project["folder"] else None})
        snapshot = definition.model_copy(update={"workspace": workspace})
        if instance["role"] == "manager":
            require(session["mode"] == "collaborative" and definition.manager is not None, "MANAGER_UNAVAILABLE", "Manager is not enabled")
            snapshot = manager_snapshot(definition)
        elif not model_probe:
            snapshot = self.host.skills.pin(db, snapshot, instance["project_id"])
        if model_probe:
            snapshot = snapshot.model_copy(update={
                "model": snapshot.model.model_copy(update={"max_output_tokens": min(snapshot.model.max_output_tokens, 128)}),
                "instructions": Instructions(system="Reply READY to verify the model connection."), "tools": Tools(), "skills": (),
                "workspace": Workspace(), "context": ContextPolicy(input_tokens=2048),
                "execution": Execution(budget=Budget(max_model_calls=1, max_input_tokens=2048, max_output_tokens=128, max_tool_calls=1,
                                                     max_elapsed_seconds=300))})
        parent = need(db, "runs", spec.parent_run_id) if spec.parent_run_id else None
        if parent:
            parent_instance = need(db, "instances", parent["instance_id"])
            require(parent_instance["project_id"] == instance["project_id"] and parent_instance["session_id"] == instance["session_id"],
                    "PROJECT_MISMATCH", "Child work must belong to the parent's project and session")
            require(parent["cancel_requested_at"] is None, "RUN_CANCELLED", "Parent work was cancelled")
            budget_id, root_run_id, root_workflow_run_id = parent["budget_id"], parent["root_run_id"], parent["root_workflow_run_id"]
            workflow_run_id = workflow_run_id or parent["workflow_run_id"]
        elif workflow_run_id:
            workflow = need(db, "workflow_runs", workflow_run_id)
            budget_id, root_run_id, root_workflow_run_id = workflow["budget_id"], workflow["root_run_id"], workflow["root_workflow_run_id"]
        else:
            root_run_id, root_workflow_run_id = identity, None
            budget_id = spec.budget_id or self.host.budgets._create(db, snapshot.execution.budget, identity, project_id=instance["project_id"], session_id=instance["session_id"])
        require(spec.budget_id in {None, budget_id}, "BUDGET_MISMATCH", "Work inherits its original cost budget")
        if spec.budget_id:
            self.host.budgets.require_scope(db, spec.budget_id, project_id=instance["project_id"], session_id=instance["session_id"])
        if workflow_run_id:
            workflow = need(db, "workflow_runs", workflow_run_id)
            require(workflow["session_id"] == instance["session_id"] and workflow["cancel_requested_at"] is None and workflow["state"] in {"accepted", "running", "waiting"},
                    "WORKFLOW_MISMATCH", "Workflow ownership is unavailable or belongs to another session")
            require((budget_id, root_run_id, root_workflow_run_id) == (workflow["budget_id"], workflow["root_run_id"], workflow["root_workflow_run_id"]),
                    "WORKFLOW_MISMATCH", "Work and workflow have different root ownership")
        if parent and root_run_id:
            require(need(db, "runs", root_run_id)["cancel_requested_at"] is None, "RUN_CANCELLED", "Root work was cancelled")
        if root_workflow_run_id:
            require(need(db, "workflow_runs", root_workflow_run_id)["cancel_requested_at"] is None, "RUN_CANCELLED", "Root workflow was cancelled")
        if source_message_id:
            source = need(db, "messages", source_message_id)
            require(source["session_id"] == instance["session_id"] and source["purpose"] == "request", "MESSAGE_MISMATCH", "Run input must reference its accepted request")
        db.execute("INSERT INTO runs(id,instance_id,root_run_id,root_workflow_run_id,parent_run_id,workflow_run_id,budget_id,state,input_json,idempotency_key,input_hash,model_role,created_at,updated_at,config_json,source_message_id) VALUES(?,?,?,?,?,?,?,'accepted',?,?,?,?,?,?,?,?)",
                   (identity, spec.instance_id, root_run_id, root_workflow_run_id, spec.parent_run_id, workflow_run_id, budget_id,
                    encode(self.host.redact(spec.input)), spec.idempotency_key, fingerprint, instance["role"], now_ms(), now_ms(), snapshot.model_dump_json(), source_message_id))
        event(db, "run.accepted", project_id=instance["project_id"], session_id=instance["session_id"], instance_id=instance["id"], run_id=identity,
              data={"root_run_id": root_run_id, "root_workflow_run_id": root_workflow_run_id, "workflow_run_id": workflow_run_id, "budget_id": budget_id,
                    **({"purpose": "model_probe"} if model_probe else {})})
        self.host.artifacts.assign(db, identity, instance, spec.artifact_ids, parent)
        db.execute("UPDATE runs SET policy_json=?,artifact_count=?,display_json=? WHERE id=?", (spec.policy.model_dump_json(), len(spec.artifact_ids), encode(self.host.redact(spec.display.model_dump(mode="json"))), identity))
        return need(db, "runs", identity), False

    async def submit(self, spec: RunSpec, *, workflow_run_id: str | None = None):
        self.host.assert_open()
        spec = RunSpec.model_validate(spec)
        row, reused = await self.host.store.call(lambda db: self._submit(db, spec, workflow_run_id=workflow_run_id), transaction=True)
        self.host.launch(row["id"])
        self.host.notify()
        return RunHandle(id=row["id"], instance_id=row["instance_id"], root_run_id=row["root_run_id"], root_workflow_run_id=row["root_workflow_run_id"], budget_id=row["budget_id"], state=row["state"], reused=reused)

    async def get(self, run_id: str):
        return self.observe(await self.host.store.call(lambda db: self.read(db, run_id), transaction="read"))

    def observe(self, value):
        # Transient task dictionaries belong to the event-loop thread. A saved
        # stop boundary needs no scan of unrelated live tool actions.
        value["in_flight"] = value["stopped_at"] is None and (value["id"] in self.host.models.streams.active or self.host.tools.has_inflight(value["id"]))
        value["stop_confirmed"] = value["stopped_at"] is not None and (value["cancel_requested_at"] is None or value.get("cancellation", {}).get("state") == "confirmed")
        return value

    def read(self, db, run_id):
        row = need(db, "runs", run_id)
        value = self.view(row)
        if row["cancel_requested_at"] is not None:
            def scope(db):
                values = rows(db, "WITH RECURSIVE owned(id) AS (SELECT ? UNION ALL SELECT r.id FROM runs r JOIN owned o ON r.parent_run_id=o.id) SELECT r.state,COUNT(*) AS count FROM runs r JOIN owned o ON o.id=r.id GROUP BY r.state", (run_id,))
                counts = {item["state"]: item["count"] for item in values}
                return {"state": "needs_review" if counts.get("needs_review") else "requested" if any(counts.get(state) for state in ("accepted", "running", "waiting")) else "confirmed", "runs": counts}
            value["cancellation"] = scope(db)
        return value

    @staticmethod
    def view(row):
        return {key: row[key] for key in ("id", "instance_id", "root_run_id", "root_workflow_run_id", "parent_run_id", "workflow_run_id", "budget_id", "state", "model_role", "cancel_requested_at", "stopped_at", "created_at", "updated_at")} | {
            "error": json.loads(row["error_json"]) if row["error_json"] else None,
            "wait": json.loads(row["wait_json"]) if row["wait_json"] else None,
            "result_available": row["result_json"] is not None,
            "display": json.loads(row["display_json"]),
        }

    async def list(self, session_id: str, *, project_id: str, cursor: int = 0, limit: int = 25, instance_id: str | None = None):
        require(0 <= cursor < 2**63 and 1 <= limit <= 100, "INVALID_REQUEST", "Read a bounded run page")
        def read(db):
            require(need(db, "sessions", session_id)["project_id"] == project_id, "PERMISSION_DENIED", "Session belongs to another project")
            if instance_id:
                require(need(db, "instances", instance_id)["session_id"] == session_id, "PERMISSION_DENIED", "Instance belongs to another session")
            return rows(db, "SELECT r.rowid AS position,r.*,i.name AS instance_name,i.agent_id,substr(COALESCE(m.body,json_extract(r.input_json,'$')),1,240) AS goal FROM runs r JOIN instances i ON i.id=r.instance_id LEFT JOIN messages m ON m.id=r.source_message_id WHERE i.session_id=? AND (? IS NULL OR i.id=?) AND (?=0 OR r.rowid<?) ORDER BY r.rowid DESC LIMIT ?", (session_id, instance_id, instance_id, cursor, cursor, limit + 1))
        values = await self.host.store.call(read)
        return {"items": [{**self.observe(self.view(row)), **{key: row[key] for key in ("position", "instance_name", "agent_id", "goal")}} for row in values[:limit]],
            "cursor": values[limit - 1]["position"] if len(values) > limit else None}

    async def inspect(self, run_id: str):
        return self.observe(await self.host.store.call(lambda db: self.read_inspection(db, run_id), transaction="read"))

    def read_inspection(self, db, run_id):
        run = self.read(db, run_id)
        actions = rows(db, "SELECT a.id,a.name,a.state,a.side_effects,a.error_json,a.result_json IS NOT NULL AS result_available,json_extract(a.input_json,'$.attempt') AS attempt_facts,json_extract(a.result_json,'$.content_view') AS content_view,(SELECT COUNT(*) FROM model_failures f WHERE f.action_id=a.id) AS archived_count FROM actions a WHERE a.run_id=? ORDER BY a.rowid DESC LIMIT 25", (run_id,))
        rejected = rows(db, "SELECT position,data_json FROM events WHERE run_id=? AND kind='tool.rejected' ORDER BY position DESC LIMIT 25", (run_id,))
        activity = one(db, "SELECT * FROM run_activity WHERE run_id=?", (run_id,))
        projection = one(db, "SELECT id,json_extract(input_json,'$.projection') AS projection,json_extract(result_json,'$.input_comparison') AS comparison FROM actions WHERE run_id=? AND name='model.complete' AND json_extract(input_json,'$.purpose')='model' ORDER BY rowid DESC LIMIT 1", (run_id,))
        return {**run, "actions": [{"id": row["id"], "name": row["name"], "state": row["state"],
                                   "side_effects": row["side_effects"], "error": json.loads(row["error_json"]) if row["error_json"] else None, "result_available": bool(row["result_available"]), "archived_count": row["archived_count"], "attempt_facts": json.loads(row["attempt_facts"]) if row["attempt_facts"] else None, "content_view": json.loads(row["content_view"]) if row["content_view"] else None} for row in actions],
                "rejections": [{"position": row["position"], **json.loads(row["data_json"])} for row in rejected],
                "activity": {"stalled_model_calls": activity["stalled_model_calls"], "blocker": json.loads(activity["blocker_json"]) if activity["blocker_json"] else None, "blocker_repeats": activity["blocker_repeats"]} if activity else None,
                "model_input": {"action_id": projection["id"], "projection": json.loads(projection["projection"] or "{}"), "comparison": json.loads(projection["comparison"] or "null")} if projection else None,
                "artifacts": self.host.artifacts.list_for_run(db, run_id), "budget": self.host.budgets.read(db, run["budget_id"])}

    async def response(self, run_id: str, *, action_id: str | None = None, attempt_id: str | None = None,
                       version: str | None = None, offset: ResultOffset = 0, limit: ResponseLimit = 16384):
        """Read one attempt's versioned live text or retained receipt; never resend it."""
        require(type(offset) is int and 0 <= offset < 2**63 and type(limit) is int and 1 <= limit <= 16384,
                "INVALID_REQUEST", "Use a bounded response page")
        require(not version or action_id and attempt_id, "INVALID_REQUEST", "Versioned reads require the original action and attempt")
        def locate(db):
            need(db, "runs", run_id)
            action = one(db, "SELECT id,state,input_json,result_json FROM actions WHERE run_id=? AND name='model.complete' "
                         "AND (? IS NULL OR id=?) AND json_extract(input_json,'$.purpose')='model' ORDER BY rowid DESC LIMIT 1", (run_id, action_id, action_id))
            return action
        action = await self.host.store.call(locate)
        if not action:
            require(action_id is None and attempt_id is None, "NOT_FOUND", "Model response does not belong to this run")
            return {"run_id": run_id, "available": False, "status": "pending", "text": "", "next_offset": None}
        action_id = action["id"]
        metadata = json.loads(action["input_json"])
        attempt_id = attempt_id or metadata.get("attempt", {}).get("attempt_id")
        live = self.host.models.streams.read(run_id, action_id, attempt_id, version)
        if live is not None:
            value = {key: item for key, item in live.items() if key != "text"}
            text = live["text"]
        else:
            def retained(db):
                record = one(db, "SELECT input_json,result_json FROM model_failures WHERE action_id=? AND id=?", (action_id, attempt_id))
                if record is None:
                    require(metadata.get("attempt", {}).get("attempt_id") == attempt_id, "NOT_FOUND", "Model attempt does not belong to this action")
                    record = action
                started = one(db, "SELECT position FROM events WHERE run_id=? AND kind='model.started' AND json_extract(data_json,'$.attempt_id')=? ORDER BY position DESC LIMIT 1", (run_id, attempt_id))
                return record, started["position"] if started else 0
            record, position = await self.host.store.call(retained)
            result = json.loads(record["result_json"] or "{}")
            text = result.get("message", {}).get("content") or ""
            saved_version = "saved:" + str(attempt_id)
            require(version in {None, saved_version}, "SOURCE_VERSION_CHANGED", "Read a retained version of the original attempt")
            value = {"run_id": run_id, "action_id": action_id, "attempt_id": attempt_id, "attempt_position": position,
                     "block_id": "content", "version": saved_version if record["result_json"] else None,
                     "sequence": None, "status": "saved" if record["result_json"] else "unavailable",
                     "unsaved_gap": record["result_json"] is None}
        require(not self.host.vault.contains_secret(text), "SOURCE_VERSION_UNAVAILABLE", "Response no longer satisfies the current secret policy")
        require(offset <= len(text), "INVALID_REQUEST", "Response offset is beyond the retained text")
        return {**value, "available": True, "text": text[offset:offset + limit], "characters": len(text),
                "offset": offset, "offset_unit": "unicode_codepoint", "next_offset": offset + limit if offset + limit < len(text) else None}

    async def action(self, action_id: str, *, offset: int = 0, limit: int = 8192, attempt_id: str | None = None):
        require(0 <= offset < 2**63 and 1 <= limit <= 16384 and (attempt_id is None or 0 < len(attempt_id) <= 256), "INVALID_REQUEST", "Use a bounded action result page")
        def read(db):
            if attempt_id is None:
                return one(db, "SELECT a.id,a.run_id,a.name,a.state,a.error_json,substr(a.result_json,?,?) AS text,length(a.result_json) AS characters,"
                    "json_extract(a.input_json,'$.attempt') AS attempt_facts,json_extract(a.result_json,'$.content_view') AS content_view,(SELECT u.id FROM usage u WHERE u.action_id=a.id AND u.kind='model' ORDER BY u.created_at DESC,u.rowid DESC LIMIT 1) AS attempt_id,1 AS is_current FROM actions a WHERE a.id=?", (offset + 1, limit, action_id))
            payload = "CASE WHEN f.id IS NOT NULL THEN f.result_json ELSE a.result_json END"
            return one(db, "SELECT a.id,a.run_id,a.name,CASE WHEN f.id IS NOT NULL THEN 'failed' ELSE a.state END AS state,"
                "CASE WHEN f.id IS NOT NULL THEN f.error_json ELSE a.error_json END AS error_json,substr(" + payload + ",?,?) AS text,length(" + payload + ") AS characters,"
                "json_extract(COALESCE(f.input_json,a.input_json),'$.attempt') AS attempt_facts,json_extract(" + payload + ",'$.content_view') AS content_view,COALESCE(f.id,u.id) AS attempt_id,f.id IS NULL AS is_current FROM actions a LEFT JOIN model_failures f ON f.action_id=a.id AND f.id=? "
                "LEFT JOIN usage u ON u.action_id=a.id AND u.id=? AND u.kind='model' WHERE a.id=? AND (f.id IS NOT NULL OR u.id="
                "(SELECT v.id FROM usage v WHERE v.action_id=a.id AND v.kind='model' ORDER BY v.created_at DESC,v.rowid DESC LIMIT 1))",
                (offset + 1, limit, attempt_id, attempt_id, action_id))
        value = await self.host.store.call(read)
        require(value is not None, "NOT_FOUND", "Action does not exist")
        next_offset = offset + limit if value["characters"] is not None and offset + limit < value["characters"] else None
        original = json.loads(value["content_view"]) if value["content_view"] else {}
        return {key: value[key] for key in ("id", "run_id", "name", "state", "text")} | {
            "attempt_id": value["attempt_id"], "is_current": bool(value["is_current"]), "offset_unit": "unicode_codepoint",
            "attempt_facts": json.loads(value["attempt_facts"]) if value["attempt_facts"] else None,
            "error": json.loads(value["error_json"]) if value["error_json"] else None, "next_offset": next_offset,
            "content_view": content_view("page", integrity=original.get("integrity", "unknown"), source_complete=original.get("source_complete"),
                source_ref={"action_id": action_id, "attempt_id": value["attempt_id"]}, offset=offset,
                length=len(value["text"] or ""), next_offset=next_offset)}

    async def attempts(self, action_id: str, project_id: str, *, cursor: int = 0, limit: int = 25):
        """Read immutable failed-model attempt metadata without calling a model."""
        require(0 <= cursor < 2**63 and 1 <= limit <= 100, "INVALID_REQUEST", "Read a bounded attempt page")
        def read(db):
            action = one(db, "SELECT a.id,a.run_id,a.name,i.project_id FROM actions a JOIN runs r ON r.id=a.run_id JOIN instances i ON i.id=r.instance_id WHERE a.id=?", (action_id,))
            require(action is not None and action["project_id"] == project_id, "PERMISSION_DENIED", "Model action belongs to another project")
            require(action["name"] == "model.complete", "INVALID_REQUEST", "Only model actions have this attempt history")
            values = rows(db, "SELECT rowid AS position,id AS attempt_id,input_hash,error_json,result_json IS NOT NULL AS result_available,created_at,json_extract(input_json,'$.attempt') AS attempt_facts FROM model_failures WHERE action_id=? AND (?=0 OR rowid<?) ORDER BY rowid DESC LIMIT ?", (action_id, cursor, cursor, limit + 1))
            current = one(db, "SELECT u.id AS attempt_id,a.state,a.result_json IS NOT NULL AS result_available,json_extract(a.input_json,'$.attempt') AS attempt_facts FROM usage u JOIN actions a ON a.id=u.action_id WHERE u.action_id=? AND u.kind='model' ORDER BY u.created_at DESC,u.rowid DESC LIMIT 1", (action_id,))
            if current:
                current["result_available"] = bool(current["result_available"])
                current["attempt_facts"] = json.loads(current["attempt_facts"]) if current["attempt_facts"] else None
            return {"action_id": action_id, "run_id": action["run_id"], "current": current,
                "items": [{key: value[key] for key in ("position", "attempt_id", "input_hash", "created_at")} | {"state": "failed", "result_available": bool(value["result_available"]), "attempt_facts": json.loads(value["attempt_facts"]) if value["attempt_facts"] else None, "error": json.loads(value["error_json"]) if value["error_json"] else None} for value in values[:limit]],
                "cursor": values[limit - 1]["position"] if len(values) > limit else None}
        return await self.host.store.call(read)

    @staticmethod
    def _workflow_outputs(db, run_ids, *, project_id, session_id, workflow_run_id, include_results=True):
        """Read only children owned by a server-selected workflow execution."""
        if not run_ids:
            return []
        require(len(run_ids) <= 128, "INVALID_REQUEST", "A workflow result page has at most 128 members")
        workflow = need(db, "workflow_runs", workflow_run_id)
        require(workflow["session_id"] == session_id and need(db, "sessions", session_id)["project_id"] == project_id,
                "PERMISSION_DENIED", "Workflow result scope does not match its owning project and session")
        placeholders = ",".join("?" for _ in run_ids)
        projection = "r.result_json,COALESCE(json_extract(a.result_json,'$.message.content'),json_extract(a.result_json,'$.text')) AS text,json_extract(a.result_json,'$.data') AS program_data" if include_results else "NULL AS result_json,NULL AS text,NULL AS program_data"
        model_join = " LEFT JOIN actions a ON a.id=json_extract(r.result_json,'$.response_id')" if include_results else ""
        values = rows(db, "SELECT r.id,r.instance_id,r.state,r.error_json," + projection + " FROM runs r JOIN instances i ON i.id=r.instance_id" + model_join +
                      " WHERE i.project_id=? AND i.session_id=? AND r.workflow_run_id=? AND r.id IN (" + placeholders + ")",
                      (project_id, session_id, workflow_run_id, *run_ids))
        indexed = {row["id"]: row for row in values}
        require(all(identity in indexed for identity in run_ids), "PERMISSION_DENIED", "A result is outside this workflow's ownership")
        artifacts = rows(db, "SELECT a.* FROM artifacts a JOIN runs r ON r.id=a.run_id JOIN instances i ON i.id=r.instance_id "
                         "WHERE i.project_id=? AND i.session_id=? AND r.workflow_run_id=? AND r.id IN (" + placeholders + ") ORDER BY a.rowid",
                         (project_id, session_id, workflow_run_id, *run_ids)) if include_results else []
        grouped = {}
        for artifact in artifacts:
            grouped.setdefault(artifact["run_id"], []).append({key: artifact[key] for key in ("id", "name", "sha256", "bytes", "media_type", "run_id", "instance_id")})
        result = []
        for identity in run_ids:
            row = indexed[identity]
            value = {"run_id": identity, "instance_id": row["instance_id"], "state": row["state"],
                     "error": json.loads(row["error_json"]) if row["error_json"] else None}
            if row["state"] == "completed" and include_results:
                saved = json.loads(row["result_json"])
                value.update(text=row["text"] or "", data=json.loads(row["program_data"] or "{}") if saved.get("origin") == "program" else saved["data"], artifacts=grouped.get(identity, []))
            result.append(value)
        return result

    async def result(self, run_id: str, *, timeout: float = 0):
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            self.host.assert_open()
            changed = self.host.run_signal(run_id)
            row = await self.host.store.call(lambda db: need(db, "runs", run_id))
            if row["state"] == "completed":
                self.host.notify_run(run_id)
                value = json.loads(row["result_json"])
                response = await self.host.store.call(lambda db: one(db, "SELECT result_json FROM actions WHERE id=? AND run_id=?", (value["response_id"], run_id)))
                response = json.loads(response["result_json"])
                artifacts = await self.host.artifacts.for_run(run_id)
                return RunResult(text=response["text"] if value.get("origin") == "program" else response["message"].get("content") or "", data=response["data"] if value.get("origin") == "program" else value["data"],
                                 artifacts=tuple(Artifact.model_validate(item) for item in artifacts), finish_reason=value["finish_reason"],
                                 usage=await self.host.budgets.get(row["budget_id"]), content_view=response.get("content_view"))
            if row["state"] in {"failed", "cancelled", "needs_review"}:
                self.host.notify_run(run_id)
                error = json.loads(row["error_json"]) if row["error_json"] else {"code": "RUN_" + row["state"].upper(), "safe_message": "Run did not complete"}
                raise RuntimeFault(**{**error, "run_id": run_id})
            remaining = deadline - asyncio.get_running_loop().time()
            require(remaining > 0, "RESULT_PENDING", "Run has not completed", run_id=run_id, details={"state": row["state"]})
            try:
                await asyncio.wait_for(changed.wait(), remaining)
            except TimeoutError:
                raise RuntimeFault("RESULT_PENDING", "Run has not completed", run_id=run_id) from None

    def _resume(self, db, run_id, idempotency_key, *, input=None, refresh_model=True, refresh_capabilities=True, source_message_id=None, execution_limits=None):
        require(isinstance(idempotency_key, str) and 0 < len(idempotency_key) <= 256, "INVALID_REQUEST", "Resume needs a stable key")
        encoded_input = encode(self.host.redact(input))
        require(len(encoded_input.encode("utf-8")) <= 1024 * 1024, "INVALID_REQUEST", "Resume large inputs by workspace or context reference")
        requested_limits = resume_limits(execution_limits)
        intent = {"input": input, "refresh_model": refresh_model, "refresh_capabilities": refresh_capabilities, "source_message_id": source_message_id}
        if requested_limits:
            intent["execution_limits"] = requested_limits
        fingerprint = hashlib.sha256(encode(intent).encode("utf-8")).hexdigest()
        run = need(db, "runs", run_id)
        previous = one(db, "SELECT * FROM run_resumptions WHERE run_id=? AND idempotency_key=?", (run_id, idempotency_key))
        if previous:
            require(previous["input_hash"] == fingerprint, "IDEMPOTENCY_CONFLICT", "Resume key has different input")
            return run
        require(run["state"] in {"waiting", "failed", "needs_review"} and run["cancel_requested_at"] is None, "RUN_NOT_RESUMABLE", "Choose a waiting or recoverable run")
        require(not one(db, "SELECT 1 FROM actions WHERE run_id=? AND (state='needs_review' OR state='dispatched' AND side_effects!='none') LIMIT 1", (run_id,)),
                "OUTCOME_UNCERTAIN", "Inspect unresolved side effects before resuming")
        instance = need(db, "instances", run["instance_id"])
        if source_message_id:
            message = need(db, "messages", source_message_id)
            require(message["session_id"] == instance["session_id"] and message["purpose"] == "reply", "MESSAGE_MISMATCH", "Reply must belong to the original session")
        snapshot = AgentDefinition.model_validate_json(run["config_json"])
        latest = one(db, "SELECT execution_limits_json FROM run_resumptions WHERE run_id=? ORDER BY rowid DESC LIMIT 1", (run_id,))
        prior_limits = json.loads(latest["execution_limits_json"]).get("effective", {}) if latest else {}
        effective = {"max_steps": prior_limits.get("max_steps", agent_steps(snapshot.execution, json.loads(need(db, "budgets", run["budget_id"])["policy_json"]))), **requested_limits}
        limits_receipt = {"requested": requested_limits, "effective": effective,
                          "checkpoint": (json.loads(run["error_json"]).get("evidence_ref") or {}).get("checkpoint") if run["error_json"] else None}
        if refresh_model or refresh_capabilities:
            current = AgentDefinition.model_validate_json(need(db, "agent_definitions", instance["agent_id"])["config_json"])
            require(instance["role"] != "manager" or current.manager is not None, "MANAGER_UNAVAILABLE", "Manager model was removed")
            changes = {}
            if refresh_model:
                changes["model"] = current.manager.model if instance["role"] == "manager" else current.model
            if refresh_capabilities:
                db.execute("DELETE FROM run_mcp_bindings WHERE run_id=?", (run_id,))
                workspace = current.workspace
                if workspace.project is None:
                    project = need(db, "projects", instance["project_id"])
                    workspace = workspace.model_copy(update={"project": Path(project["folder"]) if project["folder"] else None})
                capability = manager_snapshot(current) if instance["role"] == "manager" else current
                changes.update(instructions=capability.instructions, tools=capability.tools, skills=capability.skills,
                               context=capability.context, workspace=capability.workspace if instance["role"] == "manager" else workspace,
                               communication=capability.communication)
            snapshot = snapshot.model_copy(update=changes)
            if refresh_capabilities and instance["role"] == "main":
                snapshot = self.host.skills.pin(db, snapshot, instance["project_id"])
        db.execute("UPDATE runs SET state='accepted',error_json=NULL,stopped_at=NULL,config_json=?,updated_at=? WHERE id=?", (snapshot.model_dump_json(), now_ms(), run_id))
        waiting = json.loads(run["wait_json"]) if run["wait_json"] else {}
        position = event(db, "run.resume_requested", instance_id=run["instance_id"], run_id=run_id, data={"has_input": input is not None or source_message_id is not None})
        db.execute("INSERT INTO run_resumptions(id,run_id,idempotency_key,input_hash,input_json,interrupt_id,applied_at,created_at,event_position,failure_json,source_message_id,execution_limits_json) VALUES(?,?,?,?,?,?,NULL,?,?,?,?,?)",
                   (new_id("resume"), run_id, idempotency_key, fingerprint, encoded_input, waiting.get("interrupt_id"), now_ms(), position, run["error_json"], source_message_id, encode(limits_receipt)))
        return run

    async def resume(self, run_id: str, idempotency_key: str, *, input: str | dict | None = None, refresh_model: bool = True, refresh_capabilities: bool = True, execution_limits: ResumeExecutionLimits | None = None):
        self.host.assert_open()
        await self.host.store.call(lambda db: self._resume(db, run_id, idempotency_key, input=input,
            refresh_model=refresh_model, refresh_capabilities=refresh_capabilities, execution_limits=execution_limits), transaction=True)
        self.host.reviewing.discard(run_id)
        self.host.launch(run_id)
        self.host.notify_run(run_id)
        self.host.notify()
        return await self.get(run_id)

    async def cancel(self, run_id: str):
        def write(db):
            need(db, "runs", run_id)
            owned = rows(db, "WITH RECURSIVE owned(id) AS (SELECT ? UNION ALL SELECT r.id FROM runs r JOIN owned o ON r.parent_run_id=o.id) SELECT r.id,r.instance_id,r.cancel_requested_at FROM runs r JOIN owned o ON o.id=r.id", (run_id,))
            for run in owned:
                if run["cancel_requested_at"] is None:
                    db.execute("UPDATE runs SET cancel_requested_at=?,updated_at=? WHERE id=?", (now_ms(), now_ms(), run["id"]))
                    event(db, "run.cancel_requested", instance_id=run["instance_id"], run_id=run["id"])
            return owned
        owned = await self.host.store.call(write, transaction=True)
        await asyncio.gather(*(self._cancel_one(row["id"]) for row in reversed(owned)))
        self.host.notify()
        return await self.get(run_id)

    async def confirm_action(self, action_id: str, project_id: str, idempotency_key: str, evidence: str,
                             outcome: Literal["completed", "not_applied"], *, result: dict | None = None):
        """Record a controller's external-effect verification; never retry or resume."""
        self.host.assert_open()
        value = await self.host.tools.confirm(action_id, project_id, idempotency_key, evidence, outcome, result=result)
        self.host.on_action_settle(value["run_id"])
        self.host.notify_run(value["run_id"])
        return value

    async def _cancel_one(self, run_id):
        row = await self.host.store.call(lambda db: need(db, "runs", run_id))
        if row["state"] in {"completed", "failed", "cancelled"}:
            return
        job = self.host.jobs.get(run_id)
        if job:
            job.cancel()
            await asyncio.gather(job, return_exceptions=True)
            current = await self.get(run_id)
            if current["state"] in {"accepted", "running", "waiting"} and not await self.host._unresolved(run_id):
                await self.host.finish(run_id, "cancelled")
        else:
            await self.host.tools.cancel(run_id)
            if not await self.host._unresolved(run_id):
                await self.host.finish(run_id, "cancelled")
