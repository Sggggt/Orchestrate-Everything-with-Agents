"""Provider contracts independent of the host storage and mediation gateway."""

from typing import Literal, Protocol

from pydantic import Field

from .models import Model, ResourceRequirement


class ToolSpec(Model):
    name: str
    input_schema: dict = Field(default_factory=lambda: {"type": "object"})
    output_schema: dict = Field(default_factory=lambda: {"type": "object"})
    resources: tuple[ResourceRequirement, ...] = ()
    side_effects: Literal["NONE", "LOCAL", "EXTERNAL"] = "NONE"
    timeout_seconds: float = Field(default=60, gt=0, le=3600)
    supports_idempotency: bool = False
    supports_probe: bool = False


class ToolProvider(Protocol):
    def describe(self) -> ToolSpec: ...
    async def invoke(self, request: dict) -> dict: ...
    async def reconcile(self, operation_ref: dict) -> dict: ...
