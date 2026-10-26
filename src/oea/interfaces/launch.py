"""Start one host, or reuse the authenticated host already owning this state."""

import asyncio
from urllib.parse import urlsplit

import httpx

from oea.contracts.errors import require
from oea.interfaces.client import PlatformClient
from oea.workspace.paths import resolve_data_dir


def serve(data_dir=None, *, host="127.0.0.1", port=None, allow_commands=False):
    root = resolve_data_dir(data_dir)
    saved = None
    if (root / "client.json").is_file():
        saved = PlatformClient.from_data_dir(root)
        async def existing():
            try:
                async with saved:
                    status = await saved.call("host.status")
                    require(resolve_data_dir(status["data_dir"]) == root,
                            "HOST_DIRECTORY_MISMATCH", "Saved endpoint belongs to another data directory")
                    return True
            except httpx.ConnectError:
                return False
        if asyncio.run(existing()):
            require(port is None or port == urlsplit(saved.url).port, "HOST_ALREADY_RUNNING",
                    "Stop the existing host before changing its port")
            print("Workspace: " + saved.url)
            return
    selected_port = port if port is not None else (urlsplit(saved.url).port if saved else 8890)
    import uvicorn
    from oea.web.app import create_app
    url = f"http://[{host}]:{selected_port}" if host == "::1" else f"http://{host}:{selected_port}"
    app = create_app(root, allow_commands=allow_commands, client_url=url)
    uvicorn.run(app, host=host, port=selected_port, proxy_headers=False, timeout_graceful_shutdown=5)
