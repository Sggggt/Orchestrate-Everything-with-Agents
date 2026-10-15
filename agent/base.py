"""An agent is a black-box async stream with an explicit commit boundary."""

from typing import AsyncIterator, Protocol, TYPE_CHECKING

from ma_runtime.models import (
    AdapterEvent,
    AgentDescriptor,
    CancelReceipt,
    ExecutionRequest,
    ProbeResult,
)

if TYPE_CHECKING:
    from ma_runtime.context import RunContext


class AgentAdapter(Protocol):
    def describe(self) -> AgentDescriptor: ...
    def execute(
        self, request: ExecutionRequest, context: "RunContext"
    ) -> AsyncIterator[AdapterEvent]: ...
    async def cancel(self, run_ref: dict) -> CancelReceipt: ...
    async def probe(self, run_ref: dict) -> ProbeResult: ...
    async def aclose(self) -> None: ...
