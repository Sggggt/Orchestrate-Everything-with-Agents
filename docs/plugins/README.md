# Plugins

OEA plugins package MCP connections, Agent Skills and declared OEA capabilities. Packages use Agent Plugins 1.0.0: a root `plugin.json`, optional `mcp.json` and `skills/`, with OEA-specific declarations under `extensions`. Native operations are implemented by the OEA SDK; packages cannot load arbitrary host code.

The workbench groups installed plugins, standalone MCP and standalone Skills under Plugins. Select an installed plugin’s name, or choose **插件设置** from its menu, to open its settings page. You can toggle the plugin, inspect its components and edit an MCP connection there. Third-party packages also support updating from a source. Installing a package does not grant it to every Agent. Bind capabilities explicitly; current revocation applies before each subsequent action. The official Plugin Creator assembles reusable packages, and Agent Factory provides authorized creation of project Agents.

Wrapping a third-party MCP never edits its original configuration or remote server. Existing registrations may be referenced. Editing a referenced connection through a plugin creates a plugin-owned copy and changes only that binding. Removing a plugin retains independently registered components and original files.

Authenticated external controllers manage plugins through the same typed operations as the browser. The control MCP remains an external client connection exposing `platform_guide` and `platform_call`; do not register it as an upstream business MCP. Exact parameters are discoverable from the shared registry.

Models, probes, plugins and created Agents use the existing Run budget and child-work inheritance. Plugins introduce no budget, quota or scheduling mechanism.

## Module boundaries

`contracts.py` defines package identities and built-in declarations. `packages.py` performs filesystem preparation and portable export without Host or database access. `catalog.py` owns explicit directory refresh and cached discovery. `bindings.py` computes scoped grants and pins accepted components. `service.py` coordinates lifecycle transactions through the existing MCP, Skill and Agent services; `factory.py` supplies run-authorized creation and packaging tools. Host only composes these services.

Updates preserve an accepted Run's component identities while later tasks use the new package. Disabling a plugin revokes subsequent calls to both accepted and current components. Shared registration and ownership changes commit in one transaction. Local wrapping uses existing references; portable export emits configuration templates without local endpoints, credentials or executable paths.
