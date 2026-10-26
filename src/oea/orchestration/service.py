"""Project-owned definitions and lifecycle around native LangGraph execution."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections import OrderedDict

from oea.catalog.service import need
from oea.contracts.errors import RuntimeFault, require
from oea.tools.functions import FunctionTool
from oea.storage.persistence import encode, event, new_id, now_ms, one, rows
from oea.orchestration.types import NODE_CONFIGS, WorkflowDraft, WorkflowGraph


class Workflows:
    def __init__(self, host):
        self.host = host
        self.functions, self.jobs, self.waiters = {}, {}, {}
        self.computations, self.computation_owners = {}, {}
        self.computation_cancellation, self.cancelled_computations = {}, set()
        self.compiled, self.signals = OrderedDict(), {}
        self.maintenance, self.reviewing = set(), set()
        self.pending_launches = set()

    def register_function(self, function, *, name=None):
        provider = function if isinstance(function, FunctionTool) else FunctionTool(function, name=name)
        spec = provider.describe()
        require(spec.side_effects == "none", "FUNCTION_NOT_PURE", "Business function nodes are deterministic; execute side effects through an OEA Agent tool")
        require(spec.name not in self.functions, "FUNCTION_EXISTS", "Function is already registered")
        self.functions[spec.name] = provider

    def notify(self, run_id, *, settled=False):
        self.host.notify()
        signal = self.signals.pop(run_id, None)
        if signal is not None:
            signal.set()

    def signal(self, run_id):
        value = self.signals.get(run_id)
        if value is None:
            value = self.signals[run_id] = asyncio.Event()
        return value

    @staticmethod
    def project_id(run):
        return run["project_id"]

    @staticmethod
    def _row(db, run_id, project_id=None):
        value = one(db, "SELECT r.*,w.project_id,w.name AS workflow_name FROM workflow_runs r JOIN workflows w ON w.id=r.workflow_id WHERE r.id=?", (run_id,))
        require(value is not None, "NOT_FOUND", "Workflow execution does not exist")
        require(project_id is None or value["project_id"] == project_id, "PERMISSION_DENIED", "Workflow execution belongs to another project")
        return value

    def _compile(self, encoded):
        fingerprint = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        if fingerprint not in self.compiled:
            from oea.orchestration.graphs.workflow import compile_workflow
            # Bind closures to the saved immutable definition, not an SDK
            # caller's still-editable Pydantic object.
            definition = WorkflowGraph.model_validate_json(encoded)
            self.compiled[fingerprint] = compile_workflow(definition, self.host.saver)
            while len(self.compiled) > 16:
                self.compiled.popitem(last=False)
        self.compiled.move_to_end(fingerprint)
        return self.compiled[fingerprint]

    def _validate(self, db, project_id, definition, *, workflow_id=None, draft=False):
        need(db, "projects", project_id)
        for node in definition.nodes:
            if node.type in {"agent", "map"}:
                agent = one(db, "SELECT * FROM agent_definitions WHERE id=?", (node.config.agent_id,)) if draft else need(db, "agent_definitions", node.config.agent_id)
                if agent is None:
                    continue
                require(agent["project_id"] in {None, project_id}, "PROJECT_MISMATCH", "Workflow Agent belongs to another project")
            if node.type == "function" and not draft:
                require(node.config.name in self.functions, "FUNCTION_UNAVAILABLE", "Register this Python function in the host process")
            if node.type == "subgraph":
                child = one(db, "SELECT * FROM workflows WHERE id=?", (node.config.workflow_id,)) if draft else need(db, "workflows", node.config.workflow_id)
                if child is None:
                    continue
                require(child["project_id"] == project_id, "PROJECT_MISMATCH", "Subgraph belongs to another project")
                pending, visited = [node.config.workflow_id], set()
                while pending:
                    identity = pending.pop()
                    require(identity != workflow_id, "INVALID_WORKFLOW", "Use graph loops rather than recursive workflow definitions")
                    if identity in visited:
                        continue
                    visited.add(identity)
                    require(len(visited) <= 128, "INVALID_WORKFLOW", "Subgraph definition closure exceeds its supported size")
                    nested = need(db, "workflows", identity)
                    require(nested["project_id"] == project_id, "PROJECT_MISMATCH", "Nested subgraph belongs to another project")
                    nested_graph = WorkflowDraft.model_validate_json(nested["graph_json"])
                    pending.extend(item.config.workflow_id for item in nested_graph.nodes if item.type == "subgraph")
        nodes = {node.id for node in definition.nodes}
        from oea.orchestration.graphs.templates import references, PATH
        for node in definition.nodes if not draft else ():
            for path in references(node.config.model_dump(mode="json")):
                require(isinstance(path, str) and PATH.fullmatch(path), "INVALID_REFERENCE", "Workflow template path is invalid")
                if path.startswith("nodes."):
                    require(path.split(".")[1] in nodes, "INVALID_REFERENCE", "Template names an unknown graph node")

    async def node_types(self):
        return {name: {"config_schema": model.model_json_schema()} for name, model in NODE_CONFIGS.items()}

    async def schema(self):
        return WorkflowGraph.model_json_schema()

    async def create(self, project_id: str, name: str, graph: WorkflowDraft, *, workflow_id: str | None = None, description: str = ""):
        self.host.assert_open()
        require(0 < len(name) <= 80 and len(description) <= 640, "INVALID_REQUEST", "Use a bounded workflow name and description")
        graph = WorkflowDraft.model_validate(graph)
        encoded = graph.model_dump_json()
        identity = workflow_id or new_id("workflow")
        def write(db):
            self._validate(db, project_id, graph, workflow_id=identity, draft=True)
            previous = one(db, "SELECT * FROM workflows WHERE id=?", (identity,))
            if previous:
                require(previous["project_id"] == project_id and previous["name"] == name and previous["description"] == description and previous["graph_json"] == encoded,
                        "IDEMPOTENCY_CONFLICT", "Workflow ID has different configuration")
                return previous
            db.execute("INSERT INTO workflows(id,project_id,name,revision,graph_json,created_at,updated_at,description) VALUES(?,?,?,1,?,?,?,?)", (identity, project_id, name, encoded, now_ms(), now_ms(), description))
            event(db, "workflow.created", project_id=project_id, data={"workflow_id": identity})
            return need(db, "workflows", identity)
        row = await self.host.store.call(write, transaction=True)
        self.host.notify()
        return self._definition(row)

    @staticmethod
    def _definition(row):
        return {"id": row["id"], "project_id": row["project_id"], "name": row["name"], "description": row["description"], "state": row["state"], "revision": row["revision"], "graph": json.loads(row["graph_json"])}

    async def get(self, workflow_id: str, project_id: str):
        def read(db):
            row = need(db, "workflows", workflow_id)
            require(row["project_id"] == project_id, "PERMISSION_DENIED", "Workflow belongs to another project")
            return self._definition(row)
        return await self.host.store.call(read)

    async def list(self, project_id: str, *, cursor: int = 0, limit: int = 25, include_archived: bool = False, deleted_only: bool = False):
        require(cursor >= 0 and 1 <= limit <= 100, "INVALID_REQUEST", "Use a bounded workflow page")
        def read(db):
            need(db, "projects", project_id)
            return rows(db, "SELECT rowid AS position,* FROM workflows WHERE project_id=? AND rowid>? AND ((?=1 AND state='deleted') OR (?=0 AND (state='active' OR ?=1 AND state='archived'))) ORDER BY rowid LIMIT ?", (project_id, cursor, int(deleted_only), int(deleted_only), int(include_archived), limit + 1))
        values = await self.host.store.call(read)
        return {"items": [{**self._definition(row), "position": row["position"]} for row in values[:limit]], "cursor": values[limit - 1]["position"] if len(values) > limit else None}

    async def update(self, workflow_id: str, project_id: str, expected_revision: int, *, graph: WorkflowDraft | None = None, name: str | None = None, description: str | None = None):
        self.host.assert_open()
        if graph is not None:
            graph = WorkflowDraft.model_validate(graph)
        def write(db):
            row = need(db, "workflows", workflow_id)
            require(row["project_id"] == project_id, "PERMISSION_DENIED", "Workflow belongs to another project")
            require(row["revision"] == expected_revision, "REVISION_CONFLICT", "Workflow definition changed")
            require(row["state"] == "active", "WORKFLOW_NOT_EDITABLE", "Restore the workflow before editing")
            if graph:
                self._validate(db, project_id, graph, workflow_id=workflow_id, draft=True)
            title = name if name is not None else row["name"]
            require(0 < len(title) <= 80, "INVALID_REQUEST", "Workflow name must contain 1 to 80 characters")
            detail = description if description is not None else row["description"]
            require(len(detail) <= 640, "INVALID_REQUEST", "Workflow description is too long")
            db.execute("UPDATE workflows SET name=?,description=?,graph_json=?,revision=revision+1,updated_at=? WHERE id=?",
                       (title, detail, graph.model_dump_json() if graph else row["graph_json"], now_ms(), workflow_id))
            event(db, "workflow.updated", project_id=project_id, data={"workflow_id": workflow_id, "revision": expected_revision + 1})
            return self._definition(need(db, "workflows", workflow_id))
        result = await self.host.store.call(write, transaction=True)
        self.host.notify()
        return result

    async def validate(self, project_id: str, graph: dict):
        """Validate the current definition without dispatching or calling a model."""
        from pydantic import ValidationError
        try:
            definition = WorkflowGraph.model_validate(graph)
        except ValidationError as error:
            return {"valid": False, "errors": [{"node_id": None, "field": ".".join(str(part) for part in item["loc"]), "message": item["msg"]} for item in error.errors(include_input=False)], "warnings": []}
        try:
            await self.host.store.call(lambda db: self._validate(db, project_id, definition))
            self._compile(definition.model_dump_json())
        except RuntimeFault as error:
            return {"valid": False, "errors": [{"node_id": error.identity.get("node_id"), "field": "", "message": error.safe_message}], "warnings": []}
        return {"valid": True, "errors": [], "warnings": []}

    async def activity(self, project_id: str, *, cursor: int = 0, limit: int = 100):
        """Read live workflow counts without loading historical outputs."""
        require(0 <= cursor < 2**63 and 1 <= limit <= 100, "INVALID_REQUEST", "Use a bounded activity page")
        def read(db):
            need(db, "projects", project_id)
            return rows(db, "SELECT w.rowid AS position,w.id AS workflow_id,COUNT(r.id) AS active_count FROM workflows w LEFT JOIN workflow_runs r ON r.workflow_id=w.id AND r.state IN ('accepted','running','waiting','needs_review') WHERE w.project_id=? AND w.rowid>? GROUP BY w.id ORDER BY w.rowid LIMIT ?", (project_id, cursor, limit + 1))
        values = await self.host.store.call(read)
        return {"items": values[:limit], "cursor": values[limit - 1]["position"] if len(values) > limit else None}

    async def _set_state(self, workflow_id, project_id, expected_revision, state):
        self.host.assert_open()
        def write(db):
            row = need(db, "workflows", workflow_id)
            require(row["project_id"] == project_id, "PERMISSION_DENIED", "Workflow belongs to another project")
            if row["state"] == state:
                return self._definition(row)
            require(row["revision"] == expected_revision, "REVISION_CONFLICT", "Workflow definition changed")
            if state == "deleted":
                active = one(db, "SELECT 1 FROM workflow_runs WHERE workflow_id=? AND state IN ('accepted','running','waiting','needs_review') LIMIT 1", (workflow_id,))
                require(not active, "WORKFLOW_IN_USE", "Finish or verify existing workflow runs before moving the definition to the recycle bin")
            else:
                require(row["state"] != "deleted" or state == "active", "WORKFLOW_DELETED", "Restore the workflow before archiving")
            db.execute("UPDATE workflows SET state=?,revision=revision+1,updated_at=? WHERE id=?", (state, now_ms(), workflow_id))
            kind = "workflow.deleted" if state == "deleted" else "workflow.archived" if state == "archived" else "workflow.restored"
            event(db, kind, project_id=project_id, data={"workflow_id": workflow_id, "revision": row["revision"] + 1})
            return self._definition(need(db, "workflows", workflow_id))
        result = await self.host.store.call(write, transaction=True)
        self.host.notify()
        return result

    async def set_archived(self, workflow_id: str, project_id: str, archived: bool, expected_revision: int):
        return await self._set_state(workflow_id, project_id, expected_revision, "archived" if archived else "active")

    async def remove(self, workflow_id: str, project_id: str, expected_revision: int, *, confirmed: bool = False):
        """Move an idle definition to the recycle bin, retaining graph and runs."""
        require(confirmed, "CONFIRMATION_REQUIRED", "Confirm moving this workflow to the recycle bin")
        return await self._set_state(workflow_id, project_id, expected_revision, "deleted")

    async def restore(self, workflow_id: str, project_id: str, expected_revision: int):
        return await self._set_state(workflow_id, project_id, expected_revision, "active")

    async def run(self, workflow_id: str, inputs: dict, idempotency_key: str, *, project_id: str, session_id: str | None = None,
                  expected_revision: int | None = None, budget_id: str | None = None, parent_workflow_run_id: str | None = None):
        self.host.assert_open()
        require(0 < len(idempotency_key) <= 256 and len(encode(inputs).encode("utf-8")) <= 1024 * 1024, "INVALID_REQUEST", "Use a bounded input and stable workflow key")
        fingerprint = hashlib.sha256(encode({"workflow_id": workflow_id, "inputs": inputs, "session_id": session_id,
            "expected_revision": expected_revision, "budget_id": budget_id, "parent_workflow_run_id": parent_workflow_run_id}).encode("utf-8")).hexdigest()
        identity = new_id("workflow-run")
        def accept(db):
            definition = need(db, "workflows", workflow_id)
            require(definition["project_id"] == project_id, "PERMISSION_DENIED", "Workflow belongs to another project")
            previous = one(db, "SELECT * FROM workflow_runs WHERE workflow_id=? AND idempotency_key=?", (workflow_id, idempotency_key))
            if previous:
                require(previous["input_hash"] == fingerprint, "IDEMPOTENCY_CONFLICT", "Workflow key has different work or ownership")
                return previous, True
            need(db, "projects", project_id)
            require(definition["state"] == "active", "WORKFLOW_NOT_ACTIVE", "Restore the workflow before accepting new work")
            require(expected_revision is None or definition["revision"] == expected_revision, "REVISION_CONFLICT", "Workflow definition changed")
            from pydantic import ValidationError as GraphValidationError
            try:
                graph = WorkflowGraph.model_validate_json(definition["graph_json"])
            except GraphValidationError:
                raise RuntimeFault("INVALID_WORKFLOW", "Validate and complete the saved graph before accepting a run") from None
            from jsonschema import ValidationError
            from oea.contracts.schema_validation import schema_validator, validate_value
            try:
                validate_value(schema_validator(next(node.config.input_schema for node in graph.nodes if node.type == "start")), inputs)
            except ValidationError:
                raise RuntimeFault("INVALID_WORKFLOW_INPUT", "Inputs do not match the workflow start schema") from None
            self._validate(db, project_id, graph, workflow_id=workflow_id)
            parent = self._row(db, parent_workflow_run_id, project_id) if parent_workflow_run_id else None
            if parent:
                require(parent["cancel_requested_at"] is None and parent["state"] in {"accepted", "running", "waiting"} and session_id in {None, parent["session_id"]}, "WORKFLOW_CANCELLED", "Parent workflow cannot accept this child")
                require(budget_id in {None, parent["budget_id"]}, "BUDGET_MISMATCH", "Subgraph inherits its root cost budget")
                session = need(db, "sessions", parent["session_id"])
                budget = parent["budget_id"]
                root_run_id, root_workflow_run_id = parent["root_run_id"], parent["root_workflow_run_id"]
            else:
                session = need(db, "sessions", session_id) if session_id else self.host.sessions._create(db, project_id, definition["name"])
                require(session["project_id"] == project_id, "PROJECT_MISMATCH", "Session belongs to another project")
                require(not session["archived"], "SESSION_ARCHIVED", "Restore the session before accepting new graph work")
                budget = budget_id or self.host.budgets._create(db, graph.budget, root_workflow_run_id=identity, project_id=project_id, session_id=session["id"])
                if budget_id:
                    self.host.budgets.require_scope(db, budget_id, project_id=project_id, session_id=session["id"])
                root_run_id, root_workflow_run_id = None, identity
            db.execute("INSERT INTO workflow_runs(id,workflow_id,session_id,budget_id,root_run_id,root_workflow_run_id,parent_workflow_run_id,revision,graph_json,inputs_json,state,idempotency_key,input_hash,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,'accepted',?,?,?,?)",
                       (identity, workflow_id, session["id"], budget, root_run_id, root_workflow_run_id, parent_workflow_run_id,
                        definition["revision"], definition["graph_json"], encode(self.host.redact(inputs)), idempotency_key, fingerprint, now_ms(), now_ms()))
            event(db, "workflow.accepted", project_id=project_id, session_id=session["id"], data={"workflow_run_id": identity, "workflow_id": workflow_id})
            return need(db, "workflow_runs", identity), False
        row, reused = await self.host.store.call(accept, transaction=True)
        self.launch(row["id"])
        self.notify(row["id"])
        return {"id": row["id"], "workflow_id": workflow_id, "project_id": project_id, "session_id": row["session_id"], "state": row["state"], "budget_id": row["budget_id"], "reused": reused}

    async def get_run(self, workflow_run_id: str, *, project_id: str):
        row = await self.host.store.call(lambda db: self._row(db, workflow_run_id, project_id))
        return {key: row[key] for key in ("id", "workflow_id", "project_id", "session_id", "budget_id", "root_run_id", "root_workflow_run_id",
                "parent_workflow_run_id", "state", "revision", "cancel_requested_at", "stopped_at", "created_at", "updated_at")} | {
            "error": json.loads(row["error_json"]) if row["error_json"] else None,
            "wait": json.loads(row["wait_json"]) if row["wait_json"] else None,
            "result_available": row["result_json"] is not None}

    async def list_runs(self, workflow_id: str, *, project_id: str, cursor: int = 0, limit: int = 25):
        require(0 <= cursor < 2**63 and 1 <= limit <= 100, "INVALID_REQUEST", "Read a bounded graph execution page")
        await self.get(workflow_id, project_id)
        values = await self.host.store.call(lambda db: rows(db, "SELECT rowid AS position,id,state,session_id,budget_id,revision,created_at,updated_at FROM workflow_runs WHERE workflow_id=? AND (?=0 OR rowid<?) ORDER BY rowid DESC LIMIT ?", (workflow_id, cursor, cursor, limit + 1)))
        return {"items": values[:limit], "cursor": values[limit - 1]["position"] if len(values) > limit else None}

    async def inspect(self, workflow_run_id: str, *, project_id: str):
        run = await self.get_run(workflow_run_id, project_id=project_id)
        nodes = await self.host.store.call(lambda db: rows(db, "SELECT node_id,state,head_step_id,error_json FROM workflow_nodes WHERE workflow_run_id=? ORDER BY rowid", (workflow_run_id,)))
        row = await self.host.store.call(lambda db: self._row(db, workflow_run_id, project_id))
        definition = WorkflowGraph.model_validate_json(row["graph_json"])
        graph = self._compile(row["graph_json"])
        snapshot = await graph.aget_state(self.config(workflow_run_id, definition))
        return {**run, "graph": definition.model_dump(mode="json"), "nodes": [{"node_id": node["node_id"], "state": node["state"], "step_id": node["head_step_id"], "error": json.loads(node["error_json"]) if node["error_json"] else None} for node in nodes],
            "native_next": list(snapshot.next), "budget": await self.host.budgets.get(run["budget_id"])}

    async def node_result(self, workflow_run_id: str, node_id: str, *, project_id: str, step_id: str | None = None, version: str | None = None, metadata_only: bool = False):
        from oea.orchestration.graphs.workflow_context import WorkflowContext
        row = await self.host.store.call(lambda db: self._row(db, workflow_run_id, project_id))
        if step_id:
            head = await self.host.store.call(lambda db: one(db, "SELECT id AS head_step_id,state,error_json,outcome_hash FROM workflow_steps WHERE id=? AND workflow_run_id=? AND node_id=?", (step_id, workflow_run_id, node_id)))
        else:
            head = await self.host.store.call(lambda db: one(db, "SELECT n.head_step_id,s.state,s.error_json,s.outcome_hash FROM workflow_nodes n JOIN workflow_steps s ON s.id=n.head_step_id WHERE n.workflow_run_id=? AND n.node_id=?", (workflow_run_id, node_id)))
        require(head is not None, "RESULT_PENDING", "Node has not started")
        selected_state = head["state"]
        if version is not None and version != head["outcome_hash"]:
            archived = await self.host.store.call(lambda db: one(db, "SELECT state FROM workflow_step_failures WHERE step_id=? AND outcome_hash=?", (head["head_step_id"], version)))
            require(archived is not None, "SOURCE_VERSION_CHANGED", "Original node evidence version is unavailable")
            selected_state = archived["state"]
        if metadata_only:
            def reference(db):
                record = one(db, "SELECT ref_json FROM workflow_step_failures WHERE step_id=? AND outcome_hash=?", (head["head_step_id"], version)) if version is not None and version != head["outcome_hash"] else one(db, "SELECT ref_json FROM workflow_steps WHERE id=?", (head["head_step_id"],))
                value = json.loads(record["ref_json"]) if record and record["ref_json"] else {}
                result = {key: value[key] for key in ("kind", "run_id", "instance_id", "workflow_run_id", "count") if key in value}
                if value.get("kind") == "map" and "members" in value:
                    result["count"] = len(value["members"])
                if value.get("kind") == "value" and isinstance(value.get("value"), dict) and type(value["value"].get("value")) is bool:
                    result["branch"] = "true" if value["value"]["value"] else "false"
                return result
            return {"step_id": head["head_step_id"], "state": selected_state, "version": version or head["outcome_hash"], "reference": await self.host.store.call(reference)}
        context = WorkflowContext(self, row, WorkflowGraph.model_validate_json(row["graph_json"]))
        return {"step_id": head["head_step_id"], "state": selected_state, "version": version or head["outcome_hash"], "output": await context.materialize(head["head_step_id"], version)}

    async def members(self, workflow_run_id: str, node_id: str, *, project_id: str, step_id: str | None = None, version: str | None = None, cursor: int = 0, limit: int = 25):
        """Read map identities and states without materializing member outputs."""
        require(0 <= cursor < 2**63 and 1 <= limit <= 100, "INVALID_REQUEST", "Read a bounded member page")
        metadata = await self.node_result(workflow_run_id, node_id, project_id=project_id, step_id=step_id, version=version, metadata_only=True)
        require(metadata["reference"].get("kind") == "map", "INVALID_REQUEST", "Select a map node")
        def read(db):
            step = need(db, "workflow_steps", metadata["step_id"])
            if metadata["version"] != step["outcome_hash"]:
                original = one(db, "SELECT ref_json FROM workflow_step_failures WHERE step_id=? AND outcome_hash=?", (step["id"], metadata["version"]))
                require(original is not None, "SOURCE_VERSION_CHANGED", "Original member snapshot is unavailable")
                reference = json.loads(original["ref_json"]) if original["ref_json"] else {}
            else:
                reference = json.loads(step["ref_json"]) if step["ref_json"] else {}
            if "members" in reference:
                return [{"ordinal": index, **value} for index, value in enumerate(reference["members"][cursor:cursor + limit + 1], start=cursor)]
            values = rows(db, "SELECT m.ordinal,m.instance_id,m.run_id,r.state,COALESCE(m.error_json,r.error_json) AS error_json FROM workflow_map_members m LEFT JOIN runs r ON r.id=m.run_id WHERE m.step_id=? AND m.ordinal>=? ORDER BY m.ordinal LIMIT ?", (step["id"], cursor, limit + 1))
            return [{"ordinal": value["ordinal"], "instance_id": value["instance_id"], "run_id": value["run_id"], "state": value["state"] or "failed", "error": json.loads(value["error_json"]) if value["error_json"] else None} for value in values]
        values = await self.host.store.call(read)
        return {"items": values[:limit], "cursor": values[limit - 1]["ordinal"] + 1 if len(values) > limit else None, "version": metadata["version"]}

    async def _owned_result(self, workflow_run_id, *, parent_workflow_run_id):
        parent = await self.host.store.call(lambda db: self._row(db, parent_workflow_run_id))
        child = await self.host.store.call(lambda db: self._row(db, workflow_run_id, parent["project_id"]))
        require(child["parent_workflow_run_id"] == parent_workflow_run_id and child["session_id"] == parent["session_id"],
                "PERMISSION_DENIED", "Subgraph result belongs to another parent execution")
        return await self.result(workflow_run_id, project_id=parent["project_id"])

    async def result(self, workflow_run_id: str, *, project_id: str, timeout: float = 0):
        from oea.orchestration.graphs.workflow_context import WorkflowContext
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            self.host.assert_open()
            changed = self.signal(workflow_run_id)
            row = await self.host.store.call(lambda db: self._row(db, workflow_run_id, project_id))
            if row["state"] == "completed":
                self.notify(workflow_run_id)
                reference = json.loads(row["result_json"])
                context = WorkflowContext(self, row, WorkflowGraph.model_validate_json(row["graph_json"]))
                return await context.materialize(reference["step_id"])
            if row["state"] in {"failed", "cancelled", "needs_review"}:
                self.notify(workflow_run_id)
                error = json.loads(row["error_json"]) if row["error_json"] else {"code": "WORKFLOW_" + row["state"].upper(), "safe_message": "Workflow did not complete"}
                raise RuntimeFault(**error)
            remaining = deadline - asyncio.get_running_loop().time()
            require(remaining > 0, "RESULT_PENDING", "Workflow has not completed", details={"state": row["state"]})
            try:
                await asyncio.wait_for(changed.wait(), remaining)
            except TimeoutError:
                raise RuntimeFault("RESULT_PENDING", "Workflow has not completed") from None

    async def compute(self, context, node, step, arguments):
        """Own actual computation until its receipt is durable, across cancellation."""
        task = self.computations.get(step["id"])
        if task is None:
            provider = self.functions.get(node.config.name)
            require(provider is not None, "FUNCTION_UNAVAILABLE", "Register this Python function in the host process")
            async def invoke():
                try:
                    value = await provider.invoke({"arguments": arguments})
                    reference = {"kind": "value", "value": value}
                    await context.save(node, step, "completed", reference)
                    return reference
                except asyncio.CancelledError:
                    committed = await self.host.store.call(lambda db: need(db, "workflow_steps", step["id"]))
                    if committed["state"] == "completed":
                        step.update(committed)
                        return json.loads(committed["ref_json"])
                    await context.save(node, committed, "failed", error=RuntimeFault("FUNCTION_CANCELLED", "Owned function stopped before producing a result"))
                    raise
                except Exception as error:
                    fault = error if isinstance(error, RuntimeFault) else RuntimeFault("WORKFLOW_FUNCTION_FAILED", "Business function failed", details={"cause": type(error).__name__})
                    await context.save(node, step, "failed", error=fault)
                    raise fault
            task = asyncio.create_task(invoke(), name=step["id"])
            self.computations[step["id"]] = task
            self.computation_owners[step["id"]] = context.run["id"]
            self.computation_cancellation[step["id"]] = provider.describe().cancellation
            def finished(completed):
                self.computations.pop(step["id"], None)
                self.computation_owners.pop(step["id"], None)
                self.computation_cancellation.pop(step["id"], None)
                self.cancelled_computations.discard(step["id"])
                if not completed.cancelled():
                    completed.exception()
                self.notify(context.run["id"])
                if context.run["id"] in self.reviewing and not self.host.closing:
                    maintenance = asyncio.create_task(self._after_settle(context.run["id"]))
                    self.maintenance.add(maintenance)
                    maintenance.add_done_callback(self.maintenance.discard)
            task.add_done_callback(finished)
        return await asyncio.shield(task)

    async def _stop_computations(self, run_id):
        requested = []
        for identity, owner in tuple(self.computation_owners.items()):
            if owner != run_id or identity in self.cancelled_computations or self.computation_cancellation[identity] == "none":
                continue
            task = self.computations[identity]
            if not task.done():
                self.cancelled_computations.add(identity)
                task.cancel()
                requested.append(task)
        if requested:
            await asyncio.wait(requested, timeout=3)

    def inflight(self, run_id):
        return run_id in self.computation_owners.values()

    @staticmethod
    def config(run_id, definition):
        return {"configurable": {"thread_id": "workflow:" + run_id}, "recursion_limit": definition.max_steps}

    def launch(self, run_id, *, restart=False):
        if self.host.closing or run_id in self.jobs and not self.jobs[run_id].done():
            if restart and not self.host.closing:
                self.pending_launches.add(run_id)
            self.notify(run_id)
            return
        task = asyncio.create_task(self._drive(run_id), name=run_id)
        self.jobs[run_id] = task
        def finished(completed):
            if self.jobs.get(run_id) is completed:
                self.jobs.pop(run_id, None)
            if not completed.cancelled():
                completed.exception()
            if run_id in self.pending_launches:
                self.pending_launches.discard(run_id)
                self.launch(run_id)
        task.add_done_callback(finished)

    async def finish(self, run_id, state, *, error=None, wait=None, result_ref=None):
        stopped = not self.inflight(run_id)
        def write(db):
            row = self._row(db, run_id)
            if row["state"] in {"completed", "cancelled"}:
                return row["state"]
            children_pending = self._pending_children(db, run_id)
            actual_state = "needs_review" if state == "cancelled" and children_pending else state
            db.execute("UPDATE workflow_runs SET state=?,error_json=?,wait_json=?,result_json=COALESCE(?,result_json),stopped_at=?,updated_at=? WHERE id=?",
                (actual_state, encode(self.host.redact(error.as_dict())) if error else None, encode(wait) if wait else None,
                 encode({"step_id": result_ref}) if result_ref else None, now_ms() if stopped and not children_pending else None, now_ms(), run_id))
            event(db, "workflow." + actual_state, project_id=row["project_id"], session_id=row["session_id"], data={"workflow_run_id": run_id, "workflow_id": row["workflow_id"], "state": actual_state})
            return actual_state
        state = await self.host.store.call(write, transaction=True)
        if state == "needs_review":
            self.reviewing.add(run_id)
        else:
            self.reviewing.discard(run_id)
        self.notify(run_id, settled=True)

    async def _after_settle(self, run_id):
        if self.inflight(run_id) or self.host.closing:
            return
        row = await self.host.store.call(lambda db: self._row(db, run_id))
        if row["state"] == "needs_review":
            if row["cancel_requested_at"]:
                if await self.host.store.call(lambda db: self._pending_children(db, run_id)):
                    self._watch_cancellation(run_id)
                else:
                    await self.finish(run_id, "cancelled")
            else:
                await self.finish(run_id, "waiting", wait={"kind": "confirmation", "prompt": "The original computation has settled. Explicitly resume this workflow to continue."})

    @staticmethod
    def _pending_children(db, run_id):
        return one(db, "SELECT 1 FROM runs WHERE workflow_run_id=? AND state IN ('accepted','running','waiting','needs_review') LIMIT 1", (run_id,)) or one(db,
                   "SELECT 1 FROM workflow_runs WHERE parent_workflow_run_id=? AND state IN ('accepted','running','waiting','needs_review') LIMIT 1", (run_id,))

    def _watch_cancellation(self, run_id):
        if self.host.closing or run_id in self.waiters and not self.waiters[run_id].done():
            return
        async def run_child(identity):
            while True:
                changed = self.host.run_signal(identity)
                value = await self.host.runs.get(identity)
                if value["state"] in {"completed", "failed", "cancelled"}:
                    self.host.notify_run(identity)
                    return
                await changed.wait()
        async def workflow_child(identity):
            while True:
                changed = self.signal(identity)
                row = await self.host.store.call(lambda db: self._row(db, identity))
                if row["state"] in {"completed", "failed", "cancelled"}:
                    self.notify(identity)
                    return
                await changed.wait()
        async def settle():
            children = await self.host.store.call(lambda db: rows(db, "SELECT id FROM runs WHERE workflow_run_id=? AND state IN ('accepted','running','waiting','needs_review')", (run_id,)))
            nested = await self.host.store.call(lambda db: rows(db, "SELECT id FROM workflow_runs WHERE parent_workflow_run_id=? AND state IN ('accepted','running','waiting','needs_review')", (run_id,)))
            await asyncio.gather(*(run_child(item["id"]) for item in children), *(workflow_child(item["id"]) for item in nested))
            await self._after_settle(run_id)
        task = asyncio.create_task(settle())
        self.waiters[run_id] = task
        def done(completed):
            self.waiters.pop(run_id, None)
            if not completed.cancelled():
                completed.exception()
        task.add_done_callback(done)

    async def _await_children(self, run, descriptor):
        async def agent(identity):
            while True:
                changed = self.host.run_signal(identity)
                # The descriptor is generated from an owned step; verify the
                # actual receipt as well, rather than trusting a wake-up.
                def read(db):
                    value = need(db, "runs", identity)
                    require(value["workflow_run_id"] == run["id"], "PERMISSION_DENIED", "Child belongs to another workflow")
                    return value["state"]
                state = await self.host.store.call(read)
                if state in {"completed", "failed", "cancelled", "needs_review"}:
                    self.host.notify_run(identity)
                    return state
                await changed.wait()
        async def workflow(identity):
            while True:
                changed = self.signal(identity)
                value = await self.host.store.call(lambda db: self._row(db, identity, run["project_id"]))
                require(value["parent_workflow_run_id"] == run["id"], "PERMISSION_DENIED", "Subgraph belongs to another workflow")
                if value["state"] in {"completed", "failed", "cancelled", "needs_review"}:
                    self.notify(identity)
                    return value["state"]
                await changed.wait()
        tasks = [*(asyncio.create_task(agent(identity)) for identity in descriptor.get("run_ids", ())),
                 *(asyncio.create_task(workflow(identity)) for identity in descriptor.get("workflow_run_ids", ()))]
        try:
            if descriptor.get("fail_fast"):
                for settled in asyncio.as_completed(tasks):
                    if await settled != "completed":
                        return
            else:
                await asyncio.gather(*tasks)
        finally:
            # These are receipt observers; actual child cancellation stays at
            # the workflow ownership boundary in _drive/cancel.
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _wake(self, run, interruptions):
        """Wait only on native interrupts, durable replies, or child receipts."""
        watchers = {asyncio.create_task(self._await_children(run, item.value)): item.id for item in interruptions if item.value.get("kind") in {"runs", "workflows"}}
        try:
            while True:
                changed = self.signal(run["id"])
                replies = await self.host.store.call(lambda db: rows(db, "SELECT id,interrupt_id FROM workflow_resumptions WHERE workflow_run_id=? ORDER BY rowid", (run["id"],)))
                matching = {item.id for item in interruptions}
                resumed = {reply["interrupt_id"]: {"resumption_id": reply["id"]} for reply in replies if reply["interrupt_id"] in matching}
                resumed.update({identity: {"ready": True} for task, identity in watchers.items() if task.done() and not task.cancelled() and task.exception() is None})
                for task in watchers:
                    if task.done() and not task.cancelled() and task.exception() is not None:
                        raise task.exception()
                if resumed:
                    return resumed
                signal = asyncio.create_task(changed.wait())
                try:
                    await asyncio.wait([signal, *watchers], return_when=asyncio.FIRST_COMPLETED)
                finally:
                    signal.cancel()
                    await asyncio.gather(signal, return_exceptions=True)
        finally:
            for task in watchers:
                task.cancel()
            await asyncio.gather(*watchers, return_exceptions=True)

    async def _drive(self, run_id):
        from langgraph.types import Command
        from oea.orchestration.graphs.workflow import WorkflowGraphContext
        from oea.orchestration.graphs.workflow_context import WorkflowContext
        run = await self.host.store.call(lambda db: self._row(db, run_id))
        if run["state"] not in {"accepted", "running", "waiting"} or run["cancel_requested_at"]:
            return
        definition = WorkflowGraph.model_validate_json(run["graph_json"])
        resumption = await self.host.store.call(lambda db: one(db, "SELECT * FROM workflow_resumptions WHERE workflow_run_id=? ORDER BY rowid DESC LIMIT 1", (run_id,)))
        graph, context = self._compile(run["graph_json"]), WorkflowContext(self, run, definition, resumption=resumption)
        config = self.config(run_id, definition)
        try:
            def start(db):
                budget, policy = self.host.budgets._check(db, run)
                return (budget["started_at"] or now_ms()) + policy["max_elapsed_seconds"] * 1000
            deadline = await self.host.store.call(start, transaction=True)
            snapshot = await graph.aget_state(config)
            if not snapshot.values:
                existing = await self.host.store.call(lambda db: one(db, "SELECT 1 FROM workflow_steps WHERE workflow_run_id=? LIMIT 1", (run_id,)))
                require(not existing, "CHECKPOINT_UNAVAILABLE", "Original graph progress is unavailable; retained evidence must be inspected before continuing")
            recovery_patch = None
            if resumption and resumption["checkpoint_id"] and not resumption["applied_at"] and snapshot.values.get("recovery_request_id") != resumption["id"]:
                config["configurable"]["checkpoint_id"] = resumption["checkpoint_id"]
                snapshot = await graph.aget_state(config)
                require(snapshot.next, "RECOVERY_POINT_UNAVAILABLE", "The original node's persistent checkpoint is unavailable")
                recovery_patch = await self._recovery_patch(run_id, snapshot.values, resumption["id"])
            if snapshot.values and not snapshot.next:
                require(not snapshot.values.get("failures"), "WORKFLOW_BRANCH_FAILED", "Workflow retains failed branch evidence")
                require(snapshot.values.get("result_ref"), "DELIVERY_MISSING", "The original graph ended before its delivery")
                if resumption and resumption["interrupt_id"] is None:
                    await self.host.store.call(lambda db: db.execute("UPDATE workflow_resumptions SET applied_at=COALESCE(applied_at,?) WHERE id=?", (now_ms(), resumption["id"])), transaction=True)
                await self.finish(run_id, "completed", result_ref=snapshot.values["result_ref"])
                return
            value = Command(update=recovery_patch) if recovery_patch else None if snapshot.values else {"workflow_run_id": run_id}
            interruptions = [] if recovery_patch else [item for task in snapshot.tasks for item in task.interrupts]
            remaining = (deadline - now_ms()) / 1000
            require(remaining > 0, "BUDGET_EXHAUSTED", "Work elapsed-time budget is exhausted")
            async with asyncio.timeout(remaining):
                while True:
                    if interruptions:
                        await self.finish(run_id, "waiting", wait={"interrupts": [{"id": item.id, **item.value} for item in interruptions]})
                        value = Command(resume=await self._wake(run, interruptions))
                    await self.host.store.call(lambda db: db.execute("UPDATE workflow_runs SET state='running',wait_json=NULL,stopped_at=NULL,updated_at=? WHERE id=?", (now_ms(), run_id)), transaction=True)
                    result = await graph.ainvoke(value, config, context=WorkflowGraphContext(context), durability="sync")
                    config["configurable"].pop("checkpoint_id", None)
                    if resumption and resumption["interrupt_id"] is None:
                        await self.host.store.call(lambda db: db.execute("UPDATE workflow_resumptions SET applied_at=COALESCE(applied_at,?) WHERE id=?", (now_ms(), resumption["id"])), transaction=True)
                    interruptions = result.get("__interrupt__", ())
                    if not interruptions:
                        require(not result.get("failures"), "WORKFLOW_BRANCH_FAILED", "Workflow retained failed branch evidence", details={"nodes": list(result["failures"])})
                        require(result.get("result_ref"), "DELIVERY_MISSING", "Business graph ended before its end node")
                        await self.finish(run_id, "completed", result_ref=result["result_ref"])
                        return
        except asyncio.CancelledError:
            await self._stop_computations(run_id)
            row = await self.host.store.call(lambda db: self._row(db, run_id))
            if row["state"] == "waiting" and not row["cancel_requested_at"]:
                await self.finish(run_id, "waiting", wait=json.loads(row["wait_json"]) if row["wait_json"] else None)
            else:
                await self.finish(run_id, "needs_review" if self.inflight(run_id) else "cancelled" if row["cancel_requested_at"] else "failed",
                    error=None if row["cancel_requested_at"] else RuntimeFault("HOST_CLOSED", "Graph checkpoint and evidence remain available"))
        except Exception as error:
            await self._stop_computations(run_id)
            fault = error if isinstance(error, RuntimeFault) else RuntimeFault("BUDGET_EXHAUSTED", "Work elapsed-time budget is exhausted") if isinstance(error, TimeoutError) else RuntimeFault("WORKFLOW_FAILED", "Business graph execution failed", details={"cause": type(error).__name__})
            # Native fail-fast cancels only accepted work owned by this graph.
            # A successful sibling in collect_independent remains evidence.
            if definition.failure_policy == "fail_fast":
                children = await self.host.store.call(lambda db: rows(db, "SELECT id FROM runs WHERE workflow_run_id=? AND state IN ('accepted','running','waiting','needs_review')", (run_id,)))
                nested = await self.host.store.call(lambda db: rows(db, "SELECT id FROM workflow_runs WHERE parent_workflow_run_id=? AND state IN ('accepted','running','waiting','needs_review')", (run_id,)))
                await asyncio.gather(*(self.host.runs.cancel(item["id"]) for item in children),
                    *(self.cancel(item["id"], project_id=run["project_id"]) for item in nested))
            pending = await self.host.store.call(lambda db: self._pending_children(db, run_id))
            await self.finish(run_id, "needs_review" if self.inflight(run_id) or pending or fault.code in {"OUTCOME_UNCERTAIN", "CHECKPOINT_UNAVAILABLE", "RECOVERY_POINT_UNAVAILABLE"} else "failed", error=fault)
            if pending:
                self._watch_cancellation(run_id)

    async def _recovery_patch(self, run_id, state, request_id):
        refs, failures = state.get("refs", {}), state.get("failures", {})
        selected = [(node_id, refs[node_id]) for node_id in failures if node_id in refs]
        if not selected:
            return {"recovery_request_id": request_id}
        identities = [identity for _, identity in selected]
        values = await self.host.store.call(lambda db: rows(db, "SELECT id,state,outcome_hash FROM workflow_steps WHERE workflow_run_id=? AND id IN (" + ",".join("?" for _ in identities) + ")", (run_id, *identities)))
        completed = {value["id"]: value for value in values if value["state"] == "completed"}
        recovered = {node_id: completed[identity]["outcome_hash"] for node_id, identity in selected if identity in completed}
        return {"recovery_request_id": request_id, "failures": {node_id: None for node_id in recovered}, "versions": recovered}

    async def resume(self, workflow_run_id: str, idempotency_key: str, *, project_id: str, input: str | dict | None = None, interrupt_id: str | None = None, node_id: str | None = None):
        self.host.assert_open()
        require(0 < len(idempotency_key) <= 256, "INVALID_REQUEST", "Use a stable resumption key")
        encoded_input = encode(self.host.redact(input))
        require(len(encoded_input.encode("utf-8")) <= 1024 * 1024, "INVALID_REQUEST", "Resume large inputs by reference")
        fingerprint = hashlib.sha256(encode({"input": input, "interrupt_id": interrupt_id, "node_id": node_id}).encode("utf-8")).hexdigest()
        def current(db):
            return self._row(db, workflow_run_id, project_id), one(db, "SELECT * FROM workflow_resumptions WHERE workflow_run_id=? AND idempotency_key=?", (workflow_run_id, idempotency_key))
        run, previous = await self.host.store.call(current)
        if previous:
            require(previous["input_hash"] == fingerprint, "IDEMPOTENCY_CONFLICT", "Resume key has different input")
            return await self.get_run(workflow_run_id, project_id=project_id)
        require(not self.inflight(workflow_run_id), "OUTCOME_UNCERTAIN", "Original computation is still in flight")
        require(run["state"] in {"waiting", "failed", "needs_review"} and run["cancel_requested_at"] is None, "WORKFLOW_NOT_RESUMABLE", "Choose an original waiting or recoverable graph")
        checkpoint_id, repair_steps, selected = None, [], interrupt_id
        wait = json.loads(run["wait_json"]) if run["wait_json"] else {}
        candidates = [item for item in wait.get("interrupts", ()) if item["kind"] == "input"]
        if candidates:
            require(node_id is None, "INVALID_REQUEST", "Choose the original input interrupt when answering a question")
            selected = selected or (candidates[0]["id"] if len(candidates) == 1 else None)
            require(any(item["id"] == selected for item in candidates), "INPUT_REQUIRED", "Choose the original human input interrupt")
        else:
            require(input is None and interrupt_id is None, "INPUT_REQUIRED", "This graph has no matching human input interrupt")
            require(run["state"] != "waiting" or wait.get("kind") == "confirmation", "WORKFLOW_DEPENDENCY_PENDING", "Inspect the original child work or wait for its receipt")
            def failures(db):
                unresolved = one(db, "SELECT 1 FROM runs WHERE workflow_run_id=? AND state='needs_review' LIMIT 1", (workflow_run_id,)) or one(db, "SELECT 1 FROM workflow_runs WHERE parent_workflow_run_id=? AND state='needs_review' LIMIT 1", (workflow_run_id,))
                require(not unresolved, "OUTCOME_UNCERTAIN", "Inspect the original unresolved child before resuming its graph")
                return rows(db, "SELECT s.id,s.node_id,s.checkpoint_id,s.native_step FROM workflow_nodes n JOIN workflow_steps s ON s.id=n.head_step_id WHERE n.workflow_run_id=? AND n.state IN ('failed','needs_review') ORDER BY s.native_step,s.rowid", (workflow_run_id,))
            failed = await self.host.store.call(failures)
            if node_id is not None:
                failed = [step for step in failed if step["node_id"] == node_id]
                require(failed, "INVALID_REQUEST", "Select a currently failed node")
            repair_steps = [step["id"] for step in failed]
            definition = WorkflowGraph.model_validate_json(run["graph_json"])
            snapshot = await self._compile(run["graph_json"]).aget_state(self.config(workflow_run_id, definition))
            if not snapshot.next and snapshot.values.get("failures"):
                require(failed, "RECOVERY_REVIEW_REQUIRED", "The graph needs a revised dependency decision; successful evidence remains available")
                checkpoint_id = failed[0]["checkpoint_id"]
        def write(db):
            run = self._row(db, workflow_run_id, project_id)
            previous = one(db, "SELECT * FROM workflow_resumptions WHERE workflow_run_id=? AND idempotency_key=?", (workflow_run_id, idempotency_key))
            if previous:
                require(previous["input_hash"] == fingerprint, "IDEMPOTENCY_CONFLICT", "Resume key has different input")
                return None
            require(run["state"] in {"waiting", "failed", "needs_review"} and run["cancel_requested_at"] is None, "WORKFLOW_NOT_RESUMABLE", "Choose an original waiting or recoverable graph")
            wait = json.loads(run["wait_json"]) if run["wait_json"] else {}
            candidates = [item for item in wait.get("interrupts", ()) if item["kind"] == "input"]
            require(run["state"] != "waiting" or candidates or wait.get("kind") == "confirmation", "WORKFLOW_DEPENDENCY_PENDING", "The graph is still waiting for its original child receipts")
            require(selected is None and input is None and not candidates or any(item["id"] == selected for item in candidates), "INPUT_REQUIRED", "The original human input wait changed")
            db.execute("INSERT INTO workflow_resumptions(id,workflow_run_id,idempotency_key,interrupt_id,input_json,input_hash,created_at,checkpoint_id,repair_steps_json) VALUES(?,?,?,?,?,?,?,?,?)",
                (new_id("workflow-resume"), workflow_run_id, idempotency_key, selected, encoded_input, fingerprint, now_ms(), checkpoint_id, encode(repair_steps)))
            db.execute("UPDATE workflow_runs SET state='accepted',error_json=NULL,stopped_at=NULL,updated_at=? WHERE id=?", (now_ms(), workflow_run_id))
            event(db, "workflow.resume_requested", project_id=project_id, session_id=run["session_id"], data={"workflow_run_id": workflow_run_id, "interrupt_id": selected, "checkpoint_id": checkpoint_id, "repair_steps": repair_steps})
            return run["state"]
        prior_state = await self.host.store.call(write, transaction=True)
        if prior_state is not None:
            self.launch(workflow_run_id, restart=prior_state in {"failed", "needs_review"})
        return await self.get_run(workflow_run_id, project_id=project_id)

    async def cancel(self, workflow_run_id: str, *, project_id: str):
        def request(db):
            root = self._row(db, workflow_run_id, project_id)
            owned = rows(db, "WITH RECURSIVE owned(id) AS (SELECT ? UNION ALL SELECT w.id FROM workflow_runs w JOIN owned o ON w.parent_workflow_run_id=o.id) SELECT w.id,w.state FROM workflow_runs w JOIN owned o ON o.id=w.id", (root["id"],))
            for item in owned:
                db.execute("UPDATE workflow_runs SET cancel_requested_at=COALESCE(cancel_requested_at,?),updated_at=? WHERE id=?", (now_ms(), now_ms(), item["id"]))
            return owned
        owned = await self.host.store.call(request, transaction=True)
        for item in reversed(owned):
            await self._stop_computations(item["id"])
            children = await self.host.store.call(lambda db: rows(db, "SELECT id FROM runs WHERE workflow_run_id=? AND state IN ('accepted','running','waiting','needs_review')", (item["id"],)))
            await asyncio.gather(*(self.host.runs.cancel(child["id"]) for child in children))
            job = self.jobs.get(item["id"])
            if job:
                job.cancel()
                await asyncio.gather(job, return_exceptions=True)
            if item["state"] not in {"completed", "cancelled"}:
                unresolved = await self.host.store.call(lambda db: one(db, "SELECT 1 FROM runs WHERE workflow_run_id=? AND state='needs_review' LIMIT 1", (item["id"],)))
                await self.finish(item["id"], "needs_review" if self.inflight(item["id"]) or unresolved else "cancelled")
                self._watch_cancellation(item["id"])
        return await self.get_run(workflow_run_id, project_id=project_id)

    async def open(self):
        pending = await self.host.store.call(lambda db: rows(db, "SELECT id,state FROM workflow_runs WHERE state IN ('accepted','running','waiting','needs_review')"))
        for item in pending:
            if item["state"] == "needs_review":
                self.reviewing.add(item["id"])
                run = await self.host.store.call(lambda db: self._row(db, item["id"]))
                if run["cancel_requested_at"]:
                    self._watch_cancellation(item["id"])
            else:
                self.launch(item["id"])

    async def aclose(self):
        await asyncio.gather(*(self._stop_computations(run_id) for run_id in set(self.computation_owners.values())))
        tasks = (*tuple(self.jobs.values()), *tuple(self.waiters.values()))
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(*tuple(self.computations.values()), return_exceptions=True)
        await asyncio.gather(*tuple(self.maintenance), return_exceptions=True)
        for signal in self.signals.values():
            signal.set()
        self.signals.clear()
        self.compiled.clear()
