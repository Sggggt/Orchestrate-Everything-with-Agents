"""A finite RPC method allowlist, shared by local transports."""

from ..errors import RuntimeFault
import inspect


def methods(rt):
    return {
        "projects.create": rt.projects.create,
        "projects.get": rt.projects.get,
        "projects.list": rt.projects.list,
        "projects.update": rt.projects.update,
        "projects.archive": rt.projects.archive,
        "projects.delete": rt.projects.delete,
        "projects.restore": rt.projects.restore,
        "projects.adopt": rt.projects.adopt,
        "sessions.create": rt.sessions.create,
        "sessions.get": rt.sessions.get,
        "sessions.list": rt.sessions.list,
        "sessions.snapshot": rt.sessions.snapshot,
        "sessions.close": rt.sessions.close,
        "sessions.delete": rt.sessions.delete,
        "sessions.restore": rt.sessions.restore,
        "agents.list": rt.agents.list,
        "agents.describe": rt.agents.describe,
        "agents.configure": rt.agents.configure,
        "agents.test_model": rt.agents.test_model,
        "agents.documents": rt.agents.documents,
        "agents.set_instructions": rt.agents.set_instructions,
        "agents.skills_list": rt.agents.skills_list,
        "agents.skill_put": rt.agents.skill_put,
        "agents.skill_import": rt.agents.skill_import,
        "agents.skill_delete": rt.agents.skill_delete,
        "threads.create": rt.threads.create,
        "threads.get": rt.threads.get,
        "threads.fork": rt.threads.fork,
        "threads.conversation": rt.threads.conversation,
        "threads.turn": rt.threads.turn,
        "tasks.submit": rt.tasks.submit,
        "tasks.get": rt.tasks.get,
        "tasks.progress": rt.tasks.progress,
        "tasks.list": rt.tasks.list,
        "tasks.result": rt.tasks.result,
        "tasks.cancel": rt.tasks.cancel,
        "tasks.retry": rt.tasks.retry,
        "tasks.resume": rt.tasks.resume,
        "tasks.reconcile": rt.tasks.reconcile,
        "tasks.workflow": rt.tasks.workflow,
        **{"instances." + name: getattr(rt.instances, name) for name in (
            "spawn", "get", "list", "result", "cancel",
        )},
        **{"workflows." + name: getattr(rt.workflows, name) for name in (
            "create", "get", "list", "update", "archive", "delete", "restore", "validate", "node_types",
            "run", "get_run", "runs", "result", "cancel", "resume_node", "reconcile_node",
        )},
        "messages.send": rt.messages.send,
        "messages.get": rt.messages.get,
        "messages.list": rt.messages.list,
        "artifacts.get": rt.artifacts.get,
        "events.list": rt.events.list,
        "events.live": rt.events.live,
        "resources.inspect": rt.resources.inspect,
        "mcp.list": rt.mcp.list,
        "mcp.get": rt.mcp.get,
        "mcp.configure": rt.mcp.configure,
        "mcp.delete": rt.mcp.delete,
        "mcp.connect": rt.mcp.connect,
        "mcp.disconnect": rt.mcp.disconnect,
        "mcp.tools": rt.mcp.tools,
        "mcp.call": rt.mcp.call,
        "mcp.calls": rt.mcp.calls,
        "mcp.resources": rt.mcp.resources,
        "mcp.read_resource": rt.mcp.read_resource,
        "mcp.prompts": rt.mcp.prompts,
        "mcp.get_prompt": rt.mcp.get_prompt,
        "mcp.codex_candidates": rt.mcp.codex_candidates,
        "mcp.import_codex": rt.mcp.import_codex,
        "mcp.codex_config": rt.mcp.codex_config,
    }


async def dispatch(rt, method, params):
    if method == "protocol.describe":
        from ..protocol import describe

        return describe(rt)
    calls = methods(rt)
    function = calls.get(method)
    if function is None:
        raise RuntimeFault("METHOD_NOT_FOUND", "Method is not available on this bridge")
    try:
        inspect.signature(function).bind(**params)
    except TypeError:
        raise RuntimeFault(
            "INVALID_REQUEST", "Method parameters do not match its contract"
        ) from None
    return await function(**params)
