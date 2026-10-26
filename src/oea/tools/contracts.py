"""Tool declarations used by model discovery and the execution boundary."""

from typing import Literal, Protocol

from pydantic import Field

from oea.contracts.configuration import Configuration


class ToolSpec(Configuration):
    name: str = Field(pattern=r"^[a-zA-Z_][a-zA-Z0-9_-]{0,63}$")
    description: str = ""
    input_schema: dict = Field(default_factory=lambda: {"type": "object"})
    output_schema: dict = Field(default_factory=lambda: {"type": "object"})
    side_effects: Literal["none", "local", "external"] = "none"
    cancellation: Literal["none", "cooperative", "owned_process"] = "none"
    timeout_seconds: float = Field(default=60, gt=0, le=3600)


class ToolProvider(Protocol):
    def describe(self) -> ToolSpec: ...
    async def invoke(self, request: dict) -> dict: ...
