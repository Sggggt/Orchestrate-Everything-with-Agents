"""Shared public JSON result-page bounds."""

from typing import Annotated

from pydantic import Field

ResultOffset = Annotated[int, Field(strict=True, ge=0, le=2**63 - 1)]
ResultLimit = Annotated[int, Field(strict=True, ge=1, le=2048)]
ResultPointer = Annotated[str, Field(max_length=512)]
ResponseLimit = Annotated[int, Field(strict=True, ge=1, le=16384)]
