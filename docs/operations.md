# Supervision and guidance

The browser, SDK and MCP read the same durable Run, action, message and workflow facts. Short UI status and complete retained evidence are separate views.

Use `runs.get` and `runs.result` for execution state and accepted delivery; `runs.inspect`, `runs.action` and bounded `events.list` for original evidence. `runs.command` reads a command's original saved streams, actual exit state and environment revision, including after a restart. Reading never reruns the command.

Continue a resumable boundary through `runs.resume` with the original Run and checkpoint. Free-form board messages retain guidance but do not implicitly resume a graph or create work. Directed messages enter existing model-input boundaries.

`runs.cancel` requests cancellation. A request, observed stop and unknown external outcome are different facts. A waiting Run with a live command retains execution ownership; revocation stops that owned command. Losing ownership leaves the related outcome unknown and does not reserve global capacity.

Verify unknown effects before `runs.confirm_action` or `mcp.confirm_call`. Confirmation records evidence without dispatching the action. Late results settle the original receipt. Use complete artifacts and result references for handoff; strict delivery schemas and required evidence are optional constraints.

See [collaboration](collaboration.md), [runtime](runtime/README.md), [budgets](budgets/README.md) and [SDK](sdk.md).
