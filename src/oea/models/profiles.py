"""User-owned API Box; declarations reference immutable connection versions."""

import asyncio
import json

from oea.contracts.configuration import Model, RunConfiguration
from oea.contracts.errors import require
from oea.storage.persistence import encode, event, new_id, now_ms, one, rows


class ModelProfiles:
    def __init__(self, host):
        self.host = host
        self.current = {}

    async def open(self):
        values = await self.host.store.call(lambda db: rows(db,
            "SELECT p.*,v.config_json FROM model_profiles p JOIN model_profile_versions v ON v.profile_id=p.id AND v.revision=p.revision"))
        self.current = {row["id"]: row for row in values}

    def lookup(self, model_ref, *, available=True):
        row = self.current.get(model_ref)
        if row is None:
            row = next((item for item in self.current.values() if item["name"] == model_ref and not item["removed"]), None)
            if row is None:
                row = next((item for item in self.current.values() if item["name"] == model_ref), None)
        require(row is not None, "MODEL_NOT_FOUND", "Select an API Box model")
        if available:
            require(row["enabled"] and not row["removed"], "MODEL_DISABLED", "The API Box model is disabled or removed")
        return row

    def summary(self, model_ref):
        if not model_ref:
            return None
        row = self.lookup(model_ref, available=False)
        model = Model.model_validate_json(row["config_json"])
        return {"id": row["id"], "name": row["name"], "revision": row["revision"],
                "enabled": bool(row["enabled"] and not row["removed"]),
                "configured": bool(model.base_url and model.model and (not model.credential_ref or self.host.secret(model.credential_ref))),
                "model_id": model.model}

    def view(self, row):
        model = Model.model_validate_json(row["config_json"])
        return {**self.summary(row["id"]), "removed": bool(row["removed"]),
                "model": model.model_dump(mode="json", exclude={"credential_ref"}),
                "has_credential": bool(model.credential_ref and self.host.secret(model.credential_ref))}

    def summaries(self, definition):
        manager = definition.manager
        return {"main": self.summary(definition.model_ref), "summary": self.summary(definition.summary_model_ref),
                "manager": self.summary(manager.model_ref) if manager else None,
                "manager_summary": self.summary(manager.summary_model_ref) if manager else None}

    def validate(self, definition):
        refs = [definition.model_ref, definition.summary_model_ref]
        if definition.manager:
            refs.extend((definition.manager.model_ref, definition.manager.summary_model_ref))
        refs.extend(definition.creation.model_refs)
        for reference in refs:
            if reference:
                self.lookup(reference, available=False)

    def canonical(self, definition):
        def reference(value):
            return self.lookup(value, available=False)["id"] if value else None
        manager = definition.manager
        if manager:
            manager = manager.model_copy(update={"model_ref": reference(manager.model_ref), "summary_model_ref": reference(manager.summary_model_ref)})
        return definition.model_copy(update={"model_ref": reference(definition.model_ref), "summary_model_ref": reference(definition.summary_model_ref),
            "manager": manager, "creation": definition.creation.model_copy(update={"model_refs": tuple(reference(value) for value in definition.creation.model_refs)})})

    def resolve(self, definition, *, model_ref=None):
        changes = {}
        for field, reference in (("model", model_ref or definition.model_ref), ("summary_model", definition.summary_model_ref)):
            if reference:
                row = self.lookup(reference)
                changes[field] = Model.model_validate_json(row["config_json"])
                changes[field + "_ref"] = row["id"]
                changes[field + "_version"] = row["revision"]
                changes[field + "_name"] = row["name"]
        return RunConfiguration.model_validate(definition.model_dump(mode="json") | changes)

    def authorize(self, snapshot, *, purpose="model"):
        reference = snapshot.summary_model_ref if purpose == "summary" and snapshot.summary_model_ref else snapshot.model_ref
        if reference:
            self.lookup(reference)

    async def get(self, model_ref: str):
        return self.view(self.lookup(model_ref, available=False))

    async def list(self, *, cursor: int = 0, limit: int = 25, include_disabled: bool = False):
        require(type(cursor) is int and cursor >= 0 and 1 <= limit <= 100, "INVALID_REQUEST", "Read a bounded model page")
        values = await self.host.store.call(lambda db: rows(db,
            "SELECT rowid AS position,id FROM model_profiles WHERE removed=0 AND rowid>? AND (? OR enabled=1) ORDER BY rowid LIMIT ?",
            (cursor, int(include_disabled), limit + 1)))
        return {"items": [self.view(self.current[row["id"]]) for row in values[:limit]],
                "cursor": values[limit - 1]["position"] if len(values) > limit else None}

    async def save(self, name: str, model: Model, *, id: str | None = None, expected_revision: int | None = None, enabled: bool = True):
        require(0 < len(name.strip()) <= 120, "INVALID_REQUEST", "Use a bounded unique model name")
        require(model.credential_ref is None, "INVALID_REQUEST", "Write credentials through models.set_credential")
        identity = id or new_id("model")
        def write(db):
            prior = one(db, "SELECT * FROM model_profiles WHERE id=?", (identity,))
            require(not prior or not prior["removed"], "MODEL_REMOVED", "Removed model identities cannot be reused")
            require(expected_revision in ({None, prior["revision"]} if prior else {None, 0}), "REVISION_CONFLICT", "API Box entry changed")
            duplicate = one(db, "SELECT id FROM model_profiles WHERE name=? AND id!=? AND removed=0", (name.strip(), identity))
            require(not duplicate, "MODEL_NAME_CONFLICT", "This API Box model name already exists")
            revision = prior["revision"] + 1 if prior else 1
            if prior:
                original = Model.model_validate_json(one(db, "SELECT config_json FROM model_profile_versions WHERE profile_id=? AND revision=?", (identity, prior["revision"]))["config_json"])
                config = model.model_copy(update={"credential_ref": original.credential_ref})
                db.execute("UPDATE model_profiles SET name=?,revision=?,enabled=?,updated_at=? WHERE id=?", (name.strip(), revision, int(enabled), now_ms(), identity))
            else:
                config = model
                db.execute("INSERT INTO model_profiles VALUES(?,?,?,?,0,?)", (identity, name.strip(), revision, int(enabled), now_ms()))
            db.execute("INSERT INTO model_profile_versions VALUES(?,?,?,?)", (identity, revision, config.model_dump_json(), now_ms()))
            event(db, "model.saved", data={"id": identity, "revision": revision})
            return one(db, "SELECT p.*,v.config_json FROM model_profiles p JOIN model_profile_versions v ON v.profile_id=p.id AND v.revision=p.revision WHERE p.id=?", (identity,))
        row = await self.host.store.call(write, transaction=True)
        self.current[identity] = row
        self.host.notify()
        return self.view(row)

    async def set_credential(self, model_ref: str, value: str, expected_revision: int):
        row = self.lookup(model_ref)
        require(row["revision"] == expected_revision, "REVISION_CONFLICT", "API Box entry changed")
        require(len(value) <= 16384, "INVALID_REQUEST", "Use a bounded model credential")
        reference = "vault:" + new_id("credential") if value else None
        if reference:
            await asyncio.to_thread(self.host.vault.set, reference[6:], value)
        def write(db):
            current = one(db, "SELECT * FROM model_profiles WHERE id=?", (row["id"],))
            require(current["revision"] == expected_revision and not current["removed"], "REVISION_CONFLICT", "API Box entry changed")
            config = Model.model_validate_json(row["config_json"]).model_copy(update={"credential_ref": reference})
            revision = expected_revision + 1
            db.execute("INSERT INTO model_profile_versions VALUES(?,?,?,?)", (row["id"], revision, config.model_dump_json(), now_ms()))
            db.execute("UPDATE model_profiles SET revision=?,updated_at=? WHERE id=?", (revision, now_ms(), row["id"]))
            event(db, "model.saved", data={"id": row["id"], "revision": revision})
            return {**row, "revision": revision, "config_json": config.model_dump_json()}
        try:
            saved = await self.host.store.call(write, transaction=True)
        except Exception:
            if reference:
                await asyncio.to_thread(self.host.vault.set, reference[6:], "")
            raise
        self.current[row["id"]] = saved
        self.host.notify()
        return self.view(saved)

    async def set_enabled(self, model_ref: str, enabled: bool, expected_revision: int):
        row = self.lookup(model_ref, available=False)
        model = Model.model_validate_json(row["config_json"]).model_copy(update={"credential_ref": None})
        return await self.save(row["name"], model, id=row["id"], expected_revision=expected_revision, enabled=enabled)

    async def remove(self, model_ref: str, expected_revision: int):
        row = self.lookup(model_ref, available=False)
        def write(db):
            current = one(db, "SELECT * FROM model_profiles WHERE id=?", (row["id"],))
            require(current["revision"] == expected_revision, "REVISION_CONFLICT", "API Box entry changed")
            revision = expected_revision + 1
            db.execute("UPDATE model_profiles SET enabled=0,removed=1,revision=?,updated_at=? WHERE id=?", (revision, now_ms(), row["id"]))
            db.execute("INSERT INTO model_profile_versions VALUES(?,?,?,?)", (row["id"], revision, row["config_json"], now_ms()))
            event(db, "model.removed", data={"id": row["id"], "revision": revision})
            return {**row, "enabled": 0, "removed": 1, "revision": revision}
        self.current[row["id"]] = await self.host.store.call(write, transaction=True)
        self.host.notify()
        return {"id": row["id"], "removed": True}

    async def probe(self, model_ref: str, agent_id: str, session_id: str, idempotency_key: str, *, role: str = "main"):
        reference = self.lookup(model_ref)["id"]
        return await self.host.agents.probe(agent_id, session_id, idempotency_key, role=role, model_ref=reference)
