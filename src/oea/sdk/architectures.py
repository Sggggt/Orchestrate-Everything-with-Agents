"""Typed architecture management over the same local or remote operations."""

from typing import Literal

from oea.contracts.architecture import AgentArchitecture, ArchitectureGraph, ArchitectureOptions
from oea.contracts.configuration import ArchitectureBinding
from oea.contracts.domain import RunSpec


class ArchitectureClient:
    """Use an open Host or PlatformClient; this facade owns neither lifecycle."""

    def __init__(self, backend):
        self.backend = backend

    async def _call(self, action, **params):
        params = {key: value.model_dump(mode="json") if hasattr(value, "model_dump") else value for key, value in params.items()}
        if hasattr(self.backend, "registry"):
            result = await self.backend.registry.call("architectures." + action, params)
            return result.model_dump(mode="json") if hasattr(result, "model_dump") else result
        return await self.backend.call("architectures." + action, params)

    async def templates(self, name: str | None = None):
        return await self._call("templates", name=name)

    async def status(self, agent_id: str, project_id: str, *, role: Literal["main", "manager"] = "main"):
        return await self._call("status", agent_id=agent_id, project_id=project_id, role=role)

    async def node_types(self, kind: str | None = None):
        return await self._call("node_types", kind=kind)

    async def schema(self, representation: Literal["declarative", "python"] = "declarative"):
        return await self._call("schema", representation=representation)

    async def create(self, definition: AgentArchitecture, draft: ArchitectureGraph | dict):
        return await self._call("create", definition=definition, draft=draft)

    async def get(self, architecture_id: str, project_id: str, *, version: int | None = None):
        return await self._call("get", architecture_id=architecture_id, project_id=project_id, version=version)

    async def list(self, project_id: str, *, cursor: int = 0, limit: int = 25):
        return await self._call("list", project_id=project_id, cursor=cursor, limit=limit)

    async def save(self, architecture_id: str, project_id: str, expected_revision: int, draft: ArchitectureGraph | dict):
        return await self._call("save", architecture_id=architecture_id, project_id=project_id, expected_revision=expected_revision, draft=draft)

    async def validate(self, architecture_id: str, project_id: str, *, expected_revision: int | None = None):
        return await self._call("validate", architecture_id=architecture_id, project_id=project_id, expected_revision=expected_revision)

    async def publish(self, architecture_id: str, project_id: str, expected_revision: int):
        return await self._call("publish", architecture_id=architecture_id, project_id=project_id, expected_revision=expected_revision)

    async def versions(self, architecture_id: str, project_id: str, *, cursor: int = 0, limit: int = 25):
        return await self._call("versions", architecture_id=architecture_id, project_id=project_id, cursor=cursor, limit=limit)

    async def bind(self, agent_id: str, project_id: str, binding: ArchitectureBinding, expected_revision: int, *, role: Literal["main", "manager"] = "main"):
        return await self._call("bind", agent_id=agent_id, project_id=project_id, binding=binding, expected_revision=expected_revision, role=role)

    async def compare(self, architecture_id: str, project_id: str, version: int, *, other_version: int | None = None):
        return await self._call("compare", architecture_id=architecture_id, project_id=project_id, version=version, other_version=other_version)

    async def preview(self, architecture_id: str, project_id: str, *, version: int | None = None):
        return await self._call("preview", architecture_id=architecture_id, project_id=project_id, version=version)

    async def adjust(self, architecture_id: str, project_id: str, expected_revision: int, options: ArchitectureOptions):
        return await self._call("adjust", architecture_id=architecture_id, project_id=project_id, expected_revision=expected_revision, options=options)

    async def export(self, architecture_id: str, project_id: str, *, version: int | None = None):
        return await self._call("export", architecture_id=architecture_id, project_id=project_id, version=version)

    async def run(self, project_id: str, binding: ArchitectureBinding, spec: RunSpec):
        return await self._call("run", project_id=project_id, binding=binding, spec=spec)

    async def inspect(self, run_id: str, project_id: str, *, node_id: str | None = None, cursor: int = 0, limit: int = 25):
        return await self._call("inspect", run_id=run_id, project_id=project_id, node_id=node_id, cursor=cursor, limit=limit)

    async def node_result(self, run_id: str, project_id: str, scope: str, node_id: str, activation: str):
        return await self._call("node_result", run_id=run_id, project_id=project_id, scope=scope, node_id=node_id, activation=activation)
