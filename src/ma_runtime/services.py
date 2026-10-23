"""Public application services used by every transport."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from .errors import RuntimeFault, execution_state, require
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
            snapshot = SessionSnapshot(
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
                        "SELECT * FROM messages WHERE session_id=? AND (state IN ('PENDING','OFFERED') OR id IN (SELECT id FROM messages WHERE session_id=? ORDER BY created_at DESC,rowid DESC LIMIT 200)) ORDER BY created_at DESC,rowid DESC" + (" LIMIT 200" if presentation else ""),
                        (session_id, session_id),
                    )
                ],
                artifacts=[
                    public(r)
                    for r in rows(
                        db,
                        "SELECT * FROM artifacts WHERE session_id=? AND COALESCE(json_extract(metadata_json,'$.internal'),0)=0 ORDER BY created_at DESC LIMIT 100",
                        (session_id,),
                    )
                ],
                cursor=cursor,
            )
            attention = [r.thread_id for r in rows(db,
                "SELECT DISTINCT t.thread_id FROM operations_reports r JOIN tasks t ON t.id=r.task_id WHERE r.session_id=? AND r.reply_message_id IS NULL",
                (session_id,))] if presentation else []
            failed = [r.id for r in rows(db,
                "SELECT h.id,t.error_json FROM threads h JOIN tasks t ON t.id=(SELECT id FROM tasks WHERE thread_id=h.id ORDER BY created_at DESC,rowid DESC LIMIT 1) WHERE h.session_id=? AND t.state='FAILED'",
                (session_id,)) if execution_state("FAILED", json.loads(r.error_json) if r.error_json else None) == "FAILED"] if presentation else []
            reports = [dict(r) for r in rows(db,
                "SELECT r.id,t.thread_id,r.task_id,r.summary AS summary_markdown,r.wait_for_guidance,r.reply_message_id IS NOT NULL AS replied,r.created_at FROM operations_reports r JOIN tasks t ON t.id=r.task_id WHERE r.session_id=? ORDER BY r.created_at DESC,r.rowid DESC LIMIT 10",
                (session_id,))] if presentation else []
            activity = self.rt.supervision.summaries(db, session_id) if presentation else []
            positions = self.rt.supervision.display_positions(db, snapshot.messages, reports) if presentation else {}
            delivery = {row.message_id: row.state for row in rows(db,
                "SELECT message_id,state FROM message_delivery WHERE message_id IN (" + ",".join("?" for _ in snapshot.messages) + ")",
                tuple(item.id for item in snapshot.messages))} if presentation and snapshot.messages else {}
            return snapshot, attention, failed, reports, activity, positions, delivery

        result, attention, failed, reports, activity, positions, delivery = await self.rt.store.call(read, transaction=True)
        if presentation:
            from .presentation import markdown_summary, result_summary
            value = result.model_dump(mode="json")
            value["attention_thread_ids"] = attention
            value["failed_thread_ids"] = failed
            value["thread_activity"] = activity
            value["message_activity"] = positions
            value["agent_reports"] = [{**report, "summary_markdown": markdown_summary(report["summary_markdown"], limit=320),
                                       "wait_for_guidance": bool(report["wait_for_guidance"]), "replied": bool(report["replied"])}
                                      for report in reports]
            for task in value["tasks"]:
                task["title"] = markdown_summary(task["spec"]["payload"].get("input") or
                                                task["spec"]["metadata"].get("stage") or "协作任务", limit=160)
                task["spec"]["payload"] = {}
                if task["result"]:
                    summary = result_summary(TaskResult.model_validate(task["result"]))
                    task["result"]["output"] = {}
                    task["result"]["summary_markdown"] = summary
            for message in value["messages"]:
                message["delivery_state"] = delivery.get(message["id"], message["state"])
                if message["delivery_state"] in {"REJECTED", "EXPIRED"}:
                    message["state"] = message["delivery_state"]
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

    async def capabilities(self, agent_id):
        profile = self.rt.agent_configs.get(agent_id)
        require(profile is not None, "NOT_FOUND", "Agent does not have a managed configuration")
        from .profiles import skill_catalog
        from .execution_policy import PLATFORM_TOOLS
        platform_tools = set(PLATFORM_TOOLS)
        if profile.kind == "llm":
            platform_tools -= {"put_file", "put_bytes"}
        if not profile.collaboration:
            platform_tools -= {"list_threads", "send_message", "submit_task", "spawn_instances"}
        catalog = skill_catalog(profile.skills)
        for binding in profile.skill_bindings:
            entry = self.rt.skills.entry(binding.skill_id)
            catalog.extend({**item, "enabled": entry["enabled"]} for item in skill_catalog((entry["skill"],)))
        return {"agent_id": agent_id, "project_id": profile.project_id,
                "workbench": profile.workbench.model_dump(mode="json") if profile.workbench else None,
                "workbench_tools": list(profile.workbench.tools) if profile.workbench else [],
                "skills": catalog,
                "mcp_bindings": [b.model_dump(mode="json") for b in profile.mcp_bindings],
                "platform_tools": sorted(platform_tools),
                "control_tools": ["report_issue"],
                "policy_defaults": {"tool_policy": {"mode": "auto"}, "skill_policy": {"mode": "auto"}}}

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
        self.rt.skills.validate_bindings(config.skill_bindings)
        self.rt.skills.validate_agent(config)
        self.rt.environments.validate(config)
        require(
            config.id != "codex", "INVALID_REQUEST", "Codex is an external coordinator"
        )
        old = self.rt.adapters.get(config.id)
        if api_key is not None:
            require(len(api_key) <= 16384, "INVALID_REQUEST", "Credential is too large")
            await asyncio.to_thread(self.rt.vault.set, config.id, api_key)
        preserve = config.id in self.rt.custom_adapter_ids and (previous is None or previous.kind == config.kind)
        new = old if preserve else self.rt.make_adapter(config)
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
        if not preserve:
            self.rt.custom_adapter_ids.discard(config.id)
        self.rt.mcp.refresh_providers()
        if old is not None and old is not new:
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
        from .operations import OPERATIONS_GUIDANCE
        return {
            "agent_id": agent_id,
            "instructions": {"filename": "AGENT.md", "aliases": ["AGENTS.md"], "content": profile["agent_md"]},
            "runtime_instructions": {"content": OPERATIONS_GUIDANCE},
            "skills": [
                {**s, "filename": f"skills/{s['id']}/SKILL.md"}
                for s in [skill.model_dump(mode="json") for skill in self.rt.skills.for_agent(self.rt.agent_configs[agent_id])]
            ],
        }

    async def set_instructions(self, agent_id, content):
        async with self.rt.configuration_lock:
            profile = self._profile(agent_id)
            profile["agent_md"] = content
            await self._configure(profile)
        return await self.documents(agent_id)

    async def skills_list(self, agent_id):
        self._profile(agent_id)
        return [skill.model_dump(mode="json") for skill in self.rt.skills.for_agent(self.rt.agent_configs[agent_id])]

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
        self.fork_locks = {}

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

    async def ownership(self, thread_id):
        """Explicit origin view; the original strict thread snapshot stays stable."""
        def read(db):
            thread = need(db, "threads", thread_id)
            session = need(db, "sessions", thread.session_id)
            owner = one(db, "SELECT w.id,w.workflow_id,w.revision FROM thread_workflow_runs h "
                        "JOIN workflow_runs w ON w.id=h.run_id WHERE h.thread_id=?", (thread_id,))
            return {"thread_id": thread_id, "session_id": thread.session_id, "project_id": session.project_id,
                    "workflow_run_id": owner.id if owner else None,
                    "workflow_id": owner.workflow_id if owner else None,
                    "workflow_revision": owner.revision if owner else None}
        return await self.rt.store.call(read)

    async def instances(self, session_id, agent_id=None, cursor=None, limit=25):
        return await self.rt.supervision.instances(session_id, agent_id, cursor, limit)

    async def activity(self, thread_id, cursor=None, limit=50):
        return await self.rt.supervision.activity(thread_id, cursor, limit)

    async def mark_read(self, thread_id, positions):
        return await self.rt.supervision.mark_read(thread_id, positions)

    async def history(self, thread_id, *, checkpoint_id=None, cursor=None, limit=50):
        """Page immutable native messages without projecting or replaying them."""
        require(cursor is None or checkpoint_id, "INVALID_REQUEST", "Continue history pages at the returned checkpoint_id")
        def read(db):
            thread = need(db, "threads", thread_id)
            checkpoint = need(db, "checkpoints", checkpoint_id or thread.latest_checkpoint_id)
            require(checkpoint.thread_id == thread.id, "PERMISSION_DENIED", "Checkpoint belongs to another thread")
            from .chat_history import CHAT_VERSION, LEGACY_CHAT_VERSION
            require(checkpoint.format_version in {CHAT_VERSION, LEGACY_CHAT_VERSION}, "UNSUPPORTED_CAPABILITY", "This adapter does not store native message history")
            return self.rt.chat_history.page(db, checkpoint, thread, cursor, limit)
        return await self.rt.store.call(read, transaction=True)

    async def prepare_workdir(self, thread_id, idempotency_key, kind="managed", commit=None):
        return await self.rt.workdirs.prepare(thread_id, idempotency_key, kind, commit)

    async def workdir(self, thread_id):
        return await self.rt.workdirs.get(thread_id)

    async def fork(self, thread_id, title=None, *, checkpoint_id=None, message_id=None, idempotency_key=None):
        self.rt.admit()
        source = await self.get(thread_id)
        adapter = self.rt.adapter(source.agent_id)
        descriptor = adapter.describe()
        require(descriptor.supports_fork, "UNSUPPORTED_CAPABILITY", "Execution endpoint cannot fork checkpoints")
        require(not (checkpoint_id and message_id), "INVALID_REQUEST", "Choose a checkpoint or a result message")
        key = idempotency_key or new_id("fork")
        require(isinstance(key, str) and 0 < len(key) <= 256, "INVALID_REQUEST", "Use a stable fork request key")
        digest = content_hash({"title": title, "checkpoint_id": checkpoint_id, "message_id": message_id})

        def reserve(db):
            origin = need(db, "threads", thread_id)
            require(need(db, "sessions", origin.session_id).state == "OPEN", "SESSION_CLOSED", "Workspace is closing")
            prior = one(db, "SELECT * FROM thread_fork_receipts WHERE source_thread_id=? AND idempotency_key=?", (thread_id, key))
            if prior:
                require(prior.request_hash == digest, "IDEMPOTENCY_CONFLICT", "Fork key has different input")
                cp_id, identity = prior.checkpoint_id, prior.thread_id
                if prior.completed:
                    return {"existing": identity}
            else:
                cp_id = checkpoint_id or origin.latest_checkpoint_id
                if message_id:
                    message = one(db, "SELECT run_id,role FROM conversation WHERE id=? AND thread_id=?", (message_id, thread_id))
                    point = one(db, "SELECT id FROM checkpoints WHERE created_by_run_id=? AND thread_id=?", (message.run_id, thread_id)) if message and message.role == "assistant" else None
                    require(point, "UNSUPPORTED_CAPABILITY", "Select a result with a committed checkpoint")
                    cp_id = point.id
                require(cp_id, "UNSUPPORTED_CAPABILITY", "No committed checkpoint is available")
                identity = new_id("thr")
            cp = need(db, "checkpoints", cp_id)
            native = descriptor.checkpoint_format_version == "chat-messages-v2" and cp.format_version in {"chat-messages-v1", "chat-messages-v2"}
            require(cp.thread_id == thread_id and cp.adapter_version == descriptor.adapter_version
                    and (cp.format_version == descriptor.checkpoint_format_version or native)
                    and cp.mode == descriptor.checkpoint_mode,
                    "UNSUPPORTED_CAPABILITY", "This checkpoint is not available to the source thread")
            from .chat_history import checkpoint_window
            window = checkpoint_window(db, cp, origin)
            reference = json.loads(cp.ref_json)
            if native:
                head = self.rt.chat_history.head(db, cp, origin)
                count = one(db, "SELECT message_count FROM chat_segments WHERE id=?", (head,)).message_count if head else 0
                reference = {"task_id": reference.get("task_id"), "message_head": head, "message_count": count}
            if not prior:
                db.execute("INSERT INTO thread_fork_receipts VALUES(?,?,?,?,?,0)", (thread_id, key, digest, identity, cp_id))
            return {"identity": identity, "checkpoint": cp, "reference": reference, "window": window, "native": native}

        prepared = await self.rt.store.call(reserve, transaction=True)
        if prepared.get("existing"):
            return await self.get(prepared["existing"])
        identity, cp = prepared["identity"], prepared["checkpoint"]
        reference = prepared["reference"]
        def branch(db):
            origin = need(db, "threads", thread_id)
            require(need(db, "sessions", origin.session_id).state == "OPEN", "SESSION_CLOSED", "Workspace is closing")
            prior = one(db, "SELECT completed FROM thread_fork_receipts WHERE source_thread_id=? AND idempotency_key=?", (thread_id, key))
            if prior.completed:
                return ThreadSnapshot.model_validate(public(need(db, "threads", identity)))
            new_cp = new_id("chk")
            db.execute(
                "INSERT INTO threads(id,session_id,agent_id,title,adapter_snapshot_json,revision,latest_checkpoint_id,fork_of_thread_id,created_at) VALUES(?,?,?,?,?,1,?,?,?)",
                (
                    identity,
                    origin.session_id,
                    origin.agent_id,
                    (title or origin.title + " branch")[:120],
                    json_dump(descriptor),
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
                    descriptor.checkpoint_format_version,
                    cp.mode,
                    json_dump(reference),
                    now_ms(),
                ),
            )
            window = prepared["window"]
            db.execute("INSERT INTO thread_branches VALUES(?,?,?,?,?)", (identity, thread_id, cp.id, window.message_seq, window.user_seq))
            db.execute("INSERT INTO checkpoint_windows VALUES(?,?,?)", (new_cp, window.user_seq, window.message_seq))
            if prepared["native"]:
                db.execute("INSERT INTO chat_checkpoint_heads VALUES(?,?)", (new_cp, reference.get("message_head")))
            self.rt.context_merges.fork(db, cp.id, identity, new_cp)
            db.execute("UPDATE thread_fork_receipts SET completed=1 WHERE source_thread_id=? AND idempotency_key=?", (thread_id, key))
            emit(
                db,
                "thread.forked",
                session_id=origin.session_id,
                thread_id=identity,
                payload={"source_thread_id": origin.id},
            )
            return ThreadSnapshot.model_validate(public(need(db, "threads", identity)))

        if cp.mode == "native_ref":
            require(hasattr(adapter, "fork_checkpoint"), "UNSUPPORTED_CAPABILITY", "Adapter has no native checkpoint fork implementation")
            lock_key = (thread_id, key)
            guard = self.fork_locks.setdefault(lock_key, [asyncio.Lock(), 0])
            guard[1] += 1
            try:
                async with guard[0]:
                    completed = await self.rt.store.call(lambda db: one(db, "SELECT completed FROM thread_fork_receipts WHERE source_thread_id=? AND idempotency_key=?", (thread_id, key)).completed)
                    if completed:
                        return await self.get(identity)
                    reference = await adapter.fork_checkpoint(reference, identity)
                    result = await self.rt.store.call(branch, transaction=True)
            finally:
                guard[1] -= 1
                if not guard[1]:
                    self.fork_locks.pop(lock_key, None)
        else:
            result = await self.rt.store.call(branch, transaction=True)
        self.rt.notify()
        return result

    async def conversation(
        self, thread_id, cursor=None, limit=100, *, latest=False, before_seq=None, presentation=False
    ):
        def entry(row):
            value = public(row)
            observed = value.pop("observed_state")
            value["execution_state"] = execution_state(observed, value.pop("observed_error"))
            if presentation and value["role"] == "assistant":
                from .presentation import markdown_summary, delivery_summary
                value["content"] = (delivery_summary(value["content"]) if (value["metadata"].get("acceptance") or {}).get("state") == "FAILED"
                                    else markdown_summary(value.get("summary_markdown") or value["content"]))
            return ConversationEntry.model_validate(value)
        await self.get(thread_id)
        limit = min(200, max(1, int(limit)))
        from .chat_history import conversation_rows
        if latest:
            found = await self.rt.store.call(
                lambda db: conversation_rows(db, thread_id, before=int(before_seq) if before_seq is not None else 9223372036854775807,
                                             descending=True, limit=limit + 1)
            )
            selected = list(reversed(found[:limit]))
            return PageEnvelope(
                items=[entry(r) for r in selected],
                cursor=str(selected[0].seq) if selected else None,
                has_more=len(found) > limit,
            )
        found = await self.rt.store.call(
            lambda db: conversation_rows(db, thread_id, after=int(cursor or 0), limit=limit + 1)
        )
        return PageEnvelope(
            items=[entry(r) for r in found[:limit]],
            cursor=str(found[min(limit, len(found)) - 1].seq) if found else cursor,
            has_more=len(found) > limit,
        )

    async def turn(self, thread_id, content, idempotency_key, **options):
        thread = await self.get(thread_id)
        budget_id = options.pop("budget_id", None)
        payload = {"input": str(content)}
        return await self.rt.tasks.submit(
            TaskSpec(
                session_id=thread.session_id,
                agent_id=thread.agent_id,
                thread_id=thread_id,
                payload=payload,
                idempotency_key=idempotency_key,
                **options,
            ), budget_id=budget_id
        )


class Tasks:
    def __init__(self, rt):
        self.rt = rt

    async def continue_result(self, task_id, idempotency_key, content=""):
        return await self.rt.continuations.begin(task_id, idempotency_key, content)

    async def trace(self, task_id, cursor=None, limit=50, include_resources=False):
        """Bounded execution evidence without loading prompts or complete results."""
        require(type(include_resources) is bool, "INVALID_REQUEST", "include_resources must be boolean")
        limit = min(100, max(1, int(limit)))
        def read(db):
            task = need(db, "tasks", task_id)
            stages = rows(db, "SELECT seq,run_id,payload_json FROM events WHERE task_id=? AND type='run.stage' AND seq>? ORDER BY seq LIMIT ?",
                          (task_id, int(cursor or 0), limit + 1))
            attempts = rows(db, "SELECT id,attempt_no,state,started_at,ended_at,error_json,boundary_receipt_json FROM runs WHERE task_id=? ORDER BY attempt_no", (task_id,))
            tools = rows(db, "SELECT c.id,c.run_id,c.tool_name,c.state,c.created_at,c.updated_at,c.error_json,c.parent_stage_id FROM tool_calls c JOIN runs r ON r.id=c.run_id WHERE r.task_id=? ORDER BY c.created_at,c.id LIMIT ?", (task_id, limit + 1)) if cursor is None else []
            value = {"task_id": task_id, "state": task.state, "queue_reason": task.queue_reason,
                    "queued_ms": max(0, (attempts[0].started_at if attempts else task.updated_at if task.state in TERMINAL else now_ms()) - task.created_at),
                    "items": [{"seq": row.seq, "run_id": row.run_id, **json.loads(row.payload_json)} for row in stages[:limit]],
                    "cursor": str(stages[min(limit, len(stages)) - 1].seq) if stages else cursor,
                    "has_more": len(stages) > limit,
                    "runs": [{"id": run.id, "attempt": run.attempt_no, "state": run.state,
                              "execution_state": execution_state(run.state, json.loads(run.error_json) if run.error_json else None),
                              "started_at": run.started_at, "ended_at": run.ended_at,
                              "duration_ms": run.ended_at - run.started_at if run.ended_at else None,
                              "error": json.loads(run.error_json) if run.error_json else None,
                              "timings": json.loads(run.boundary_receipt_json).get("timings", {}) if run.boundary_receipt_json else {}}
                             for run in attempts],
                    "tools_has_more": len(tools) > limit,
                    "tools": [{"id": call.id, "run_id": call.run_id, "tool": call.tool_name, "state": call.state, "parent_stage_id": call.parent_stage_id,
                               "duration_ms": call.updated_at - call.created_at if call.state not in {"PENDING", "DISPATCHED", "UNCERTAIN"} else None,
                               "error": json.loads(call.error_json) if call.error_json else None} for call in tools[:limit]]}
            if include_resources:
                resources = rows(db, "SELECT c.id,c.run_id,c.tool_name,c.state,c.error_json,x.state AS resource_state FROM mcp_calls c "
                                 "JOIN runs r ON r.id=c.run_id JOIN mcp_call_resources x ON x.call_id=c.id "
                                 "WHERE r.task_id=? ORDER BY c.started_at DESC,c.id DESC LIMIT ?", (task_id, limit + 1))
                value["resource_calls"] = [{"call_id": item.id, "run_id": item.run_id, "tool": item.tool_name,
                    "state": item.state, "resource_state": item.resource_state,
                    "recovery_state": self.rt.mcp.capacity.recovery_state(item.id, item.resource_state),
                    "error": json.loads(item.error_json) if item.error_json else None} for item in resources[:limit]]
                value["resources_has_more"] = len(resources) > limit
            return value
        return await self.rt.store.call(read)

    async def progress(self, task_id):
        from .presentation import result_summary
        task = await self.get(task_id)
        live = [v for v in self.rt.events.summaries.values() if v["task_id"] == task_id]
        return {"task_id": task_id, "state": task.state, "format": "markdown",
                "summary_markdown": result_summary(task.result) if task.result else
                    (live[-1]["summary_markdown"] if live else ""),
                "ephemeral": not bool(task.result)}

    def insert(self, db, spec, identity=None, retry_of=None, caller_run_id=None, *, workflow_run_id=None, budget_id=None, inherited_budget_id=None):
        from .execution_policy import request_hash
        digest = request_hash(spec)
        if budget_id is not None:
            digest = content_hash({"request_hash": digest, "budget_id": budget_id})
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
        session, thread, caller = self.validate_submission(db, spec, identity=identity, caller_run_id=caller_run_id)
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
        if spec.context_refs:
            db.executemany("INSERT OR IGNORE INTO context_access(task_id,reference) VALUES(?,?)", ((identity, reference) for reference in spec.context_refs))
        self.rt.context_merges.assign(db, identity, thread, spec, session.project_id)
        parent_task_id = caller.id if caller_run_id else retry_of
        self.rt.budgets.task(db, identity, explicit=budget_id if budget_id is not None else inherited_budget_id,
                             parent_task_id=parent_task_id, workflow_run_id=workflow_run_id)
        if workflow_run_id is not None:
            from .ownership import own_workflow_task
            own_workflow_task(db, identity, workflow_run_id)
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

    def validate_submission(self, db, spec, *, identity=None, caller_run_id=None):
        """Shared read-only request boundary for submission and admission advice."""
        caller = None
        session = need(db, "sessions", spec.session_id)
        resolve_project(db, session.project_id)
        require(agent_project(db, spec.agent_id) == session.project_id, "PROJECT_MISMATCH",
                "Agent and session must belong to the same project")
        allowed = session.state == "OPEN"
        if caller_run_id is not None:
            caller, run = self.rt.check_run(db, caller_run_id)
            self.rt.check_collaboration(run)
            self.rt.environments.check_delegation(db, caller_run_id, spec.agent_id)
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
        self.rt.workdirs.snapshot(db, thread.id)
        self.rt.contexts.validate_refs(db, spec, session.project_id, caller_run_id)
        require(
            thread.session_id == spec.session_id and thread.agent_id == spec.agent_id,
            "INVALID_REQUEST",
            "Thread, session and agent do not match",
        )
        self.rt.adapter(spec.agent_id)
        from .execution_policy import validate_policies
        validate_policies(self.rt, spec)
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
        return session, thread, caller

    async def submit(self, spec: TaskSpec | dict, *, caller_run_id=None, budget_id=None):
        self.rt.admit()
        spec = TaskSpec.model_validate(spec)
        spec = TaskSpec.model_validate(self.rt.clean_json(spec.model_dump(mode="json")))
        result = await self.rt.store.call(
            lambda db: self.insert(db, spec, caller_run_id=caller_run_id, budget_id=budget_id),
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
            if task.continuation_runs:
                task["continuation"] = self.rt.continuations.snapshot(db, task_id)
            policy = task["spec"]
            if any(policy.get(key, {}).get("required") or policy.get(key, {}).get("mode") == "required"
                   for key in ("tool_policy", "skill_policy")):
                task["execution_evidence"] = [dict(row) for row in rows(db,
                    "SELECT kind,name,run_id,created_at FROM execution_evidence WHERE task_id=? ORDER BY kind,name", (task_id,))]
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
            value = resolution.resolution
            result = resolution.result
            artifacts = self.rt.check_artifacts(db, task.session_id, result.artifact_ids) if result else []
            if value == "SUCCEEDED":
                from .execution_policy import check_completion
                from .result_validation import validate_result
                from .models import ResultPolicy
                spec = TaskSpec.model_validate_json(task.spec_json)
                check_completion(db, task, spec)
                if spec.result_policy != ResultPolicy() or result.metadata.get("model_completion"):
                    result, error = validate_result(result, spec.result_policy, artifacts)
                    if error:
                        raise RuntimeFault(error["code"], error["safe_message"], details=error.get("details", {}))
            result_json = json_dump(result) if result else task.result_json
            error_json = task.error_json if value in {"FAILED", "CANCELLED"} else None
            db.execute(
                "UPDATE tasks SET state=?,result_json=?,error_json=?,queue_reason=?,updated_at=? WHERE id=?",
                (
                    value,
                    result_json,
                    error_json,
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
                        json_dump({**json.loads(run.error_json or "{}"), "evidence": resolution.evidence,
                                   "reconciliation": {"resolution": value, "confirmed_stopped": True}}),
                        run.id,
                    ),
                )
                self.rt.delivery.reset_offers(db, run.id)
                db.execute(
                    "UPDATE tool_calls SET state='RESOLVED',error_json=?,updated_at=? WHERE run_id=? AND state IN ('INTENT','DISPATCHED','UNCERTAIN')",
                    (json_dump({"evidence": resolution.evidence}), now_ms(), run.id),
                )
            if result:
                self.rt.append_reply(
                    db, task, run.id if run else None, result
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

    async def delivery(self, message_id):
        return await self.rt.delivery.get(message_id)

    async def reject(self, message_id, reason, idempotency_key, *, caller_run_id=None):
        return await self.rt.delivery.dispose(message_id, "REJECTED", reason, idempotency_key, caller_run_id=caller_run_id)

    async def expire(self, message_id, expires_at_ms, reason, idempotency_key, *, caller_run_id=None):
        return await self.rt.delivery.dispose(message_id, "EXPIRED", reason, idempotency_key,
                                              expires_at_ms=expires_at_ms, caller_run_id=caller_run_id)

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
