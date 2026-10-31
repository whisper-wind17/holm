"""TC-CT-02 Cursor Pagination contract tests."""

from __future__ import annotations

import pytest
from holm_contracts.pagination import CursorPage, CursorPageParams
from pydantic import ValidationError


def test_TC_CT_02_cursor_pagination_defaults_boundaries_and_shape() -> None:
    assert CursorPageParams().page_size == 20
    assert CursorPageParams(page_size=1).page_size == 1
    assert CursorPageParams(cursor="opaque", page_size=100).page_size == 100

    page = CursorPage[str](items=("a", "b"), next_cursor="next")
    assert page.model_dump(mode="json") == {
        "items": ["a", "b"],
        "next_cursor": "next",
    }


@pytest.mark.parametrize("page_size", [0, 101, True, "20", 20.0])
def test_TC_CT_02_cursor_pagination_rejects_invalid_page_size(page_size: object) -> None:
    with pytest.raises(ValidationError):
        CursorPageParams(page_size=page_size)  # type: ignore[arg-type]


def test_TC_CT_02_cursor_pagination_rejects_empty_cursor_and_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        CursorPageParams(cursor="")
    with pytest.raises(ValidationError):
        CursorPageParams.model_validate({"unknown": 1})
    with pytest.raises(ValidationError):
        CursorPage[str].model_validate({"items": [], "next_cursor": None, "unknown": 1})
