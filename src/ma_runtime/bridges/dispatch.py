"""A finite RPC method allowlist, shared by local transports."""

from ..errors import RuntimeFault


def methods(rt):
    from ..operations import OPERATIONS_ACTIONS
    return {
        **{"admission." + name: getattr(rt.admission, name) for name in ("describe", "candidates", "check")},
        **{"budgets." + name: getattr(rt.budgets, name) for name in ("create", "get", "for_task", "entries", "amend")},
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
        "agents.capabilities": rt.agents.capabilities,
        **{"environments." + name: getattr(rt.environments, name) for name in ("list", "get", "configure", "prepare", "check")},
        **{"workbench." + name: getattr(rt.workbench, name) for name in ("inspect", "calls", "reconcile")},
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
        "threads.ownership": rt.threads.ownership,
        "threads.instances": rt.threads.instances,
        "threads.activity": rt.threads.activity,
        "threads.mark_read": rt.threads.mark_read,
        "threads.history": rt.threads.history,
        "threads.prepare_workdir": rt.threads.prepare_workdir,
        "threads.workdir": rt.threads.workdir,
        "threads.fork": rt.threads.fork,
        "threads.conversation": rt.threads.conversation,
        "threads.turn": rt.threads.turn,
        "tasks.submit": rt.tasks.submit,
        "tasks.get": rt.tasks.get,
        "tasks.progress": rt.tasks.progress,
        "tasks.trace": rt.tasks.trace,
        "tasks.continue_result": rt.tasks.continue_result,
        "contexts.read": rt.contexts.read,
        "contexts.merge": rt.contexts.merge,
        "contexts.get_merge": rt.contexts.get_merge,
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
            "run", "get_run", "runs", "activity", "result", "cancel", "resume_node", "reconcile_node", "repair", "get_repair", "preview_repair",
        )},
        "messages.send": rt.messages.send,
        "messages.get": rt.messages.get,
        "messages.list": rt.messages.list,
        "messages.delivery": rt.messages.delivery,
        "messages.reject": rt.messages.reject,
        "messages.expire": rt.messages.expire,
        "channels.get": rt.channels.get,
        "a2a.capabilities": rt.a2a.capabilities,
        "a2a.send": rt.a2a.send,
        "a2a.get_task": rt.a2a.get_task,
        "artifacts.get": rt.artifacts.get,
        "events.list": rt.events.list,
        "events.live": rt.events.live,
        "resources.inspect": rt.resources.inspect,
        **{"resources." + name: getattr(rt.resources, name) for name in (
            "configure", "preflight", "request", "get", "release_lease", "status", "recover",
        )},
        "mcp.list": rt.mcp.list,
        "mcp.get": rt.mcp.get,
        "mcp.configure": rt.mcp.configure,
        "mcp.configure_policy": rt.mcp.configure_policy,
        "mcp.policy": rt.mcp.policy,
        "mcp.reconcile_capacity": rt.mcp.reconcile_capacity,
        "mcp.get_call": rt.mcp.get_call,
        "mcp.register_directory": rt.mcp.register_directory,
        "mcp.delete": rt.mcp.delete,
        "mcp.connect": rt.mcp.connect,
        "mcp.disconnect": rt.mcp.disconnect,
        "mcp.tools": rt.mcp.tools,
        "mcp.catalog": rt.mcp.catalog,
        "mcp.tool": rt.mcp.tool,
        "mcp.call": rt.mcp.call,
        "mcp.calls": rt.mcp.calls,
        "mcp.resources": rt.mcp.resources,
        "mcp.read_resource": rt.mcp.read_resource,
        "mcp.prompts": rt.mcp.prompts,
        "mcp.get_prompt": rt.mcp.get_prompt,
        "mcp.codex_candidates": rt.mcp.codex_candidates,
        "mcp.import_codex": rt.mcp.import_codex,
        "mcp.codex_config": rt.mcp.codex_config,
        **{"skills." + name: getattr(rt.skills, name) for name in (
            "list", "get", "register_directory", "refresh", "set_enabled", "read", "delete",
        )},
        **{"operations." + name: getattr(rt.operations, name) for name in OPERATIONS_ACTIONS},
    }


async def dispatch(rt, method, params, *, view_version="1.1"):
    from ..views import check_request, project
    await check_request(rt, method, params, view_version)
    if method == "protocol.describe":
        from ..protocol import describe

        return describe(rt)
    from ..models import CommandEnvelope
    if method not in rt.commands.handlers:
        raise RuntimeFault("METHOD_NOT_FOUND", "Method is not available on this bridge")
    value = await rt.commands.execute(CommandEnvelope(method=method, params=params))
    return project(method, value, view_version)
