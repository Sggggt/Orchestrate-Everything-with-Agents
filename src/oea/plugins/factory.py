"""Run-bound Agent creation and packaging; no administrator client or new budget."""

import hashlib

from oea.catalog.service import need
from oea.contracts.configuration import AgentDefinition, ProjectWorkspace, manager_snapshot
from oea.contracts.errors import RuntimeFault, require
from oea.storage.persistence import encode, now_ms, one
from oea.tools.contracts import ToolSpec


class AgentFactory:
    def __init__(self, host):
        self.host = host

    def names(self, context):
        if context.scope.source != "platform":
            return []
        current = context.current_definition()
        result = []
        for plugin, name in (("agent-factory", "create_agent"), ("plugin-creator", "build_plugin")):
            saved = self.host.plugins.current.get(plugin)
            if (plugin in context.definition.plugin_versions and plugin in current.plugins and saved and saved["enabled"] and not saved["removed"]
                    and (name != "create_agent" or context.definition.creation.enabled and current.creation.enabled)):
                result.append(name)
        return ["plugin_options", *result] if result else []

    def authorize(self, context, name):
        require(name in self.names(context), "PERMISSION_DENIED", "This plugin capability is not currently granted")

    def validate_child(self, context, definition, db=None, current=None):
        self.authorize(context, "create_agent")
        parent = current or context.current_definition()
        before, current = context.definition.creation, parent.creation
        require(definition.project_id == context.instance["project_id"] and not definition.removed, "PERMISSION_DENIED", "Create an Agent only in the current project")
        allowed_models = set(before.model_refs) & set(current.model_refs)
        requested_models = {value for value in (definition.model_ref, definition.summary_model_ref,
                            definition.manager.model_ref if definition.manager else None,
                            definition.manager.summary_model_ref if definition.manager else None) if value}
        require(requested_models <= allowed_models, "PERMISSION_DENIED", "A child model is outside the creation grant")
        allowed_plugins = set(before.plugins) & set(current.plugins)
        require(set(definition.plugins) <= allowed_plugins, "PERMISSION_DENIED", "A child plugin is outside the creation grant")
        require(not definition.creation.enabled or before.recursive and current.recursive, "PERMISSION_DENIED", "Recursive creation is not granted")
        require(set(definition.creation.model_refs) <= allowed_models and set(definition.creation.plugins) <= allowed_plugins,
                "PERMISSION_DENIED", "A child cannot enlarge the creation grant")
        require(not definition.creation.recursive or before.recursive and current.recursive, "PERMISSION_DENIED", "Recursive creation cannot be self-granted")
        normalized_skills = []
        for owner in (definition, definition.manager):
            if owner is None:
                continue
            tools = owner.tools or definition.tools
            require(set(tools.local) <= set(context.definition.tools.local) & set(parent.tools.local)
                    and set(tools.functions) <= set(context.definition.tools.functions) & set(parent.tools.functions),
                    "PERMISSION_DENIED", "Child tools exceed the creator's current capability scope")
            requested = {item.server_id: item for item in tools.mcp}
            for source in (context.definition.tools.mcp, parent.tools.mcp):
                available = {item.server_id: item for item in source}
                require(set(requested) <= set(available) and all(available[key].tools is None or value.tools is not None and set(value.tools) <= set(available[key].tools) for key, value in requested.items()),
                        "PERMISSION_DENIED", "Child MCP tools exceed the creation scope")
            skills = owner.skills if owner.skills is not None else definition.skills
            require({item.id for item in skills} <= {item.id for item in context.definition.skills} & {item.id for item in parent.skills}, "PERMISSION_DENIED", "Child Skills exceed the creation scope")
            before_skills = {item.id: item for item in context.definition.skills}
            current_skills = {item.id: item for item in parent.skills}
            canonical = []
            for requested_skill in skills:
                accepted = before_skills[requested_skill.id]
                current_skill = current_skills[requested_skill.id]
                require(accepted.path is not None and (requested_skill.path is None or requested_skill.path.resolve() == accepted.path.resolve())
                        and requested_skill.revision in {None, accepted.revision}, "PERMISSION_DENIED", "Child Skill source or revision exceeds the creation grant")
                if accepted.revision is not None:
                    self.host.skills.authorize(accepted, current_skill, context.instance["project_id"])
                    if db is not None:
                        source = self.host.skills.scoped(db, accepted.id, context.instance["project_id"])
                        from oea.skills.service import SkillSource
                        live = SkillSource.model_validate_json(source["config_json"])
                        require(live.enabled and live.path == accepted.path, "PERMISSION_DENIED", "The persisted Skill source was revoked or moved")
                    canonical.append(accepted.model_copy(update={"path": None, "required": requested_skill.required}))
                else:
                    require(current_skill.path == accepted.path, "PERMISSION_DENIED", "The direct Skill source changed")
                    canonical.append(accepted.model_copy(update={"required": requested_skill.required}))
            normalized_skills.append(tuple(canonical))
            declared = owner.workspace or definition.workspace
            project = need(db, "projects", context.instance["project_id"]) if db is not None else None
            defaults = ProjectWorkspace.model_validate_json(project["workspace_json"]) if project else ProjectWorkspace.model_validate(context.definition.workspace.model_dump(include=set(ProjectWorkspace.model_fields)))
            folder = project["folder"] if project else context.definition.workspace.project
            workspace = declared.resolved(defaults, folder)
            allowed = parent.workspace.resolved(defaults, folder)
            require(workspace.sandbox == "native" and not declared.environment and not declared.work,
                    "PERMISSION_DENIED", "Child execution must retain the authorized native environment")
            for limit in (context.definition.workspace, allowed):
                roots = (*limit.references, *limit.writable_directories, *((limit.project,) if limit.project else ()))
                for path in (*workspace.references, *((workspace.project,) if workspace.project else ())):
                    require(any(path.resolve().is_relative_to(root.resolve()) for root in roots), "PERMISSION_DENIED", "Child directory exceeds the creation scope")
                writes = (*limit.writable_directories, *((limit.project / part for part in limit.writable_project_paths) if limit.project else ()))
                require(all(any(path.resolve().is_relative_to(root.resolve()) for root in writes) for path in workspace.writable_directories), "PERMISSION_DENIED", "Child write directory exceeds the creation scope")
                require(set(workspace.writable_project_paths) <= set(limit.writable_project_paths) and (workspace.network == "deny" or limit.network == "allow"), "PERMISSION_DENIED", "Child workspace exceeds the current grant")
            partners = set(parent.communication.departments) & set(context.definition.communication.departments)
            require(set(owner.communication.departments) <= partners | {context.instance["agent_id"]}, "PERMISSION_DENIED", "Child partners exceed the creation scope")
            require(not owner.communication.can_request or context.definition.communication.can_request and parent.communication.can_request or definition.creation.enabled,
                    "PERMISSION_DENIED", "Child delegation is outside the current grant")
            execution = owner.execution or definition.execution
            require(execution.program == parent.execution.program and execution.graph == parent.execution.graph and execution.graph_version == parent.execution.graph_version
                    and execution.architecture == parent.execution.architecture, "PERMISSION_DENIED", "Child execution extensions must retain the authorized architecture")
        self.host.model_profiles.validate(definition)
        self.host.plugins.validate_bindings(definition)
        changes = {"skills": normalized_skills[0]}
        if definition.manager is not None and definition.manager.skills is not None:
            changes["manager"] = definition.manager.model_copy(update={"skills": normalized_skills[1]})
        return definition.model_copy(update=changes)

    def created(self, db, context, agent_id):
        require(self.names(context) and context.definition.creation.enabled, "PERMISSION_DENIED", "Agent creation grant was revoked")
        return one(db, "SELECT c.agent_id FROM agent_creations c JOIN runs r ON r.id=c.run_id WHERE c.agent_id=? AND r.id=?", (agent_id, context.run["id"])) is not None

    async def create(self, context, definition, action_id):
        definition = self.validate_child(context, definition)
        fingerprint = hashlib.sha256(definition.model_dump_json().encode()).hexdigest()
        def write(db):
            self.host.tools.guard(db, context.run["id"], None)
            current = AgentDefinition.model_validate_json(need(db, "agent_definitions", context.instance["agent_id"])["config_json"])
            from oea.collaboration.messages import Messages
            Messages.source(db, context.run["id"], need(db, "sessions", context.instance["session_id"]), current_definition=current)
            plugin = need(db, "plugins", "agent-factory")
            require(plugin["enabled"] and not plugin["removed"] and current.creation.enabled and "agent-factory" in current.plugins,
                    "PERMISSION_DENIED", "The persisted Agent creation grant was revoked")
            selected = manager_snapshot(current) if context.instance["role"] == "manager" else current
            selected = self.host.plugins.current_definition(selected, context.instance["project_id"], context.definition)
            normalized = self.validate_child(context, definition, db, current=selected)
            prior = one(db, "SELECT * FROM agent_creations WHERE run_id=? AND operation_key=?", (context.run["id"], action_id))
            if prior:
                require(prior["input_hash"] == fingerprint, "IDEMPOTENCY_CONFLICT", "Creation key has different content")
                saved = need(db, "agent_definitions", prior["agent_id"])
                return {"agent_id": saved["id"], "revision": saved["revision"], "definition": AgentDefinition.model_validate_json(saved["config_json"]).model_dump(mode="json")}
            revision = self.host.agents._save(db, normalized, 0)
            db.execute("INSERT INTO agent_creations VALUES(?,?,?,?,?)", (context.run["id"], action_id, fingerprint, normalized.id, now_ms()))
            result = {"agent_id": normalized.id, "revision": revision, "definition": normalized.model_dump(mode="json")}
            return self.host.tools.commit_owned(db, action_id, context.run["id"], result)
        result = await self.host.store.call(write, transaction=True)
        self.host.definitions[result["agent_id"]] = AgentDefinition.model_validate(result["definition"])
        self.host.notify()
        return result


class FactoryTool:
    def __init__(self, host, name):
        self.host, self.name = host, name
        definitions = {}
        if name == "plugin_options":
            properties, required = {}, []
            description = "Read the currently granted model references, plugins and component IDs for Agent creation and plugin packaging. No credentials or API configuration are returned."
        elif name == "create_agent":
            declaration = AgentDefinition.model_json_schema()
            definitions = declaration.pop("$defs", {})
            properties = {"definition": declaration}
            required = ["definition"]
            description = "Create a new project Agent within the explicit creation grant. Returns a definition, not a started task. Use create_instance and assign_work next."
        else:
            properties = {"name": {"type": "string"}, "server_ids": {"type": "array", "items": {"type": "string"}}, "skill_ids": {"type": "array", "items": {"type": "string"}}, "idempotency_key": {"type": "string"}}
            required = list(properties)
            description = "Package granted MCP connections and Skills as an Agent Plugin without credentials or private data."
        self.spec = ToolSpec(name=name, description=description, input_schema={"type": "object", "properties": properties, "required": required, "additionalProperties": False, "$defs": definitions}, side_effects="none" if name == "plugin_options" else "local", cancellation="cooperative")

    def describe(self):
        return self.spec

    async def authorize_replay(self, run_id, args):
        context = self.host.active[run_id]
        self.host.factory.authorize(context, self.name)
        if self.name == "create_agent":
            self.host.factory.validate_child(context, self.host.model_profiles.canonical(AgentDefinition.model_validate(args["definition"])))

    async def invoke(self, request):
        context = self.host.active[request["run_id"]]
        self.host.factory.authorize(context, self.name)
        args = request["arguments"]
        if self.name == "plugin_options":
            current = context.current_definition()
            before = context.definition
            refs = set(before.creation.model_refs) & set(current.creation.model_refs)
            servers = {item.server_id for item in before.tools.mcp} & {item.server_id for item in current.tools.mcp}
            skills = {item.id for item in before.skills} & {item.id for item in current.skills}
            return {"project_id": context.instance["project_id"], "models": [self.host.model_profiles.summary(reference) for reference in sorted(refs)],
                    "plugins": sorted(set(before.creation.plugins) & set(current.creation.plugins)),
                    "recursive": before.creation.recursive and current.creation.recursive,
                    "server_ids": sorted(servers), "skill_ids": sorted(skills),
                    "tools": {"local": sorted(set(before.tools.local) & set(current.tools.local)), "functions": sorted(set(before.tools.functions) & set(current.tools.functions))},
                    "workspace": before.workspace.model_dump(mode="json", include={"inherit_project", "project", "references", "writable_directories", "writable_project_paths", "network", "shell", "sandbox"}),
                    "execution": before.execution.model_dump(mode="json", include={"graph", "graph_version", "architecture", "program", "program_uses_model"})}
        if self.name == "create_agent":
            try:
                return await self.host.factory.create(context, self.host.model_profiles.canonical(AgentDefinition.model_validate(args["definition"])), request["action_id"])
            except RuntimeFault as error:
                return {"isError": True, "error": error.as_dict()}
        current = context.current_definition()
        require(set(args["server_ids"]) <= {item.server_id for item in context.definition.tools.mcp} & {item.server_id for item in current.tools.mcp}
                and set(args["skill_ids"]) <= {item.id for item in context.definition.skills} & {item.id for item in current.skills}, "PERMISSION_DENIED", "Package only currently granted components")
        return await self.host.plugins.build(project_id=context.instance["project_id"], **args)
