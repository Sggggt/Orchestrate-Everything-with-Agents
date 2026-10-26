"""Typed v2 process boundaries; JSONL carries no administrator credentials."""

from typing import Literal

from pydantic import Field, model_validator

from oea.contracts.configuration import Configuration
from oea.contracts.domain import Wait

MAX_FRAME_BYTES = 16 * 1024 * 1024


class ProgramResult(Configuration):
    status: Literal["completed", "suspended"] = "completed"
    text: str = ""
    data: dict = Field(default_factory=dict)
    checkpoint: dict = Field(default_factory=dict)
    wait: Wait | None = None

    @model_validator(mode="after")
    def boundary(self):
        from oea.storage.persistence import encode
        if (self.status == "suspended") != (self.wait is not None):
            raise ValueError("A suspended boundary requires a wait; a completed boundary has none")
        if len(encode(self.checkpoint).encode("utf-8")) > 262144:
            raise ValueError("Use source references for checkpoints exceeding 256 KiB")
        return self
