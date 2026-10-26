"""Read legacy state offline, preserving facts without restoring its executor."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import sqlite3
import time
from collections import Counter
from pathlib import Path

from pydantic import ValidationError
from cryptography.fernet import InvalidToken

from oea.contracts.configuration import AgentDefinition, Model, PROTOCOL_VERSION, manager_snapshot
from oea.contracts.errors import RuntimeFault, require
from oea.mcp.upstream import MCPServer
from oea.security.secrets import dpapi
from oea.skills.service import SkillSource
from oea.workspace.paths import external_path, require_local_state
from oea.migration.source import file_sha256 as _sha, inventory_files, offline_source, verify_files

TERMINAL = {"SUCCEEDED", "COMPLETED", "FAILED", "CANCELLED", "SKIPPED", "RESOLVED", "RECONCILED", "SETTLED", "RELEASED"}
EXECUTION_TABLES = {"tasks", "runs", "tool_calls", "mcp_calls", "workflow_runs", "workflow_node_runs", "instance_batches", "environment_jobs", "budget_entries"}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _quote(name):
    return '"' + name.replace('"', '""') + '"'


def _cell(value):
    if isinstance(value, bytes):
        return {"$sqlite_blob": base64.b64encode(value).decode("ascii")}
    if isinstance(value, float) and not (-float("inf") < value < float("inf")):
        return {"$sqlite_float": repr(value)}
    return value


def _decode(value, default=None):
    try:
        decoded = json.loads(value) if value else ({} if default is None else default)
        require(isinstance(decoded, dict), "MIGRATION_RECORD_INVALID", "Legacy configuration must be a JSON object")
        return decoded
    except (TypeError, ValueError):
        raise RuntimeFault("MIGRATION_RECORD_INVALID", "Legacy state contains invalid JSON") from None


def _vault(root):
    path = root / "secrets.enc"
    if not path.exists():
        return {}, {"present": False, "available": True, "entry_count": 0}
    try:
        if os.name == "nt":
            decoded = dpapi(path.read_bytes(), decrypt=True)
        else:
            from cryptography.fernet import Fernet
            decoded = Fernet((root / "vault.key").read_bytes()).decrypt(path.read_bytes())
        values = json.loads(decoded)
        require(isinstance(values, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in values.items()),
                "MIGRATION_SECRET_INVALID", "Credential vault has an unsupported shape")
        return values, {"present": True, "available": True, "entry_count": len(values)}
    except (OSError, ValueError, RuntimeFault, InvalidToken):
        return {}, {"present": True, "available": False, "entry_count": None}


class Converter:
    def __init__(self, source, destination, tables, vault, files, report):
        self.source, self.db, self.tables, self.vault = source, destination, tables, vault
        self.files, self.report = files, report
        self.migration_id = "legacy_" + report["source_sha256"][:32]
        self.core = {name: self.read(name) for name in ("projects", "sessions", "agent_configs", "threads", "tasks", "runs", "collaboration_sessions", "collaboration_participants", "task_workbenches", "thread_workdirs")}
        self.projects = {r["id"]: r for r in self.core["projects"]}
        self.sessions = {r["id"]: r for r in self.core["sessions"]}
        self.threads = {r["id"]: r for r in self.core["threads"]}
        self.tasks = {r["id"]: r for r in self.core["tasks"]}
        self.runs = {r["id"]: r for r in self.core["runs"]}
        self.definitions = {}
        self.related = {}
        for table in ("messages", "collaboration_messages", "workflow_runs", "workflows", "execution_budgets"):
            for row in self.read(table):
                if "id" in row:
                    self.related[(table, row["id"])] = row
        if "checkpoints" in tables:
            for row in source.execute("SELECT id,thread_id FROM checkpoints"):
                self.related[("checkpoints", row["id"])] = dict(row)
        self.agent_projects = {row["id"]: _decode(row["config_json"]).get("project_id") for row in self.core["agent_configs"]}
        self.mapped = Counter()
        self.issues = report["issues"]
        self.missing_secrets = set()

    def read(self, table):
        return [dict(row) for row in self.source.execute("SELECT * FROM " + _quote(table))] if table in self.tables else []

    def issue(self, code, table, identity=None, *, blocking=False):
        item = {"code": code, "table": table, "blocking": blocking}
        if identity is not None:
            item["id"] = identity
        self.issues.append(item)

    def link(self, table, source_id, target_type, target_id):
        self.db.execute("INSERT INTO imported_links VALUES(?,?,?,?,?)", (self.migration_id, table, str(source_id), target_type, target_id))
        self.mapped[target_type] += 1

    def scope(self, row, seen=frozenset()):
        thread_id = row.get("thread_id") or row.get("target_thread_id")
        task = self.tasks.get(row.get("task_id"))
        run = self.runs.get(row.get("run_id") or row.get("created_by_run_id"))
        if not task and run:
            task = self.tasks.get(run.get("task_id"))
        if not thread_id and task:
            thread_id = task.get("thread_id")
        thread = self.threads.get(thread_id, {})
        session_id = row.get("session_id") or thread.get("session_id") or (task or {}).get("session_id")
        project_id = row.get("project_id") or self.sessions.get(session_id, {}).get("project_id")
        if project_id is None:
            project_id = self.agent_projects.get(row.get("agent_id"))
        if project_id is None:
            for field, table in (("group_message_id", "collaboration_messages"), ("message_id", "messages"),
                                 ("workflow_run_id", "workflow_runs"), ("run_id", "workflow_runs"),
                                 ("workflow_id", "workflows"), ("budget_id", "execution_budgets"), ("checkpoint_id", "checkpoints")):
                related = self.related.get((table, row.get(field)))
                if related and id(related) not in seen and related is not row:
                    project_id, related_session, related_thread = self.scope(related, seen | {id(row)})
                    session_id = session_id or related_session
                    thread_id = thread_id or related_thread
                    if project_id is not None:
                        break
        return project_id, session_id, thread_id

    def model(self, old, secret_key):
        values = {name: old[name] for name in Model.model_fields if name in old and name != "credential_ref"}
        if values.get("max_output_tokens") is None:
            values["max_output_tokens"] = 4096
        if secret_key in self.vault:
            values["credential_ref"] = "vault:" + secret_key
        return values

    def configurations(self):
        for row in self.core["projects"]:
            self.db.execute("INSERT INTO projects VALUES(?,?,?,?,?)", (row["id"], row["name"], row.get("local_folder") or None,
                int(row.get("state") != "OPEN"), row["created_at"]))
            self.link("projects", row["id"], "project", row["id"])
        modes = {row["session_id"]: row for row in self.core["collaboration_sessions"]}
        for row in self.core["sessions"]:
            if row.get("project_id") not in self.projects:
                self.issue("SESSION_PROJECT_UNMAPPED", "sessions", row["id"])
                continue
            mode = modes.get(row["id"], {})
            self.db.execute("INSERT INTO sessions(id,project_id,name,mode,epoch,created_at,archived) VALUES(?,?,?,?,?,?,?)",
                (row["id"], row["project_id"], row.get("title") or row["id"], mode.get("mode", "direct"), mode.get("epoch", 0),
                 row["created_at"], int(row.get("state") != "OPEN")))
            self.link("sessions", row["id"], "session", row["id"])
        for row in self.core["agent_configs"]:
            old = _decode(row["config_json"])
            workspace = {}
            workbench = old.get("workbench") or {}
            # Environment programs and legacy process transports require explicit v2 review.
            compatible = old.get("kind", "llm") == "llm" and not workbench
            tools = {"local": ["run_command" if name == "exec_command" else name for name in workbench.get("tools", [])],
                     "mcp": [{"server_id": item["server_id"], "tools": item.get("tools")} for item in old.get("mcp_bindings", [])]}
            value = {"id": row["id"], "project_id": old.get("project_id"), "name": old.get("name") or row["id"],
                "enabled": bool(old.get("enabled", True) and compatible), "description": old.get("card_description", ""),
                "model": self.model(old, row["id"]), "instructions": {"system": old.get("system_prompt", ""), "agent_md": old.get("agent_md", "")},
                "tools": tools, "skills": [{"id": item["skill_id"]} for item in old.get("skill_bindings", [])],
                "context": {"input_tokens": old.get("context_budget_tokens", 32768)}, "workspace": workspace,
                "communication": {"departments": old.get("communication") or [], "can_request": bool(old.get("communication"))}}
            if old.get("manager_model") is not None:
                value["manager"] = {"model": self.model(old["manager_model"], "manager:" + row["id"]),
                    "context": {"input_tokens": old["manager_model"].get("context_budget_tokens", 32768)},
                    "communication": value["communication"]}
            if old.get("project_id") not in self.projects and old.get("project_id") is not None:
                self.issue("AGENT_PROJECT_UNMAPPED", "agent_configs", row["id"])
                continue
            try:
                definition = AgentDefinition.model_validate(value)
            except ValidationError:
                self.issue("AGENT_CONFIG_REQUIRES_REVIEW", "agent_configs", row["id"])
                continue
            if not compatible:
                self.issue("LEGACY_EXECUTION_REQUIRES_REVIEW", "agent_configs", row["id"])
            if old.get("skills"):
                definition.enabled = False
                self.issue("INLINE_SKILLS_REQUIRE_REVIEW", "agent_configs", row["id"])
            self.definitions[row["id"]] = definition.model_dump(mode="json")
            self.db.execute("INSERT INTO agent_definitions VALUES(?,?,?,?,?)", (row["id"], definition.project_id, definition.model_dump_json(), 1, row["updated_at"]))
            self.link("agent_configs", row["id"], "agent", row["id"])
        for row in self.read("mcp_servers"):
            old, credentials = _decode(row["config_json"]), _decode(row.get("credentials_json"))
            value = {name: old[name] for name in MCPServer.model_fields if name in old}
            value.update(command=([old["command"], *old.get("args", [])] if isinstance(old.get("command"), str) and old["command"] else old.get("command") or []))
            if value.get("transport") == "streamable-http":
                value["transport"] = "streamable_http"
            for field in ("cwd", "source_directory"):
                if not value.get(field):
                    value[field] = None
            for group, target in (("env", "environment_refs"), ("headers", "header_refs")):
                value[target] = {key: "vault:" + reference for key, reference in credentials.get(group, {}).items()}
                self.missing_secrets.update(reference for reference in credentials.get(group, {}).values() if not self.vault.get(reference))
            try:
                config = MCPServer.model_validate(value)
            except ValidationError:
                self.issue("MCP_CONFIG_REQUIRES_REVIEW", "mcp_servers", row["id"])
                continue
            self.db.execute("INSERT INTO mcp_servers VALUES(?,?,?,?,?)", (row["id"], config.project_id, config.model_dump_json(), row.get("revision", 1), row["updated_at"]))
            self.link("mcp_servers", row["id"], "mcp_server", row["id"])
        for row in self.read("skill_registry"):
            old = _decode(row["skill_json"])
            try:
                config = SkillSource(id=row["id"], path=row["directory"], name=old.get("name", ""), description=old.get("description", ""), enabled=bool(row["enabled"]))
            except ValidationError:
                self.issue("SKILL_CONFIG_REQUIRES_REVIEW", "skill_registry", row["id"])
                continue
            body = old.get("content", "")
            fingerprint = hashlib.sha256(body.encode("utf-8")).hexdigest()
            self.db.execute("INSERT INTO skills VALUES(?,?,?,?,0,?)", (row["id"], None, config.model_dump_json(), 1, row["updated_at"]))
            self.db.execute("INSERT OR IGNORE INTO skill_bodies VALUES(?,?)", (fingerprint, body))
            self.db.execute("INSERT INTO skill_versions VALUES(?,?,?,?,?,?,?)", (row["id"], 1, row["directory"], fingerprint, config.name, config.description, row["updated_at"]))
            self.link("skill_registry", row["id"], "skill", row["id"])

    def instances(self):
        roles = {row["thread_id"]: row["kind"] for row in self.core["collaboration_participants"] if row.get("thread_id")}
        workspaces = {}
        for item in sorted(self.core["task_workbenches"], key=lambda item: (self.tasks.get(item["task_id"], {}).get("created_at", 0), item["task_id"])):
            task = self.tasks.get(item["task_id"], {})
            snapshot = _decode(item["snapshot_json"])
            if task.get("thread_id"):
                workspaces[task["thread_id"]] = {"work": snapshot.get("directory"), "project": snapshot.get("project_folder")}
        for item in self.core["thread_workdirs"]:
            workspaces.setdefault(item["thread_id"], {})["work"] = item["path"]
        for row in self.core["threads"]:
            session = self.sessions.get(row["session_id"], {})
            definition = self.definitions.get(row["agent_id"])
            if not definition or session.get("project_id") not in self.projects:
                self.issue("INSTANCE_OWNER_UNMAPPED", "threads", row["id"])
                continue
            role = "manager" if roles.get(row["id"]) == "manager" else "main"
            if role == "manager" and not definition.get("manager"):
                self.issue("MANAGER_CONFIG_UNMAPPED", "threads", row["id"])
                continue
            config = {**definition, "workspace": {**definition["workspace"], **workspaces.get(row["id"], {})}}
            config["workspace"]["project"] = config["workspace"].get("project") or self.projects[session["project_id"]].get("local_folder") or None
            config = AgentDefinition.model_validate(config)
            if role == "manager":
                config = manager_snapshot(config)
            binding = {}
            if workspaces.get(row["id"], {}).get("work"):
                binding = {"work_dir": workspaces[row["id"]]["work"]}
            # Legacy titles need not be unique; immutable IDs are stable instance names.
            self.db.execute("INSERT INTO instances VALUES(?,?,?,?,?,?,?,?,NULL,?)", (row["id"], session["project_id"], row["session_id"], row["agent_id"], row["id"], role, config.model_dump_json(), _json(binding), row["created_at"]))
            self.link("threads", row["id"], "instance", row["id"])

    def messages(self):
        mapped = set()
        for table in ("messages", "collaboration_messages"):
            for row in self.read(table):
                session = self.sessions.get(row["session_id"], {})
                if session.get("project_id") not in self.projects:
                    self.issue("MESSAGE_OWNER_UNMAPPED", table, row["id"])
                    continue
                if row["id"] in mapped:
                    self.issue("MESSAGE_ID_COLLISION", table, row["id"])
                    continue
                envelope = _decode(row.get("envelope_json"))
                payload = envelope.get("payload", {})
                body = row.get("content")
                if body is None:
                    body = payload.get("content", payload.get("text")) if isinstance(payload, dict) else payload
                    if not isinstance(body, str):
                        body = _json(payload)
                source = {"kind": "import", "history_only": True, "legacy_table": table,
                          "legacy_source": row.get("source_id") or row.get("source_key"), "legacy_purpose": row.get("kind") or envelope.get("message_type")}
                purpose = "report" if row.get("kind") == "report" else "inform"
                key = "import:" + table + ":" + row["id"]
                self.db.execute("INSERT INTO messages(id,project_id,session_id,source_json,purpose,body,idempotency_key,input_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (row["id"], session["project_id"], row["session_id"], _json(source), purpose, body, key, hashlib.sha256(body.encode("utf-8")).hexdigest(), row["created_at"]))
                self.link(table, row["id"], "message", row["id"])
                mapped.add(row["id"])
        # Exact, possibly non-contiguous old reads remain in the history archive.
        # Do not fabricate a v2 cursor or delivery that would hide unread items.

    def artifacts(self, source_root, target_root):
        inventory = {item["relative_path"]: item for item in self.files}
        total, verified, byte_count, issues = 0, 0, 0, []
        for row in self.read("artifacts"):
            total += 1
            relative = Path(row["relative_path"])
            original = (source_root / "artifacts" / relative).resolve()
            if not original.is_relative_to(source_root / "artifacts"):
                issues.append({"id": row["id"], "code": "ARTIFACT_PATH_INVALID"})
                continue
            material = inventory.get(original.relative_to(source_root).as_posix())
            if not material or material["sha256"] != row["sha256"] or material["bytes"] != row["size"]:
                issues.append({"id": row["id"], "code": "ARTIFACT_CONTENT_MISMATCH"})
                continue
            verified += 1
            byte_count += row["size"]
            session = self.sessions.get(row["session_id"], {})
            if session.get("project_id") not in self.projects:
                self.issue("ARTIFACT_OWNER_UNMAPPED", "artifacts", row["id"])
                continue
            path = target_root / "artifacts" / row["sha256"] if target_root else Path("artifacts") / row["sha256"]
            if target_root and not path.exists():
                path.parent.mkdir(exist_ok=True)
                shutil.copyfile(original, path)
                require(_sha(path) == row["sha256"], "MIGRATION_COPY_FAILED", "Copied artifact content changed")
            self.db.execute("INSERT INTO artifacts VALUES(?,?,NULL,NULL,?,?,?,?,?,?,?,'import')",
                (row["id"], session["project_id"], row["name"], str(path), row["sha256"], row["size"], row["media_type"], row["created_at"], row["session_id"]))
            self.link("artifacts", row["id"], "artifact", row["id"])
        self.report["artifacts"] = {"total": total, "verified": verified, "bytes": byte_count, "issues": issues}
        self.issues.extend({**item, "table": "artifacts", "blocking": True} for item in issues)

    def archive(self):
        review_counts, usage = Counter(), Counter()
        usage_by_budget, usage_by_state = {}, {}
        activity_reads = {(r["thread_id"], r["source_id"]): r.get("read_at") for r in self.read("thread_activity_items") if r["kind"] == "conversation"}
        for table, ddl in self.tables.items():
            columns = list(self.source.execute("PRAGMA table_info(" + _quote(table) + ")"))
            primary = [row["name"] for row in sorted(columns, key=lambda row: row["pk"]) if row["pk"]]
            digest, count = hashlib.sha256(), 0
            order = " ORDER BY " + ",".join(_quote(name) for name in primary) if primary else ""
            for row in self.source.execute("SELECT * FROM " + _quote(table) + order):
                value = dict(row)
                raw = _json({key: _cell(item) for key, item in value.items()})
                key = _json([_cell(value[name]) for name in primary]) if primary else str(count)
                sha = hashlib.sha256(raw.encode("utf-8")).hexdigest()
                digest.update((key + "\0" + sha + "\n").encode("utf-8"))
                scope = self.scope(value)
                if table == "projects":
                    scope = value["id"], None, None
                elif table == "agent_configs":
                    scope = self.agent_projects[value["id"]], None, None
                elif table == "threads":
                    scope = scope[0], scope[1], value["id"]
                review = table in EXECUTION_TABLES and "state" in value and str(value["state"]).upper() not in TERMINAL
                if table == "workflows":
                    review = True
                    self.issue("WORKFLOW_DEFINITION_REQUIRES_REVIEW", table, value["id"])
                review_counts[table] += int(review)
                self.db.execute("INSERT INTO imported_history(migration_id,table_name,row_key,row_json,sha256,project_id,session_id,instance_id,review_state,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (self.migration_id, table, key, raw, sha, *scope, "needs_review" if review else "history", value.get("created_at", value.get("started_at", 0)) or 0))
                position = self.db.execute("SELECT last_insert_rowid()").fetchone()[0]
                if table == "conversation":
                    self.db.execute("INSERT INTO imported_transcript VALUES(?,?,?,?,?,?,?)", (position, value["session_id"], value["thread_id"], value["seq"], value["role"], value["created_at"], activity_reads.get((value["thread_id"], value["id"]))))
                if table == "budget_entries":
                    totals = {"input_tokens": value.get("input_tokens", 0), "output_tokens": value.get("output_tokens", 0),
                              "model_calls": int(value.get("kind") == "model"), "tool_calls": int(value.get("kind") == "tool")}
                    require(all(type(amount) is int and amount >= 0 for amount in totals.values()),
                            "MIGRATION_USAGE_INVALID", "Legacy token usage must be nonnegative integers")
                    usage.update(totals)
                    usage_by_state.setdefault(value.get("state", ""), Counter()).update(totals)
                    if value.get("budget_id") is not None:
                        usage_by_budget.setdefault(value["budget_id"], Counter()).update(totals)
                    usage["entries"] += 1
                    usage["state:" + value.get("state", "")] += 1
                    usage["kind:" + value.get("kind", "")] += 1
                count += 1
            self.db.execute("INSERT INTO imported_tables VALUES(?,?,?,?,?)", (self.migration_id, table, ddl, count, digest.hexdigest()))
        self.report["needs_review"] = {"count": sum(review_counts.values()), "by_table": {key: value for key, value in review_counts.items() if value}}
        cumulative, mismatched, matched = Counter(), [], 0
        budgets = {identity: value for (table, identity), value in self.related.items() if table == "execution_budgets"}
        fields = ("model_calls", "tool_calls", "input_tokens", "output_tokens")
        for identity, budget in budgets.items():
            expected = {field: budget.get(field, 0) for field in fields}
            actual = {field: usage_by_budget.get(identity, {}).get(field, 0) for field in fields}
            cumulative.update(expected)
            if expected != actual:
                mismatched.append({"budget_id": identity, "cumulative": expected, "ledger": actual})
                self.issue("USAGE_TOTAL_MISMATCH", "execution_budgets", identity, blocking=True)
            else:
                matched += 1
        unknown = sorted(set(usage_by_budget) - set(budgets))
        for identity in unknown:
            self.issue("USAGE_BUDGET_UNMAPPED", "budget_entries", identity, blocking=True)
        self.report["usage"] = {**{field: 0 for field in (*fields, "entries")}, **dict(usage),
            "by_state": [{"state": state, **dict(totals)} for state, totals in sorted(usage_by_state.items())],
            "cumulative": dict(cumulative) if budgets else None,
            "reconciliation": {"available": bool(budgets), "matched": matched, "mismatched": mismatched, "unmapped": unknown}}
        self.report["mapped_counts"] = dict(self.mapped)
        actual_counts = {name: count for name, count in self.db.execute("SELECT table_name,COUNT(*) FROM imported_history GROUP BY table_name")}
        self.report["archive_counts"] = {name: actual_counts.get(name, 0) for name in self.tables}
        require(self.report["archive_counts"] == self.report["table_counts"], "MIGRATION_COUNT_MISMATCH", "Archived row counts differ from the legacy source")
        self.report["table_sha256"] = {name: sha for name, sha in self.db.execute("SELECT name,sha256 FROM imported_tables WHERE migration_id=?", (self.migration_id,))}
        self.report["history_only_tables"] = sorted(set(self.tables) - {"projects", "sessions", "agent_configs", "threads", "messages", "collaboration_messages", "artifacts", "mcp_servers", "skill_registry"})


def migrate_legacy(source: str | Path, target: str | Path | None = None, *, apply: bool = False) -> dict:
    """Inspect schema 29 by default; apply only into a previously absent directory.

    Stop the old host and checkpoint SQLite before calling this function. Active locks
    and nonempty SQLite journals are rejected rather than omitted from the snapshot. Source files are never
    created, modified, moved, or deleted. A failed new target is retained for review.
    """
    source = Path(source).expanduser().resolve()
    require(source.is_dir() and (source / "runtime.sqlite").is_file(), "MIGRATION_SOURCE_INVALID", "Choose a legacy state directory containing runtime.sqlite")
    source_db = source / "runtime.sqlite"
    destination = external_path(target) if target is not None else None
    if destination:
        require_local_state(destination)
        require(not destination.exists(), "MIGRATION_TARGET_EXISTS", "Migration requires a new target directory")
        require(not destination.is_relative_to(source) and not source.is_relative_to(destination), "MIGRATION_TARGET_INVALID", "Source and target directories must be separate")
    require(not apply or destination is not None, "MIGRATION_TARGET_REQUIRED", "Apply requires an explicit new target directory")
    with offline_source(source) as source_check:
        return _convert(source, source_db, destination, apply, source_check)


def _convert(source, source_db, destination, apply, source_check):
    source_hash = _sha(source_db)
    original = sqlite3.connect(source_db.as_uri() + "?mode=ro&immutable=1", uri=True)
    original.row_factory = sqlite3.Row
    output = None
    try:
        try:
            integrity = original.execute("PRAGMA quick_check").fetchone()[0]
        except sqlite3.DatabaseError:
            raise RuntimeFault("MIGRATION_SOURCE_CORRUPT", "Legacy database integrity check failed") from None
        require(integrity == "ok", "MIGRATION_SOURCE_CORRUPT", "Legacy database integrity check failed")
        tables = {row["name"]: row["sql"] for row in original.execute("SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")}
        require("schema_migrations" in tables, "MIGRATION_VERSION_UNSUPPORTED", "Source is not a supported legacy database")
        version = original.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        require(version == 29, "MIGRATION_VERSION_UNSUPPORTED", "Only legacy schema 29 is supported")
        foreign_keys = [list(row) for row in original.execute("PRAGMA foreign_key_check")]
        require(not foreign_keys, "MIGRATION_REFERENCE_INVALID", "Legacy database has broken foreign-key references", details={"count": len(foreign_keys)})
        files = inventory_files(source)
        vault, vault_report = _vault(source)
        report = {"format_version": PROTOCOL_VERSION, "mode": "apply" if apply else "dry-run", "applied": False,
            "source_schema_version": version, "source_sha256": source_hash, "table_counts": {name: original.execute("SELECT COUNT(*) FROM " + _quote(name)).fetchone()[0] for name in tables},
            "secrets": vault_report, "files": {"count": len(files), "bytes": sum(item["bytes"] for item in files)}, "issues": []}
        if not vault_report["available"]:
            report["issues"].append({"code": "SECRET_UNAVAILABLE", "table": "secrets", "blocking": True})
        if apply:
            destination.mkdir(mode=0o700, parents=True, exist_ok=False)
            (destination / "imported" / "source-files").mkdir(parents=True)
            shutil.copyfile(source_db, destination / "imported" / "legacy.sqlite")
            require(_sha(destination / "imported" / "legacy.sqlite") == source_hash, "MIGRATION_SOURCE_CHANGED", "Legacy database changed while copying")
        output = sqlite3.connect(destination / "oea.sqlite" if apply else ":memory:")
        output.execute("PRAGMA foreign_keys=ON")
        script = Path(__file__).parents[1].joinpath("storage/schema-v2.sql").read_text(encoding="utf-8")
        output.executescript(script)
        output.execute("BEGIN")
        converter = Converter(original, output, tables, vault, files, report)
        output.execute("INSERT INTO imported_migrations VALUES(?,?,?,?,?)", (converter.migration_id, source_hash, version, time.time_ns() // 1_000_000, "{}"))
        converter.configurations()
        converter.instances()
        converter.messages()
        converter.artifacts(source, destination if apply else None)
        converter.archive()
        report["secrets"]["missing_references"] = len(converter.missing_secrets)
        if converter.missing_secrets:
            converter.issue("SECRET_REFERENCE_MISSING", "mcp_servers", blocking=True)
        directories = []
        def directory(kind, identity, path):
            if path:
                directories.append({"kind": kind, "id": identity, "path": str(path), "available": Path(path).is_dir()})
        for row in converter.core["projects"]:
            directory("project", row["id"], row.get("local_folder"))
        for row in converter.core["task_workbenches"]:
            snapshot = _decode(row["snapshot_json"])
            directory("task_work", row["task_id"], snapshot.get("directory"))
            directory("task_project", row["task_id"], snapshot.get("project_folder"))
            for name, path in snapshot.get("environment", {}).get("resources", {}).items():
                directory("task_reference:" + name, row["task_id"], path)
        for row in converter.core["thread_workdirs"]:
            directory("instance_work", row["thread_id"], row["path"])
        for row in converter.read("skill_registry"):
            directory("skill", row["id"], row["directory"])
        for row in converter.read("mcp_servers"):
            config = _decode(row["config_json"])
            for field in ("cwd", "source_directory"):
                directory("mcp_" + field, row["id"], config.get(field))
        unique = {item["path"] for item in directories}
        report["directories"] = {"references": len(directories), "unique": len(unique),
            "available": sum(Path(path).is_dir() for path in unique), "moved": 0, "copied": 0, "items": directories}
        for item in files:
            output.execute("INSERT INTO imported_files VALUES(?,?,?,?)", (converter.migration_id, item["relative_path"], item["sha256"], item["bytes"]))
            if apply:
                material = source / item["relative_path"]
                copied = destination / "imported/source-files" / item["relative_path"]
                copied.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(material, copied)
                require(_sha(copied) == item["sha256"], "MIGRATION_SOURCE_CHANGED", "Legacy state material changed while copying")
                if item["relative_path"] in {"secrets.enc", "vault.key"}:
                    shutil.copyfile(copied, destination / item["relative_path"])
                    if os.name != "nt":
                        os.chmod(destination / item["relative_path"], 0o600)
        source_check()
        verify_files(source, files)
        require(_sha(source_db) == source_hash, "MIGRATION_SOURCE_CHANGED", "Legacy database changed during offline migration")
        require(not list(output.execute("PRAGMA foreign_key_check")), "MIGRATION_REFERENCE_INVALID", "Converted database has broken references")
        report["reference_check"] = "ok"
        report["ready_for_apply"] = not any(item.get("blocking") for item in report["issues"])
        if apply:
            if not report["ready_for_apply"]:
                output.execute("UPDATE imported_migrations SET report_json=? WHERE id=?", (_json(report), converter.migration_id))
                output.commit()
                (destination / "migration-report.json").write_text(_json(report), encoding="utf-8")
                raise RuntimeFault("MIGRATION_REVIEW_REQUIRED", "Blocking source issues prevent publishing the new state", details={"issue_count": len(report["issues"])})
            copied_vault, copied_vault_report = _vault(destination)
            require(copied_vault_report["available"] and copied_vault == vault, "MIGRATION_SECRET_INVALID", "Copied credential vault cannot be verified")
            report["secrets"]["copied_verified"] = True
            report["applied"] = True
            output.execute("INSERT INTO metadata VALUES('protocol_version',?)", (PROTOCOL_VERSION,))
            output.execute("INSERT INTO metadata VALUES('schema_sha256',?)", (hashlib.sha256(script.encode("utf-8")).hexdigest(),))
            output.execute("INSERT INTO metadata VALUES('migration_complete','true')")
        output.execute("UPDATE imported_migrations SET report_json=? WHERE id=?", (_json(report), converter.migration_id))
        output.commit()
        if apply:
            (destination / "migration-report.json").write_text(_json(report), encoding="utf-8")
        return report
    finally:
        if output is not None:
            output.close()
        original.close()
