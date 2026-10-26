"""Durable delivery references to domain-owned data; never replay a write."""

import hashlib
import json

from oea.contracts.measurement import content_view
from oea.contracts.errors import require
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

    def __init__(self, host):
        self.host = host

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
        }
        for key, query in queries.items():
            if params.get(key):
                record = one(db, query, (params[key],))
                require(record is not None, "SOURCE_UNAVAILABLE", "Original result owner is unavailable")
                return record["project_id"]
        return None

    async def issue(self, operation, params, result, consumer_id, *, characters):
        source, query, version, overlay = self.source(operation, params, result)
        if not self.host.registry.read_only.get(source):
            return None
        digest = hashlib.sha256(encode({"consumer_id": consumer_id, "source": source, "query": query, "version": version, "overlay": overlay}).encode("utf-8")).hexdigest()
        identity = "result_" + digest
        def accept(db):
            project = self.project(db, query, result)
            db.execute("INSERT OR IGNORE INTO result_refs VALUES(?,?,?,?,?,?,?,?)", (identity, consumer_id, project, source, encode(query), encode(version), encode(overlay), now_ms()))
            return project
        project = await self.host.store.call(accept, transaction=True)
        return {"result_ref": identity, "project_id": project, "consumer_id": consumer_id, "characters": characters,
            "read_operation": "results.read", "source_operation": source}

    async def read(self, result_ref: str, *, project_id: str | None = None, consumer_id: str | None = None,
                   json_pointer: str = "", offset: int = 0, limit: int = 2048):
        """Read one field or a bounded JSON text page from its original owner."""
        self.host.assert_open()
        context = operation_context.get() or {}
        reader = consumer_id or context.get("consumer_id")
        require(reader and (not context.get("consumer_id") or reader == context["consumer_id"]), "PERMISSION_DENIED", "Use the original conversation consumer identity")
        require(0 <= offset < 2**63 and 1 <= limit <= 2048 and len(json_pointer) <= 512, "INVALID_REQUEST", "Use a bounded result page and JSON Pointer")
        record = await self.host.store.call(lambda db: one(db, "SELECT * FROM result_refs WHERE id=? AND consumer_id=?", (result_ref, reader)))
        require(record is not None and record["project_id"] == project_id, "PERMISSION_DENIED", "Result reference belongs to another conversation or project")
        operation = record["operation"]
        require(self.host.registry.read_only.get(operation), "SOURCE_UNAVAILABLE", "The original read capability is unavailable")
        # Stored queries were already normalized at the operation boundary.
        # Invoke that one read handler; never re-enter a mutation operation.
        value = plain(await self.host.registry.handlers[operation](**json.loads(record["params_json"])))
        version, overlay = json.loads(record["version_json"]), json.loads(record["overlay_json"])
        if "revision" in version:
            require(value.get("revision") == version["revision"], "SOURCE_VERSION_CHANGED", "Configuration changed after the original receipt")
        if "revisions" in version:
            current = {item.get("id", (item.get("definition") or {}).get("id")): item["revision"] for item in value["items"]}
            require(current == version["revisions"], "SOURCE_VERSION_CHANGED", "A listed configuration revision changed")
        if "content_hash" in version:
            require(hashlib.sha256(encode(value).encode("utf-8")).hexdigest() == version["content_hash"], "SOURCE_VERSION_CHANGED", "The discovered contract changed")
        if overlay:
            value = {**value, **overlay}
        selected = pointer(value, json_pointer)
        encoded = encode(selected)
        if offset == 0 and len(encoded) <= limit:
            return {"result_ref": result_ref, "json_pointer": json_pointer, "value": selected, "complete": True, "offset_unit": "unicode_codepoint", "content_view": content_view("full", source_ref=result_ref, length=len(encoded))}
        end = min(len(encoded), offset + limit)
        return {"result_ref": result_ref, "json_pointer": json_pointer, "encoding": "json", "text": encoded[offset:end],
            "next_offset": end if end < len(encoded) else None, "complete": end == len(encoded), "offset_unit": "unicode_codepoint",
            "content_view": content_view("page", source_ref=result_ref, offset=offset, length=end-offset, next_offset=end if end < len(encoded) else None)}
