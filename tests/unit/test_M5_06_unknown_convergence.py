"""M5-06 unit coverage: Unknown sequencing, Ledger summaries and the attention API."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final, cast
from uuid import UUID

import pytest
from fixtures.asgi_client import NO_CONFIGURED_CREDENTIAL_ROWS, AsgiTestClient
from holm_contracts import VersionPayload
from holm_control_api import (
    AuthenticatedSession,
    AuthenticationService,
    DependencyCheck,
    ReadinessReason,
    create_app,
)
from holm_control_api.attention_service import (
    AttentionItem,
    AttentionKind,
    AttentionService,
)
from holm_domain import UserRole, UserStatus
from holm_ledger import (
    CompletedEvent,
    EventType,
    InProgressEvent,
    ReceivedEvent,
    RequestEventSummary,
    RequestOperation,
    RequestProtocol,
    SequenceAction,
    SQLiteLedger,
    UnknownEvent,
    decide_event_sequence,
    open_ledger,
)
from holm_ledger_writer import LedgerWriter
from holm_persistence import (
    AsyncSessionFactory,
    AuditEventDraft,
    AuditEventRecord,
    SessionRecord,
    UserRecord,
)
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

NOW: Final = datetime(2026, 9, 17, 9, 0, tzinfo=UTC)
REQUEST_ID: Final = UUID("7e000000-0000-4000-8000-000000000001")
OTHER_ID: Final = UUID("7e000000-0000-4000-8000-000000000002")
USER_ID: Final = UUID("7e000000-0000-4000-8000-000000000003")
ATTENTION_PATH: Final = "/api/v1/overview/attention"


def _received(request_id: UUID = REQUEST_ID) -> ReceivedEvent:
    return ReceivedEvent(
        request_id=request_id,
        event_seq=1,
        created_at=NOW,
        received_at=NOW,
        protocol=RequestProtocol.OPENAI,
        operation=RequestOperation.CHAT_COMPLETIONS,
        endpoint="/v1/chat/completions",
        stream=True,
        input_bytes=8,
    )


def _in_progress(request_id: UUID = REQUEST_ID) -> InProgressEvent:
    return InProgressEvent(
        request_id=request_id,
        event_seq=2,
        created_at=NOW + timedelta(seconds=1),
        received_at=NOW,
        user_id=USER_ID,
        api_key_id=USER_ID,
        service_id=USER_ID,
        instance_id=USER_ID,
        requested_max_tokens=None,
    )


def _completed(seq: int = 3) -> CompletedEvent:
    return CompletedEvent(
        request_id=REQUEST_ID,
        event_seq=seq,
        created_at=NOW + timedelta(seconds=2),
        received_at=NOW,
        input_tokens=1,
        output_tokens=1,
        duration_ms=1,
        http_status=200,
    )


def _unknown(seq: int, last: EventType, last_seq: int) -> UnknownEvent:
    return UnknownEvent(
        request_id=REQUEST_ID,
        event_seq=seq,
        created_at=NOW + timedelta(hours=2),
        received_at=NOW,
        last_confirmed_event_type=last,
        last_confirmed_event_seq=last_seq,
    )


def test_M5_06_unknown_may_follow_received_or_in_progress() -> None:
    received = _received()
    after_received = decide_event_sequence((received,), _unknown(2, EventType.RECEIVED, 1))
    after_progress = decide_event_sequence(
        (received, _in_progress()), _unknown(3, EventType.IN_PROGRESS, 2)
    )
    assert after_received.action is SequenceAction.WRITE
    assert after_progress.action is SequenceAction.WRITE


def test_M5_06_unknown_after_terminal_is_never_written() -> None:
    history = (_received(), _in_progress(), _completed())
    # Same sequence as the terminal: the Gateway won the race; ignored quietly.
    assert (
        decide_event_sequence(history, _unknown(3, EventType.IN_PROGRESS, 2)).action
        is SequenceAction.IGNORE
    )
    assert (
        decide_event_sequence(history, _unknown(4, EventType.IN_PROGRESS, 2)).action
        is SequenceAction.REJECT
    )
    # Received cannot jump straight to a result other than Rejected/InProgress/Unknown.
    assert decide_event_sequence((_received(),), _completed(seq=2)).action is SequenceAction.REJECT


def test_M5_06_summarize_requests_reports_max_seq_and_unacked(tmp_path: Path) -> None:
    directory = tmp_path / "spool"
    directory.mkdir(mode=0o700)
    with open_ledger(directory / "request-ledger.sqlite3") as ledger:
        ledger.append_batch((_received(), _in_progress(), _received(OTHER_ID)))
        ledger.mark_acked_batch(((REQUEST_ID, 1), (REQUEST_ID, 2)), acked_at=NOW)
        missing = UUID("7e000000-0000-4000-8000-000000000009")
        summaries = ledger.summarize_requests([REQUEST_ID, OTHER_ID, missing, REQUEST_ID])
    assert summaries == {
        REQUEST_ID: RequestEventSummary(max_event_seq=2, unacked_count=0),
        OTHER_ID: RequestEventSummary(max_event_seq=1, unacked_count=1),
    }


def test_M5_06_writer_rejects_non_positive_unknown_settings() -> None:
    ledger = cast(SQLiteLedger, None)
    factory = cast(AsyncSessionFactory, None)
    engine = cast(AsyncEngine, None)
    with pytest.raises(ValueError, match="positive"):
        LedgerWriter(ledger=ledger, session_factory=factory, engine=engine, unknown_after_seconds=0)
    with pytest.raises(ValueError, match="positive"):
        LedgerWriter(ledger=ledger, session_factory=factory, engine=engine, unknown_scan_seconds=-1)


async def _readiness_ok() -> ReadinessReason:
    return ReadinessReason.OK


class _AuditWriter:
    async def add(self, session: AsyncSession, event: AuditEventDraft) -> AuditEventRecord:
        raise AssertionError("attention test has no business transaction")

    async def write(self, event: AuditEventDraft) -> AuditEventRecord:
        return cast(AuditEventRecord, object())


class _Authenticator:
    def __init__(self, role: UserRole) -> None:
        user = UserRecord(
            id=USER_ID,
            username_norm="attention-user",
            display_name="Attention User",
            role=role,
            status=UserStatus.ACTIVE,
            must_change_password=False,
            created_at=NOW,
            updated_at=NOW,
            deleted_at=None,
            password_hash="$argon2id$not-returned",
        )
        session = SessionRecord(
            id=UUID("7e000000-0000-4000-8000-000000000004"),
            user_id=user.id,
            session_hash="a" * 64,
            csrf_hash="b" * 64,
            created_at=NOW,
            last_seen_at=NOW,
            expires_at=NOW + timedelta(days=3650),
            revoked_at=None,
        )
        self.authenticated = AuthenticatedSession(user=user, session=session)
        self.audit_writer = _AuditWriter()

    async def authenticate_session(
        self,
        *,
        session_token: str | None,
        csrf_token: str | None = None,
        require_csrf: bool = False,
    ) -> AuthenticatedSession:
        return self.authenticated


class _Attention:
    def __init__(self, items: tuple[AttentionItem, ...]) -> None:
        self._items = items
        self.calls = 0

    async def items(self) -> tuple[AttentionItem, ...]:
        self.calls += 1
        return self._items


def _get(role: UserRole, attention: _Attention) -> tuple[int, bytes]:
    app = create_app(
        version_payload=VersionPayload(
            build_version="1.0.0+gm506",
            commit="1" * 40,
            schema_version=1,
            runtime_compat_hash="2" * 64,
        ),
        readiness_checks=[DependencyCheck(name="database", probe=_readiness_ok)],
        credential_startup_check=NO_CONFIGURED_CREDENTIAL_ROWS,
        authentication=cast(AuthenticationService, _Authenticator(role)),
        attention=cast(AttentionService, attention),
    )

    async def scenario() -> tuple[int, bytes]:
        client = AsgiTestClient(app)
        async with client.lifespan():
            response = await client.get(ATTENTION_PATH)
            return response.status, response.body

    return asyncio.run(scenario())


def test_M5_06_attention_api_rejects_normal_users() -> None:
    attention = _Attention(
        (AttentionItem(kind=AttentionKind.UNKNOWN_REQUESTS, count=1, latest_at=NOW),)
    )
    status, body = _get(UserRole.USER, attention)
    assert status == 403
    assert attention.calls == 0
    assert b"unknown_requests" not in body


def test_M5_06_attention_api_returns_counts_without_request_content() -> None:
    attention = _Attention(
        (AttentionItem(kind=AttentionKind.UNKNOWN_REQUESTS, count=4, latest_at=NOW),)
    )
    status, body = _get(UserRole.ADMIN, attention)
    assert status == 200
    assert json.loads(body) == {
        "items": [
            {
                "kind": "unknown_requests",
                "severity": "notice",
                "target_id": None,
                "target_name": None,
                "detail": None,
                "count": 4,
                "latest_at": "2026-09-17T09:00:00Z",
            }
        ]
    }


def test_M5_06_attention_api_returns_empty_list_when_nothing_to_handle() -> None:
    status, body = _get(UserRole.ADMIN, _Attention(()))
    assert status == 200
    assert json.loads(body) == {"items": []}
