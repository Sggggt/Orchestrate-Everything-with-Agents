"""Single-owner lifecycle and direct graph execution; no resource scheduler."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import replace
from pathlib import Path

from oea.tools.gateway import ToolGateway
from oea.artifacts.service import Artifacts
from oea.budgets.service import Budgets
from oea.catalog.service import Agents, Instances, Projects, Sessions, need
from oea.contracts.configuration import AgentDefinition
from oea.context.store import ContextStore
from oea.contracts.errors import RuntimeFault, require
from oea.security.lock import WorkspaceLock
from oea.models.api import ModelAPI
from oea.models.execution import ModelExecutor
from oea.models.transport import ModelTransport
from oea.storage.persistence import Store, encode, event, now_ms, one, rows
from oea.interfaces.registry import OperationRegistry
from oea.runtime.context import BUILTIN_SCHEMAS, BuiltinTool, RunContext
from oea.runtime.runs import Runs
from oea.security.secrets import SecretVault
from oea.workspace.service import SCHEMAS, FileWrites, LocalTool, WorkspacePaths
from oea.orchestration.service import Workflows
from oea.orchestration.registry import AgentGraphs
from oea.collaboration.messages import Messages
from oea.collaboration.coordination import Coordination, CoordinationTool, MANAGER_TOOLS, INSTANCE_TOOLS
from oea.collaboration.dependencies import RunDependencies
from oea.storage.events import Events
from oea.storage.result_refs import ResultReferences
from oea.mcp.upstream import UpstreamMCP, UpstreamTool
from oea.stdio.agent import ProcessAgentTool, PROGRAM_TOOL
from oea.skills.service import Skills
from oea.migration.history import ImportedHistory


class Host:
    def __init__(self, state_dir=None, *, model_api=None, allow_commands=False):
        from oea.workspace.paths import resolve_data_dir
        self.root = resolve_data_dir(state_dir)
        self.lock, self.store = WorkspaceLock(self.root), Store(self.root)
        self.definitions, self.active, self.jobs = {}, {}, {}
        self.maintenance, self.reviewing = set(), set()
        self.changed = asyncio.Event()
        self.run_changes = {}
        self.subscribers = {}
        self.opened, self.closing = False, False
        self.vault = None
        self.transport = ModelTransport()
        self.model_api = model_api
        self.models = ModelExecutor(self)
        self.budgets = Budgets(self.store)
        self.contexts = ContextStore(self.store, redact=self.redact)
        self.tools = ToolGateway(self.store, authorize=self.authorize, record_usage=self.budgets.tool,
                                 record_progress=self.budgets.progress, redact=self.redact, on_settle=self.on_action_settle,
                                 authorize_intent=self.authorize_intent, on_intent_settle=self.on_intent_settle)
        self.projects, self.agents, self.sessions, self.instances = Projects(self), Agents(self), Sessions(self), Instances(self)
        self.runs, self.artifacts = Runs(self), Artifacts(self)
        self.messages = Messages(self)
        self.coordination, self.dependencies = Coordination(self), RunDependencies(self)
        self.events_service = Events(self)
        self.results = ResultReferences(self)
        self.mcp = UpstreamMCP(self)
        self.skills = Skills(self)
        self.history = ImportedHistory(self)
        self.pending_launches = set()
        self.project_deletion_scopes = {}
        self.workflows = Workflows(self)
        self.agent_graphs = AgentGraphs(self)
        from oea.orchestration.graphs import compile_agent_graph
        self.agent_graphs.register("default", compile_agent_graph, version="1")
        self.registry = OperationRegistry()
        self.file_writes = FileWrites()
        self.saver_scope = self.saver = self.graph = None
        for name in SCHEMAS:
            self.tools.register(LocalTool(name, resolve_run=self.resolve_workspace, publish=self.artifacts.publish, commands_enabled=allow_commands, file_writes=self.file_writes, image_artifact=self.artifacts.image_for_run))
        self.tools.register(ProcessAgentTool(self, enabled=allow_commands))
        for name in BUILTIN_SCHEMAS:
            self.tools.register(BuiltinTool(self, name))
        for name in sorted(MANAGER_TOOLS):
            self.tools.register(CoordinationTool(self, name))
        for area, service, names in (("projects", self.projects, ("create", "get", "list", "update", "delete")),
                                     ("agents", self.agents, ("save", "get", "list", "schema", "check", "probe", "remove", "set_credential")),
                                     ("sessions", self.sessions, ("create", "get", "list", "update", "set_archived", "set_mode", "summary", "unread")),
                                     ("instances", self.instances, ("create", "get", "list", "history", "mark_read")),
                                     ("runs", self.runs, ("submit", "get", "list", "inspect", "action", "attempts", "response", "result", "resume", "cancel", "confirm_action")),
                                     ("artifacts", self.artifacts, ("get", "for_run", "read", "list", "begin_upload", "get_upload", "commit_upload")),
                                     ("workflows", self.workflows, ("create", "get", "list", "update", "schema", "node_types", "validate", "activity", "set_archived", "remove", "restore", "run", "get_run", "list_runs", "inspect", "node_result", "members", "result", "resume", "cancel")),
                                     ("budgets", self.budgets, ("create", "get"))):
            for name in names:
                self.registry.register(area + "." + name, getattr(service, name), read_only=name in {"get", "list", "schema", "check", "inspect", "action", "attempts", "response", "result", "for_run", "read", "node_types", "get_run", "list_runs", "node_result", "members", "summary", "history", "validate", "activity", "get_upload", "unread"})
        for name in ("send", "get", "list", "mark_read", "unread"):
            self.registry.register("messages." + name, getattr(self.messages, name), read_only=name in {"get", "list", "unread"})
        self.registry.register("host.status", self.status, read_only=True)
        self.registry.register("host.describe", self.describe, read_only=True)
        self.registry.register("host.guide", self.guide, read_only=True)
        self.registry.register("executions.graphs", self.agent_graphs.list, read_only=True)
        self.registry.register("contexts.revoke", self.contexts.revoke)
        self.registry.register("events.list", self.events_service.list, read_only=True)
        self.registry.register("results.read", self.results.read, read_only=True)
        for name in ("save", "register_directory", "get", "inspect", "list", "schema", "discover", "tools", "calls", "call", "call_result", "cancel_call", "confirm_call", "disconnect", "set_credential", "remove", "codex_candidates", "import_codex"):
            self.registry.register("mcp." + name, getattr(self.mcp, name), read_only=name in {"get", "inspect", "list", "schema", "tools", "calls", "call_result", "codex_candidates"})
        for name in ("summary", "list", "get", "conversation", "usage"):
            self.registry.register("history." + name, getattr(self.history, name), read_only=True)
        for name in ("register", "register_directory", "get", "describe", "list", "schema", "refresh", "set_enabled", "remove", "read", "files"):
            self.registry.register("skills." + name, getattr(self.skills, name), read_only=name in {"get", "describe", "list", "schema", "read", "files"})

    async def __aenter__(self):
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
        from oea.contracts.configuration import Execution

        require(not self.opened and not self.closing, "HOST_ALREADY_OPEN", "Host lifecycle has already started")
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock.acquire()
        try:
            await self.store.open()
            self.vault = SecretVault(self.root)
            if self.model_api is None:
                self.model_api = ModelAPI(self.transport, secret=self.secret, redact=self.redact)
            definitions = await self.store.call(lambda db: rows(db, "SELECT config_json FROM agent_definitions"))
            self.definitions = {definition.id: definition for definition in (AgentDefinition.model_validate_json(row["config_json"]) for row in definitions) if not definition.removed}
            await self.mcp.open()
            await self.skills.open()
            self.saver_scope = AsyncSqliteSaver.from_conn_string(str(self.root / "graphs.sqlite"))
            self.saver = await self.saver_scope.__aenter__()
            await self.saver.setup()
            self.graph = self.agent_graphs.resolve(Execution())
            self.opened = True
            await self._recover()
            await self.workflows.open()
            await self.dependencies.open()
            return self
        except BaseException:
            if self.saver_scope is not None and self.saver is not None:
                await self.saver_scope.__aexit__(None, None, None)
            await self.transport.aclose()
            await self.store.close()
            self.lock.release()
            raise

    async def __aexit__(self, *_):
        await self.aclose()

    def assert_open(self):
        require(self.opened and not self.closing, "HOST_CLOSED", "Host cannot accept new work")

    async def status(self):
        from oea import __version__
        def read(db):
            return (rows(db, "SELECT state,COUNT(*) AS count FROM runs WHERE state IN ('accepted','running','waiting','needs_review') GROUP BY state"),
                    rows(db, "SELECT state,COUNT(*) AS count FROM workflow_runs WHERE state IN ('accepted','running','waiting','needs_review') GROUP BY state"))
        counts, workflow_counts = await self.store.call(read)
        workflows = {row["state"]: row["count"] for row in workflow_counts}
        active_workflows = workflows.get("running", 0) + len(set(self.workflows.computation_owners.values()) & self.workflows.reviewing)
        return {"version": __version__, "protocol_version": "2.0", "opened": self.opened, "closing": self.closing,
                "data_dir": str(self.root), "active_executions": len(self.active) + active_workflows,
                "active_agent_executions": len(self.active), "active_workflow_executions": active_workflows,
                "active_artifact_uploads": len(self.artifacts.receiving),
                "runs": {row["state"]: row["count"] for row in counts}, "workflows": workflows, "engine": "langgraph"}

    def assert_project_inactive(self, project_id):
        from oea.runtime.projects import assert_project_inactive
        assert_project_inactive(self, project_id)

    async def prepare_project_deletion(self, project_id):
        from oea.runtime.projects import prepare_project_deletion
        await prepare_project_deletion(self, project_id)

    def assert_project_available(self, project_id):
        require(project_id not in self.project_deletion_scopes, "PROJECT_DELETING", "Project deletion has revoked new work")

    async def on_project_deleted(self, project_id):
        from oea.runtime.projects import on_project_deleted
        await on_project_deleted(self, project_id)

    async def describe(self, operation: str):
        return self.registry.describe(operation)

    async def guide(self, area: str = "", action: str | None = None):
        """Discover one capability area or the exact current action contract."""
        return self.registry.guide(area, action)

    def redact(self, value):
        return self.vault.redact(value) if self.vault is not None else value

    def secret(self, reference):
        if not reference:
            return ""
        if reference.startswith("env:"):
            value = os.environ.get(reference[4:], "")
            if value:
                self.vault.known_secrets.add(value)
            return value
        require(reference.startswith("vault:"), "SECRET_REFERENCE_INVALID", "Choose an environment or vault secret reference")
        return self.vault.get(reference[6:])

    def register_tool(self, provider):
        self.tools.register(provider)

    def register_graph(self, name, factory, *, version):
        """Bind trusted Python code; saved runs retain the exact version."""
        self.agent_graphs.register(name, factory, version=version)

    def notify(self):
        changed, self.changed = self.changed, asyncio.Event()
        changed.set()

    def run_signal(self, run_id):
        signal = self.run_changes.get(run_id)
        if signal is None:
            signal = self.run_changes[run_id] = asyncio.Event()
        return signal

    def notify_run(self, run_id):
        signal = self.run_changes.pop(run_id, None)
        if signal is not None:
            signal.set()

    def on_action_settle(self, run_id):
        self.notify()
        self.mcp.after_settle()
        if self.closing or run_id not in self.reviewing or self.tools.has_inflight(run_id):
            return
        job = self.jobs.get(run_id)
        if job is not None and not job.done():
            return
        task = asyncio.create_task(self._after_settle(run_id))
        self.maintenance.add(task)
        def finished(completed):
            self.maintenance.discard(completed)
            if not completed.cancelled():
                completed.exception()
        task.add_done_callback(finished)

    async def authorize_intent(self, intent_id, name):
        await self.mcp.authorize_intent(intent_id, name)

    def on_intent_settle(self, intent_id):
        self.notify()
        self.mcp.after_settle()

    async def _after_settle(self, run_id):
        if self.closing or self.tools.has_inflight(run_id):
            return
        context = self.active.get(run_id)
        row = await self.store.call(lambda db: one(db, "SELECT * FROM runs WHERE id=?", (run_id,)))
        if row is None:
            self.reviewing.discard(run_id)
            return
        if row["state"] != "needs_review":
            self.reviewing.discard(run_id)
            return
        await self.store.call(lambda db: db.execute("UPDATE runs SET stopped_at=COALESCE(stopped_at,?) WHERE id=? AND state='needs_review'", (now_ms(), run_id)), transaction=True)
        if await self._unresolved(run_id):
            return
        if row["cancel_requested_at"] is not None:
            changed = await self.finish(run_id, "cancelled", expected_state="needs_review")
            if changed:
                await self._launch_next(row["instance_id"])
        else:
            previous_wait = json.loads(row["wait_json"]) if row["wait_json"] else {}
            changed = await self.finish(run_id, "waiting", wait={**previous_wait, "kind": "confirmation", "prompt": "The original action result is available. Explicitly resume this run to continue."}, expected_state="needs_review")
        if changed and self.active.get(run_id) is context:
            self.active.pop(run_id, None)

    async def emit_progress(self, run_id, frame):
        for queue in tuple(self.subscribers.get(run_id, ())):
            if queue.full():
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait({"kind": "resync", "run_id": run_id})
            else:
                queue.put_nowait({**self.redact(frame), "run_id": run_id})

    async def events(self, run_id):
        queue = asyncio.Queue(maxsize=256)
        subscribers = self.subscribers.setdefault(run_id, set())
        subscribers.add(queue)
        try:
            snapshot = await self.runs.get(run_id)
            yield {"kind": "run.snapshot", "run": snapshot, "run_id": run_id}
            while True:
                if snapshot["state"] == "completed":
                    result = await self.runs.result(run_id)
                    yield {"kind": "run.completed", "run_id": run_id, "result": result.model_dump(mode="json")}
                    return
                if snapshot["state"] in {"failed", "cancelled", "needs_review", "waiting"}:
                    yield {"kind": "run." + snapshot["state"], "run_id": run_id, "run": snapshot}
                    return
                frame = await queue.get()
                if frame["kind"] == "host.closed":
                    yield frame
                    return
                if frame["kind"].startswith("run.") or frame["kind"] == "resync":
                    snapshot = await self.runs.get(run_id)
                    if frame["kind"] == "resync":
                        yield frame
                else:
                    yield frame
        finally:
            subscribers.discard(queue)
            if not subscribers:
                self.subscribers.pop(run_id, None)

    async def authorize_model(self, context):
        self.assert_open()
        self.assert_project_available(context.instance["project_id"])
        current = self.definitions.get(context.instance["agent_id"])
        require(current is not None and current.enabled, "AGENT_DISABLED", "Agent is disabled")
        model = current.model
        if context.instance["role"] == "manager":
            session = await self.sessions.get(context.instance["session_id"])
            require(session["mode"] == "collaborative" and current.manager is not None, "MODE_CHANGED", "Manager is no longer enabled")
            model = current.manager.model
        require(model.credential_ref == context.definition.model.credential_ref, "MODEL_CONFIGURATION_CHANGED", "Model credentials changed; inspect and resume at an explicit boundary")
        require(context.definition.model.base_url and context.definition.model.model, "MODEL_NOT_CONFIGURED", "Save an endpoint and model before running")
        credential = self.secret(context.definition.model.credential_ref)
        require(not context.definition.model.credential_ref or credential, "MODEL_NOT_CONFIGURED", "The configured model credential is unavailable")

    async def authorize(self, run_id, name, *, source_path=None):
        self.assert_open()
        context = self.active.get(run_id)
        require(context is not None, "STALE_RUN", "Run no longer owns tool access", run_id=run_id)
        self.assert_project_available(context.instance["project_id"])
        current = self.definitions.get(context.instance["agent_id"])
        require(current is not None and current.enabled, "AGENT_DISABLED", "Agent is disabled")
        if name == PROGRAM_TOOL:
            require(context.instance["role"] == "main" and self.tools.providers[name].enabled and context.definition.execution.program
                    and context.definition.execution.program == current.execution.program, "PERMISSION_DENIED", "Process program needs its current Agent binding and host command grant")
            return
        context.check_tool_policy(name)
        if context.instance["role"] == "manager":
            session = await self.sessions.get(context.instance["session_id"])
            require(session["mode"] == "collaborative" and current.manager is not None, "MODE_CHANGED", "Manager is no longer enabled")
            require(name in MANAGER_TOOLS | {"read_context", "read_history", "read_action", "read_artifact", "wait_for_input"}, "PERMISSION_DENIED", "Manager has no business tools")
            return
        if name in INSTANCE_TOOLS:
            require(name in context.coordination_tools(), "PERMISSION_DENIED", "Communication capability is not currently bound")
            return
        if name in BUILTIN_SCHEMAS:
            if name == "read_skill":
                require(context.available_skills(), "PERMISSION_DENIED", "No Skill is currently granted")
            return
        if name.startswith("skill:"):
            binding = context.skill_binding(name[6:])
            require(source_path is None or source_path == str(binding.path), "PERMISSION_DENIED", "Skill evidence belongs to a different source directory")
            return
        if isinstance(self.tools.providers.get(name), UpstreamTool):
            await self.mcp.authorize(context, name)
            return
        before = (*context.definition.tools.local, *context.definition.tools.functions)
        after = (*current.tools.local, *current.tools.functions)
        require(name in before and name in after, "PERMISSION_DENIED", "Tool is not currently granted", run_id=run_id)

    async def resolve_workspace(self, run_id):
        context = self.active.get(run_id)
        require(context is not None, "STALE_RUN", "Run no longer owns this workspace")
        current = self.definitions[context.instance["agent_id"]]
        paths = context.paths
        configured_project = current.workspace.project
        if configured_project is None:
            project = await self.projects.get(context.instance["project_id"])
            configured_project = Path(project["folder"]) if project["folder"] else None
        current_project = configured_project.expanduser().resolve() if configured_project else None
        project = paths.project_dir if current_project == paths.project_dir else None
        references = {path.expanduser().resolve() for path in current.workspace.references}
        writable = {(current_project / relative).resolve() for relative in current.workspace.writable_project_paths} if current_project else set()
        paths = replace(paths, project_dir=project, reference_dirs=tuple(path for path in paths.reference_dirs if path in references),
                        writable_project_dirs=tuple(path for path in paths.writable_project_dirs if path in writable))
        return paths, context.definition.workspace

    def launch(self, run_id):
        if self.closing:
            return
        if run_id in self.jobs and not self.jobs[run_id].done():
            self.pending_launches.add(run_id)
            return
        task = asyncio.create_task(self._drive(run_id), name=run_id)
        self.jobs[run_id] = task
        def finished(completed):
            if self.jobs.get(run_id) is completed:
                self.jobs.pop(run_id, None)
            if not completed.cancelled():
                completed.exception()
            self.on_action_settle(run_id)
            if run_id in self.pending_launches:
                self.pending_launches.discard(run_id)
                self.launch(run_id)
        task.add_done_callback(finished)

    async def _drive(self, run_id):
        from langgraph.types import Command
        from langgraph.errors import GraphRecursionError
        from oea.contracts.execution import agent_steps, step_limit_fault
        from oea.orchestration.graphs import AgentGraphContext

        def claim(db):
            run = need(db, "runs", run_id)
            if run["state"] not in {"accepted", "running"}:
                return None
            instance = need(db, "instances", run["instance_id"])
            if instance["active_run_id"] and instance["active_run_id"] != run_id:
                return None
            if run["cancel_requested_at"]:
                return None
            db.execute("UPDATE instances SET active_run_id=? WHERE id=?", (run_id, instance["id"]))
            db.execute("UPDATE runs SET state='running',wait_json=NULL,updated_at=? WHERE id=?", (now_ms(), run_id))
            db.execute("UPDATE deliveries SET started_at=COALESCE(started_at,?) WHERE run_id=? AND applied_at IS NULL AND rejected_at IS NULL", (now_ms(), run_id))
            resumption = one(db, "SELECT * FROM run_resumptions WHERE run_id=? ORDER BY rowid DESC LIMIT 1", (run_id,))
            budget = need(db, "budgets", run["budget_id"])
            started = budget["started_at"] or now_ms()
            if budget["started_at"] is None:
                db.execute("UPDATE budgets SET started_at=? WHERE id=?", (started, budget["id"]))
            policy = json.loads(budget["policy_json"])
            deadline = started + policy["max_elapsed_seconds"] * 1000
            event(db, "run.started", project_id=instance["project_id"], session_id=instance["session_id"], instance_id=instance["id"], run_id=run_id)
            return run, instance, resumption, deadline, policy

        claimed = await self.store.call(claim, transaction=True)
        if claimed is None:
            return
        run, instance, resumption, deadline, budget_policy = claimed
        graph = config = None
        try:
            definition = AgentDefinition.model_validate_json(run["config_json"])
            graph = self.agent_graphs.resolve(definition.execution)
            paths = WorkspacePaths.prepare(definition.workspace, instance["id"], run_id, self.root, binding=json.loads(instance["workspace_json"]))
            await self.store.call(lambda db: db.execute("UPDATE instances SET workspace_json=? WHERE id=?", (encode(paths.as_dict()), instance["id"])), transaction=True)
            context = RunContext(self, run, instance, definition, paths, resumption=resumption)
            self.active[run_id] = context
            await self.mcp.bind(context)
            self.notify()
            effective = json.loads(resumption["execution_limits_json"]).get("effective", {}) if resumption else {}
            steps = effective.get("max_steps", agent_steps(definition.execution, budget_policy))
            config = {"configurable": {"thread_id": run_id}, "recursion_limit": steps}
            snapshot = await graph.aget_state(config)
            repair_codes = {"EMPTY_OUTPUT", "OUTPUT_TRUNCATED", "MODEL_REFUSED", "DELIVERY_MISSING", "DELIVERY_INVALID", "REQUIRED_TOOL_UNUSED", "REQUIRED_SKILL_UNUSED"}
            failure = json.loads(resumption["failure_json"]) if resumption and resumption["failure_json"] else {}
            if definition.execution.graph == "default" and failure.get("code") in repair_codes and "deliver" in snapshot.next:
                config = await graph.aupdate_state(config, {"decision": "program" if definition.execution.program else "model"},
                    as_node="wait" if definition.execution.program else "observe")
                snapshot = await graph.aget_state(config)
            interrupts = [value for task in snapshot.tasks for value in task.interrupts]
            if interrupts:
                require(resumption is not None and resumption["interrupt_id"] == interrupts[0].id,
                        "INPUT_REQUIRED", "The original graph is waiting for its matching reply")
                value = Command(resume={"request_id": resumption["id"]})
            else:
                value = None if snapshot.next else {"run_id": run_id, "instance_id": instance["id"]}
                if snapshot.values.get("result_ref"):
                    await self.finish(run_id, "completed")
                    return
                if resumption and not resumption["interrupt_id"] and (json.loads(resumption["input_json"]) is not None or resumption["failure_json"] is not None or resumption["source_message_id"] is not None):
                    await context.apply_reply(snapshot.values, {"request_id": resumption["id"]})
            remaining = (deadline - now_ms()) / 1000
            require(remaining > 0, "BUDGET_EXHAUSTED", "Work elapsed-time budget is exhausted")
            async with asyncio.timeout(remaining):
                result = await graph.ainvoke(value, config, context=AgentGraphContext(context))
            if result.get("__interrupt__"):
                interruption = result["__interrupt__"][0]
                wait = {**interruption.value, "interrupt_id": interruption.id}
                await self.finish(run_id, "waiting", wait=wait)
                if wait.get("kind") == "runs":
                    self.dependencies.watch(run_id, wait)
            else:
                await self.finish(run_id, "completed")
        except asyncio.CancelledError:
            await self.tools.cancel(run_id)
            row = await self.store.call(lambda db: need(db, "runs", run_id))
            pending = await self._unresolved(run_id)
            error = None if row["cancel_requested_at"] else RuntimeFault("HOST_CLOSED", "Execution stopped with the owned host; its checkpoint and evidence remain")
            await self.finish(run_id, "needs_review" if pending else "cancelled" if row["cancel_requested_at"] else "failed", error=error)
        except Exception as error:
            await self.tools.cancel(run_id)
            fault = error if isinstance(error, RuntimeFault) else RuntimeFault("BUDGET_EXHAUSTED", "Work elapsed-time budget is exhausted") if isinstance(error, TimeoutError) else RuntimeFault("EXECUTION_FAILED", "Graph execution failed", details={"cause": type(error).__name__})
            if isinstance(error, GraphRecursionError):
                saved = await graph.aget_state(config)
                fault = step_limit_fault(steps, definition.execution.graph, definition.execution.graph_version, saved.config, run_id=run_id)
            elif graph is not None and config is not None:
                saved = await graph.aget_state(config)
                fault.identity["evidence_ref"] = {**(fault.identity.get("evidence_ref") or {}), "checkpoint": saved.config}
            fault.identity.update(run_id=run_id, instance_id=instance["id"])
            if fault.code in {"BUDGET_EXHAUSTED", "EXECUTION_STEP_LIMIT"}:
                await self.store.call(lambda db: self.budgets.annotate_stop(db, run_id, fault))
            await self.finish(run_id, "needs_review" if await self._unresolved(run_id) else "failed", error=fault)
        finally:
            if not self.tools.has_inflight(run_id):
                self.active.pop(run_id, None)
            if not self.closing:
                await self._launch_next(instance["id"])

    async def _unresolved(self, run_id):
        return await self.store.call(lambda db: one(db, "SELECT 1 FROM actions WHERE run_id=? AND (state='needs_review' OR state='dispatched' AND side_effects!='none') LIMIT 1", (run_id,)))

    async def finish(self, run_id, state, *, error=None, wait=None, expected_state=None):
        locally_stopped = not self.tools.has_inflight(run_id)
        def write(db):
            run = need(db, "runs", run_id)
            if run["state"] in {"completed", "cancelled"} or expected_state is not None and run["state"] != expected_state:
                return False
            if state == "completed":
                require(run["result_json"] is not None, "DELIVERY_MISSING", "Graph ended without a persisted delivery")
            instance = need(db, "instances", run["instance_id"])
            db.execute("UPDATE runs SET state=?,error_json=?,wait_json=?,stopped_at=?,updated_at=? WHERE id=?",
                       (state, encode(self.redact(error.as_dict())) if error else None, encode(wait) if wait else None,
                        now_ms() if locally_stopped else None, now_ms(), run_id))
            if state not in {"waiting", "needs_review"}:
                db.execute("UPDATE instances SET active_run_id=NULL WHERE id=? AND active_run_id=?", (instance["id"], run_id))
            if state == "completed":
                db.execute("UPDATE deliveries SET completed_at=COALESCE(completed_at,?) WHERE run_id=? AND applied_at IS NOT NULL AND rejected_at IS NULL", (now_ms(), run_id))
            event(db, "run." + state, project_id=instance["project_id"], session_id=instance["session_id"], instance_id=instance["id"], run_id=run_id,
                  data={"state": state, "error": self.redact(error.as_dict()) if error else None, "wait": wait})
            return True
        changed = await self.store.call(write, transaction=True)
        if not changed:
            return False
        if state == "needs_review":
            self.reviewing.add(run_id)
        else:
            self.reviewing.discard(run_id)
        self.notify_run(run_id)
        self.notify()
        await self.emit_progress(run_id, {"kind": "run." + state, "state": state})
        return True

    async def _launch_next(self, instance_id):
        row = await self.store.call(lambda db: one(db, "SELECT r.id FROM runs r JOIN instances i ON i.id=r.instance_id WHERE r.instance_id=? AND r.state='accepted' AND i.active_run_id IS NULL ORDER BY r.rowid LIMIT 1", (instance_id,)))
        if row:
            self.launch(row["id"])

    async def _recover(self):
        def recover(db):
            interrupted = rows(db, "SELECT * FROM actions WHERE state='dispatched'")
            for action in interrupted:
                model = action["name"] == "model.complete"
                unknown = action["side_effects"] != "none"
                error = {"code": "OUTCOME_UNCERTAIN" if unknown else "MODEL_OUTCOME_UNKNOWN" if model else "TOOL_INTERRUPTED",
                         "safe_message": "Host restart requires checking the original invocation", "action_id": action["id"]}
                db.execute("UPDATE actions SET state=?,error_json=?,updated_at=? WHERE id=?",
                           ("needs_review" if unknown else "failed", encode(error), now_ms(), action["id"]))
                db.execute("UPDATE usage SET state='needs_review' WHERE action_id=? AND state='reserved'", (action["id"],))
                event(db, "model.failed" if model else "action.needs_review" if unknown else "action.failed", run_id=action["run_id"], intent_id=action["intent_id"], data={"action_id": action["id"], "error": error})
            return rows(db, "SELECT id,instance_id,state FROM runs WHERE state IN ('accepted','running','needs_review') ORDER BY rowid")
        pending = await self.store.call(recover, transaction=True)
        for run in pending:
            if run["state"] == "needs_review":
                self.reviewing.add(run["id"])
                await self._after_settle(run["id"])
                continue
            if await self._unresolved(run["id"]):
                await self.finish(run["id"], "needs_review", error=RuntimeFault("OUTCOME_UNCERTAIN", "Inspect the original unresolved actions"))
            else:
                self.launch(run["id"])

    async def aclose(self):
        if self.closing:
            return
        self.closing = True
        await self.artifacts.aclose()
        await self.dependencies.aclose()
        await self.workflows.aclose()
        pending = tuple(self.jobs.values())
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        await self.mcp.aclose()
        await self.tools.aclose()
        await asyncio.gather(*tuple(self.maintenance), return_exceptions=True)
        self.active.clear()
        for run_id in tuple(self.subscribers):
            await self.emit_progress(run_id, {"kind": "host.closed"})
        if self.saver_scope is not None:
            await self.saver_scope.__aexit__(None, None, None)
        self.agent_graphs.compiled.clear()
        await self.transport.aclose()
        await self.store.close()
        self.lock.release()
        self.opened = False
        for signal in self.run_changes.values():
            signal.set()
        self.run_changes.clear()
        self.notify()
