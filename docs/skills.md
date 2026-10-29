# Skills and execution policies

Skills provide task guidance. Tools provide executable capabilities. Both belong to accepted Agent configuration, while actual permission is enforced by the host at the point of use.

Register an explicit directory containing `SKILL.md` with `skills.register_directory`. Discovery retains its name and description; the model reads the body and references only when needed. Shared Skill bindings pin accepted metadata and content revisions. Direct SDK directory bindings can supply name and description explicitly.

Use `skills.get`, `skills.describe`, `skills.files`, and `skills.read` for bounded inspection. `refresh` adopts a new source revision. Disabling or removing a registration withdraws its current grant and preserves original files. Registration is distinct from real model or business validation.

A Run's tool and Skill policy can narrow saved grants. Instructions, Skill text, imported files, model output and messages never expand authorization. See [SDK](sdk.md), [Skill module](skills/README.md), and the [supplied control Skill](../integrations/codex/orchestrate-everything-with-agents/SKILL.md).

Keep the initial catalog short. Put triggers, routing and common conventions in `SKILL.md`; load only the current workflow and needed reference through `read_skill(skill_id, path)`. Parameter schemas come from tool discovery rather than duplicated Skill prose.

Use `required` only for instructions every run must load. Optional business manuals stay discoverable without loading their bodies. The host preserves explicit required bindings and their load evidence. Within the active model projection, repeated reads of the same immutable Skill reference retain one body and keep paired calls and source references. Original history and receipts remain complete; a different revision or a body no longer visible can be read again.

Loading once does not remove that content from later requests. Prompt caching and provider-reported cached input are separate from context size. Connection checks use `agents.probe` rather than a normal business run with a shortened goal.

`skills.remove(skill_id, project_id, expected_revision)` deletes the active registration and revokes subsequent reads. It works without the source directory or `SKILL.md`; retained bodies and old evidence remain stored. The frontend offers deletion on both cards and details, and applies removals made through external MCP. Agent configuration references are retained and need explicit repair. Repeating deletion with the original or resulting revision returns the existing result.
