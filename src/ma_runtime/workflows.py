"""Project-owned graph definitions and durable orchestration on the task scheduler.

Graph/viewport JSON is shared by the SDK, MCP and canvas. Deterministic nodes run
inside a store transaction; Agent nodes use ordinary fenced platform tasks. No
Python evaluation, additional worker pools or direct upstream tool calls occur here.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections import defaultdict

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError as SchemaValidationError
from pydantic import ValidationError

from .errors import RuntimeFault, require
from .models import (
    TERMINAL, WORKFLOW_CONFIGS, InstanceBatchSpec, PageEnvelope, TaskSpec, WorkflowEdge, WorkflowGraph,
    WorkflowInfo, WorkflowNode, WorkflowRunHandle, WorkflowRunSnapshot, WorkflowSummary,
    content_hash, json_dump, new_id, now_ms,
)
from .services import agent_project, need, public, resolve_project
from .store import emit, one, rows

TOKEN = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")
PATH = re.compile(r"^(inputs|nodes|item|index)(?:\.[\w-]+)*$")
LIVE = ("RUNNING", "WAITING", "UNCERTAIN", "CANCELLING", "FAILING")
LABELS = {"start": "开始", "agent": "Agent", "map": "并行实例", "condition": "条件分支", "template": "模板转换", "end": "结束"}


def default_graph():
    return WorkflowGraph(
        nodes=(WorkflowNode(id="start", type="start", title="开始", position={"x": 80, "y": 160}),
               WorkflowNode(id="end", type="end", title="结束", position={"x": 440, "y": 160},
                            config={"outputs": {"result": "{{inputs}}"}})),
        edges=(WorkflowEdge(id="start-end", source="start", target="end"),),
    )


def references(value, depth=0):
    require(depth <= 40, "INVALID_WORKFLOW", "节点参数嵌套过深。")
    if isinstance(value, str):
        yield from (m.group(1).strip() for m in TOKEN.finditer(value))
    elif isinstance(value, dict):
        if "$ref" in value:
            require(set(value) <= {"$ref", "default"} and isinstance(value["$ref"], str),
                    "INVALID_WORKFLOW", "变量引用仅允许 $ref 和可选 default。")
            yield value["$ref"]
        else:
            for child in value.values():
                yield from references(child, depth + 1)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from references(child, depth + 1)


def lookup(scope, path):
    require(isinstance(path, str) and len(path) <= 512 and PATH.fullmatch(path),
            "INVALID_VARIABLE", "变量路径应为 inputs.name 或 nodes.node_id.field。")
    current = scope
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdecimal() and int(part) < len(current):
            current = current[int(part)]
        else:
            raise RuntimeFault("VARIABLE_NOT_FOUND", f"变量 {path} 没有可用值。")
    return current


def resolve(value, scope, depth=0):
    require(depth <= 40, "INVALID_WORKFLOW", "节点参数嵌套过深。")
    if isinstance(value, dict):
        if "$ref" in value:
            try:
                return lookup(scope, value["$ref"])
            except RuntimeFault as exc:
                if exc.code == "VARIABLE_NOT_FOUND" and "default" in value:
                    return value["default"]
                raise
        return {key: resolve(child, scope, depth + 1) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [resolve(child, scope, depth + 1) for child in value]
    if not isinstance(value, str):
        return value
    exact = TOKEN.fullmatch(value)
    if exact:
        return lookup(scope, exact.group(1).strip())
    def replace(match):
        result = lookup(scope, match.group(1).strip())
        return result if isinstance(result, str) else json_dump(result)
    return TOKEN.sub(replace, value)


def analyse_graph(graph):
    """Collect useful draft diagnostics instead of requiring a runnable draft."""
    issues = []
    def error(code, message, **where):
        issues.append({"code": code, "message": message, **where})
    keyed = {node.id: node for node in graph.nodes}
    if len(keyed) != len(graph.nodes):
        error("DUPLICATE_NODE", "节点 ID 不能重复。")
    if len({edge.id for edge in graph.edges}) != len(graph.edges):
        error("DUPLICATE_EDGE", "连线 ID 不能重复。")
    starts = [node.id for node in graph.nodes if node.type == "start"]
    ends = [node.id for node in graph.nodes if node.type == "end"]
    if len(starts) != 1:
        error("START_REQUIRED", "工作流需要且只能有一个开始节点。")
    if not ends:
        error("END_REQUIRED", "工作流至少需要一个结束节点。")
    incoming, outgoing = defaultdict(list), defaultdict(list)
    seen = set()
    for edge in graph.edges:
        if edge.source not in keyed or edge.target not in keyed:
            error("MISSING_NODE", "连线引用了不存在的节点。", edge_id=edge.id)
            continue
        signature = (edge.source, edge.target, edge.source_handle)
        if signature in seen:
            error("DUPLICATE_CONNECTION", "请删除重复连线。", edge_id=edge.id)
        seen.add(signature)
        incoming[edge.target].append(edge)
        outgoing[edge.source].append(edge)
        source, target = keyed[edge.source], keyed[edge.target]
        if source.type == "end" or target.type == "start":
            error("INVALID_CONNECTION", "开始节点不能有输入，结束节点不能有输出。", edge_id=edge.id)
        if (source.type == "condition" and edge.source_handle not in {"true", "false"}) or (
                source.type != "condition" and edge.source_handle != "default"):
            error("INVALID_HANDLE", "连线端口与节点类型不匹配。", edge_id=edge.id)
    pending, order, ancestors = set(keyed), [], {}
    while pending:
        ready = [identity for identity in keyed if identity in pending
                 and all(edge.source not in pending for edge in incoming[identity])]
        if not ready:
            error("DEPENDENCY_CYCLE", "工作流包含循环依赖；请移除形成回路的连线。")
            break
        for identity in ready:
            ancestors[identity] = set()
            for edge in incoming[identity]:
                ancestors[identity].add(edge.source)
                ancestors[identity].update(ancestors.get(edge.source, ()))
        order.extend(ready)
        pending.difference_update(ready)
    reached, stack = set(), starts[:1]
    while stack:
        identity = stack.pop()
        if identity in reached:
            continue
        reached.add(identity)
        stack.extend(edge.target for edge in outgoing[identity])
    for node in graph.nodes:
        if node.id not in reached:
            error("UNREACHABLE_NODE", "节点未连接到开始节点。", node_id=node.id)
        if node.type != "end" and not outgoing[node.id]:
            error("DEAD_END", "请将节点连接到后续节点或结束节点。", node_id=node.id)
        if node.type == "condition" and {edge.source_handle for edge in outgoing[node.id]} != {"true", "false"}:
            error("BRANCH_REQUIRED", "条件节点需要连接“是”和“否”两个出口。", node_id=node.id)
        if node.type == "start":
            schema = node.config["input_schema"]
            try:
                Draft202012Validator.check_schema(schema)
                require(schema.get("type") == "object", "INVALID_SCHEMA", "开始节点的输入结构必须是 object。")
                def local_schema(value):
                    if isinstance(value, dict):
                        require(not any(key in value for key in ("$ref", "$dynamicRef")),
                                "INVALID_SCHEMA", "输入结构不支持引用其他 Schema。")
                        for child in value.values():
                            local_schema(child)
                    elif isinstance(value, list):
                        for child in value:
                            local_schema(child)
                local_schema(schema)
            except (SchemaError, RuntimeFault, RecursionError):
                error("INVALID_INPUT_SCHEMA", "输入结构须为有效的 object JSON Schema，且不包含 Schema 引用。", node_id=node.id)
            continue
        try:
            values = {"left": node.config["left"]} if node.type == "condition" and node.config["operator"] in {"is_empty", "is_not_empty"} else node.config
            scopes = [(node.config["items"], False), (node.config["payload"], True)] if node.type == "map" else [(values, False)]
            for value, item_scope in scopes:
                for path in references(value):
                    if not isinstance(path, str) or len(path) > 512 or not PATH.fullmatch(path):
                        error("INVALID_VARIABLE", "变量路径应为 inputs.name 或 nodes.node_id.field。", node_id=node.id)
                        continue
                    parts = path.split(".")
                    if parts[0] in {"item", "index"} and not item_scope:
                        error("INVALID_VARIABLE", "item/index 只能用于并行实例的 payload。", node_id=node.id)
                    if parts[0] == "nodes" and (len(parts) < 2 or parts[1] not in ancestors.get(node.id, ())):
                        error("VARIABLE_NOT_UPSTREAM", f"变量 {path} 必须来自已连接的前置节点。", node_id=node.id)
        except RuntimeFault as exc:
            error(exc.code, exc.safe_message, node_id=node.id)
    return {"valid": not issues, "errors": issues, "warnings": [], "order": order}


def condition(config, scope):
    op = config["operator"]
    left = resolve(config["left"], scope)
    right = None if op in {"is_empty", "is_not_empty"} else resolve(config["right"], scope)
    equal = (type(left) is type(right) or (type(left) in {int, float} and type(right) in {int, float})) and left == right
    if op == "equals":
        return equal
    if op == "not_equals":
        return not equal
    if op in {"is_empty", "is_not_empty"}:
        empty = left is None or left == "" or left == [] or left == {}
        return empty if op == "is_empty" else not empty
    if op == "contains":
        require(isinstance(left, (str, list, dict)) and (not isinstance(left, str) or isinstance(right, str)),
                "CONDITION_TYPE", "包含条件需要文本、列表或对象。")
        try:
            return right in left
        except TypeError:
            raise RuntimeFault("CONDITION_TYPE", "包含条件的值类型不匹配。") from None
    require(type(left) in {int, float} and type(right) in {int, float},
            "CONDITION_TYPE", "大小比较的两个值必须都是数字。")
    return {"gt": left > right, "gte": left >= right, "lt": left < right, "lte": left <= right}[op]


class WorkflowBuilder:
    """Build the same serializable DAG that the browser canvas edits."""
    def __init__(self):
        self._nodes, self._edges = [], []

    def node(self, identity, kind, *, title="", x=0, y=0, **config):
        self._nodes.append(WorkflowNode(id=identity, type=kind, title=title or LABELS.get(kind, kind),
                                        position={"x": x, "y": y}, config=config))
        return self

    def connect(self, source, target, *, branch="default"):
        self._edges.append(WorkflowEdge(id=f"edge-{len(self._edges) + 1}", source=source,
                                       target=target, source_handle=branch))
        return self

    def build(self):
        graph = WorkflowGraph(nodes=tuple(self._nodes), edges=tuple(self._edges))
        report = analyse_graph(graph)
        require(report["valid"], "INVALID_WORKFLOW", "工作流结构无效。", details=report)
        return graph


class Workflows:
    def __init__(self, rt):
        self.rt = rt
        self._plans = {}

    async def node_types(self):
        return [{"type": kind, "title": LABELS[kind], "config_schema": model.model_json_schema(),
                 "defaults": model().model_dump(mode="json"),
                 "outputs": ["true", "false"] if kind == "condition" else [] if kind == "end" else ["default"]}
                for kind, model in WORKFLOW_CONFIGS.items()]

    def _validation(self, db, project_id, graph):
        report = analyse_graph(graph)
        for node in graph.nodes:
            if node.type == "map":
                try:
                    self.rt.instances.validate_agents(db, project_id, node.config["agent_ids"])
                except RuntimeFault as exc:
                    report["errors"].append({"code": exc.code, "node_id": node.id, "message": exc.safe_message})
                continue
            if node.type != "agent":
                continue
            identity = node.config["agent_id"]
            if not identity or identity not in self.rt.adapters:
                report["errors"].append({"code": "AGENT_REQUIRED", "node_id": node.id, "message": "请选择已注册的项目 Agent。"})
            elif agent_project(db, identity) != project_id:
                report["errors"].append({"code": "PROJECT_MISMATCH", "node_id": node.id, "message": "Agent 必须属于工作流所在项目。"})
            elif self.rt.adapters[identity].describe().execution_mode == "external":
                report["errors"].append({"code": "EXTERNAL_AGENT", "node_id": node.id, "message": "请选择平台管理的执行端。"})
            else:
                from .llm import model_setup
                config = self.rt.agent_configs.get(identity)
                if config and (not config.enabled or model_setup(config)["state"] == "needs_configuration"):
                    report["warnings"].append({"code": "AGENT_CONFIGURATION", "node_id": node.id,
                                               "message": "Agent 已停用或尚未配置模型；运行后会等待配置。"})
        report["valid"] = not report["errors"]
        return report

    def _membership(self, db, project_id, graph):
        for node in graph.nodes:
            if node.type == "map" and node.config["agent_ids"]:
                self.rt.instances.validate_agents(db, project_id, node.config["agent_ids"])
            if node.type == "agent" and node.config["agent_id"] in self.rt.adapters:
                require(agent_project(db, node.config["agent_id"]) == project_id,
                        "PROJECT_MISMATCH", "工作流只能使用当前项目的 Agent。")

    def _info(self, db, row):
        value = public(row)
        for key in ("creation_key", "request_hash", "position"):
            value.pop(key, None)
        deletion = one(db, "SELECT deleted_at FROM workflow_deletions WHERE workflow_id=?", (row.id,))
        if deletion:
            value.update(state="DELETED", deleted_at=deletion.deleted_at)
        value["validation"] = self._validation(db, row.project_id, WorkflowGraph.model_validate(value["graph"]))
        return WorkflowInfo.model_validate(value)

    @staticmethod
    def _available(db, workflow_id):
        require(not one(db, "SELECT workflow_id FROM workflow_deletions WHERE workflow_id=?", (workflow_id,)),
                "WORKFLOW_DELETED", "请先从工作流回收站恢复此工作流。")

    async def validate(self, project_id, graph: WorkflowGraph | dict):
        require(isinstance(project_id, str) and 0 < len(project_id) <= 256, "PROJECT_REQUIRED", "请指定工作流所属项目。")
        try:
            parsed = WorkflowGraph.model_validate(graph)
        except ValidationError as exc:
            return {"valid": False, "errors": [{"code": "INVALID_NODE", "message": "节点或连线参数无效。",
                                                 "field": ".".join(map(str, item["loc"]))} for item in exc.errors()],
                    "warnings": [], "order": []}
        def check(db):
            need(db, "projects", project_id)
            return self._validation(db, project_id, parsed)
        return await self.rt.store.call(check)

    async def create(self, project_id, name, idempotency_key, graph: WorkflowGraph | dict | None = None, description=""):
        self.rt.admit()
        require(isinstance(project_id, str) and 0 < len(project_id) <= 256, "PROJECT_REQUIRED", "请指定工作流所属项目。")
        title, description = self._text(name, description)
        self._key(idempotency_key)
        parsed = WorkflowGraph.model_validate(graph if graph is not None else default_graph())
        parsed = WorkflowGraph.model_validate(self.rt.clean_json(parsed.model_dump(mode="json"), limit=1024 * 1024))
        digest = content_hash({"name": title, "description": description, "graph": parsed})
        def insert(db):
            resolve_project(db, project_id)
            old = one(db, "SELECT * FROM workflows WHERE project_id=? AND creation_key=?", (project_id, idempotency_key))
            if old:
                require(old.request_hash == digest, "IDEMPOTENCY_CONFLICT", "工作流创建键已有不同参数。")
                return self._info(db, old)
            self._membership(db, project_id, parsed)
            identity, stamp = new_id("wf"), now_ms()
            db.execute("INSERT INTO workflows(id,project_id,name,description,graph_json,creation_key,request_hash,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                       (identity, project_id, title, description, json_dump(parsed), idempotency_key, digest, stamp, stamp))
            emit(db, "workflow.created", payload={"workflow_id": identity, "project_id": project_id})
            return self._info(db, need(db, "workflows", identity))
        result = await self.rt.store.call(insert, transaction=True)
        self.rt.notify()
        return result

    @staticmethod
    def _key(value):
        require(isinstance(value, str) and 0 < len(value) <= 256, "INVALID_REQUEST", "请提供稳定的幂等键（1..256 字符）。")

    @staticmethod
    def _text(name, description):
        require(isinstance(name, str) and 0 < len(name.strip()) <= 120,
                "INVALID_REQUEST", "工作流名称须为 1..120 字符。")
        require(isinstance(description, str) and len(description) <= 4000,
                "INVALID_REQUEST", "工作流说明最多 4000 字符。")
        return name.strip(), description

    async def get(self, workflow_id):
        return await self.rt.store.call(lambda db: self._info(db, need(db, "workflows", workflow_id)))

    async def list(self, project_id, cursor=None, limit=25, include_archived=False, deleted_only=False):
        require(isinstance(project_id, str) and 0 < len(project_id) <= 256, "PROJECT_REQUIRED", "请指定工作流所属项目。")
        require(type(include_archived) is bool and type(deleted_only) is bool, "INVALID_REQUEST", "列表筛选必须为布尔值。")
        limit = max(1, min(100, int(limit)))
        def read(db):
            project = need(db, "projects", project_id)
            require(project.state != "DELETED", "PROJECT_DELETED", "请先恢复工作流所在项目。")
            found = rows(db, "SELECT w.rowid AS position,w.id,w.project_id,w.name,w.description,"
                         "CASE WHEN d.workflow_id IS NULL THEN w.state ELSE 'DELETED' END AS state,"
                         "w.revision,w.created_at,w.updated_at,d.deleted_at,"
                         "json_array_length(w.graph_json,'$.nodes') AS node_count,json_array_length(w.graph_json,'$.edges') AS edge_count "
                         "FROM workflows w LEFT JOIN workflow_deletions d ON d.workflow_id=w.id "
                         "WHERE w.project_id=? AND w.rowid>? "
                         + ("AND d.workflow_id IS NOT NULL " if deleted_only else
                            "AND d.workflow_id IS NULL " + ("" if include_archived else "AND w.state='ACTIVE' "))
                         + "ORDER BY w.rowid LIMIT ?",
                         (project_id, int(cursor or 0), limit + 1))
            return PageEnvelope(items=[WorkflowSummary.model_validate({key: value for key, value in row.items() if key != "position"}) for row in found[:limit]],
                                cursor=str(found[min(limit, len(found)) - 1].position) if found else cursor,
                                has_more=len(found) > limit)
        return await self.rt.store.call(read)

    async def update(self, workflow_id, expected_revision, graph: WorkflowGraph | dict | None = None, name=None, description=None):
        self.rt.admit()
        parsed = None if graph is None else WorkflowGraph.model_validate(
            self.rt.clean_json(WorkflowGraph.model_validate(graph).model_dump(mode="json"), limit=1024 * 1024))
        def save(db):
            old = need(db, "workflows", workflow_id)
            resolve_project(db, old.project_id)
            self._available(db, workflow_id)
            require(old.state == "ACTIVE", "INVALID_STATE", "请先恢复已归档的工作流。")
            require(type(expected_revision) is int and old.revision == expected_revision,
                    "REVISION_CONFLICT", "工作流已被其他编辑更新，请重新加载后再保存。")
            title, desc = self._text(old.name if name is None else name, old.description if description is None else description)
            if parsed is not None:
                self._membership(db, old.project_id, parsed)
            db.execute("UPDATE workflows SET name=?,description=?,graph_json=?,revision=revision+1,updated_at=? WHERE id=?",
                       (title, desc, json_dump(parsed) if parsed is not None else old.graph_json, now_ms(), workflow_id))
            emit(db, "workflow.updated", payload={"workflow_id": workflow_id, "project_id": old.project_id, "revision": old.revision + 1})
            return self._info(db, need(db, "workflows", workflow_id))
        result = await self.rt.store.call(save, transaction=True)
        self.rt.notify()
        return result

    async def archive(self, workflow_id, archived=True):
        self.rt.admit()
        require(type(archived) is bool, "INVALID_REQUEST", "archived 必须为布尔值。")
        def save(db):
            old = need(db, "workflows", workflow_id)
            resolve_project(db, old.project_id)
            self._available(db, workflow_id)
            db.execute("UPDATE workflows SET state=?,updated_at=? WHERE id=?", ("ARCHIVED" if archived else "ACTIVE", now_ms(), workflow_id))
            emit(db, "workflow.archived" if archived else "workflow.restored", payload={"workflow_id": workflow_id, "project_id": old.project_id})
            return self._info(db, need(db, "workflows", workflow_id))
        result = await self.rt.store.call(save, transaction=True)
        self.rt.notify()
        return result

    async def delete(self, workflow_id, confirmed=False):
        """Hide a definition reversibly; retain graph, revision and run evidence."""
        self.rt.admit()
        require(confirmed is True, "CONFIRMATION_REQUIRED", "请确认删除此工作流。")
        def remove(db):
            workflow = need(db, "workflows", workflow_id)
            if one(db, "SELECT workflow_id FROM workflow_deletions WHERE workflow_id=?", (workflow_id,)):
                return {"id": workflow_id, "deleted": True, "recoverable": True, "reused": True}
            resolve_project(db, workflow.project_id)
            require(not one(db, "SELECT id FROM workflow_runs WHERE workflow_id=? AND ended_at IS NULL LIMIT 1", (workflow_id,)),
                    "RESOURCE_BUSY", "工作流有未完成或待核对的运行，请先结束或取消并核对结果。")
            stamp = now_ms()
            db.execute("INSERT INTO workflow_deletions VALUES(?,?)", (workflow_id, stamp))
            db.execute("UPDATE workflows SET updated_at=? WHERE id=?", (stamp, workflow_id))
            emit(db, "workflow.deleted", payload={"workflow_id": workflow_id, "project_id": workflow.project_id})
            return {"id": workflow_id, "deleted": True, "recoverable": True, "reused": False}
        result = await self.rt.store.call(remove, transaction=True)
        self.rt.notify()
        return result

    async def restore(self, workflow_id):
        self.rt.admit()
        def restore(db):
            workflow = need(db, "workflows", workflow_id)
            resolve_project(db, workflow.project_id)
            if one(db, "SELECT workflow_id FROM workflow_deletions WHERE workflow_id=?", (workflow_id,)):
                db.execute("DELETE FROM workflow_deletions WHERE workflow_id=?", (workflow_id,))
                db.execute("UPDATE workflows SET updated_at=? WHERE id=?", (now_ms(), workflow_id))
                emit(db, "workflow.restored", payload={"workflow_id": workflow_id, "project_id": workflow.project_id})
            return self._info(db, need(db, "workflows", workflow_id))
        result = await self.rt.store.call(restore, transaction=True)
        self.rt.notify()
        return result

    async def run(self, workflow_id, inputs, idempotency_key, expected_revision=None):
        self.rt.admit()
        self._key(idempotency_key)
        inputs = self.rt.clean_json(inputs)
        require(isinstance(inputs, dict), "INVALID_REQUEST", "工作流输入必须为 JSON 对象。")
        digest = content_hash({"inputs": inputs, "expected_revision": expected_revision})
        def insert(db):
            # Receipt lookup precedes mutable state checks: a retry never starts a new revision.
            old = one(db, "SELECT * FROM workflow_runs WHERE workflow_id=? AND idempotency_key=?", (workflow_id, idempotency_key))
            if old:
                require(old.request_hash == digest, "IDEMPOTENCY_CONFLICT", "运行键已有不同参数。")
                return self._handle(old, reused=True)
            workflow = need(db, "workflows", workflow_id)
            resolve_project(db, workflow.project_id)
            self._available(db, workflow_id)
            require(workflow.state == "ACTIVE", "INVALID_STATE", "已归档的工作流不能开始运行。")
            require(expected_revision is None or (type(expected_revision) is int and workflow.revision == expected_revision),
                    "REVISION_CONFLICT", "工作流版本已改变，请重新加载后运行。")
            graph = WorkflowGraph.model_validate_json(workflow.graph_json)
            report = self._validation(db, workflow.project_id, graph)
            require(report["valid"], "INVALID_WORKFLOW", "请先修复工作流校验问题。", details=report)
            schema = next(node.config["input_schema"] for node in graph.nodes if node.type == "start")
            try:
                Draft202012Validator(schema).validate(inputs)
            except (SchemaValidationError, RecursionError):
                raise RuntimeFault("INVALID_INPUT", "运行输入不符合开始节点的输入结构。") from None
            require(one(db, "SELECT COUNT(*) AS count FROM workflow_runs WHERE ended_at IS NULL").count < 128,
                    "QUEUE_FULL", "同时未完成的工作流已达上限。")
            identity, session, stamp = new_id("wfr"), new_id("ses"), now_ms()
            db.execute("INSERT INTO sessions(id,state,title,metadata_json,created_at,project_id) VALUES(?,'OPEN',?,?,?,?)",
                       (session, f"{workflow.name} · r{workflow.revision}"[:120], json_dump({"workflow_id": workflow_id, "workflow_run_id": identity}), stamp, workflow.project_id))
            db.execute("INSERT INTO workflow_runs(id,workflow_id,project_id,session_id,revision,graph_json,inputs_json,state,idempotency_key,request_hash,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'RUNNING',?,?,?,?)",
                       (identity, workflow_id, workflow.project_id, session, workflow.revision, workflow.graph_json,
                        json_dump(inputs), idempotency_key, digest, stamp, stamp))
            for node in graph.nodes:
                db.execute("INSERT INTO workflow_node_runs(run_id,node_id,type,title) VALUES(?,?,?,?)",
                           (identity, node.id, node.type, node.title or LABELS[node.type]))
            emit(db, "session.created", session_id=session, payload={"title": workflow.name})
            emit(db, "workflow.run_started", session_id=session, payload={"workflow_id": workflow_id, "workflow_run_id": identity, "revision": workflow.revision})
            return self._handle(need(db, "workflow_runs", identity))
        result = await self.rt.store.call(insert, transaction=True)
        self.rt.notify()
        return result

    @staticmethod
    def _handle(row, reused=False):
        return WorkflowRunHandle(**{key: row[key] for key in ("id", "workflow_id", "project_id", "session_id", "revision", "state")}, reused=reused)

    async def get_run(self, run_id):
        from .presentation import markdown_summary

        live = {item["task_id"]: item["summary_markdown"] for item in self.rt.events.summaries.values()}

        def read(db):
            value = public(need(db, "workflow_runs", run_id))
            value.pop("idempotency_key", None)
            value.pop("request_hash", None)
            value["nodes"] = [public(row) for row in rows(db, "SELECT * FROM workflow_node_runs WHERE run_id=? ORDER BY rowid", (run_id,))]
            for node in value["nodes"]:
                if node.get("batch_id"):
                    node["instances"] = self.rt.instances.snapshot(db, node["batch_id"])["instances"]
                node["summary_markdown"] = live.get(node["task_id"], "")
                if node["output"] is not None:
                    result = one(db, "SELECT result_json FROM tasks WHERE id=?", (node["task_id"],)) if node["task_id"] else None
                    task_result = json.loads(result.result_json) if result and result.result_json else {}
                    node["summary_markdown"] = markdown_summary(task_result.get("summary_markdown") or node["output"])
            if value["result"] is not None:
                value["summary_markdown"] = markdown_summary(value["result"]["outputs"])
            return WorkflowRunSnapshot.model_validate(value)
        return await self.rt.store.call(read)

    async def runs(self, workflow_id, cursor=None, limit=25):
        limit = max(1, min(100, int(limit)))
        def read(db):
            need(db, "workflows", workflow_id)
            found = rows(db, "SELECT rowid AS position,* FROM workflow_runs WHERE workflow_id=? AND rowid<? ORDER BY rowid DESC LIMIT ?",
                         (workflow_id, int(cursor) if cursor is not None else 9223372036854775807, limit + 1))
            return PageEnvelope(items=[{**self._handle(row).model_dump(mode="json"), "created_at": row.created_at,
                                        "ended_at": row.ended_at, "error": json.loads(row.error_json) if row.error_json else None} for row in found[:limit]],
                                cursor=str(found[min(limit, len(found)) - 1].position) if found else cursor, has_more=len(found) > limit)
        return await self.rt.store.call(read)

    async def result(self, run_id, timeout=0):
        require(type(timeout) in {int, float} and 0 <= timeout <= 30, "INVALID_REQUEST", "等待时间须为 0..30 秒。")
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            run = await self.get_run(run_id)
            if run.state == "SUCCEEDED":
                return run.result
            if run.state == "UNCERTAIN" or run.state in TERMINAL:
                raise RuntimeFault("OUTCOME_UNCERTAIN" if run.state == "UNCERTAIN" else "WORKFLOW_" + run.state,
                                   "工作流需要核对执行结果。" if run.state == "UNCERTAIN" else f"工作流以 {run.state} 结束。",
                                   details={"run_id": run_id, "error": run.error})
            if asyncio.get_running_loop().time() >= deadline:
                raise RuntimeFault("WAIT_TIMEOUT", "等待超时，工作流仍在执行。", details={"run_id": run_id})
            await asyncio.sleep(0.05)

    async def cancel(self, run_id):
        self.rt.admit()
        def cancel(db):
            row = need(db, "workflow_runs", run_id)
            if row.state in TERMINAL:
                return {"accepted": False, "confirmed_stopped": True, "state": row.state}, set()
            if row.cancel_requested_at is None:
                db.execute("UPDATE workflow_runs SET cancel_requested_at=?,updated_at=? WHERE id=?", (now_ms(), now_ms(), run_id))
                emit(db, "workflow.cancel_requested", session_id=row.session_id, payload={"workflow_run_id": run_id})
            cancelled = self._stop_tasks(db, row)
            state = "UNCERTAIN" if row.state == "UNCERTAIN" else "FAILING" if row.error_json else "CANCELLING"
            self._state(db, row, state)
            return {"accepted": True, "confirmed_stopped": False, "state": state}, cancelled
        result, cancelled = await self.rt.store.call(cancel, transaction=True)
        for active in self.rt.active.values():
            if active.request.task_id in cancelled:
                active.context.cancelled.set()
        self.rt.notify()
        return result

    async def resume_node(self, run_id, node_id, input, idempotency_key, instance_index=None):
        require(isinstance(input, dict), "INVALID_REQUEST", "恢复输入必须为 JSON 对象。")
        self._key(idempotency_key)
        task_id = await self._node_task(run_id, node_id, continuing=True, instance_index=instance_index)
        return await self.rt.tasks.resume(task_id, input, idempotency_key)

    async def reconcile_node(self, run_id, node_id, resolution, instance_index=None):
        task_id = await self._node_task(run_id, node_id, instance_index=instance_index)
        return await self.rt.tasks.reconcile(task_id, resolution)

    async def _node_task(self, run_id, node_id, continuing=False, instance_index=None):
        def read(db):
            run = need(db, "workflow_runs", run_id)
            if continuing:
                require(run.cancel_requested_at is None and run.error_json is None and run.ended_at is None,
                        "INVALID_STATE", "已停止的工作流不能恢复节点。")
            node = one(db, "SELECT task_id,batch_id FROM workflow_node_runs WHERE run_id=? AND node_id=?", (run_id, node_id))
            require(node is not None, "NOT_FOUND", "节点不存在。")
            if node.batch_id:
                require(type(instance_index) is int and instance_index >= 0,
                        "INVALID_REQUEST", "并行节点需要指定 instance_index。")
                member = one(db, "SELECT task_id FROM instance_members WHERE batch_id=? AND ordinal=?", (node.batch_id, instance_index))
                require(member is not None, "NOT_FOUND", "实例不存在。")
                return member.task_id
            require(node.task_id and instance_index is None, "NOT_FOUND", "节点尚未创建 Agent 任务。")
            return node.task_id
        return await self.rt.store.call(read)

    async def tick(self):
        """Called by the existing scheduler; all scheduling receipts commit atomically."""
        def advance(db):
            before = db.total_changes()
            cancelled = self.rt.instances.cancel_children(db)
            for run in rows(db, "SELECT * FROM workflow_runs WHERE ended_at IS NULL ORDER BY created_at,rowid"):
                cancelled.update(self._advance(db, run))
            return cancelled, db.total_changes() != before
        cancelled, changed = await self.rt.store.call(advance, transaction=True)
        if changed:
            self.rt.events.notify()
        for active in self.rt.active.values():
            if active.request.task_id in cancelled:
                active.context.cancelled.set()

    def _set_node(self, db, run, node, state, *, inputs=None, output=None, error=None, branch=None, task_id=None, batch_id=None):
        if node.state == state:
            return
        stamp = now_ms()
        db.execute("UPDATE workflow_node_runs SET state=?,inputs_json=COALESCE(?,inputs_json),output_json=COALESCE(?,output_json),error_json=?,branch=COALESCE(?,branch),task_id=COALESCE(?,task_id),batch_id=COALESCE(?,batch_id),started_at=COALESCE(started_at,?),ended_at=? WHERE run_id=? AND node_id=?",
                   (state, json_dump(inputs) if inputs is not None else None, json_dump(output) if output is not None else None,
                    json_dump(error) if error else None, branch, task_id, batch_id, stamp if state != "SKIPPED" else None,
                    stamp if state in TERMINAL else None, run.id, node.node_id))
        node.update(state=state, inputs_json=json_dump(inputs) if inputs is not None else node.inputs_json,
                    output_json=json_dump(output) if output is not None else node.output_json, error_json=json_dump(error) if error else None,
                    branch=branch if branch is not None else node.branch, task_id=task_id or node.task_id,
                    batch_id=batch_id or node.batch_id)
        emit(db, "workflow.node_changed", session_id=run.session_id, task_id=node.task_id,
             payload={"workflow_run_id": run.id, "node_id": node.node_id, "state": state})

    def _state(self, db, run, state, *, result=None, error=None):
        if run.state == state and error is None:
            return
        stamp = now_ms()
        db.execute("UPDATE workflow_runs SET state=?,result_json=COALESCE(?,result_json),error_json=COALESCE(?,error_json),updated_at=?,ended_at=? WHERE id=?",
                   (state, json_dump(result) if result is not None else None, json_dump(error) if error else None,
                    stamp, stamp if state in TERMINAL else None, run.id))
        run.state = state
        if state in TERMINAL:
            self._plans.pop(run.id, None)
        if error:
            run.error_json = json_dump(error)
        emit(db, "workflow.run_changed", session_id=run.session_id, payload={"workflow_run_id": run.id, "state": state})

    def _stop_tasks(self, db, run):
        cancelled = set()
        for task in rows(db, "SELECT * FROM tasks WHERE session_id=? AND state NOT IN ('SUCCEEDED','FAILED','CANCELLED','SKIPPED','UNCERTAIN')", (run.session_id,)):
            if task.state == "RUNNING":
                active = one(db, "SELECT adapter_snapshot_json FROM runs WHERE task_id=? AND state='RUNNING' ORDER BY attempt_no DESC LIMIT 1", (task.id,))
                if not active or json.loads(active.adapter_snapshot_json)["cancellation"] == "unsupported":
                    continue  # Wait for natural completion; never claim an unsupported stop.
            if task.cancel_requested_at is None:
                db.execute("UPDATE tasks SET cancel_requested_at=?,updated_at=? WHERE id=?", (now_ms(), now_ms(), task.id))
                emit(db, "task.cancel_requested", session_id=task.session_id, thread_id=task.thread_id, task_id=task.id)
            if task.state in {"QUEUED", "WAITING"}:
                self.rt.finish_idle(db, task, "CANCELLED", None)
            cancelled.add(task.id)
        return cancelled

    def _agent_task(self, db, run, spec, payload):
        identity = spec.config["agent_id"]
        require(agent_project(db, identity) == run.project_id, "PROJECT_MISMATCH", "Agent 的项目归属已改变。")
        adapter = self.rt.adapter(identity)
        thread_id = new_id("thr")
        db.execute("INSERT INTO threads(id,session_id,agent_id,title,adapter_snapshot_json,created_at) VALUES(?,?,?,?,?,?)",
                   (thread_id, run.session_id, identity, spec.title or identity, json_dump(adapter.describe()), now_ms()))
        emit(db, "thread.created", session_id=run.session_id, thread_id=thread_id, payload={"agent_id": identity})
        task = TaskSpec(session_id=run.session_id, thread_id=thread_id, agent_id=identity, payload=payload,
                        idempotency_key=f"{run.id}:{spec.id}", attempt_timeout_s=spec.config["attempt_timeout_s"],
                        max_attempts=spec.config["max_attempts"], retry_policy=spec.config["retry_policy"],
                        metadata={"workflow_id": run.workflow_id, "workflow_run_id": run.id, "workflow_node_id": spec.id})
        return self.rt.tasks.insert(db, task).id

    def _advance(self, db, run):
        if run.id not in self._plans:
            graph = WorkflowGraph.model_validate_json(run.graph_json)
            incoming = defaultdict(list)
            for edge in graph.edges:
                incoming[edge.target].append(edge)
            self._plans[run.id] = (graph, analyse_graph(graph)["order"], incoming,
                                   {node.id: node for node in graph.nodes})
        graph, order, incoming, specs = self._plans[run.id]
        nodes = {row.node_id: row for row in rows(db, "SELECT * FROM workflow_node_runs WHERE run_id=?", (run.id,))}
        for node in nodes.values():
            if node.batch_id and node.state not in TERMINAL:
                batch = self.rt.instances.snapshot(db, node.batch_id)
                state = batch["state"]
                if run.cancel_requested_at is not None and state in TERMINAL:
                    state = "CANCELLED"
                if state not in {"UNCERTAIN", "CANCELLING"} and any(batch["counts"].get(key, 0) for key in ("FAILED", "CANCELLED", "SKIPPED")):
                    state = "CANCELLED" if run.cancel_requested_at is not None else "FAILED"
                if state == "CANCELLING":
                    state = "RUNNING"
                if state == "SUCCEEDED":
                    batch = self.rt.instances.snapshot(db, node.batch_id, include_outputs=True)
                output = {"outputs": [member["result"]["output"] for member in batch["instances"]],
                          "count": len(batch["instances"])} if state == "SUCCEEDED" else None
                self._set_node(db, run, node, state, output=output,
                               error={"code": "INSTANCE_FAILED", "batch_id": node.batch_id} if state == "FAILED" else None)
            if node.task_id and node.state not in TERMINAL:
                task = need(db, "tasks", node.task_id)
                output = json.loads(task.result_json)["output"] if task.result_json else None
                state = "RUNNING" if task.state == "QUEUED" else task.state
                self._set_node(db, run, node, state, output=output, error=json.loads(task.error_json) if task.error_json else None)
        failed = next((node for node in nodes.values() if node.state in {"FAILED", "CANCELLED"}), None)
        if failed and not run.error_json and run.cancel_requested_at is None:
            self._state(db, run, "FAILING", error={"code": "NODE_FAILED", "node_id": failed.node_id,
                                                    "safe_message": "工作流节点未成功完成。"})
        stopping = run.cancel_requested_at is not None or run.error_json is not None
        cancelled = self._stop_tasks(db, run) if stopping else set()
        if stopping:
            for node in nodes.values():
                if node.state == "PENDING":
                    self._set_node(db, run, node, "SKIPPED")
                elif node.task_id and node.state not in TERMINAL:
                    task = need(db, "tasks", node.task_id)
                    if task.state in TERMINAL:
                        self._set_node(db, run, node, task.state,
                                       output=json.loads(task.result_json)["output"] if task.result_json else None,
                                       error=json.loads(task.error_json) if task.error_json else None)
        unsettled = rows(db, "SELECT state FROM tasks WHERE session_id=? AND state NOT IN ('SUCCEEDED','FAILED','CANCELLED','SKIPPED')", (run.session_id,))
        if any(task.state == "UNCERTAIN" for task in unsettled):
            self._state(db, run, "UNCERTAIN")
            return cancelled
        if stopping:
            self._state(db, run, ("FAILING" if run.error_json else "CANCELLING") if unsettled else ("FAILED" if run.error_json else "CANCELLED"))
            return cancelled
        scope = {"inputs": json.loads(run.inputs_json), "nodes": {key: json.loads(node.output_json) for key, node in nodes.items() if node.state == "SUCCEEDED" and node.output_json}}
        for identity in order:
            node, spec = nodes[identity], specs[identity]
            if node.state != "PENDING":
                continue
            edges = incoming[identity]
            if any(nodes[edge.source].state not in TERMINAL for edge in edges):
                continue
            active = [edge for edge in edges if nodes[edge.source].state == "SUCCEEDED"
                      and (edge.source_handle == "default" or nodes[edge.source].branch == edge.source_handle)]
            if spec.type != "start" and not active:
                self._set_node(db, run, node, "SKIPPED")
                continue
            try:
                branch, payload = None, None
                if spec.type == "start":
                    output = scope["inputs"]
                elif spec.type == "agent":
                    payload = self.rt.clean_json(resolve(spec.config["payload"], scope))
                    # The queue can fill between nodes; defer without creating an orphan thread.
                    if one(db, "SELECT COUNT(*) AS count FROM tasks WHERE state='QUEUED'").count >= self.rt.config.max_queued_tasks:
                        continue
                    task_id = self._agent_task(db, run, spec, payload)
                    self._set_node(db, run, node, "RUNNING", inputs=payload, task_id=task_id)
                    continue
                elif spec.type == "map":
                    items = resolve(spec.config["items"], scope)
                    require(isinstance(items, list) and len(items) <= 128,
                            "INVALID_INPUT", "并行实例输入必须是最多 128 项的列表。")
                    if items:
                        if one(db, "SELECT COUNT(*) AS count FROM tasks WHERE state='QUEUED'").count + len(items) > self.rt.config.max_queued_tasks:
                            continue
                        payloads = [resolve(spec.config["payload"], {**scope, "item": item, "index": index})
                                    for index, item in enumerate(items)]
                        batch_spec = InstanceBatchSpec(session_id=run.session_id, agent_ids=spec.config["agent_ids"],
                            payloads=payloads, idempotency_key=f"{run.id}:{spec.id}",
                            **{key: spec.config[key] for key in ("max_concurrency", "routing", "attempt_timeout_s", "max_attempts", "retry_policy")})
                        batch = self.rt.instances.insert(db, batch_spec)
                        self._set_node(db, run, node, "RUNNING", inputs={"count": len(items)}, batch_id=batch["id"])
                        continue
                    output = {"outputs": [], "count": 0}
                elif spec.type == "condition":
                    passed = condition(spec.config, scope)
                    branch, output = "true" if passed else "false", {"result": passed}
                    payload = {key: resolve(spec.config[key], scope) for key in (("left",) if spec.config["operator"] in {"is_empty", "is_not_empty"} else ("left", "right"))}
                elif spec.type == "template":
                    text = resolve(spec.config["template"], scope)
                    output = {"text": text if isinstance(text, str) else json_dump(text)}
                else:
                    output = resolve(spec.config["outputs"], scope)
                output = self.rt.clean_json(output)
                self._set_node(db, run, node, "SUCCEEDED", inputs=payload, output=output, branch=branch)
                scope["nodes"][identity] = output
            except RuntimeFault as exc:
                self._set_node(db, run, node, "FAILED", error=exc.as_dict())
                self._state(db, run, "FAILING", error={"code": exc.code, "node_id": identity, "safe_message": exc.safe_message})
                return cancelled | self._stop_tasks(db, run)
        if all(node.state in TERMINAL for node in nodes.values()) and not unsettled:
            outputs = {identity: json.loads(node.output_json) for identity, node in nodes.items() if node.type == "end" and node.state == "SUCCEEDED"}
            if outputs:
                try:
                    result = self.rt.clean_json({"outputs": next(iter(outputs.values())) if len(outputs) == 1 else outputs})
                    self._state(db, run, "SUCCEEDED", result=result)
                except RuntimeFault as exc:
                    self._state(db, run, "FAILED", error=exc.as_dict())
            else:
                self._state(db, run, "FAILED", error={"code": "NO_OUTPUT", "safe_message": "没有执行到结束节点。"})
        else:
            self._state(db, run, "WAITING" if unsettled and all(task.state == "WAITING" for task in unsettled) else "RUNNING")
        return cancelled
