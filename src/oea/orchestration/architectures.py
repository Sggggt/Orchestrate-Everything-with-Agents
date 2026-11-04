"""Project-owned architecture drafts, immutable versions and atomic bindings."""

import hashlib
import json
from pprint import pformat
from textwrap import indent
from importlib.metadata import version as package_version
from typing import Literal

from oea.catalog.service import need
from pydantic import ValidationError

from oea.contracts.architecture import AgentArchitecture, ArchitectureGraph, ArchitectureOptions, PythonGraphReference
from oea.contracts.configuration import AgentDefinition, ArchitectureBinding, manager_snapshot
from oea.contracts.domain import RunSpec
from oea.contracts.errors import require
from oea.orchestration.architecture_validation import NODE_CONFIGS, validate_graph
from oea.storage.persistence import encode, event, now_ms, one, rows


def execution_identity(execution):
    return execution.graph, execution.graph_version, execution.program, execution.architecture


def assert_switchable(db, agent_id):
    pending = switch_blocker(db, agent_id)
    require(not pending, "AGENT_IN_USE", "Finish pending work and reconcile original effects before switching architecture", details=pending or {})


def switch_blocker(db, agent_id):
    pending = one(db, "SELECT r.id,r.state FROM runs r JOIN instances i ON i.id=r.instance_id WHERE i.agent_id=? AND r.state IN ('accepted','running','waiting','needs_review') LIMIT 1", (agent_id,))
    if pending:
        return {"run_id": pending["id"], "state": pending["state"]}
    action = one(db, "SELECT a.id,a.run_id,a.state FROM actions a JOIN runs r ON r.id=a.run_id JOIN instances i ON i.id=r.instance_id WHERE i.agent_id=? AND a.state IN ('intent','dispatched','needs_review') LIMIT 1", (agent_id,))
    if action:
        return {"action_id": action["id"], "run_id": action["run_id"], "state": action["state"]}
    usage = one(db, "SELECT u.id,u.run_id,u.state FROM usage u JOIN runs r ON r.id=u.run_id JOIN instances i ON i.id=r.instance_id WHERE i.agent_id=? AND u.state!='settled' LIMIT 1", (agent_id,))
    return {"usage_id": usage["id"], "run_id": usage["run_id"], "state": usage["state"]} if usage else None


class Architectures:
    def __init__(self, host):
        self.host = host

    @staticmethod
    def _row(db, architecture_id, project_id):
        row = need(db, "agent_architectures", architecture_id)
        require(row["project_id"] == project_id, "PERMISSION_DENIED", "Architecture belongs to another project")
        return row

    @staticmethod
    def _version(db, binding, project_id):
        owner = Architectures._row(db, binding.architecture_id, project_id)
        row = one(db, "SELECT * FROM architecture_versions WHERE architecture_id=? AND version=?", (binding.architecture_id, binding.version))
        require(row is not None, "ARCHITECTURE_UNPUBLISHED", "Bind an immutable published architecture version")
        return owner, row

    def check_binding(self, db, definition):
        for execution in (definition.execution, definition.manager.execution if definition.manager else None):
            if execution and execution.architecture:
                require(definition.project_id is not None, "PROJECT_REQUIRED", "Project-owned architectures require a project-owned Agent")
                self._version(db, execution.architecture, definition.project_id)

    def check_save(self, db, definition, previous):
        self.check_binding(db, definition)
        if previous:
            old = AgentDefinition.model_validate_json(previous["config_json"])
            before = (execution_identity(old.execution), execution_identity(manager_snapshot(old).execution) if old.manager else None)
            after = (execution_identity(definition.execution), execution_identity(manager_snapshot(definition).execution) if definition.manager else None)
            if before != after:
                assert_switchable(db, definition.id)

    @staticmethod
    def _view(row, *, draft=True):
        value = {key: row[key] for key in ("id", "project_id", "name", "representation", "revision")}
        value["source"] = json.loads(row["source_json"])
        if draft:
            value["draft"] = json.loads(row["draft_json"])
        return value

    async def schema(self, representation: Literal["declarative", "python"] = "declarative"):
        return (ArchitectureGraph if representation == "declarative" else PythonGraphReference).model_json_schema()

    async def status(self, agent_id: str, project_id: str, *, role: Literal["main", "manager"] = "main"):
        """Bounded current binding and one concrete switch blocker, across all instances."""
        def read(db):
            row = need(db, "agent_definitions", agent_id)
            definition = AgentDefinition.model_validate_json(row["config_json"])
            require(definition.project_id in {None, project_id}, "PERMISSION_DENIED", "Agent belongs to another project")
            if role == "manager":
                require(definition.manager is not None, "MANAGER_UNAVAILABLE", "Configure the optional role first")
                definition = manager_snapshot(definition)
            execution = definition.execution
            return {"agent_id": agent_id, "revision": row["revision"], "role": role,
                    "binding": execution.architecture.model_dump(mode="json") if execution.architecture else None,
                    "graph": execution.graph, "graph_version": execution.graph_version, "program": bool(execution.program),
                    "blocker": switch_blocker(db, agent_id)}
        return await self.host.store.call(read)

    async def node_types(self, kind: str | None = None):
        """Discover a single primitive or the short Agent node directory."""
        if kind:
            require(kind in NODE_CONFIGS, "NOT_FOUND", "Unknown Agent primitive")
            return {"type": kind, "config_schema": NODE_CONFIGS[kind].model_json_schema()}
        return {"items": [{"type": name} for name in NODE_CONFIGS]}

    async def templates(self, name: str | None = None):
        from oea.orchestration.architecture_templates import templates
        values = templates()
        if name:
            require(name in values, "NOT_FOUND", "Unknown architecture template")
            return {"name": name, "version": 1, "graph": values[name].model_dump(mode="json")}
        return {"items": [{"name": key, "version": 1} for key in values]}

    async def create(self, definition: AgentArchitecture, draft: dict):
        """Save an incomplete draft; its stable ID is the creation idempotency key."""
        self.host.assert_open()
        definition = AgentArchitecture.model_validate(definition)
        encoded = encode(draft)
        require(len(encoded.encode()) <= 1024 * 1024, "INVALID_REQUEST", "Keep declarations bounded; store long content in source files")
        def write(db):
            need(db, "projects", definition.project_id)
            self.host.assert_project_available(definition.project_id)
            previous = one(db, "SELECT * FROM agent_architectures WHERE id=?", (definition.id,))
            if previous:
                require(previous["project_id"] == definition.project_id, "PERMISSION_DENIED", "Architecture belongs to another project")
                require(self._view(previous) == {**definition.model_dump(mode="json"), "revision": 1, "draft": draft}, "IDEMPOTENCY_CONFLICT", "Architecture ID has different content")
                return self._view(previous)
            preview = encode(self._preview(draft, definition.representation))
            db.execute("INSERT INTO agent_architectures VALUES(?,?,?,?,?,?,1,?,?,?)", (definition.id, definition.project_id, definition.name, definition.representation, encode(definition.source), encoded, now_ms(), now_ms(), preview))
            event(db, "architecture.created", project_id=definition.project_id, data={"architecture_id": definition.id, "revision": 1})
            return self._view(need(db, "agent_architectures", definition.id))
        value = await self.host.store.call(write, transaction=True)
        self.host.notify()
        return value

    async def get(self, architecture_id: str, project_id: str, *, version: int | None = None):
        """Read the complete draft or one immutable published definition on demand."""
        def read(db):
            owner = self._row(db, architecture_id, project_id)
            if version is None:
                return self._view(owner)
            _, row = self._version(db, ArchitectureBinding(architecture_id=architecture_id, version=version), project_id)
            return {**self._view(owner, draft=False), "revision": row["draft_revision"], "version": row["version"], "draft_revision": row["draft_revision"],
                    "definition": json.loads(row["definition_json"]), "dependencies": json.loads(row["dependencies_json"]), "sha256": row["sha256"]}
        return await self.host.store.call(read)

    async def list(self, project_id: str, *, cursor: int = 0, limit: int = 25):
        require(0 <= cursor < 2**63 and 1 <= limit <= 100, "INVALID_REQUEST", "Use a bounded page")
        def read(db):
            need(db, "projects", project_id)
            return rows(db, "SELECT rowid AS position,id,project_id,name,representation,revision,source_json FROM agent_architectures WHERE project_id=? AND rowid>? ORDER BY rowid LIMIT ?", (project_id, cursor, limit + 1))
        values = await self.host.store.call(read)
        return {"items": [self._view(row, draft=False) for row in values[:limit]], "cursor": values[limit - 1]["position"] if len(values) > limit else None}

    async def save(self, architecture_id: str, project_id: str, expected_revision: int, draft: dict):
        """Save a full declaration by revision, including incomplete advanced fields."""
        self.host.assert_open()
        encoded = encode(draft)
        require(len(encoded.encode()) <= 1024 * 1024, "INVALID_REQUEST", "Keep declarations bounded")
        def write(db):
            row = self._row(db, architecture_id, project_id)
            require(row["revision"] == expected_revision, "REVISION_CONFLICT", "Architecture draft changed")
            preview = encode(self._preview(draft, row["representation"]))
            db.execute("UPDATE agent_architectures SET draft_json=?,preview_json=?,revision=revision+1,updated_at=? WHERE id=?", (encoded, preview, now_ms(), architecture_id))
            event(db, "architecture.saved", project_id=project_id, data={"architecture_id": architecture_id, "revision": expected_revision + 1})
            return self._view(need(db, "agent_architectures", architecture_id))
        value = await self.host.store.call(write, transaction=True)
        self.host.notify()
        return value

    def _validate(self, owner, draft):
        if owner["representation"] == "python":
            try:
                reference = PythonGraphReference.model_validate(draft)
            except ValidationError as exc:
                return {"valid": False, "errors": [{"path": list(item["loc"]), "node_id": None, "message": item["msg"]} for item in exc.errors(include_input=False)], "warnings": []}
            key = reference.graph, reference.graph_version
            available = key in self.host.agent_graphs.factories
            return {"valid": available, "errors": [] if available else [{"path": ["graph"], "node_id": None, "message": "Register this exact Python graph factory and version in the host"}], "warnings": []}
        _, errors, warnings = validate_graph(draft, functions=self.host.workflows.functions)
        return {"valid": not errors, "errors": errors, "warnings": warnings}

    async def validate(self, architecture_id: str, project_id: str, *, expected_revision: int | None = None):
        """Check structure and currently available primitives without invoking a model or tool."""
        def read(db):
            row = self._row(db, architecture_id, project_id)
            require(expected_revision is None or row["revision"] == expected_revision, "REVISION_CONFLICT", "Architecture draft changed")
            return {**self._validate(row, json.loads(row["draft_json"])), "revision": row["revision"]}
        return await self.host.store.call(read)

    def _preview(self, draft, representation):
        if representation == "python":
            key = draft.get("graph"), draft.get("graph_version")
            if all(isinstance(item, str) for item in key) and key in self.host.agent_graphs.factories:
                preview = self.host.agent_graphs.preview(*key)
                return {name: preview[name] for name in ("stages", "omitted", "description", "editable")}
            return {"stages": [], "omitted": True, "description": "Register this graph version to inspect its known topology", "editable": False}
        raw_nodes = draft.get("nodes", [])
        nodes = [node for node in raw_nodes if isinstance(node, dict) and isinstance(node.get("id"), str) and len(node["id"]) <= 64] if isinstance(raw_nodes, list) else []
        by_id = {node.get("id"): node for node in nodes}
        outgoing = {}
        raw_edges = draft.get("edges", [])
        for edge in raw_edges if isinstance(raw_edges, list) else []:
            if isinstance(edge, dict) and isinstance(edge.get("source"), str) and isinstance(edge.get("target"), str):
                outgoing.setdefault(edge["source"], []).append({"target": edge["target"], "branch": edge.get("branch", "default")[:32] if isinstance(edge.get("branch", "default"), str) else "default"})
        ordered, visited = [], set()
        def walk(identity):
            if not isinstance(identity, str) or identity not in by_id or identity in visited:
                return
            visited.add(identity)
            ordered.append(by_id[identity])
            for edge in outgoing.get(identity, []):
                walk(edge.get("target"))
        walk(draft.get("entry"))
        ordered.extend(node for node in nodes if node.get("id") not in visited)
        visible = [node for node in ordered if isinstance(node.get("type"), str) and node["type"] in {"prepare", "model", "tools", "deliver", "subgraph"}][:8]
        metadata = draft.get("metadata") or {}
        preferred = metadata.get("preview_stages") if isinstance(metadata, dict) else None
        if isinstance(preferred, list) and all(isinstance(identity, str) and identity in by_id for identity in preferred):
            visible = [by_id[identity] for identity in dict.fromkeys(preferred)][:8]
        visible_ids = {node.get("id") for node in visible}
        links = []
        for source in visible_ids:
            pending = [(edge.get("target"), edge.get("branch", "default")) for edge in outgoing.get(source, [])]
            seen = set()
            while pending:
                target, branch = pending.pop()
                if (target, branch) in seen:
                    continue
                seen.add((target, branch))
                if target in visible_ids:
                    links.append({"source": source, "target": target, "branch": branch})
                else:
                    pending.extend((edge.get("target"), branch if branch != "default" else edge.get("branch", "default")) for edge in outgoing.get(target, []))
        def label(value, fallback, limit):
            return (value if isinstance(value, str) else fallback)[:limit]
        return {"stages": [{"id": node["id"], "title": label(node.get("title") or node["id"], node["id"], 120), "type": label(node.get("type"), "unknown", 64), "description": label(node.get("description"), "", 640)} for node in visible],
                "links": links[:24], "omitted": len(visible) != len(nodes) or len(links) > 24, "description": "Key stages; edit source for the complete topology", "editable": True,
                "options": {"max_steps": draft.get("max_steps", 512) if isinstance(draft.get("max_steps", 512), int) else None, **({"max_attempts": next((node["config"].get("value", 2) + 1 for node in nodes if node.get("id") == "bounded" and isinstance(node.get("config"), dict) and isinstance(node["config"].get("value", 2), int)), 3)} if isinstance(draft.get("source"), dict) and draft["source"].get("template") == "reflexion" else {})}}

    async def publish(self, architecture_id: str, project_id: str, expected_revision: int):
        """Publish an immutable candidate. Existing Agent bindings do not change."""
        self.host.assert_open()
        dependencies = {name: package_version(name) for name in ("langgraph", "langgraph-checkpoint-sqlite")}
        def write(db):
            owner = self._row(db, architecture_id, project_id)
            require(owner["revision"] == expected_revision, "REVISION_CONFLICT", "Architecture draft changed")
            existing = one(db, "SELECT version,sha256 FROM architecture_versions WHERE architecture_id=? AND draft_revision=?", (architecture_id, expected_revision))
            if existing:
                return {"architecture_id": architecture_id, **existing, "reused": True}
            draft = json.loads(owner["draft_json"])
            validation = self._validate(owner, draft)
            require(validation["valid"], "ARCHITECTURE_INVALID", "Fix the architecture diagnostics before publishing", details=validation)
            version = one(db, "SELECT COALESCE(MAX(version),0)+1 AS value FROM architecture_versions WHERE architecture_id=?", (architecture_id,))["value"]
            digest = hashlib.sha256(owner["draft_json"].encode()).hexdigest()
            db.execute("INSERT INTO architecture_versions VALUES(?,?,?,?,?,?,?,?)", (architecture_id, version, expected_revision, owner["draft_json"], encode(dependencies), encode(self._preview(draft, owner["representation"])), digest, now_ms()))
            event(db, "architecture.published", project_id=project_id, data={"architecture_id": architecture_id, "version": version})
            return {"architecture_id": architecture_id, "version": version, "sha256": digest, "reused": False}
        value = await self.host.store.call(write, transaction=True)
        self.host.notify()
        return value

    async def versions(self, architecture_id: str, project_id: str, *, cursor: int = 0, limit: int = 25):
        require(cursor >= 0 and 1 <= limit <= 100, "INVALID_REQUEST", "Use a bounded page")
        def read(db):
            self._row(db, architecture_id, project_id)
            return rows(db, "SELECT version,draft_revision,sha256,created_at FROM architecture_versions WHERE architecture_id=? AND version>? ORDER BY version LIMIT ?", (architecture_id, cursor, limit + 1))
        values = await self.host.store.call(read)
        return {"items": values[:limit], "cursor": values[limit - 1]["version"] if len(values) > limit else None}

    async def bind(self, agent_id: str, project_id: str, binding: ArchitectureBinding, expected_revision: int, *, role: Literal["main", "manager"] = "main"):
        """Explicitly switch one Agent role; acceptance and switch checks share a transaction."""
        saved = await self.host.agents.get(agent_id)
        definition = AgentDefinition.model_validate(saved["definition"])
        require(definition.project_id == project_id, "PERMISSION_DENIED", "Agent belongs to another project")
        require(saved["revision"] == expected_revision, "REVISION_CONFLICT", "Agent configuration changed")
        require(role != "manager" or definition.manager is not None, "MANAGER_UNAVAILABLE", "Configure a manager before binding it")
        execution = definition.execution if role == "main" else definition.manager.execution or definition.execution
        execution = execution.model_copy(update={"architecture": ArchitectureBinding.model_validate(binding), "graph": "default", "graph_version": "1", "program": ()})
        if role == "main":
            definition = definition.model_copy(update={"execution": execution})
        else:
            definition = definition.model_copy(update={"manager": definition.manager.model_copy(update={"execution": execution})})
        return await self.host.agents.save(definition, expected_revision=expected_revision)

    async def preview(self, architecture_id: str, project_id: str, *, version: int | None = None):
        """Read at most eight cached stages without loading the full draft or version."""
        def read(db):
            owner = one(db, "SELECT id,project_id,name,representation,revision,preview_json FROM agent_architectures WHERE id=?", (architecture_id,))
            require(owner is not None, "NOT_FOUND", "Architecture does not exist")
            require(owner["project_id"] == project_id, "PERMISSION_DENIED", "Architecture belongs to another project")
            if version is not None:
                row = one(db, "SELECT preview_json,draft_revision FROM architecture_versions WHERE architecture_id=? AND version=?", (architecture_id, version))
                require(row is not None, "ARCHITECTURE_UNPUBLISHED", "Published version does not exist")
                preview = json.loads(row["preview_json"])
                owner = {**owner, "revision": row["draft_revision"]}
            else:
                preview = json.loads(owner["preview_json"])
            return {**{key: owner[key] for key in ("id", "project_id", "name", "representation", "revision")}, "version": version,
                    **{key: preview[key] for key in ("stages", "links", "omitted", "description", "editable", "options") if key in preview}}
        return await self.host.store.call(read)

    async def compare(self, architecture_id: str, project_id: str, version: int, *, other_version: int | None = None):
        """Return changed JSON field paths against a candidate version or current draft."""
        first = await self.get(architecture_id, project_id, version=version)
        second = await self.get(architecture_id, project_id, version=other_version)
        changes = []
        def visit(left, right, path):
            if isinstance(left, dict) and isinstance(right, dict):
                for key in sorted(left.keys() | right.keys()):
                    if key not in left or key not in right:
                        changes.append(path + [key])
                    else:
                        visit(left[key], right[key], path + [key])
            elif left != right:
                changes.append(path)
        visit(first["definition"], second.get("definition", second.get("draft")), [])
        return {"architecture_id": architecture_id, "version": version, "other_version": other_version, "revision": second["revision"], "changed_paths": changes}

    async def export(self, architecture_id: str, project_id: str, *, version: int | None = None):
        """Return reproducible Python source; the caller writes it with authorized file tools."""
        value = await self.get(architecture_id, project_id, version=version)
        if value["representation"] == "python":
            definition = value.get("definition", value.get("draft"))
            key = definition.get("graph"), definition.get("graph_version")
            registered = self.host.agent_graphs.metadata.get(key, {}) if all(isinstance(item, str) for item in key) else {}
            return {"source": value["source"] or registered.get("source", {}), "editable": False, "message": "Edit the registered source and register a new factory version"}
        body = pformat(value.get("definition", value.get("draft")), width=96, sort_dicts=False)
        return {"filename": architecture_id + ".py", "source": "from oea import ArchitectureGraph\n\ngraph = ArchitectureGraph.model_validate(\n" + indent(body, "    ") + "\n)\n", "registered": False}

    async def adjust(self, architecture_id: str, project_id: str, expected_revision: int, options: ArchitectureOptions):
        """Change only template-exposed options, retaining all unshown draft fields."""
        options = ArchitectureOptions.model_validate(options)
        saved = await self.get(architecture_id, project_id)
        require(saved["revision"] == expected_revision, "REVISION_CONFLICT", "Architecture draft changed")
        require(saved["representation"] == "declarative", "ARCHITECTURE_READ_ONLY", "Edit Python architectures through their source")
        graph = saved["draft"]
        if options.max_steps is not None:
            graph["max_steps"] = options.max_steps
        if options.max_attempts is not None:
            require(graph.get("source", {}).get("template") == "reflexion", "OPTION_UNAVAILABLE", "Only the Reflexion template exposes max_attempts")
            node = next((node for node in graph.get("nodes", []) if node.get("id") == "bounded" and node.get("type") == "condition"), None)
            require(node is not None and node.get("config", {}).get("field") == "attempts" and node["config"].get("operator") == "ge", "OPTION_UNAVAILABLE", "The template option was changed in source; edit its source instead")
            node["config"]["value"] = options.max_attempts - 1
        return await self.save(architecture_id, project_id, expected_revision, graph)

    async def run(self, project_id: str, binding: ArchitectureBinding, spec: RunSpec):
        """Try a published candidate on an explicit instance without rebinding its Agent."""
        from oea.contracts.domain import RunHandle, RunSpec
        self.host.assert_open()
        spec = RunSpec.model_validate(spec)
        binding = ArchitectureBinding.model_validate(binding)
        def write(db):
            instance = need(db, "instances", spec.instance_id)
            require(instance["project_id"] == project_id, "PERMISSION_DENIED", "Instance belongs to another project")
            self._version(db, binding, project_id)
            return self.host.runs._submit(db, spec, architecture=binding)
        row, reused = await self.host.store.call(write, transaction=True)
        self.host.launch(row["id"])
        self.host.notify()
        return RunHandle(id=row["id"], instance_id=row["instance_id"], root_run_id=row["root_run_id"], root_workflow_run_id=row["root_workflow_run_id"], budget_id=row["budget_id"], state=row["state"], reused=reused)

    async def inspect(self, run_id: str, project_id: str, *, node_id: str | None = None, cursor: int = 0, limit: int = 25):
        """Read one bounded page of actual node activations and retained result references."""
        require(0 <= cursor < 2**63 and 1 <= limit <= 100, "INVALID_REQUEST", "Use a bounded page")
        def read(db):
            run = need(db, "runs", run_id)
            instance = need(db, "instances", run["instance_id"])
            require(instance["project_id"] == project_id, "PERMISSION_DENIED", "Run belongs to another project")
            values = rows(db, "SELECT rowid AS position,scope,node_id,activation,state,error_json,created_at,updated_at FROM architecture_steps WHERE run_id=? AND rowid>? AND (? IS NULL OR node_id=?) ORDER BY rowid LIMIT ?", (run_id, cursor, node_id, node_id, limit + 1))
            return {"run_id": run_id, "state": run["state"], "architecture": json.loads(run["config_json"]).get("execution", {}).get("architecture"), "items": [
                {**{key: value for key, value in row.items() if key != "error_json"}, "error": json.loads(row["error_json"]) if row["error_json"] else None} for row in values[:limit]],
                "cursor": values[limit - 1]["position"] if len(values) > limit else None}
        return await self.host.store.call(read)

    async def node_result(self, run_id: str, project_id: str, scope: str, node_id: str, activation: str):
        """Read a specific activation; full model/tool bodies stay in their original receipts."""
        def read(db):
            run = need(db, "runs", run_id)
            require(need(db, "instances", run["instance_id"])["project_id"] == project_id, "PERMISSION_DENIED", "Run belongs to another project")
            row = one(db, "SELECT state,output_json,error_json FROM architecture_steps WHERE run_id=? AND scope=? AND node_id=? AND activation=?", (run_id, scope, node_id, activation))
            require(row is not None, "NOT_FOUND", "Node activation does not exist")
            return {"run_id": run_id, "scope": scope, "node_id": node_id, "activation": activation, "state": row["state"],
                    "output": json.loads(row["output_json"]) if row["output_json"] else None, "error": json.loads(row["error_json"]) if row["error_json"] else None}
        return await self.host.store.call(read)
