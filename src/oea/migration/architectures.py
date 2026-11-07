"""Offline additive schema transition; earlier checkpoints remain inspectable."""

from contextlib import closing
import hashlib
import json
from pathlib import Path
from secrets import token_urlsafe
import shutil
import sqlite3

from oea.contracts.errors import require
from oea.migration.offline import _vault
from oea.migration.source import file_sha256, inventory_files, verify_files
from oea.storage.persistence import encode
from oea.security.secrets import atomic_write

INITIAL_V3_SCHEMA = "5622b6c8f0d62809640c0f9c6e2131b4845d7976dc0a3fe5a322dd64807827de"


def transition(original, source, destination, apply, check):
    """Copy recognized state without dispatching unverified old graph positions."""
    from oea.migration.upgrade import quote, insert, remap
    files = inventory_files(source)
    vault, secrets = _vault(source)
    require(secrets["available"], "MIGRATION_SECRET_INVALID", "Source vault must be readable")
    script = Path(__file__).parents[1].joinpath("storage/schema-v3.sql").read_text(encoding="utf-8")
    require(original.execute("PRAGMA quick_check").fetchone()[0] == "ok" and not original.execute("PRAGMA foreign_key_check").fetchall(),
            "STATE_INTEGRITY", "Source database integrity needs review")
    report = {"format_version": "3.0", "source_protocol": "3.0", "source_schema_sha256": INITIAL_V3_SCHEMA,
              "applied": False, "changed": False, "ready_for_apply": True, "secrets": secrets, "table_counts": {},
              "review_runs": [], "review_workflows": [], "checkpoint_policy": "archive; no automatic replay",
              "control_credential_policy": "rotate in upgraded target"}
    def paths(value):
        if isinstance(value, dict):
            return {key: paths(child) for key, child in value.items()}
        if isinstance(value, list):
            return [paths(child) for child in value]
        return remap(value, source, destination)
    if apply:
        destination.mkdir(parents=True, exist_ok=False)
    with closing(sqlite3.connect(destination / "oea.sqlite" if apply else ":memory:")) as output:
        output.executescript(script)
        output.execute("BEGIN")
        targets = {row[0] for row in output.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for (table,) in original.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
            if table == "metadata":
                continue
            require(table in targets, "SCHEMA_MISMATCH", "Unknown source table cannot be discarded", details={"table": table})
            count = 0
            for saved in original.execute("SELECT * FROM " + quote(table)):
                row = dict(saved)
                if table in {"agent_definitions", "instances", "runs", "skills", "mcp_servers"}:
                    row["config_json"] = encode(paths(json.loads(row["config_json"])))
                if table in {"instances", "projects"}:
                    row["workspace_json"] = encode(paths(json.loads(row["workspace_json"])))
                if table == "instances":
                    row["active_run_id"] = None
                if table == "projects":
                    row["folder"] = remap(row["folder"], source, destination)
                if table in {"runs", "workflow_runs"}:
                    row["execution_version"] = "3.0-pre-architecture"
                    if row["state"] not in {"completed", "cancelled"}:
                        row.update(state="needs_review", wait_json=None, error_json=encode({"code": "EXECUTION_VERSION_UNSUPPORTED", "safe_message": "Prior graph position is archived; inspect original results before new work"}))
                        report["review_runs" if table == "runs" else "review_workflows"].append(row["id"])
                if table == "actions" and row["state"] in {"intent", "dispatched"}:
                    row.update(state="needs_review", error_json=encode({"code": "OUTCOME_UNCERTAIN", "safe_message": "Inspect the original effect before accepting new work"}))
                if table == "usage" and row["state"] == "reserved":
                    row["state"] = "needs_review"
                if table == "artifacts":
                    path = Path(row["path"])
                    require(path.is_file() and path.stat().st_size == row["bytes"] and file_sha256(path) == row["sha256"], "MIGRATION_ARTIFACT_INVALID", "Retained artifact bytes must match their receipt")
                    row["path"] = remap(row["path"], source, destination)
                insert(output, table, row)
                count += 1
            report["table_counts"][table] = count
        require(not output.execute("PRAGMA foreign_key_check").fetchall(), "MIGRATION_REFERENCE_INVALID", "Converted state has broken references")
        output.row_factory = sqlite3.Row
        for instance in output.execute("SELECT id FROM instances").fetchall():
            previous = output.execute("SELECT * FROM memories WHERE instance_id=? ORDER BY through_position DESC,rowid DESC LIMIT 1", (instance["id"],)).fetchone()
            position = previous["through_position"] if previous else 0
            later = output.execute("SELECT id,position,run_id FROM conversation WHERE instance_id=? AND position>? ORDER BY position DESC LIMIT 1", (instance["id"], position)).fetchone()
            if later:
                locator = {"instance_id": instance["id"], "after": position, "through_position": later["position"], "last_record_id": later["id"]}
                body = (previous["body"] + "\n\n" if previous else "") + "Earlier graph positions are archived. Before related work, use read_history to inspect the entire retained range and original receipts, including unresolved effects.\n" + encode(locator)
                output.execute("INSERT INTO memories VALUES(?,?,?,?,?,?,?)", ("memory_transition_" + hashlib.sha256(instance["id"].encode()).hexdigest()[:32], instance["id"], later["run_id"], later["position"], previous["source_ids_json"] if previous else "[]", body, 0))
        if apply:
            for item in files:
                relative = Path(item["relative_path"])
                if relative == Path("client-token"):
                    continue
                archived = len(relative.parts) == 1 and (relative.name.startswith(("oea.sqlite", "graphs.sqlite")) or relative.name == "migration-report.json")
                copied = destination / "imported/protocol-3-initial" / relative if archived else destination / relative
                copied.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source / relative, copied)
                require(file_sha256(copied) == item["sha256"], "MIGRATION_SOURCE_CHANGED", "Copied bytes must match the snapshot")
            if (source / "client-token").exists() or (source / "client.json").exists():
                atomic_write(destination / "client-token", token_urlsafe(32).encode("utf-8"))
            copied_vault, copied_secrets = _vault(destination)
            require(copied_secrets["available"] and copied_vault == vault, "MIGRATION_SECRET_INVALID", "Copied vault could not be verified")
            output.executemany("INSERT INTO metadata VALUES(?,?)", [("protocol_version", "3.0"), ("schema_sha256", hashlib.sha256(script.encode()).hexdigest()), ("schema_revision", "2"), ("migration_complete", "true")])
            report.update(applied=True, changed=True)
        check()
        verify_files(source, files)
        output.commit()
    if apply:
        (destination / "migration-report.json").write_text(encode(report), encoding="utf-8")
    return report
