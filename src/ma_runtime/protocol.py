"""Machine-readable discovery for SDK, CLI, MCP and HTTP clients."""

import inspect

from . import models
from .brand import PRODUCT_NAME, VERSION
from .bridges.dispatch import methods


def describe(runtime):
    return {
        "name": PRODUCT_NAME,
        "version": VERSION,
        "protocol_version": models.PROTOCOL_VERSION,
        "capabilities": [
            "projects",
            "confirmed_recoverable_deletion",
            "project_agent_isolation",
            "markdown_summaries",
            "streaming_summary",
            "full_peer_messages",
            "sessions",
            "tasks",
            "managed_instances",
            "agent_pool_routing",
            "workflow_map",
            "workflows",
            "project_workflow_canvas",
            "durable_workflow_runs",
            "messages",
            "artifacts",
            "agent_profiles",
            "skills",
            "skill_registry",
            "task_execution_policies",
            "operations_channel",
            "guidance_suspend_resume",
            "settled_task_wait",
            "run_bound_llm",
            "mcp_registry",
            "mcp_tools",
            "mcp_resources",
            "mcp_prompts",
        ],
        "rpc_endpoint": "/api/rpc",
        "methods": {
            name: {
                "parameters": [
                    {"name": p.name, "required": p.default is inspect.Parameter.empty}
                    for p in inspect.signature(fn).parameters.values()
                    if p.name != "caller_run_id"
                ],
            }
            for name, fn in methods(runtime).items()
        },
        "schemas": {
            name: getattr(models, name).model_json_schema()
            for name in (
                "AgentConfig",
                "ProjectInfo",
                "TaskResult",
                "AgentSkill",
                "SkillBinding",
                "UsePolicy",
                "MCPBinding",
                "MCPServerConfig",
                "ExecutionRequest",
                "AgentDescriptor",
                "AdapterEvent",
                "TaskSpec",
                "InstanceBatchSpec",
                "MessageSpec",
                "RuntimeConfig",
                "LLMRequest",
                "LLMResponse",
                "WorkflowGraph",
                "WorkflowNode",
                "WorkflowEdge",
                "WorkflowInfo",
                "WorkflowSummary",
                "WorkflowRunHandle",
                "WorkflowNodeRun",
                "WorkflowRunSnapshot",
            )
        },
        "contracts": {
            "operations": "Per-session coordinator binding, compact cursor inbox, durable acknowledgements, on-demand report bodies and idempotent guidance. Agents report through run-bound callbacks and suspend on ordinary messages. Internal coordinator notifications are coalesced and can interrupt a task wait without fabricating child completion; self-failure never recursively starts maintenance. External consumers receive via inbox long wait or client event subscriptions, not automatic external chat activation. Diagnostic tests reuse ordinary instance scheduling and grants.",
            "instances": "Instances reuse independent Threads/Tasks/Runs and the ordinary scheduler. Spawn is atomic and idempotent, with 1..128 ordered payloads, same-project managed Agent pools, fixed round_robin/least_loaded assignment and a batch concurrency cap. Execution remains bounded by global and Agent budgets, including uncertain slots. Parent-scoped batches are cancelled when their parent fails or is cancelled; active parents suspend to wait.",
            "workflow_map": "A map node resolves an items list (0..128), renders payload per item/index, and persists one batch_id atomically. All-success aggregation preserves input order in outputs; empty input returns an empty list. Failures stop normal advancement; UNCERTAIN blocks downstream. Resume/reconcile one map member with instance_index. No additional scheduler.",
            "workflow_deletion": "confirmed=true authorizes recoverable deletion of one workflow. Unfinished/uncertain runs block deletion. Graph, project ownership and all run history are retained. list(deleted_only=true) reads its project's recycle bin; restore returns the previous active/archived state.",
            "workflow_graph": "Project ownership is required and immutable. Canvas positions/viewport and typed node configs share one serializable DAG. Drafts may be incomplete; run requires validation. Updates require expected_revision; runs snapshot graph/revision/inputs. Variables use {{inputs.name}}, {{nodes.node_id.field}} or typed {$ref:path,default:value}. No arbitrary code evaluation. Branch joins wait for all incoming paths to settle and execute if at least one incoming edge is active.",
            "workflow_execution": "Agent nodes use ordinary tasks, global concurrency, per-Agent grants and saved model configuration. Node task receipts and graph advancement commit atomically. Recovery observes durable tasks without replay; UNCERTAIN blocks new downstream execution. Cancel waits for actual stopped evidence. Workflow run sessions cannot move to another project.",
            "projects": "Each session and Agent belongs to one project. Project folders are explicit; switching projects changes the visible Agent team. Cross-project task/thread creation is rejected.",
            "presentation": "TaskResult.summary_markdown is the UI summary. output and conversation content retain full peer data. Presentation APIs never render raw result JSON. Markdown output does not trigger format retries.",
            "run_bound_llm": "Process Agents with llm_required=true call context.llm.complete. The host uses that Agent's captured UI configuration and credentials; keys never enter JSONL. Configuration changes apply to subsequent runs.",
            "agent_instructions": "AGENT.md is loaded on each execution; enabled SKILL.md documents are read on demand with read_skill.",
            "mcp_consumers": "Keep consumer_id stable within one client conversation. consumer scope isolates upstream state by thread/client; shared scope deliberately shares upstream state.",
            "mcp_idempotency": "Use a stable key for the same invocation. Calls are not automatically replayed after uncertain outcomes; non-persisted results may be unavailable after restart.",
            "secrets": "secret_values.env and secret_values.headers replace the selected credential map. Omit a map to preserve it; use an empty map to clear it. Values are write-only.",
            "state": "Keep runtime data, credentials, artifacts and test outputs outside the source repository.",
        },
    }
