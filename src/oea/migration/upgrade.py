"""Protocol-two conversion into an independent protocol-three state directory."""

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3

from oea.budgets.measurement import normalize
from oea.contracts.configuration import AgentDefinition, Budget, PROTOCOL_VERSION
from oea.contracts.errors import RuntimeFault, require
from oea.migration.offline import _vault
from oea.migration.source import file_sha256, inventory_files, offline_source, verify_files
from oea.storage.persistence import encode, now_ms
from oea.workspace.paths import external_path, require_local_state, resolve_data_dir

PRE_READING_SCHEMA = "f1ae954f76e97a33ed398e63efcfd4a9a689e27be0988264efc54be1d23bc59e"
PRE_DELIVERY_SCHEMA = "63eaabe949e6e57f3fb5d024a6a78bbee84c89570b6bdd2a7dfebb9c804bb011"
LATEST_V2_SCHEMA = "801fb5a11b1e183934b768b948afccb5b810fd3da206561d4edfefb2e4b46b64"
TOOL_NAMES = {"read_file": ("exec_command", "write_stdin"), "search_files": ("exec_command", "write_stdin"),
              "write_file": ("apply_patch",), "edit_file": ("apply_patch",), "run_command": ("exec_command", "write_stdin"), "read_image": ("view_image",)}
OLD_BUDGET = {"max_model_calls": 128, "max_input_tokens": 2000000, "max_output_tokens": 262144,
              "max_tool_calls": 1024, "max_elapsed_seconds": 86400, "max_stalled_model_calls": 6}


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def names(values):
    return list(dict.fromkeys(item for value in values for item in TOOL_NAMES.get(value, (value,))))


def budget(value):
    previous = {**OLD_BUDGET, **value}
    previous["max_total_input_tokens"] = previous.pop("max_input_tokens")
    # Preserve an already accepted cap and its historical hard-stop semantics.
    return Budget.model_validate(previous).model_dump(mode="json")


def remap(value, source, target):
    if not isinstance(value, str) or target is None:
        return value
    path = Path(value)
    if path.is_absolute() and path.is_relative_to(source):
        return str(target / path.relative_to(source))
    return value


def configuration(value, source, target):
    value = json.loads(encode(value))
    tools = value.setdefault("tools", {})
    old_tools = tools.get("local", ())
    tools["local"] = names(old_tools)
    workspace = value.setdefault("workspace", {})
    # Existing unrestricted Shell authority stays explicit. File-only bindings
    # require a real isolated backend for the new command capability.
    workspace["sandbox"] = "trusted" if "run_command" in old_tools else "native"
    workspace["network"] = "allow" if "run_command" in old_tools else "deny"
    for field in ("project", "work"):
        if field in workspace:
            workspace[field] = remap(workspace[field], source, target)
    workspace["references"] = [remap(path, source, target) for path in workspace.get("references", ())]
    for skill in value.get("skills", ()):
        if skill.get("path"):
            skill["path"] = remap(skill["path"], source, target)
    execution = value.setdefault("execution", {})
    execution["budget"] = budget(execution.get("budget", {}))
    if execution.get("program"):
        value["enabled"] = False
    manager = value.get("manager")
    if manager is not None:
        # A previously restricted manager does not silently gain business tools.
        manager.update(tools={"local": []}, skills=[], workspace={}, execution={"budget": execution["budget"]})
    return AgentDefinition.model_validate(value).model_dump(mode="json")


def board_id(kind, identity):
    return "board_" + hashlib.sha256(encode([kind, identity]).encode()).hexdigest()[:32]


def insert(db, table, value):
    fields = list(value)
    db.execute("INSERT INTO " + quote(table) + "(" + ",".join(map(quote, fields)) + ") VALUES(" + ",".join("?" for _ in fields) + ")", [value[field] for field in fields])


def upgrade_state(data_dir=None, *, target=None, apply=False):
    """Dry-run a recognized v2 snapshot; apply requires a new target directory.

    Source databases, vaults, checkpoints and files remain unchanged. Old graph
    positions are archived, never dispatched by the new host.
    """
    source = resolve_data_dir(data_dir)
    database = source / "oea.sqlite"
    require(database.is_file(), "STATE_UNAVAILABLE", "Choose an existing v2 state directory")
    destination = external_path(target) if target is not None else None
    if destination:
        require_local_state(destination)
        require(not destination.exists(), "MIGRATION_TARGET_EXISTS", "Conversion requires a new target directory")
        require(not destination.is_relative_to(source) and not source.is_relative_to(destination), "MIGRATION_TARGET_INVALID", "Keep source and target separate")
    require(not apply or destination is not None, "MIGRATION_TARGET_REQUIRED", "Choose a new target directory before applying")
    with offline_source(source) as check, closing(sqlite3.connect(database.as_uri() + "?mode=ro&immutable=1", uri=True)) as original:
        original.row_factory = sqlite3.Row
        metadata = dict(original.execute("SELECT key,value FROM metadata"))
        from oea.migration.architectures import INITIAL_V3_SCHEMA, transition
        if metadata.get("protocol_version") == "3.0" and metadata.get("schema_sha256") == INITIAL_V3_SCHEMA:
            return transition(original, source, destination, apply, check)
        require(metadata.get("protocol_version") == "2.0" and metadata.get("schema_sha256") in {PRE_READING_SCHEMA, PRE_DELIVERY_SCHEMA, LATEST_V2_SCHEMA},
                "SCHEMA_MISMATCH", "This converter accepts only recognized protocol-two snapshots")
        require(original.execute("PRAGMA quick_check").fetchone()[0] == "ok" and not original.execute("PRAGMA foreign_key_check").fetchall(), "STATE_INTEGRITY", "Source database integrity needs review")
        files = inventory_files(source)
        vault, vault_report = _vault(source)
        require(vault_report["available"], "MIGRATION_SECRET_INVALID", "Source vault must be readable by the migrating user")
        source_hash = file_sha256(database)
        script = Path(__file__).parents[1].joinpath("storage/schema-v3.sql").read_text(encoding="utf-8")
        source_tables = {row[0] for row in original.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        report = {"format_version": PROTOCOL_VERSION, "source_protocol": "2.0", "source_sha256": source_hash,
                  "source_schema_sha256": metadata["schema_sha256"], "applied": False, "changed": False, "ready_for_apply": True,
                  "secrets": vault_report, "table_counts": {}, "review_runs": [], "review_workflows": [], "disabled_program_agents": [],
                  "tool_bindings_converted": 0, "source_files": [{key: item[key] for key in ("relative_path", "sha256", "bytes")} for item in files]}
        if apply:
            destination.mkdir(parents=True, exist_ok=False)
        with closing(sqlite3.connect(destination / "oea.sqlite" if apply else ":memory:")) as output:
            output.row_factory = sqlite3.Row
            output.executescript(script)
            output.execute("BEGIN")
            target_tables = {row[0]: {column[1] for column in output.execute("PRAGMA table_info(" + quote(row[0]) + ")")} for row in output.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
            sessions = {row["id"]: dict(row) for row in original.execute("SELECT * FROM sessions")}
            instances = {row["id"]: dict(row) for row in original.execute("SELECT * FROM instances")}
            for session in sessions.values():
                output.execute("INSERT INTO boards VALUES(?,?,?,'session',NULL,'General',?)", (board_id("session", session["id"]), session["project_id"], session["id"], session["created_at"]))
            for instance in instances.values():
                identity = board_id("inbox", instance["id"])
                output.execute("INSERT INTO boards VALUES(?,?,?,'inbox',?,'Inbox',?)", (identity, instance["project_id"], instance["session_id"], instance["id"], instance["created_at"]))
                output.execute("INSERT INTO board_readers VALUES(?,?)", (identity, instance["id"]))
            notification_readers = {}
            for row in original.execute("SELECT message_id,instance_id FROM deliveries"):
                notification_readers.setdefault(row["message_id"], set()).add(row["instance_id"])
            converted_boards = set()
            # Ordinary facts retain their identifiers and content. Specialized
            # tables below convert only fields whose public contract changed.
            for table in source_tables:
                if table in {"metadata", "read_positions", "messages", "deliveries"}:
                    continue
                require(table in target_tables, "SCHEMA_MISMATCH", "Unrecognized source table cannot be discarded", details={"table": table})
                count = 0
                for original_row in original.execute("SELECT * FROM " + quote(table)):
                    row = dict(original_row)
                    if table in {"agent_definitions", "instances", "runs"}:
                        old = json.loads(row["config_json"])
                        converted = configuration(old, source, destination)
                        row["config_json"] = encode(converted)
                        if table == "agent_definitions":
                            row["revision"] += 1
                            report["tool_bindings_converted"] += bool(set(old.get("tools", {}).get("local", ())) & TOOL_NAMES.keys())
                            if old.get("execution", {}).get("program"):
                                report["disabled_program_agents"].append(row["id"])
                        if table == "instances":
                            row["active_run_id"] = None
                            paths = json.loads(row["workspace_json"])
                            row["workspace_json"] = encode({key: [remap(path, source, destination) for path in value] if isinstance(value, list) else remap(value, source, destination) for key, value in paths.items()})
                    if table == "budgets":
                        row["policy_json"] = encode(budget(json.loads(row["policy_json"])))
                    if table in {"runs", "workflow_runs"}:
                        row["execution_version"] = "2.0"
                        if row["state"] not in {"completed", "cancelled"}:
                            row["state"] = "needs_review"
                            row["wait_json"] = None
                            row["error_json"] = encode({"code": "EXECUTION_VERSION_UNSUPPORTED", "safe_message": "Original graph checkpoint is archived; inspect prior outcomes before accepting new work", "source_protocol": "2.0"})
                            report["review_runs" if table == "runs" else "review_workflows"].append(row["id"])
                    if table in {"workflows", "workflow_runs"}:
                        graph = json.loads(row["graph_json"])
                        if "budget" in graph:
                            graph["budget"] = budget(graph["budget"])
                        row["graph_json"] = encode(graph)
                        if table == "workflows":
                            row["revision"] += 1
                    if table == "actions" and row["state"] in {"intent", "dispatched"}:
                        row["state"] = "needs_review"
                        row["error_json"] = encode({"code": "OUTCOME_UNCERTAIN", "safe_message": "Inspect the original invocation before any new external action", "action_id": row["id"]})
                    if table == "usage" and row["state"] == "reserved":
                        row["state"] = "needs_review"
                    if table == "artifacts":
                        artifact_path = Path(row["path"])
                        require(artifact_path.is_file() and artifact_path.stat().st_size == row["bytes"] and file_sha256(artifact_path) == row["sha256"], "MIGRATION_ARTIFACT_INVALID", "Published artifact bytes must match their original receipt", details={"artifact_id": row["id"]})
                        row["path"] = remap(row["path"], source, destination)
                        if row["run_id"] and row["name"].endswith((".stdout.txt", ".stderr.txt")):
                            action = original.execute("SELECT result_json FROM actions WHERE id=? AND run_id=? AND name='run_command'", (row["name"][:-11], row["run_id"])).fetchone()
                            for captured in json.loads(action[0] or "{}").get("output_refs", ()) if action else ():
                                if captured.get("artifact", {}).get("id") == row["id"]:
                                    row["capture_json"] = encode({key: captured.get(key) for key in ("truncated", "observed_bytes")})
                    if table == "result_refs" and json.loads(row["version_json"]).get("mode") != "snapshot":
                        row["version_json"] = encode({**json.loads(row["version_json"]), "mode": "unavailable"})
                    if table in {"skills", "mcp_servers"}:
                        config = json.loads(row["config_json"])
                        for field in ("path", "cwd", "source_directory"):
                            if field in config:
                                config[field] = remap(config[field], source, destination)
                        row["config_json"] = encode(config)
                    if table in {"skill_versions", "skill_loads"}:
                        for field in ("path", "source_path"):
                            if field in row:
                                row[field] = remap(row[field], source, destination)
                    if table == "context_sources" and row["origin"].startswith("tool:"):
                        name = row["origin"][5:]
                        row["origin"] = "tool:" + TOOL_NAMES.get(name, (name,))[0]
                    insert(output, table, {key: value for key, value in row.items() if key in target_tables[table]})
                    count += 1
                report["table_counts"][table] = count
            for original_row in original.execute("SELECT * FROM messages ORDER BY position"):
                row = dict(original_row)
                origin = json.loads(row["source_json"])
                readers = notification_readers.get(row["id"], set()).copy()
                if origin.get("instance_id") in instances:
                    readers.add(origin["instance_id"])
                identity = board_id("imported", [row["session_id"], sorted(readers)])
                if identity not in converted_boards:
                    owner = sorted(readers)[0] if readers else None
                    output.execute("INSERT INTO boards VALUES(?,?,?,'inbox',?,'Imported messages',?)", (identity, row["project_id"], row["session_id"], owner, row["created_at"]))
                    output.executemany("INSERT INTO board_readers VALUES(?,?)", [(identity, reader) for reader in readers])
                    converted_boards.add(identity)
                row["board_id"] = identity
                origin["kind"] = {"controller": "leader", "employee": "instance"}.get(origin.get("kind"), origin.get("kind"))
                origin["historical_purpose"] = row["purpose"]
                row["source_json"] = encode(origin)
                insert(output, "messages", {key: value for key, value in row.items() if key in target_tables["messages"]})
            for row in original.execute("SELECT * FROM deliveries"):
                insert(output, "deliveries", {"message_id": row["message_id"], "instance_id": row["instance_id"], "run_id": row["run_id"], "delivered_at": row["accepted_at"], "read_at": row["applied_at"]})
            if "read_positions" in source_tables:
                for row in original.execute("SELECT * FROM read_positions"):
                    output.execute("INSERT INTO board_read_positions SELECT ?,board_id,MAX(position),? FROM messages WHERE session_id=? AND position<=? GROUP BY board_id", (row["consumer_id"], row["updated_at"], row["session_id"], row["position"]))
            for row in output.execute("SELECT * FROM usage WHERE kind='model'").fetchall():
                raw = {}
                failure = output.execute("SELECT result_json FROM model_failures WHERE id=?", (row["id"],)).fetchone()
                action = output.execute("SELECT result_json FROM actions WHERE id=?", (row["action_id"],)).fetchone() if not failure else None
                saved = json.loads((failure or action)[0] or "{}") if failure or action else {}
                if saved.get("usage_id") == row["id"] or failure:
                    raw = saved.get("usage", {})
                normalized = normalize(raw)
                normalized.update(source="historical_provider" if raw else "historical_unknown")
                output.execute("INSERT INTO usage_measurements(usage_id,profile,raw_json,normalized_json,price_json,cost_micros,cost_state,revision,updated_at) VALUES(?,?,?,?,NULL,NULL,'unknown',1,?)", (row["id"], "openai", encode(raw) if raw else None, encode(normalized), now_ms()))
            report["table_counts"].update(messages=original.execute("SELECT COUNT(*) FROM messages").fetchone()[0], deliveries=original.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0])
            # Existing incomplete tool groups must not become a new logical tool
            # continuation. Seed bounded-by-reference memory, retaining the prior
            # valid body and a locator for every later conversation record.
            for instance in instances.values():
                previous = output.execute("SELECT * FROM memories WHERE instance_id=? ORDER BY through_position DESC,rowid DESC LIMIT 1", (instance["id"],)).fetchone()
                position = previous["through_position"] if previous else 0
                subsequent = output.execute("""WITH later AS (
                    SELECT id,position,run_id,COUNT(*) OVER() AS records,ROW_NUMBER() OVER(ORDER BY position) AS ordinal
                    FROM conversation WHERE instance_id=? AND position>?
                ) SELECT * FROM later WHERE ordinal=1 OR ordinal=records ORDER BY position""", (instance["id"], position)).fetchall()
                if not subsequent:
                    continue
                retained = {"instance_id": instance["id"], "after": position, "through_position": subsequent[-1]["position"],
                            "record_count": subsequent[-1]["records"], "first_record_id": subsequent[0]["id"], "last_record_id": subsequent[-1]["id"]}
                body = (previous["body"] + "\n\n" if previous else "") + "Protocol-two graph checkpoints are archived. Before related work, use read_history to inspect the entire retained range below; it can include completed or unresolved effects. Original records and receipts remain unchanged.\n" + encode(retained)
                output.execute("INSERT INTO memories VALUES(?,?,?,?,?,?,?)", ("memory_migration_" + hashlib.sha256(instance["id"].encode()).hexdigest()[:32], instance["id"], subsequent[-1]["run_id"], subsequent[-1]["position"], previous["source_ids_json"] if previous else "[]", body, now_ms()))
            totals = "SELECT COUNT(*),COALESCE(SUM(input_tokens),0),COALESCE(SUM(output_tokens),0),SUM(cached_input_tokens) FROM usage"
            require(tuple(original.execute(totals).fetchone()) == tuple(output.execute(totals).fetchone()), "MIGRATION_USAGE_MISMATCH", "Historical usage totals must remain unchanged")
            report["usage_totals"] = list(output.execute(totals).fetchone())
            require(not output.execute("PRAGMA foreign_key_check").fetchall(), "MIGRATION_REFERENCE_INVALID", "Converted state has broken references")
            if apply:
                for item in files:
                    relative = Path(item["relative_path"])
                    if len(relative.parts) == 1 and (relative.name.startswith(("oea.sqlite", "graphs.sqlite")) or relative.name == "migration-report.json"):
                        copied = destination / "imported/protocol-2" / relative
                    else:
                        copied = destination / relative
                    copied.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source / relative, copied)
                    require(file_sha256(copied) == item["sha256"], "MIGRATION_SOURCE_CHANGED", "Copied material must match the original snapshot")
                copied_vault, copied_report = _vault(destination)
                require(copied_report["available"] and copied_vault == vault, "MIGRATION_SECRET_INVALID", "Copied vault could not be verified")
                report["secrets"]["copied_verified"] = True
            check()
            verify_files(source, files)
            require(file_sha256(database) == source_hash, "MIGRATION_SOURCE_CHANGED", "Source database changed during conversion")
            if apply:
                output.executemany("INSERT INTO metadata VALUES(?,?)", [("protocol_version", PROTOCOL_VERSION), ("schema_sha256", hashlib.sha256(script.encode()).hexdigest()), ("schema_revision", "1"), ("migration_complete", "true")])
                report.update(applied=True, changed=True)
            output.commit()
        if apply:
            (destination / "migration-report.json").write_text(encode(report), encoding="utf-8")
        return report
