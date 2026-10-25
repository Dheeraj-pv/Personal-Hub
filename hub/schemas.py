"""Request validation. Errors are converted to a generic 400 without echoing input."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, field_validator

MAX_CHANGES = 500
_MAX_INT = 2**62  # stay inside SQLite's signed 64-bit INTEGER


class ChangeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")  # the client may send `seq`; it is ignored on push

    table: str = Field(min_length=1, max_length=64)
    id: str = Field(min_length=1, max_length=256)
    updated_at: StrictInt = Field(ge=0, le=_MAX_INT)
    deleted: StrictBool | StrictInt = 0
    data: dict[str, Any] = Field(default_factory=dict)

    @field_validator("deleted")
    @classmethod
    def _deleted_flag(cls, v: bool | int) -> int:
        if v not in (0, 1):
            raise ValueError("deleted must be 0 or 1")
        return int(v)


class SyncIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    device_id: str = Field(min_length=1, max_length=128)
    cursor: StrictInt = Field(ge=0, le=_MAX_INT)
    changes: list[ChangeIn] = Field(default_factory=list)
