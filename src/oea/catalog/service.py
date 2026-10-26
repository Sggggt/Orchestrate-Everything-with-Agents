"""Project, configuration, session, and instance ownership services."""

import asyncio
import hashlib
import json
import shutil
from pathlib import Path

import apsw

from oea.contracts.configuration import AgentDefinition
from oea.catalog import reading
from oea.contracts.errors import RuntimeFault, require
from oea.storage.persistence import encode, event, new_id, now_ms, one, rows
from oea.storage.project_deletion import delete_project, deletion_receipt


def need(db, table, identity):
    # Table names are service constants, never caller input.
    value = one(db, "SELECT * FROM " + table + " WHERE id=?", (identity,))
    require(value is not None, "NOT_FOUND", "Object does not exist", object_id=identity)
    return value


class Projects:
    def __init__(self, host):
        self.host = host

    @staticmethod
    def view(row):
        return {key: value for key, value in row.items() if key != "archived"}

    async def create(self, name: str, *, folder: Path | None = None, project_id: str | None = None):
        name = name.strip()
        require(0 < len(name) <= 80, "INVALID_REQUEST", "Project name must contain 1 to 80 characters")
        folder = str(Path(folder).expanduser().resolve()) if folder else None
        require(folder is None or Path(folder).is_dir(), "PROJECT_UNAVAILABLE", "Project folder is unavailable")
        identity = project_id or new_id("project")
        def write(db):
            previous = one(db, "SELECT * FROM projects WHERE id=?", (identity,))
            if previous:
                require(previous["name"] == name and previous["folder"] == folder, "IDEMPOTENCY_CONFLICT", "Project ID has different configuration")
                return previous
            db.execute("INSERT INTO projects VALUES(?,?,?,0,?)", (identity, name, folder, now_ms()))
            event(db, "project.created", project_id=identity)
            return need(db, "projects", identity)
        result = await self.host.store.call(write, transaction=True)
        self.host.notify()
        return self.view(result)

    async def get(self, project_id: str):
        return self.view(await self.host.store.call(lambda db: need(db, "projects", project_id)))

    async def update(self, project_id: str, name: str):
        """Rename a project without moving its files or changing ownership."""
        require(0 < len(name.strip()) <= 80, "INVALID_REQUEST", "Project name must contain 1 to 80 characters")
        def write(db):
            need(db, "projects", project_id)
            db.execute("UPDATE projects SET name=? WHERE id=?", (name.strip(), project_id))
            event(db, "project.updated", project_id=project_id)
            return need(db, "projects", project_id)
        value = await self.host.store.call(write, transaction=True)
        self.host.notify()
        return self.view(value)

    async def delete(self, project_id: str, confirm_name: str, idempotency_key: str):
        """Permanently delete idle platform records; retain original files and backups."""
        require(isinstance(idempotency_key, str) and 0 < len(idempotency_key) <= 256 and idempotency_key.strip(),
                "INVALID_REQUEST", "Use a stable project deletion key")
        require(isinstance(confirm_name, str) and 0 < len(confirm_name) <= 80,
                "INVALID_REQUEST", "Confirm the current project name")
        key_hash = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
        input_hash = hashlib.sha256(encode([project_id, confirm_name]).encode("utf-8")).hexdigest()
        previous = await self.host.store.call(lambda db: deletion_receipt(db, project_id, key_hash, input_hash))
        if previous is None:
            try:
                await self.host.assert_project_idle(project_id)
            except RuntimeFault:
                # Another caller may have committed this same request while the
                # host's actual-work check was waiting. Read its receipt once.
                previous = await self.host.store.call(lambda db: deletion_receipt(db, project_id, key_hash, input_hash))
                if previous is None:
                    raise
        if previous is None:
            try:
                value = await self.host.store.call(lambda db: delete_project(db, project_id, confirm_name, key_hash, input_hash, assert_inactive=self.host.assert_project_inactive), transaction=True)
            except apsw.ConstraintError:
                raise RuntimeFault("PROJECT_REFERENCED", "Other platform records still reference this project; its deletion was rolled back") from None
        else:
            value = previous
        exists = await self.host.store.call(lambda db: one(db, "SELECT 1 FROM projects WHERE id=?", (project_id,)))
        if exists is None:
            await self.host.on_project_deleted(project_id)
        self.host.notify()
        return value

    async def list(self, *, cursor: int = 0, limit: int = 25):
        require(type(limit) is int and 1 <= limit <= 100 and type(cursor) is int and 0 <= cursor < 2**63, "INVALID_REQUEST", "Use a bounded page")
        values = await self.host.store.call(lambda db: rows(db, "SELECT rowid AS position,* FROM projects WHERE rowid>? ORDER BY rowid LIMIT ?", (cursor, limit + 1)))
        return {"items": [self.view(row) for row in values[:limit]], "cursor": values[limit - 1]["position"] if len(values) > limit else None}


class Agents:
    def __init__(self, host):
        self.host = host

    async def save(self, definition: AgentDefinition, *, expected_revision: int | None = None):
        definition = AgentDefinition.model_validate(definition)
        workspace = definition.workspace
        absolute = lambda path: path.expanduser().resolve() if path is not None else None
        workspace = workspace.model_copy(update={"project": absolute(workspace.project), "work": absolute(workspace.work),
                                                 "references": tuple(absolute(path) for path in workspace.references)})
        definition = definition.model_copy(update={"workspace": workspace,
            "skills": tuple(skill.model_copy(update={"path": absolute(skill.path)}) for skill in definition.skills)})
        def write(db):
            if definition.project_id:
                need(db, "projects", definition.project_id)
            previous = one(db, "SELECT * FROM agent_definitions WHERE id=?", (definition.id,))
            if previous:
                require(previous["project_id"] == definition.project_id, "PROJECT_MISMATCH", "Agent cannot change its project")
                require(expected_revision is None or expected_revision == previous["revision"], "REVISION_CONFLICT", "Agent configuration changed")
                revision = previous["revision"] + 1
                db.execute("UPDATE agent_definitions SET config_json=?,revision=?,updated_at=? WHERE id=?",
                           (definition.model_dump_json(), revision, now_ms(), definition.id))
            else:
                require(expected_revision in {None, 0}, "REVISION_CONFLICT", "Agent does not have that revision")
                revision = 1
                db.execute("INSERT INTO agent_definitions VALUES(?,?,?,?,?)",
                           (definition.id, definition.project_id, definition.model_dump_json(), revision, now_ms()))
            event(db, "agent.saved", project_id=definition.project_id, data={"agent_id": definition.id, "revision": revision})
            return revision
        revision = await self.host.store.call(write, transaction=True)
        self.host.definitions[definition.id] = definition.model_copy(deep=True)
        self.host.notify()
        return {"definition": definition.model_dump(mode="json"), "revision": revision}

    async def get(self, agent_id: str):
        row = await self.host.store.call(lambda db: need(db, "agent_definitions", agent_id))
        return {"definition": json.loads(row["config_json"]), "revision": row["revision"]}

    async def list(self, project_id: str, *, cursor: int = 0, limit: int = 25):
        require(type(limit) is int and 1 <= limit <= 100 and type(cursor) is int and 0 <= cursor < 2**63, "INVALID_REQUEST", "Use a bounded page")
        def read(db):
            need(db, "projects", project_id)
            return rows(db, "SELECT rowid AS position,* FROM agent_definitions WHERE (project_id=? OR project_id IS NULL) AND rowid>? ORDER BY rowid LIMIT ?",
                        (project_id, cursor, limit + 1))
        values = await self.host.store.call(read)
        return {"items": [{"position": row["position"], "revision": row["revision"], "definition": json.loads(row["config_json"])} for row in values[:limit]],
                "cursor": values[limit - 1]["position"] if len(values) > limit else None}

    async def schema(self):
        return AgentDefinition.model_json_schema()

    async def set_credential(self, agent_id: str, project_id: str, role: str, value: str, expected_revision: int):
        """Write a model secret without returning its value or reusing a live reference."""
        require(role in {"main", "manager"} and len(value) <= 16384, "INVALID_REQUEST", "Choose a model role and a bounded credential")
        saved = await self.get(agent_id)
        definition = AgentDefinition.model_validate(saved["definition"])
        require(definition.project_id in {None, project_id}, "PERMISSION_DENIED", "Agent belongs to another project")
        await self.host.projects.get(project_id)
        require(saved["revision"] == expected_revision, "REVISION_CONFLICT", "Agent configuration changed")
        require(role != "manager" or definition.manager is not None, "MANAGER_UNAVAILABLE", "Configure the manager before assigning its credential")
        reference = None
        if value:
            identity = new_id("credential")
            await asyncio.to_thread(self.host.vault.set, identity, value)
            reference = "vault:" + identity
        model = definition.model if role == "main" else definition.manager.model
        model.credential_ref = reference
        result = await self.save(definition, expected_revision=expected_revision)
        return {"agent_id": agent_id, "role": role, "configured": bool(value), "revision": result["revision"], "credential_ref": reference}

    async def check(self, agent_id: str, *, project_id: str | None = None):
        """Inspect saved capabilities without issuing a model or tool request."""
        from oea.workspace.service import WorkspacePaths
        value = await self.get(agent_id)
        definition = AgentDefinition.model_validate(value["definition"])
        require(project_id is None or definition.project_id in {None, project_id}, "PERMISSION_DENIED", "Agent belongs to another project")
        project_id = project_id or definition.project_id
        project = await self.host.projects.get(project_id) if project_id else None
        workspace = definition.workspace
        if workspace.project is None and project and project["folder"]:
            workspace = workspace.model_copy(update={"project": Path(project["folder"])})
        def model_status(model):
            missing = [field for field in ("base_url", "model") if not getattr(model, field)]
            credential = self.host.secret(model.credential_ref) if model.credential_ref else None
            if model.credential_ref and not credential:
                missing.append("credential")
            return {"state": "needs_configuration" if missing else "configured", "missing": missing,
                    "has_credential": bool(credential), "connection_verified": False}
        unavailable = [name for name in definition.tools.functions if name not in self.host.tools.providers]
        for binding in definition.skills:
            source = self.host.skills.definitions.get(binding.id)
            if binding.path is not None:
                if not (binding.path / "SKILL.md").is_file():
                    unavailable.append("skill:" + binding.id)
            elif not source or not source[0].enabled or project_id is None or source[0].project_id not in {None, project_id}:
                unavailable.append("skill:" + binding.id)
        if "run_command" in definition.tools.local and not self.host.tools.providers["run_command"].commands_enabled:
            unavailable.append("run_command:host_grant")
        for binding in definition.tools.mcp:
            saved = self.host.mcp.configurations.get(binding.server_id)
            if not saved or not saved[0].enabled or project_id is None or saved[0].project_id not in {None, project_id} or (
                binding.tools is not None and saved[0].allowed_tools is not None and not set(binding.tools) <= set(saved[0].allowed_tools)):
                unavailable.append("mcp:" + binding.server_id)
        if not self.host.agent_graphs.available(definition.execution):
            unavailable.append("execution:not_registered")
        if definition.execution.program and not self.host.tools.providers["_oea_program"].enabled:
            unavailable.append("execution:host_grant")
        workspace_check = {"state": "resolved", "preview": True, "issues": []}
        try:
            paths = WorkspacePaths.for_instance(workspace, "new-instance", "new-run", self.host.root)
            workspace_check["paths"] = paths.as_dict()
            workspace_check["issues"] = [{"code": "REFERENCE_UNAVAILABLE", "path": str(path)} for path in paths.reference_dirs if not path.is_dir()]
            if workspace_check["issues"]:
                workspace_check["state"] = "needs_configuration"
        except RuntimeFault as error:
            workspace_check.update(state="needs_configuration", issues=[error.as_dict()])
            paths = None
        executables = []
        for kind, command in (("shell", definition.workspace.shell if "run_command" in definition.tools.local else ()), ("program", definition.execution.program)):
            if command:
                program = Path(command[0])
                if program.is_absolute():
                    resolved = str(program)
                elif program.parent != Path("."):
                    resolved = str(paths.work_dir / program) if paths else None
                else:
                    resolved = shutil.which(command[0], path=definition.workspace.environment.get("PATH"))
                available = bool(resolved and Path(resolved).is_file())
                executables.append({"kind": kind, "program": command[0], "resolved": resolved if available else None, "available": available})
                if not available:
                    unavailable.append(kind + ":program_unavailable")
        return {"agent_id": agent_id, "enabled": definition.enabled, "revision": value["revision"],
                "model": model_status(definition.model),
                "manager": {"model": model_status(definition.manager.model), "business_tools": False} if definition.manager else None,
                "tools": {"local": list(definition.tools.local), "functions": list(definition.tools.functions), "unavailable": unavailable,
                          "details": [{"name": name, "side_effects": self.host.tools.providers[name].describe().side_effects,
                                       "cancellation": self.host.tools.providers[name].describe().cancellation}
                                      for name in (*definition.tools.local, *definition.tools.functions) if name in self.host.tools.providers]},
                "workspace": definition.workspace.model_dump(mode="json"), "workspace_check": workspace_check,
                "execution": definition.execution.graph, "executables": executables,
                "verification": "configuration_only"}


class Sessions:
    def __init__(self, host):
        self.host = host

    def _create(self, db, project_id, name="Session", *, session_id=None):
        name = name.strip()
        require(0 < len(name) <= 80, "INVALID_REQUEST", "Session name must contain 1 to 80 characters")
        identity = session_id or new_id("session")
        need(db, "projects", project_id)
        previous = one(db, "SELECT * FROM sessions WHERE id=?", (identity,))
        if previous:
            require(previous["project_id"] == project_id and previous["name"] == name,
                    "IDEMPOTENCY_CONFLICT", "Session identity has different configuration")
            return previous
        db.execute("INSERT INTO sessions(id,project_id,name,created_at) VALUES(?,?,?,?)", (identity, project_id, name, now_ms()))
        event(db, "session.created", project_id=project_id, session_id=identity)
        return need(db, "sessions", identity)

    async def create(self, project_id: str, name: str = "Session", *, session_id: str | None = None):
        result = await self.host.store.call(lambda db: self._create(db, project_id, name, session_id=session_id), transaction=True)
        self.host.notify()
        return result

    async def get(self, session_id: str):
        return await self.host.store.call(lambda db: need(db, "sessions", session_id))

    async def update(self, session_id: str, project_id: str, name: str):
        """Rename an owned session while retaining its work and conversation."""
        require(0 < len(name.strip()) <= 80, "INVALID_REQUEST", "Session name must contain 1 to 80 characters")
        def write(db):
            session = need(db, "sessions", session_id)
            require(session["project_id"] == project_id, "PERMISSION_DENIED", "Session belongs to another project")
            db.execute("UPDATE sessions SET name=? WHERE id=?", (name.strip(), session_id))
            event(db, "session.updated", project_id=project_id, session_id=session_id)
            return need(db, "sessions", session_id)
        value = await self.host.store.call(write, transaction=True)
        self.host.notify()
        return value

    async def set_archived(self, session_id: str, project_id: str, archived: bool):
        """Archive or restore a session without cancelling existing work."""
        def write(db):
            session = need(db, "sessions", session_id)
            require(session["project_id"] == project_id, "PERMISSION_DENIED", "Session belongs to another project")
            db.execute("UPDATE sessions SET archived=? WHERE id=?", (int(archived), session_id))
            event(db, "session.archived" if archived else "session.restored", project_id=project_id, session_id=session_id)
            return need(db, "sessions", session_id)
        value = await self.host.store.call(write, transaction=True)
        self.host.notify()
        return value

    async def summary(self, session_id: str, project_id: str, *, consumer_id: str | None = None):
        """Read the workbench counts and reader-owned unread totals."""
        consumer = reading.reader(consumer_id, required=False)
        def read(db):
            session = need(db, "sessions", session_id)
            require(session["project_id"] == project_id, "PERMISSION_DENIED", "Session belongs to another project")
            counts = rows(db, "SELECT r.state,COUNT(*) AS count FROM runs r JOIN instances i ON i.id=r.instance_id WHERE i.session_id=? GROUP BY r.state", (session_id,))
            artifacts = one(db, "SELECT COUNT(*) AS count FROM artifacts WHERE session_id=?", (session_id,))["count"]
            return {"session_id": session_id, "project_id": project_id, "runs": {value["state"]: value["count"] for value in counts}, "artifacts": artifacts,
                    "unread": reading.summary(db, session_id, project_id, consumer) if consumer else None}
        return await self.host.store.call(read)

    async def unread(self, session_id: str, project_id: str, *, consumer_id: str | None = None):
        consumer = reading.reader(consumer_id)
        def read(db):
            require(need(db, "sessions", session_id)["project_id"] == project_id, "PERMISSION_DENIED", "Session belongs to another project")
            return reading.summary(db, session_id, project_id, consumer)
        return await self.host.store.call(read)

    async def list(self, project_id: str, *, archived: bool | None = None, cursor: int = 0, limit: int = 25):
        require(archived is None or type(archived) is bool, "INVALID_REQUEST", "Choose an active or archived session page")
        require(type(cursor) is int and 0 <= cursor < 2**63 and type(limit) is int and 1 <= limit <= 100, "INVALID_REQUEST", "Read a bounded session page")
        def read(db):
            need(db, "projects", project_id)
            query = "SELECT rowid AS position,* FROM sessions WHERE project_id=? AND rowid>?"
            params = [project_id, cursor]
            if archived is not None:
                query += " AND archived=?"
                params.append(int(archived))
            return rows(db, query + " ORDER BY rowid LIMIT ?", (*params, limit + 1))
        values = await self.host.store.call(read)
        return {"items": values[:limit], "cursor": values[limit - 1]["position"] if len(values) > limit else None}

    async def set_mode(self, session_id: str, mode: str, expected_epoch: int):
        require(mode in {"direct", "collaborative"}, "INVALID_REQUEST", "Choose direct or collaborative")
        def write(db):
            session = need(db, "sessions", session_id)
            require(session["epoch"] == expected_epoch, "REVISION_CONFLICT", "Session mode changed")
            if session["mode"] != mode:
                db.execute("UPDATE sessions SET mode=?,epoch=epoch+1 WHERE id=?", (mode, session_id))
                event(db, "session.mode_changed", project_id=session["project_id"], session_id=session_id, data={"mode": mode, "epoch": expected_epoch + 1})
            return need(db, "sessions", session_id)
        value = await self.host.store.call(write, transaction=True)
        self.host.notify()
        return value


class Instances:
    def __init__(self, host):
        self.host = host

    def _create(self, db, session_id, agent_id, *, name=None, role="main"):
        require(role in {"main", "manager"}, "INVALID_REQUEST", "Choose main or manager")
        identity = new_id("instance")
        name = identity if name is None else name.strip()
        require(0 < len(name) <= 256, "INVALID_REQUEST", "Instance name must contain 1 to 256 characters")
        session = need(db, "sessions", session_id)
        require(not session["archived"], "SESSION_ARCHIVED", "Restore the session before creating an instance")
        need(db, "projects", session["project_id"])
        current = need(db, "agent_definitions", agent_id)
        definition = AgentDefinition.model_validate_json(current["config_json"])
        require(definition.project_id in {None, session["project_id"]}, "PROJECT_MISMATCH", "Agent belongs to another project")
        require(definition.enabled, "AGENT_DISABLED", "Agent is disabled")
        if role == "manager":
            require(session["mode"] == "collaborative" and definition.manager is not None,
                    "MANAGER_UNAVAILABLE", "Enable collaborative mode and configure this manager")
        previous = one(db, "SELECT * FROM instances WHERE session_id=? AND agent_id=? AND name=? AND role=?", (session_id, agent_id, name, role))
        if previous:
            return previous
        config = definition.model_dump(mode="json")
        if not config["workspace"]["project"]:
            config["workspace"]["project"] = need(db, "projects", session["project_id"])["folder"]
        db.execute("INSERT INTO instances VALUES(?,?,?,?,?,?,?,?,NULL,?)",
                   (identity, session["project_id"], session_id, agent_id, name, role, encode(config), "{}", now_ms()))
        event(db, "instance.created", project_id=session["project_id"], session_id=session_id, instance_id=identity,
              data={"agent_id": agent_id, "role": role})
        return need(db, "instances", identity)

    async def create(self, session_id: str, agent_id: str, *, name: str | None = None, role: str = "main"):
        result = await self.host.store.call(lambda db: self._create(db, session_id, agent_id, name=name, role=role), transaction=True)
        self.host.notify()
        return self.view(result)

    @staticmethod
    def view(row):
        return {key: row[key] for key in ("id", "project_id", "session_id", "agent_id", "name", "role", "active_run_id", "created_at")}

    async def get(self, instance_id: str):
        row = await self.host.store.call(lambda db: need(db, "instances", instance_id))
        return {**self.view(row), "workspace": json.loads(row["workspace_json"])}

    async def history(self, instance_id: str, project_id: str, *, before: int | None = None, limit: int = 25):
        """Read bounded conversation previews; original receipts remain referenced."""
        from oea.context.store import CONVERSATION
        require((before is None or type(before) is int and 0 < before < 2**63) and type(limit) is int and 1 <= limit <= 100, "INVALID_REQUEST", "Read a bounded conversation page")
        def read(db):
            instance = need(db, "instances", instance_id)
            require(instance["project_id"] == project_id, "PERMISSION_DENIED", "Instance belongs to another project")
            query = "WITH transcript AS (" + CONVERSATION + "WHERE c.instance_id=? AND c.role IN ('user','assistant') AND (? IS NULL OR c.position<?)) "
            query += "SELECT t.id,t.position,t.run_id,c.action_id,c.created_at,json_extract(t.body_json,'$.role') AS role,substr(CAST(json_extract(t.body_json,'$.content') AS TEXT),1,8192) AS text,length(CAST(json_extract(t.body_json,'$.content') AS TEXT)) AS characters,json_extract(a.result_json,'$.finish_reason') AS finish_reason,json_extract(a.result_json,'$.content_view.integrity') AS integrity FROM transcript t JOIN conversation c ON c.id=t.id LEFT JOIN actions a ON a.id=c.action_id ORDER BY t.position DESC LIMIT ?"
            return rows(db, query, (instance_id, before, before, limit + 1))
        values = await self.host.store.call(read)
        return {"items": values[:limit], "cursor": values[limit - 1]["position"] if len(values) > limit else None}

    async def mark_read(self, instance_id: str, project_id: str, position: int, *, consumer_id: str | None = None):
        """Advance only this reader's cursor over an actually saved message."""
        consumer = reading.reader(consumer_id)
        require(type(position) is int and 0 <= position < 2**63, "INVALID_REQUEST", "Use a saved conversation position")
        def write(db):
            instance = need(db, "instances", instance_id)
            require(instance["project_id"] == project_id, "PERMISSION_DENIED", "Instance belongs to another project")
            require(position == 0 or one(db, "SELECT 1 FROM conversation WHERE instance_id=? AND position=?", (instance_id, position)),
                    "INVALID_REQUEST", "Read position belongs to another conversation")
            previous = one(db, "SELECT position FROM instance_read_positions WHERE consumer_id=? AND instance_id=?", (consumer, instance_id))
            current = max(position, previous["position"] if previous else 0)
            changed = current > (previous["position"] if previous else 0)
            if changed:
                db.execute("INSERT INTO instance_read_positions VALUES(?,?,?,?) ON CONFLICT(consumer_id,instance_id) DO UPDATE SET position=excluded.position,updated_at=excluded.updated_at", (consumer, instance_id, current, now_ms()))
                event(db, "instance.read", project_id=project_id, session_id=instance["session_id"], instance_id=instance_id,
                      data={"consumer_id": consumer, "position": current})
            return {"instance_id": instance_id, "position": current, "summary": reading.summary(db, instance["session_id"], project_id, consumer)}, changed
        value, changed = await self.host.store.call(write, transaction=True)
        if changed:
            self.host.notify()
        return value

    async def list(self, session_id: str, *, agent_id: str | None = None, cursor: int = 0, limit: int = 25, consumer_id: str | None = None):
        require(type(limit) is int and 1 <= limit <= 100 and type(cursor) is int and 0 <= cursor < 2**63, "INVALID_REQUEST", "Use a bounded page")
        def read(db):
            need(db, "sessions", session_id)
            consumer = reading.reader(consumer_id, required=False)
            query = "SELECT i.rowid AS position,i.*," + (reading.instance_count_sql() if consumer else "NULL") + " AS unread_count FROM instances i WHERE i.session_id=? AND i.rowid>?"
            params = [consumer, session_id, cursor] if consumer else [session_id, cursor]
            if agent_id is not None:
                query += " AND i.agent_id=?"
                params.append(agent_id)
            return rows(db, query + " ORDER BY i.rowid LIMIT ?", (*params, limit + 1))
        values = await self.host.store.call(read)
        return {"items": [{**self.view(row), "position": row["position"], "unread_count": row["unread_count"]} for row in values[:limit]],
                "cursor": values[limit - 1]["position"] if len(values) > limit else None}
