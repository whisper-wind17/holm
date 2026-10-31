"""M3-08 Control API log cursor binding guards."""

from __future__ import annotations

import asyncio
from typing import cast
from uuid import UUID

import pytest
from holm_control_api.agent_client import (
    AgentLogEntry,
    AgentLogObservation,
    AgentLogPage,
    AgentLogUnavailable,
    AgentLogUnavailableReason,
    HostAgentClient,
)
from holm_control_api.model_instance_service import (
    ModelInstanceLogCursorInvalid,
    ModelInstanceLogsUnavailable,
    ModelInstanceService,
    _decode_log_cursor,
    _encode_log_cursor,
)
from holm_persistence import AsyncSessionFactory, ModelInstanceRecord

INSTANCE_A = UUID("01990000-0000-7000-8000-000000000308")
INSTANCE_B = UUID("01990000-0000-7000-8000-000000000309")


class _LogAgent:
    def __init__(self, result: AgentLogObservation) -> None:
        self.result = result
        self.calls: list[tuple[str, int, int]] = []

    async def read_logs(
        self,
        instance_id: str,
        *,
        cursor_offset: int,
        page_size: int,
    ) -> AgentLogObservation:
        self.calls.append((instance_id, cursor_offset, page_size))
        return self.result


class _Service(ModelInstanceService):
    async def get(self, instance_id: UUID) -> ModelInstanceRecord:
        del instance_id
        return cast(ModelInstanceRecord, object())


def _service(agent: _LogAgent) -> ModelInstanceService:
    return _Service(
        session_factory=cast(AsyncSessionFactory, object()),
        agent=cast(HostAgentClient, object()),
        log_agent=agent,
    )


def test_M3_08_log_cursor_round_trip_is_bound_to_one_instance() -> None:
    cursor = _encode_log_cursor(INSTANCE_A, 100)
    assert _decode_log_cursor(cursor, INSTANCE_A) == 100
    with pytest.raises(ModelInstanceLogCursorInvalid):
        _decode_log_cursor(cursor, INSTANCE_B)


@pytest.mark.parametrize("cursor", ("not-base64", "e30", ""))
def test_M3_08_log_cursor_rejects_malformed_or_incomplete_values(cursor: str) -> None:
    with pytest.raises(ModelInstanceLogCursorInvalid):
        _decode_log_cursor(cursor, INSTANCE_A)


def test_M3_08_log_service_maps_agent_page_to_instance_bound_next_cursor() -> None:
    agent = _LogAgent(
        AgentLogPage(
            entries=(AgentLogEntry(0, "ready", False, 5),),
            next_cursor_offset=1,
            has_more=True,
        )
    )
    page = asyncio.run(_service(agent).logs(INSTANCE_A, cursor=None, page_size=1))

    assert [entry.text for entry in page.entries] == ["ready"]
    assert page.next_cursor is not None
    assert _decode_log_cursor(page.next_cursor, INSTANCE_A) == 1
    assert agent.calls == [(str(INSTANCE_A), 0, 1)]


@pytest.mark.parametrize(
    ("reason", "error_type"),
    (
        (AgentLogUnavailableReason.REQUEST_REJECTED, ModelInstanceLogCursorInvalid),
        (AgentLogUnavailableReason.CONNECTION_FAILED, ModelInstanceLogsUnavailable),
    ),
)
def test_M3_08_log_service_maps_sanitized_agent_failures(
    reason: AgentLogUnavailableReason,
    error_type: type[Exception],
) -> None:
    with pytest.raises(error_type):
        asyncio.run(
            _service(_LogAgent(AgentLogUnavailable(reason))).logs(
                INSTANCE_A,
                cursor=None,
                page_size=100,
            )
        )
