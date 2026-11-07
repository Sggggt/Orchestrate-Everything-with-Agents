"""Application registrations are managed separately from application requests."""

import asyncio
import hashlib
import hmac
import json
import secrets

from oea.catalog.service import need
from oea.contracts.applications import ApplicationBinding, ApplicationGrant, ExecutionScope
from oea.contracts.configuration import AgentDefinition
from oea.contracts.errors import require
from oea.storage.persistence import event, new_id, now_ms, one, rows


def application_id(prefix):
    """128-bit opaque identities keep nested application directories practical."""
    return prefix + "_" + secrets.token_urlsafe(16)


class Applications:
    def __init__(self, host):
        self.host = host
        self.registrations = {}
        self.authorizations = {}
        self.authorization_changes = {}

    def authorization_signal(self, app_id):
        return self.authorization_changes.setdefault(app_id, asyncio.Event())

    def authorization_changed(self, app_id):
        signal = self.authorization_changes.pop(app_id, None)
        if signal is not None:
            signal.set()

    async def open(self):
        applications, bindings = await self.host.store.call(lambda db: (
            rows(db, "SELECT * FROM applications"), rows(db, "SELECT * FROM application_bindings")))
        self.registrations = {row["id"]: self.view(row) for row in applications}
        self.authorizations = {row["id"]: {**self.binding_view(row), "capabilities": ApplicationGrant.model_validate_json(row["grant_json"])} for row in bindings}

    def current_grant(self, scope):
        application = self.registrations.get(scope.app_id)
        binding = self.authorizations.get(scope.binding_id)
        require(application and application["enabled"] and not application["removed"] and binding
                and binding["enabled"] and binding["app_id"] == scope.app_id,
                "PERMISSION_DENIED", "Application execution authorization was revoked")
        return binding["capabilities"]

    @staticmethod
    def view(row):
        return {key: row[key] for key in ("id", "name", "enabled", "removed", "revision", "credential_revision", "revoked_at", "created_at")}

    @staticmethod
    def binding_view(row):
        return {key: row[key] for key in ("id", "app_id", "project_id", "agent_id", "enabled", "revision", "created_at")} | {
            "grant": json.loads(row["grant_json"])}

    @staticmethod
    def credential(identity):
        token = "oap." + identity + "." + secrets.token_urlsafe(32)
        return token, hashlib.sha256(token.encode()).hexdigest()

    async def create(self, name: str):
        """Register an application and return its credential once."""
        require(0 < len(name.strip()) <= 80, "INVALID_REQUEST", "Use an application name of 1 to 80 characters")
        identity = application_id("app")
        token, digest = self.credential(identity)
        def write(db):
            db.execute("INSERT INTO applications(id,name,token_hash,created_at) VALUES(?,?,?,?)", (identity, name.strip(), digest, now_ms()))
            event(db, "application.created", data={"app_id": identity})
            return self.view(need(db, "applications", identity))
        value = await self.host.store.call(write, transaction=True)
        self.registrations[value["id"]] = dict(value)
        self.host.notify()
        return {"application": value, "credential": token}

    async def get(self, app_id: str):
        """Read registration metadata without credentials or business content."""
        return await self.host.store.call(lambda db: self.view(need(db, "applications", app_id)))

    async def list(self, *, cursor: int = 0, limit: int = 25):
        """Read a bounded page of application registrations."""
        require(0 <= cursor < 2**63 and 1 <= limit <= 100, "INVALID_REQUEST", "Use a bounded application page")
        values = await self.host.store.call(lambda db: rows(db, "SELECT rowid AS position,* FROM applications WHERE rowid>? ORDER BY rowid LIMIT ?", (cursor, limit + 1)))
        return {"items": [self.view(row) for row in values[:limit]], "cursor": values[limit - 1]["position"] if len(values) > limit else None}

    async def rotate_credential(self, app_id: str, expected_revision: int):
        """Invalidate the previous credential; retain the application identity."""
        token, digest = self.credential(app_id)
        def write(db):
            current = need(db, "applications", app_id)
            require(not current["removed"], "APPLICATION_DISABLED", "Application registration was removed")
            require(current["revision"] == expected_revision, "REVISION_CONFLICT", "Application registration changed")
            db.execute("UPDATE applications SET token_hash=?,credential_revision=credential_revision+1,revision=revision+1,revoked_at=? WHERE id=?", (digest, now_ms(), app_id))
            event(db, "application.credential_rotated", data={"app_id": app_id})
            return self.view(need(db, "applications", app_id))
        value = await self.host.store.call(write, transaction=True)
        self.registrations[value["id"]] = dict(value)
        self.authorization_changed(app_id)
        self.host.notify()
        return {"application": value, "credential": token}

    async def set_enabled(self, app_id: str, enabled: bool, expected_revision: int):
        """Enable or revoke application access without deleting its history."""
        return await self._status(app_id, enabled, expected_revision)

    async def remove(self, app_id: str, expected_revision: int):
        """Remove the registration while retaining original execution facts."""
        return await self._status(app_id, False, expected_revision, removed=True)

    async def _status(self, app_id, enabled, expected_revision, *, removed=False):
        def write(db):
            current = need(db, "applications", app_id)
            require(current["revision"] == expected_revision, "REVISION_CONFLICT", "Application registration changed")
            require(not current["removed"] or removed, "APPLICATION_DISABLED", "Removed registration cannot be enabled")
            db.execute("UPDATE applications SET enabled=?,removed=?,revision=revision+1,revoked_at=? WHERE id=?", (int(enabled), int(removed), now_ms() if not enabled else current["revoked_at"], app_id))
            event(db, "application.updated", data={"app_id": app_id, "enabled": enabled, "removed": removed})
            return self.view(need(db, "applications", app_id))
        value = await self.host.store.call(write, transaction=True)
        self.registrations[value["id"]] = dict(value)
        self.authorization_changed(app_id)
        await self.host.revalidate_commands(app_id=app_id)
        self.host.notify()
        return value

    async def authenticate(self, token, *, metadata=False):
        """Verify a bearer credential; identifiers and management tokens confer no access."""
        parts = token.split(".")
        require(len(parts) == 3 and parts[0] == "oap" and len(token) <= 256, "PERMISSION_DENIED", "Application authentication required")
        digest = hashlib.sha256(token.encode()).hexdigest()
        row = await self.host.store.call(lambda db: one(db, "SELECT * FROM applications WHERE id=?", (parts[1],)))
        require(row is not None and row["enabled"] and not row["removed"] and hmac.compare_digest(row["token_hash"], digest),
                "PERMISSION_DENIED", "Application authentication required")
        return {"app_id": row["id"], "credential_revision": row["credential_revision"]} if metadata else row["id"]

    def authorize_stream(self, app_id, credential_revision):
        current = self.registrations.get(app_id)
        require(current and current["enabled"] and not current["removed"] and current["credential_revision"] == credential_revision,
                "PERMISSION_DENIED", "Application authentication is no longer valid")

    async def bind(self, binding: ApplicationBinding, *, binding_id: str | None = None, expected_revision: int | None = None):
        """Grant a saved Agent to an application in one explicit source project."""
        binding = ApplicationBinding.model_validate(binding)
        identity = binding_id or application_id("bind")
        def write(db):
            application = need(db, "applications", binding.app_id)
            require(not application["removed"], "APPLICATION_DISABLED", "Application registration was removed")
            need(db, "projects", binding.project_id)
            self.host.assert_project_available(binding.project_id)
            definition = AgentDefinition.model_validate_json(need(db, "agent_definitions", binding.agent_id)["config_json"])
            require(not definition.removed and definition.project_id in {None, binding.project_id}, "PERMISSION_DENIED", "Agent is not available in this project")
            previous = one(db, "SELECT * FROM application_bindings WHERE id=?", (identity,))
            if previous:
                require((previous["app_id"], previous["project_id"], previous["agent_id"]) == (binding.app_id, binding.project_id, binding.agent_id), "PERMISSION_DENIED", "Binding ownership cannot change")
                require(expected_revision == previous["revision"], "REVISION_CONFLICT", "Application binding changed")
                db.execute("UPDATE application_bindings SET grant_json=?,enabled=?,revision=revision+1 WHERE id=?", (binding.grant.model_dump_json(), int(binding.enabled), identity))
            else:
                require(binding_id is None and expected_revision in {None, 0}, "REVISION_CONFLICT", "Create a new host-assigned binding")
                db.execute("INSERT INTO application_bindings(id,app_id,project_id,agent_id,grant_json,enabled,created_at) VALUES(?,?,?,?,?,?,?)",
                           (identity, binding.app_id, binding.project_id, binding.agent_id, binding.grant.model_dump_json(), int(binding.enabled), now_ms()))
            event(db, "application.binding_saved", project_id=binding.project_id, data={"app_id": binding.app_id, "binding_id": identity})
            return self.binding_view(need(db, "application_bindings", identity))
        value = await self.host.store.call(write, transaction=True)
        self.authorizations[value["id"]] = {**value, "capabilities": binding.grant.model_copy(deep=True)}
        await self.host.revalidate_commands(binding_id=identity)
        self.host.notify()
        return value

    async def bindings(self, app_id: str, *, cursor: int = 0, limit: int = 25):
        """Read granted configurations; this management view contains no conversations."""
        require(0 <= cursor < 2**63 and 1 <= limit <= 100, "INVALID_REQUEST", "Use a bounded binding page")
        def read(db):
            need(db, "applications", app_id)
            return rows(db, "SELECT rowid AS position,* FROM application_bindings WHERE app_id=? AND rowid>? ORDER BY rowid LIMIT ?", (app_id, cursor, limit + 1))
        values = await self.host.store.call(read)
        return {"items": [self.binding_view(row) for row in values[:limit]], "cursor": values[limit - 1]["position"] if len(values) > limit else None}

    @staticmethod
    def scope(db, session_id):
        session = need(db, "sessions", session_id)
        if session["source"] == "platform":
            return ExecutionScope()
        value = one(db, "SELECT id,app_id,binding_id FROM application_conversations WHERE session_id=?", (session_id,))
        require(value is not None, "APPLICATION_SCOPE_INVALID", "Application session has no persistent ownership")
        return ExecutionScope(source="application", app_id=value["app_id"], binding_id=value["binding_id"], conversation_id=value["id"])

    @staticmethod
    def grant(db, scope):
        application = need(db, "applications", scope.app_id)
        binding = need(db, "application_bindings", scope.binding_id)
        require(application["enabled"] and not application["removed"] and binding["enabled"] and binding["app_id"] == scope.app_id,
                "PERMISSION_DENIED", "Application execution authorization was revoked")
        return ApplicationGrant.model_validate_json(binding["grant_json"])
