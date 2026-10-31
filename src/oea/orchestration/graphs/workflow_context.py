"""OEA capabilities for one business graph; LangGraph owns advancement."""

import asyncio
import hashlib
import json
from collections import OrderedDict

from langgraph.config import get_config
from langgraph.runtime import get_runtime

from oea.catalog.service import need
from oea.contracts.domain import RunSpec
from oea.contracts.errors import RuntimeFault, require
from oea.storage.persistence import encode, event, new_id, now_ms, one, rows
from oea.runtime.runs import Runs
from oea.orchestration.graphs.templates import compare, references, resolve


class WorkflowContext:
    def __init__(self, service, run, definition, *, resumption=None):
        self.service, self.host, self.run, self.definition = service, service.host, run, definition
        self.nodes = {node.id: node for node in definition.nodes}
        self.inputs = json.loads(run["inputs_json"])
        self.cache = OrderedDict()
        self.resumption = resumption
        self.repair_steps = set(json.loads(resumption["repair_steps_json"])) if resumption else set()

    def iteration(self, node, state):
        return state.get("iterations", {}).get(node.id, 0)

    async def step(self, node, state, arguments, *, unblocked=False):
        self.host.assert_open()
        if node.type == "map" and not arguments.get("config", {}).get("item_label"):
            arguments["config"].pop("item_label", None)
        versions = {identity: state.get("versions", {}).get(identity) for identity in arguments.get("refs", {})}
        fingerprint = hashlib.sha256(encode({"arguments": arguments, "source_versions": versions}).encode("utf-8")).hexdigest()
        key = node.id + ":" + str(self.iteration(node, state))
        if node.type in {"start", "template", "condition", "function", "end"}:
            key += ":" + fingerprint
        execution = get_runtime().execution_info
        native_step = get_config()["metadata"]["langgraph_step"]
        def accept(db):
            owner = need(db, "workflow_runs", self.run["id"])
            require(owner["state"] == "running" and owner["cancel_requested_at"] is None, "WORKFLOW_CANCELLED", "Graph no longer owns permission to start another step")
            previous = one(db, "SELECT * FROM workflow_steps WHERE workflow_run_id=? AND node_id=? AND operation_key=?", (self.run["id"], node.id, key))
            if previous:
                first_execution = previous["state"] == "skipped" and unblocked
                require(first_execution or previous["input_hash"] == fingerprint, "IDEMPOTENCY_CONFLICT", "Workflow step has different inputs or source versions; inspect the accepted work")
                repair = previous["state"] in {"failed", "needs_review"} and previous["id"] in self.repair_steps and self.resumption["id"] != previous["repair_request_id"]
                if first_execution or repair:
                    db.execute("INSERT OR IGNORE INTO workflow_step_failures VALUES(?,?,?,?,?,?)", (previous["id"], previous["outcome_hash"], previous["state"], previous["ref_json"], previous["error_json"], now_ms()))
                    db.execute("UPDATE workflow_steps SET state='running',input_hash=?,checkpoint_id=?,native_step=?,error_json=NULL,outcome_hash=NULL,repair_request_id=?,updated_at=? WHERE id=?",
                        (fingerprint, execution.checkpoint_id, native_step, self.resumption["id"] if self.resumption else None, now_ms(), previous["id"]))
                    db.execute("UPDATE workflow_nodes SET state='running',head_step_id=?,error_json=NULL,updated_at=? WHERE workflow_run_id=? AND node_id=?", (previous["id"], now_ms(), self.run["id"], node.id))
                    event(db, "workflow.node_repair_started", session_id=self.run["session_id"], data={"workflow_run_id": self.run["id"], "node_id": node.id, "step_id": previous["id"], "resumption_id": self.resumption["id"] if self.resumption else None})
                    return need(db, "workflow_steps", previous["id"])
                return previous
            identity = new_id("step")
            db.execute("INSERT INTO workflow_steps(id,workflow_run_id,node_id,operation_key,input_hash,checkpoint_id,native_step,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'running',?,?)", (identity, self.run["id"], node.id, key, fingerprint, execution.checkpoint_id, native_step, now_ms(), now_ms()))
            db.execute("INSERT INTO workflow_nodes VALUES(?,?,'running',?,NULL,?) ON CONFLICT(workflow_run_id,node_id) DO UPDATE SET state='running',head_step_id=excluded.head_step_id,error_json=NULL,updated_at=excluded.updated_at",
                       (self.run["id"], node.id, identity, now_ms()))
            event(db, "workflow.node_started", session_id=self.run["session_id"], data={"workflow_run_id": self.run["id"], "node_id": node.id, "step_id": identity})
            return need(db, "workflow_steps", identity)
        result = await self.host.store.call(accept, transaction=True)
        self.service.notify(self.run["id"])
        return result

    async def save(self, node, step, state, reference=None, error=None):
        encoded = encode(self.host.redact(reference)) if reference is not None else None
        require(encoded is None or len(encoded.encode("utf-8")) <= 1024 * 1024, "WORKFLOW_OUTPUT_TOO_LARGE", "Publish large evidence as an artifact instead of embedding it in graph output")
        fault = self.host.redact(error.as_dict()) if error else None
        encoded_fault = encode(fault) if fault else None
        stored_ref = encoded if reference is not None else step.get("ref_json")
        outcome_hash = hashlib.sha256((state + "\0" + (stored_ref or "null") + "\0" + (encoded_fault or "null")).encode("utf-8")).hexdigest()
        if step["state"] == "completed":
            require(state == "completed" and step["outcome_hash"] == outcome_hash, "WORKFLOW_RESULT_CHANGED", "An accepted successful step cannot be overwritten")
            return
        if step["state"] == state and step.get("outcome_hash") == outcome_hash:
            return
        def write(db):
            db.execute("UPDATE workflow_steps SET state=?,ref_json=?,error_json=?,outcome_hash=?,updated_at=? WHERE id=?",
                       (state, stored_ref, encoded_fault, outcome_hash, now_ms(), step["id"]))
            db.execute("UPDATE workflow_nodes SET state=?,error_json=?,updated_at=? WHERE workflow_run_id=? AND node_id=? AND head_step_id=?",
                       (state, encode(fault) if fault else None, now_ms(), self.run["id"], node.id, step["id"]))
            event(db, "workflow.node_" + state, session_id=self.run["session_id"],
                  data={"workflow_run_id": self.run["id"], "node_id": node.id, "step_id": step["id"], "error": fault})
            if state == "completed" and reference and reference.get("kind") == "reply":
                db.execute("UPDATE workflow_resumptions SET applied_at=COALESCE(applied_at,?) WHERE id=? AND workflow_run_id=?", (now_ms(), reference["resumption_id"], self.run["id"]))
        await self.host.store.call(write, transaction=True)
        step.update(state=state, ref_json=stored_ref, error_json=encoded_fault, outcome_hash=outcome_hash)
        self.service.notify(self.run["id"])

    def update(self, node, state, step, *, failed=None, **other):
        return {"refs": {node.id: step["id"]}, "iterations": {node.id: self.iteration(node, state) + 1},
                "versions": {node.id: step["outcome_hash"]}, "failures": {node.id: failed}, **other}

    def recorded_failure(self, node, state, step, *, dispatched=False):
        fault = RuntimeFault(**json.loads(step["error_json"]))
        if self.definition.failure_policy == "fail_fast":
            raise fault
        update = self.update(node, state, step, failed=fault.code)
        if node.type == "condition":
            update["routes"] = {node.id: None}
        if dispatched:
            update["dispatch"] = {node.id: {"step_id": step["id"], "skipped": True, "count": 0}}
        return update

    async def outputs(self, run_ids, *, include_results=True):
        values = []
        for start in range(0, len(run_ids), 128):
            batch = run_ids[start:start + 128]
            values.extend(await self.host.store.call(lambda db: Runs._workflow_outputs(db, batch,
                project_id=self.service.project_id(self.run), session_id=self.run["session_id"], workflow_run_id=self.run["id"], include_results=include_results)))
        return values

    async def materialize(self, step_id, version=None):
        cache_key = (step_id, version)
        if cache_key in self.cache:
            self.cache.move_to_end(cache_key)
            return self.cache[cache_key]
        step = await self.host.store.call(lambda db: one(db, "SELECT * FROM workflow_steps WHERE id=? AND workflow_run_id=?", (step_id, self.run["id"])))
        require(step is not None, "PERMISSION_DENIED", "Step reference is outside this workflow")
        if version is not None and step["outcome_hash"] != version:
            prior = await self.host.store.call(lambda db: one(db, "SELECT state,ref_json,error_json,outcome_hash FROM workflow_step_failures WHERE step_id=? AND outcome_hash=?", (step_id, version)))
            require(prior is not None, "SOURCE_VERSION_CHANGED", "Referenced workflow evidence has a different version")
            step.update(prior)
        reference = json.loads(step["ref_json"]) if step["ref_json"] else {}
        kind = reference.get("kind")
        if step["state"] in {"failed", "needs_review", "skipped"}:
            value = {"state": step["state"], "error": json.loads(step["error_json"]) if step["error_json"] else None}
        elif kind == "value":
            value = reference["value"]
        elif kind == "run":
            values = await self.outputs([reference["run_id"]])
            value = values[0]
        elif kind == "map":
            members = reference.get("members", [])
            completed = [member["run_id"] for member in members if member["state"] == "completed"]
            values = await self.outputs(completed)
            indexed = {item["run_id"]: item for item in values}
            value = {"outputs": [indexed.get(member["run_id"], member) for member in members]}
        elif kind == "subgraph":
            value = await self.service._owned_result(reference["workflow_run_id"], parent_workflow_run_id=self.run["id"])
        elif kind == "reply":
            reply = await self.host.store.call(lambda db: one(db, "SELECT input_json FROM workflow_resumptions WHERE id=? AND workflow_run_id=?", (reference["resumption_id"], self.run["id"])))
            require(reply is not None, "PERMISSION_DENIED", "Workflow reply belongs to another execution")
            value = json.loads(reply["input_json"])
        elif kind == "outputs":
            scope = await self.scope(reference["refs"], self.nodes[step["node_id"]].config.outputs, versions=reference["versions"])
            value = resolve(self.nodes[step["node_id"]].config.outputs, scope)
        else:
            value = {}
        if step["state"] == "completed" or version is not None:
            self.cache[cache_key] = value
            while len(self.cache) > 8:
                self.cache.popitem(last=False)
        return value

    async def scope(self, refs, value, *, item=None, index=None, versions=None):
        required = {path.split(".")[1] for path in references(value) if isinstance(path, str) and path.startswith("nodes.") and len(path.split(".")) > 1}
        nodes = {}
        for identity in required:
            if identity in refs:
                require(versions is None or versions.get(identity) is not None, "SOURCE_NOT_READY", "Connect the producing node or use its native wait before consuming evidence")
                nodes[identity] = await self.materialize(refs[identity], (versions or {}).get(identity))
        return {"inputs": self.inputs, "nodes": nodes, "item": item, "index": index}

    async def arguments(self, node, state):
        value = node.config.model_dump(mode="json")
        scope = await self.scope(state.get("refs", {}), value, versions=state.get("versions", {}))
        return resolve(value, scope)

    def blocked(self, node, state):
        if node.join == "any":
            return False
        parents = [edge.source for edge in self.definition.edges if edge.target == node.id]
        return any(state.get("failures", {}).get(parent) for parent in parents)

    def input_refs(self, node, state):
        needed = {path.split(".")[1] for path in references(node.config.model_dump(mode="json"))
                  if isinstance(path, str) and path.startswith("nodes.") and len(path.split(".")) > 1}
        return {identity: state.get("refs", {})[identity] for identity in needed if identity in state.get("refs", {})}

    async def skip(self, node, state):
        step = await self.step(node, state, {"config": node.config.model_dump(mode="json"), "refs": self.input_refs(node, state)})
        error = RuntimeFault("DEPENDENCY_FAILED", "A required predecessor did not complete")
        await self.save(node, step, "skipped", error=error)
        result = self.update(node, state, step, failed=error.code)
        if node.type == "condition":
            result["routes"] = {node.id: None}
        if node.type in {"agent", "map", "subgraph"}:
            result["dispatch"] = {node.id: {"step_id": step["id"], "skipped": True, "count": 0}}
        return result

    async def execute(self, node, state):
        if self.blocked(node, state):
            return await self.skip(node, state)
        step = await self.step(node, state, {"config": node.config.model_dump(mode="json"), "refs": self.input_refs(node, state)}, unblocked=True)
        if step["state"] == "completed":
            other = {}
            if node.type == "end":
                other["result_ref"] = step["id"]
            if node.type == "condition":
                value = json.loads(step["ref_json"])["value"]["value"]
                other["routes"] = {node.id: "true" if value else "false"}
            return self.update(node, state, step, **other)
        if step["state"] in {"failed", "needs_review", "skipped"}:
            return self.recorded_failure(node, state, step)
        try:
            arguments = await self.arguments(node, state)
            if node.type == "start":
                reference = {"kind": "value", "value": {}}
            elif node.type == "template":
                reference = {"kind": "value", "value": arguments["value"]}
            elif node.type == "condition":
                result = compare(arguments["left"], arguments["operator"], arguments["right"])
                reference = {"kind": "value", "value": {"value": result}}
            elif node.type == "function":
                reference = await self.service.compute(self, node, step, arguments["arguments"])
            elif node.type == "end":
                reference = {"kind": "outputs", "refs": {key: value for key, value in state.get("refs", {}).items() if key != node.id},
                             "versions": {key: value for key, value in state.get("versions", {}).items() if key != node.id}}
            else:
                raise RuntimeFault("WORKFLOW_NODE_INVALID", "Node needs its registered execution mapping")
            if step["state"] != "completed":
                await self.save(node, step, "completed", reference)
            other = {"routes": {node.id: "true" if result else "false"}} if node.type == "condition" else {}
            if node.type == "end":
                other["result_ref"] = step["id"]
            return self.update(node, state, step, **other)
        except Exception as error:
            fault = error if isinstance(error, RuntimeFault) else RuntimeFault("WORKFLOW_NODE_FAILED", "Workflow node failed", details={"cause": type(error).__name__})
            if step["state"] == "completed":
                # A consumer/checkpoint failure cannot erase an already
                # committed computation receipt. Native replay reads it.
                raise fault
            await self.save(node, step, "needs_review" if fault.code == "OUTCOME_UNCERTAIN" else "failed", error=fault)
            if self.definition.failure_policy == "fail_fast":
                raise fault
            return self.update(node, state, step, failed=fault.code, **({"routes": {node.id: None}} if node.type == "condition" else {}))

    async def dispatch_agent(self, node, state):
        if self.blocked(node, state):
            return await self.skip(node, state)
        arguments = await self.arguments(node, state)
        step = await self.step(node, state, {"config": node.config.model_dump(mode="json"), "refs": self.input_refs(node, state)}, unblocked=True)
        if step["state"] in {"failed", "needs_review", "skipped"}:
            return self.recorded_failure(node, state, step, dispatched=True)
        if step["state"] == "completed":
            return {"refs": {node.id: step["id"]}, "dispatch": {node.id: {"step_id": step["id"], "cached": True}}}
        if not step["ref_json"]:
            instance = await self.host.instances.create(self.run["session_id"], arguments["agent_id"], name=self.run["id"] + ":" + node.id, role=arguments["role"])
            value = arguments["input"]
            value = value if isinstance(value, (str, dict)) else {"value": value}
            handle = await self.host.runs.submit(RunSpec(instance_id=instance["id"], input=value, idempotency_key=step["operation_key"], policy=arguments["policy"],
                display={"title": node.title or node.id, "stage": node.id}), workflow_run_id=self.run["id"])
            await self.save(node, step, "waiting", {"kind": "run", "run_id": handle.id, "instance_id": instance["id"]})
        return {"refs": {node.id: step["id"]}, "dispatch": {node.id: {"step_id": step["id"], "iteration": self.iteration(node, state)}}}

    async def prepare_map(self, node, state):
        if self.blocked(node, state):
            return await self.skip(node, state)
        # item/index substitutions belong to the worker, after the frozen input
        # list and predecessor versions have been selected.
        step = await self.step(node, state, {"config": node.config.model_dump(mode="json"), "refs": self.input_refs(node, state)}, unblocked=True)
        if step["state"] in {"failed", "needs_review", "skipped"}:
            return self.recorded_failure(node, state, step, dispatched=True)
        if step["state"] == "completed":
            return {"refs": {node.id: step["id"]}, "dispatch": {node.id: {"step_id": step["id"], "cached": True, "count": 0}}}
        scope = await self.scope(state.get("refs", {}), node.config.items, versions=state.get("versions", {}))
        items = resolve(node.config.items, scope)
        require(isinstance(items, list), "WORKFLOW_MAP_INVALID", "Map expects a list")
        if not step["ref_json"]:
            await self.save(node, step, "waiting", {"kind": "map", "count": len(items), "input_refs": self.input_refs(node, state),
                                                 "input_versions": {key: state.get("versions", {}).get(key) for key in self.input_refs(node, state)}})
        return {"refs": {node.id: step["id"]}, "dispatch": {node.id: {"step_id": step["id"], "iteration": self.iteration(node, state), "count": len(items)}}}

    async def map_member(self, node, item):
        step = await self.host.store.call(lambda db: one(db, "SELECT * FROM workflow_steps WHERE id=? AND workflow_run_id=?", (item["step_id"], self.run["id"])))
        require(step is not None, "PERMISSION_DENIED", "Map dispatch is outside this workflow")
        existing = await self.host.store.call(lambda db: one(db, "SELECT run_id FROM workflow_map_members WHERE step_id=? AND ordinal=?", (step["id"], item["index"])))
        if not existing:
            reference = json.loads(step["ref_json"])
            refs = reference["input_refs"]
            versions = reference["input_versions"]
            items = resolve(node.config.items, await self.scope(refs, node.config.items, versions=versions))
            index = item["index"]
            value = resolve(node.config.input, await self.scope(refs, node.config.input, item=items[index], index=index, versions=versions))
            value = value if isinstance(value, (str, dict)) else {"value": value}
            instance = await self.host.instances.create(self.run["session_id"], node.config.agent_id, name=self.run["id"] + ":" + node.id + ":" + str(index))
            label = resolve(node.config.item_label, await self.scope(refs, node.config.item_label, item=items[index], index=index, versions=versions)) if node.config.item_label else str(index + 1)
            require(isinstance(label, str) and len(label) <= 120, "WORKFLOW_MAP_INVALID", "Map item label must be text of at most 120 characters")
            spec = RunSpec(instance_id=instance["id"], input=value, idempotency_key=step["operation_key"] + ":" + str(index), policy=node.config.policy,
                display={"title": node.title or node.id, "stage": node.id, "item_label": label})
            def accept(db):
                run, _ = self.host.runs._submit(db, spec, workflow_run_id=self.run["id"])
                db.execute("INSERT OR IGNORE INTO workflow_map_members VALUES(?,?,?,?,NULL)", (step["id"], index, instance["id"], run["id"]))
                return run["id"]
            run_id = await self.host.store.call(accept, transaction=True)
            self.host.launch(run_id)
        return {"dispatched": {step["id"]: True}}

    async def map_member_error(self, item, error):
        fault = error if isinstance(error, RuntimeFault) else RuntimeFault("WORKFLOW_MAP_MEMBER_FAILED", "Map item was not accepted", details={"cause": type(error).__name__})
        def save(db):
            step = one(db, "SELECT id FROM workflow_steps WHERE id=? AND workflow_run_id=?", (item["step_id"], self.run["id"]))
            require(step is not None, "PERMISSION_DENIED", "Map item is outside this workflow")
            db.execute("INSERT OR IGNORE INTO workflow_map_members VALUES(?,?,NULL,NULL,?)", (step["id"], item["index"], encode(self.host.redact(fault.as_dict()))))
        await self.host.store.call(save, transaction=True)
        return {"dispatched": {item["step_id"]: True}}

    async def dispatch_subgraph(self, node, state):
        if self.blocked(node, state):
            return await self.skip(node, state)
        arguments = await self.arguments(node, state)
        step = await self.step(node, state, {"config": node.config.model_dump(mode="json"), "refs": self.input_refs(node, state)}, unblocked=True)
        if step["state"] in {"failed", "needs_review", "skipped"}:
            return self.recorded_failure(node, state, step, dispatched=True)
        if step["state"] == "completed":
            return {"refs": {node.id: step["id"]}, "dispatch": {node.id: {"step_id": step["id"], "cached": True}}}
        if not step["ref_json"]:
            child = await self.service.run(arguments["workflow_id"], arguments["inputs"], self.run["id"] + ":" + step["operation_key"],
                                           project_id=self.service.project_id(self.run), session_id=self.run["session_id"], parent_workflow_run_id=self.run["id"])
            await self.save(node, step, "waiting", {"kind": "subgraph", "workflow_run_id": child["id"]})
        return {"refs": {node.id: step["id"]}, "dispatch": {node.id: {"step_id": step["id"], "iteration": self.iteration(node, state)}}}

    async def wait_descriptor(self, node, state):
        step = await self.host.store.call(lambda db: one(db, "SELECT * FROM workflow_steps WHERE id=? AND workflow_run_id=?", (state["dispatch"][node.id]["step_id"], self.run["id"])))
        require(step is not None, "PERMISSION_DENIED", "Wait is outside this workflow")
        reference = json.loads(step["ref_json"])
        if node.type == "map":
            members = await self.host.store.call(lambda db: rows(db, "SELECT * FROM workflow_map_members WHERE step_id=? ORDER BY ordinal", (step["id"],)))
            return step, {"kind": "runs", "run_ids": [member["run_id"] for member in members if member["run_id"]], "policy": node.config.outcome, "node_id": node.id,
                          "fail_fast": self.definition.failure_policy == "fail_fast" and node.config.outcome == "all_succeeded"}
        if node.type == "agent":
            return step, {"kind": "runs", "run_ids": [reference["run_id"]], "policy": "all_succeeded", "node_id": node.id}
        return step, {"kind": "workflows", "workflow_run_ids": [reference["workflow_run_id"]], "policy": "all_succeeded", "node_id": node.id}

    async def dispatch_error(self, node, state, error):
        step = await self.step(node, state, {"config": node.config.model_dump(mode="json"), "refs": self.input_refs(node, state)})
        fault = error if isinstance(error, RuntimeFault) else RuntimeFault("WORKFLOW_NODE_FAILED", "Workflow dispatch failed", details={"cause": type(error).__name__})
        await self.save(node, step, "failed", error=fault)
        if self.definition.failure_policy == "fail_fast":
            raise fault
        return {**self.update(node, state, step, failed=fault.code), "dispatch": {node.id: {"step_id": step["id"], "skipped": True, "count": 0}}}

    async def prepare_input(self, node, state):
        if self.blocked(node, state):
            return None, await self.skip(node, state)
        step = await self.step(node, state, {"config": node.config.model_dump(mode="json"), "refs": self.input_refs(node, state)}, unblocked=True)
        if step["state"] == "completed":
            return None, self.update(node, state, step)
        if step["state"] in {"failed", "needs_review", "skipped"}:
            return None, self.recorded_failure(node, state, step)
        await self.save(node, step, "waiting", {"kind": "input_wait"})
        return step, None

    async def cached_dispatch(self, node, state):
        step = await self.host.store.call(lambda db: one(db, "SELECT * FROM workflow_steps WHERE id=? AND workflow_run_id=?", (state["dispatch"][node.id]["step_id"], self.run["id"])))
        require(step is not None and step["state"] == "completed", "RECEIPT_UNAVAILABLE", "Cached node evidence is unavailable")
        return {**self.update(node, state, step), "ready": {node.id: True}}

    async def accept_input(self, node, state, step, value):
        require(isinstance(value, dict) and "resumption_id" in value, "INVALID_REQUEST", "Workflow input needs its original durable resumption")
        reply = await self.host.store.call(lambda db: one(db, "SELECT id FROM workflow_resumptions WHERE id=? AND workflow_run_id=?", (value["resumption_id"], self.run["id"])))
        require(reply is not None, "PERMISSION_DENIED", "Input belongs to another workflow execution")
        await self.save(node, step, "completed", {"kind": "reply", "resumption_id": value["resumption_id"]})
        return self.update(node, state, step)

    async def settle(self, node, state):
        step, descriptor = await self.wait_descriptor(node, state)
        if descriptor["kind"] == "runs":
            values = await self.outputs(descriptor["run_ids"], include_results=False)
            if node.type == "map":
                members = await self.host.store.call(lambda db: rows(db, "SELECT * FROM workflow_map_members WHERE step_id=? ORDER BY ordinal", (step["id"],)))
                indexed = {value["run_id"]: value for value in values}
                values = [indexed[member["run_id"]] if member["run_id"] else {"run_id": None, "instance_id": member["instance_id"], "state": "failed", "error": json.loads(member["error_json"])} for member in members]
        else:
            values = [await self.service.get_run(identity, project_id=self.service.project_id(self.run)) for identity in descriptor["workflow_run_ids"]]
        pending = any(value["state"] in {"accepted", "running", "waiting"} for value in values)
        unsuccessful = [value for value in values if value["state"] not in {"completed", "accepted", "running", "waiting"}]
        if pending and not (descriptor.get("fail_fast") and unsuccessful):
            return {"ready": {node.id: False}}
        if any(value["state"] == "needs_review" for value in values) or unsuccessful and descriptor["policy"] != "all_settled":
            fault = RuntimeFault("OUTCOME_UNCERTAIN" if any(value["state"] == "needs_review" for value in values) else "WORKFLOW_CHILD_FAILED",
                                 "Required child work did not complete", details={"node_id": node.id, "children": [{key: value[key] for key in ("run_id", "state", "error") if key in value} for value in unsuccessful]})
            await self.save(node, step, "needs_review" if fault.code == "OUTCOME_UNCERTAIN" else "failed", error=fault)
            if self.definition.failure_policy == "fail_fast":
                raise fault
            return {**self.update(node, state, step, failed=fault.code), "ready": {node.id: True}}
        reference = json.loads(step["ref_json"])
        if node.type == "map":
            reference = {"kind": "map", "members": [{key: value[key] for key in ("run_id", "instance_id", "state", "error")} for value in values]}
        await self.save(node, step, "completed", reference)
        return {**self.update(node, state, step), "ready": {node.id: True}}
