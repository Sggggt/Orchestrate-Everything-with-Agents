"""The management boundary excludes application business objects by saved ownership."""

from oea.contracts.errors import require
from oea.storage.persistence import one


OWNERS = {
    "session_id": "SELECT source FROM sessions WHERE id=?",
    "instance_id": "SELECT s.source FROM instances i JOIN sessions s ON s.id=i.session_id WHERE i.id=?",
    "run_id": "SELECT s.source FROM runs r JOIN instances i ON i.id=r.instance_id JOIN sessions s ON s.id=i.session_id WHERE r.id=?",
    "action_id": "SELECT s.source FROM actions a JOIN runs r ON r.id=a.run_id JOIN instances i ON i.id=r.instance_id JOIN sessions s ON s.id=i.session_id WHERE a.id=?",
    "workflow_run_id": "SELECT s.source FROM workflow_runs w JOIN sessions s ON s.id=w.session_id WHERE w.id=?",
    "artifact_id": "SELECT s.source FROM artifacts a JOIN sessions s ON s.id=a.session_id WHERE a.id=?",
    "upload_id": "SELECT s.source FROM artifact_uploads a JOIN sessions s ON s.id=a.session_id WHERE a.id=?",
    "budget_id": "SELECT s.source FROM budgets b JOIN sessions s ON s.id=b.session_id WHERE b.id=?",
    "reference": "SELECT s.source FROM context_sources c JOIN instances i ON i.id=c.instance_id JOIN sessions s ON s.id=i.session_id WHERE c.id=?",
}
ALIASES = {"parent_run_id": "run_id", "root_run_id": "run_id", "owner_instance_id": "instance_id"}


def platform_object(db, kind, identity):
    owner = one(db, OWNERS[kind], (identity,))
    require(owner is None or owner["source"] == "platform", "PERMISSION_DENIED", "Object is not available through the platform interface")


def objects(params):
    for key in (*OWNERS, *ALIASES):
        value = params.get(key)
        if value:
            yield ALIASES.get(key, key), value
    for key, kind in (("artifact_ids", "artifact_id"), ("context_refs", "reference")):
        for value in params.get(key, ()):
            yield kind, value
    spec = params.get("spec")
    if spec is not None:
        # Only the typed intent's ownership fields are relevant. Business input
        # and arbitrary document dictionaries are never recursively interpreted.
        yield from objects(vars(spec) if hasattr(type(spec), "model_fields") else spec)


def platform_parameters(db, params):
    for kind, identity in objects(params):
        platform_object(db, kind, identity)


class PlatformAccess:
    def __init__(self, host):
        self.host = host

    async def guard(self, operation, params):
        owned = tuple(objects(params))
        if owned:
            await self.host.store.call(lambda db: [platform_object(db, kind, identity) for kind, identity in owned])
