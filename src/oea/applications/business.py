"""Authenticated application operations reuse the existing run and artifact owners."""

import hashlib
import json
from urllib.parse import quote

from oea.catalog.service import need
from oea.applications.service import application_id
from oea.applications.views import public_error, tool_result
from oea.contracts.domain import RunHandle, RunSpec
from oea.contracts.errors import require
from oea.storage.persistence import encode, event, new_id, now_ms, one, rows


def require_conversation(db, app_id, conversation_id):
    row = one(db, "SELECT * FROM application_conversations WHERE id=? AND app_id=?", (conversation_id, app_id))
    require(row is not None, "PERMISSION_DENIED", "Object is not available to this application")
    return row


def require_run(db, app_id, run_id):
    row = one(db, "SELECT c.*,r.id AS run_id FROM runs r JOIN instances i ON i.id=r.instance_id JOIN application_conversations c ON c.session_id=i.session_id WHERE r.id=? AND c.app_id=?", (run_id, app_id))
    require(row is not None, "PERMISSION_DENIED", "Object is not available to this application")
    return row


def require_artifact(db, app_id, artifact_id):
    row = one(db, "SELECT a.*,c.id AS conversation_id,c.app_id FROM artifacts a JOIN application_conversations c ON c.session_id=a.session_id WHERE a.id=? AND c.app_id=?", (artifact_id, app_id))
    require(row is not None, "PERMISSION_DENIED", "Object is not available to this application")
    return row


def accept(db, app_id, operation, target, key, value, write, *, with_reused=False):
    """Save the receipt in the same transaction as the accepted intent."""
    require(isinstance(key, str) and 0 < len(key) <= 256, "INVALID_REQUEST", "Supply a bounded Idempotency-Key")
    application = need(db, "applications", app_id)
    require(application["enabled"] and not application["removed"], "PERMISSION_DENIED", "Application access was revoked")
    fingerprint = hashlib.sha256(encode(value).encode()).hexdigest()
    previous = one(db, "SELECT * FROM application_requests WHERE app_id=? AND operation=? AND target=? AND idempotency_key=?", (app_id, operation, target, key))
    if previous:
        require(previous["input_hash"] == fingerprint, "IDEMPOTENCY_CONFLICT", "Idempotency key has different parameters")
        result = json.loads(previous["result_json"])
        return (result, True) if with_reused else result
    result = write()
    db.execute("INSERT INTO application_requests(app_id,operation,target,idempotency_key,input_hash,result_json,created_at) VALUES(?,?,?,?,?,?,?)", (app_id, operation, target, key, fingerprint, encode(result), now_ms()))
    return (result, False) if with_reused else result


class ApplicationBusiness:
    def __init__(self, host):
        self.host = host
        from oea.applications.files import ApplicationFiles
        self.files = ApplicationFiles(self)

    @staticmethod
    def conversation_view(row):
        return {key: row[key] for key in ("id", "binding_id", "name", "instance_id", "closed_at", "created_at")}

    async def agents(self, app_id, *, cursor=0, limit=25):
        require(0 <= cursor < 2**63 and 1 <= limit <= 100, "INVALID_REQUEST", "Use a bounded Agent page")
        values = await self.host.store.call(lambda db: rows(db,
            "SELECT b.rowid AS position,b.id,b.revision,b.agent_id,json_extract(a.config_json,'$.name') AS name,json_extract(a.config_json,'$.description') AS description "
            "FROM application_bindings b JOIN agent_definitions a ON a.id=b.agent_id WHERE b.app_id=? AND b.enabled=1 "
            "AND json_extract(a.config_json,'$.enabled')=1 AND COALESCE(json_extract(a.config_json,'$.removed'),0)=0 AND b.rowid>? ORDER BY b.rowid LIMIT ?",
            (app_id, cursor, limit + 1)))
        return {"items": values[:limit], "cursor": values[limit - 1]["position"] if len(values) > limit else None}

    async def create_conversation(self, app_id, request, key):
        def write(db):
            def create():
                binding = one(db, "SELECT b.* FROM application_bindings b JOIN agent_definitions a ON a.id=b.agent_id "
                              "WHERE b.id=? AND b.app_id=? AND b.enabled=1 AND json_extract(a.config_json,'$.enabled')=1 "
                              "AND COALESCE(json_extract(a.config_json,'$.removed'),0)=0", (request.binding_id, app_id))
                require(binding is not None, "PERMISSION_DENIED", "Agent binding is not available to this application")
                self.host.assert_project_available(binding["project_id"])
                session = self.host.sessions._create(db, binding["project_id"], request.name, source="application")
                identity = application_id("conv")
                db.execute("INSERT INTO application_conversations(id,app_id,binding_id,session_id,name,created_at) VALUES(?,?,?,?,?,?)",
                           (identity, app_id, request.binding_id, session["id"], request.name, now_ms()))
                return self.conversation_view(require_conversation(db, app_id, identity))
            return accept(db, app_id, "conversations.create", request.binding_id, key, request.model_dump(mode="json"), create)
        value = await self.host.store.call(write, transaction=True)
        self.host.notify()
        return value

    async def conversations(self, app_id, *, cursor=0, limit=25):
        require(0 <= cursor < 2**63 and 1 <= limit <= 100, "INVALID_REQUEST", "Use a bounded conversation page")
        values = await self.host.store.call(lambda db: rows(db, "SELECT rowid AS position,* FROM application_conversations WHERE app_id=? AND rowid>? ORDER BY rowid LIMIT ?", (app_id, cursor, limit + 1)))
        return {"items": [self.conversation_view(row) for row in values[:limit]], "cursor": values[limit - 1]["position"] if len(values) > limit else None}

    async def conversation(self, app_id, conversation_id):
        return await self.host.store.call(lambda db: self.conversation_view(require_conversation(db, app_id, conversation_id)))

    async def submit(self, app_id, conversation_id, request, key):
        await self.files.materialize(app_id, conversation_id, request.artifact_ids)
        def write(db):
            def submit():
                conversation = require_conversation(db, app_id, conversation_id)
                binding = need(db, "application_bindings", conversation["binding_id"])
                instance_id = conversation["instance_id"]
                if instance_id is None:
                    require(conversation["closed_at"] is None, "CONVERSATION_CLOSED", "Conversation is closed to new work")
                    instance = self.host.instances._create(db, conversation["session_id"], binding["agent_id"], name="Application")
                    instance_id = instance["id"]
                    db.execute("UPDATE application_conversations SET instance_id=? WHERE id=?", (instance_id, conversation_id))
                spec = RunSpec(instance_id=instance_id, idempotency_key=key, **request.model_dump())
                row, reused = self.host.runs._submit(db, spec)
                return RunHandle(id=row["id"], instance_id=instance_id, root_run_id=row["root_run_id"], root_workflow_run_id=row["root_workflow_run_id"], budget_id=row["budget_id"], state=row["state"], reused=reused).model_dump(mode="json")
            return accept(db, app_id, "runs.submit", conversation_id, key, request.model_dump(mode="json"), submit, with_reused=True)
        value, reused = await self.host.store.call(write, transaction=True)
        handle = RunHandle.model_validate({**value, "reused": reused or value["reused"]})
        self.host.launch(handle.id)
        self.host.notify()
        return handle

    async def close_conversation(self, app_id, conversation_id, key):
        def write(db):
            conversation = require_conversation(db, app_id, conversation_id)
            def close():
                db.execute("UPDATE application_conversations SET closed_at=COALESCE(closed_at,?) WHERE id=?", (now_ms(), conversation_id))
                event(db, "application.conversation_closed", session_id=conversation["session_id"])
                return self.conversation_view(require_conversation(db, app_id, conversation_id))
            value = accept(db, app_id, "conversations.close", conversation_id, key, {}, close)
            pending = rows(db, "SELECT r.id,r.state FROM runs r JOIN instances i ON i.id=r.instance_id WHERE i.session_id=? AND r.state IN ('accepted','running','waiting','needs_review')", (conversation["session_id"],))
            return {**value, "unfinished_runs": pending}
        value = await self.host.store.call(write, transaction=True)
        self.host.notify()
        return value

    async def history(self, app_id, conversation_id, *, after=0, limit=25, version=None):
        require(0 <= after < 2**63 and 1 <= limit <= 100 and (version is None or 0 <= version < 2**63), "INVALID_REQUEST", "Use a bounded history page")
        def read(db):
            conversation = require_conversation(db, app_id, conversation_id)
            visible = "(input_run_id IS NOT NULL OR resumption_id IS NOT NULL OR role IN ('assistant','tool'))"
            boundary = version
            if boundary is None:
                boundary = one(db, "SELECT COALESCE(MAX(position),0) AS position FROM conversation WHERE instance_id=? AND " + visible, (conversation["instance_id"],))["position"]
            else:
                require(boundary == 0 or one(db, "SELECT 1 FROM conversation WHERE instance_id=? AND position=? AND " + visible, (conversation["instance_id"], boundary)),
                        "SOURCE_VERSION_UNAVAILABLE", "History version is not available in this conversation")
            values = rows(db, "SELECT c.id,c.position,c.run_id,c.role,c.action_id,c.tool_call_id,a.name,a.state,"
                "CASE WHEN c.input_run_id IS NOT NULL THEN 'input' WHEN c.resumption_id IS NOT NULL THEN 'resume' END AS input_kind,"
                "CASE WHEN c.input_run_id IS NOT NULL THEN r.input_json WHEN c.resumption_id IS NOT NULL THEN x.input_json "
                "WHEN c.role='assistant' THEN COALESCE(c.body_json,json_extract(a.result_json,'$.message'),json_object('role','assistant','content',json_extract(a.result_json,'$.text'))) "
                "WHEN c.action_id IS NULL THEN c.body_json END AS body_json "
                "FROM conversation c LEFT JOIN actions a ON a.id=c.action_id LEFT JOIN runs r ON r.id=c.input_run_id "
                "LEFT JOIN run_resumptions x ON x.id=c.resumption_id WHERE c.instance_id=? AND c.position>? AND c.position<=? "
                "AND (c.input_run_id IS NOT NULL OR c.resumption_id IS NOT NULL OR c.role IN ('assistant','tool')) ORDER BY c.position LIMIT ?",
                (conversation["instance_id"], after, boundary, limit + 1))
            items = []
            for row in values[:limit]:
                if row["input_kind"]:
                    body = {"role": "user", "content": json.loads(row["body_json"]), "kind": row["input_kind"]}
                elif row["role"] == "assistant":
                    original = json.loads(row["body_json"])
                    body = {key: original[key] for key in ("role", "content", "tool_calls") if key in original}
                else:
                    body = {"role": "tool", "tool_call_id": row["tool_call_id"], "action_id": row["action_id"], "name": row["name"], "state": row["state"] or "rejected"}
                    if row["body_json"]:
                        original = json.loads(row["body_json"])
                        value = json.loads(original["content"])
                        if value.get("error"):
                            body["error"] = public_error(value["error"])
                items.append({"id": row["id"], "position": row["position"], "run_id": row["run_id"], "body": body})
            return {"items": items, "version": boundary, "cursor": values[limit - 1]["position"] if len(values) > limit else None}
        return await self.host.store.call(read)

    async def response(self, app_id, run_id, *, action_id=None, attempt_id=None, version=None, offset=0, limit=8192):
        await self.host.store.call(lambda db: require_run(db, app_id, run_id))
        return await self.host.runs.response(run_id, action_id=action_id, attempt_id=attempt_id, version=version, offset=offset, limit=limit)

    async def actions(self, app_id, run_id, *, cursor=0, limit=25):
        require(0 <= cursor < 2**63 and 1 <= limit <= 100, "INVALID_REQUEST", "Use a bounded action page")
        def read(db):
            require_run(db, app_id, run_id)
            return rows(db, "SELECT rowid AS position,id,name,state,side_effects,result_json IS NOT NULL AS result_available,created_at,updated_at FROM actions WHERE run_id=? AND rowid>? ORDER BY rowid LIMIT ?", (run_id, cursor, limit + 1))
        values = await self.host.store.call(read)
        return {"items": values[:limit], "cursor": values[limit - 1]["position"] if len(values) > limit else None}

    async def action(self, app_id, run_id, action_id, *, json_pointer="", version=None):
        from oea.storage.result_refs import pointer
        require(isinstance(json_pointer, str) and len(json_pointer) <= 512, "INVALID_REQUEST", "Use a bounded result JSON Pointer")
        def read(db):
            require_run(db, app_id, run_id)
            row = one(db, "SELECT id,run_id,name,state,side_effects,result_json,error_json,updated_at FROM actions WHERE id=? AND run_id=?", (action_id, run_id))
            require(row is not None, "PERMISSION_DENIED", "Action is not available in this application Run")
            resolution = one(db, "SELECT outcome,evidence,created_at FROM action_resolutions WHERE action_id=?", (action_id,))
            return row, resolution
        row, resolution = await self.host.store.call(read)
        digest = hashlib.sha256()
        for item in (row["state"], row["result_json"], row["error_json"], encode(resolution)):
            digest.update((item or "").encode())
            digest.update(b"\0")
        content_version = digest.hexdigest()
        require(version is None or version == content_version, "SOURCE_VERSION_CHANGED", "The original action receipt has a newer verification or result")
        result = tool_result(row["name"], json.loads(row["result_json"])) if row["result_json"] is not None and row["name"] != "model.complete" else None
        return {key: row[key] for key in ("id", "run_id", "name", "state", "side_effects", "updated_at")} | {
            "result": pointer(result, json_pointer), "json_pointer": json_pointer, "version": content_version,
            "error": public_error(json.loads(row["error_json"])) if row["error_json"] else None,
            "resolution": {**resolution, "source": "external_verification"} if resolution else None}

    async def confirm_action(self, app_id, run_id, action_id, request, key):
        def check(db):
            owner = require_run(db, app_id, run_id)
            require(one(db, "SELECT 1 FROM actions WHERE id=? AND run_id=?", (action_id, run_id)), "PERMISSION_DENIED", "Action is not available in this application Run")
            return need(db, "sessions", owner["session_id"])["project_id"]
        project_id = await self.host.store.call(check)
        return await self.host.runs.confirm_action(action_id, project_id, key, **request.model_dump())

    async def run(self, app_id, run_id):
        await self.host.store.call(lambda db: require_run(db, app_id, run_id))
        value = await self.host.runs.get(run_id)
        value["usage"] = await self.host.store.call(lambda db: self.host.budgets.scopes(db, need(db, "runs", run_id)))
        return value

    async def result(self, app_id, run_id, *, offset=0, limit=None):
        await self.host.store.call(lambda db: require_run(db, app_id, run_id))
        require(0 <= offset < 2**63 and (limit is None or 1 <= limit <= 1048576), "INVALID_REQUEST", "Use a bounded result range")
        result = await self.host.runs.result(run_id)
        if offset == 0 and limit is None:
            return result
        require(offset <= len(result.text), "INVALID_REQUEST", "Offset exceeds the original result")
        length = min(limit or 65536, len(result.text) - offset)
        next_offset = offset + length if offset + length < len(result.text) else None
        return {**result.model_dump(mode="json"), "text": result.text[offset:offset + length], "content_view": {
            **(result.content_view or {}), "view": "page", "source_ref": run_id,
            "range": {"offset": offset, "length": length, "unit": "unicode_codepoint"}, "total_characters": len(result.text), "next_offset": next_offset}}

    async def resume(self, app_id, run_id, request, key):
        await self.host.store.call(lambda db: require_run(db, app_id, run_id))
        return await self.host.runs.resume(run_id, key, **request.model_dump())

    async def cancel(self, app_id, run_id, key):
        await self.host.store.call(lambda db: require_run(db, app_id, run_id))
        # Cancellation is already idempotent on the original run and its owned tree.
        require(isinstance(key, str) and 0 < len(key) <= 256, "INVALID_REQUEST", "Supply a bounded Idempotency-Key")
        return await self.host.runs.cancel(run_id)

    async def command(self, app_id, run_id, command_id, *, stdout_offset=0, stderr_offset=0):
        await self.host.store.call(lambda db: require_run(db, app_id, run_id))
        require(stdout_offset >= 0 and stderr_offset >= 0, "INVALID_REQUEST", "Use nonnegative byte offsets")
        record = self.host.command_sessions.load(command_id, run_id)
        result = self.host.command_sessions.result(record, {"stdout_offset": stdout_offset, "stderr_offset": stderr_offset})
        for reference in result["output_refs"]:
            reference["download_url"] = "/api/oap/v1/runs/" + quote(run_id, safe="") + "/commands/" + quote(command_id, safe="") + "/output/" + reference["stream"]
        # Effective paths and host toolchain directories are internal capability
        # material, not part of an application's command output receipt.
        return {key: value for key, value in result.items() if key not in {"cwd", "environment"}}

    async def command_output(self, app_id, run_id, command_id, stream):
        def read(db):
            owner = require_run(db, app_id, run_id)
            return need(db, "sessions", owner["session_id"])["project_id"]
        project = await self.host.store.call(read)
        return await self.host.runs.command_output(run_id, command_id, project, stream)

    async def artifacts(self, app_id, run_id, *, cursor=0, limit=25):
        require(0 <= cursor < 2**63 and 1 <= limit <= 100, "INVALID_REQUEST", "Use a bounded artifact page")
        def read(db):
            require_run(db, app_id, run_id)
            return rows(db, "SELECT rowid AS position,* FROM artifacts WHERE run_id=? AND rowid>? ORDER BY rowid LIMIT ?", (run_id, cursor, limit + 1))
        values = await self.host.store.call(read)
        return {"items": [self.host.artifacts.view(row) for row in values[:limit]], "cursor": values[limit - 1]["position"] if len(values) > limit else None}

    async def events_page(self, app_id, run_id, *, after=0, limit=100):
        require(0 <= after < 2**63 and 1 <= limit <= 250, "INVALID_REQUEST", "Use a bounded event cursor")
        def read(db):
            require_run(db, app_id, run_id)
            return rows(db, "SELECT position,kind,data_json,created_at FROM events WHERE run_id=? AND source='application' AND position>? ORDER BY position LIMIT ?", (run_id, after, limit + 1))
        values = await self.host.store.call(read)
        page = values[:limit]
        return {"items": [{"position": row["position"], "kind": row["kind"], "created_at": row["created_at"], "data": json.loads(row["data_json"])} for row in page],
                "position": page[-1]["position"] if page else after, "cursor": page[-1]["position"] if len(values) > limit else None}
