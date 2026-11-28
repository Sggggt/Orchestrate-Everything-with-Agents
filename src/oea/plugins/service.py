"""Plugin packaging and ownership; execution stays in the existing capability core."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

from pydantic import ValidationError

from oea.contracts.configuration import AgentDefinition
from oea.contracts.errors import RuntimeFault, require
from oea.interfaces.registry import operation_context
from oea.mcp.upstream import MCPServer
from oea.skills.service import SkillSource
from oea.storage.persistence import encode, event, new_id, now_ms, one, rows


from oea.plugins.contracts import PLUGIN_SCHEMA, MCP_SCHEMA, NAMESPACE, OFFICIAL, component_id
from oea.plugins.packages import PluginPackages
from oea.plugins.catalog import PluginCatalog
from oea.plugins.bindings import PluginBindings


class Plugins:
    def __init__(self, host):
        self.host = host
        self.current = {}
        self.bindings = PluginBindings(self.need, host.mcp, host.skills)
        self.packages = PluginPackages(host.root, lambda value: host.vault.contains_secret(value))
        self.catalogs = PluginCatalog(host.store, host.root, lambda value: host.vault.contains_secret(value), host.notify, lambda: self.current)

    async def open(self):
        def seed(db):
            for identity, spec in OFFICIAL.items():
                manifest = {"$schema": PLUGIN_SCHEMA, "name": identity, "version": "1.0.0", "description": spec["description"],
                            "extensions": {NAMESPACE: {"operations": spec["operations"]}}}
                if not one(db, "SELECT 1 FROM plugins WHERE id=?", (identity,)):
                    db.execute("INSERT INTO plugins VALUES(?,?,?,?,1,1,0,1,?,?,NULL,?)", (identity, spec["name"], "1.0.0", None, "official:" + identity, encode(manifest), now_ms()))
                    db.execute("INSERT INTO plugin_versions VALUES(?,1,?,NULL)", (identity, encode(manifest)))
            return rows(db, "SELECT * FROM plugins")
        self.current = {row["id"]: row for row in await self.host.store.call(seed, transaction=True)}
        await self.catalogs.open()

    async def start(self):
        for identity in OFFICIAL:
            skill_id = "oea-" + identity
            if skill_id not in self.host.skills.definitions:
                path = Path(__file__).with_name("builtin") / identity
                await self.host.skills.register(SkillSource(id=skill_id, path=path, plugin_owner=identity, name=OFFICIAL[identity]["name"], description=OFFICIAL[identity]["description"]), expected_revision=0)
            await self.host.store.call(lambda db, identity=identity, skill_id=skill_id: db.execute("INSERT OR IGNORE INTO plugin_components VALUES(?,?,'skill',?,'owned')", (identity, "skill:" + identity, skill_id)), transaction=True)
        await self.refresh()

    def need(self, identity, project_id=None, *, available=True):
        row = self.current.get(identity)
        require(row is not None and row["project_id"] in {None, project_id}, "PLUGIN_NOT_FOUND", "Plugin is unavailable in this project")
        if available:
            require(row["enabled"] and not row["removed"], "PLUGIN_DISABLED", "Plugin is disabled or removed")
        return row

    def validate_bindings(self, definition):
        self.bindings.validate(definition)

    def pin(self, db, snapshot, project_id, *, scope):
        return self.bindings.pin(db, snapshot, project_id, scope=scope)

    def current_definition(self, definition, project_id, snapshot=None):
        return self.bindings.current_definition(definition, project_id, snapshot)

    def authorize_component(self, kind, identity):
        self.bindings.authorize(kind, identity)

    async def refresh(self, plugin_id=None):
        def read(db):
            values = rows(db, "SELECT * FROM plugins" + (" WHERE id=?" if plugin_id else ""), (plugin_id,) if plugin_id else ())
            components = rows(db, "SELECT * FROM plugin_components" + (" WHERE plugin_id=?" if plugin_id else ""), (plugin_id,) if plugin_id else ())
            grouped = {}
            for component in components:
                grouped.setdefault(component["plugin_id"], []).append(component)
            for value in values:
                value["components"] = grouped.get(value["id"], [])
            return values
        values = await self.host.store.call(read)
        if plugin_id:
            self.current.update({row["id"]: row for row in values})
        else:
            self.current = {row["id"]: row for row in values}

    async def get(self, plugin_id: str, project_id: str | None = None):
        row = self.need(plugin_id, project_id, available=False)
        components = await self.host.store.call(lambda db: rows(db, "SELECT * FROM plugin_components WHERE plugin_id=?", (plugin_id,)))
        return self.view(row, components)

    def view(self, row, components, prepared_servers=None):
        manifest = json.loads(row["manifest_json"])
        items = []
        for component in components:
            item = {"id": component["component_id"], "kind": component["kind"], "ownership": component["ownership"]}
            if component["kind"] == "mcp":
                saved = (prepared_servers or {}).get(component["target_id"]) or self.host.mcp.configurations.get(component["target_id"])
                item["server_id"] = component["target_id"]
                if saved:
                    item.update(definition=saved[0].model_dump(mode="json"), revision=saved[1])
            else:
                item["skill_id"] = component["target_id"]
            items.append(item)
        return {key: row[key] for key in ("id", "name", "version", "project_id", "revision", "source")} | {
            "description": manifest.get("description", ""), "enabled": bool(row["enabled"]), "removed": bool(row["removed"]),
            "official": bool(row["official"]), "components": items,
            "operations": manifest.get("extensions", {}).get(NAMESPACE, {}).get("operations", []), "manifest": manifest}

    async def list(self, project_id: str | None = None, *, cursor: int = 0, limit: int = 25):
        require(type(cursor) is int and cursor >= 0 and 1 <= limit <= 100, "INVALID_REQUEST", "Read a bounded plugin page")
        values = await self.host.store.call(lambda db: rows(db,
            "SELECT rowid AS position,id FROM plugins WHERE removed=0 AND (project_id IS NULL OR project_id=?) AND rowid>? ORDER BY rowid LIMIT ?", (project_id, cursor, limit + 1)))
        return {"items": [await self.get(row["id"], project_id) for row in values[:limit]], "cursor": values[limit - 1]["position"] if len(values) > limit else None}


    async def install(self, source: str, project_id: str | None, idempotency_key: str):
        require(0 < len(idempotency_key) <= 256, "INVALID_REQUEST", "Plugin installation requires a stable key")
        consumer = (operation_context.get() or {}).get("consumer_id") or "local"
        idempotency_key = "plugin-install:" + idempotency_key
        fingerprint = hashlib.sha256(encode([source, project_id]).encode()).hexdigest()
        prior = await self.host.store.call(lambda db: one(db, "SELECT * FROM plugin_intents WHERE project_id=? AND consumer_id=? AND idempotency_key=?", (project_id or "", consumer, idempotency_key)))
        if prior:
            require(prior["input_hash"] == fingerprint, "IDEMPOTENCY_CONFLICT", "Installation key has different content")
            return json.loads(prior["result_json"])
        if source.startswith("official:"):
            identity = source.partition(":")[2]
            require(identity in OFFICIAL, "PLUGIN_NOT_FOUND", "Unknown official plugin")
            row = self.current[identity]
            if row["removed"] or not row["enabled"]:
                await self._state(identity, row["revision"], enabled=True)
            result = await self.get(identity, project_id)
        else:
            root, manifest = await asyncio.to_thread(self.packages.prepare, source)
            result = await self._install_package(root, manifest, source, project_id, intent=(consumer, idempotency_key, fingerprint))
        def save(db):
            saved = one(db, "SELECT * FROM plugin_intents WHERE project_id=? AND consumer_id=? AND idempotency_key=?", (project_id or "", consumer, idempotency_key))
            require(not saved or saved["input_hash"] == fingerprint, "IDEMPOTENCY_CONFLICT", "Installation key has different content")
            db.execute("INSERT OR IGNORE INTO plugin_intents VALUES(?,?,?,?,?,?)", (project_id or "", consumer, idempotency_key, fingerprint, encode(result), now_ms()))
        await self.host.store.call(save, transaction=True)
        return result

    async def _install_package(self, root, manifest, source, project_id, *, intent=None):
        if project_id:
            await self.host.projects.get(project_id)
        identity = manifest["name"]
        previous = self.current.get(identity)
        require(identity not in OFFICIAL and (not previous or previous["project_id"] == project_id), "PLUGIN_CONFLICT", "Plugin identity already belongs to another source")
        revision = previous["revision"] + 1 if previous else 1
        components, issues, prepared_servers, prepared_skills = [], [], {}, {}
        previous_components = {item["component_id"]: item for item in (previous or {}).get("components", ())}
        extension = manifest.get("extensions", {}).get(NAMESPACE, {})
        require(all(name in self.host.registry.handlers for name in extension.get("operations", [])), "PLUGIN_COMPONENT_UNSUPPORTED", "Declared native operations must already be registered by the installed SDK")
        for server_id in extension.get("mcp_refs", []):
            saved = await self.host.mcp.get(server_id)
            require(saved["definition"]["project_id"] in {None, project_id}, "PERMISSION_DENIED", "MCP reference belongs to another project")
            owned = previous_components.get("mcp:" + server_id)
            components.append(("mcp:" + server_id, "mcp", owned["target_id"] if owned and owned["ownership"] == "owned" else server_id, "owned" if owned and owned["ownership"] == "owned" else "reference"))
        for skill_id in extension.get("skill_refs", []):
            await self.host.skills.get(skill_id, project_id)
            components.append(("skill:" + skill_id, "skill", skill_id, "reference"))
        mcp_file = root / "mcp.json"
        if mcp_file.exists():
            try:
                config = json.loads(mcp_file.read_text(encoding="utf-8"))
                require(isinstance(config, dict) and config.get("$schema") == MCP_SCHEMA and set(config) <= {"$schema", "mcpServers"} and isinstance(config.get("mcpServers"), dict), "INVALID_PLUGIN", "Use the portable mcp.json schema")
                entries = config["mcpServers"]
            except (ValueError, RuntimeFault) as error:
                entries = {}
                issues.append({"component": "mcp.json", "reason": "INVALID_MCP_CONFIGURATION"})
            for name, entry in entries.items():
                try:
                    require(isinstance(entry, dict), "INVALID_PLUGIN", "An MCP entry must be an object")
                    server = self.packages.server(root, identity, root.name, name, entry, project_id)
                    if "mcp:" + name in extension.get("configuration_required", {}):
                        server = server.model_copy(update={"enabled": False})
                        issues.append({"component": name, "reason": "CONFIGURATION_REQUIRED"})
                    prior_component = previous_components.get("mcp:" + name)
                    if prior_component and prior_component["ownership"] == "owned":
                        configured = self.host.mcp.configurations.get(prior_component["target_id"])
                        if configured:
                            try:
                                server = self.packages.merge_server(server, configured[0], previous["package_path"], name, json.loads(previous["manifest_json"]))
                            except (OSError, ValueError, KeyError, RuntimeFault) as error:
                                raise RuntimeFault("PLUGIN_CONFIGURATION_CONFLICT", "Saved connection overrides are incompatible with the new package; keep the current version and inspect configuration", details={"component": name, "cause": type(error).__name__}) from error
                    existing = self.host.mcp.configurations.get(server.id)
                    require(existing is None or existing[0] == server, "PLUGIN_CONFLICT", "Prepared component identity has different content")
                    if existing is None:
                        prepared_servers[server.id] = server
                    components.append(("mcp:" + name, "mcp", server.id, "owned"))
                except RuntimeFault as error:
                    if error.code == "PLUGIN_CONFIGURATION_CONFLICT":
                        raise
                    issues.append({"component": name, "error": error.as_dict()})
                except ValidationError:
                    issues.append({"component": name, "reason": "INVALID_MCP_ENTRY"})
        directory = root / "skills"
        if directory.is_dir():
            for child in sorted(directory.iterdir()):
                if child.is_dir() and (child / "SKILL.md").is_file():
                    try:
                        skill = SkillSource(id=component_id(identity, child.name, root.name), path=child, project_id=project_id, plugin_owner=identity)
                        metadata = self.host.skills.metadata((child / "SKILL.md").read_text(encoding="utf-8"), required=True)
                        if skill.id not in self.host.skills.definitions:
                            skill = skill.model_copy(update={"name": metadata["name"], "description": metadata["description"]})
                            body, digest = self.host.skills.main_body(skill)
                            prepared_skills[skill.id] = (skill, body, digest)
                        components.append(("skill:" + child.name, "skill", skill.id, "owned"))
                    except (RuntimeFault, ValidationError, UnicodeError):
                        issues.append({"component": child.name, "reason": "INVALID_SKILL"})
        stored = {**manifest, "extensions": {**manifest.get("extensions", {}), NAMESPACE: {**extension, "issues": [*extension.get("issues", []), *issues]}}}
        def write(db):
            if intent:
                prior = one(db, "SELECT * FROM plugin_intents WHERE project_id=? AND consumer_id=? AND idempotency_key=?", (project_id or "", intent[0], intent[1]))
                if prior:
                    require(prior["input_hash"] == intent[2], "IDEMPOTENCY_CONFLICT", "Installation key has different content")
                    return json.loads(prior["result_json"])
            current = one(db, "SELECT revision FROM plugins WHERE id=?", (identity,))
            require((current["revision"] if current else 0) == revision - 1, "REVISION_CONFLICT", "Plugin changed during preparation")
            for server in prepared_servers.values():
                self.host.mcp._save(db, server, 0)
            for skill, body, digest in prepared_skills.values():
                self.host.skills._save(db, skill, body, digest, 0)
            db.execute("INSERT INTO plugins VALUES(?,?,?,?,?,1,0,0,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,version=excluded.version,revision=excluded.revision,removed=0,source=excluded.source,manifest_json=excluded.manifest_json,package_path=excluded.package_path,updated_at=excluded.updated_at",
                       (identity, identity, str(manifest.get("version", "0.0.0")), project_id, revision, source, encode(stored), str(root), now_ms()))
            db.execute("INSERT INTO plugin_versions VALUES(?,?,?,?)", (identity, revision, encode(stored), str(root)))
            db.execute("DELETE FROM plugin_components WHERE plugin_id=?", (identity,))
            db.executemany("INSERT INTO plugin_components VALUES(?,?,?,?,?)", [(identity, *component) for component in components])
            event(db, "plugin.saved", data={"id": identity, "revision": revision})
            if intent:
                result = self.view(one(db, "SELECT * FROM plugins WHERE id=?", (identity,)), rows(db, "SELECT * FROM plugin_components WHERE plugin_id=?", (identity,)), {key: (value, 1) for key, value in prepared_servers.items()})
                db.execute("INSERT INTO plugin_intents VALUES(?,?,?,?,?,?)", (project_id or "", intent[0], intent[1], intent[2], encode(result), now_ms()))
                return result
            return None
        repeated = await self.host.store.call(write, transaction=True)
        for server in prepared_servers.values():
            self.host.mcp.configurations[server.id] = (server, 1)
        for skill, body, digest in prepared_skills.values():
            self.host.skills.definitions[skill.id] = (skill, 1)
        await self.refresh(identity)
        self.host.notify()
        return repeated if repeated is not None else await self.get(identity, project_id)


    async def update(self, plugin_id: str, source: str, expected_revision: int, idempotency_key: str):
        consumer = (operation_context.get() or {}).get("consumer_id") or "local"
        key = "plugin-update:" + idempotency_key
        fingerprint = hashlib.sha256(encode([plugin_id, source, expected_revision]).encode()).hexdigest()
        row = self.current.get(plugin_id)
        require(row is not None, "PLUGIN_NOT_FOUND", "Plugin is unavailable")
        prior = await self.host.store.call(lambda db: one(db, "SELECT * FROM plugin_intents WHERE project_id=? AND consumer_id=? AND idempotency_key=?", (row["project_id"] or "", consumer, key)))
        if prior:
            require(prior["input_hash"] == fingerprint, "IDEMPOTENCY_CONFLICT", "Update key has different content")
            return json.loads(prior["result_json"])
        require(0 < len(idempotency_key) <= 256 and row["revision"] == expected_revision and not row["official"], "REVISION_CONFLICT", "Plugin changed or is managed by OEA")
        root, manifest = await asyncio.to_thread(self.packages.prepare, source)
        require(manifest["name"] == plugin_id, "PLUGIN_CONFLICT", "An update must retain plugin identity")
        return await self._install_package(root, manifest, source, row["project_id"], intent=(consumer, key, fingerprint))

    async def set_enabled(self, plugin_id: str, enabled: bool, expected_revision: int):
        return await self._state(plugin_id, expected_revision, enabled=enabled)

    async def remove(self, plugin_id: str, expected_revision: int):
        return await self._state(plugin_id, expected_revision, enabled=False, removed=True)

    async def _state(self, plugin_id, expected_revision, *, enabled, removed=False):
        def write(db):
            row = one(db, "SELECT * FROM plugins WHERE id=?", (plugin_id,))
            require(row and row["revision"] == expected_revision, "REVISION_CONFLICT", "Plugin changed")
            revision = expected_revision + 1
            db.execute("UPDATE plugins SET enabled=?,removed=?,revision=?,updated_at=? WHERE id=?", (int(enabled), int(removed), revision, now_ms(), plugin_id))
            db.execute("INSERT INTO plugin_versions VALUES(?,?,?,?)", (plugin_id, revision, row["manifest_json"], row["package_path"]))
            event(db, "plugin.removed" if removed else "plugin.saved", data={"id": plugin_id, "revision": revision})
            return row["project_id"]
        project_id = await self.host.store.call(write, transaction=True)
        await self.refresh(plugin_id)
        self.host.notify()
        return await self.get(plugin_id, project_id)

    async def bind(self, agent_id: str, project_id: str, plugin_id: str, enabled: bool, expected_revision: int):
        self.need(plugin_id, project_id, available=enabled)
        saved = await self.host.agents.get(agent_id)
        definition = AgentDefinition.model_validate(saved["definition"])
        require(definition.project_id in {None, project_id}, "PERMISSION_DENIED", "Agent belongs to another project")
        bindings = set(definition.plugins)
        bindings.add(plugin_id) if enabled else bindings.discard(plugin_id)
        creation = definition.creation if enabled or plugin_id != "agent-factory" else definition.creation.model_copy(update={"enabled": False})
        return await self.host.agents.save(definition.model_copy(update={"plugins": tuple(sorted(bindings)), "creation": creation}), expected_revision=expected_revision)

    async def configure_component(self, plugin_id: str, component_id: str, definition: MCPServer, expected_revision: int):
        plugin = self.current.get(plugin_id)
        require(plugin and plugin["revision"] == expected_revision, "REVISION_CONFLICT", "Plugin changed")
        def write(db):
            row = one(db, "SELECT revision FROM plugins WHERE id=?", (plugin_id,))
            require(row["revision"] == expected_revision, "REVISION_CONFLICT", "Plugin changed")
            component = one(db, "SELECT * FROM plugin_components WHERE plugin_id=? AND component_id=?", (plugin_id, component_id))
            require(component and component["kind"] == "mcp", "NOT_FOUND", "Choose an MCP component")
            prior = one(db, "SELECT * FROM mcp_servers WHERE id=?", (component["target_id"],))
            require(prior is not None, "NOT_FOUND", "The original MCP registration is unavailable")
            identity = component["target_id"] if component["ownership"] == "owned" else "plg-" + new_id("mcp")[4:]
            config = definition.model_copy(update={"id": identity, "project_id": plugin["project_id"], "plugin_owner": plugin_id})
            revision = prior["revision"] + 1 if component["ownership"] == "owned" else 1
            self.host.mcp._save(db, config, prior["revision"] if component["ownership"] == "owned" else 0)
            db.execute("UPDATE plugin_components SET target_id=?,ownership='owned' WHERE plugin_id=? AND component_id=?", (identity, plugin_id, component_id))
            db.execute("UPDATE plugins SET revision=revision+1,updated_at=? WHERE id=?", (now_ms(), plugin_id))
            db.execute("INSERT INTO plugin_versions VALUES(?,?,?,?)", (plugin_id, expected_revision + 1, plugin["manifest_json"], plugin["package_path"]))
            event(db, "plugin.saved", data={"id": plugin_id, "revision": expected_revision + 1})
            return config, revision
        config, revision = await self.host.store.call(write, transaction=True)
        self.host.mcp.configurations[config.id] = (config, revision)
        self.host.mcp.retiring.add(config.id)
        await self.host.mcp.retire_idle()
        await self.refresh(plugin_id)
        self.host.notify()
        return await self.get(plugin_id, plugin["project_id"])


    async def export(self, plugin_id: str, project_id: str, idempotency_key: str):
        plugin = await self.get(plugin_id, project_id)
        servers = [item["server_id"] for item in plugin["components"] if item["kind"] == "mcp"]
        skills = [item["skill_id"] for item in plugin["components"] if item["kind"] == "skill"]
        return await self.build(plugin_id, servers, skills, project_id, "export:" + idempotency_key, portable=True)


    async def catalog(self, *, cursor: int = 0, limit: int = 25):
        return await self.catalogs.list(cursor=cursor, limit=limit)

    async def sources(self):
        return await self.catalogs.sources()

    async def add_source(self, source: str, name: str):
        return await self.catalogs.add_source(source, name)

    async def refresh_source(self, source_id: str):
        return await self.catalogs.refresh_source(source_id)

    async def remove_source(self, source_id: str):
        return await self.catalogs.remove_source(source_id)

    async def build(self, name: str, server_ids: list[str], skill_ids: list[str], project_id: str, idempotency_key: str, *, portable: bool = False):
        await self.host.projects.get(project_id)
        configurations = []
        for identity in server_ids:
            saved = await self.host.mcp.get(identity)
            config = MCPServer.model_validate(saved["definition"])
            require(config.project_id in {None, project_id}, "PERMISSION_DENIED", "MCP belongs to another project")
            configurations.append(config)
        material = [(await self.host.skills.get(identity, project_id))["definition"] for identity in skill_ids]
        return await asyncio.to_thread(self.packages.build, name, configurations, material, project_id, idempotency_key, portable=portable)
