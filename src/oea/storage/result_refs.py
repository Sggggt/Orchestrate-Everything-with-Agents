"""Durable delivery references to domain-owned data; never replay a write."""

import hashlib
import json
from collections import OrderedDict

from oea.contracts.measurement import content_view
from oea.contracts.errors import require
from oea.contracts.paging import ResultLimit, ResultOffset, ResultPointer
from oea.storage.persistence import encode, now_ms, one
from oea.interfaces.registry import operation_context


def plain(value):
    return value.model_dump(mode="json") if hasattr(value, "model_dump") else value


def pointer(value, path):
    require(not path or path.startswith("/"), "INVALID_REQUEST", "Use a JSON Pointer beginning with /")
    for token in path.split("/")[1:] if path else ():
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict) and token in value:
            value = value[token]
        elif isinstance(value, list) and token.isdecimal() and int(token) < len(value):
            value = value[int(token)]
        else:
            require(False, "POINTER_UNAVAILABLE", "Requested result field is unavailable")
    return value


class ResultReferences:
    inline_bytes = 16384
    snapshot_operations = frozenset({
        "runs.get", "runs.list", "runs.inspect", "runs.action", "runs.attempts", "runs.response", "runs.result",
        "budgets.get", "sessions.get", "sessions.list", "sessions.summary", "sessions.unread",
        "instances.get", "instances.list", "instances.history", "messages.get", "messages.list", "messages.unread",
        "artifacts.get", "artifacts.list", "artifacts.for_run", "artifacts.get_upload", "events.list",
        "workflows.get_run", "workflows.list_runs", "workflows.inspect", "workflows.node_result",
        "workflows.members", "workflows.result", "workflows.activity", "mcp.call_result", "mcp.calls",
        "history.summary", "history.list", "history.get", "history.conversation", "history.usage",
    })
    versioned_operations = frozenset({
        "projects.get", "projects.list", "agents.get", "agents.list", "agents.schema",
        "mcp.get", "mcp.list", "mcp.schema", "mcp.tools", "mcp.codex_candidates",
        "skills.get", "skills.list", "skills.describe", "skills.schema", "skills.read", "skills.files",
        "workflows.get", "workflows.list", "workflows.schema", "workflows.node_types", "workflows.validate",
        "artifacts.read", "host.describe", "host.guide", "executions.graphs",
    })

    def __init__(self, host):
        self.host = host
        self.fields = OrderedDict()
        self.field_bytes = 0
        self.checked_secrets = OrderedDict()

    @staticmethod
    def source(operation, params, result):
        params, overlay, version = dict(params), {}, {}
        if operation in {"agents.save", "mcp.save"}:
            operation, params = ("agents.get", {"agent_id": result["definition"]["id"]}) if operation == "agents.save" else ("mcp.get", {"server_id": result["definition"]["id"]})
        elif operation == "mcp.discover":
            operation, params = "mcp.tools", {"server_id": result["server_id"], "project_id": result["project_id"], "revision": result["revision"]}
        elif operation == "mcp.call":
            operation, params = "mcp.call_result", {"call_id": result["id"], "project_id": result["project_id"], "consumer_id": result["consumer_id"], "version": result["version"]}
            overlay = {"in_flight": result["in_flight"], "stop_confirmed": result["stop_confirmed"]}
        elif operation in {"workflows.create", "workflows.update"}:
            operation, params = "workflows.get", {"workflow_id": result["id"], "project_id": result["project_id"]}
        elif operation == "messages.send":
            operation, params = "messages.get", {"message_id": result["id"], "project_id": result["project_id"]}
            overlay = {"reused": result["reused"]}
        elif operation in {"runs.submit", "runs.resume", "runs.cancel"}:
            operation, params = "runs.get", {"run_id": result["id"]}
            if "reused" in result:
                overlay["reused"] = result["reused"]
        elif operation in {"workflows.run", "workflows.resume", "workflows.cancel"}:
            operation, params = "workflows.get_run", {"workflow_run_id": result["id"], "project_id": result["project_id"]}
            if "reused" in result:
                overlay["reused"] = result["reused"]
        if operation in {"agents.get", "workflows.get", "mcp.get"}:
            version["revision"] = result["revision"]
        if operation == "runs.action" and result.get("attempt_id"):
            params["attempt_id"] = result["attempt_id"]
        if operation == "workflows.node_result":
            params.update(step_id=result["step_id"], version=result["version"])
        if isinstance(result, dict) and isinstance(result.get("items"), list) and result["items"]:
            items = result["items"]
            params["limit"] = len(items)
            positions = [item["position"] for item in items if isinstance(item, dict) and "position" in item]
            if operation == "messages.list" and positions:
                params["before"] = max(positions) + 1
            elif operation in {"runs.list", "workflows.list_runs", "runs.attempts", "mcp.calls"} and positions:
                params["cursor"] = max(positions) + 1
            if "cursor" in result:
                overlay["cursor"] = result["cursor"]
            if operation in {"agents.list", "workflows.list"}:
                version["revisions"] = {item.get("id", (item.get("definition") or {}).get("id")): item["revision"] for item in items}
        if operation in {"agents.schema", "mcp.schema", "workflows.schema", "workflows.node_types", "host.describe", "host.guide", "executions.graphs"}:
            version["content_hash"] = hashlib.sha256(encode(result).encode("utf-8")).hexdigest()
        return operation, params, version, overlay

    @staticmethod
    def project(db, params, result):
        if params.get("project_id"):
            return params["project_id"]
        if isinstance(result, dict):
            if result.get("project_id"):
                return result["project_id"]
            if "definition" in result:
                return result["definition"].get("project_id")
        queries = {
            "run_id": "SELECT i.project_id FROM runs r JOIN instances i ON i.id=r.instance_id WHERE r.id=?",
            "action_id": "SELECT i.project_id FROM actions a JOIN runs r ON r.id=a.run_id JOIN instances i ON i.id=r.instance_id WHERE a.id=?",
            "workflow_run_id": "SELECT w.project_id FROM workflow_runs r JOIN workflows w ON w.id=r.workflow_id WHERE r.id=?",
            "workflow_id": "SELECT project_id FROM workflows WHERE id=?", "agent_id": "SELECT project_id FROM agent_definitions WHERE id=?",
            "instance_id": "SELECT project_id FROM instances WHERE id=?", "session_id": "SELECT project_id FROM sessions WHERE id=?",
            "artifact_id": "SELECT project_id FROM artifacts WHERE id=?", "budget_id": "SELECT project_id FROM budgets WHERE id=?",
            "message_id": "SELECT project_id FROM messages WHERE id=?",
        }
        for key, query in queries.items():
            if params.get(key):
                record = one(db, query, (params[key],))
                require(record is not None, "SOURCE_UNAVAILABLE", "Original result owner is unavailable")
                return record["project_id"]
        return None

    async def issue(self, operation, params, result, consumer_id, *, characters):
        result = plain(result)
        require(not self.host.vault or not self.host.vault.contains_secret(result), "SOURCE_VERSION_UNAVAILABLE", "The response contains protected content")
        source, query, version, overlay = self.source(operation, params, result)
        if not self.host.registry.read_only.get(source):
            return None
        require(source in self.snapshot_operations or source in self.versioned_operations,
                "SOURCE_VERSION_UNAVAILABLE", "This result has no stable pageable source; use a smaller read-only query")
        payload = encode(result)
        content_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        version.update(content_version=content_hash)
        def accept(db):
            project = self.project(db, query, result)
            if source in self.snapshot_operations:
                require(project is not None, "SOURCE_UNAVAILABLE", "A response snapshot requires an owning project")
                snapshot = "snapshot_" + hashlib.sha256(encode([project, consumer_id, content_hash]).encode("utf-8")).hexdigest()
                version.update(mode="snapshot", snapshot_id=snapshot)
                db.execute("INSERT OR IGNORE INTO result_snapshots VALUES(?,?,?,?,?,?,?)",
                           (snapshot, project, consumer_id, content_hash, payload, len(payload), now_ms()))
            else:
                # Versioned configuration and discovery reads may be revalidated,
                # but no page is allowed to silently adopt replacement content.
                version.update(mode="versioned", content_hash=content_hash)
            digest = hashlib.sha256(encode({"consumer_id": consumer_id, "source": source, "query": query, "version": version, "overlay": overlay}).encode("utf-8")).hexdigest()
            identity = "result_" + digest
            db.execute("INSERT OR IGNORE INTO result_refs VALUES(?,?,?,?,?,?,?,?)", (identity, consumer_id, project, source, encode(query), encode(version), encode(overlay), now_ms()))
            return project, identity
        project, identity = await self.host.store.call(accept, transaction=True)
        return {"result_ref": identity, "project_id": project, "consumer_id": consumer_id, "characters": len(payload),
            "content_version": content_hash, "content_sha256": content_hash, "page_limit": 2048, "offset_unit": "unicode_codepoint",
            "read_operation": "results.read", "source_operation": source}

    def authorize_snapshot(self, db, record, params):
        """Check retained owners, without materializing a mutable domain query."""
        from oea.catalog.service import need

        project = record["project_id"]
        need(db, "projects", project)
        tables = {"run_id": "runs", "action_id": "actions", "session_id": "sessions", "instance_id": "instances",
                  "workflow_id": "workflows", "workflow_run_id": "workflow_runs", "artifact_id": "artifacts",
                  "budget_id": "budgets", "message_id": "messages", "step_id": "workflow_steps"}
        for key, table in tables.items():
            if params.get(key):
                owner = need(db, table, params[key])
                if "project_id" in owner:
                    require(owner["project_id"] == project, "PERMISSION_DENIED", "Result owner belongs to another project")
        if params.get("call_id"):
            self.host.mcp.intent_scope(db, params["call_id"], project, record["consumer_id"])
        if params.get("upload_id"):
            owner = need(db, "artifact_uploads", params["upload_id"])
            require(owner["project_id"] == project and owner["consumer_id"] == record["consumer_id"],
                    "PERMISSION_DENIED", "Upload belongs to another caller")
        if params.get("server_id"):
            owner = need(db, "mcp_servers", params["server_id"])
            require(owner["project_id"] in {None, project}, "PERMISSION_DENIED", "Service belongs to another project")

    def selected_snapshot(self, snapshot, path):
        payload = snapshot["payload_json"]
        # A changed secret set invalidates unsafe old content rather than editing
        # the text between pages. Cache this decision, never the authorization.
        secrets = frozenset(self.host.vault.known_secrets) if self.host.vault else frozenset()
        key = snapshot["id"]
        if self.checked_secrets.get(key) != secrets:
            value = json.loads(payload)
            require(not self.host.vault or not self.host.vault.contains_secret(value), "SOURCE_VERSION_UNAVAILABLE", "The saved response no longer satisfies the current secret policy")
            self.checked_secrets[key] = secrets
            self.checked_secrets.move_to_end(key)
            if len(self.checked_secrets) > 128:
                self.checked_secrets.popitem(last=False)
        if not path:
            return payload, snapshot["sha256"]
        field_key = (key, path)
        cached = self.fields.get(field_key)
        if cached is not None:
            self.fields.move_to_end(field_key)
            return cached[:2]
        encoded = encode(pointer(json.loads(payload), path))
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        size = len(encoded.encode("utf-8"))
        if size <= 1024 * 1024:
            while self.fields and (self.field_bytes + size > 4 * 1024 * 1024 or len(self.fields) >= 128):
                _, removed = self.fields.popitem(last=False)
                self.field_bytes -= removed[2]
            self.fields[field_key] = (encoded, digest, size)
            self.field_bytes += size
        return encoded, digest

    async def read(self, result_ref: str, *, project_id: str | None = None, consumer_id: str | None = None,
                   json_pointer: ResultPointer = "", offset: ResultOffset = 0, limit: ResultLimit = 2048):
        """Read one field or a bounded JSON text page from its original owner."""
        self.host.assert_open()
        context = operation_context.get() or {}
        reader = consumer_id or context.get("consumer_id")
        require(reader and (not context.get("consumer_id") or reader == context["consumer_id"]), "PERMISSION_DENIED", "Use the original conversation consumer identity")
        require(type(offset) is int and 0 <= offset < 2**63 and type(limit) is int and 1 <= limit <= 2048 and isinstance(json_pointer, str) and len(json_pointer) <= 512, "INVALID_REQUEST", "Use a bounded result page and JSON Pointer")
        def read_record(db):
            record = one(db, "SELECT * FROM result_refs WHERE id=? AND consumer_id=?", (result_ref, reader))
            require(record is not None and record["project_id"] == project_id, "PERMISSION_DENIED", "Result reference belongs to another conversation or project")
            version = json.loads(record["version_json"])
            snapshot = None
            if version.get("mode") == "snapshot":
                self.authorize_snapshot(db, record, json.loads(record["params_json"]))
                snapshot = one(db, "SELECT * FROM result_snapshots WHERE id=? AND project_id=? AND consumer_id=?",
                               (version["snapshot_id"], project_id, reader))
                require(snapshot is not None, "SOURCE_VERSION_UNAVAILABLE", "The original response snapshot is unavailable")
            return record, version, snapshot
        record, version, snapshot = await self.host.store.call(read_record)
        operation = record["operation"]
        require(self.host.registry.read_only.get(operation), "SOURCE_UNAVAILABLE", "The original read capability is unavailable")
        require(version.get("mode") != "unavailable" and (version.get("content_version") or operation not in self.snapshot_operations),
                "SOURCE_VERSION_UNAVAILABLE", "The original response has no retained content version; issue a new read-only query")
        if snapshot is not None:
            encoded, digest = self.selected_snapshot(snapshot, json_pointer)
        else:
            value = plain(await self.host.registry.handlers[operation](**json.loads(record["params_json"])))
            require(not self.host.vault or not self.host.vault.contains_secret(value), "SOURCE_VERSION_UNAVAILABLE", "The response no longer satisfies the current secret policy")
            overlay = json.loads(record["overlay_json"])
            if overlay:
                value = {**value, **overlay}
            self.check_version(value, version)
            encoded = encode(pointer(value, json_pointer))
            digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        require(offset <= len(encoded), "INVALID_REQUEST", "Offset exceeds the selected result length",
                details={"offset": offset, "characters": len(encoded), "errors": [{"path": ["offset"], "type": "less_than_equal", "message": "Offset must not exceed characters"}]})
        metadata = {"result_ref": result_ref, "json_pointer": json_pointer, "content_version": version.get("content_version", version.get("content_hash", encode(version))),
                    "content_sha256": digest, "characters": len(encoded), "page_limit": 2048, "offset_unit": "unicode_codepoint"}
        if offset == 0 and len(encoded) <= limit:
            return {**metadata, "value": json.loads(encoded), "complete": True, "next_offset": None,
                    "content_view": content_view("full", source_ref=result_ref, length=len(encoded))}
        end = min(len(encoded), offset + limit)
        return {**metadata, "encoding": "json", "text": encoded[offset:end],
            "next_offset": end if end < len(encoded) else None, "complete": end == len(encoded),
            "content_view": content_view("page", source_ref=result_ref, offset=offset, length=end-offset, next_offset=end if end < len(encoded) else None)}

    @staticmethod
    def check_version(value, version):
        if "revision" in version:
            require(value.get("revision") == version["revision"], "SOURCE_VERSION_CHANGED", "Configuration changed after the original receipt")
        if "revisions" in version:
            current = {item.get("id", (item.get("definition") or {}).get("id")): item["revision"] for item in value["items"]}
            require(current == version["revisions"], "SOURCE_VERSION_CHANGED", "A listed configuration revision changed")
        if "content_hash" in version:
            require(hashlib.sha256(encode(value).encode("utf-8")).hexdigest() == version["content_hash"], "SOURCE_VERSION_CHANGED", "The discovered contract changed")
