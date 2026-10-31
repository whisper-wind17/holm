"""Administrator Request Record query service and opaque keyset cursors."""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from holm_common import as_utc
from holm_domain import GATEWAY_ERROR_CODES, ErrorCode, RequestRecordState
from holm_persistence import (
    AsyncSessionFactory,
    RequestRecordFilters,
    RequestRecordListItem,
    RequestRecordPosition,
    RequestRecordRepository,
)
from holm_persistence.request_record_repository import RequestRecordErrorGroup

_CURSOR_VERSION = 1
_CURSOR_KIND = "request-records"


class RequestRecordQueryInvalid(ValueError):
    """A filter or cursor is invalid; the rejected value is never retained."""


@dataclass(frozen=True, slots=True)
class RequestRecordQuery:
    service_id: UUID | None = None
    user_id: UUID | None = None
    api_key_id: UUID | None = None
    status: RequestRecordState | None = None
    error_code: str | None = None
    request_id: UUID | None = None
    from_time: datetime | None = None
    to_time: datetime | None = None
    cursor: str | None = None
    limit: int = 50


@dataclass(frozen=True, slots=True)
class RequestRecordPage:
    items: tuple[RequestRecordListItem, ...]
    next_cursor: str | None


class RequestRecordQueryService:
    __slots__ = ("_session_factory",)

    def __init__(self, *, session_factory: AsyncSessionFactory) -> None:
        self._session_factory = session_factory

    async def list(self, query: RequestRecordQuery) -> RequestRecordPage:
        if not 1 <= query.limit <= 200:
            raise RequestRecordQueryInvalid()
        error_code = _error_code(query.error_code)
        from_time = _aware_time(query.from_time)
        to_time = _aware_time(query.to_time)
        if from_time is not None and to_time is not None and from_time > to_time:
            raise RequestRecordQueryInvalid()
        after = None if query.cursor is None else _decode_cursor(query.cursor)
        filters = RequestRecordFilters(
            service_id=query.service_id,
            user_id=query.user_id,
            api_key_id=query.api_key_id,
            status=query.status,
            error_code=error_code,
            request_id=query.request_id,
            from_time=from_time,
            to_time=to_time,
        )
        async with self._session_factory() as session:
            records = await RequestRecordRepository(session).list(
                filters=filters,
                after=after,
                limit=query.limit + 1,
            )
        items = records[: query.limit]
        next_cursor = None
        if len(records) > query.limit:
            last = items[-1]
            next_cursor = _encode_cursor(
                RequestRecordPosition(
                    received_at=last.received_at,
                    request_id=last.request_id,
                )
            )
        return RequestRecordPage(items=items, next_cursor=next_cursor)

    async def error_groups(
        self,
        *,
        from_time: datetime | None,
        to_time: datetime | None = None,
        service_id: UUID | None = None,
    ) -> tuple[RequestRecordErrorGroup, ...]:
        start = _aware_time(from_time)
        end = _aware_time(to_time)
        if start is None or (end is not None and start > end):
            raise RequestRecordQueryInvalid()
        async with self._session_factory() as session:
            return await RequestRecordRepository(session).error_groups(
                from_time=start, to_time=end, service_id=service_id
            )


def _aware_time(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    try:
        return as_utc(value)
    except ValueError:
        raise RequestRecordQueryInvalid() from None


def _error_code(value: str | None) -> ErrorCode | None:
    if value is None:
        return None
    try:
        code = ErrorCode(value)
    except ValueError:
        raise RequestRecordQueryInvalid() from None
    if code not in GATEWAY_ERROR_CODES:
        raise RequestRecordQueryInvalid()
    return code


def _encode_cursor(position: RequestRecordPosition) -> str:
    payload = json.dumps(
        {
            "v": _CURSOR_VERSION,
            "k": _CURSOR_KIND,
            "t": as_utc(position.received_at).isoformat(),
            "i": str(position.request_id),
        },
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str) -> RequestRecordPosition:
    decoded: object = None
    try:
        padding = "=" * (-len(cursor) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(cursor + padding).decode("utf-8"))
    except (ValueError, binascii.Error, UnicodeDecodeError):
        decoded = None
    if not isinstance(decoded, dict) or set(decoded) != {"v", "k", "t", "i"}:
        raise RequestRecordQueryInvalid()
    if decoded["v"] != _CURSOR_VERSION or decoded["k"] != _CURSOR_KIND:
        raise RequestRecordQueryInvalid()
    moment = decoded["t"]
    row_id = decoded["i"]
    if not isinstance(moment, str) or not isinstance(row_id, str):
        raise RequestRecordQueryInvalid()
    try:
        received_at = datetime.fromisoformat(moment)
        if received_at.tzinfo is None:
            raise ValueError
        return RequestRecordPosition(
            received_at=as_utc(received_at),
            request_id=UUID(row_id),
        )
    except (TypeError, ValueError):
        raise RequestRecordQueryInvalid() from None


__all__ = [
    "RequestRecordPage",
    "RequestRecordQuery",
    "RequestRecordQueryInvalid",
    "RequestRecordQueryService",
]
