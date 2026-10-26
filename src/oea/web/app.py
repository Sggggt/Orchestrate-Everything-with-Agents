"""Authenticated loopback HTTP, durable events and the buildless workbench."""

import hmac
import json
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from pydantic import Field

from oea import __version__
from oea.catalog.service import need
from oea.contracts.configuration import Configuration, PROTOCOL_VERSION
from oea.contracts.errors import RuntimeFault, require
from oea.runtime.host import Host
from oea.workspace.paths import safe_child
from oea.storage.persistence import encode, one
from oea.security.secrets import atomic_write


class Call(Configuration):
    protocol_version: str
    operation: str
    params: dict = Field(default_factory=dict)
    request_id: str
    compact: bool = False
    agent_name: str | None = Field(default=None, min_length=1, max_length=80)


def create_app(state_dir=None, *, host=None, allow_commands=False, client_url=None):
    @asynccontextmanager
    async def lifespan(app):
        backend = host or Host(state_dir, allow_commands=allow_commands)
        if host is None:
            await backend.__aenter__()
        else:
            backend.assert_open()
        try:
            app.state.host = backend
            token_path = backend.root / "client-token"
            if not token_path.exists():
                atomic_write(token_path, secrets.token_urlsafe(32).encode("utf-8"))
            app.state.token = token_path.read_text(encoding="utf-8").strip()
            if client_url:
                atomic_write(backend.root / "client.json", encode({"url": client_url, "protocol_version": PROTOCOL_VERSION}).encode("utf-8"))
            yield
        finally:
            if host is None:
                await backend.aclose()

    app = FastAPI(title="Orchestrate Everything with Agents", version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    assets = Path(__file__).with_name("static")

    def envelope(*, result=None, error=None, request_id=None):
        return {"protocol_version": PROTOCOL_VERSION, "request_id": request_id, **({"error": error} if error else {"result": result})}

    @app.exception_handler(RuntimeFault)
    async def fault(request, error):
        return JSONResponse(envelope(error=error.as_dict()), status_code=404 if error.code == "NOT_FOUND" else 403 if error.code == "PERMISSION_DENIED" else 400)

    @app.middleware("http")
    async def security(request, call_next):
        try:
            local = urlsplit("http://" + request.headers.get("host", "")).hostname in {"127.0.0.1", "localhost", "::1"}
        except ValueError:
            local = False
        origin = request.headers.get("origin")
        if not local or origin and origin.rstrip("/") != str(request.base_url).rstrip("/") or request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse(envelope(error={"code": "PERMISSION_DENIED", "safe_message": "Local same-origin access required"}), status_code=403)
        if request.url.path.startswith("/api/"):
            token = getattr(app.state, "token", "")
            provided = request.headers.get("authorization", "").removeprefix("Bearer ") or request.cookies.get("oea-session-" + str(request.url.port or 80), "")
            if not token or not hmac.compare_digest(token.encode("utf-8"), provided.encode("utf-8")):
                return JSONResponse(envelope(error={"code": "PERMISSION_DENIED", "safe_message": "Local client authentication required"}), status_code=401)
            if request.headers.get("X-OEA-Protocol-Version", PROTOCOL_VERSION) != PROTOCOL_VERSION:
                return JSONResponse(envelope(error={"code": "PROTOCOL_MISMATCH", "safe_message": "Use an OEA v2 client"}), status_code=409)
        response = await call_next(request)
        response.headers.update({"X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer", "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})
        return response

    @app.get("/")
    async def index(request: Request):
        response = FileResponse(assets / "index.html")
        response.set_cookie("oea-session-" + str(request.url.port or 80), app.state.token, httponly=True, samesite="strict", path="/")
        return response

    app.mount("/assets", StaticFiles(directory=assets, check_dir=False), name="assets")

    @app.get("/api/protocol")
    async def protocol():
        return {"protocol_version": PROTOCOL_VERSION, "version": __version__, "engine": "langgraph"}

    @app.post("/api/rpc")
    async def rpc(request: Request):
        require(request.headers.get("content-type", "").startswith("application/json"), "INVALID_REQUEST", "Send application/json")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            require(len(body) <= 2 * 1024 * 1024, "PAYLOAD_TOO_LARGE", "Request exceeds its bounded input size")
        try:
            def invalid_constant(value):
                raise ValueError("Non-finite values are not JSON protocol input")
            call = Call.model_validate(json.loads(body, parse_constant=invalid_constant))
        except (ValidationError, ValueError):
            return envelope(error={"code": "INVALID_REQUEST", "safe_message": "Use the versioned operation request schema"})
        try:
            require(call.protocol_version == PROTOCOL_VERSION, "PROTOCOL_MISMATCH", "Use the current operation contract")
            consumer = request.headers.get("X-OEA-Consumer-ID", "browser")
            require(0 < len(consumer) <= 128 and 0 < len(call.request_id) <= 256, "INVALID_REQUEST", "Use bounded request and consumer identities")
            value = await app.state.host.registry.call(call.operation, call.params, correlation_id=call.request_id, consumer_id=consumer, agent_name=call.agent_name)
            result = value.model_dump(mode="json") if hasattr(value, "model_dump") else value
            encoded = encode(result)
            if call.compact and call.operation != "results.read" and len(encoded.encode("utf-8")) > app.state.host.results.inline_bytes:
                reference = await app.state.host.results.issue(call.operation, call.params, result, consumer, characters=len(encoded))
                if reference is not None:
                    encoded = encode(reference)
            header = encode({"protocol_version": PROTOCOL_VERSION, "request_id": call.request_id})
            return Response(header[:-1] + ',"result":' + encoded + '}', media_type="application/json")
        except RuntimeFault as error:
            return envelope(error=app.state.host.redact(error.as_dict()), request_id=call.request_id)

    @app.get("/api/events")
    async def events(request: Request, project_id: str | None = None, session_id: str | None = None, run_id: str | None = None, after: int = 0):
        backend = app.state.host
        if request.headers.get("last-event-id"):
            try:
                after = max(after, int(request.headers["last-event-id"]))
            except ValueError:
                raise RuntimeFault("INVALID_REQUEST", "Use the original durable event position") from None
        if run_id:
            def scope(db):
                run = need(db, "runs", run_id)
                return need(db, "instances", run["instance_id"])["project_id"]
            owner = await backend.store.call(scope)
            require(project_id in {None, owner}, "PERMISSION_DENIED", "Run belongs to another project")
            project_id = owner
        require(project_id is not None, "INVALID_REQUEST", "Select an owning project")
        await backend.events_service.list(project_id, session_id=session_id, run_id=run_id, after=after, limit=1)
        async def frames():
            position = after
            if run_id:
                async for frame in backend.events(run_id):
                    if frame["kind"] != "model.delta":
                        while True:
                            page = await backend.events_service.list(project_id, session_id=session_id, run_id=run_id, after=position)
                            for fact in page["items"]:
                                yield "id: " + str(fact["position"]) + "\ndata: " + encode({"kind": "event", "position": fact["position"], "event": fact}) + "\n\n"
                            position = page["position"]
                            if page["cursor"] is None:
                                break
                    yield "data: " + encode(frame) + "\n\n"
            else:
                async for fact in backend.events_service.stream(project_id, session_id=session_id, after=position):
                    yield "id: " + str(fact["position"]) + "\ndata: " + encode({"kind": "event", "position": fact["position"], "event": fact}) + "\n\n"
        return StreamingResponse(frames(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"})

    @app.put("/api/artifact-uploads/{upload_id}/content")
    async def upload_content(upload_id: str, project_id: str, request: Request):
        result = await app.state.host.artifacts.write_upload(upload_id, project_id, request.stream(),
            consumer_id=request.headers.get("X-OEA-Consumer-ID"))
        return envelope(result=result)

    @app.get("/api/artifacts/{artifact_id}/content")
    async def artifact(artifact_id: str, project_id: str, inline: bool = False):
        backend = app.state.host
        record = await backend.store.call(lambda db: one(db, "SELECT * FROM artifacts WHERE id=? AND project_id=?", (artifact_id, project_id)))
        require(record is not None, "NOT_FOUND", "Artifact is not in this project")
        path = safe_child(backend.root / "artifacts", record["id"])
        require(path == Path(record["path"]).resolve(), "ARTIFACT_INVALID", "Artifact escaped its managed directory")
        media = record["media_type"]
        preview = inline and (media in {"image/png", "image/jpeg", "image/webp", "text/plain", "text/markdown", "application/json"})
        return FileResponse(path, filename=record["name"], media_type=media if preview else "application/octet-stream",
                            content_disposition_type="inline" if preview else "attachment")

    return app
