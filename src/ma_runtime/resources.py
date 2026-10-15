"""Atomic set acquisition, shared readers, and explicit uncertain isolation."""

from .models import ResourceRequirement


class ResourceCoordinator:
    def __init__(self):
        self.holders: dict[str, dict[str, str]] = {}
        self.isolated: dict[str, str] = {}

    def can_acquire(self, owner: str, requirements) -> bool:
        for item in requirements:
            for held_owner, mode in self.holders.get(item.key, {}).items():
                if held_owner != owner and (
                    mode == "EXCLUSIVE" or item.mode == "EXCLUSIVE"
                ):
                    return False
        return True

    def acquire(self, owner: str, requirements) -> bool:
        if not self.can_acquire(owner, requirements):
            return False
        for item in sorted(requirements, key=lambda item: item.key):
            self.holders.setdefault(item.key, {})[owner] = item.mode
        return True

    def release(self, owner: str):
        if owner in self.isolated:
            return
        for key in list(self.holders):
            self.holders[key].pop(owner, None)
            if not self.holders[key]:
                del self.holders[key]

    def isolate(self, owner: str, reason: str):
        self.isolated[owner] = reason

    def resolve(self, owner: str):
        self.isolated.pop(owner, None)
        self.release(owner)

    async def inspect(self):
        return {
            "resources": [
                {
                    "key": key,
                    "holders": [
                        {
                            "owner": owner,
                            "mode": mode,
                            "isolated": owner in self.isolated,
                            "reason": self.isolated.get(owner),
                        }
                        for owner, mode in holders.items()
                    ],
                }
                for key, holders in sorted(self.holders.items())
            ]
        }
