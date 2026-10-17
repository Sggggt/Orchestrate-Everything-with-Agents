"""Public application services used by every transport."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from .errors import RuntimeFault, require
from .models import (
    TERMINAL,
    AgentConfig,
    AgentSkill,
    ConversationEntry,
    MessageHandle,
    MessageSnapshot,
    MessageSpec,
    PageEnvelope,
    ProjectInfo,
    Reconciliation,
    SessionInfo,
    SessionSnapshot,
    TaskHandle,
    TaskResult,
    TaskSnapshot,
    TaskSpec,
    ThreadSnapshot,
    View,
    content_hash,
    json_dump,
    new_id,
    now_ms,
)
from .store import emit, one, rows


def public(row):
    result = View(row)
    result["schema_version"] = "1.0"
    for key in list(result):
        if key.endswith("_json"):
            value = result.pop(key)
            result[key[:-5]] = json.loads(value) if value is not None else None
    if "sha256" in result:
        result.pop("relative_path", None)
    return result


def need(db, table, identity):
    # Table names are internal constants, never transport input.
    result = one(db, f"SELECT * FROM {table} WHERE id=?", (identity,))
    require(
        result is not None,
        "NOT_FOUND",
        f"{table.rstrip('s').capitalize()} does not exist",
    )
    return result


def resolve_project(db, project_id=None):
    if project_id is None:
        available = rows(db, "SELECT id FROM projects WHERE state='OPEN'")
        require(len(available) == 1, "PROJECT_REQUIRED", "Choose an explicit project")
        project_id = available[0].id
    project = need(db, "projects", project_id)
    require(project.state == "OPEN", "PROJECT_CLOSED", "Project is archived")
    return project


def agent_project(db, agent_id):
    saved = one(db, "SELECT config_json FROM agent_configs WHERE id=?", (agent_id,))
    return (json.loads(saved.config_json).get("project_id") or "default") if saved else "default"


def project_public(row):
    return ProjectInfo.model_validate({k: v for k, v in public(row).items()
                                       if k not in {"creation_key", "request_hash"}})


def require_no_live_workflows(db, *, project_id=None, session_id=None):
    field, identity = ("project_id", project_id) if project_id is not None else ("session_id", session_id)
    require(not one(db, f"SELECT id FROM workflow_runs WHERE {field}=? AND ended_at IS NULL LIMIT 1", (identity,)),
            "RESOURCE_BUSY", "请先完成或取消工作流，并核对未确定的执行结果。")


def recycle_session(db, session_id, batch_id, *, cascade=False):
    session = need(db, "sessions", session_id)
    if session.state == "DELETED":
        return False
    require_no_live_workflows(db, session_id=session_id)
    require(not one(db, "SELECT id FROM tasks WHERE session_id=? "
                    "AND state NOT IN ('SUCCEEDED','FAILED','CANCELLED','SKIPPED') LIMIT 1",
                    (session_id,)), "RESOURCE_BUSY", "会话有未完成或待核对的任务，暂时不能删除。")
    db.execute("INSERT INTO deleted_items VALUES('session',?,?,?,?)",
               (session_id, session.state, batch_id, now_ms()))
    db.execute("UPDATE sessions SET state='DELETED' WHERE id=?", (session_id,))
    emit(db, "session.deleted", session_id=session_id,
         payload={"state": "DELETED", "project_id": session.project_id, "cascade": cascade})
    return True


class Projects:
    def __init__(self, rt):
        self.rt = rt

    @staticmethod
    def folder(value):
        path = Path(str(value)).expanduser()
        require(path.is_absolute() and path.is_dir(), "INVALID_REQUEST",
                "Project folder must be an existing absolute local directory")
        return str(path.resolve())

    async def list(self, include_archived=False, include_deleted=False):
        conditions = ["1=1"]
        if not include_archived:
            conditions.append("state!='ARCHIVED'")
        if not include_deleted:
            conditions.append("state!='DELETED'")
        found = await self.rt.store.call(lambda db: rows(
            db, "SELECT * FROM projects WHERE " + " AND ".join(conditions) + " ORDER BY created_at,rowid"))
        return [project_public(row) for row in found]

    async def get(self, project_id):
        return await self.rt.store.call(lambda db: project_public(need(db, "projects", project_id)))

    async def create(self, name, local_folder, idempotency_key):
        self.rt.admit()
        title = str(name).strip()
        require(0 < len(title) <= 120, "INVALID_REQUEST", "Project name must be 1..120 characters")
        require(isinstance(idempotency_key, str) and 0 < len(idempotency_key) <= 256,
                "INVALID_REQUEST", "Provide a stable project creation key")
        folder = self.folder(local_folder)
        digest = content_hash({"name": title, "local_folder": folder})
        def insert(db):
            old = one(db, "SELECT * FROM projects WHERE creation_key=?", (idempotency_key,))
            if old:
                require(old.request_hash == digest, "IDEMPOTENCY_CONFLICT", "Project key has different input")
                return project_public(old)
            identity, stamp = new_id("prj"), now_ms()
            db.execute("INSERT INTO projects VALUES(?,?,?,'OPEN',?,?,?,?)",
                       (identity, title, folder, idempotency_key, digest, stamp, stamp))
            emit(db, "project.created", payload={"project_id": identity, "name": title})
            return project_public(need(db, "projects", identity))
        return await self.rt.store.call(insert, transaction=True)

    async def update(self, project_id, name=None, local_folder=None):
        self.rt.admit()
        folder = self.folder(local_folder) if local_folder is not None else None
        if name is not None:
            name = str(name).strip()
            require(0 < len(name) <= 120, "INVALID_REQUEST", "Project name must be 1..120 characters")
        def update(db):
            project = need(db, "projects", project_id)
            require(project.state != "DELETED", "PROJECT_DELETED", "请先从回收站恢复项目。")
            if folder is not None and folder != project.local_folder:
                require_no_live_workflows(db, project_id=project_id)
                require(not one(db, "SELECT t.id FROM tasks t JOIN sessions s ON s.id=t.session_id "
                                "WHERE s.project_id=? AND t.state NOT IN ('SUCCEEDED','FAILED','CANCELLED','SKIPPED') LIMIT 1",
                                (project_id,)), "RESOURCE_BUSY", "Wait for project work to finish before rebinding its folder")
            db.execute("UPDATE projects SET name=?,local_folder=?,updated_at=? WHERE id=?",
                       (name if name is not None else project.name,
                        folder if folder is not None else project.local_folder, now_ms(), project_id))
            emit(db, "project.updated", payload={"project_id": project_id})
            return project_public(need(db, "projects", project_id))
        return await self.rt.store.call(update, transaction=True)

    async def archive(self, project_id, archived=True):
        self.rt.admit()
        def update(db):
            require_no_live_workflows(db, project_id=project_id)
            require(need(db, "projects", project_id).state != "DELETED", "PROJECT_DELETED", "请使用恢复操作找回已删除项目。")
            require(not one(db, "SELECT t.id FROM tasks t JOIN sessions s ON s.id=t.session_id "
                            "WHERE s.project_id=? AND t.state NOT IN ('SUCCEEDED','FAILED','CANCELLED','SKIPPED') LIMIT 1",
                            (project_id,)), "RESOURCE_BUSY", "Project still has unfinished work")
            db.execute("UPDATE projects SET state=?,updated_at=? WHERE id=?",
                       ("ARCHIVED" if archived else "OPEN", now_ms(), project_id))
            return project_public(need(db, "projects", project_id))
        return await self.rt.store.call(update, transaction=True)

    async def delete(self, project_id, confirmed=False):
        """Recoverable deletion; never remove the bound local directory or credentials."""
        self.rt.admit()
        require(confirmed is True, "CONFIRMATION_REQUIRED", "请确认删除此项目及其会话。")
        async with self.rt.configuration_lock:
            def remove(db):
                project = need(db, "projects", project_id)
                if project.state == "DELETED":
                    return {"id": project_id, "deleted": True, "recoverable": True, "reused": True}
                require_no_live_workflows(db, project_id=project_id)
                require(not one(db, "SELECT t.id FROM tasks t JOIN sessions s ON s.id=t.session_id "
                                "WHERE s.project_id=? AND t.state NOT IN ('SUCCEEDED','FAILED','CANCELLED','SKIPPED') LIMIT 1",
                                (project_id,)), "RESOURCE_BUSY", "项目有未完成或待核对的任务，暂时不能删除。")
                batch = new_id("del")
                db.execute("INSERT INTO deleted_items VALUES('project',?,?,?,?)",
                           (project_id, project.state, batch, now_ms()))
                children = rows(db, "SELECT id FROM sessions WHERE project_id=?", (project_id,))
                for child in children:
                    recycle_session(db, child.id, batch, cascade=True)
                db.execute("UPDATE projects SET state='DELETED',updated_at=? WHERE id=?", (now_ms(), project_id))
                emit(db, "project.deleted", payload={"project_id": project_id})
                return {"id": project_id, "deleted": True, "recoverable": True, "session_count": len(children)}
            result = await self.rt.store.call(remove, transaction=True)
        self.rt.notify()
        return result

    async def restore(self, project_id):
        self.rt.admit()
        async with self.rt.configuration_lock:
            def restore(db):
                project = need(db, "projects", project_id)
                if project.state != "DELETED":
                    return project_public(project)
                saved = one(db, "SELECT * FROM deleted_items WHERE kind='project' AND entity_id=?", (project_id,))
                require(saved is not None, "RESTORE_UNAVAILABLE", "项目恢复记录不存在。")
                for child in rows(db, "SELECT * FROM deleted_items WHERE kind='session' AND batch_id=?", (saved.batch_id,)):
                    db.execute("UPDATE sessions SET state=? WHERE id=? AND state='DELETED'",
                               (child.previous_state, child.entity_id))
                    db.execute("DELETE FROM deleted_items WHERE kind='session' AND entity_id=?", (child.entity_id,))
                    emit(db, "session.restored", session_id=child.entity_id, payload={})
                db.execute("UPDATE projects SET state=?,updated_at=? WHERE id=?",
                           (saved.previous_state, now_ms(), project_id))
                db.execute("DELETE FROM deleted_items WHERE kind='project' AND entity_id=?", (project_id,))
                emit(db, "project.restored", payload={"project_id": project_id})
                return project_public(need(db, "projects", project_id))
            result = await self.rt.store.call(restore, transaction=True)
        self.rt.notify()
        return result

    async def adopt(self, project_id, session_ids, agent_ids):
        """Move explicit related sessions and Agents together without changing their history."""
        self.rt.admit()
        require(isinstance(session_ids, list) and isinstance(agent_ids, list)
                and all(isinstance(x, str) for x in session_ids + agent_ids),
                "INVALID_REQUEST", "session_ids and agent_ids must be ID lists")
        sessions, agents = set(session_ids), set(agent_ids)
        require(len(sessions) + len(agents) <= 500, "INVALID_REQUEST", "Move at most 500 objects")
        async with self.rt.configuration_lock:
            def move(db):
                resolve_project(db, project_id)
                configs = {}
                for identity in agents:
                    row = need(db, "agent_configs", identity)
                    config = AgentConfig.model_validate_json(row.config_json)
                    require(need(db, "projects", config.project_id or "default").state != "DELETED",
                            "PROJECT_DELETED", "请先恢复 Agent 所属的项目。")
                    configs[identity] = config.model_copy(update={"project_id": project_id})
                    for thread in rows(db, "SELECT session_id FROM threads WHERE agent_id=?", (identity,)):
                        require(thread.session_id in sessions or need(db, "sessions", thread.session_id).project_id == project_id,
                                "PROJECT_CONFLICT", "Include every existing session of the moved Agent")
                for identity in sessions:
                    session = need(db, "sessions", identity)
                    require(session.project_id == project_id or not one(db, "SELECT id FROM workflow_runs WHERE session_id=? LIMIT 1", (identity,)),
                            "PROJECT_CONFLICT", "工作流运行会话不能移入其他项目。")
                    require(need(db, "sessions", identity).state != "DELETED",
                            "SESSION_DELETED", "请先恢复要移动的会话。")
                    require(not one(db, "SELECT id FROM tasks WHERE session_id=? "
                                    "AND state NOT IN ('SUCCEEDED','FAILED','CANCELLED','SKIPPED') LIMIT 1", (identity,)),
                            "RESOURCE_BUSY", "Cannot move unfinished sessions")
                    for thread in rows(db, "SELECT agent_id FROM threads WHERE session_id=?", (identity,)):
                        require(thread.agent_id in agents or agent_project(db, thread.agent_id) == project_id,
                                "PROJECT_CONFLICT", "Include the session's Agents in the same move")
                for identity, config in configs.items():
                    db.execute("UPDATE agent_configs SET config_json=?,updated_at=? WHERE id=?",
                               (json_dump(config), now_ms(), identity))
                for identity in sessions:
                    db.execute("UPDATE sessions SET project_id=? WHERE id=?", (project_id, identity))
                emit(db, "project.contents_moved", payload={"project_id": project_id,
                     "session_ids": sorted(sessions), "agent_ids": sorted(agents)})
                return configs
            changed = await self.rt.store.call(move, transaction=True)
            self.rt.agent_configs.update(changed)
        self.rt.notify()
        return {"project_id": project_id, "session_ids": sorted(sessions), "agent_ids": sorted(agents)}


class Sessions:
    def __init__(self, rt):
        self.rt = rt

    async def create(self, metadata=None, title="新会话", project_id=None):
        self.rt.admit()
        title = str(title).strip()[:120] or "New workspace"
        meta = self.rt.clean_json(metadata or {})
        require(
            isinstance(meta, dict),
            "INVALID_REQUEST",
            "Session metadata must be an object",
        )
        identity = new_id("ses")

        def insert(db):
            project = resolve_project(db, project_id)
            db.execute(
                "INSERT INTO sessions(id,state,title,metadata_json,created_at,project_id) VALUES(?,'OPEN',?,?,?,?)",
                (identity, title, json_dump(meta), now_ms(), project.id),
            )
            emit(db, "session.created", session_id=identity, payload={"title": title})
            return SessionInfo.model_validate(public(need(db, "sessions", identity)))

        result = await self.rt.store.call(insert, transaction=True)
        self.rt.notify()
        return result

    async def get(self, session_id):
        return await self.rt.store.call(
            lambda db: SessionInfo.model_validate(
                public(need(db, "sessions", session_id))
            )
        )

    async def list(self, cursor=None, limit=100, project_id=None, include_deleted=False, deleted_only=False):
        limit = min(200, max(1, int(limit)))
        found = await self.rt.store.call(
            lambda db: rows(
                db,
                "SELECT rowid AS position,* FROM sessions WHERE rowid>? "
                + ("AND project_id=? " if project_id is not None else "")
                + ("AND state='DELETED' AND project_id IN (SELECT id FROM projects WHERE state!='DELETED') "
                   if deleted_only else "" if include_deleted else "AND state!='DELETED' ")
                + "ORDER BY rowid LIMIT ?",
                (int(cursor or 0), *((project_id,) if project_id is not None else ()), limit + 1),
            )
        )
        return PageEnvelope(
            items=[SessionInfo.model_validate(public(r)) for r in found[:limit]],
            cursor=str(found[min(limit, len(found)) - 1].position) if found else cursor,
            has_more=len(found) > limit,
        )

    async def delete(self, session_id, confirmed=False):
        self.rt.admit()
        require(confirmed is True, "CONFIRMATION_REQUIRED", "请确认删除此会话。")
        def remove(db):
            changed = recycle_session(db, session_id, new_id("del"))
            return {"id": session_id, "deleted": True, "recoverable": True, "reused": not changed}
        result = await self.rt.store.call(remove, transaction=True)
        self.rt.notify()
        return result

    async def restore(self, session_id):
        self.rt.admit()
        def restore(db):
            session = need(db, "sessions", session_id)
            require(need(db, "projects", session.project_id).state != "DELETED",
                    "PROJECT_DELETED", "请先恢复会话所属的项目。")
            if session.state == "DELETED":
                saved = one(db, "SELECT * FROM deleted_items WHERE kind='session' AND entity_id=?", (session_id,))
                require(saved is not None, "RESTORE_UNAVAILABLE", "会话恢复记录不存在。")
                db.execute("UPDATE sessions SET state=? WHERE id=?", (saved.previous_state, session_id))
                db.execute("DELETE FROM deleted_items WHERE kind='session' AND entity_id=?", (session_id,))
                emit(db, "session.restored", session_id=session_id, payload={})
            return SessionInfo.model_validate(public(need(db, "sessions", session_id)))
        result = await self.rt.store.call(restore, transaction=True)
        self.rt.notify()
        return result

    async def snapshot(self, session_id, presentation=False):
        def read(db):
            session = public(need(db, "sessions", session_id))
            cursor = one(db, "SELECT COALESCE(MAX(seq),0) AS seq FROM events").seq
            return SessionSnapshot(
                session=session,
                threads=[
                    public(r)
                    for r in rows(
                        db,
                        "SELECT * FROM threads WHERE session_id=? ORDER BY created_at,rowid",
                        (session_id,),
                    )
                ],
                tasks=[
                    public(r)
                    for r in rows(
                        db,
                        "SELECT * FROM tasks WHERE session_id=? AND (state NOT IN ('SUCCEEDED','FAILED','CANCELLED','SKIPPED') OR id IN (SELECT id FROM tasks WHERE session_id=? ORDER BY created_at DESC,rowid DESC LIMIT 300)) ORDER BY created_at DESC,rowid DESC",
                        (session_id, session_id),
                    )
                ],
                messages=[
                    public(r)
                    for r in rows(
                        db,
                        "SELECT * FROM messages WHERE session_id=? AND (state IN ('PENDING','OFFERED') OR id IN (SELECT id FROM messages WHERE session_id=? ORDER BY created_at DESC,rowid DESC LIMIT 200)) ORDER BY created_at DESC,rowid DESC",
                        (session_id, session_id),
                    )
                ],
                artifacts=[
                    public(r)
                    for r in rows(
                        db,
                        "SELECT * FROM artifacts WHERE session_id=? ORDER BY created_at DESC LIMIT 100",
                        (session_id,),
                    )
                ],
                cursor=cursor,
            )

        result = await self.rt.store.call(read, transaction=True)
        if presentation:
            from .presentation import markdown_summary, result_summary
            value = result.model_dump(mode="json")
            for task in value["tasks"]:
                task["title"] = markdown_summary(task["spec"]["payload"].get("input") or
                                                task["spec"]["metadata"].get("stage") or "协作任务", limit=160)
                task["spec"]["payload"] = {}
                if task["result"]:
                    summary = result_summary(TaskResult.model_validate(task["result"]))
                    task["result"]["output"] = {}
                    task["result"]["summary_markdown"] = summary
            for message in value["messages"]:
                message["envelope"]["payload"] = {"summary_markdown": markdown_summary(message["envelope"]["payload"])}
            value["progress"] = await self.rt.events.live(session_id)
            return value
        return result

    async def close(self, session_id, mode="drain"):
        require(
            mode in {"drain", "cancel"},
            "INVALID_REQUEST",
            "Close mode must be drain or cancel",
        )

        def close(db):
            session = need(db, "sessions", session_id)
            require_no_live_workflows(db, session_id=session_id)
            require(session.state != "DELETED", "SESSION_DELETED", "请先从回收站恢复会话。")
            if session.state != "CLOSED":
                db.execute(
                    "UPDATE sessions SET state='CLOSING',close_mode=? WHERE id=?",
                    (mode, session_id),
                )
                emit(
                    db, "session.closing", session_id=session_id, payload={"mode": mode}
                )
            return rows(
                db,
                "SELECT id FROM tasks WHERE session_id=? AND state IN ('QUEUED','RUNNING','WAITING')",
                (session_id,),
            )

        pending = await self.rt.store.call(close, transaction=True)
        if mode == "cancel":
            for task in pending:
                await self.rt.tasks.cancel(task.id)
        await self.rt.close_sessions()
        self.rt.notify()
        snapshot = await self.snapshot(session_id)
        return View(
            session=snapshot.session,
            unresolved=[t.id for t in snapshot.tasks if t.state == "UNCERTAIN"],
        )


class Agents:
    def __init__(self, rt):
        self.rt = rt
        self.model_tests = set()

    async def list(self, project_id=None):
        configs = await self.rt.store.call(
            lambda db: rows(db, "SELECT * FROM agent_configs ORDER BY rowid")
        )
        items = []
        deleted_projects = await self.rt.store.call(lambda db: {
            row.id for row in rows(db, "SELECT id FROM projects WHERE state='DELETED'")})
        for row in configs:
            from .llm import model_setup

            saved = AgentConfig.model_validate_json(row.config_json)
            if (saved.project_id or "default") in deleted_projects:
                continue
            if project_id is not None and saved.project_id != project_id:
                continue
            config = saved.model_dump(mode="json")
            config["has_api_key"] = bool(self.rt.vault.get(row.id))
            config["model_setup"] = model_setup(
                saved, has_api_key=config["has_api_key"]
            )
            adapter = self.rt.adapters.get(row.id)
            config["descriptor"] = adapter.describe().model_dump() if adapter else None
            config["active_runs"] = sum(
                a.request.agent_id == row.id for a in self.rt.active.values()
            )
            items.append(config)
        registered = {item["id"] for item in items}
        for key, adapter in self.rt.adapters.items():
            if key not in registered and "default" not in deleted_projects and (project_id is None or project_id == "default") and key not in self.rt.agent_configs:
                items.append(
                    {
                        "id": key,
                        "name": key,
                        "kind": "custom",
                        "enabled": True,
                        "has_api_key": False,
                        "descriptor": adapter.describe().model_dump(),
                        "active_runs": 0,
                    }
                )
        return items

    async def describe(self, agent_id):
        for item in await self.list():
            if item["id"] == agent_id:
                return item
        raise RuntimeFault("NOT_FOUND", "Agent does not exist")

    async def configure(self, config: AgentConfig | dict, api_key: str | None = None):
        async with self.rt.configuration_lock:
            return await self._configure(config, api_key)

    async def test_model(self, agent_id):
        import time
        from types import SimpleNamespace

        from .llm import ConfiguredLLM, model_required

        require(
            agent_id not in self.model_tests,
            "RESOURCE_BUSY",
            "A model connection test is already running for this Agent",
        )
        async with self.rt.configuration_lock:
            config = self.rt.agent_configs.get(agent_id)
            require(config is not None, "NOT_FOUND", "Agent does not exist")
            require(
                model_required(config),
                "MODEL_NOT_ENABLED",
                "This Agent does not use the platform model",
            )
            require(
                config.base_url and config.model,
                "AGENT_NOT_CONFIGURED",
                "Save the model API address and model name first",
            )
            test_config = config.model_copy(
                update={
                    "max_output_tokens": 16,
                    "timeout_seconds": min(config.timeout_seconds, 30),
                }
            )
            client = ConfiguredLLM(test_config, self.rt.vault.get(agent_id))
            self.model_tests.add(agent_id)
        started = time.monotonic()
        try:
            context = SimpleNamespace(_runtime=self.rt, cancelled=asyncio.Event())
            await client.complete(
                [{"role": "user", "content": "Reply with OK."}], context
            )
            return {
                "agent_id": agent_id,
                "model": config.model,
                "connected": True,
                "latency_ms": round((time.monotonic() - started) * 1000),
                "message": "模型接口已响应；业务效果请另行验收。",
            }
        finally:
            self.model_tests.discard(agent_id)

    async def _configure(self, config: AgentConfig | dict, api_key: str | None = None):
        self.rt.admit()
        config = AgentConfig.model_validate(config).model_copy(deep=True)
        previous = self.rt.agent_configs.get(config.id)
        target = config.project_id or (previous.project_id if previous else None)
        project = await self.rt.store.call(lambda db: resolve_project(db, target))
        config = config.model_copy(update={"project_id": project.id})
        conflict = await self.rt.store.call(lambda db: one(
            db, "SELECT h.id FROM threads h JOIN sessions s ON s.id=h.session_id "
                "WHERE h.agent_id=? AND s.project_id!=? LIMIT 1", (config.id, project.id)))
        require(not conflict, "PROJECT_CONFLICT", "Move existing sessions and Agents together with projects.adopt")
        self.rt.mcp.validate_bindings(config.mcp_bindings)
        require(
            config.id != "codex", "INVALID_REQUEST", "Codex is an external coordinator"
        )
        old = self.rt.adapters.get(config.id)
        if api_key is not None:
            require(len(api_key) <= 16384, "INVALID_REQUEST", "Credential is too large")
            await asyncio.to_thread(self.rt.vault.set, config.id, api_key)
        new = self.rt.make_adapter(config)
        self.rt.validate_adapter(new)
        await self.rt.store.call(
            lambda db: db.execute(
                "INSERT INTO agent_configs VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET config_json=excluded.config_json,updated_at=excluded.updated_at",
                (config.id, json_dump(config), now_ms()),
            ),
            transaction=True,
        )
        self.rt.agent_configs[config.id] = config
        self.rt.adapters[config.id] = new
        self.rt.mcp.refresh_providers()
        if old is not None:
            self.rt.retired_adapters.append(old)
        self.rt.notify()
        return await self.describe(config.id)

    async def delete_secret(self, agent_id):
        async with self.rt.configuration_lock:
            await self.describe(agent_id)
            await asyncio.to_thread(self.rt.vault.set, agent_id, "")
            config = self.rt.agent_configs.get(agent_id)
            if config:
                await self._configure(config)
            return {"deleted": True}

    def _profile(self, agent_id):
        config = self.rt.agent_configs.get(agent_id)
        require(
            config is not None,
            "NOT_FOUND",
            "Agent does not have a managed configuration",
        )
        return config.model_dump(mode="json")

    async def documents(self, agent_id):
        profile = self._profile(agent_id)
        return {
            "agent_id": agent_id,
            "instructions": {"filename": "AGENT.md", "content": profile["agent_md"]},
            "skills": [
                {**s, "filename": f"skills/{s['id']}/SKILL.md"}
                for s in profile["skills"]
            ],
        }

    async def set_instructions(self, agent_id, content):
        async with self.rt.configuration_lock:
            profile = self._profile(agent_id)
            profile["agent_md"] = content
            await self._configure(profile)
        return await self.documents(agent_id)

    async def skills_list(self, agent_id):
        return self._profile(agent_id)["skills"]

    async def skill_put(self, agent_id, skill):
        skill = AgentSkill.model_validate(skill)
        async with self.rt.configuration_lock:
            profile = self._profile(agent_id)
            remaining = [s for s in profile["skills"] if s["id"] != skill.id]
            profile["skills"] = [*remaining, skill.model_dump(mode="json")]
            await self._configure(profile)
        return skill

    async def skill_import(self, agent_id, skill_id, markdown, enabled=True):
        from .profiles import skill_from_markdown

        return await self.skill_put(
            agent_id, skill_from_markdown(skill_id, markdown, enabled=enabled)
        )

    async def skill_delete(self, agent_id, skill_id):
        async with self.rt.configuration_lock:
            profile = self._profile(agent_id)
            require(
                any(s["id"] == skill_id for s in profile["skills"]),
                "NOT_FOUND",
                "Skill does not exist for this Agent",
            )
            profile["skills"] = [s for s in profile["skills"] if s["id"] != skill_id]
            await self._configure(profile)
        return {"deleted": True}


class Threads:
    def __init__(self, rt):
        self.rt = rt

    async def create(self, session_id, agent_id, title=None):
        self.rt.admit()
        adapter = self.rt.adapter(agent_id)
        identity = new_id("thr")

        def insert(db):
            session = need(db, "sessions", session_id)
            resolve_project(db, session.project_id)
            require(agent_project(db, agent_id) == session.project_id, "PROJECT_MISMATCH",
                    "Agent and session must belong to the same project")
            require(
                need(db, "sessions", session_id).state == "OPEN",
                "SESSION_CLOSED",
                "Workspace is closing",
            )
            db.execute(
                "INSERT INTO threads(id,session_id,agent_id,title,adapter_snapshot_json,created_at) VALUES(?,?,?,?,?,?)",
                (
                    identity,
                    session_id,
                    agent_id,
                    (title or agent_id)[:120],
                    json_dump(adapter.describe()),
                    now_ms(),
                ),
            )
            emit(
                db,
                "thread.created",
                session_id=session_id,
                thread_id=identity,
                payload={"agent_id": agent_id},
            )
            return ThreadSnapshot.model_validate(public(need(db, "threads", identity)))

        result = await self.rt.store.call(insert, transaction=True)
        self.rt.notify()
        return result

    async def get(self, thread_id):
        return await self.rt.store.call(
            lambda db: ThreadSnapshot.model_validate(
                public(need(db, "threads", thread_id))
            )
        )

    async def fork(self, thread_id, title=None):
        self.rt.admit()
        source = await self.get(thread_id)
        adapter = self.rt.adapter(source.agent_id)
        descriptor = adapter.describe()
        require(
            descriptor.supports_fork,
            "UNSUPPORTED_CAPABILITY",
            "Execution endpoint cannot fork checkpoints",
        )
        identity = new_id("thr")

        def branch(db):
            origin = need(db, "threads", thread_id)
            require(
                origin.latest_checkpoint_id is not None,
                "UNSUPPORTED_CAPABILITY",
                "No committed checkpoint is available",
            )
            require(
                need(db, "sessions", origin.session_id).state == "OPEN",
                "SESSION_CLOSED",
                "Workspace is closing",
            )
            cp = need(db, "checkpoints", origin.latest_checkpoint_id)
            require(
                cp.mode == "opaque_json"
                and cp.adapter_version == descriptor.adapter_version
                and cp.format_version == descriptor.checkpoint_format_version,
                "UNSUPPORTED_CAPABILITY",
                "This checkpoint cannot be copied safely",
            )
            new_cp = new_id("chk")
            db.execute(
                "INSERT INTO threads(id,session_id,agent_id,title,adapter_snapshot_json,revision,latest_checkpoint_id,fork_of_thread_id,created_at) VALUES(?,?,?,?,?,1,?,?,?)",
                (
                    identity,
                    origin.session_id,
                    origin.agent_id,
                    (title or origin.title + " branch")[:120],
                    origin.adapter_snapshot_json,
                    new_cp,
                    origin.id,
                    now_ms(),
                ),
            )
            db.execute(
                "INSERT INTO checkpoints VALUES(?,?,1,?,?,?,?,NULL,?)",
                (
                    new_cp,
                    identity,
                    cp.adapter_version,
                    cp.format_version,
                    cp.mode,
                    cp.ref_json,
                    now_ms(),
                ),
            )
            for entry in rows(
                db,
                "SELECT * FROM conversation WHERE thread_id=? ORDER BY seq",
                (origin.id,),
            ):
                db.execute(
                    "INSERT INTO conversation(id,session_id,thread_id,role,content,metadata_json,created_at,summary_markdown) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        new_id("chat"),
                        origin.session_id,
                        identity,
                        entry.role,
                        entry.content,
                        entry.metadata_json,
                        entry.created_at,
                        entry.summary_markdown,
                    ),
                )
            emit(
                db,
                "thread.forked",
                session_id=origin.session_id,
                thread_id=identity,
                payload={"source_thread_id": origin.id},
            )
            return ThreadSnapshot.model_validate(public(need(db, "threads", identity)))

        result = await self.rt.store.call(branch, transaction=True)
        self.rt.notify()
        return result

    async def conversation(
        self, thread_id, cursor=None, limit=100, *, latest=False, before_seq=None, presentation=False
    ):
        def entry(row):
            value = public(row)
            if presentation and value["role"] == "assistant":
                from .presentation import markdown_summary
                value["content"] = markdown_summary(value.get("summary_markdown") or value["content"])
            return ConversationEntry.model_validate(value)
        await self.get(thread_id)
        limit = min(200, max(1, int(limit)))
        if latest:
            found = await self.rt.store.call(
                lambda db: rows(
                    db,
                    "SELECT * FROM conversation WHERE thread_id=? AND seq<? ORDER BY seq DESC LIMIT ?",
                    (
                        thread_id,
                        (
                            int(before_seq)
                            if before_seq is not None
                            else 9223372036854775807
                        ),
                        limit + 1,
                    ),
                )
            )
            selected = list(reversed(found[:limit]))
            return PageEnvelope(
                items=[entry(r) for r in selected],
                cursor=str(selected[0].seq) if selected else None,
                has_more=len(found) > limit,
            )
        found = await self.rt.store.call(
            lambda db: rows(
                db,
                "SELECT * FROM conversation WHERE thread_id=? AND seq>? ORDER BY seq LIMIT ?",
                (thread_id, int(cursor or 0), limit + 1),
            )
        )
        return PageEnvelope(
            items=[entry(r) for r in found[:limit]],
            cursor=str(found[min(limit, len(found)) - 1].seq) if found else cursor,
            has_more=len(found) > limit,
        )

    async def turn(self, thread_id, content, idempotency_key, **options):
        thread = await self.get(thread_id)
        payload = {"input": str(content)}
        return await self.rt.tasks.submit(
            TaskSpec(
                session_id=thread.session_id,
                agent_id=thread.agent_id,
                thread_id=thread_id,
                payload=payload,
                idempotency_key=idempotency_key,
                **options,
            )
        )


class Tasks:
    def __init__(self, rt):
        self.rt = rt

    async def progress(self, task_id):
        from .presentation import result_summary
        task = await self.get(task_id)
        live = [v for v in self.rt.events.summaries.values() if v["task_id"] == task_id]
        return {"task_id": task_id, "state": task.state, "format": "markdown",
                "summary_markdown": result_summary(task.result) if task.result else
                    (live[-1]["summary_markdown"] if live else ""),
                "ephemeral": not bool(task.result)}

    def insert(self, db, spec, identity=None, retry_of=None, caller_run_id=None):
        digest = content_hash(spec)
        existing = one(
            db,
            "SELECT * FROM tasks WHERE session_id=? AND idempotency_key=?",
            (spec.session_id, spec.idempotency_key),
        )
        if existing:
            require(
                existing.request_hash == digest,
                "IDEMPOTENCY_CONFLICT",
                "Idempotency key has different input",
            )
            require(
                existing.retry_of_task_id == retry_of,
                "IDEMPOTENCY_CONFLICT",
                "Idempotency key belongs to another retry request",
            )
            return TaskHandle(id=existing.id, state=existing.state, reused=True)
        session = need(db, "sessions", spec.session_id)
        resolve_project(db, session.project_id)
        require(agent_project(db, spec.agent_id) == session.project_id, "PROJECT_MISMATCH",
                "Agent and session must belong to the same project")
        allowed = session.state == "OPEN"
        if caller_run_id is not None:
            caller, run = self.rt.check_run(db, caller_run_id)
            self.rt.check_collaboration(run)
            require(
                caller.session_id == spec.session_id,
                "PERMISSION_DENIED",
                "Cross-session task is not allowed",
            )
            allowed = allowed or (
                session.state == "CLOSING" and session.close_mode == "drain"
            )
        require(allowed, "SESSION_CLOSED", "Workspace is closing")
        thread = need(db, "threads", spec.thread_id)
        require(
            thread.session_id == spec.session_id and thread.agent_id == spec.agent_id,
            "INVALID_REQUEST",
            "Thread, session and agent do not match",
        )
        self.rt.adapter(spec.agent_id)
        queued = one(
            db, "SELECT COUNT(*) AS count FROM tasks WHERE state='QUEUED'"
        ).count
        require(
            queued < self.rt.config.max_queued_tasks,
            "QUEUE_FULL",
            "Task queue is full",
            retryable=True,
        )
        for dependency_id in spec.depends_on:
            dependency = need(db, "tasks", dependency_id)
            require(
                dependency.session_id == spec.session_id,
                "INVALID_REQUEST",
                "Dependencies must share a workspace",
            )
            require(
                dependency_id != identity,
                "DEPENDENCY_CYCLE",
                "A task cannot depend on itself",
            )
        identity = identity or new_id("tsk")
        stamp = now_ms()
        db.execute(
            "INSERT INTO tasks(id,session_id,thread_id,spec_json,state,queue_reason,idempotency_key,request_hash,retry_of_task_id,created_at,updated_at) VALUES(?,?,?,?,'QUEUED','SCHEDULER',?,?,?,?,?)",
            (
                identity,
                spec.session_id,
                spec.thread_id,
                json_dump(spec),
                spec.idempotency_key,
                digest,
                retry_of,
                stamp,
                stamp,
            ),
        )
        for dependency in set(spec.depends_on):
            db.execute(
                "INSERT INTO task_dependencies VALUES(?,?)", (identity, dependency)
            )
        user_input = spec.payload.get("input")
        if isinstance(user_input, str) and user_input:
            db.execute(
                "INSERT INTO conversation(id,session_id,thread_id,task_id,role,content,metadata_json,created_at) VALUES(?,?,?,?,'user',?,'{}',?)",
                (
                    new_id("chat"),
                    spec.session_id,
                    spec.thread_id,
                    identity,
                    user_input,
                    stamp,
                ),
            )
        emit(
            db,
            "task.accepted",
            session_id=spec.session_id,
            thread_id=spec.thread_id,
            task_id=identity,
            payload={"agent_id": spec.agent_id, "state": "QUEUED"},
        )
        return TaskHandle(id=identity, state="QUEUED", reused=False)

    async def submit(self, spec: TaskSpec | dict, *, caller_run_id=None):
        self.rt.admit()
        spec = TaskSpec.model_validate(spec)
        spec = TaskSpec.model_validate(self.rt.clean_json(spec.model_dump(mode="json")))
        result = await self.rt.store.call(
            lambda db: self.insert(db, spec, caller_run_id=caller_run_id),
            transaction=True,
        )
        self.rt.notify()
        return result

    async def workflow(self, session_id, nodes, idempotency_key):
        self.rt.admit()
        require(
            0 < len(nodes) <= 100,
            "INVALID_REQUEST",
            "Workflow must contain 1 to 100 tasks",
        )
        keyed = {n["key"]: n for n in nodes}
        require(
            len(keyed) == len(nodes), "INVALID_REQUEST", "Workflow keys must be unique"
        )
        pending, order = set(keyed), []
        while pending:
            ready = [
                key
                for key in keyed
                if key in pending
                and not (set(keyed[key].get("depends_on", [])) & pending)
            ]
            require(ready, "DEPENDENCY_CYCLE", "Workflow dependencies contain a cycle")
            order.extend(ready)
            pending.difference_update(ready)

        def insert(db):
            results = {}
            for key in order:
                node = dict(keyed[key])
                node.pop("key")
                node["depends_on"] = tuple(
                    results[d].id if d in results else d
                    for d in node.get("depends_on", ())
                )
                thread = need(db, "threads", node["thread_id"])
                node.setdefault("agent_id", thread.agent_id)
                spec = TaskSpec.model_validate(
                    self.rt.clean_json(
                        {
                            **node,
                            "session_id": session_id,
                            "idempotency_key": f"workflow:{idempotency_key}:{key}",
                        }
                    )
                )
                results[key] = self.insert(db, spec)
            return results

        result = await self.rt.store.call(insert, transaction=True)
        self.rt.notify()
        return result

    async def get(self, task_id, presentation=False):
        def read(db):
            task = public(need(db, "tasks", task_id))
            task["runs"] = [
                public(r)
                for r in rows(
                    db,
                    "SELECT * FROM runs WHERE task_id=? ORDER BY attempt_no",
                    (task_id,),
                )
            ]
            current = [
                r
                for r in task["runs"]
                if r["state"] in {"RUNNING", "UNCERTAIN"} and r["ended_at"] is None
            ]
            task["current_run"] = current[-1] if current else None
            return TaskSnapshot.model_validate(task)

        result = await self.rt.store.call(read)
        if presentation:
            from .presentation import markdown_summary, result_summary
            value = result.model_dump(mode="json")
            value["title"] = markdown_summary(value["spec"]["payload"].get("input") or
                                             value["spec"]["metadata"].get("stage") or "协作任务", limit=160)
            value["spec"]["payload"] = {}
            if result.result:
                value["result"]["summary_markdown"] = result_summary(result.result)
                value["result"]["output"] = {}
            return value
        return result

    async def list(self, session_id, cursor=None, limit=100):
        await self.rt.sessions.get(session_id)
        limit = max(1, min(int(limit), 200))
        found = await self.rt.store.call(
            lambda db: rows(
                db,
                "SELECT rowid AS position,* FROM tasks WHERE session_id=? AND rowid>? ORDER BY rowid LIMIT ?",
                (session_id, int(cursor or 0), limit + 1),
            )
        )
        return PageEnvelope(
            items=[TaskSnapshot.model_validate(public(r)) for r in found[:limit]],
            cursor=str(found[min(limit, len(found)) - 1].position) if found else cursor,
            has_more=len(found) > limit,
        )

    async def result(self, task_id, timeout=None):
        deadline = (
            None
            if timeout is None
            else asyncio.get_running_loop().time() + float(timeout)
        )
        while True:
            task = await self.get(task_id)
            if task.state == "SUCCEEDED":
                return TaskResult.model_validate(task.result)
            if task.state in TERMINAL or task.state == "UNCERTAIN":
                raise RuntimeFault(
                    (
                        "OUTCOME_UNCERTAIN"
                        if task.state == "UNCERTAIN"
                        else f"TASK_{task.state}"
                    ),
                    (
                        "Task requires reconciliation"
                        if task.state == "UNCERTAIN"
                        else f"Task ended with {task.state}"
                    ),
                    task_id=task_id,
                    details={"error": task.error},
                )
            if deadline is not None and asyncio.get_running_loop().time() >= deadline:
                raise RuntimeFault(
                    "WAIT_TIMEOUT",
                    "Client wait timed out; task continues",
                    task_id=task_id,
                )
            await asyncio.sleep(0.05)

    async def cancel(self, task_id):
        def cancel(db):
            task = need(db, "tasks", task_id)
            if task.state in TERMINAL or task.state == "UNCERTAIN":
                return View(
                    accepted=False,
                    confirmed_stopped=task.state in TERMINAL,
                    state=task.state,
                )
            if task.state == "RUNNING":
                run = one(
                    db,
                    "SELECT adapter_snapshot_json FROM runs WHERE task_id=? AND state='RUNNING' ORDER BY attempt_no DESC LIMIT 1",
                    (task_id,),
                )
                require(
                    run is not None
                    and json.loads(run.adapter_snapshot_json)["cancellation"]
                    != "unsupported",
                    "UNSUPPORTED_CAPABILITY",
                    "Execution endpoint does not support cancellation",
                )
            db.execute(
                "UPDATE tasks SET cancel_requested_at=?,updated_at=? WHERE id=?",
                (now_ms(), now_ms(), task_id),
            )
            if task.state in {"QUEUED", "WAITING"}:
                self.rt.finish_idle(db, task, "CANCELLED", None)
            emit(
                db,
                "task.cancel_requested",
                session_id=task.session_id,
                thread_id=task.thread_id,
                task_id=task.id,
            )
            return View(
                accepted=True,
                confirmed_stopped=task.state != "RUNNING",
                state="CANCELLED" if task.state != "RUNNING" else "RUNNING",
            )

        result = await self.rt.store.call(cancel, transaction=True)
        for active in self.rt.active.values():
            if active.request.task_id == task_id:
                active.context.cancelled.set()
        self.rt.notify()
        return result

    async def retry(self, task_id, idempotency_key):
        original = await self.get(task_id)
        require(
            original.state in TERMINAL,
            "INVALID_STATE",
            "Only terminal tasks can be retried; reconcile uncertain tasks first",
        )
        spec = TaskSpec.model_validate(
            {**original.spec, "idempotency_key": idempotency_key}
        )
        self.rt.admit()
        result = await self.rt.store.call(
            lambda db: self.insert(db, spec, retry_of=task_id), transaction=True
        )
        self.rt.notify()
        return result

    async def resume(self, task_id, input, idempotency_key):
        self.rt.admit()
        value = self.rt.clean_json(input)
        digest = content_hash(value)

        def resume(db):
            task = need(db, "tasks", task_id)
            receipt = one(
                db,
                "SELECT request_hash FROM resume_receipts WHERE task_id=? AND idempotency_key=?",
                (task_id, idempotency_key),
            )
            if receipt:
                require(
                    receipt.request_hash == digest,
                    "IDEMPOTENCY_CONFLICT",
                    "Resume key has different input",
                )
                return TaskHandle(id=task_id, state=task.state, reused=True)
            require(task.state == "WAITING", "INVALID_STATE", "Task is not waiting")
            wait = json.loads(task.wait_json)
            require(
                wait["kind"] == "input"
                and value.get("wait_token") == wait.get("wait_token"),
                "INVALID_REQUEST",
                "Input does not match the current wait token",
            )
            db.execute(
                "INSERT INTO resume_receipts VALUES(?,?,?,?,?)",
                (task_id, idempotency_key, digest, json_dump(value), now_ms()),
            )
            db.execute(
                "UPDATE tasks SET state='QUEUED',queue_reason='RESUME',resume_input_json=?,resume_key=?,resume_hash=?,updated_at=? WHERE id=?",
                (json_dump(value), idempotency_key, digest, now_ms(), task_id),
            )
            emit(
                db,
                "task.resumed",
                session_id=task.session_id,
                thread_id=task.thread_id,
                task_id=task_id,
            )
            return TaskHandle(id=task_id, state="QUEUED", reused=False)

        result = await self.rt.store.call(resume, transaction=True)
        self.rt.notify()
        return result

    async def reconcile(self, task_id, resolution):
        resolution = Reconciliation.model_validate(resolution)
        require(
            resolution.confirmed_stopped and resolution.external_actions_confirmed,
            "OUTCOME_UNCERTAIN",
            "Reconciliation must confirm stopped execution and external actions",
        )
        for active in self.rt.active.values():
            require(
                active.request.task_id != task_id
                or (active.worker is not None and active.worker.done()),
                "OUTCOME_UNCERTAIN",
                "A known execution is still active",
            )
        from .runtime import LOCAL_WORKERS

        saved = await self.get(task_id)
        for run in saved.runs:
            worker = LOCAL_WORKERS.get(run.id)
            require(
                worker is None or worker.done(),
                "OUTCOME_UNCERTAIN",
                "A previous local execution is still active",
            )
        if resolution.resolution == "QUEUED":
            require(
                resolution.safe_to_retry,
                "OUTCOME_UNCERTAIN",
                "Requeue requires verified retry safety",
            )
        if resolution.resolution == "SUCCEEDED":
            require(
                resolution.result is not None,
                "INVALID_REQUEST",
                "Successful reconciliation requires a result",
            )

        def resolve(db):
            task = need(db, "tasks", task_id)
            require(task.state == "UNCERTAIN", "INVALID_STATE", "Task is not uncertain")
            run = one(
                db,
                "SELECT * FROM runs WHERE task_id=? ORDER BY attempt_no DESC LIMIT 1",
                (task_id,),
            )
            if resolution.result:
                self.rt.check_artifacts(
                    db, task.session_id, resolution.result.artifact_ids
                )
            value = resolution.resolution
            result_json = json_dump(resolution.result) if resolution.result else None
            db.execute(
                "UPDATE tasks SET state=?,result_json=?,error_json=NULL,queue_reason=?,updated_at=? WHERE id=?",
                (
                    value,
                    result_json,
                    "RECONCILED" if value == "QUEUED" else None,
                    now_ms(),
                    task_id,
                ),
            )
            db.execute(
                "UPDATE threads SET active_run_id=NULL,blocked_reason=NULL,owner_task_id=? WHERE id=?",
                (task_id if value == "QUEUED" else None, task.thread_id),
            )
            if value == "SUCCEEDED":
                db.execute(
                    "UPDATE threads SET revision=revision+1 WHERE id=?",
                    (task.thread_id,),
                )
            if value == "QUEUED":
                db.execute(
                    "UPDATE tasks SET cancel_requested_at=NULL WHERE id=?", (task_id,)
                )
            if run:
                db.execute(
                    "UPDATE runs SET state=?,ended_at=?,error_json=? WHERE id=?",
                    (
                        "RECONCILED",
                        now_ms(),
                        json_dump({"evidence": resolution.evidence}),
                        run.id,
                    ),
                )
                db.execute(
                    "UPDATE messages SET state='PENDING',target_run_id=NULL WHERE target_run_id=? AND state='OFFERED'",
                    (run.id,),
                )
                db.execute(
                    "UPDATE tool_calls SET state='RESOLVED',error_json=?,updated_at=? WHERE run_id=? AND state IN ('INTENT','DISPATCHED','UNCERTAIN')",
                    (json_dump({"evidence": resolution.evidence}), now_ms(), run.id),
                )
            if resolution.result:
                self.rt.append_reply(
                    db, task, run.id if run else None, resolution.result
                )
            emit(
                db,
                "task.reconciled",
                session_id=task.session_id,
                thread_id=task.thread_id,
                task_id=task_id,
                payload={"state": value, "evidence": resolution.evidence},
            )
            return run.id if run else None

        run_id = await self.rt.store.call(resolve, transaction=True)
        if run_id:
            self.rt.resources.resolve(run_id)
            self.rt.uncertain_slots.discard(run_id)
            self.rt._uncertain_owners.pop(run_id, None)
            for owner in list(self.rt.resources.isolated):
                if owner.startswith(run_id + ":"):
                    self.rt.resources.resolve(owner)
        self.rt.notify()
        return await self.get(task_id)


class Messages:
    def __init__(self, rt):
        self.rt = rt

    def insert(self, db, spec, caller_run_id=None):
        source_key = spec.source_thread_id or "external"
        semantic = spec.model_dump(
            mode="json", exclude={"source_run_id", "causation_id"}
        )
        digest = content_hash(semantic)
        existing = one(
            db,
            "SELECT * FROM messages WHERE session_id=? AND source_key=? AND idempotency_key=?",
            (spec.session_id, source_key, spec.idempotency_key),
        )
        if existing:
            require(
                existing.request_hash == digest,
                "IDEMPOTENCY_CONFLICT",
                "Message key has different input",
            )
            return MessageHandle(id=existing.id, state=existing.state, reused=True)
        session = need(db, "sessions", spec.session_id)
        require(
            session.state == "OPEN"
            or (
                caller_run_id
                and session.state == "CLOSING"
                and session.close_mode == "drain"
            ),
            "SESSION_CLOSED",
            "Workspace is closing",
        )
        target = need(db, "threads", spec.target_thread_id)
        require(
            target.session_id == spec.session_id,
            "INVALID_REQUEST",
            "Target belongs to another workspace",
        )
        require(
            spec.target_port in self.rt.adapter(target.agent_id).describe().ports,
            "INVALID_REQUEST",
            "Target port is not registered",
        )
        if spec.source_thread_id:
            require(
                need(db, "threads", spec.source_thread_id).session_id
                == spec.session_id,
                "INVALID_REQUEST",
                "Source belongs to another workspace",
            )
        if caller_run_id:
            caller, run = self.rt.check_run(db, caller_run_id)
            self.rt.check_collaboration(run)
            require(
                caller.session_id == spec.session_id
                and caller.thread_id == spec.source_thread_id
                and caller.id == spec.source_task_id
                and run.id == spec.source_run_id,
                "PERMISSION_DENIED",
                "Message source does not match the authenticated run",
            )
        else:
            require(
                spec.source_run_id is None and spec.source_task_id is None,
                "PERMISSION_DENIED",
                "External callers cannot impersonate a run",
            )
        self.rt.check_artifacts(db, spec.session_id, spec.artifact_ids)
        identity = new_id("msg")
        envelope = {"message_id": identity, **spec.model_dump(mode="json")}
        db.execute(
            "INSERT INTO messages(id,session_id,source_key,source_thread_id,target_thread_id,target_port,envelope_json,state,idempotency_key,request_hash,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'PENDING',?,?,?,?)",
            (
                identity,
                spec.session_id,
                source_key,
                spec.source_thread_id,
                spec.target_thread_id,
                spec.target_port,
                json_dump(envelope),
                spec.idempotency_key,
                digest,
                now_ms(),
                now_ms(),
            ),
        )
        emit(
            db,
            "message.accepted",
            session_id=spec.session_id,
            thread_id=target.id,
            payload={
                "message_id": identity,
                "source_thread_id": spec.source_thread_id,
                "target_thread_id": target.id,
                "state": "PENDING",
            },
        )
        return MessageHandle(id=identity, state="PENDING", reused=False)

    async def send(self, spec, *, caller_run_id=None):
        self.rt.admit()
        spec = MessageSpec.model_validate(spec)
        spec = MessageSpec.model_validate(
            self.rt.clean_json(spec.model_dump(mode="json"))
        )
        result = await self.rt.store.call(
            lambda db: self.insert(db, spec, caller_run_id), transaction=True
        )
        self.rt.notify()
        return result

    async def get(self, message_id):
        return await self.rt.store.call(
            lambda db: MessageSnapshot.model_validate(
                public(need(db, "messages", message_id))
            )
        )

    async def list(self, session_id, cursor=None, limit=100):
        await self.rt.sessions.get(session_id)
        limit = max(1, min(int(limit), 200))
        found = await self.rt.store.call(
            lambda db: rows(
                db,
                "SELECT rowid AS position,* FROM messages WHERE session_id=? AND rowid>? ORDER BY rowid LIMIT ?",
                (session_id, int(cursor or 0), limit + 1),
            )
        )
        return PageEnvelope(
            items=[MessageSnapshot.model_validate(public(r)) for r in found[:limit]],
            cursor=str(found[min(limit, len(found)) - 1].position) if found else cursor,
            has_more=len(found) > limit,
        )
