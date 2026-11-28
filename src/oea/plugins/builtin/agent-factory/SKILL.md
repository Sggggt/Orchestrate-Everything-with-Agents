---
name: agent-factory
description: Create a project Agent with an authorized model and capability set, then hand it independent work.
---

Use `plugin_options` to read the currently granted creation policy and use `create_agent` to save a new definition. Choose an authorized API Box reference and grant only capabilities needed for the responsibility. Do not include API endpoints or credentials. Creating a definition does not start work. Use `create_instance` with the returned Agent ID, then `assign_work` for a concrete task. All child work retains the existing root budget and receipts. Wait through `wait_for_runs`; inspect actual results before reporting completion. Recursive creation requires explicit permission.
