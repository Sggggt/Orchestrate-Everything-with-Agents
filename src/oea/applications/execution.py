"""One configuration assembler for application acceptance, recovery and revocation."""

from pathlib import Path

from oea.contracts.configuration import Communication, ProjectWorkspace
from oea.contracts.errors import require
from oea.workspace.paths import directory_intersection


def architecture_requires_trust(owner, definition):
    if owner["representation"] == "python":
        return True
    graphs = [definition]
    while graphs:
        for node in graphs.pop().get("nodes", ()):
            if node["type"] == "function":
                return True
            if node["type"] == "subgraph":
                graphs.append(node["config"]["graph"])
    return False


def assemble(definition, grant, project_workspace: ProjectWorkspace, project_folder):
    """Intersect the current source capabilities without inheriting its private workspace."""
    source = definition.workspace.resolved(project_workspace, project_folder)
    allowed_mcp = {item.server_id: item for item in grant.tools.mcp}
    mcp = []
    for item in definition.tools.mcp:
        allowed = allowed_mcp.get(item.server_id)
        if allowed is not None:
            tools = allowed.tools if item.tools is None else item.tools if allowed.tools is None else tuple(name for name in item.tools if name in allowed.tools)
            mcp.append(item.model_copy(update={"tools": tools}))
    require(grant.trusted_extensions or not (definition.execution.program or definition.execution.graph != "default"),
            "PERMISSION_DENIED", "Custom execution requires explicit trusted extension authorization")
    require(not any(skill.required and skill.id not in grant.skills for skill in definition.skills),
            "REQUIRED_SKILL_UNAVAILABLE", "An application grant omits a required Skill")
    tools = definition.tools.model_copy(update={
        "local": tuple(name for name in definition.tools.local if name in grant.tools.local),
        "functions": tuple(name for name in definition.tools.functions if name in grant.tools.functions),
        "mcp": tuple(mcp),
    })
    references = (*source.references, *source.writable_directories, *((source.project,) if source.project else ()))
    writes = (*source.writable_directories, *((source.project / name for name in source.writable_project_paths) if source.project else ()))
    def intersect(requested, available):
        return directory_intersection(tuple(path.expanduser().resolve() for path in requested),
                                      tuple(path.expanduser().resolve() for path in available))
    settings = grant.workspace.model_copy(update={
        "inherit_project": False, "project": None, "work": None,
        "references": intersect(grant.workspace.references, references),
        "writable_directories": intersect(grant.workspace.writable_directories, writes),
        "network": "allow" if source.network == grant.workspace.network == "allow" else "deny",
        "environment": {key: value for key, value in grant.workspace.environment.items() if source.environment.get(key) == value},
        "shell": grant.workspace.shell if grant.workspace.shell == source.shell else (),
        "command_timeout_seconds": min(source.command_timeout_seconds, grant.workspace.command_timeout_seconds),
        "command_output_bytes": min(source.command_output_bytes, grant.workspace.command_output_bytes),
        "command_inline_bytes": min(source.command_inline_bytes, grant.workspace.command_inline_bytes),
    })
    before, maximum = definition.execution.budget, grant.budget
    require(before.currency == maximum.currency, "BUDGET_MISMATCH", "Application and source budgets must use the same currency")
    limits = {}
    for field in type(before).model_fields:
        if field.startswith("max_"):
            values = [value for value in (getattr(before, field), getattr(maximum, field)) if value is not None]
            limits[field] = min(values) if values else None
    budget = before.model_copy(update=limits)
    return definition.model_copy(update={
        "tools": tools, "skills": tuple(skill for skill in definition.skills if skill.id in grant.skills),
        "workspace": settings, "manager": None, "communication": Communication(),
        "instructions": definition.instructions.model_copy(update={"agent_md": definition.instructions.agent_md if grant.agent_md else ""}),
        "execution": definition.execution.model_copy(update={"budget": budget}),
    })


def instance_directory(root, scope, instance_id):
    """Only host-generated persisted identities are accepted by this internal helper."""
    return Path(root) / "application-workspaces" / scope.app_id / scope.binding_id / scope.conversation_id / "instances" / instance_id
