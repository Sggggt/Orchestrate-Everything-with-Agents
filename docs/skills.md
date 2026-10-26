# Skills and execution policies

Skills provide task guidance. Tools provide executable capabilities. Both belong to accepted Agent configuration, while actual permission is enforced by the host at the point of use.

Register an explicit directory containing `SKILL.md` with `skills.register_directory`. Discovery retains its name and description; the model reads the body and references only when needed. Shared Skill bindings pin accepted metadata and content revisions. Direct SDK directory bindings can supply name and description explicitly.

Use `skills.get`, `skills.describe`, `skills.files`, and `skills.read` for bounded inspection. `refresh` adopts a new source revision. Disabling or removing a registration withdraws its current grant and preserves original files. Registration is distinct from real model or business validation.

A Run's tool and Skill policy can narrow saved grants. Instructions, Skill text, imported files, model output and messages never expand authorization. See [SDK](sdk.md), [Skill module](skills/README.md), and the [supplied control Skill](../integrations/codex/orchestrate-everything-with-agents/SKILL.md).
