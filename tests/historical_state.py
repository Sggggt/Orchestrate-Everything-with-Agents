"""Build a protocol-three SQLite fixture from synthetic test work and frozen DDL."""

import json
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path


def historical_state(current, target, *, revision, schema_hash):
    schema_directory = Path(__file__).parents[1] / "src/oea/migration"
    schema = "schema-v3-pre-canvas.sql" if revision == 2 else "schema-v3-pre-handling.sql"
    target.mkdir()
    shutil.copy2(current / "graphs.sqlite", target / "graphs.sqlite")
    with closing(sqlite3.connect(target / "oea.sqlite")) as db, db:
        db.executescript((schema_directory / schema).read_text(encoding="utf-8"))
        if revision == 4:
            db.execute("ALTER TABLE runs ADD COLUMN handled_at INTEGER")
            db.execute("CREATE INDEX usage_attention ON usage(run_id) WHERE state='needs_review'")
        db.execute("ATTACH DATABASE ? AS fixture", (str(current / "oea.sqlite"),))
        for table, in db.execute("SELECT name FROM main.sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
            columns = ",".join(f'"{row[1]}"' for row in db.execute(f'PRAGMA main.table_info("{table}")'))
            db.execute(f'INSERT INTO main."{table}" ({columns}) SELECT {columns} FROM fixture."{table}"')
        def legacy(config):
            for field in ("model", "summary_model"):
                reference = config.pop(field + "_ref", None)
                if reference:
                    row = db.execute("SELECT v.config_json FROM fixture.model_profiles p JOIN fixture.model_profile_versions v ON v.profile_id=p.id AND v.revision=p.revision WHERE p.id=?", (reference,)).fetchone()
                    config[field] = json.loads(row[0])
                for suffix in ("_name", "_version"):
                    config.pop(field + suffix, None)
            config.pop("plugin_versions", None)
            config.pop("plugin_components", None)
            config.pop("plugins", None)
            config.pop("creation", None)
            if config.get("manager"):
                legacy(config["manager"])
            return config
        for table in ("agent_definitions", "instances", "runs"):
            for identity, config in db.execute(f"SELECT id,config_json FROM {table}").fetchall():
                db.execute(f"UPDATE {table} SET config_json=? WHERE id=?", (json.dumps(legacy(json.loads(config))), identity))
        db.execute("UPDATE runs SET execution_version='3.0'")
        db.executemany("UPDATE metadata SET value=? WHERE key=?", [
            ("3.0", "protocol_version"), (str(revision), "schema_revision"), (schema_hash, "schema_sha256")])
    return target
