"""Shared router helpers."""
from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import Header, Path, Query
from pydantic import BaseModel

Vin = Annotated[str, Path(pattern=r"^[A-HJ-NPR-Z0-9]{17}$")]
OemCode = Annotated[str, Path(pattern=r"^OEM_[A-Z]$")]
Version = Annotated[int, Path(ge=1)]
CursorQ = Annotated[str | None, Query(max_length=512, description="Opaque keyset cursor from a previous page")]
LimitQ = Annotated[int | None, Query(ge=1, le=200)]
FleetQ = Annotated[UUID | None, Query()]
IdemH = Annotated[str | None, Header(alias="Idempotency-Key", min_length=8, max_length=128)]

PROBLEM_RESPONSES: dict[int | str, dict[str, Any]] = {
    401: {"description": "Unauthorized", "content": {"application/problem+json": {}}},
    403: {"description": "Forbidden", "content": {"application/problem+json": {}}},
    429: {"description": "Rate limited", "content": {"application/problem+json": {}}},
}


def dump(model: type[BaseModel], data: dict[str, Any] | list[Any]) -> Any:
    """Validate a service result against the contract model and serialise it (drops undeclared-nullable None)."""
    if isinstance(data, list):
        return [model.model_validate(x).model_dump(mode="json") for x in data]
    return model.model_validate(data).model_dump(mode="json")
