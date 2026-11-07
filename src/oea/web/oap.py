"""Fixed OAP HTTP actions authenticated independently of the management protocol."""

import asyncio
from contextlib import aclosing

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import ValidationError
from pydantic_core import from_json

from oea.contracts.applications import OAP_PREFIX, OAP_VERSION, ApplicationRun, ApplicationResume, ApplicationResolution, ApplicationUpload, ConversationCreate
from oea.contracts.errors import RuntimeFault, require
from oea.storage.persistence import encode, new_id
from oea.applications.views import public_error, public_status


def envelope(*, result=None, error=None, request_id=None):
    return {"protocol_version": OAP_VERSION, "request_id": request_id, **({"error": error} if error else {"result": result})}


def reply(request, result):
    value = result.model_dump(mode="json") if hasattr(result, "model_dump") else result
    return Response(encode(envelope(result=value, request_id=request.state.request_id)), media_type="application/json")


def failure(request, error, *, status=None):
    code = error.code
    status = status or (403 if code == "PERMISSION_DENIED" else 409 if code in {"PROTOCOL_MISMATCH", "IDEMPOTENCY_CONFLICT", "REVISION_CONFLICT"} else 413 if code == "PAYLOAD_TOO_LARGE" else 400)
    return JSONResponse(envelope(error=public_error(error), request_id=getattr(request.state, "request_id", None)), status_code=status)


async def body(request, model=None):
    require(request.headers.get("content-type", "").startswith("application/json"), "INVALID_REQUEST", "Send application/json")
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        require(len(data) <= 2 * 1024 * 1024, "PAYLOAD_TOO_LARGE", "OAP JSON requests are limited to 2097152 bytes")
    try:
        value = from_json(data, allow_inf_nan=False)
        require(isinstance(value, dict), "INVALID_REQUEST", "Send a JSON object")
        if model is not None:
            return model.model_validate(value)
        require(not value, "INVALID_REQUEST", "This action takes an empty JSON object")
        return value
    except (ValidationError, ValueError):
        raise RuntimeFault("INVALID_REQUEST", "Body does not match this OAP operation's schema") from None


def key(request):
    value = request.headers.get("Idempotency-Key", "")
    require(0 < len(value) <= 256 and value.strip(), "INVALID_REQUEST", "Supply a bounded Idempotency-Key")
    return value


async def progress(backend, app_id, run_id, credential_revision):
    stream = backend.events(run_id)
    pending, changed = None, None
    try:
        pending = asyncio.create_task(anext(stream))
        changed = asyncio.create_task(backend.applications.authorization_signal(app_id).wait())
        while True:
            backend.applications.authorize_stream(app_id, credential_revision)
            done, _ = await asyncio.wait({pending, changed}, return_when=asyncio.FIRST_COMPLETED)
            backend.applications.authorize_stream(app_id, credential_revision)
            if changed in done:
                changed = asyncio.create_task(backend.applications.authorization_signal(app_id).wait())
            if pending not in done:
                continue
            try:
                frame = pending.result()
            except StopAsyncIteration:
                return
            yield frame
            pending = asyncio.create_task(anext(stream))
    finally:
        tasks = [task for task in (pending, changed) if task is not None]
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await stream.aclose()


def router():
    api = APIRouter(prefix=OAP_PREFIX)

    @api.get("/capabilities")
    async def capabilities(request: Request):
        from oea.workspace.native import inspect_component
        native = await inspect_component()
        backend = native.get("backend", {})
        return reply(request, {"protocol_version": OAP_VERSION, "execution": "langgraph", "native_sandbox_required": True,
            "native_component": {**{key: native[key] for key in ("state", "version", "os", "arch") if key in native},
                                 "backend": {key: backend[key] for key in ("name", "supported", "installed", "execution_verified") if key in backend}},
            "operations": ["agents.list", "conversations.create", "conversations.list", "conversations.get", "conversations.history", "conversations.close",
                           "runs.submit", "runs.get", "runs.events", "runs.response", "runs.result", "runs.actions", "actions.get", "actions.confirm", "runs.resume", "runs.cancel", "runs.commands", "commands.output", "runs.artifacts", "runs.candidates",
                           "uploads.begin", "uploads.content", "uploads.commit", "uploads.get", "artifacts.content"],
            "limits": {"json_bytes": 2 * 1024 * 1024, "input_bytes": 1024 * 1024, "file_bytes": 50 * 1024 * 1024},
            "schemas": {"conversations.create": "/schema/conversation", "runs.submit": "/schema/run", "runs.resume": "/schema/resume", "actions.confirm": "/schema/resolution", "uploads.begin": "/schema/upload"},
            "events": {"durable_cursor": True, "model_deltas": "attempt_block_sequence", "reconnect_replays_execution": False}})

    @api.get("/schema/{operation}")
    async def schema(request: Request, operation: str):
        models = {"conversation": ConversationCreate, "run": ApplicationRun, "resume": ApplicationResume, "resolution": ApplicationResolution, "upload": ApplicationUpload}
        require(operation in models, "NOT_FOUND", "OAP schema is not available")
        return reply(request, models[operation].model_json_schema())

    @api.get("/agents")
    async def agents(request: Request, cursor: int = 0, limit: int = 25):
        return reply(request, await request.app.state.host.oap.agents(request.state.app_id, cursor=cursor, limit=limit))

    @api.post("/conversations")
    async def create(request: Request):
        return reply(request, await request.app.state.host.oap.create_conversation(request.state.app_id, await body(request, ConversationCreate), key(request)))

    @api.get("/conversations")
    async def conversations(request: Request, cursor: int = 0, limit: int = 25):
        return reply(request, await request.app.state.host.oap.conversations(request.state.app_id, cursor=cursor, limit=limit))

    @api.get("/conversations/{conversation_id}")
    async def conversation(request: Request, conversation_id: str):
        return reply(request, await request.app.state.host.oap.conversation(request.state.app_id, conversation_id))

    @api.get("/conversations/{conversation_id}/history")
    async def history(request: Request, conversation_id: str, after: int = 0, limit: int = 25, version: int | None = None):
        return reply(request, await request.app.state.host.oap.history(request.state.app_id, conversation_id, after=after, limit=limit, version=version))

    @api.post("/conversations/{conversation_id}/runs")
    async def submit(request: Request, conversation_id: str):
        return reply(request, await request.app.state.host.oap.submit(request.state.app_id, conversation_id, await body(request, ApplicationRun), key(request)))

    @api.post("/conversations/{conversation_id}/close")
    async def close(request: Request, conversation_id: str):
        await body(request)
        return reply(request, await request.app.state.host.oap.close_conversation(request.state.app_id, conversation_id, key(request)))

    @api.get("/runs/{run_id}")
    async def run(request: Request, run_id: str):
        return reply(request, public_status(await request.app.state.host.oap.run(request.state.app_id, run_id)))

    @api.get("/runs/{run_id}/result")
    async def result(request: Request, run_id: str, offset: int = 0, limit: int | None = None):
        return reply(request, await request.app.state.host.oap.result(request.state.app_id, run_id, offset=offset, limit=limit))

    @api.get("/runs/{run_id}/response")
    async def response(request: Request, run_id: str, action_id: str | None = None, attempt_id: str | None = None, version: str | None = None, offset: int = 0, limit: int = 8192):
        return reply(request, await request.app.state.host.oap.response(request.state.app_id, run_id, action_id=action_id, attempt_id=attempt_id, version=version, offset=offset, limit=limit))

    @api.get("/runs/{run_id}/actions")
    async def actions(request: Request, run_id: str, cursor: int = 0, limit: int = 25):
        return reply(request, await request.app.state.host.oap.actions(request.state.app_id, run_id, cursor=cursor, limit=limit))

    @api.get("/runs/{run_id}/actions/{action_id}")
    async def action(request: Request, run_id: str, action_id: str, json_pointer: str = "", version: str | None = None):
        return reply(request, await request.app.state.host.oap.action(request.state.app_id, run_id, action_id, json_pointer=json_pointer, version=version))

    @api.post("/runs/{run_id}/actions/{action_id}/confirm")
    async def confirm_action(request: Request, run_id: str, action_id: str):
        return reply(request, await request.app.state.host.oap.confirm_action(request.state.app_id, run_id, action_id, await body(request, ApplicationResolution), key(request)))

    @api.post("/runs/{run_id}/resume")
    async def resume(request: Request, run_id: str):
        return reply(request, public_status(await request.app.state.host.oap.resume(request.state.app_id, run_id, await body(request, ApplicationResume), key(request))))

    @api.post("/runs/{run_id}/cancel")
    async def cancel(request: Request, run_id: str):
        await body(request)
        return reply(request, public_status(await request.app.state.host.oap.cancel(request.state.app_id, run_id, key(request))))

    @api.get("/runs/{run_id}/commands/{command_id}")
    async def command(request: Request, run_id: str, command_id: str, stdout_offset: int = 0, stderr_offset: int = 0):
        return reply(request, public_status(await request.app.state.host.oap.command(request.state.app_id, run_id, command_id, stdout_offset=stdout_offset, stderr_offset=stderr_offset)))

    @api.get("/runs/{run_id}/commands/{command_id}/output/{stream}")
    async def command_output(request: Request, run_id: str, command_id: str, stream: str, offset: int = 0, limit: int | None = None):
        from oea.artifacts.service import file_io
        path, record = await request.app.state.host.oap.command_output(request.state.app_id, run_id, command_id, stream)
        size = path.stat().st_size
        require(0 <= offset <= size and (limit is None or limit > 0), "INVALID_REQUEST", "Use a byte range within the retained command output")
        length = min(size - offset, limit) if limit is not None else size - offset
        async def chunks():
            remaining = length
            with path.open("rb") as source:
                source.seek(offset)
                while remaining:
                    chunk = await file_io(source.read, min(65536, remaining))
                    require(chunk, "COMMAND_OUTPUT_CHANGED", "Retained command output became unavailable")
                    remaining -= len(chunk)
                    yield chunk
        return StreamingResponse(chunks(), media_type="application/octet-stream", headers={
            "Content-Length": str(length), "Content-Disposition": f'attachment; filename="{command_id}-{stream}.bin"',
            "X-OEA-Source-State": record["state"], "X-OEA-Snapshot-Bytes": str(size), "X-OEA-Source-Offset": str(offset)})

    @api.get("/runs/{run_id}/artifacts")
    async def artifacts(request: Request, run_id: str, cursor: int = 0, limit: int = 25):
        return reply(request, await request.app.state.host.oap.artifacts(request.state.app_id, run_id, cursor=cursor, limit=limit))

    @api.get("/runs/{run_id}/candidates")
    async def candidates(request: Request, run_id: str, cursor: int = 0, limit: int = 25):
        backend = request.app.state.host
        from oea.applications.business import require_run
        from oea.catalog.service import need
        owner = await backend.store.call(lambda db: require_run(db, request.state.app_id, run_id))
        project = await backend.store.call(lambda db: need(db, "sessions", owner["session_id"])["project_id"])
        return reply(request, await backend.artifacts.candidates(run_id, project, cursor=cursor, limit=limit))

    @api.post("/uploads")
    async def begin_upload(request: Request):
        return reply(request, await request.app.state.host.oap.files.begin(request.state.app_id, await body(request, ApplicationUpload), key(request)))

    @api.get("/uploads/{upload_id}")
    async def upload(request: Request, upload_id: str):
        return reply(request, await request.app.state.host.oap.files.get(request.state.app_id, upload_id))

    @api.put("/uploads/{upload_id}/content")
    async def content(request: Request, upload_id: str):
        return reply(request, await request.app.state.host.oap.files.write(request.state.app_id, upload_id, request.stream()))

    @api.post("/uploads/{upload_id}/commit")
    async def commit(request: Request, upload_id: str):
        await body(request)
        key(request)
        return reply(request, await request.app.state.host.oap.files.commit(request.state.app_id, upload_id))

    @api.get("/artifacts/{artifact_id}/content")
    async def download(request: Request, artifact_id: str):
        record, path = await request.app.state.host.oap.files.download(request.state.app_id, artifact_id)
        return FileResponse(path, filename=record["name"], media_type="application/octet-stream",
            headers={"ETag": '"' + record["sha256"] + '"', "X-Content-SHA256": record["sha256"]})

    @api.get("/runs/{run_id}/events")
    async def events(request: Request, run_id: str, after: int = 0):
        backend, app_id = request.app.state.host, request.state.app_id
        if request.headers.get("last-event-id"):
            try:
                after = max(after, int(request.headers["last-event-id"]))
            except ValueError:
                raise RuntimeFault("INVALID_REQUEST", "Use the original durable event position") from None
        await backend.oap.events_page(app_id, run_id, after=after, limit=1)
        async def frames():
            position = after
            try:
                async with aclosing(progress(backend, app_id, run_id, request.state.credential_revision)) as stream:
                    async for frame in stream:
                        if frame["kind"] != "model.delta":
                            while True:
                                page = await backend.oap.events_page(app_id, run_id, after=position)
                                for fact in page["items"]:
                                    fact["data"] = public_status(fact["data"])
                                    yield "id: " + str(fact["position"]) + "\ndata: " + encode({"kind": "event", "position": fact["position"], "event": fact}) + "\n\n"
                                position = page["position"]
                                if page["cursor"] is None:
                                    break
                        if "run" in frame:
                            frame["run"] = public_status(frame["run"])
                        yield "data: " + encode(frame) + "\n\n"
            except RuntimeFault as error:
                yield "data: " + encode({"kind": "error", "error": public_error(error)}) + "\n\n"
        return StreamingResponse(frames(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"})

    return api
