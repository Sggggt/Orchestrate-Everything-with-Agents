"""Declared grants and accepted component snapshots, independent of packaging and catalogs."""

from oea.contracts.configuration import MCPBinding, SkillBinding
from oea.contracts.errors import require
from oea.storage.persistence import rows


class PluginBindings:
    def __init__(self, lookup, mcp, skills):
        self.lookup, self.mcp, self.skills = lookup, mcp, skills

    def validate(self, definition):
        require(len(set(definition.plugins)) == len(definition.plugins), "INVALID_REQUEST", "Plugin bindings must be unique")
        for identity in (*definition.plugins, *definition.creation.plugins):
            self.lookup(identity, definition.project_id, available=False)
        require(not definition.creation.enabled or "agent-factory" in definition.plugins, "PERMISSION_DENIED", "Agent creation requires the Agent Factory plugin")

    def pin(self, db, snapshot, project_id, *, scope):
        if scope.source == "application":
            return snapshot.model_copy(update={"plugins": (), "plugin_components": {}, "creation": snapshot.creation.model_copy(update={"enabled": False})})
        mcp = {item.server_id: item for item in snapshot.tools.mcp}
        skills = {item.id: item for item in snapshot.skills}
        versions, grants = {}, {}
        for identity in snapshot.plugins:
            plugin = self.lookup(identity, project_id, available=False)
            if not plugin["enabled"] or plugin["removed"]:
                continue
            versions[identity] = plugin["revision"]
            values = rows(db, "SELECT kind,target_id FROM plugin_components WHERE plugin_id=?", (identity,))
            grant = grants[identity] = {"mcp": tuple(item["target_id"] for item in values if item["kind"] == "mcp"),
                                        "skill": tuple(item["target_id"] for item in values if item["kind"] == "skill")}
            for target in grant["mcp"]:
                mcp.setdefault(target, MCPBinding(server_id=target))
            for target in grant["skill"]:
                skills.setdefault(target, SkillBinding(id=target))
        return snapshot.model_copy(update={"tools": snapshot.tools.model_copy(update={"mcp": tuple(mcp.values())}),
            "skills": tuple(skills.values()), "plugin_versions": versions, "plugin_components": grants})

    def current_definition(self, definition, project_id, snapshot=None):
        mcp = {item.server_id: item for item in definition.tools.mcp}
        skills = {item.id: item for item in definition.skills}
        for identity in definition.plugins:
            plugin = self.lookup(identity, project_id, available=False)
            if not plugin["enabled"] or plugin["removed"]:
                continue
            if snapshot is not None:
                if identity not in snapshot.plugins:
                    continue
                grant = snapshot.plugin_components.get(identity, {})
                components = [(kind, target) for kind in ("mcp", "skill") for target in grant.get(kind, ())]
            else:
                components = [(item["kind"], item["target_id"]) for item in plugin.get("components", ())]
            for kind, target in components:
                if kind == "mcp":
                    mcp.setdefault(target, MCPBinding(server_id=target))
                elif kind == "skill":
                    skills.setdefault(target, SkillBinding(id=target))
        return definition.model_copy(update={"tools": definition.tools.model_copy(update={"mcp": tuple(mcp.values())}), "skills": tuple(skills.values())})

    def available(self, kind, identity):
        saved = (self.mcp.configurations if kind == "mcp" else self.skills.definitions).get(identity)
        owner = saved[0].plugin_owner if saved else None
        if owner:
            plugin = self.lookup(owner, saved[0].project_id, available=False)
            return bool(plugin["enabled"] and not plugin["removed"])
        return True

    def authorize(self, kind, identity):
        require(self.available(kind, identity), "PLUGIN_DISABLED", "The owning plugin is disabled or removed")
