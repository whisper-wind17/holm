"""Closed Cursor Pagination request and response contracts."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

PageSize = Annotated[int, Field(strict=True, ge=1, le=100)]
Cursor = Annotated[str, Field(min_length=1)]


class _ClosedContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CursorPageParams(_ClosedContract):
    cursor: Cursor | None = None
    page_size: PageSize = 20


class CursorPage[ItemT](_ClosedContract):
    items: tuple[ItemT, ...]
    next_cursor: Cursor | None = None
