"""Explicit offline upgrades of a recognized v2 state, with a new backup."""

from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3

from oea.contracts.errors import require
from oea.security.lock import WorkspaceLock
from oea.workspace.paths import external_path, resolve_data_dir

PRE_READING_SCHEMA = "f1ae954f76e97a33ed398e63efcfd4a9a689e27be0988264efc54be1d23bc59e"


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
            return {"state": "current", "schema_revision": 2, "changed": False}
        require(previous == PRE_READING_SCHEMA, "SCHEMA_MISMATCH", "This offline upgrade does not recognize the installed schema")
        report = {"state": "ready", "from_sha256": previous, "to_sha256": checksum, "schema_revision": 2,
                  "changes": ["instance_read_positions", "message_source_roles"], "changed": False}
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
                if line.startswith(("CREATE TABLE instance_read_positions(", "CREATE INDEX instance_read_positions_instance ")):
                    connection.execute(line)
            connection.execute("UPDATE messages SET source_json=json_set(source_json,'$.kind','leader') WHERE json_extract(source_json,'$.kind')='controller'")
            connection.execute("UPDATE messages SET source_json=json_set(source_json,'$.kind','instance') WHERE json_extract(source_json,'$.kind')='employee'")
            connection.execute("UPDATE metadata SET value=? WHERE key='schema_sha256'", (checksum,))
            connection.execute("INSERT INTO metadata(key,value) VALUES('schema_revision','2') ON CONFLICT(key) DO UPDATE SET value=excluded.value")
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
