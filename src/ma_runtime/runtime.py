"""Single-host scheduling, fenced commits and conservative recovery."""

from __future__ import annotations

import asyncio
import json
import os
import random
import shutil
from dataclasses import dataclass

from .artifacts import ArtifactStore
from .context import RunContext
from .errors import AdapterFailure, RuntimeFault, require
from .events import EventHub
from .llm import ConfiguredLLM, model_required
from .lock import WorkspaceLock
from .models import (
    TERMINAL,
    AdapterEvent,
    AgentConfig,
    ExecutionRequest,
    ProbeResult,
    ResourceRequirement,
    RuntimeConfig,
    TaskSpec,
    WaitCondition,
    content_hash,
    json_dump,
    new_id,
    now_ms,
)
from .paths import default_data_dir, external_path, require_local_state
from .resources import ResourceCoordinator
from .secrets import SecretVault
from .services import Agents, Messages, Projects, Sessions, Tasks, Threads, need, public
from .store import StoreActor, emit, one, rows

LOCAL_WORKERS = {}


def process_alive(pid):
    if not pid:
        return False
    if os.name == "nt":
        import ctypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.GetExitCodeProcess.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_ulong),
        ]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5
        try:
            code = ctypes.c_ulong()
            return (
                not kernel.GetExitCodeProcess(handle, ctypes.byref(code))
                or code.value == 259
            )
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


@dataclass
class ActiveRun:
    request: ExecutionRequest
    adapter: object
    context: RunContext
    spec: TaskSpec
    worker: asyncio.Task | None = None
    driver: asyncio.Task | None = None
    committed: bool = False
    started: bool = False


class Runtime:
    def __init__(self, workspace=None, config=None, adapters=None):
        self.root = external_path(workspace or default_data_dir())
        require_local_state(self.root)
        self.config = (
            config
            if isinstance(config, RuntimeConfig)
            else RuntimeConfig.model_validate(config or {})
        )
        self.config = self.config.model_copy(deep=True)
        self.adapters = dict(adapters or {})
        self.agent_configs = {}
        self.retired_adapters = []
        self.configuration_lock = asyncio.Lock()
        self.host_instance_id = new_id("host")
        self.lock = WorkspaceLock(self.root)
        self.store = StoreActor(self.root)
        self.resources = ResourceCoordinator()
        self.projects = Projects(self)
        self.sessions, self.agents, self.threads = (
            Sessions(self),
            Agents(self),
            Threads(self),
        )
        self.tasks, self.messages = Tasks(self), Messages(self)
        from .workflows import Workflows

        self.workflows = Workflows(self)
        self.events = EventHub(self)
        from .mcp_service import MCPRegistry

        self.mcp = MCPRegistry(self)
        self.active: dict[str, ActiveRun] = {}
        self.uncertain_slots = set()
        self.wake = asyncio.Event()
        self.closing = False
        self.closed = False
        self.accepting = False
        self.scheduler = None
        self.last_session = None

    @classmethod
    def open(cls, workspace=None, config=None, adapters=None):
        return cls(workspace, config, adapters)

    async def __aenter__(self):
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock.acquire()
        try:
            await self.store.open()
            self.vault = SecretVault(self.root)
            self.artifacts = ArtifactStore(self)
            from .tools import ToolGateway

            self.tools = ToolGateway(self)
            configs = await self.store.call(
                lambda db: rows(db, "SELECT * FROM agent_configs")
            )
            for row in configs:
                config = AgentConfig.model_validate_json(row.config_json)
                self.agent_configs[config.id] = config
                self.adapters.setdefault(config.id, self.make_adapter(config))
            for adapter in self.adapters.values():
                self.validate_adapter(adapter)
            await self.mcp.open()
            await self.recover()
            self.accepting = True
            self.scheduler = asyncio.create_task(
                self.schedule_loop(), name="ma-scheduler"
            )
            return self
        except BaseException:
            try:
                await self.mcp.aclose()
                await self.store.close()
            finally:
                self.lock.release()
            raise

    async def __aexit__(self, *_):
        await self.aclose()

    def admit(self):
        require(
            self.accepting
            and not self.closing
            and not self.closed
            and not self.store.failed,
            "STORE_UNAVAILABLE" if self.store.failed else "RUNTIME_CLOSING",
            "Runtime cannot accept new commands",
        )

    def clean_json(self, value, limit=None):
        value = self.vault.redact(value)
        try:
            data = json_dump(value)
        except (ValueError, TypeError):
            raise RuntimeFault(
                "INVALID_REQUEST", "Payload must be finite JSON"
            ) from None
        require(
            len(data.encode("utf-8")) <= (limit or self.config.max_message_bytes),
            "PAYLOAD_TOO_LARGE",
            "Payload exceeds the configured size limit",
        )
        return value

    def notify(self):
        self.events.notify()
        self.wake.set()

    def make_adapter(self, config):
        if config.kind == "mock":
            from agent.mock import MockAgent

            return MockAgent(config.id, max_concurrency=config.max_concurrency)
        if config.kind == "llm":
            from agent.llm import LLMApiAgent

            return LLMApiAgent(config, self.vault.get(config.id))
        if config.kind == "process":
            from agent.subprocess import JsonlAgent

            return JsonlAgent(
                config.id,
                config.command,
                self.root,
                max_concurrency=config.max_concurrency,
                descriptor=config.process_descriptor,
            )
        from agent.external import ExternalAgent

        return ExternalAgent(config.id)

    def validate_adapter(self, adapter):
        descriptor = adapter.describe()
        require(
            descriptor.protocol_version.split(".")[0] == "1",
            "UNSUPPORTED_CAPABILITY",
            "Agent protocol major version does not match",
        )
        if self.config.strict_concurrency:
            require(
                descriptor.nested_execution != "unmanaged",
                "UNSUPPORTED_CAPABILITY",
                "Strict concurrency rejects unmanaged nested executions",
            )

    def adapter(self, agent_id):
        adapter = self.adapters.get(agent_id)
        require(adapter is not None, "NOT_FOUND", "Agent is not registered")
        require(
            adapter.describe().execution_mode != "external",
            "UNSUPPORTED_CAPABILITY",
            "External coordinators execute in their own clients",
        )
        return adapter

    def check_run(self, db, run_id):
        run = need(db, "runs", run_id)
        task = need(db, "tasks", run.task_id)
        thread = need(db, "threads", task.thread_id)
        require(
            task.state == "RUNNING"
            and run.state == "RUNNING"
            and thread.owner_task_id == task.id
            and thread.active_run_id == run.id
            and thread.revision == run.expected_revision
            and run.host_instance_id == self.host_instance_id,
            "STALE_RUN",
            "Execution is no longer the current thread writer",
            run_id=run_id,
            task_id=task.id,
        )
        return task, run

    def check_artifacts(self, db, session_id, artifact_ids):
        for identity in artifact_ids:
            artifact = need(db, "artifacts", identity)
            require(
                artifact.session_id == session_id and artifact.state == "READY",
                "ARTIFACT_INVALID",
                "Artifact belongs to another workspace or is unavailable",
            )

    def check_collaboration(self, run):
        reference = (
            json.loads(run.adapter_run_ref_json) if run.adapter_run_ref_json else {}
        )
        require(
            reference.get("scopes", {}).get("collaboration") is True,
            "PERMISSION_DENIED",
            "This run is not allowed to submit tasks or send messages",
        )

    def append_reply(self, db, task, run_id, result):
        from .presentation import result_summary
        text = result.output.get("text")
        if not isinstance(text, str):
            text = json_dump(result.output)
        text = self.vault.redact(text)
        db.execute(
            "INSERT INTO conversation(id,session_id,thread_id,task_id,run_id,role,content,metadata_json,created_at,summary_markdown) VALUES(?,?,?,?,?,'assistant',?,?,?,?)",
            (
                new_id("chat"),
                task.session_id,
                task.thread_id,
                task.id,
                run_id,
                text,
                json_dump({"artifact_ids": result.artifact_ids}),
                now_ms(),
                self.vault.redact(result_summary(result)),
            ),
        )

    def finish_idle(self, db, task, state, error):
        task = need(db, "tasks", task.id)
        if task.state not in {"QUEUED", "WAITING"}:
            return
        db.execute(
            "UPDATE tasks SET state=?,error_json=?,queue_reason=NULL,wait_json=NULL,updated_at=? WHERE id=?",
            (state, json_dump(error) if error else None, now_ms(), task.id),
        )
        db.execute(
            "UPDATE threads SET owner_task_id=NULL,blocked_reason=NULL WHERE id=? AND owner_task_id=? AND active_run_id IS NULL",
            (task.thread_id, task.id),
        )
        emit(
            db,
            "task." + state.lower(),
            session_id=task.session_id,
            thread_id=task.thread_id,
            task_id=task.id,
            payload={"state": state, "error": error},
        )

    def validate_wait(self, db, task, wait):
        if wait.kind == "tasks":
            require(
                wait.task_ids, "INVALID_REQUEST", "Task wait must name dependencies"
            )
            graph = {}
            for entry in rows(
                db,
                "SELECT id,wait_json FROM tasks WHERE session_id=?",
                (task.session_id,),
            ):
                current = json.loads(entry.wait_json) if entry.wait_json else {}
                graph[entry.id] = set(current.get("task_ids", ()))
            for entry in rows(
                db,
                "SELECT t.id,h.owner_task_id FROM tasks t JOIN threads h ON h.id=t.thread_id WHERE t.session_id=? AND t.state='QUEUED' AND h.owner_task_id IS NOT NULL AND h.owner_task_id!=t.id",
                (task.session_id,),
            ):
                graph.setdefault(entry.id, set()).add(entry.owner_task_id)
            for dependency in rows(
                db,
                "SELECT d.task_id,d.depends_on_task_id FROM task_dependencies d JOIN tasks t ON t.id=d.task_id WHERE t.session_id=?",
                (task.session_id,),
            ):
                graph.setdefault(dependency.task_id, set()).add(
                    dependency.depends_on_task_id
                )
            graph.setdefault(task.id, set()).update(wait.task_ids)
            for identity in wait.task_ids:
                target = need(db, "tasks", identity)
                require(
                    target.session_id == task.session_id,
                    "INVALID_REQUEST",
                    "Waited tasks must share a workspace",
                )
                require(
                    identity != task.id
                    and (
                        target.thread_id != task.thread_id or target.state in TERMINAL
                    ),
                    "DEPENDENCY_CYCLE",
                    "Cannot wait on work blocked behind this thread",
                )
            visiting, visited, stack = set(), set(), [(task.id, False)]
            while stack:
                identity, leaving = stack.pop()
                if leaving:
                    visiting.remove(identity)
                    visited.add(identity)
                    continue
                if identity in visited:
                    continue
                require(
                    identity not in visiting,
                    "DEPENDENCY_CYCLE",
                    "Wait dependencies contain a cycle",
                )
                visiting.add(identity)
                stack.append((identity, True))
                stack.extend((child, False) for child in graph.get(identity, ()))
        elif wait.kind == "message":
            agent = self.adapter(TaskSpec.model_validate_json(task.spec_json).agent_id)
            require(
                wait.port in agent.describe().ports,
                "INVALID_REQUEST",
                "Wait port is not registered",
            )
        elif wait.kind == "input" and wait.wait_token is None:
            wait.wait_token = new_id("wait")

    async def commit_boundary(self, run_id, event, local_seq=1):
        event = AdapterEvent.model_validate(event)
        require(
            event.type in {"completed", "suspended"},
            "ADAPTER_PROTOCOL_ERROR",
            "A commit must contain a final boundary",
        )
        require(
            isinstance(local_seq, int) and local_seq > 0,
            "ADAPTER_PROTOCOL_ERROR",
            "Boundary sequence must be positive",
        )
        # Boundaries can contain the complete provider context; still bounded.
        event = AdapterEvent.model_validate(
            self.clean_json(event.model_dump(mode="json"), limit=8 * 1024 * 1024)
        )
        for message in event.outgoing_messages:
            self.clean_json(message.model_dump(mode="json"))
        if event.result:
            self.clean_json(event.result.model_dump(mode="json"), limit=524288)
        digest = content_hash(event)

        def commit(db):
            prior = need(db, "runs", run_id)
            if prior.boundary_receipt_json:
                require(
                    prior.boundary_seq == local_seq and prior.boundary_hash == digest,
                    "IDEMPOTENCY_CONFLICT",
                    "Boundary sequence has different content",
                )
                return json.loads(prior.boundary_receipt_json)
            task, run = self.check_run(db, run_id)
            thread = need(db, "threads", task.thread_id)
            descriptor = json.loads(run.adapter_snapshot_json)
            if event.type == "suspended":
                require(
                    descriptor["supports_suspend"]
                    and descriptor["checkpoint_mode"] != "none",
                    "UNSUPPORTED_CAPABILITY",
                    "Execution endpoint cannot suspend",
                )
                self.validate_wait(db, task, event.wait)
            if event.checkpoint:
                require(
                    event.checkpoint.mode == descriptor["checkpoint_mode"]
                    and event.checkpoint.adapter_version
                    == descriptor["adapter_version"]
                    and event.checkpoint.format_version
                    == descriptor["checkpoint_format_version"],
                    "UNSUPPORTED_CAPABILITY",
                    "Checkpoint format does not match the execution endpoint",
                )
            offered = {
                m.id
                for m in rows(
                    db,
                    "SELECT id FROM messages WHERE target_run_id=? AND state='OFFERED'",
                    (run_id,),
                )
            }
            require(
                set(event.applied_message_ids) <= offered,
                "ADAPTER_PROTOCOL_ERROR",
                "Receipt contains messages not offered to this run",
            )
            require(
                not event.applied_message_ids
                or descriptor["message_receipts"] == "next_turn_ack",
                "UNSUPPORTED_CAPABILITY",
                "Execution endpoint does not support message receipts",
            )
            if event.result:
                self.check_artifacts(db, task.session_id, event.result.artifact_ids)
            unresolved = one(
                db,
                "SELECT id FROM tool_calls WHERE run_id=? AND state IN ('DISPATCHED','UNCERTAIN') LIMIT 1",
                (run_id,),
            )
            require(
                unresolved is None,
                "OUTCOME_UNCERTAIN",
                "A tool outcome must be reconciled before committing",
            )
            checkpoint_id = thread.latest_checkpoint_id
            revision = thread.revision + 1
            if event.checkpoint:
                checkpoint_id = new_id("chk")
                cp = event.checkpoint
                db.execute(
                    "INSERT INTO checkpoints VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        checkpoint_id,
                        thread.id,
                        revision,
                        cp.adapter_version,
                        cp.format_version,
                        cp.mode,
                        json_dump(cp.ref),
                        run_id,
                        now_ms(),
                    ),
                )
            for identity in event.applied_message_ids:
                db.execute(
                    "UPDATE messages SET state='APPLIED',applied_checkpoint_id=?,updated_at=? WHERE id=?",
                    (checkpoint_id, now_ms(), identity),
                )
                emit(
                    db,
                    "message.applied",
                    session_id=task.session_id,
                    thread_id=thread.id,
                    task_id=task.id,
                    run_id=run_id,
                    payload={"message_id": identity, "state": "APPLIED"},
                )
            # Preserve source validation while the run is still current.
            if event.outgoing_messages:
                self.check_collaboration(run)
            for outgoing in event.outgoing_messages:
                require(
                    outgoing.session_id == task.session_id,
                    "PERMISSION_DENIED",
                    "Cross-session boundary message is not allowed",
                )
                outgoing = outgoing.model_copy(
                    update={
                        "source_thread_id": thread.id,
                        "source_task_id": task.id,
                        "source_run_id": run_id,
                    }
                )
                self.messages.insert(db, outgoing, caller_run_id=run_id)
            # Unacknowledged offers stay OFFERED until the stopped run is reconciled.
            state = "WAITING" if event.type == "suspended" else "SUCCEEDED"
            receipt = {
                "run_id": run_id,
                "task_id": task.id,
                "thread_revision": revision,
                "checkpoint_id": checkpoint_id,
                "state": state,
                "local_seq": local_seq,
            }
            db.execute(
                "UPDATE runs SET state=?,boundary_seq=?,boundary_hash=?,boundary_receipt_json=?,ended_at=? WHERE id=?",
                (state, local_seq, digest, json_dump(receipt), now_ms(), run_id),
            )
            db.execute(
                "UPDATE tasks SET state=?,result_json=?,wait_json=?,resume_input_json=NULL,error_json=NULL,queue_reason=NULL,updated_at=? WHERE id=?",
                (
                    state,
                    json_dump(event.result) if event.result else None,
                    json_dump(event.wait) if event.wait else None,
                    now_ms(),
                    task.id,
                ),
            )
            db.execute(
                "UPDATE threads SET revision=?,latest_checkpoint_id=?,active_run_id=NULL,owner_task_id=?,blocked_reason=NULL WHERE id=?",
                (
                    revision,
                    checkpoint_id,
                    task.id if state == "WAITING" else None,
                    thread.id,
                ),
            )
            if event.result:
                self.append_reply(db, task, run_id, event.result)
            emit(
                db,
                "task.suspended" if state == "WAITING" else "task.completed",
                session_id=task.session_id,
                thread_id=thread.id,
                task_id=task.id,
                run_id=run_id,
                payload={
                    "state": state,
                    "thread_revision": revision,
                    "wait": event.wait.model_dump() if event.wait else None,
                },
            )
            return receipt

        try:
            receipt = await self.store.call(commit, transaction=True)
        except RuntimeFault as exc:
            if exc.code == "STALE_RUN":
                await self.store.call(
                    lambda db, code=exc.code: emit(
                        db,
                        "run.stale_rejected",
                        run_id=run_id,
                        payload={"code": code},
                    ),
                    transaction=True,
                )
            raise
        if run_id in self.active:
            self.active[run_id].committed = True
        self.notify()
        return receipt

    async def close_sessions(self):
        def close(db):
            for session in rows(db, "SELECT * FROM sessions WHERE state='CLOSING'"):
                unfinished = one(
                    db,
                    "SELECT id FROM tasks WHERE session_id=? AND state NOT IN ('SUCCEEDED','FAILED','CANCELLED','SKIPPED') LIMIT 1",
                    (session.id,),
                )
                if not unfinished:
                    db.execute(
                        "UPDATE sessions SET state='CLOSED',closed_at=? WHERE id=?",
                        (now_ms(), session.id),
                    )
                    emit(db, "session.closed", session_id=session.id)

        await self.store.call(close, transaction=True)

    async def wake_waiting(self):
        def wake(db):
            for task in rows(db, "SELECT * FROM tasks WHERE state='WAITING'"):
                wait = WaitCondition.model_validate_json(task.wait_json)
                spec = TaskSpec.model_validate_json(task.spec_json)
                deadlines = [
                    d
                    for d in (spec.deadline_at_ms, wait.deadline_at_ms)
                    if d is not None
                ]
                if deadlines and min(deadlines) <= now_ms():
                    self.finish_idle(db, task, "FAILED", {"code": "DEADLINE_EXCEEDED"})
                    continue
                ready, input_value = False, {}
                if wait.kind == "tasks":
                    dependencies = [
                        need(db, "tasks", identity) for identity in wait.task_ids
                    ]
                    if all(d.state in TERMINAL for d in dependencies):
                        if wait.policy == "ALL_SUCCEEDED" and any(
                            d.state != "SUCCEEDED" for d in dependencies
                        ):
                            self.finish_idle(
                                db, task, "FAILED", {"code": "WAIT_DEPENDENCY_FAILED"}
                            )
                            continue
                        ready = True
                        input_value = {
                            "tasks": {
                                d.id: {
                                    "state": d.state,
                                    "result": (
                                        json.loads(d.result_json)
                                        if d.result_json
                                        else None
                                    ),
                                }
                                for d in dependencies
                            }
                        }
                elif wait.kind == "message":
                    pending = rows(
                        db,
                        "SELECT * FROM messages WHERE target_thread_id=? AND target_port=? AND state='PENDING'",
                        (task.thread_id, wait.port),
                    )
                    ready = any(
                        all(
                            json.loads(m.envelope_json)["payload"].get(k) == v
                            for k, v in wait.match.items()
                        )
                        for m in pending
                    )
                if ready:
                    db.execute(
                        "UPDATE tasks SET state='QUEUED',queue_reason='RESUME',resume_input_json=?,updated_at=? WHERE id=?",
                        (json_dump(input_value), now_ms(), task.id),
                    )
                    emit(
                        db,
                        "task.resumed",
                        session_id=task.session_id,
                        thread_id=task.thread_id,
                        task_id=task.id,
                    )

        await self.store.call(wake, transaction=True)

    async def schedule_loop(self):
        try:
            while not self.closing:
                self.wake.clear()
                await self.wake_waiting()
                await self.workflows.tick()
                await self.dispatch()
                await self.close_sessions()
                try:
                    await asyncio.wait_for(self.wake.wait(), timeout=0.15)
                except TimeoutError:
                    pass
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self.accepting = False
            if not self.store.failed:
                await self.store.call(
                    lambda db, error=exc: emit(
                        db,
                        "runtime.scheduler_stopped",
                        payload={
                            "code": (
                                error.code
                                if isinstance(error, RuntimeFault)
                                else "INTERNAL_ERROR"
                            )
                        },
                    ),
                    transaction=True,
                )

    async def dispatch(self):
        capacity = self.config.max_concurrency - self.config.reserved_external_slots
        if (
            len(self.active) + len(self.uncertain_slots) >= capacity
            or self.store.failed
        ):
            return
        candidates = await self.store.call(
            lambda db: rows(
                db, "SELECT * FROM tasks WHERE state='QUEUED' ORDER BY created_at,rowid"
            )
        )
        groups = {}
        for task in candidates:
            groups.setdefault(task.session_id, []).append(task)
        sessions = list(groups)
        if self.last_session in sessions:
            index = sessions.index(self.last_session) + 1
            sessions = sessions[index:] + sessions[:index]
        while sessions and len(self.active) + len(self.uncertain_slots) < capacity:
            session_id = sessions.pop(0)
            group = groups[session_id]
            task = group.pop(0)
            if group:
                sessions.append(session_id)
            self.last_session = session_id
            spec = TaskSpec.model_validate_json(task.spec_json)
            reason = None
            adapter = self.adapters.get(spec.agent_id)
            config = self.agent_configs.get(spec.agent_id)
            if spec.deadline_at_ms is not None and spec.deadline_at_ms <= now_ms():
                await self.store.call(
                    lambda db, t=task: self.finish_idle(
                        db, t, "FAILED", {"code": "DEADLINE_EXCEEDED"}
                    ),
                    transaction=True,
                )
                continue
            if task.not_before > now_ms():
                reason = "RETRY_BACKOFF"
            elif adapter is None:
                reason = "AGENT_UNAVAILABLE"
            elif config and not config.enabled:
                reason = "AGENT_DISABLED"
            elif model_required(config) and not (config.base_url and config.model):
                reason = "MODEL_CONFIGURATION"
            elif (
                sum(a.request.agent_id == spec.agent_id for a in self.active.values())
                >= adapter.describe().max_concurrency
            ):
                reason = "AGENT_LIMIT"
            dependencies = await self.store.call(
                lambda db, t=task: rows(
                    db,
                    "SELECT d.* FROM tasks d JOIN task_dependencies e ON e.depends_on_task_id=d.id WHERE e.task_id=?",
                    (t.id,),
                )
            )
            if any(d.state not in TERMINAL for d in dependencies):
                reason = "DEPENDENCIES"
            elif spec.dependency_policy == "ALL_SUCCEEDED" and any(
                d.state != "SUCCEEDED" for d in dependencies
            ):
                await self.store.call(
                    lambda db, t=task: self.finish_idle(
                        db, t, "SKIPPED", {"code": "DEPENDENCY_FAILED"}
                    ),
                    transaction=True,
                )
                continue
            thread = await self.store.call(
                lambda db, t=task: need(db, "threads", t.thread_id)
            )
            if (
                thread.active_run_id
                or (thread.owner_task_id and thread.owner_task_id != task.id)
                or any(a.request.thread_id == thread.id for a in self.active.values())
            ):
                reason = "THREAD"
            if not self.resources.can_acquire("", spec.resources):
                reason = "RESOURCES"
            if reason:
                if task.queue_reason != reason:
                    await self.store.call(
                        lambda db, t=task, r=reason: db.execute(
                            "UPDATE tasks SET queue_reason=? WHERE id=? AND state='QUEUED'",
                            (r, t.id),
                        ),
                        transaction=True,
                    )
                continue
            run_id = new_id("run")
            self.resources.acquire(run_id, spec.resources)
            try:
                async with self.configuration_lock:
                    adapter = self.adapter(spec.agent_id)
                    config = self.agent_configs.get(spec.agent_id)
                    model_client = (
                        ConfiguredLLM(config, self.vault.get(config.id))
                        if config and config.kind in {"llm", "process"}
                        else None
                    )
                    current_capacity = (
                        self.config.max_concurrency
                        - self.config.reserved_external_slots
                    )
                    if (
                        len(self.active) + len(self.uncertain_slots) >= current_capacity
                        or (config and not config.enabled)
                        or (
                            model_required(config)
                            and not (config.base_url and config.model)
                        )
                        or sum(
                            a.request.agent_id == spec.agent_id
                            for a in self.active.values()
                        )
                        >= adapter.describe().max_concurrency
                    ):
                        self.resources.release(run_id)
                        continue
                    request = await self.store.call(
                        lambda db, t=task, s=spec, a=adapter, rid=run_id: self.prepare(
                            db, t.id, s, a, rid
                        ),
                        transaction=True,
                    )
            except RuntimeFault as exc:
                self.resources.release(run_id)
                if exc.code in {"UNSUPPORTED_CAPABILITY", "ADAPTER_PROTOCOL_ERROR"}:
                    await self.store.call(
                        lambda db, t=task, e=exc: self.finish_idle(
                            db, t, "FAILED", e.as_dict()
                        ),
                        transaction=True,
                    )
                    continue
                raise
            except BaseException:
                self.resources.release(run_id)
                raise
            if request is None:
                self.resources.release(run_id)
                continue
            context = RunContext(
                self,
                request,
                collaboration=config.collaboration if config else True,
                model_client=model_client,
            )
            active = ActiveRun(request, adapter, context, spec)
            self.active[run_id] = active
            active.driver = asyncio.create_task(self.execute_run(active), name=run_id)
            self.events.notify()

    def prepare(self, db, task_id, spec, adapter, run_id):
        task = need(db, "tasks", task_id)
        thread = need(db, "threads", task.thread_id)
        if (
            task.state != "QUEUED"
            or thread.active_run_id
            or (thread.owner_task_id and thread.owner_task_id != task_id)
        ):
            return None
        descriptor = adapter.describe()
        profile = self.agent_configs.get(spec.agent_id)
        checkpoint = None
        if thread.latest_checkpoint_id:
            cp = need(db, "checkpoints", thread.latest_checkpoint_id)
            require(
                cp.adapter_version == descriptor.adapter_version
                and cp.format_version == descriptor.checkpoint_format_version
                and cp.mode == descriptor.checkpoint_mode,
                "UNSUPPORTED_CAPABILITY",
                "Saved checkpoint is not compatible with this adapter",
            )
            checkpoint = json.loads(cp.ref_json)
        attempt = task.attempt_count + 1
        db.execute(
            "INSERT INTO runs(id,task_id,attempt_no,host_instance_id,expected_revision,state,adapter_snapshot_json,adapter_run_ref_json,held_resources_json,started_at) VALUES(?,?,?,?,?,'RUNNING',?,?,?,?)",
            (
                run_id,
                task_id,
                attempt,
                self.host_instance_id,
                thread.revision,
                json_dump(descriptor),
                json_dump(
                    {
                        "run_id": run_id,
                        "host_pid": os.getpid(),
                        "scopes": {
                            "collaboration": (
                                self.agent_configs[spec.agent_id].collaboration
                                if spec.agent_id in self.agent_configs
                                else True
                            ),
                            "mcp_bindings": [
                                b.model_dump(mode="json") for b in profile.mcp_bindings
                            ]
                            if profile
                            else [],
                        },
                    }
                ),
                json_dump(spec.resources),
                now_ms(),
            ),
        )
        db.execute(
            "UPDATE tasks SET state='RUNNING',queue_reason=NULL,attempt_count=?,updated_at=? WHERE id=?",
            (attempt, now_ms(), task_id),
        )
        db.execute(
            "UPDATE threads SET owner_task_id=?,active_run_id=? WHERE id=?",
            (task_id, run_id, thread.id),
        )
        pending = rows(
            db,
            "SELECT * FROM messages WHERE target_thread_id=? AND state='PENDING' ORDER BY created_at,rowid LIMIT 100",
            (thread.id,),
        )
        for message in pending:
            db.execute(
                "UPDATE messages SET state='OFFERED',target_run_id=?,updated_at=? WHERE id=?",
                (run_id, now_ms(), message.id),
            )
            emit(
                db,
                "message.offered",
                session_id=task.session_id,
                thread_id=thread.id,
                task_id=task_id,
                run_id=run_id,
                payload={"message_id": message.id, "state": "OFFERED"},
            )
        cutoff = one(
            db,
            "SELECT MAX(seq) AS seq FROM conversation WHERE task_id=? AND role='user'",
            (task_id,),
        ).seq
        if cutoff is None:
            cutoff = one(
                db,
                "SELECT COALESCE(MAX(c.seq),0) AS seq FROM conversation c LEFT JOIN tasks t ON t.id=c.task_id WHERE c.thread_id=? AND (c.task_id IS NULL OR t.created_at<=?)",
                (thread.id, task.created_at),
            ).seq
        history = rows(
            db,
            "SELECT * FROM (SELECT * FROM conversation WHERE thread_id=? AND (role!='user' OR seq<=?) ORDER BY seq DESC LIMIT 200) ORDER BY seq",
            (thread.id, cutoff),
        )
        dependencies = rows(
            db,
            "SELECT d.id,d.result_json FROM tasks d JOIN task_dependencies e ON e.depends_on_task_id=d.id WHERE e.task_id=?",
            (task_id,),
        )
        emit(
            db,
            "run.started",
            session_id=task.session_id,
            thread_id=thread.id,
            task_id=task_id,
            run_id=run_id,
            payload={"agent_id": spec.agent_id, "state": "RUNNING", "attempt": attempt},
        )
        project = need(db, "projects", need(db, "sessions", task.session_id).project_id)
        return ExecutionRequest(
            session_id=task.session_id,
            task_id=task_id,
            run_id=run_id,
            thread_id=thread.id,
            agent_id=spec.agent_id,
            project_id=project.id,
            project_folder=project.local_folder,
            expected_thread_revision=thread.revision,
            payload=spec.payload,
            messages=[json.loads(m.envelope_json) for m in pending],
            conversation=[public(h) for h in history],
            dependency_results={
                d.id: json.loads(d.result_json) if d.result_json else None
                for d in dependencies
            },
            resume_ref=checkpoint,
            resume_input=(
                json.loads(task.resume_input_json) if task.resume_input_json else None
            ),
            deadline_at_ms=spec.deadline_at_ms,
            attempt=attempt,
            agent_md=profile.agent_md if profile else "",
            skills=profile.skills if profile else (),
            mcp_bindings=profile.mcp_bindings if profile else (),
        )

    async def consume(self, active):
        from .presentation import SummaryStream
        summary_stream = SummaryStream()
        if active.context.cancelled.is_set():
            raise asyncio.CancelledError
        active.started = True
        boundary = None
        last_seq = 0
        async for event in active.adapter.execute(active.request, active.context):
            event = AdapterEvent.model_validate(event)
            require(
                boundary is None,
                "ADAPTER_PROTOCOL_ERROR",
                "Adapter emitted content after its final boundary",
            )
            if event.local_seq is not None:
                require(
                    event.local_seq > last_seq,
                    "ADAPTER_PROTOCOL_ERROR",
                    "Adapter sequence did not increase",
                )
                last_seq = event.local_seq
            if event.type in {"completed", "suspended"}:
                boundary = event
            else:
                await active.context.check_current()
                payload = self.clean_json(
                    event.payload, self.config.max_event_payload_bytes
                )
                if event.type == "progress" and isinstance(payload.get("delta"), str):
                    delta = summary_stream.feed(payload["delta"])
                    if not delta:
                        continue
                    payload = {**payload, "delta": delta, "format": "markdown", "channel": "summary"}
                elif event.type == "log":
                    # Tool identities/status may be shown; arbitrary logs and results stay internal.
                    payload = {k: payload[k] for k in ("tool", "state") if k in payload}
                    if not payload:
                        continue
                req = active.request
                self.events.progress(
                    req.session_id,
                    req.thread_id,
                    req.task_id,
                    req.run_id,
                    "run." + event.type,
                    payload,
                )
        require(
            boundary is not None,
            "ADAPTER_PROTOCOL_ERROR",
            "Adapter stream ended without a final boundary",
        )
        return await self.commit_boundary(
            active.request.run_id, boundary, boundary.local_seq or max(last_seq + 1, 1)
        )

    async def execute_run(self, active):
        run_id = active.request.run_id
        try:
            saved = await self.store.call(
                lambda db: need(db, "tasks", active.request.task_id)
            )
        except BaseException:
            self.accepting = False
            self.resources.isolate(run_id, "Execution admission could not be verified")
            self.uncertain_slots.add(run_id)
            self.active.pop(run_id, None)
            self.notify()
            return
        if saved.cancel_requested_at is not None:
            active.context.cancelled.set()
        active.worker = asyncio.create_task(
            self.consume(active), name=run_id + ":worker"
        )
        LOCAL_WORKERS[run_id] = active.worker
        cancellation = asyncio.create_task(active.context.cancelled.wait())
        timeout = active.spec.attempt_timeout_s
        if active.spec.deadline_at_ms is not None:
            timeout = max(
                0, min(timeout, (active.spec.deadline_at_ms - now_ms()) / 1000)
            )
        stopped = False
        try:
            done, _ = await asyncio.wait(
                {active.worker, cancellation},
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if active.worker in done:
                if active.worker.cancelled():
                    probe = await self.probe(active.adapter, {"run_id": run_id})
                    stopped = not active.started or probe.confirmed_stopped
                    await self.fail_run(
                        active,
                        RuntimeFault("TASK_CANCELLED", "Execution was cancelled"),
                        stopped=stopped,
                        cancelled=active.context.cancelled.is_set(),
                        never_started=not active.started,
                    )
                else:
                    await active.worker
                    stopped = True
            else:
                user_cancel = cancellation in done
                active.context.cancelled.set()
                try:
                    await asyncio.wait_for(
                        active.adapter.cancel({"run_id": run_id}),
                        self.config.probe_timeout_seconds,
                    )
                except Exception:
                    pass
                active.worker.cancel()
                finished, _ = await asyncio.wait(
                    {active.worker}, timeout=self.config.cancel_grace_seconds
                )
                probe = await self.probe(active.adapter, {"run_id": run_id})
                stopped = bool(finished) and (
                    not active.started or probe.confirmed_stopped
                )
                await self.fail_run(
                    active,
                    RuntimeFault(
                        "TASK_CANCELLED" if user_cancel else "ATTEMPT_TIMEOUT",
                        (
                            "Cancellation requested"
                            if user_cancel
                            else "Execution deadline exceeded"
                        ),
                    ),
                    stopped=stopped,
                    cancelled=user_cancel,
                    never_started=not active.started,
                )
                if finished:
                    self.consume_exception(active.worker)
        except asyncio.CancelledError:
            active.context.cancelled.set()
            active.worker.cancel()
            await self.fail_run(
                active,
                RuntimeFault("HOST_SHUTDOWN", "Host stopped during execution"),
                stopped=False,
            )
        except Exception as exc:
            probe = await self.probe(active.adapter, {"run_id": run_id})
            stopped = active.worker.done() and probe.confirmed_stopped
            error = (
                exc
                if isinstance(exc, RuntimeFault)
                else RuntimeFault("ADAPTER_FAILED", "Execution endpoint failed")
            )
            await self.fail_run(active, error, stopped=stopped)
        finally:
            cancellation.cancel()
            self.consume_exception(cancellation)
            if active.worker.done():
                LOCAL_WORKERS.pop(run_id, None)
                self.consume_exception(active.worker)
                self.active.pop(run_id, None)
                if not stopped and not active.committed:
                    self.uncertain_slots.add(run_id)
                self.resources.release(run_id)
            else:
                active.worker.add_done_callback(
                    lambda task: self.on_late_stop(run_id, task)
                )
            self.notify()

    @staticmethod
    def consume_exception(task):
        if task.done() and not task.cancelled():
            task.exception()

    def on_late_stop(self, run_id, worker):
        LOCAL_WORKERS.pop(run_id, None)
        self.consume_exception(worker)
        active = self.active.pop(run_id, None)
        if active is not None:
            self.uncertain_slots.add(run_id)
        self.notify()

    async def fail_run(
        self, active, error, *, stopped, cancelled=False, never_started=False
    ):
        if self.store.closed or self.store.failed:
            return

        def fail(db):
            prior = need(db, "runs", active.request.run_id)
            if prior.boundary_receipt_json:
                emit(
                    db,
                    "run.protocol_warning",
                    session_id=active.request.session_id,
                    run_id=prior.id,
                    payload={"code": error.code},
                )
                return "COMMITTED"
            task, run = self.check_run(db, prior.id)
            unresolved = rows(
                db,
                "SELECT * FROM tool_calls WHERE run_id=? AND state IN ('DISPATCHED','UNCERTAIN')",
                (run.id,),
            )
            uncertain = (
                not stopped
                or bool(unresolved)
                or error.code == "OUTCOME_UNCERTAIN"
                or (
                    not never_started
                    and active.adapter.describe().tool_mediation != "full"
                )
            )
            safe = (
                isinstance(error, AdapterFailure)
                and error.retryable
                and error.safe_to_retry
            )
            retry = (
                not uncertain
                and not cancelled
                and safe
                and active.spec.retry_policy == "SAFE_ONLY"
                and task.attempt_count < active.spec.max_attempts
            )
            state = (
                "UNCERTAIN"
                if uncertain
                else "QUEUED"
                if retry
                else "CANCELLED"
                if cancelled
                else "FAILED"
            )
            safe_error = self.vault.redact(error.as_dict())
            db.execute(
                "UPDATE runs SET state=?,ended_at=?,error_json=? WHERE id=?",
                (
                    (
                        "UNCERTAIN"
                        if uncertain
                        else "CANCELLED"
                        if cancelled
                        else "FAILED"
                    ),
                    now_ms() if stopped else None,
                    json_dump(safe_error),
                    run.id,
                ),
            )
            db.execute(
                "UPDATE tasks SET state=?,queue_reason=?,error_json=?,not_before=?,updated_at=? WHERE id=?",
                (
                    state,
                    "RETRY_BACKOFF" if retry else None,
                    json_dump(safe_error),
                    (
                        now_ms()
                        + int((2 ** (task.attempt_count - 1) + random.random()) * 200)
                        if retry
                        else 0
                    ),
                    now_ms(),
                    task.id,
                ),
            )
            db.execute(
                "UPDATE threads SET active_run_id=?,owner_task_id=?,blocked_reason=? WHERE id=?",
                (
                    None if stopped else run.id,
                    task.id if uncertain or retry else None,
                    "OUTCOME_UNCERTAIN" if uncertain else None,
                    task.thread_id,
                ),
            )
            if stopped and not uncertain:
                db.execute(
                    "UPDATE messages SET state='PENDING',target_run_id=NULL WHERE target_run_id=? AND state='OFFERED'",
                    (run.id,),
                )
            for tool in unresolved:
                db.execute(
                    "UPDATE tool_calls SET state='UNCERTAIN',updated_at=? WHERE id=?",
                    (now_ms(), tool.id),
                )
            emit(
                db,
                "task." + state.lower(),
                session_id=task.session_id,
                thread_id=task.thread_id,
                task_id=task.id,
                run_id=run.id,
                payload={
                    "state": state,
                    "error": safe_error,
                    "confirmed_stopped": stopped,
                },
            )
            return state

        state = await self.store.call(fail, transaction=True)
        if state == "UNCERTAIN":
            self.resources.isolate(
                active.request.run_id, "Unreconciled execution or external operation"
            )
        self.notify()

    async def probe(self, adapter, run_ref):
        if not adapter.describe().supports_probe:
            return ProbeResult(state="UNKNOWN")
        try:
            return ProbeResult.model_validate(
                await asyncio.wait_for(
                    adapter.probe(run_ref), self.config.probe_timeout_seconds
                )
            )
        except Exception:
            return ProbeResult(state="UNKNOWN")

    async def recover(self):
        pending = await self.store.call(
            lambda db: rows(
                db,
                "SELECT r.*,t.session_id,t.thread_id,t.spec_json FROM runs r JOIN tasks t ON t.id=r.task_id WHERE r.state IN ('RUNNING','UNCERTAIN')",
            )
        )
        for run in pending:
            spec = TaskSpec.model_validate_json(run.spec_json)
            adapter = self.adapters.get(spec.agent_id)
            ref = (
                json.loads(run.adapter_run_ref_json)
                if run.adapter_run_ref_json
                else {"run_id": run.id}
            )
            ref["run_id"] = run.id
            probe = (
                await self.probe(adapter, ref)
                if adapter
                else ProbeResult(state="UNKNOWN")
            )
            if (
                run.ended_at is None
                and probe.state == "STOPPED"
                and json.loads(run.adapter_snapshot_json)["execution_mode"]
                == "in_process"
                and process_alive(ref.get("host_pid"))
            ):
                worker = LOCAL_WORKERS.get(run.id)
                if worker is None or not worker.done():
                    probe = ProbeResult(
                        state="UNKNOWN", evidence="Previous host process is still alive"
                    )
            requirements = [
                ResourceRequirement.model_validate(r)
                for r in json.loads(run.held_resources_json)
            ]
            self.resources.acquire(run.id, requirements)
            if probe.state == "NOT_STARTED" and probe.confirmed_stopped:

                def not_started(db, r=run):
                    unresolved = one(
                        db,
                        "SELECT id FROM tool_calls WHERE run_id=? AND state IN ('DISPATCHED','UNCERTAIN') LIMIT 1",
                        (r.id,),
                    )
                    if unresolved:
                        return False
                    db.execute(
                        "UPDATE runs SET state='NOT_STARTED',ended_at=? WHERE id=?",
                        (now_ms(), r.id),
                    )
                    db.execute(
                        "UPDATE tasks SET state='QUEUED',queue_reason='RECOVERY',attempt_count=MAX(0,attempt_count-1),updated_at=? WHERE id=?",
                        (now_ms(), r.task_id),
                    )
                    db.execute(
                        "UPDATE threads SET active_run_id=NULL,blocked_reason=NULL WHERE id=?",
                        (r.thread_id,),
                    )
                    db.execute(
                        "UPDATE messages SET state='PENDING',target_run_id=NULL WHERE target_run_id=? AND state='OFFERED'",
                        (r.id,),
                    )
                    emit(
                        db,
                        "run.recovered_not_started",
                        session_id=r.session_id,
                        thread_id=r.thread_id,
                        task_id=r.task_id,
                        run_id=r.id,
                    )
                    return True

                if await self.store.call(not_started, transaction=True):
                    self.resources.release(run.id)
                    continue
            if (
                probe.state == "COMPLETED"
                and probe.confirmed_stopped
                and probe.boundary
            ):

                def adopt_verified_result(db, recovered=run):
                    task = need(db, "tasks", recovered.task_id)
                    thread = need(db, "threads", recovered.thread_id)
                    require(
                        task.state in {"RUNNING", "UNCERTAIN"}
                        and thread.owner_task_id == task.id
                        and thread.revision == recovered.expected_revision
                        and thread.active_run_id in {None, recovered.id},
                        "STALE_RUN",
                        "Recovered result no longer owns this thread",
                    )
                    db.execute(
                        "UPDATE runs SET state='RUNNING',host_instance_id=? WHERE id=?",
                        (self.host_instance_id, recovered.id),
                    )
                    db.execute(
                        "UPDATE tasks SET state='RUNNING',updated_at=? WHERE id=?",
                        (now_ms(), task.id),
                    )
                    db.execute(
                        "UPDATE threads SET active_run_id=? WHERE id=?",
                        (recovered.id, thread.id),
                    )

                try:
                    await self.store.call(adopt_verified_result, transaction=True)
                    await self.commit_boundary(
                        run.id, probe.boundary, probe.boundary.local_seq or 1
                    )
                    self.resources.release(run.id)
                    continue
                except RuntimeFault:
                    pass

            def uncertain(db, r=run, p=probe):
                db.execute(
                    "UPDATE runs SET state='UNCERTAIN',ended_at=? WHERE id=?",
                    (now_ms() if p.confirmed_stopped else None, r.id),
                )
                db.execute(
                    "UPDATE tasks SET state='UNCERTAIN',error_json=?,updated_at=? WHERE id=?",
                    (
                        json_dump(
                            {
                                "code": "OUTCOME_UNCERTAIN",
                                "safe_message": "Previous execution requires reconciliation",
                            }
                        ),
                        now_ms(),
                        r.task_id,
                    ),
                )
                db.execute(
                    "UPDATE threads SET owner_task_id=?,active_run_id=?,blocked_reason='OUTCOME_UNCERTAIN' WHERE id=?",
                    (r.task_id, None if p.confirmed_stopped else r.id, r.thread_id),
                )
                db.execute(
                    "UPDATE tool_calls SET state='UNCERTAIN' WHERE run_id=? AND state='DISPATCHED'",
                    (r.id,),
                )
                emit(
                    db,
                    "run.recovery_required",
                    session_id=r.session_id,
                    thread_id=r.thread_id,
                    task_id=r.task_id,
                    run_id=r.id,
                    payload={
                        "probe_state": p.state,
                        "confirmed_stopped": p.confirmed_stopped,
                        "evidence": p.evidence,
                    },
                )

            await self.store.call(uncertain, transaction=True)
            self.resources.isolate(run.id, "Previous execution needs reconciliation")
            if not probe.confirmed_stopped:
                self.uncertain_slots.add(run.id)
        for tool in await self.store.call(
            lambda db: rows(
                db, "SELECT * FROM tool_calls WHERE state IN ('DISPATCHED','UNCERTAIN')"
            )
        ):
            owner = tool.run_id + ":" + tool.id
            self.resources.acquire(
                owner,
                [
                    ResourceRequirement.model_validate(r)
                    for r in json.loads(tool.resources_json)
                ],
            )
            self.resources.isolate(owner, "Tool outcome needs reconciliation")

    async def backup(self, destination):
        target = external_path(destination)
        require(
            not target.exists(),
            "INVALID_REQUEST",
            "Backup destination must not already exist",
        )
        target.mkdir(parents=True)
        # A frozen metadata copy determines which immutable files belong to this backup.
        await self.store.backup(target / "runtime.sqlite")

        def copy():
            import apsw

            db = apsw.Connection(str(target / "runtime.sqlite"))
            try:
                paths = list(
                    db.execute(
                        "SELECT relative_path,sha256,size FROM artifacts WHERE state='READY'"
                    )
                )
            finally:
                db.close()
            import hashlib

            from .paths import safe_child

            for relative, checksum, size in paths:
                source = safe_child(self.artifacts.root, relative)
                dest = safe_child(target / "artifacts", relative)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, dest)
                require(
                    dest.stat().st_size == size
                    and hashlib.sha256(dest.read_bytes()).hexdigest() == checksum,
                    "ARTIFACT_INVALID",
                    "Backup artifact checksum mismatch",
                )

        await asyncio.to_thread(copy)
        return {"database": "complete", "artifacts": "complete"}

    async def aclose(self):
        if self.closed or self.closing:
            return
        self.accepting = False
        self.closing = True
        self.wake.set()
        if self.scheduler:
            await self.scheduler
        for active in tuple(self.active.values()):
            active.context.cancelled.set()
        drivers = [active.driver for active in self.active.values() if active.driver]
        if drivers:
            done, pending = await asyncio.wait(
                drivers,
                timeout=self.config.cancel_grace_seconds
                + self.config.probe_timeout_seconds
                + 1,
            )
            for driver in pending:
                driver.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        for adapter in {
            id(a): a for a in [*self.adapters.values(), *self.retired_adapters]
        }.values():
            try:
                await asyncio.wait_for(
                    adapter.aclose(), self.config.cancel_grace_seconds
                )
            except Exception:
                pass
        await self.mcp.aclose()
        self.closed = True
        self.events.notify()
        try:
            await self.store.close()
        finally:
            self.lock.release()
