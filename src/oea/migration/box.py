"""Offline protocol-four conversion: centralize connections, preserve original facts."""

from contextlib import closing
import hashlib
import json
from pathlib import Path
import secrets
import shutil
import sqlite3
import tempfile

from oea.contracts.configuration import AgentDefinition, Model, RunConfiguration
from oea.contracts.errors import require
from oea.migration.offline import _vault
from oea.migration.source import inventory_files, verify_files, file_sha256
from oea.security.secrets import SecretVault, atomic_write
from oea.storage.persistence import encode, now_ms


def chain(convert, source, *, target=None, apply=False):
    """Keep the historical converter's diagnostics, then finalize the new target."""
    from oea.migration.upgrade import upgrade_state
    from oea.workspace.paths import external_path, resolve_data_dir
    source = resolve_data_dir(source)
    destination = external_path(target) if target is not None else None
    require(not apply or destination is not None, "MIGRATION_TARGET_REQUIRED", "Choose a new target")
    if destination is not None:
        require(not destination.exists(), "MIGRATION_TARGET_EXISTS", "Choose an absent target")
        require(not destination.is_relative_to(source) and not source.is_relative_to(destination), "MIGRATION_TARGET_INVALID", "Keep source and target separate")
    if not apply:
        inspected = convert(source, target=destination, apply=False)
        if not inspected.get("ready_for_apply", True):
            return inspected
        with tempfile.TemporaryDirectory(prefix="oea-offline-conversion-") as temporary:
            intermediate = Path(temporary) / "state"
            previous = convert(source, target=intermediate, apply=True)
            result = upgrade_state(intermediate, apply=False)
        return {**inspected, "format_version": "4.0", "source_protocol": inspected.get("source_protocol", "legacy"),
                "applied": False, "api_box": result, "models_imported": result["models_imported"],
                "legacy_model_fields_removed": result["legacy_model_fields_removed"], "orphan_credentials_removed": result["orphan_credentials_removed"]}
    # The old converter writes directly to the explicitly selected, previously
    # absent target. Its existing partial-target diagnostics remain available.
    previous = convert(source, target=destination, apply=True)
    with tempfile.TemporaryDirectory(prefix="oea-finalize-", dir=destination.parent) as temporary:
        temporary = Path(temporary).resolve()
        converted, backup = temporary / "current", temporary / "previous"
        result = upgrade_state(destination, target=converted, apply=True)
        with closing(sqlite3.connect(converted / "oea.sqlite")) as database, database:
            for identity, path in database.execute("SELECT id,path FROM artifacts").fetchall():
                relative = Path(path).relative_to(converted)
                database.execute("UPDATE artifacts SET path=? WHERE id=?", (str(destination / relative), identity))
        require(converted.resolve().is_relative_to(temporary) and backup.resolve().is_relative_to(temporary)
                and destination.parent == temporary.parent and destination != source,
                "MIGRATION_TARGET_INVALID", "Verify exact target ownership before replacing conversion staging")
        destination.rename(backup)
        try:
            converted.rename(destination)
        except OSError:
            backup.rename(destination)
            raise
    result = {**previous, "format_version": "4.0", "source_protocol": previous.get("source_protocol", "legacy"),
              "applied": True, "api_box": result, "models_imported": result["models_imported"],
              "legacy_model_fields_removed": result["legacy_model_fields_removed"], "orphan_credentials_removed": result["orphan_credentials_removed"]}
    (destination / "migration-report.json").write_text(encode(result), encoding="utf-8")
    return result


class BoxConversion:
    def __init__(self, db, vault):
        self.db, self.vault = db, vault
        self.connections, self.names = {}, set()
        self.imported = 0

    def model(self, config):
        if not config or not (config.get("model") or config.get("base_url") or config.get("credential_ref")):
            return None
        model = Model.model_validate(config)
        if model.credential_ref and model.credential_ref.startswith("vault:"):
            require(bool(self.vault.get(model.credential_ref[6:])), "MIGRATION_SECRET_INVALID", "A model credential is unavailable; retain the original state")
        material = model.model_dump(mode="json")
        reference = model.credential_ref
        if reference and reference.startswith("vault:"):
            material["credential_ref"] = hashlib.sha256(self.vault[reference[6:]].encode()).hexdigest()
        fingerprint = hashlib.sha256(encode(material).encode()).hexdigest()
        if fingerprint in self.connections:
            return self.connections[fingerprint]
        identity = "model_" + fingerprint[:32]
        base = (model.model or "Imported model")[:100]
        name, suffix = base, 2
        while name in self.names:
            name, suffix = base + " (" + str(suffix) + ")", suffix + 1
        self.names.add(name)
        self.db.execute("INSERT INTO model_profiles VALUES(?,?,1,1,0,?)", (identity, name, now_ms()))
        self.db.execute("INSERT INTO model_profile_versions VALUES(?,1,?,?)", (identity, model.model_dump_json(), now_ms()))
        result = {"id": identity, "name": name, "config": model.model_dump(mode="json"), "revision": 1}
        self.connections[fingerprint] = result
        self.imported += 1
        return result

    def definition(self, config, *, snapshot=False):
        value = dict(config)
        models = {}
        for field in ("model", "summary_model"):
            original = value.pop(field, None)
            row = self.model(original)
            value[field + "_ref"] = row["id"] if row else None
            models[field] = (original, row)
        if value.get("manager") is not None:
            manager = dict(value["manager"])
            for field in ("model", "summary_model"):
                row = self.model(manager.pop(field, None))
                manager[field + "_ref"] = row["id"] if row else None
            value["manager"] = manager
        declaration = AgentDefinition.model_validate(value)
        if not snapshot:
            return declaration.model_dump(mode="json")
        resolved = declaration.model_dump(mode="json")
        for field, (original, row) in models.items():
            resolved[field] = original if original is not None else ({} if field == "model" else None)
            resolved[field + "_name"] = row["name"] if row else None
            resolved[field + "_version"] = 1 if row else None
        return RunConfiguration.model_validate(resolved).model_dump(mode="json")


def hold_imported_work(db):
    """Require an explicit continuation across an offline product upgrade."""
    held = {}
    for table in ("runs", "workflow_runs"):
        values = db.execute("SELECT id,state,error_json FROM " + table + " WHERE state IN ('accepted','running','waiting')").fetchall()
        held[table] = [{"id": row[0], "state": row[1], "error_json": row[2]} for row in values]
        for identity, state, error in values:
            fault = {"code": "MIGRATION_CONTINUATION_REQUIRED", "safe_message": "Inspect the original work and explicitly resume its retained checkpoint after migration",
                     "details": {"previous_state": state, "previous_error": json.loads(error) if error else None}}
            db.execute("UPDATE " + table + " SET state='needs_review',error_json=?,handled_at=NULL,updated_at=? WHERE id=?", (encode(fault), now_ms(), identity))
    return held


def transition(original, source, destination, apply, check):
    """Copy a stopped, recognized protocol-three state into a new protocol-four target."""
    files = inventory_files(source)
    vault, secret_status = _vault(source)
    require(secret_status["available"], "MIGRATION_SECRET_INVALID", "Source credentials are unavailable")
    require(original.execute("PRAGMA integrity_check").fetchone()[0] == "ok" and not original.execute("PRAGMA foreign_key_check").fetchall(),
            "STATE_INTEGRITY", "Source state needs integrity review")
    script = Path(__file__).parents[1].joinpath("storage/schema-v4.sql").read_text(encoding="utf-8")
    additions = script[script.index("CREATE TABLE model_profiles"):]
    report = {"format_version": "4.0", "source_protocol": "3.0", "applied": False, "ready_for_apply": True,
              "secrets": secret_status, "table_counts": {}, "checkpoint_policy": "preserve", "budget_policy": "unchanged",
              "source_policy": "unchanged", "legacy_model_fields_removed": 0, "orphan_credentials_removed": 0}
    if apply:
        destination.mkdir(parents=True, exist_ok=False)
    with closing(sqlite3.connect(destination / "oea.sqlite" if apply else ":memory:")) as output:
        output.row_factory = sqlite3.Row
        original.backup(output)
        output.executescript("BEGIN;\n" + additions)
        converter = BoxConversion(output, vault)
        for table in ("agent_definitions", "instances", "runs"):
            report["table_counts"][table] = original.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
            for row in original.execute("SELECT id,config_json FROM " + table):
                value = json.loads(row["config_json"])
                converted = converter.definition(value, snapshot=table == "runs")
                output.execute("UPDATE " + table + " SET config_json=? WHERE id=?", (encode(converted), row["id"]))
                if table != "runs":
                    report["legacy_model_fields_removed"] += sum(field in value for field in ("model", "summary_model"))
        # Version three and four share the graph format. Configuration decoding
        # changes, but original graph positions, action keys and budget facts do not.
        output.execute("UPDATE runs SET execution_version='4.0' WHERE execution_version='3.0'")
        output.execute("UPDATE workflow_runs SET execution_version='4.0' WHERE execution_version='3.0'")
        report["continuation_review"] = hold_imported_work(output)
        for table in ("projects", "sessions", "actions", "usage", "messages", "artifacts", "workflow_runs"):
            count = original.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
            require(output.execute("SELECT COUNT(*) FROM " + table).fetchone()[0] == count, "MIGRATION_COUNT_MISMATCH", "Migration changed retained record counts")
            report["table_counts"][table] = count
        material = {item["relative_path"]: item for item in files}
        for row in original.execute("SELECT id,path,bytes,sha256 FROM artifacts"):
            original_path = Path(row["path"]).resolve()
            require(original_path.is_relative_to(source / "artifacts"), "MIGRATION_ARTIFACT_INVALID", "Artifact is outside managed storage")
            relative = original_path.relative_to(source)
            item = material.get(relative.as_posix())
            require(item and item["bytes"] == row["bytes"] and item["sha256"] == row["sha256"],
                    "MIGRATION_ARTIFACT_INVALID", "Artifact bytes differ from the original receipt")
            output.execute("UPDATE artifacts SET path=? WHERE id=?", (str((destination or source) / relative), row["id"]))
        output.execute("UPDATE metadata SET value='4.0' WHERE key='protocol_version'")
        output.execute("UPDATE metadata SET value='6' WHERE key='schema_revision'")
        output.execute("UPDATE metadata SET value=? WHERE key='schema_sha256'", (hashlib.sha256(script.encode()).hexdigest(),))
        output.execute("INSERT OR REPLACE INTO metadata VALUES('migration_complete','true')")
        require(not output.execute("PRAGMA foreign_key_check").fetchall() and output.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "MIGRATION_REFERENCE_INVALID", "Converted references are invalid")
        require(output.execute("SELECT COUNT(*) FROM agent_definitions WHERE json_type(config_json,'$.model') IS NOT NULL OR json_type(config_json,'$.summary_model') IS NOT NULL OR json_type(config_json,'$.manager.model') IS NOT NULL OR json_type(config_json,'$.manager.summary_model') IS NOT NULL").fetchone()[0] == 0,
                "MIGRATION_MODEL_FIELDS_REMAIN", "Legacy Agent API configuration remains")
        report["models_imported"] = converter.imported
        retained = set()
        def references(value):
            if isinstance(value, str) and value.startswith("vault:"):
                retained.add(value[6:])
            elif isinstance(value, dict):
                for child in value.values():
                    references(child)
            elif isinstance(value, list):
                for child in value:
                    references(child)
        # Credential cleanup is a one-off offline operation, not a runtime scan.
        tables = [row[0] for row in output.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        from oea.migration.upgrade import quote
        for table in tables:
            for column in output.execute("PRAGMA table_info(" + quote(table) + ")").fetchall():
                if column[1].endswith("_json"):
                    for row in output.execute("SELECT " + quote(column[1]) + " FROM " + quote(table) + " WHERE instr(" + quote(column[1]) + ",'vault:')>0"):
                        references(json.loads(row[0]))
        orphaned = set(vault) - retained
        report["orphan_credentials_removed"] = len(orphaned)
        if apply:
            for item in files:
                relative = Path(item["relative_path"])
                if len(relative.parts) == 1 and (relative.name.startswith("oea.sqlite") or relative.name == "client-token"):
                    continue
                target = destination / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source / relative, target)
                require(file_sha256(target) == item["sha256"], "MIGRATION_SOURCE_CHANGED", "Copied state bytes differ")
            atomic_write(destination / "client-token", secrets.token_urlsafe(32).encode())
            if (destination / "client.json").exists():
                metadata = json.loads((destination / "client.json").read_text(encoding="utf-8"))
                metadata["protocol_version"] = "4.0"
                atomic_write(destination / "client.json", encode(metadata).encode())
            copied = SecretVault(destination)
            require(copied.values == vault, "MIGRATION_SECRET_INVALID", "Copied credential vault differs")
            if orphaned:
                for key in orphaned:
                    copied.values.pop(key)
                # Persist the complete filtered vault once through its existing atomic writer.
                key, value = next(iter(copied.values.items()), ("migration-empty", ""))
                copied.set(key, value)
            require(copied.values == {key: value for key, value in vault.items() if key in retained}, "MIGRATION_SECRET_INVALID", "Credential cleanup did not retain the expected entries")
            report["applied"] = True
        check()
        verify_files(source, files)
        output.commit()
    if apply:
        (destination / "migration-report.json").write_text(encode(report), encoding="utf-8")
    return report
