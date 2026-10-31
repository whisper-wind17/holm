"""Anthropic HTTP Protocol Adapter, sharing only canonical inference orchestration."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import suppress
from uuid import UUID

from fastapi import FastAPI
from holm_domain import ErrorCode
from holm_protocols import (
    CanonicalEvent,
    CanonicalRequest,
    Failed,
    InferenceCore,
    InferenceFailure,
    MappedError,
    PlatformError,
    ProtocolRoute,
    ServiceModelSummary,
)
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from holm_gateway.anthropic_request import invalid, obj, parse, string
from holm_gateway.anthropic_response import AnthropicResponseEncoder, error_body, render, sse
from holm_gateway.errors import describe_error
from holm_gateway.openai_adapter import _json_depth, _read_bounded_body, _validate_content_type
from holm_gateway.request_context import RequestRecordContext, request_id, request_lifecycle


class AnthropicProtocolAdapter:
    """Shared route/parser instance; encoding instances are bound per request."""

    def __init__(
        self,
        *,
        maximum_json_depth: int,
        maximum_response_bytes: int,
        encoder: AnthropicResponseEncoder | None = None,
    ) -> None:
        self.maximum_json_depth = maximum_json_depth
        self.maximum_response_bytes = maximum_response_bytes
        self.encoder = encoder

    def routes(self) -> tuple[ProtocolRoute, ...]:
        return (ProtocolRoute("POST", "/v1/messages", "messages"),)

    def extract_credential(self, headers: Mapping[str, str]) -> str | None:
        key = headers.get("x-api-key")
        authorization = headers.get("authorization")
        parts = () if authorization is None else tuple(authorization.split())
        bearer = parts[1] if len(parts) == 2 and parts[0].casefold() == "bearer" else None
        if key is not None and authorization is not None and key != bearer:
            raise InferenceFailure(PlatformError(ErrorCode.INVALID_API_KEY))
        return key if key is not None else bearer

    def decode(self, body: bytes) -> dict[str, object]:
        try:
            payload = obj(
                json.loads(body, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            )
            if _json_depth(payload) > self.maximum_json_depth:
                invalid()
            return payload
        except (ValueError, RecursionError):
            invalid()

    def parse_request(self, body: bytes, *, runtime_name: str) -> CanonicalRequest:
        del runtime_name
        return parse(self.decode(body))

    def bind(self, request_id_value: UUID, request: CanonicalRequest) -> AnthropicProtocolAdapter:
        return AnthropicProtocolAdapter(
            maximum_json_depth=self.maximum_json_depth,
            maximum_response_bytes=self.maximum_response_bytes,
            encoder=AnthropicResponseEncoder(
                request_id_value, request, maximum_bytes=self.maximum_response_bytes
            ),
        )

    def encode_stream_event(self, event: CanonicalEvent) -> bytes | None:
        if self.encoder is None:
            raise RuntimeError("Anthropic encoder must be bound to a request")
        return self.encoder.encode(event) or None

    def encode_non_stream(self, events: tuple[CanonicalEvent, ...]) -> bytes:
        if self.encoder is None:
            raise RuntimeError("Anthropic encoder must be bound to a request")
        return self.encoder.non_stream(events)

    def map_error(self, error: PlatformError, request_id_value: UUID) -> MappedError:
        status, _, _ = describe_error(error)
        headers: tuple[tuple[str, str], ...] = (
            ("Cache-Control", "no-store"),
            ("x-request-id", str(request_id_value)),
            ("request-id", str(request_id_value)),
        )
        if status == 429:
            headers += (("Retry-After", str(max(1, error.retry_after or 1))),)
        return MappedError(
            status, render(error_body(error, request_id_value)), "application/json", headers
        )

    def failure_response(self, error: PlatformError, request_id_value: UUID) -> Response:
        mapped = self.map_error(error, request_id_value)
        return Response(mapped.body, mapped.status_code, dict(mapped.headers), mapped.media_type)

    @staticmethod
    def models_response(models: tuple[ServiceModelSummary, ...]) -> JSONResponse:
        data = [
            {
                "type": "model",
                "id": model.public_model_name,
                "display_name": model.public_model_name,
                "created_at": "1970-01-01T00:00:00Z",
            }
            for model in models
        ]
        return JSONResponse(
            {
                "data": data,
                "has_more": False,
                "first_id": data[0]["id"] if data else None,
                "last_id": data[-1]["id"] if data else None,
            }
        )

    def install(
        self,
        application: FastAPI,
        core: InferenceCore,
        *,
        max_request_body_bytes: int,
        current_limits: Callable[[UUID, UUID], tuple[int | None, int | None]] | None = None,
    ) -> None:
        @application.post("/v1/messages")
        async def messages(request: Request) -> Response:
            lifecycle = request_lifecycle(request)
            credential = self.extract_credential(request.headers)
            started = lifecycle.stage_timings.now()
            try:
                if set(request.query_params) - {"beta"}:
                    invalid(*sorted(set(request.query_params) - {"beta"}))
                _validate_content_type(request)
                body = await _read_bounded_body(request, max_request_body_bytes)
                payload = self.decode(body)
                model = string(payload.get("model"))
                stream = payload.get("stream", False)
                if not isinstance(stream, bool):
                    invalid()
            except InferenceFailure:
                await core.authenticate(credential, lifecycle)
                raise
            lifecycle.stage_timings.finish_request_parse(started)
            lifecycle.set_stream(stream)
            lifecycle.raise_if_ledger_unavailable()
            lifecycle.begin_received(len(body))
            await lifecycle.raise_if_received_unavailable()
            started = lifecycle.stage_timings.now()
            try:
                principal = await core.authenticate(credential, lifecycle)
                route = await core.authorize_route(
                    principal=principal, public_model_name=model, lifecycle=lifecycle
                )
                canonical = parse(payload)
            finally:
                lifecycle.stage_timings.finish_auth_route(started)
            try:
                response = await core.infer_routed(
                    route=route,
                    principal=principal,
                    current_limits=current_limits,
                    disconnected=request.is_disconnected,
                    request_id=request_id(request),
                    request=canonical,
                    lifecycle=lifecycle,
                )
            except asyncio.CancelledError:
                if await request.is_disconnected():
                    return Response(status_code=499)
                raise
            codec = self.bind(request_id(request), canonical)
            if response.stream:
                return StreamingResponse(
                    codec.stream_body(response.events, lifecycle),
                    media_type="text/event-stream",
                    headers={"Cache-Control": "no-store"},
                )
            collected: list[CanonicalEvent] = []
            validator = self.bind(request_id(request), canonical)
            try:
                async for event in response.events:
                    # Validate blocks before advancing the Core to its terminal
                    # write, so malformed tool output is recorded as Failed.
                    validator.encode_stream_event(event)
                    collected.append(event)
            except InferenceFailure as failure:
                await lifecycle.mark_error(failure.error)
                raise
            finally:
                close = getattr(response.events, "aclose", None)
                if close is not None:
                    await close()
            return Response(
                codec.encode_non_stream(tuple(collected)), media_type="application/json"
            )

    async def stream_body(
        self,
        events: AsyncIterator[CanonicalEvent],
        lifecycle: RequestRecordContext,
        *,
        ping_seconds: float = 10,
    ) -> AsyncIterator[bytes]:
        assert self.encoder is not None
        # Keep the Core iterator in one task: its timeout spans yields and must
        # not migrate between one-shot anext tasks. One slot bounds read-ahead.
        queue: asyncio.Queue[CanonicalEvent | None] = asyncio.Queue(maxsize=1)

        async def produce() -> None:
            try:
                async for event in events:
                    await queue.put(event)
                    await queue.join()
            except InferenceFailure as failure:
                await queue.put(Failed(failure.error))
            except Exception:
                await queue.put(Failed(PlatformError(ErrorCode.BACKEND_UNAVAILABLE)))
            finally:
                close = getattr(events, "aclose", None)
                if close is not None:
                    await close()
            await queue.put(None)

        producer = asyncio.create_task(produce())
        loop = asyncio.get_running_loop()
        last_output = loop.time()
        try:
            while True:
                remaining = ping_seconds - (loop.time() - last_output)
                if remaining <= 0:
                    yield sse("ping", {})
                    last_output = loop.time()
                    continue
                try:
                    async with asyncio.timeout(remaining):
                        event = await queue.get()
                except TimeoutError:
                    continue
                try:
                    if event is None:
                        break
                    encoded = self.encode_stream_event(event)
                    if encoded:
                        yield encoded
                        last_output = loop.time()
                finally:
                    queue.task_done()
            if lifecycle.error_code is not None:
                raise InferenceFailure(PlatformError(lifecycle.error_code))
            yield self.encoder.finish()
        except InferenceFailure as failure:
            await lifecycle.mark_error(failure.error)
            yield sse("error", error_body(failure.error, self.encoder.request_id))
        finally:
            producer.cancel()
            with suppress(asyncio.CancelledError):
                await producer
