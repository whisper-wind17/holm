"""M4-05 contract proof that the inference core has no OpenAI DTO dependency."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from holm_domain import ErrorCode
from holm_gateway.openai_adapter import OpenAIProtocolAdapter
from holm_gateway.request_context import RequestRecordContext
from holm_gateway.vllm_adapter import VllmRuntimeAdapter
from holm_protocols import (
    AdmissionController,
    AuthenticatedPrincipal,
    CanonicalEvent,
    CanonicalMessage,
    CanonicalRequest,
    Completed,
    ContentDelta,
    InferenceCore,
    InferenceFailure,
    MappedError,
    PlatformError,
    ProtocolExtension,
    ProtocolRoute,
    ReasoningControl,
    ReasoningEffort,
    RuntimeAdapter,
    SamplingParameters,
    ServiceModelReference,
    ServiceRoute,
    StopReason,
    StreamEnded,
    TextContent,
    UsageInfo,
    UsageReported,
)
from holm_protocols import models as canonical_models
from holm_runtimes import load_runtime_compatibility

SERVICE_ID = UUID("00000000-0000-7000-8000-000000004501")
INSTANCE_ID = UUID("00000000-0000-7000-8000-000000004502")
USER_ID = UUID("00000000-0000-7000-8000-000000004503")
KEY_ID = UUID("00000000-0000-7000-8000-000000004504")
REQUEST_ID = UUID("00000000-0000-7000-8000-000000004505")
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
VLLM_CAPABILITIES = load_runtime_compatibility(REPOSITORY_ROOT / "runtime-compat.yaml").runtime(
    "vllm"
)
assert VLLM_CAPABILITIES is not None


@dataclass(frozen=True, slots=True)
class FakeEventExtension(ProtocolExtension):
    sequence: int


class FakeProtocolAdapter:
    """Intentionally unlike OpenAI in credential, stream, and error shapes."""

    def __init__(self, reasoning: ReasoningControl | None = None) -> None:
        self.reasoning = reasoning

    def routes(self) -> tuple[ProtocolRoute, ...]:
        return (ProtocolRoute("POST", "/fake/generate", "fake_generate"),)

    def extract_credential(self, headers: Mapping[str, str]) -> str | None:
        return headers.get("x-workspace-key")

    def parse_request(self, body: bytes, *, runtime_name: str) -> CanonicalRequest:
        assert runtime_name == "fake"
        try:
            model, prompt = body.decode("utf-8").split("|", maxsplit=1)
        except (UnicodeDecodeError, ValueError):
            raise InferenceFailure(PlatformError(ErrorCode.INVALID_REQUEST)) from None
        return CanonicalRequest(
            service_model=ServiceModelReference(model),
            stream=True,
            sampling=SamplingParameters(maximum_output_tokens=16),
            messages=(CanonicalMessage("human", (TextContent(prompt),)),),
            tools=(),
            reasoning=self.reasoning,
        )

    def encode_stream_event(self, event: CanonicalEvent) -> bytes | None:
        if isinstance(event, ContentDelta):
            return f"chunk={event.text}\n".encode()
        if isinstance(event, StreamEnded):
            return b"end\n"
        return None

    def encode_non_stream(self, events: tuple[CanonicalEvent, ...]) -> bytes:
        return b"|".join(event.text.encode() for event in events if isinstance(event, ContentDelta))

    def map_error(self, error: PlatformError, request_id: UUID) -> MappedError:
        status = (
            401 if error.code in {ErrorCode.INVALID_API_KEY, ErrorCode.API_KEY_EXPIRED} else 503
        )
        return MappedError(
            status,
            f"problem={error.code.value};request={request_id}".encode(),
            "text/plain",
        )


class FakeAuthenticator:
    async def authenticate(self, credential: str | None) -> AuthenticatedPrincipal:
        if credential == "expired-secret":
            raise InferenceFailure(PlatformError(ErrorCode.API_KEY_EXPIRED))
        if credential != "fake-secret":
            raise InferenceFailure(PlatformError(ErrorCode.INVALID_API_KEY))
        return AuthenticatedPrincipal(USER_ID, KEY_ID, frozenset(), True, frozenset({SERVICE_ID}))


class FakeRoutes:
    def __init__(self, route: ServiceRoute | None) -> None:
        self.route = route

    async def find(self, public_model_name: str) -> ServiceRoute | None:
        if self.route is None or self.route.public_model_name != public_model_name:
            return None
        return self.route

    async def list_enabled(self) -> tuple[ServiceRoute, ...]:
        return () if self.route is None or not self.route.enabled else (self.route,)


class FakeRuntime:
    def __init__(self) -> None:
        self.calls = 0
        self.requests: list[CanonicalRequest] = []

    async def stream(
        self,
        endpoint: str,
        request_id: UUID,
        canonical_request: CanonicalRequest,
    ) -> AsyncIterator[CanonicalEvent]:
        assert endpoint == "fake-runtime:9000"
        assert request_id == REQUEST_ID
        assert canonical_request.service_model.runtime_model_name == "internal-model"
        self.calls += 1
        self.requests.append(canonical_request)
        yield ContentDelta("FAKE_SECRET_OUTPUT", FakeEventExtension(1))
        yield UsageReported(UsageInfo(input_tokens=3, output_tokens=2))
        yield Completed(StopReason.STOP)
        yield StreamEnded(FakeEventExtension(2))


def _route(
    *,
    enabled: bool = True,
    ready: bool = True,
) -> ServiceRoute:
    return ServiceRoute(
        service_id=SERVICE_ID,
        instance_id=INSTANCE_ID,
        public_model_name="fake-model",
        enabled=enabled,
        ready=ready,
        endpoint="fake-runtime:9000",
        runtime_name="fake",
        runtime_model_name="internal-model",
    )


def _record() -> RequestRecordContext:
    return RequestRecordContext(REQUEST_ID, datetime(2026, 9, 16, tzinfo=UTC), time.monotonic())


def _core(
    *,
    route: ServiceRoute | None = None,
    runtime: FakeRuntime | None = None,
    admission: AdmissionController | None = None,
) -> tuple[InferenceCore, FakeRuntime]:
    selected_runtime = runtime or FakeRuntime()
    return (
        InferenceCore(
            authenticator=FakeAuthenticator(),
            routes=FakeRoutes(_route() if route is None else route),
            runtime=selected_runtime,
            admission=admission or AdmissionController(2),
            first_token_timeout_seconds=1,
            total_timeout_seconds=2,
        ),
        selected_runtime,
    )


async def _invoke_fake(
    adapter: FakeProtocolAdapter,
    core: InferenceCore,
    *,
    headers: Mapping[str, str],
    body: bytes = b"fake-model|FAKE_SECRET_PROMPT",
    lifecycle: RequestRecordContext | None = None,
) -> tuple[int, bytes, RequestRecordContext]:
    record = lifecycle or _record()
    try:
        response = await core.infer(
            credential=adapter.extract_credential(headers),
            request_id=REQUEST_ID,
            request=adapter.parse_request(body, runtime_name="fake"),
            lifecycle=record,
        )
        encoded = bytearray()
        async for event in response.events:
            chunk = adapter.encode_stream_event(event)
            if chunk is not None:
                encoded.extend(chunk)
        return 200, bytes(encoded), record
    except InferenceFailure as failure:
        mapped = adapter.map_error(failure.error, REQUEST_ID)
        return mapped.status_code, mapped.body, record


@pytest.mark.parametrize("effort", (ReasoningEffort.NONE, ReasoningEffort.HIGH))
def test_FIX_20_TC_GW_21_fake_protocol_reasoning_reaches_runtime_without_openai_dto(
    effort: ReasoningEffort, caplog: pytest.LogCaptureFixture
) -> None:
    """FX-PA/FIX-20: the existing fake protocol with a neutral thinking control."""

    async def scenario() -> None:
        reasoning = ReasoningControl(effort)
        core, runtime = _core()
        with caplog.at_level(logging.INFO):
            status, _, record = await _invoke_fake(
                FakeProtocolAdapter(reasoning), core, headers={"x-workspace-key": "fake-secret"}
            )
        assert status == 200
        assert runtime.calls == 1
        assert runtime.requests[0].reasoning == reasoning
        assert runtime.requests[0].extension is None
        assert not hasattr(record, "reasoning")
        assert "FAKE_SECRET" not in repr(record)
        assert "FAKE_SECRET" not in caplog.text

    asyncio.run(scenario())


def test_M4_05_fake_adapter_uses_non_openai_header_stream_and_error_shapes() -> None:
    async def scenario() -> None:
        adapter = FakeProtocolAdapter()
        core, _ = _core()
        status, stream_body, _ = await _invoke_fake(
            adapter, core, headers={"x-workspace-key": "fake-secret"}
        )
        error_status, error_body, _ = await _invoke_fake(adapter, core, headers={})

        assert adapter.routes()[0].path == "/fake/generate"
        assert adapter.extract_credential({"Authorization": "Bearer fake-secret"}) is None
        assert status == 200
        assert stream_body == b"chunk=FAKE_SECRET_OUTPUT\nend\n"
        assert b"data:" not in stream_body and b"[DONE]" not in stream_body
        assert error_status == 401
        assert error_body.startswith(b"problem=invalid_api_key;")
        assert b'"error"' not in error_body

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("credential", "expected_status", "expected_code"),
    (
        ("fake-secret", 200, None),
        (None, 401, ErrorCode.INVALID_API_KEY),
        ("wrong-secret", 401, ErrorCode.INVALID_API_KEY),
        ("expired-secret", 401, ErrorCode.API_KEY_EXPIRED),
    ),
)
def test_M4_05_fake_adapter_authentication_four_states_share_core(
    credential: str | None,
    expected_status: int,
    expected_code: ErrorCode | None,
) -> None:
    async def scenario() -> None:
        core, runtime = _core()
        headers = {} if credential is None else {"x-workspace-key": credential}
        status, _, record = await _invoke_fake(FakeProtocolAdapter(), core, headers=headers)

        assert status == expected_status
        assert record.error_code == expected_code
        assert runtime.calls == (1 if expected_status == 200 else 0)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("route_mode", "expected_code"),
    (
        ("missing", ErrorCode.MODEL_NOT_FOUND),
        ("unauthorized", ErrorCode.SERVICE_NOT_AUTHORIZED),
        ("disabled", ErrorCode.SERVICE_DISABLED),
        ("unavailable", ErrorCode.BACKEND_UNAVAILABLE),
    ),
)
def test_M4_05_fake_adapter_route_four_states_share_core(
    route_mode: str,
    expected_code: ErrorCode,
) -> None:
    class ScopedAuthenticator(FakeAuthenticator):
        async def authenticate(self, credential: str | None) -> AuthenticatedPrincipal:
            principal = await super().authenticate(credential)
            if route_mode == "unauthorized":
                return replace(principal, scoped_service_ids=frozenset({UUID(int=1)}))
            return principal

    async def scenario() -> None:
        selected_route = {
            "missing": ServiceRoute(
                SERVICE_ID,
                INSTANCE_ID,
                "another-model",
                True,
                True,
                "fake-runtime:9000",
                "fake",
                "internal-model",
            ),
            "unauthorized": _route(),
            "disabled": _route(enabled=False),
            "unavailable": _route(ready=False),
        }[route_mode]
        runtime = FakeRuntime()
        core = InferenceCore(
            authenticator=ScopedAuthenticator(),
            routes=FakeRoutes(selected_route),
            runtime=runtime,
            admission=AdmissionController(1),
            first_token_timeout_seconds=1,
            total_timeout_seconds=2,
        )
        status, body, record = await _invoke_fake(
            FakeProtocolAdapter(), core, headers={"x-workspace-key": "fake-secret"}
        )

        assert status != 200
        assert expected_code.value.encode() in body
        assert record.error_code == expected_code
        assert runtime.calls == 0

    asyncio.run(scenario())


def test_M4_05_fake_adapter_overload_and_request_record_lifecycle() -> None:
    async def scenario() -> None:
        admission = AdmissionController(1)
        occupied = await admission.try_acquire()
        assert occupied is not None
        core, runtime = _core(admission=admission)
        try:
            status, body, record = await _invoke_fake(
                FakeProtocolAdapter(), core, headers={"x-workspace-key": "fake-secret"}
            )
            assert status == 503
            assert b"gateway_overloaded" in body
            assert record.status is not None and record.status.value == "rejected"
            assert runtime.calls == 0
        finally:
            await occupied.release()

        success_core, _ = _core()
        success_status, _, success_record = await _invoke_fake(
            FakeProtocolAdapter(),
            success_core,
            headers={"x-workspace-key": "fake-secret"},
        )
        assert success_status == 200
        assert success_record.status is not None and success_record.status.value == "completed"
        assert success_record.user_id == USER_ID and success_record.api_key_id == KEY_ID
        assert success_record.service_id == SERVICE_ID and success_record.instance_id == INSTANCE_ID
        assert success_record.input_tokens == 3 and success_record.output_tokens == 2
        assert success_record.finish_reason is not None
        assert success_record.finish_reason.value == "stop"

    asyncio.run(scenario())


def test_M4_05_canonical_content_stays_out_of_logs_records_and_errors(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def scenario() -> None:
        adapter = FakeProtocolAdapter()
        canonical = adapter.parse_request(b"fake-model|FAKE_SECRET_PROMPT", runtime_name="fake")
        assert "FAKE_SECRET_PROMPT" not in repr(canonical)

        core, _ = _core()
        with caplog.at_level(logging.INFO):
            status, body, record = await _invoke_fake(
                adapter, core, headers={"x-workspace-key": "fake-secret"}
            )
        assert status == 200
        assert "FAKE_SECRET_PROMPT" not in repr(record)
        assert "FAKE_SECRET_OUTPUT" not in repr(record)
        assert b"FAKE_SECRET_PROMPT" not in body
        assert all("FAKE_SECRET" not in record.getMessage() for record in caplog.records)

        error_status, error_body, _ = await _invoke_fake(
            adapter, core, headers={}, body=b"fake-model|FAKE_SECRET_ERROR_PROMPT"
        )
        assert error_status == 401
        assert b"FAKE_SECRET" not in error_body

    asyncio.run(scenario())


def test_M4_06_openai_private_extension_no_longer_carries_raw_body() -> None:
    captured: dict[str, object] = {}

    async def upstream(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            content=b"data: "
            + json.dumps(
                {"model": "internal-model", "choices": [{"finish_reason": "stop"}]}
            ).encode()
            + b"\n\ndata: [DONE]\n\n",
        )

    async def scenario() -> None:
        protocol = OpenAIProtocolAdapter(
            {"vllm": VLLM_CAPABILITIES.request_capabilities},
            maximum_json_depth=32,
        )
        canonical = protocol.parse_request(
            json.dumps(
                {
                    "model": "public-model",
                    "messages": [{"role": "user", "content": "secret"}],
                    "temperature": 0.75,
                }
            ).encode(),
            runtime_name="vllm",
        )
        canonical = replace(
            canonical,
            service_model=replace(canonical.service_model, runtime_model_name="internal-model"),
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            runtime = VllmRuntimeAdapter(
                client,
                max_response_bytes=1024,
                max_sse_event_bytes=1024,
            )
            events = tuple(
                [event async for event in runtime.stream("runtime:8000", REQUEST_ID, canonical)]
            )
        rendered = protocol.encode_non_stream(events)

        assert captured["model"] == "internal-model"
        assert captured["temperature"] == 0.75
        assert not hasattr(canonical.extension, "payload")
        assert json.loads(rendered)["model"] == "public-model"

    asyncio.run(scenario())


def test_M4_05_canonical_models_and_runtime_signature_are_protocol_neutral() -> None:
    source = inspect.getsource(canonical_models)
    signature = inspect.signature(RuntimeAdapter.stream)

    assert "dict[str, Any]" not in source
    assert "http" not in source.casefold()
    assert "openai" not in source.casefold()
    assert list(signature.parameters) == [
        "self",
        "endpoint",
        "request_id",
        "canonical_request",
    ]
