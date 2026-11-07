"""Single-use browser login tickets issued by an authenticated local controller."""

import hashlib
import secrets
import time

from oea.contracts.errors import require


class BrowserLogins:
    lifetime = 60

    def __init__(self):
        self.pending = {}

    def issue(self):
        current = time.monotonic()
        self.pending = {key: expiry for key, expiry in self.pending.items() if expiry > current}
        ticket = secrets.token_urlsafe(32)
        self.pending[hashlib.sha256(ticket.encode()).digest()] = current + self.lifetime
        return {"ticket": ticket, "expires_in": self.lifetime}

    def redeem(self, ticket):
        require(isinstance(ticket, str) and 0 < len(ticket) <= 128, "PERMISSION_DENIED", "Use a fresh local browser login link")
        expiry = self.pending.pop(hashlib.sha256(ticket.encode()).digest(), None)
        require(expiry is not None and expiry > time.monotonic(), "PERMISSION_DENIED", "Browser login link expired or was already used")
