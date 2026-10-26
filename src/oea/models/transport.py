"""Host-owned HTTP connections with request-scoped authentication and timeouts."""

import asyncio
from contextlib import asynccontextmanager
from http.cookiejar import DefaultCookiePolicy
from urllib.parse import urlsplit

import httpx

from oea.contracts.errors import require


class _NoResponseCookies(DefaultCookiePolicy):
    def set_ok(self, cookie, request):
        return False


class ModelTransport:
    def __init__(self):
        self.clients = {}
        self.closed = False

    @asynccontextmanager
    async def borrow(self, base_url):
        require(not self.closed, "RUNTIME_CLOSING", "Model transport is closed")
        trust_env = urlsplit(base_url).hostname not in {"localhost", "127.0.0.1", "::1"}
        client = self.clients.get(trust_env)
        if client is None:
            client = httpx.AsyncClient(timeout=None, follow_redirects=False, trust_env=trust_env,
                limits=httpx.Limits(max_connections=None, max_keepalive_connections=16, keepalive_expiry=30))
            # Provider cookies must not become another Agent's authentication.
            client.cookies.jar.set_policy(_NoResponseCookies())
            self.clients[trust_env] = client
        yield client

    async def aclose(self):
        self.closed = True
        await asyncio.gather(*(client.aclose() for client in self.clients.values()))
        self.clients.clear()
