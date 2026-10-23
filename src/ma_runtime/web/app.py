"""Authenticated local API and a buildless browser workspace."""

from __future__ import annotations

import asyncio
import hmac
import json
import inspect
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import unquote, urlsplit

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from ..bridges.dispatch import dispatch
from ..brand import PRODUCT_NAME, VERSION
from ..errors import RuntimeFault, require
from ..models import AgentConfig, RuntimeConfig, json_dump
from ..runtime import Runtime
from ..secrets import atomic_write


from agent.mock import configure_mocks


def create_app(
    workspace=None, config=None, *, mock=False, runtime=None, client_url=None, allow_trusted_local=False
):
    @asynccontextmanager
    async def lifespan(app):
        rt = runtime or Runtime.open(workspace, config, allow_trusted_local=allow_trusted_local)
        if runtime is None:
            await rt.__aenter__()
        try:
            if mock:
                await configure_mocks(rt)
            app.state.runtime = rt
            token_file = rt.root / "client-token"
            if not token_file.exists():
                atomic_write(token_file, secrets.token_urlsafe(32).encode())
            app.state.token = token_file.read_text(encoding="utf-8").strip()
            if client_url:
                atomic_write(
                    rt.root / "client.json", json_dump({"url": client_url}).encode()
                )
            yield
        finally:
            if runtime is None:
                await rt.aclose()

    app = FastAPI(
        title=PRODUCT_NAME,
        version=VERSION,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    assets = Path(__file__).with_name("static")

    @app.exception_handler(RuntimeFault)
    async def fault_handler(request, exc):
        status = (
            404
            if exc.code == "NOT_FOUND"
            else (
                403
                if exc.code == "PERMISSION_DENIED"
                else (
                    409
                    if exc.code
                    in {
                        "IDEMPOTENCY_CONFLICT",
                        "INVALID_STATE",
                        "OUTCOME_UNCERTAIN",
                        "THREAD_BUSY",
                        "REVISION_CONFLICT",
                        "RESOURCE_BUSY",
                    }
                    else 503
                    if exc.code == "STORE_UNAVAILABLE"
                    else 400
                )
            )
        )
        return JSONResponse({"error": exc.as_dict()}, status_code=status)

    @app.exception_handler(RequestValidationError)
    @app.exception_handler(ValidationError)
    async def validation_handler(request, exc):
        fields = [
            ".".join(str(part) for part in error["loc"]) for error in exc.errors()
        ]
        return JSONResponse(
            {
                "error": {
                    "code": "INVALID_REQUEST",
                    "safe_message": "Invalid request fields: " + ", ".join(fields),
                    "details": {"fields": fields},
                }
            },
            status_code=422,
        )

    @app.middleware("http")
    async def local_security(request, call_next):
        host = urlsplit("http://" + request.headers.get("host", "")).hostname
        if host not in {"127.0.0.1", "localhost", "::1"}:
            return JSONResponse(
                {
                    "error": {
                        "code": "PERMISSION_DENIED",
                        "safe_message": "Loopback host required",
                    }
                },
                status_code=403,
            )
        origin = request.headers.get("origin")
        if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
            return JSONResponse(
                {
                    "error": {
                        "code": "PERMISSION_DENIED",
                        "safe_message": "Same-origin request required",
                    }
                },
                status_code=403,
            )
        if request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse(
                {
                    "error": {
                        "code": "PERMISSION_DENIED",
                        "safe_message": "Cross-site access is not allowed",
                    }
                },
                status_code=403,
            )
        if request.url.path.startswith("/api/"):
            token = getattr(app.state, "token", "")
            cookie = f"ma-session-{request.url.port or 80}"
            provided = request.headers.get("authorization", "").removeprefix(
                "Bearer "
            ) or request.cookies.get(cookie, "")
            if not token or not hmac.compare_digest(token, provided):
                return JSONResponse(
                    {
                        "error": {
                            "code": "PERMISSION_DENIED",
                            "safe_message": "Local client authentication required",
                        }
                    },
                    status_code=401,
                )
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                binary = request.url.path == "/api/artifacts"
                expected = "application/octet-stream" if binary else "application/json"
                if not request.headers.get("content-type", "").startswith(expected):
                    return JSONResponse(
                        {
                            "error": {
                                "code": "INVALID_REQUEST",
                                "safe_message": "Unsupported request content type",
                            }
                        },
                        status_code=415,
                    )
                limit = (
                    app.state.runtime.config.max_artifact_bytes
                    if binary
                    else 1024 * 1024
                )
                try:
                    content_length = int(request.headers.get("content-length", "0"))
                except ValueError:
                    content_length = limit + 1
                if content_length > limit:
                    return JSONResponse(
                        {
                            "error": {
                                "code": "PAYLOAD_TOO_LARGE",
                                "safe_message": "Request body is too large",
                            }
                        },
                        status_code=413,
                    )
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    def rt():
        return app.state.runtime

    async def command(request, method, **params):
        return await dispatch(rt(), method, params, view_version=request.headers.get("X-OEA-View-Version", "1.0"))

    async def invoke(function, *args, **kwargs):
        try:
            inspect.signature(function).bind(*args, **kwargs)
        except TypeError:
            raise RuntimeFault(
                "INVALID_REQUEST", "Method parameters do not match its contract"
            ) from None
        return await function(*args, **kwargs)

    async def body(request):
        value = await request.body()
        require(
            len(value) <= 1024 * 1024, "PAYLOAD_TOO_LARGE", "Request body is too large"
        )
        try:
            data = json.loads(value)
        except (ValueError, UnicodeError):
            raise RuntimeFault(
                "INVALID_REQUEST", "Request body must be UTF-8 JSON"
            ) from None
        require(
            isinstance(data, dict), "INVALID_REQUEST", "Request body must be an object"
        )
        return data

    @app.get("/")
    async def index(request: Request):
        response = FileResponse(assets / "index.html")
        response.set_cookie(
            f"ma-session-{request.url.port or 80}",
            app.state.token,
            httponly=True,
            samesite="strict",
            path="/",
        )
        return response

    app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/api/bootstrap")
    async def bootstrap(request: Request, project_id: str | None = None):
        projects = await rt().projects.list()
        selected = next((p for p in projects if p.id == project_id), projects[0] if projects else None)
        return {
            "projects": projects,
            "project": selected,
            "agents": await command(request, "agents.list", project_id=selected.id) if selected else [],
            "environments": await rt().environments.list(project_id=selected.id) if selected else [],
            "mcp_servers": await rt().mcp.list(),
            "skill_registry": await rt().skills.list(limit=200),
            "sessions": (await rt().sessions.list(project_id=selected.id))["items"] if selected else [],
            "config": rt().config.model_dump(),
            "runtime": {
                "trusted_local_execution": rt().allow_trusted_local,
                "active": len(rt().active),
                "uncertain_slots": len(rt().uncertain_slots),
                "accepting": rt().accepting,
                "external_coordinator": "codex",
                "transport": "SSE",
            },
        }

    @app.get("/api/projects")
    async def projects():
        return await rt().projects.list(include_archived=True)

    @app.post("/api/projects")
    async def create_project(request: Request):
        return await invoke(rt().projects.create, **await body(request))

    @app.patch("/api/projects/{project_id}")
    async def update_project(project_id: str, request: Request):
        return await invoke(rt().projects.update, project_id=project_id, **await body(request))

    @app.post("/api/projects/{project_id}/archive")
    async def archive_project(project_id: str, request: Request):
        return await invoke(rt().projects.archive, project_id=project_id, **await body(request))

    @app.delete("/api/projects/{project_id}")
    async def delete_project(project_id: str, request: Request):
        return await invoke(rt().projects.delete, project_id=project_id, **await body(request))

    @app.post("/api/projects/{project_id}/restore")
    async def restore_project(project_id: str):
        return await rt().projects.restore(project_id)

    @app.get("/api/trash")
    async def trash(cursor: str | None = None, limit: int = 25):
        projects = await rt().projects.list(include_archived=True, include_deleted=True)
        sessions = await rt().sessions.list(cursor=cursor, limit=limit, deleted_only=True)
        return {"projects": [p for p in projects if p.state == "DELETED"], "sessions": sessions}

    @app.put("/api/config")
    async def configure(request: Request):
        config = RuntimeConfig.model_validate(await body(request))
        async with rt().configuration_lock:
            require(
                len(rt().active) + len(rt().uncertain_slots)
                <= config.max_concurrency - config.reserved_external_slots,
                "RESOURCE_BUSY",
                "Wait for active calls to drain before reducing execution capacity",
            )
            atomic_write(rt().root / "runtime-config.json", json_dump(config).encode())
            rt().config = config
        rt().notify()
        return config.model_dump()

    @app.get("/api/agents")
    async def agents(request: Request, project_id: str | None = None):
        return await command(request, "agents.list", project_id=project_id)

    @app.put("/api/agents/{agent_id}")
    async def agent_config(agent_id: str, request: Request):
        data = await body(request)
        key = data.pop("api_key", None)
        require(
            key is None or isinstance(key, str),
            "INVALID_REQUEST",
            "API key must be a string",
        )
        require(
            data.get("id", agent_id) == agent_id,
            "INVALID_REQUEST",
            "Agent ID does not match the URL",
        )
        data["id"] = agent_id
        return await command(request, "agents.configure", config=data, api_key=key if key else None)

    @app.delete("/api/agents/{agent_id}/secret")
    async def delete_secret(agent_id: str):
        return await rt().agents.delete_secret(agent_id)

    @app.post("/api/agents/{agent_id}/model-test")
    async def test_agent_model(agent_id: str):
        return await rt().agents.test_model(agent_id)

    @app.post("/api/sessions")
    async def create_session(request: Request):
        return await invoke(rt().sessions.create, **await body(request))

    @app.get("/api/sessions/{session_id}/snapshot")
    async def snapshot(session_id: str):
        return await rt().sessions.snapshot(session_id, presentation=True)

    @app.post("/api/sessions/{session_id}/close")
    async def close_session(session_id: str, request: Request):
        return await invoke(rt().sessions.close, session_id, **await body(request))

    @app.delete("/api/sessions/{session_id}")
    async def delete_session(session_id: str, request: Request):
        return await invoke(rt().sessions.delete, session_id=session_id, **await body(request))

    @app.post("/api/sessions/{session_id}/restore")
    async def restore_session(session_id: str):
        return await rt().sessions.restore(session_id)

    @app.get("/api/presence")
    async def presence(request: Request):
        async def stream():
            yield ": connected\n\n"
            while not rt().closed and not await request.is_disconnected():
                try:
                    await asyncio.wait_for(rt().events.changed.wait(), timeout=15)
                except TimeoutError:
                    pass
                yield ": heartbeat\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"X-Accel-Buffering": "no"})

    @app.get("/api/sessions/{session_id}/events")
    async def events(session_id: str, request: Request, after_seq: int = 0):
        await rt().sessions.get(session_id)
        try:
            cursor = max(after_seq, int(request.headers.get("last-event-id", "0")))
        except ValueError:
            raise RuntimeFault("INVALID_REQUEST", "Invalid event cursor") from None

        async def stream():
            async for event in rt().events.subscribe(session_id, cursor, include_project_workflows=True):
                if await request.is_disconnected():
                    break
                if event["type"] == "stream.heartbeat":
                    yield ": heartbeat\n\n"
                    continue
                identity = f"id: {event['seq']}\n" if event.get("durable") else ""
                yield identity + "event: runtime\ndata: " + json_dump(event) + "\n\n"

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"X-Accel-Buffering": "no"},
        )

    @app.post("/api/threads")
    async def create_thread(request: Request):
        return await invoke(rt().threads.create, **await body(request))

    @app.get("/api/threads/{thread_id}/conversation")
    async def conversation(
        thread_id: str,
        cursor: str | None = None,
        limit: int = 100,
        latest: bool = False,
        before_seq: int | None = None,
    ):
        return await rt().threads.conversation(
            thread_id, cursor, limit, latest=latest, before_seq=before_seq, presentation=True
        )

    @app.post("/api/threads/{thread_id}/fork")
    async def fork(thread_id: str, request: Request):
        return await invoke(rt().threads.fork, thread_id, **await body(request))

    @app.post("/api/threads/{thread_id}/turns")
    async def turn(thread_id: str, request: Request):
        return await invoke(rt().threads.turn, thread_id, **await body(request))

    @app.post("/api/tasks")
    async def task_submit(request: Request):
        return await rt().tasks.submit(await body(request))

    @app.post("/api/workflows")
    async def workflow(request: Request):
        data = await body(request)
        # Preserve the original one-shot task DAG API.
        return await command(request, "tasks.workflow" if "session_id" in data else "workflows.create", **data)

    @app.get("/api/workflows")
    async def workflows_list(project_id: str, cursor: str | None = None, limit: int = 25, include_archived: bool = False, deleted_only: bool = False):
        return await rt().workflows.list(project_id, cursor, limit, include_archived, deleted_only)

    @app.get("/api/workflows/node-types")
    async def workflow_node_types(request: Request):
        return await command(request, "workflows.node_types")

    @app.post("/api/workflows/validate")
    async def workflow_validate(request: Request):
        return await command(request, "workflows.validate", **await body(request))

    @app.get("/api/workflows/{workflow_id}")
    async def workflow_get(workflow_id: str, request: Request):
        return await command(request, "workflows.get", workflow_id=workflow_id)

    @app.patch("/api/workflows/{workflow_id}")
    async def workflow_update(workflow_id: str, request: Request):
        return await command(request, "workflows.update", workflow_id=workflow_id, **await body(request))

    @app.delete("/api/workflows/{workflow_id}")
    async def workflow_delete(workflow_id: str, request: Request):
        return await command(request, "workflows.delete", workflow_id=workflow_id, **await body(request))

    @app.post("/api/workflows/{workflow_id}/restore")
    async def workflow_restore(workflow_id: str, request: Request):
        return await command(request, "workflows.restore", workflow_id=workflow_id)

    @app.post("/api/workflows/{workflow_id}/archive")
    async def workflow_archive(workflow_id: str, request: Request):
        return await command(request, "workflows.archive", workflow_id=workflow_id, **await body(request))

    @app.post("/api/workflows/{workflow_id}/run")
    async def workflow_run(workflow_id: str, request: Request):
        return await command(request, "workflows.run", workflow_id=workflow_id, **await body(request))

    @app.get("/api/workflows/{workflow_id}/runs")
    async def workflow_runs(workflow_id: str, cursor: str | None = None, limit: int = 25):
        return await rt().workflows.runs(workflow_id, cursor, limit)

    @app.get("/api/workflow-runs/{run_id}")
    async def workflow_run_get(run_id: str, request: Request):
        return await command(request, "workflows.get_run", run_id=run_id)

    @app.post("/api/workflow-runs/{run_id}/{action}")
    async def workflow_run_action(run_id: str, action: str, request: Request):
        function = {"cancel": rt().workflows.cancel, "resume-node": rt().workflows.resume_node,
                    "reconcile-node": rt().workflows.reconcile_node, "result": rt().workflows.result}.get(action)
        require(function is not None, "NOT_FOUND", "Unknown workflow run action")
        return await invoke(function, run_id=run_id, **await body(request))

    @app.get("/api/tasks/{task_id}")
    async def task_get(task_id: str):
        return await rt().tasks.get(task_id, presentation=True)

    @app.get("/api/tasks/{task_id}/result")
    async def task_result(task_id: str):
        return await rt().tasks.result(task_id, timeout=0)

    @app.get("/api/tasks/{task_id}/trace")
    async def task_trace(task_id: str, cursor: str | None = None, limit: int = 50, include_resources: bool = False):
        return await rt().tasks.trace(task_id, cursor, limit, include_resources)

    @app.post("/api/tasks/{task_id}/{action}")
    async def task_action(task_id: str, action: str, request: Request):
        data = await body(request)
        if action == "cancel":
            return await rt().tasks.cancel(task_id)
        if action == "retry":
            return await invoke(rt().tasks.retry, task_id, **data)
        if action == "continue":
            return await invoke(rt().tasks.continue_result, task_id, **data)
        if action == "resume":
            return await invoke(rt().tasks.resume, task_id, **data)
        if action == "reconcile":
            return await rt().tasks.reconcile(task_id, data)
        raise RuntimeFault("NOT_FOUND", "Task action does not exist")

    @app.get("/api/messages")
    async def message_list(
        session_id: str, cursor: str | None = None, limit: int = 100
    ):
        return await rt().messages.list(session_id, cursor, limit)

    @app.post("/api/messages")
    async def message_send(request: Request):
        return await rt().messages.send(await body(request))

    @app.get("/api/resources")
    async def resources():
        return await rt().resources.inspect()

    @app.post("/api/artifacts")
    async def artifact_put(request: Request, session_id: str):
        content = bytearray()
        async for chunk in request.stream():
            content.extend(chunk)
            require(
                len(content) <= rt().config.max_artifact_bytes,
                "ARTIFACT_INVALID",
                "File exceeds the configured size limit",
            )
        return await rt().artifacts.put_bytes(
            session_id,
            unquote(request.headers.get("x-filename", "artifact")),
            bytes(content),
            source_kind="upload",
        )

    @app.get("/api/artifacts/{artifact_id}")
    async def artifact_get(artifact_id: str):
        return await rt().artifacts.get(artifact_id)

    @app.get("/api/artifacts/{artifact_id}/download")
    async def artifact_download(artifact_id: str):
        path = await rt().artifacts.open(artifact_id)
        meta = await rt().artifacts.get(artifact_id)
        return FileResponse(
            path, filename=meta.name, media_type="application/octet-stream"
        )

    @app.post("/api/rpc")
    async def rpc(request: Request):
        data = await body(request)
        require(
            isinstance(data.get("method"), str)
            and isinstance(data.get("params", {}), dict),
            "INVALID_REQUEST",
            "Invalid RPC request",
        )
        return {"result": await dispatch(rt(), data["method"], data.get("params", {}),
                                         view_version=request.headers.get("X-OEA-View-Version", "1.0"))}

    @app.get("/api/protocol")
    async def protocol():
        from ..protocol import describe

        return describe(rt())

    @app.post("/api/skills/parse")
    async def parse_skill(request: Request):
        from ..profiles import skill_from_markdown

        data = await body(request)
        try:
            inspect.signature(skill_from_markdown).bind(**data)
        except TypeError:
            raise RuntimeFault(
                "INVALID_REQUEST", "Invalid skill document parameters"
            ) from None
        return skill_from_markdown(**data)

    @app.get("/api/agents/{agent_id}/documents")
    async def agent_documents(agent_id: str):
        return await rt().agents.documents(agent_id)

    @app.put("/api/agents/{agent_id}/instructions")
    async def agent_instructions(agent_id: str, request: Request):
        return await invoke(
            rt().agents.set_instructions, agent_id, **await body(request)
        )

    @app.get("/api/agents/{agent_id}/skills")
    async def agent_skills(agent_id: str):
        return await rt().agents.skills_list(agent_id)

    @app.post("/api/agents/{agent_id}/skills/import")
    async def agent_skill_import(agent_id: str, request: Request):
        return await invoke(rt().agents.skill_import, agent_id, **await body(request))

    @app.put("/api/agents/{agent_id}/skills/{skill_id}")
    async def agent_skill_put(agent_id: str, skill_id: str, request: Request):
        data = await body(request)
        require(
            data.get("id", skill_id) == skill_id,
            "INVALID_REQUEST",
            "Skill ID does not match the URL",
        )
        return await rt().agents.skill_put(agent_id, {**data, "id": skill_id})

    @app.delete("/api/agents/{agent_id}/skills/{skill_id}")
    async def agent_skill_delete(agent_id: str, skill_id: str):
        return await rt().agents.skill_delete(agent_id, skill_id)

    @app.get("/api/skills")
    async def skills_registry(cursor: str | None = None, limit: int = 50):
        return await rt().skills.list(cursor, limit)

    @app.post("/api/skills/register-directory")
    async def skill_register_directory(request: Request):
        return await invoke(rt().skills.register_directory, **await body(request))

    @app.get("/api/skills/{skill_id}")
    async def skill_details(skill_id: str):
        return await rt().skills.get(skill_id)

    @app.get("/api/skills/{skill_id}/read")
    async def skill_read(skill_id: str, path: str = "SKILL.md"):
        return await rt().skills.read(skill_id, path)

    @app.post("/api/skills/{skill_id}/refresh")
    async def skill_refresh(skill_id: str):
        return await rt().skills.refresh(skill_id)

    @app.patch("/api/skills/{skill_id}")
    async def skill_enabled(skill_id: str, request: Request):
        return await invoke(rt().skills.set_enabled, skill_id, **await body(request))

    @app.delete("/api/skills/{skill_id}")
    async def skill_remove(skill_id: str):
        return await rt().skills.delete(skill_id)

    @app.post("/api/mcp/register-directory")
    async def mcp_register_directory(request: Request):
        return await invoke(rt().mcp.register_directory, **await body(request))

    @app.get("/api/agents/{agent_id}/capabilities")
    async def agent_capabilities(agent_id: str):
        return await rt().agents.capabilities(agent_id)

    @app.get("/api/mcp/servers")
    async def mcp_servers():
        return await rt().mcp.list()

    @app.get("/api/mcp/codex-candidates")
    async def mcp_codex_candidates():
        return await rt().mcp.codex_candidates()

    @app.post("/api/mcp/import-codex")
    async def mcp_import_codex(request: Request):
        return await invoke(rt().mcp.import_codex, **await body(request))

    @app.get("/api/mcp/codex-config")
    async def mcp_codex_config():
        return await rt().mcp.codex_config()

    @app.get("/api/mcp/calls")
    async def mcp_calls(server_id: str | None = None, limit: int = 50):
        return await rt().mcp.calls(server_id, limit)

    @app.get("/api/mcp/servers/{server_id}")
    async def mcp_server_get(server_id: str):
        return await rt().mcp.get(server_id)

    @app.put("/api/mcp/servers/{server_id}")
    async def mcp_server_put(server_id: str, request: Request):
        data = await body(request)
        secrets = data.pop("secret_values", None)
        require(
            data.get("id", server_id) == server_id,
            "INVALID_REQUEST",
            "MCP server ID does not match the URL",
        )
        return await rt().mcp.configure({**data, "id": server_id}, secrets)

    @app.delete("/api/mcp/servers/{server_id}")
    async def mcp_server_delete(server_id: str):
        return await rt().mcp.delete(server_id)

    @app.post("/api/mcp/servers/{server_id}/connect")
    async def mcp_connect(server_id: str, request: Request):
        return await invoke(rt().mcp.connect, server_id, **await body(request))

    @app.post("/api/mcp/servers/{server_id}/disconnect")
    async def mcp_disconnect(server_id: str, request: Request):
        await body(request)
        return await rt().mcp.disconnect(server_id)

    @app.get("/api/mcp/servers/{server_id}/tools")
    async def mcp_tools(
        server_id: str, refresh: bool = False, consumer_id: str = "console"
    ):
        return await rt().mcp.tools(server_id, refresh, consumer_id)

    @app.post("/api/mcp/servers/{server_id}/call")
    async def mcp_call(server_id: str, request: Request):
        return await invoke(rt().mcp.call, server_id, **await body(request))

    return app
