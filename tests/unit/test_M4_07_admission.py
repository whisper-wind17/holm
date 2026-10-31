"""M4-07 fail-fast admission, pool timeout, and Permit lifecycle coverage."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable, Coroutine
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
from holm_domain import ErrorCode, ModelInstanceStatus
from holm_gateway.application import create_app
from holm_gateway.authentication import ApiKeyAuthenticator
from holm_gateway.configuration import GatewayConfiguration, GatewayConfigurationError
from holm_gateway.openai_types import OpenAIRequestExtension
from holm_gateway.request_context import RequestRecordContext
from holm_gateway.routing import GatewayRoute
from holm_gateway.vllm_adapter import VllmRuntimeAdapter
from holm_ledger import FailedEvent, LedgerEvent, RejectedEvent
from holm_protocols import (
    AdmissionController,
    CanonicalMessage,
    CanonicalRequest,
    ContentDelta,
    InferenceCore,
    InferenceFailure,
    SamplingParameters,
    ServiceModelReference,
    ServiceRoute,
    TextContent,
)
from holm_runtimes import load_runtime_compatibility
from holm_security import (
    ApiKeyPepperKeyring,
    ApiKeyValidationSnapshot,
    issue_api_key,
)

SERVICE_ID = UUID("77000000-0000-4000-8000-000000000001")
INSTANCE_A = UUID("77000000-0000-4000-8000-000000000002")
INSTANCE_B = UUID("77000000-0000-4000-8000-000000000003")
USER_ID = UUID("77000000-0000-4000-8000-000000000004")
KEY_ID = UUID("77000000-0000-4000-8000-000000000005")
REQUEST_ID = UUID("77000000-0000-4000-8000-000000000006")
NOW = datetime(2026, 9, 17, tzinfo=UTC)
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
VLLM_RUNTIME = load_runtime_compatibility(REPOSITORY_ROOT / "runtime-compat.yaml").runtime("vllm")
assert VLLM_RUNTIME is not None


class _UnusedAuthenticator:
    async def authenticate(self, credential: str | None) -> Any:
        raise AssertionError("infer_routed must not authenticate again")


class _UnusedRoutes:
    async def find(self, public_model_name: str) -> None:
        raise AssertionError("infer_routed must not resolve again")

    async def list_enabled(self) -> tuple[()]:
        return ()


class _OneEventRuntime:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def stream(
        self,
        endpoint: str,
        request_id: UUID,
        canonical_request: CanonicalRequest,
    ) -> AsyncIterator[ContentDelta]:
        del request_id, canonical_request
        self.calls.append(endpoint)
        yield ContentDelta("ok")


class _RecordingSink:
    def __init__(self) -> None:
        self.records: list[RejectedEvent | FailedEvent] = []

    async def submit(self, event: LedgerEvent) -> None:
        if isinstance(event, (RejectedEvent, FailedEvent)):
            self.records.append(event)


class _Routes:
    def __init__(self, route: GatewayRoute) -> None:
        self.route = route

    async def find(self, public_model_name: str) -> GatewayRoute | None:
        return self.route if public_model_name == self.route.public_model_name else None

    async def list_enabled(self) -> tuple[GatewayRoute, ...]:
        return (self.route,)


def _route(instance_id: UUID = INSTANCE_A, endpoint: str = "runtime-a:8000") -> ServiceRoute:
    return ServiceRoute(
        service_id=SERVICE_ID,
        instance_id=instance_id,
        public_model_name="m4-07-model",
        enabled=True,
        ready=True,
        endpoint=endpoint,
        runtime_name="vllm",
        runtime_model_name="internal-model",
    )


def _gateway_route() -> GatewayRoute:
    return GatewayRoute(
        service_id=SERVICE_ID,
        instance_id=INSTANCE_A,
        public_model_name="m4-07-model",
        enabled=True,
        instance_status=ModelInstanceStatus.RUNNING.value,
        runtime_name="vllm",
        internal_endpoint="runtime-a:8000",
        internal_model_name="internal-model",
    )


def _request(*, stream: bool) -> CanonicalRequest:
    return CanonicalRequest(
        service_model=ServiceModelReference("m4-07-model"),
        stream=stream,
        sampling=SamplingParameters(maximum_output_tokens=8),
        messages=(CanonicalMessage("user", (TextContent("not persisted"),)),),
        tools=(),
        extension=OpenAIRequestExtension(False),
    )


def _record() -> RequestRecordContext:
    return RequestRecordContext(REQUEST_ID, NOW, time.monotonic())


def _core(
    runtime: Any, admission: AdmissionController, *, first_token: float = 1.0
) -> InferenceCore:
    return InferenceCore(
        authenticator=_UnusedAuthenticator(),
        routes=_UnusedRoutes(),
        runtime=runtime,
        admission=admission,
        first_token_timeout_seconds=first_token,
        total_timeout_seconds=2.0,
    )


async def _infer(
    runtime: Any,
    admission: AdmissionController,
    *,
    stream: bool = False,
    first_token: float = 1.0,
) -> tuple[InferenceCore, RequestRecordContext]:
    record = _record()
    response = await _core(runtime, admission, first_token=first_token).infer_routed(
        route=_route(),
        request_id=REQUEST_ID,
        request=_request(stream=stream),
        lifecycle=record,
    )
    async for _ in response.events:
        pass
    return _core(runtime, admission), record


def _runtime_client(
    handler: Callable[[httpx.Request], Coroutine[Any, Any, httpx.Response]],
) -> tuple[VllmRuntimeAdapter, httpx.AsyncClient]:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=None)
    return (
        VllmRuntimeAdapter(client, max_response_bytes=4096, max_sse_event_bytes=4096),
        client,
    )


async def _assert_zero(admission: AdmissionController) -> None:
    global_active, by_instance = await admission.active_counts()
    assert global_active == 0
    assert by_instance == {}


def test_M4_07_normal_completion_releases_global_and_instance_permits() -> None:
    async def upstream(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            content=b"data: "
            + json.dumps(
                {"model": "internal-model", "choices": [{"finish_reason": "stop"}]}
            ).encode()
            + b"\n\ndata: [DONE]\n\n",
        )

    async def scenario() -> None:
        admission = AdmissionController(2, 1)
        runtime, client = _runtime_client(upstream)
        try:
            await _infer(runtime, admission)
            await _assert_zero(admission)
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_M4_07_stream_client_disconnect_releases_global_and_instance_permits() -> None:
    class BlockingStream(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b'data: {"model":"internal-model","choices":[{"delta":{"content":"a"}}]}\n\n'
            await asyncio.Event().wait()

    async def upstream(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, stream=BlockingStream())

    async def scenario() -> None:
        admission = AdmissionController(2, 1)
        runtime, client = _runtime_client(upstream)
        try:
            response = await _core(runtime, admission).infer_routed(
                route=_route(),
                request_id=REQUEST_ID,
                request=_request(stream=True),
                lifecycle=_record(),
            )
            await anext(response.events)
            await response.events.aclose()
            await _assert_zero(admission)
        finally:
            await client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["runtime_5xx", "connection_failed"])
def test_M4_07_upstream_failure_releases_global_and_instance_permits(failure: str) -> None:
    async def upstream(request: httpx.Request) -> httpx.Response:
        if failure == "connection_failed":
            raise httpx.ConnectError("internal detail", request=request)
        return httpx.Response(500)

    async def scenario() -> None:
        admission = AdmissionController(2, 1)
        runtime, client = _runtime_client(upstream)
        try:
            with pytest.raises(InferenceFailure) as raised:
                await _infer(runtime, admission)
            assert raised.value.error.code is ErrorCode.BACKEND_UNAVAILABLE
            await _assert_zero(admission)
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_M4_07_first_token_timeout_releases_global_and_instance_permits() -> None:
    class NeverResponds(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            await asyncio.Event().wait()
            yield b"unreachable"

    async def upstream(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, stream=NeverResponds())

    async def scenario() -> None:
        admission = AdmissionController(2, 1)
        runtime, client = _runtime_client(upstream)
        try:
            with pytest.raises(InferenceFailure) as raised:
                await _infer(runtime, admission, stream=True, first_token=0.01)
            assert raised.value.error.code is ErrorCode.INFERENCE_TIMEOUT
            await _assert_zero(admission)
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_M4_07_instance_limit_does_not_block_another_instance() -> None:
    async def scenario() -> None:
        admission = AdmissionController(2, 1)
        occupied = await admission.try_acquire(INSTANCE_A)
        assert occupied is not None
        runtime = _OneEventRuntime()
        try:
            with pytest.raises(InferenceFailure) as raised:
                await _core(runtime, admission).infer_routed(
                    route=_route(INSTANCE_A),
                    request_id=REQUEST_ID,
                    request=_request(stream=False),
                    lifecycle=_record(),
                )
            assert raised.value.error.code is ErrorCode.GATEWAY_OVERLOADED
            response = await _core(runtime, admission).infer_routed(
                route=_route(INSTANCE_B, "runtime-b:8000"),
                request_id=REQUEST_ID,
                request=_request(stream=False),
                lifecycle=_record(),
            )
            assert [event async for event in response.events]
            assert runtime.calls == ["runtime-b:8000"]
        finally:
            await occupied.release()
        await _assert_zero(admission)

    asyncio.run(scenario())


def test_M4_07_full_global_limit_rejects_all_concurrent_requests_without_waiting() -> None:
    async def scenario() -> None:
        admission = AdmissionController(1, 1)
        occupied = await admission.try_acquire(INSTANCE_A)
        assert occupied is not None
        runtime = _OneEventRuntime()

        async def rejected() -> ErrorCode:
            with pytest.raises(InferenceFailure) as raised:
                await _core(runtime, admission).infer_routed(
                    route=_route(),
                    request_id=REQUEST_ID,
                    request=_request(stream=False),
                    lifecycle=_record(),
                )
            return raised.value.error.code

        started = time.monotonic()
        codes = await asyncio.wait_for(asyncio.gather(*(rejected() for _ in range(12))), 0.3)
        assert time.monotonic() - started < 0.3
        assert codes == [ErrorCode.GATEWAY_OVERLOADED] * 12
        assert runtime.calls == []
        await occupied.release()
        response = await _core(runtime, admission).infer_routed(
            route=_route(),
            request_id=REQUEST_ID,
            request=_request(stream=False),
            lifecycle=_record(),
        )
        assert [event async for event in response.events]
        await _assert_zero(admission)

    asyncio.run(scenario())


def test_M4_07_pool_timeout_is_under_300_ms_maps_overload_and_releases_permit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class PoolBlockedClient:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

        def build_request(self, method: str, url: str, **kwargs: object) -> httpx.Request:
            return httpx.Request(method, url, **kwargs)

        async def send(self, request: httpx.Request, *, stream: bool) -> httpx.Response:
            del stream
            timeout = captured["timeout"]
            assert isinstance(timeout, httpx.Timeout)
            await asyncio.sleep(timeout.pool or 0)
            raise httpx.PoolTimeout("pool full", request=request)

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(httpx, "AsyncClient", PoolBlockedClient)

    async def scenario() -> None:
        configuration = GatewayConfiguration(runtime_pool_timeout_ms=100)
        admission = AdmissionController(2, 2)
        runtime = VllmRuntimeAdapter(
            None,
            max_response_bytes=4096,
            max_sse_event_bytes=4096,
            pool_max_connections=configuration.resolved_max_inflight_per_instance,
            pool_timeout_seconds=configuration.runtime_pool_timeout_ms / 1000,
        )
        started = time.monotonic()
        with pytest.raises(InferenceFailure) as raised:
            await _infer(runtime, admission)
        elapsed = time.monotonic() - started
        assert raised.value.error.code is ErrorCode.GATEWAY_OVERLOADED
        assert 0.08 <= elapsed < 0.3
        timeout = captured["timeout"]
        assert isinstance(timeout, httpx.Timeout) and timeout.pool == pytest.approx(0.1)
        assert timeout.connect is None and timeout.read is None and timeout.write is None
        await _assert_zero(admission)

    asyncio.run(scenario())


def test_M4_07_overload_request_is_audited_with_gateway_overloaded() -> None:
    async def scenario() -> None:
        keyring = ApiKeyPepperKeyring(current_key_id="m4-07", keys={"m4-07": b"K" * 32})
        issued = issue_api_key(keyring)

        async def lookup(lookup_id: str) -> ApiKeyValidationSnapshot | None:
            if lookup_id != issued.lookup_id:
                return None
            return ApiKeyValidationSnapshot(
                user_id=USER_ID,
                api_key_id=KEY_ID,
                pepper_key_id=issued.pepper_key_id,
                secret_hmac=issued.secret_hmac,
                revoked=False,
                expired_status=False,
                expires_at=None,
                user_active=True,
                scoped_service_ids=frozenset(),
                database_now=NOW,
                user_is_admin=True,
                authorized_service_ids=frozenset({SERVICE_ID}),
            )

        async def unused(request: httpx.Request) -> httpx.Response:
            raise AssertionError(f"overload reached Runtime: {request.url}")

        admission = AdmissionController(1, 1)
        occupied = await admission.try_acquire(INSTANCE_A)
        assert occupied is not None
        sink = _RecordingSink()
        upstream = httpx.AsyncClient(transport=httpx.MockTransport(unused), timeout=None)
        app = create_app(
            authenticator=ApiKeyAuthenticator(lookup=lookup, keyring=keyring),
            routes=_Routes(_gateway_route()),
            runtime_capabilities={"vllm": VLLM_RUNTIME.request_capabilities},
            upstream_client=upstream,
            admission=admission,
            request_ledger=sink,
        )
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://gateway"
            ) as client:
                started = time.monotonic()
                responses = await asyncio.wait_for(
                    asyncio.gather(
                        *(
                            client.post(
                                "/v1/chat/completions",
                                headers={"Authorization": f"Bearer {issued.plaintext}"},
                                json={"model": "m4-07-model", "messages": []},
                            )
                            for _ in range(8)
                        )
                    ),
                    timeout=0.3,
                )
                assert time.monotonic() - started < 0.3
            assert [response.status_code for response in responses] == [503] * 8
            assert [response.json()["error"]["code"] for response in responses] == [
                "gateway_overloaded"
            ] * 8
            assert len(sink.records) == 8
            assert all(record.error_code is ErrorCode.GATEWAY_OVERLOADED for record in sink.records)
        finally:
            await occupied.release()
            await upstream.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("values", "code"),
    (
        ({"max_inflight_per_instance": 0}, "max_inflight_per_instance_invalid"),
        ({"max_inflight_per_instance": "invalid"}, "max_inflight_per_instance_invalid"),
        ({"runtime_pool_timeout_ms": 0}, "runtime_pool_timeout_invalid"),
        ({"runtime_pool_timeout_ms": -1}, "runtime_pool_timeout_invalid"),
    ),
)
def test_M4_07_invalid_admission_and_pool_configuration_fails_loudly(
    values: dict[str, object], code: str
) -> None:
    with pytest.raises(GatewayConfigurationError) as raised:
        GatewayConfiguration(**values)  # type: ignore[arg-type]
    assert raised.value.code == code


def test_M4_07_auto_and_explicit_instance_limits_resolve_against_global_limit() -> None:
    assert GatewayConfiguration(max_inflight_requests=7).resolved_max_inflight_per_instance == 7
    assert (
        GatewayConfiguration(
            max_inflight_requests=7, max_inflight_per_instance=3
        ).resolved_max_inflight_per_instance
        == 3
    )
    assert (
        GatewayConfiguration(
            max_inflight_requests=7, max_inflight_per_instance=99
        ).resolved_max_inflight_per_instance
        == 7
    )


def test_M4_07_instance_counter_entry_is_reclaimed_after_last_release() -> None:
    async def scenario() -> None:
        admission = AdmissionController(3, 2)
        first = await admission.try_acquire(INSTANCE_A)
        second = await admission.try_acquire(INSTANCE_A)
        assert first is not None and second is not None
        await first.release()
        assert await admission.active_counts() == (1, {INSTANCE_A: 1})
        await second.release()
        await _assert_zero(admission)

    asyncio.run(scenario())
