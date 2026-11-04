"""Task-local capability narrowing for programmable Agent nodes."""

from contextvars import ContextVar

from oea.contracts.configuration import CapabilityUse, UsePolicy

node_scope = ContextVar("oea_agent_node", default=None)


def narrow_policy(parent, child):
    def capability(before, after):
        allowed = after.allowed if before.allowed is None else before.allowed if after.allowed is None else tuple(name for name in before.allowed if name in after.allowed)
        required = tuple(dict.fromkeys((*before.required, *after.required)))
        return CapabilityUse(allowed=allowed, required=required)
    return UsePolicy(tools=capability(parent.tools, child.tools), skills=capability(parent.skills, child.skills))
