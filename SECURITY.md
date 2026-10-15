# Security

This is a personal, local-first platform. The default host binds to loopback. Anyone holding its client token can manage its workspace. Keep the token, local encryption key, provider credentials, MCP secrets and backups private.

Run only trusted Agent programs and upstream MCP servers. A managed subprocess is not a sandbox. Skills, model output, messages and tool results do not grant additional authority. Do not expose the gateway directly to the public internet.

For a suspected vulnerability, use the repository host's private vulnerability reporting feature when available. Otherwise contact a maintainer through an existing private channel before disclosing sensitive details. Do not attach real tokens, populated SQLite files, private documents or customer records to public issues. Include a minimal synthetic reproduction, affected version and observed impact.

The project does not currently promise a fixed security support window. Fixes should preserve existing data and document any required operator action.
