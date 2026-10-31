"""Explicit offline upgrades of a recognized v2 state, with a new backup."""

from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3

from oea.contracts.errors import require
from oea.security.lock import WorkspaceLock
from oea.workspace.paths import external_path, resolve_data_dir

PRE_READING_SCHEMA = "f1ae954f76e97a33ed398e63efcfd4a9a689e27be0988264efc54be1d23bc59e"
PRE_DELIVERY_SCHEMA = "63eaabe949e6e57f3fb5d024a6a78bbee84c89570b6bdd2a7dfebb9c804bb011"

ADDED_COLUMNS = {
    "runs": ("display_json TEXT NOT NULL DEFAULT '{}'",),
    "run_activity": ("blocker_json TEXT", "blocker_signature TEXT", "blocker_repeats INTEGER NOT NULL DEFAULT 0"),
    "run_resumptions": ("execution_limits_json TEXT NOT NULL DEFAULT '{}'",),
    "workflow_resumptions": ("execution_limits_json TEXT NOT NULL DEFAULT '{}'", "failure_json TEXT"),
}


def upgrade_state(data_dir=None, *, backup_file=None, apply=False):
    root = resolve_data_dir(data_dir)
    database = root / "oea.sqlite"
    require(database.is_file(), "STATE_UNAVAILABLE", "Choose an existing v2 state directory")
    script = (Path(__file__).parents[1] / "storage/schema-v2.sql").read_text(encoding="utf-8")
    checksum = hashlib.sha256(script.encode("utf-8")).hexdigest()
    lock = WorkspaceLock(root)
    lock.acquire()
    connection = None
    try:
        connection = sqlite3.connect(database.as_uri() + ("?mode=rw" if apply else "?mode=ro"), uri=True)
        metadata = dict(connection.execute("SELECT key,value FROM metadata"))
        require(metadata.get("protocol_version") == "2.0", "SCHEMA_MISMATCH", "Use the legacy offline converter for this state")
        previous = metadata.get("schema_sha256")
        if previous == checksum:
            return {"state": "current", "schema_revision": 3, "changed": False}
        require(previous in {PRE_READING_SCHEMA, PRE_DELIVERY_SCHEMA}, "SCHEMA_MISMATCH", "This offline upgrade does not recognize the installed schema")
        report = {"state": "ready", "from_sha256": previous, "to_sha256": checksum, "schema_revision": 3,
                  "changes": (["instance_read_positions", "message_source_roles"] if previous == PRE_READING_SCHEMA else []) +
                             ["result_snapshots", "run_tool_state", "blocking_evidence", "resume_execution_limits", "unversioned_result_invalidation"], "changed": False}
        if not apply:
            return report
        require(backup_file is not None, "BACKUP_REQUIRED", "Choose a new backup file before applying this upgrade")
        backup = external_path(backup_file)
        require(not backup.is_relative_to(root), "INVALID_BACKUP", "Keep the upgrade backup outside the live state directory")
        backup.parent.mkdir(parents=True, exist_ok=True)
        with backup.open("xb"):
            pass
        with closing(sqlite3.connect(backup)) as destination:
            connection.backup(destination)
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        try:
            for line in script.splitlines():
                if line.startswith(("CREATE TABLE result_snapshots(", "CREATE INDEX result_snapshots_project ", "CREATE TABLE run_tool_state(")) or previous == PRE_READING_SCHEMA and line.startswith(("CREATE TABLE instance_read_positions(", "CREATE INDEX instance_read_positions_instance ")):
                    connection.execute(line)
            for table, columns in ADDED_COLUMNS.items():
                for column in columns:
                    connection.execute("ALTER TABLE " + table + " ADD COLUMN " + column)
            # Old discovery receipts recorded selection, not a schema version.
            # Retain those candidates; the next projection binds current grants
            # and contracts before offering a schema to the model.
            connection.execute("INSERT INTO run_tool_state(run_id,name,contract_id,loaded,loaded_step) "
                "SELECT run_id,name,NULL,loaded,0 FROM (SELECT run_id,json_extract(result_json,'$.name') AS name,"
                "json_extract(result_json,'$.loaded') AS loaded,ROW_NUMBER() OVER(PARTITION BY run_id,json_extract(result_json,'$.name') ORDER BY rowid DESC) AS ordinal "
                "FROM actions WHERE name='tool_guide' AND state='completed' AND json_type(result_json,'$.loaded') IN ('true','false') AND json_type(result_json,'$.name')='text') WHERE ordinal=1")
            # No response snapshot can be reconstructed from mutable current data.
            connection.execute("UPDATE result_refs SET version_json=json_set(version_json,'$.mode','unavailable') WHERE version_json='{}'")
            connection.execute("UPDATE messages SET source_json=json_set(source_json,'$.kind','leader') WHERE json_extract(source_json,'$.kind')='controller'")
            connection.execute("UPDATE messages SET source_json=json_set(source_json,'$.kind','instance') WHERE json_extract(source_json,'$.kind')='employee'")
            connection.execute("UPDATE metadata SET value=? WHERE key='schema_sha256'", (checksum,))
            connection.execute("INSERT INTO metadata(key,value) VALUES('schema_revision','3') ON CONFLICT(key) DO UPDATE SET value=excluded.value")
            require(not connection.execute("PRAGMA foreign_key_check").fetchall(), "STATE_INTEGRITY", "State references need review before upgrading")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return {**report, "state": "upgraded", "changed": True, "backup_file": str(backup)}
    finally:
        if connection is not None:
            connection.close()
        lock.release()
