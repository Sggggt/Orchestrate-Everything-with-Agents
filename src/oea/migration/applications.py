"""Offline additive OAP upgrade, retaining graph positions and directory bindings."""

from contextlib import closing
import hashlib
import secrets
from pathlib import Path
import shutil
import sqlite3

from oea.contracts.errors import require
from oea.migration.offline import _vault
from oea.migration.source import file_sha256, inventory_files, verify_files
from oea.storage.persistence import encode
from oea.security.secrets import atomic_write


def transition(original, source, destination, apply, check):
    """Copy facts and checkpoints unchanged, then add application ownership tables."""
    files = inventory_files(source)
    material = {item["relative_path"]: item for item in files}
    vault, secret_status = _vault(source)
    require(secret_status["available"], "MIGRATION_SECRET_INVALID", "Source vault must be readable")
    require(original.execute("PRAGMA integrity_check").fetchone()[0] == "ok" and not original.execute("PRAGMA foreign_key_check").fetchall(),
            "STATE_INTEGRITY", "Source database integrity needs review")
    schema = Path(__file__).parents[1].joinpath("storage/schema-v3.sql").read_text(encoding="utf-8")
    additions = schema.split("-- Application ownership (OAP 1.0).", 1)[1]
    report = {"format_version": "3.0", "source_protocol": "3.0", "applied": False, "changed": False,
              "ready_for_apply": True, "secrets": secret_status, "checkpoint_policy": "preserve original graph positions",
              "workspace_policy": "preserve absolute directory bindings", "artifact_path_policy": "bind managed copies to upgraded target",
              "control_credential_policy": "rotate in upgraded target", "table_counts": {}}
    if apply:
        destination.mkdir(parents=True, exist_ok=False)
    with closing(sqlite3.connect(destination / "oea.sqlite" if apply else ":memory:")) as output:
        original.backup(output)
        for (table,) in original.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
            from oea.migration.upgrade import quote
            report["table_counts"][table] = original.execute("SELECT COUNT(*) FROM " + quote(table)).fetchone()[0]
        output.executescript("BEGIN; ALTER TABLE sessions ADD COLUMN source TEXT NOT NULL DEFAULT 'platform' CHECK(source IN ('platform','application'));\n"
                            "ALTER TABLE events ADD COLUMN source TEXT NOT NULL DEFAULT 'platform' CHECK(source IN ('platform','application'));\n" + additions)
        output.execute("UPDATE metadata SET value=? WHERE key='schema_sha256'", (hashlib.sha256(schema.encode()).hexdigest(),))
        output.execute("UPDATE metadata SET value='2' WHERE key='schema_revision'")
        for identity, path, size, digest in original.execute("SELECT id,path,bytes,sha256 FROM artifacts"):
            relative = Path("artifacts") / identity
            copied = material.get(relative.as_posix())
            require(Path(path).resolve() == source / relative and copied is not None
                    and copied["bytes"] == size and copied["sha256"] == digest,
                    "MIGRATION_ARTIFACT_INVALID", "Retained artifact bytes must match their managed receipt")
            output.execute("UPDATE artifacts SET path=? WHERE id=?", (str((destination or source) / relative), identity))
        require(not output.execute("PRAGMA foreign_key_check").fetchall(), "MIGRATION_REFERENCE_INVALID", "Upgraded state has broken references")
        require(output.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "STATE_INTEGRITY", "Upgraded database integrity needs review")
        if apply:
            for item in files:
                relative = Path(item["relative_path"])
                if len(relative.parts) == 1 and relative.name.startswith("oea.sqlite"):
                    continue
                if relative == Path("client-token"):
                    continue
                copied = destination / relative
                copied.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source / relative, copied)
                require(file_sha256(copied) == item["sha256"], "MIGRATION_SOURCE_CHANGED", "Copied bytes must match the snapshot")
            if (source / "client-token").exists() or (source / "client.json").exists():
                atomic_write(destination / "client-token", secrets.token_urlsafe(32).encode("utf-8"))
            copied_vault, copied_secrets = _vault(destination)
            require(copied_secrets["available"] and copied_vault == vault, "MIGRATION_SECRET_INVALID", "Copied vault could not be verified")
            report.update(applied=True, changed=True)
        check()
        verify_files(source, files)
        output.commit()
    if apply:
        (destination / "migration-report.json").write_text(encode(report), encoding="utf-8")
    return report
