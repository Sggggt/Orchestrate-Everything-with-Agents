"""Finite execution constraints and durable evidence shared by all adapters."""

from .errors import require
from .models import TaskSpec, UsePolicy, content_hash, now_ms
from .store import rows

PLATFORM_TOOLS = frozenset({"list_threads", "send_message", "submit_task", "spawn_instances", "put_file", "put_bytes"})
LIFECYCLE_TOOLS = frozenset({"wait_for_tasks", "wait_for_message", "wait_for_input", "report_issue", "operations"})


def request_hash(spec):
    # Preserve idempotency keys issued before optional policies were introduced.
    value = spec.model_dump(mode="json")
    for key in ("tool_policy", "skill_policy"):
        if getattr(spec, key) == UsePolicy():
            value.pop(key)
    return content_hash(value)


def mcp_name(server_id, tool_name):
    return f"mcp:{server_id}:{tool_name}"


def validate_policies(runtime, spec):
    if all(policy.mode == "auto" and policy.allowed is None and not policy.required
           for policy in (spec.tool_policy, spec.skill_policy)):
        return
    config = runtime.agent_configs.get(spec.agent_id)
    skills = {s.id for s in runtime.skills.for_agent(config) if s.enabled}
    selected = set(spec.skill_policy.required) | set(spec.skill_policy.allowed or ())
    require(selected <= skills, "PERMISSION_DENIED", "Skill policy selects an unlinked or disabled Skill")
    require(spec.skill_policy.mode != "required" or any(spec.skill_policy.permits(s) for s in skills),
            "INVALID_REQUEST", "Required Skill policy has no available Skill")
    bindings = {b.server_id: b for b in config.mcp_bindings} if config else {}
    for name in set(spec.tool_policy.required) | set(spec.tool_policy.allowed or ()):
        if name.startswith("mcp:"):
            parts = name.split(":", 2)
            require(len(parts) == 3 and bool(parts[2]), "INVALID_REQUEST", "Use mcp:server_id:tool_name identifiers")
            binding = bindings.get(parts[1])
            require(binding is not None and (binding.tools is None or parts[2] in binding.tools),
                    "PERMISSION_DENIED", "Tool policy selects an unbound MCP tool")
            server = runtime.mcp._config(parts[1])
            require(server.enabled and (server.allowed_tools is None or parts[2] in server.allowed_tools),
                    "PERMISSION_DENIED", "Tool policy selects a disabled MCP tool")
            catalog = runtime.mcp.catalogs.get(parts[1], {})
            require(not catalog.get("discovered_at") or any(tool["name"] == parts[2] for tool in catalog.get("tools", [])),
                    "INVALID_REQUEST", "Tool policy selects an unknown MCP tool")
        else:
            require(name in PLATFORM_TOOLS, "INVALID_REQUEST", "Unknown platform tool identifier")
            require(name not in {"list_threads", "send_message", "submit_task", "spawn_instances"}
                    or config is None or config.collaboration, "PERMISSION_DENIED", "Collaboration is disabled")


def record(db, task_id, run_id, kind, name):
    db.execute("INSERT OR IGNORE INTO execution_evidence VALUES(?,?,?,?,?)", (task_id, kind, name, run_id, now_ms()))


def model_tools(context, tools):
    """Constrain only the next necessary step, then restore normal selection."""
    request = context.request
    if request.skill_policy.mode == "required" and not context.loaded_skill_ids:
        require(any(t["function"]["name"] == "read_skill" for t in tools),
                "EXECUTION_REQUIREMENT_UNMET", "No Skill reader is available")
        return tools, "required"
    if request.tool_policy.mode != "required" and not request.tool_policy.required:
        return tools, None
    names = {}
    for tool in tools:
        alias = tool["function"]["name"]
        provider = context._runtime.tools.providers.get(alias)
        identifier = mcp_name(provider.server_id, provider.tool["name"]) if hasattr(provider, "server_id") else alias
        if identifier not in LIFECYCLE_TOOLS and identifier != "read_skill":
            names[identifier] = alias
    pending = [name for name in request.tool_policy.required if name not in context.used_tools]
    if pending or (request.tool_policy.mode == "required" and not context.used_tools):
        missing = sorted(set(pending) - set(names))
        require(not missing, "EXECUTION_REQUIREMENT_UNMET", "Required tool is unavailable to this execution",
                details={"tools": missing})
        require(bool(names), "EXECUTION_REQUIREMENT_UNMET", "No tool is available to satisfy the required policy")
        # Keep discovery/selection and Skill loading available before the target
        # action. A required set is a completion condition, not a forced order.
        # Supervision and safe suspension remain callable even when only one
        # business tool is available; completion still requires exact evidence.
        return tools, "required"
    return tools, None


def check_completion(db, task, spec=None):
    spec = spec or TaskSpec.model_validate_json(task.spec_json)
    if not (spec.tool_policy.required or spec.skill_policy.required or
            spec.tool_policy.mode == "required" or spec.skill_policy.mode == "required"):
        return
    evidence = rows(db, "SELECT kind,name FROM execution_evidence WHERE task_id=?", (task.id,))
    for kind, policy in (("tool", spec.tool_policy), ("skill", spec.skill_policy)):
        observed = {e.name for e in evidence if e.kind == kind and policy.permits(e.name)}
        missing = sorted(set(policy.required) - observed)
        require(not missing and (policy.mode != "required" or bool(observed)),
                "EXECUTION_REQUIREMENT_UNMET", "Task completed without required execution evidence",
                details={"kind": kind, "missing": missing, "requires_any": policy.mode == "required" and not observed})
