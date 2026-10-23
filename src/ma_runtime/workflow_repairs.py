"""New repair intents reuse versioned results without owning earlier execution."""

import json
from collections import Counter

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from .errors import RuntimeFault, require
from .models import TERMINAL, TaskSpec, WorkflowGraph, content_hash, json_dump, new_id, now_ms
from .services import need, resolve_project
from .store import emit, one, rows


class WorkflowRepairs:
    def __init__(self, runtime):
        self.rt = runtime

    def validate_task(self, db, task):
        require(task.state == "SUCCEEDED" and task.result_json, "SOURCE_UNSETTLED", "Only successful task results can be reused")
        spec = TaskSpec.model_validate_json(task.spec_json)
        from .execution_policy import validate_policies
        validate_policies(self.rt, spec)
        for call in rows(db, "SELECT DISTINCT c.server_id,c.tool_name FROM mcp_calls c JOIN runs r ON r.id=c.run_id WHERE r.task_id=?", (task.id,)):
            self.rt.contexts.check_origin_for_agent(spec.agent_id, "tool:mcp:" + call.server_id + ":" + call.tool_name)
        for call in rows(db, "SELECT DISTINCT c.tool_name FROM tool_calls c JOIN runs r ON r.id=c.run_id WHERE r.task_id=?", (task.id,)):
            if not call.tool_name.startswith("mcp_"):
                self.rt.contexts.check_origin_for_agent(spec.agent_id, "tool:" + call.tool_name)
        for row in rows(db, "SELECT c.origin FROM context_access a JOIN context_sources c ON c.reference=a.reference WHERE a.task_id=?", (task.id,)):
            self.rt.contexts.check_origin_for_agent(spec.agent_id, row.origin)
        result = json.loads(task.result_json)
        self.rt.check_artifacts(db, task.session_id, result.get("artifact_ids", ()))

    def node_rows(self, db, run_id):
        result = rows(db, "SELECT n.*,s.inputs_json AS source_inputs,s.output_json AS source_output,s.branch AS source_branch "
            "FROM workflow_node_runs n LEFT JOIN workflow_reused_nodes r ON r.run_id=n.run_id AND r.node_id=n.node_id "
            "LEFT JOIN workflow_node_runs s ON s.run_id=r.source_run_id AND s.node_id=r.source_node_id WHERE n.run_id=? ORDER BY n.rowid", (run_id,))
        for row in result:
            for field, source in (("inputs_json", "source_inputs"), ("output_json", "source_output"), ("branch", "source_branch")):
                inherited = row.pop(source)
                if row[field] is None:
                    row[field] = inherited
        return result

    def plan(self, db, source, inputs, expected_revision, rerun_nodes):
        resolve_project(db, source.project_id)
        require(source.state in TERMINAL and source.ended_at is not None, "SOURCE_UNSETTLED", "Repair only a terminal workflow after unknown execution is resolved")
        require(need(db, "sessions", source.session_id).state == "OPEN", "SESSION_CLOSED", "Repair requires an open source session")
        workflow = need(db, "workflows", source.workflow_id)
        self.rt.workflows._available(db, workflow.id)
        require(workflow.state == "ACTIVE", "INVALID_STATE", "Restore the workflow before repair")
        require(expected_revision is None or type(expected_revision) is int and workflow.revision == expected_revision,
                "REVISION_CONFLICT", "Definition changed; select its current revision explicitly")
        graph_json = workflow.graph_json if expected_revision is not None else source.graph_json
        graph = WorkflowGraph.model_validate_json(graph_json)
        report = self.rt.workflows._validation(db, source.project_id, graph)
        require(report["valid"], "INVALID_WORKFLOW", "Repair graph is invalid", details=report)
        values = inputs if inputs is not None else json.loads(source.inputs_json)
        try:
            Draft202012Validator(next(node.config["input_schema"] for node in graph.nodes if node.type == "start")).validate(values)
        except (ValidationError, RecursionError):
            raise RuntimeFault("INVALID_INPUT", "Repair input does not match the start schema") from None
        previous = {node.id: node for node in WorkflowGraph.model_validate_json(source.graph_json).nodes}
        nodes = {node.node_id: node for node in self.node_rows(db, source.id)}
        require(isinstance(rerun_nodes, (list, tuple)) and all(isinstance(node, str) for node in rerun_nodes),
                "INVALID_REQUEST", "Choose node IDs to rerun")
        require(set(rerun_nodes) <= {node.id for node in graph.nodes}, "INVALID_REQUEST", "A selected repair node is absent from the graph")
        from .workflows import references
        old_inputs = json.loads(source.inputs_json)
        changed_inputs = {name for name in set(values) | set(old_inputs)
                          if values.get(name) != old_inputs.get(name) or (name in values) != (name in old_inputs)}
        old_edges = {(edge.source, edge.target, edge.source_handle) for edge in WorkflowGraph.model_validate_json(source.graph_json).edges}
        new_edges = {(edge.source, edge.target, edge.source_handle) for edge in graph.edges}
        invalid = set(rerun_nodes) | {target for _, target, _ in old_edges ^ new_edges}
        for node in graph.nodes:
            old = nodes.get(node.id)
            if old is None or node.type != previous[node.id].type or node.config != previous[node.id].config or old.state not in {"SUCCEEDED", "SKIPPED"}:
                invalid.add(node.id)
            if changed_inputs and node.type != "start" and any(path == "inputs" or path.startswith("inputs.")
                    and path.split(".")[1] in changed_inputs for path in references(node.config)):
                invalid.add(node.id)
            if old and old.type == "map" and old.batch_id and self.map_snapshot(db, source.id, node.id, old.batch_id)["state"] != "SUCCEEDED":
                invalid.add(node.id)
        pending = list(invalid)
        while pending:
            parent = pending.pop()
            for edge in graph.edges:
                if edge.source == parent and edge.target not in invalid:
                    invalid.add(edge.target)
                    pending.append(edge.target)
        if changed_inputs:
            invalid.update(node.id for node in graph.nodes if node.type == "start")
        return workflow, graph_json, graph, values, nodes, invalid, changed_inputs

    async def preview(self, source_run_id, *, inputs=None, expected_revision=None, rerun_nodes=()):
        inputs = self.rt.clean_json(inputs) if inputs is not None else None
        require(inputs is None or isinstance(inputs, dict), "INVALID_REQUEST", "Repair inputs must be a JSON object")
        def read(db):
            source = need(db, "workflow_runs", source_run_id)
            workflow, graph_json, graph, values, nodes, invalid, changed = self.plan(db, source, inputs, expected_revision, rerun_nodes)
            from .workflows import references, resolve
            scope = {"inputs": values, "nodes": {key: json.loads(node.output_json) for key, node in nodes.items()
                     if key not in invalid and node.state == "SUCCEEDED" and node.output_json}}
            member_plans = []
            for spec in graph.nodes:
                if spec.type != "map" or spec.id not in invalid:
                    continue
                member_plan = {"node_id": spec.id, "status": "UNKNOWN", "reason": "upstream_output_unavailable"}
                # A default reference must not pretend an invalidated upstream value is known.
                refs = references({key: spec.config[key] for key in ("items", "payload")})
                if not any(path.startswith("nodes.") and path.split(".")[1] in invalid for path in refs):
                    try:
                        items = resolve(spec.config["items"], scope)
                        require(isinstance(items, list) and len(items) <= 128, "INVALID_INPUT", "Map input must be a list of at most 128 items")
                        payloads = [resolve(spec.config["payload"], {**scope, "item": item, "index": index})
                                    for index, item in enumerate(items)]
                    except RuntimeFault as exc:
                        if exc.code != "VARIABLE_NOT_FOUND":
                            raise
                    else:
                        reusable, reason = ({}, "explicit_rerun") if spec.id in rerun_nodes else self.member_candidates(db, source, spec, payloads)
                        member_plan.update(status="KNOWN", reason=reason, member_count=len(payloads),
                            reusable_ordinals=sorted(reusable), rerun_ordinals=[index for index in range(len(payloads)) if index not in reusable])
                member_plans.append(member_plan)
            return {"source_run_id": source_run_id, "revision": workflow.revision if expected_revision is not None else source.revision,
                    "invalidated_nodes": sorted(invalid), "reuse_candidates": sorted(set(nodes) & {node.id for node in graph.nodes} - invalid),
                    "map_members": member_plans,
                    "changed_input_keys": sorted(changed), "snapshot_hash": content_hash({"graph": graph_json, "inputs": values}),
                    "reservation": False, "note": "Creation rechecks authorization and current state; rerun_nodes is not an invalidation upper bound"}
        return await self.rt.store.call(read)

    async def repair(self, source_run_id, idempotency_key, *, inputs=None, expected_revision=None, rerun_nodes=()):
        self.rt.admit()
        self.rt.workflows._key(idempotency_key)
        require(isinstance(rerun_nodes, (list, tuple)) and all(isinstance(node, str) for node in rerun_nodes), "INVALID_REQUEST", "Choose node IDs to rerun")
        inputs = self.rt.clean_json(inputs) if inputs is not None else None
        require(inputs is None or isinstance(inputs, dict), "INVALID_REQUEST", "Repair inputs must be a JSON object")
        digest = content_hash({"repair_of": source_run_id, "inputs": inputs, "expected_revision": expected_revision, "rerun_nodes": sorted(set(rerun_nodes))})
        def create(db):
            source = need(db, "workflow_runs", source_run_id)
            prior = one(db, "SELECT * FROM workflow_runs WHERE workflow_id=? AND idempotency_key=?", (source.workflow_id, idempotency_key))
            if prior:
                require(prior.request_hash == digest, "IDEMPOTENCY_CONFLICT", "Repair key has different input")
                return self.rt.workflows._handle(prior, reused=True)
            workflow, graph_json, graph, values, nodes, invalid, _ = self.plan(db, source, inputs, expected_revision, rerun_nodes)
            require(one(db, "SELECT COUNT(*) AS count FROM workflow_runs WHERE ended_at IS NULL").count < 128,
                    "QUEUE_FULL", "Too many live workflow runs")
            identity, stamp = new_id("wfr"), now_ms()
            db.execute("INSERT INTO workflow_runs(id,workflow_id,project_id,session_id,revision,graph_json,inputs_json,state,idempotency_key,request_hash,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'RUNNING',?,?,?,?)",
                (identity, source.workflow_id, source.project_id, source.session_id, workflow.revision if expected_revision is not None else source.revision,
                 graph_json, json_dump(values), idempotency_key, digest, stamp, stamp))
            db.execute("INSERT INTO workflow_repairs VALUES(?,?,?,?,?)", (identity, source.id, json_dump(sorted(set(rerun_nodes))), json_dump(sorted(invalid)), stamp))
            self.rt.budgets.workflow(db, identity, source.session_id, source_run_id=source.id)
            for spec in graph.nodes:
                old = nodes.get(spec.id)
                reuse = old is not None and spec.id not in invalid
                db.execute("INSERT INTO workflow_node_runs(run_id,node_id,type,title,state,started_at,ended_at) VALUES(?,?,?,?,?,?,?)",
                    (identity, spec.id, spec.type, spec.title, old.state if reuse else "PENDING", stamp if reuse else None, stamp if reuse else None))
                if reuse:
                    original = one(db, "SELECT source_run_id,source_node_id FROM workflow_reused_nodes WHERE run_id=? AND node_id=?", (source.id, spec.id))
                    origin_run, origin_node = (original.source_run_id, original.source_node_id) if original else (source.id, spec.id)
                    origin = one(db, "SELECT * FROM workflow_node_runs WHERE run_id=? AND node_id=?", (origin_run, origin_node))
                    if origin.task_id:
                        self.validate_task(db, need(db, "tasks", origin.task_id))
                    if origin.batch_id:
                        for member in self.map_snapshot(db, origin_run, origin_node, origin.batch_id)["instances"]:
                            if member["state"] == "SUCCEEDED":
                                self.validate_task(db, need(db, "tasks", member["task_id"]))
                    db.execute("INSERT INTO workflow_reused_nodes VALUES(?,?,?,?,?)", (identity, spec.id, origin_run, origin_node, content_hash({"output": old.output_json, "branch": old.branch})))
            emit(db, "workflow.repair_started", session_id=source.session_id,
                 payload={"workflow_id": source.workflow_id, "workflow_run_id": identity, "source_run_id": source.id, "project_id": source.project_id})
            return self.rt.workflows._handle(need(db, "workflow_runs", identity))
        result = await self.rt.store.call(create, transaction=True)
        self.rt.notify()
        return result

    async def get(self, run_id):
        def read(db):
            need(db, "workflow_runs", run_id)
            record = one(db, "SELECT * FROM workflow_repairs WHERE run_id=?", (run_id,))
            if record is None:
                return None
            return {"run_id": run_id, "source_run_id": record.source_run_id,
                    "rerun_nodes": json.loads(record.rerun_nodes_json), "invalidated_nodes": json.loads(record.invalidated_nodes_json),
                    "reused_nodes": rows(db, "SELECT node_id,source_run_id,source_node_id,output_hash FROM workflow_reused_nodes WHERE run_id=?", (run_id,)),
                    "members": rows(db, "SELECT node_id,ordinal,task_id,source_run_id,result_hash FROM workflow_repair_members WHERE run_id=? ORDER BY node_id,ordinal", (run_id,))}
        return await self.rt.store.call(read)

    def reusable_members(self, db, run, spec, payloads):
        repair = one(db, "SELECT * FROM workflow_repairs WHERE run_id=?", (run.id,))
        if not repair or spec.id in json.loads(repair.rerun_nodes_json):
            return {}
        source = need(db, "workflow_runs", repair.source_run_id)
        return self.member_candidates(db, source, spec, payloads)[0]

    def member_candidates(self, db, source, spec, payloads):
        """Shared read-only member decision for preview and actual repair admission."""
        old_spec = next((node for node in WorkflowGraph.model_validate_json(source.graph_json).nodes if node.id == spec.id), None)
        old_node = one(db, "SELECT * FROM workflow_node_runs WHERE run_id=? AND node_id=?", (source.id, spec.id))
        if not old_spec or not old_node:
            return {}, "no_source_node"
        old_config = {key: value for key, value in old_spec.config.items() if key != "result_policy"}
        new_config = {key: value for key, value in spec.config.items() if key != "result_policy"}
        if old_config != new_config:
            return {}, "map_contract_changed"
        policy_changed = old_spec.config.get("result_policy") != spec.config.get("result_policy")
        result = {}
        for member in self.map_snapshot(db, source.id, spec.id, old_node.batch_id)["instances"]:
            ordinal = member["ordinal"]
            if member["state"] == "SUCCEEDED" and ordinal < len(payloads):
                task = need(db, "tasks", member["task_id"])
                if json.loads(task.spec_json)["payload"] == payloads[ordinal]:
                    self.validate_task(db, task)
                    if policy_changed:
                        from .models import ResultPolicy, TaskResult
                        from .result_validation import validate_result
                        result_value = TaskResult.model_validate_json(task.result_json)
                        artifacts = self.rt.check_artifacts(db, task.session_id, result_value.artifact_ids)
                        _, error = validate_result(result_value, ResultPolicy.model_validate(spec.config.get("result_policy", {})), artifacts)
                        if error:
                            continue
                    result[ordinal] = {"task_id": task.id, "source_run_id": member.get("source_run_id") or source.id, "result_hash": content_hash(json.loads(task.result_json))}
        return result, "matching_successful_payloads"

    def bind_members(self, db, run_id, node_id, reusable, batch, count):
        remaining = iter(batch["instances"] if batch else ())
        for ordinal in range(count):
            old = reusable.get(ordinal)
            task = old["task_id"] if old else next(remaining)["task_id"]
            db.execute("INSERT INTO workflow_repair_members VALUES(?,?,?,?,?,?)", (run_id, node_id, ordinal, task,
                old["source_run_id"] if old else None, old["result_hash"] if old else None))

    def map_snapshot(self, db, run_id, node_id, batch_id, *, include_outputs=False):
        selected = rows(db, "SELECT m.ordinal,m.task_id,m.source_run_id,m.result_hash,t.thread_id,t.state,t.queue_reason,h.agent_id" +
            (",t.result_json,t.error_json" if include_outputs else "") + " FROM workflow_repair_members m JOIN tasks t ON t.id=m.task_id "
            "JOIN threads h ON h.id=t.thread_id WHERE m.run_id=? AND m.node_id=? ORDER BY m.ordinal", (run_id, node_id))
        if not selected:
            if not batch_id:
                reused = one(db, "SELECT source_run_id,source_node_id FROM workflow_reused_nodes WHERE run_id=? AND node_id=?",
                             (run_id, node_id))
                if reused:
                    source = one(db, "SELECT batch_id FROM workflow_node_runs WHERE run_id=? AND node_id=?",
                                 (reused.source_run_id, reused.source_node_id))
                    return self.map_snapshot(db, reused.source_run_id, reused.source_node_id, source.batch_id,
                                             include_outputs=include_outputs)
                return {"state": "SUCCEEDED", "counts": {}, "instances": []}
            return self.rt.instances.snapshot(db, batch_id, include_outputs=include_outputs)
        members = []
        for row in selected:
            member = {key: row[key] for key in ("ordinal", "task_id", "thread_id", "agent_id", "state", "queue_reason")}
            if row.source_run_id:
                member.update(reused=True, source_run_id=row.source_run_id)
            if include_outputs:
                member.update(result=json.loads(row.result_json) if row.result_json else None, error=json.loads(row.error_json) if row.error_json else None)
                require(not row.result_hash or content_hash(member["result"]) == row.result_hash, "SOURCE_VERSION_CONFLICT", "Reused member result changed")
            members.append(member)
        states = {member["state"] for member in members}
        state = ("UNCERTAIN" if "UNCERTAIN" in states else "SUCCEEDED" if states == {"SUCCEEDED"} else
                 "FAILED" if states <= TERMINAL else "WAITING" if states - TERMINAL == {"WAITING"} else "RUNNING")
        return {"state": state, "counts": dict(Counter(member["state"] for member in members)), "instances": members}
