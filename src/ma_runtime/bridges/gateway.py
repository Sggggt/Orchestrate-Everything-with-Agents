"""Finite HTTP/file operations for a connected MCP bridge; credentials stay local."""

from pathlib import Path
from urllib.parse import quote

from ..client import PlatformClient
from ..errors import RuntimeFault, require
from ..models import RuntimeConfig
from ..paths import external_path, require_local_state


async def gateway_request(root, operation, params):
    async with PlatformClient.from_data_dir(root) as client:

        async def request(method, path, **kwargs):
            response = await client.http.request(method, path, **kwargs)
            if not response.is_success:
                try:
                    error = response.json().get("error", {})
                except ValueError:
                    error = {}
                raise RuntimeFault(
                    error.get("code", "GATEWAY_ERROR"),
                    error.get("safe_message", "Platform HTTP request failed"),
                )
            return response

        if operation in {"@status", "@config", "@configure"}:
            bootstrap = (await request("GET", "/api/bootstrap")).json()
            if operation == "@status":
                return {
                    "url": str(client.http.base_url).rstrip("/"),
                    "data_dir": str(root),
                    "project_count": len(bootstrap.get("projects", [])),
                    "project": bootstrap.get("project"),
                    "runtime": bootstrap["runtime"],
                    "agent_count": len(bootstrap["agents"]),
                    "mcp_count": len(bootstrap.get("mcp_servers", [])),
                    "session_count": len(bootstrap["sessions"]),
                }
            if operation == "@config":
                return bootstrap["config"]
            require(
                isinstance(params["changes"], dict),
                "INVALID_REQUEST",
                "changes must be an object",
            )
            config = RuntimeConfig.model_validate(
                {**bootstrap["config"], **params["changes"]}
            )
            return (
                await request("PUT", "/api/config", json=config.model_dump(mode="json"))
            ).json()
        if operation == "@delete_secret":
            return (
                await request(
                    "DELETE",
                    "/api/agents/" + quote(params["agent_id"], safe="") + "/secret",
                    headers={"Content-Type": "application/json"},
                )
            ).json()
        if operation == "@skill_parse":
            return (await request("POST", "/api/skills/parse", json=params)).json()
        if operation == "@upload_file":
            source = Path(params["path"]).expanduser()
            require(
                source.is_absolute() and source.is_file(),
                "INVALID_REQUEST",
                "Select one existing absolute local file",
            )
            bootstrap = (await request("GET", "/api/bootstrap")).json()
            limit = bootstrap["config"]["max_artifact_bytes"]
            require(
                source.stat().st_size <= limit,
                "ARTIFACT_INVALID",
                "File exceeds the configured limit",
            )
            with source.open("rb") as stream:
                content = stream.read(limit + 1)
            require(
                len(content) <= limit,
                "ARTIFACT_INVALID",
                "File exceeds the configured limit",
            )
            return (
                await request(
                    "POST",
                    "/api/artifacts",
                    params={"session_id": params["session_id"]},
                    content=content,
                    headers={
                        "Content-Type": "application/octet-stream",
                        "X-Filename": quote(source.name, safe=""),
                    },
                )
            ).json()
        if operation == "@download_file":
            target = Path(params["path"]).expanduser()
            require(
                target.is_absolute(),
                "INVALID_REQUEST",
                "Download path must be absolute",
            )
            target = external_path(target)
            require_local_state(target)
            require(
                type(params.get("overwrite", False)) is bool,
                "INVALID_REQUEST",
                "overwrite must be boolean",
            )
            # Exclusive creation is the default even if the file appears after validation.
            mode = "wb" if params.get("overwrite", False) else "xb"
            artifact_id = quote(params["artifact_id"], safe="")
            response = await request("GET", f"/api/artifacts/{artifact_id}/download")
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                with target.open(mode) as stream:
                    stream.write(response.content)
            except FileExistsError:
                raise RuntimeFault(
                    "FILE_EXISTS",
                    "Choose another destination or explicitly allow overwrite",
                ) from None
            return {
                "artifact_id": params["artifact_id"],
                "path": str(target),
                "bytes": len(response.content),
            }
        raise RuntimeFault("METHOD_NOT_FOUND", "Gateway operation is not available")
