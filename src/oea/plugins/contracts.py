"""Portable plugin identities and the built-in capability catalog."""

import hashlib
import json

PLUGIN_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
MCP_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"
NAMESPACE = "org.oea"
OFFICIAL = {
    "plugin-creator": {"name": "Plugin Creator", "description": "Package MCP connections and Skills for reuse.", "operations": ["plugins.build"]},
    "agent-factory": {"name": "Agent Factory", "description": "Create project Agents within an explicit creation grant.", "operations": ["agents.create"]},
}


def component_id(plugin_id, name, version):
    value = json.dumps([plugin_id, name, version], ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return "plg-" + hashlib.sha256(value.encode()).hexdigest()[:36]


def package_json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True)
