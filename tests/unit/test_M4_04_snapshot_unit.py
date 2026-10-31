"""M4-04 unit coverage for immutable Route Snapshot and fail-closed policy."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from types import MappingProxyType
from uuid import UUID

import pytest
from holm_control_api.event_stream import EventFanout
from holm_domain import (
    PRODUCTION_EVENT_REGISTRY,
    ApiKeyStatus,
    AuthorizationSubject,
    DomainEvent,
    DomainEventType,
    ErrorCode,
    UserRole,
    UserStatus,
)
from holm_gateway.authentication import ApiKeyAuthenticator
from holm_gateway.configuration import GatewayConfiguration, GatewayConfigurationError
from holm_gateway.route_snapshot import (
    RouteSnapshot,
    RouteSnapshotManager,
    RouteSnapshotUnavailable,
    SnapshotApiKey,
    SnapshotApiKeyValidationLookup,
    SnapshotUser,
)
from holm_gateway.routing import GatewayRoute, InferenceRouteDirectory
from holm_protocols import InferenceFailure
from holm_security import ApiKeyPepperKeyring, ApiKeyValidationError, issue_api_key

USER_ID = UUID("74000000-0000-4000-8000-000000000001")
KEY_ID = UUID("74000000-0000-4000-8000-000000000002")
SERVICE_ID = UUID("74000000-0000-4000-8000-000000000003")
INSTANCE_ID = UUID("74000000-0000-4000-8000-000000000004")
LOOKUP_ID = "A" * 26
NOW = datetime(2026, 9, 17, tzinfo=UTC)


class _Clock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value


class _Source:
    def __init__(self, clock: _Clock) -> None:
        self.clock = clock
        self.version = 1
        self.endpoint = "http://127.0.0.1:9001"
        self.loads = 0
        self.fail = False

    async def load(self, *, loaded_at: float) -> RouteSnapshot:
        if self.fail:
            raise RuntimeError("database unavailable")
        self.loads += 1
        return _snapshot(version=self.version, loaded_at=loaded_at, endpoint=self.endpoint)

    async def current_event_sequence(self) -> int:
        if self.fail:
            raise RuntimeError("database unavailable")
        return self.version


def _snapshot(*, version: int, loaded_at: float, endpoint: str) -> RouteSnapshot:
    key = SnapshotApiKey(
        id=KEY_ID,
        user_id=USER_ID,
        pepper_key_id="current",
        secret_hmac=b"S" * 32,
        status=ApiKeyStatus.ACTIVE,
        expires_at=None,
    )
    user = SnapshotUser(
        status=UserStatus.ACTIVE,
        is_admin=False,
        authorized_service_ids=frozenset({SERVICE_ID}),
    )
    route = GatewayRoute(
        service_id=SERVICE_ID,
        instance_id=INSTANCE_ID,
        public_model_name="m4-04-model",
        enabled=True,
        instance_status="running",
        runtime_name="vllm",
        internal_endpoint=endpoint,
        internal_model_name="internal-model",
        route_version=version,
    )
    return RouteSnapshot(
        version=version,
        loaded_at=loaded_at,
        api_keys_by_lookup=MappingProxyType({LOOKUP_ID: key}),
        users_by_id=MappingProxyType({USER_ID: user}),
        key_scopes_by_key_id=MappingProxyType({KEY_ID: frozenset({SERVICE_ID})}),
        routes_by_model=MappingProxyType({route.public_model_name: route}),
    )


def test_M4_04_snapshot_is_immutable_and_repr_hides_sensitive_route_facts() -> None:
    snapshot = _snapshot(version=1, loaded_at=100.0, endpoint="http://secret-internal")

    with pytest.raises(TypeError):
        snapshot.routes_by_model["other"] = snapshot.routes_by_model["m4-04-model"]  # type: ignore[index]
    rendered = repr(snapshot)
    assert "secret-internal" not in rendered
    assert repr(b"S" * 32) not in rendered
    assert "password" not in snapshot.__dataclass_fields__
    ordinary_user = AuthorizationSubject(USER_ID, UserRole.USER)
    fanout = EventFanout(PRODUCTION_EVENT_REGISTRY)
    for event_type in (
        DomainEventType.API_KEY_CHANGED,
        DomainEventType.USER_ACCESS_CHANGED,
        DomainEventType.AI_SERVICE_CHANGED,
        DomainEventType.INSTANCE_ROUTE_CHANGED,
    ):
        draft = PRODUCTION_EVENT_REGISTRY.build_draft(
            event_type.value,
            aggregate_id=SERVICE_ID,
            version=1,
            candidates={
                "api_key": "plaintext",
                "secret_hmac": "digest",
                "password_hash": "hash",
                "messages": "body",
                "internal_endpoint": "internal",
            },
        )
        event = DomainEvent(
            id=1,
            event=draft.event,
            aggregate_id=draft.aggregate_id,
            version=draft.version,
            occurred_at=NOW,
            payload=draft.payload,
        )
        assert event.payload_json() == {}
        assert fanout.can_deliver(event, ordinary_user) is False


def test_M4_04_lookup_uses_gateway_utc_and_never_calls_source_on_hot_path() -> None:
    async def scenario() -> None:
        clock = _Clock()
        source = _Source(clock)
        manager = RouteSnapshotManager(source, listener=None, clock=clock)
        await manager.refresh()
        source.fail = True

        record = await SnapshotApiKeyValidationLookup(manager, utc_now=lambda: NOW)(LOOKUP_ID)
        route = await manager.find("m4-04-model")

        assert record is not None
        assert record.database_now == NOW
        assert record.secret_hmac == b"S" * 32
        assert record.scoped_service_ids == frozenset({SERVICE_ID})
        assert route is not None and route.internal_endpoint == "http://127.0.0.1:9001"
        assert source.loads == 1

    asyncio.run(scenario())


def test_M4_04_key_expiry_uses_gateway_utc_without_database_access() -> None:
    async def scenario() -> None:
        clock = _Clock()
        keyring = ApiKeyPepperKeyring(
            current_key_id="current",
            keys={"current": b"K" * 32},
        )
        issued = issue_api_key(keyring)
        key = SnapshotApiKey(
            id=KEY_ID,
            user_id=USER_ID,
            pepper_key_id=issued.pepper_key_id,
            secret_hmac=issued.secret_hmac,
            status=ApiKeyStatus.ACTIVE,
            expires_at=NOW,
        )
        snapshot = replace(
            _snapshot(version=1, loaded_at=clock(), endpoint="http://127.0.0.1:9001"),
            api_keys_by_lookup=MappingProxyType({issued.lookup_id: key}),
        )

        class FixedSource:
            async def load(self, *, loaded_at: float) -> RouteSnapshot:
                return replace(snapshot, loaded_at=loaded_at)

            async def current_event_sequence(self) -> int:
                raise AssertionError("hot-path expiry must not poll PostgreSQL")

        manager = RouteSnapshotManager(FixedSource(), listener=None, clock=clock)
        await manager.refresh()
        authenticator = ApiKeyAuthenticator(
            lookup=SnapshotApiKeyValidationLookup(manager, utc_now=lambda: NOW),
            keyring=keyring,
            availability_check=manager.ensure_available,
        )

        with pytest.raises(ApiKeyValidationError) as raised:
            await authenticator.authenticate(issued.plaintext)
        assert raised.value.code == ErrorCode.API_KEY_EXPIRED.value

    asyncio.run(scenario())


def test_M4_04_snapshot_absent_fails_closed() -> None:
    manager = RouteSnapshotManager(_Source(_Clock()), listener=None)

    with pytest.raises(RouteSnapshotUnavailable):
        manager.ensure_available()


def test_M4_04_database_failure_serves_through_60_seconds_then_fails_closed() -> None:
    async def scenario() -> None:
        clock = _Clock()
        source = _Source(clock)
        manager = RouteSnapshotManager(
            source,
            listener=None,
            max_age_seconds=60.0,
            clock=clock,
        )
        await manager.refresh()
        source.fail = True
        await manager.check_once()

        clock.value = 160.0
        manager.ensure_available()
        clock.value = 160.001
        with pytest.raises(RouteSnapshotUnavailable):
            manager.ensure_available()

    asyncio.run(scenario())


def test_M4_04_successful_sequence_poll_renews_database_proof_without_reload() -> None:
    async def scenario() -> None:
        clock = _Clock()
        source = _Source(clock)
        manager = RouteSnapshotManager(source, listener=None, clock=clock)
        await manager.refresh()
        clock.value = 120.0

        await manager.check_once()

        assert manager.last_confirmed_at == 120.0
        assert source.loads == 1

    asyncio.run(scenario())


def test_M4_04_poll_detects_new_version_and_atomically_replaces_route() -> None:
    async def scenario() -> None:
        clock = _Clock()
        source = _Source(clock)
        manager = RouteSnapshotManager(source, listener=None, clock=clock)
        await manager.refresh()
        old_route = await manager.find("m4-04-model")
        source.version = 2
        source.endpoint = "http://127.0.0.1:9002"

        await manager.check_once()
        new_route = await manager.find("m4-04-model")

        assert old_route is not None and old_route.internal_endpoint.endswith("9001")
        assert new_route is not None and new_route.internal_endpoint.endswith("9002")
        assert old_route.internal_endpoint.endswith("9001")
        assert manager.snapshot.version == 2

    asyncio.run(scenario())


def test_M4_04_recovery_forces_refresh_even_when_sequence_is_unchanged() -> None:
    async def scenario() -> None:
        clock = _Clock()
        source = _Source(clock)
        manager = RouteSnapshotManager(source, listener=None, clock=clock)
        await manager.refresh()
        source.fail = True
        await manager.check_once()
        source.fail = False

        await manager.check_once()

        assert source.loads == 2
        manager.ensure_available()

    asyncio.run(scenario())


def test_M4_04_authentication_checks_snapshot_age_before_parsing_credential() -> None:
    async def scenario() -> None:
        clock = _Clock()
        manager = RouteSnapshotManager(
            _Source(clock), listener=None, max_age_seconds=60.0, clock=clock
        )
        await manager.refresh()
        clock.value = 161.0
        authenticator = ApiKeyAuthenticator(
            lookup=SnapshotApiKeyValidationLookup(manager, utc_now=lambda: NOW),
            keyring=ApiKeyPepperKeyring(current_key_id="current", keys={"current": b"K" * 32}),
            availability_check=manager.ensure_available,
        )

        with pytest.raises(InferenceFailure) as raised:
            await authenticator.authenticate("malformed")
        assert raised.value.error.code is ErrorCode.CONTROL_PLANE_UNAVAILABLE

    asyncio.run(scenario())


def test_M4_04_no_primary_route_is_unavailable_without_historical_fallback() -> None:
    route = GatewayRoute(
        service_id=SERVICE_ID,
        instance_id=None,
        public_model_name="m4-04-model",
        enabled=True,
        instance_status=None,
        runtime_name="",
        internal_endpoint=None,
        internal_model_name="",
        route_version=2,
    )

    adapted = InferenceRouteDirectory(_SingleRouteDirectory(route))
    selected = asyncio.run(adapted.find("m4-04-model"))

    assert selected is not None
    assert selected.instance_id is None
    assert selected.ready is False
    assert selected.endpoint is None


class _SingleRouteDirectory:
    def __init__(self, route: GatewayRoute) -> None:
        self.route = route

    async def find(self, public_model_name: str) -> GatewayRoute | None:
        return self.route if public_model_name == self.route.public_model_name else None

    async def list_enabled(self) -> tuple[GatewayRoute, ...]:
        return (self.route,)


@pytest.mark.parametrize(
    ("values", "code"),
    (
        ({"route_snapshot_poll_seconds": 0.09}, "route_snapshot_poll_invalid"),
        ({"route_snapshot_poll_seconds": 2.01}, "route_snapshot_poll_invalid"),
        ({"route_snapshot_max_age_seconds": 0.9}, "route_snapshot_max_age_invalid"),
        ({"route_snapshot_max_age_seconds": 300.1}, "route_snapshot_max_age_invalid"),
    ),
)
def test_M4_04_snapshot_configuration_bounds(values: dict[str, float], code: str) -> None:
    with pytest.raises(GatewayConfigurationError) as raised:
        GatewayConfiguration(**values)  # type: ignore[arg-type]
    assert raised.value.code == code
