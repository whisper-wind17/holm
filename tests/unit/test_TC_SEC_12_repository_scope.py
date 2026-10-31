"""TC-SEC-12: Repository filtering is applied to SQL before count and paging."""

from __future__ import annotations

import asyncio
from typing import Any, Final, cast
from uuid import UUID

import sqlalchemy as sa
from holm_domain import AuthorizationSubject, UserRole
from holm_persistence import USERS, UserRepository
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

SUBJECT_ID: Final[UUID] = UUID("00000000-0000-7000-8000-00000000000a")
OTHER_ID: Final[UUID] = UUID("00000000-0000-7000-8000-00000000000b")


class _MappingsResult:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def all(self) -> list[dict[str, Any]]:
        return self._rows


class _Result:
    def __init__(self, *, scalar: int | None = None, rows: list[dict[str, Any]] | None = None):
        self._scalar = scalar
        self._rows = [] if rows is None else rows

    def scalar_one(self) -> int:
        if self._scalar is None:
            raise AssertionError("not a scalar result")
        return self._scalar

    def mappings(self) -> _MappingsResult:
        return _MappingsResult(self._rows)


class _ScriptedSession:
    def __init__(self) -> None:
        self.statements: list[sa.Executable] = []
        self._results = [
            _Result(scalar=1),
            _Result(rows=[]),
        ]

    async def execute(self, statement: sa.Executable) -> _Result:
        self.statements.append(statement)
        return self._results.pop(0)


def _sql(statement: sa.Executable) -> str:
    return str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).replace("\n", " ")


def test_TC_SEC_12_count_and_page_share_the_subject_filter() -> None:
    scripted = _ScriptedSession()
    repository = UserRepository(cast(AsyncSession, scripted))
    subject = AuthorizationSubject(user_id=SUBJECT_ID, role=UserRole.USER)

    page = asyncio.run(repository.list_visible(subject, page=2, page_size=1))

    assert page.total == 1
    assert page.total_pages == 1
    assert page.page == 2
    assert page.items == ()
    assert len(scripted.statements) == 2
    count_sql, page_sql = map(_sql, scripted.statements)
    subject_predicate = f"core.users.id = '{SUBJECT_ID}'"
    assert subject_predicate in count_sql
    assert subject_predicate in page_sql
    assert str(OTHER_ID) not in count_sql
    assert "LIMIT 1 OFFSET 1" in page_sql
    assert "count(*)" in count_sql


def test_TC_SEC_12_get_visible_embeds_subject_scope_in_the_query_signature() -> None:
    parameter_names = tuple(UserRepository.get_visible.__annotations__)
    assert parameter_names[:2] == ("subject", "user_id")
    assert not hasattr(UserRepository, "get")
    assert USERS.c.id is not None
