# Supervision and guidance

OEA records durable Run, action, message and workflow facts for supervision. The browser and control clients read the same services; the UI's short status is separate from complete retained evidence.

Use `runs.get` and `runs.result` to inspect work state and accepted delivery. Read a bounded `events.list` page, `runs.inspect` for execution and receipt metadata, or `runs.action` for an original action or attempt. Supply additional input through `runs.resume` at a resumable boundary, or a `messages.send` reply associated with the original work. The service validates the current state and permissions before continuing. `runs.cancel` requests cancellation. A cancellation request, observed stop, and unknown external outcome are different facts.

Unknown tool outcomes remain attached to their original action. Verify the real effect before confirming it through `runs.confirm_action` or `mcp.confirm_call`. Confirmation records evidence and never dispatches the action. Explicit continuation uses the original Run and checkpoint. A late result settles that same receipt.

Messages distinguish `inform`, `request`, `reply`, and `report`. Reports preserve observed problems; a reply can answer a valid wait without inventing a new goal or widening permission. Main and manager roles have separate configuration. See [collaboration](collaboration.md), [runtime interfaces](runtime/README.md), and [SDK](sdk.md).
