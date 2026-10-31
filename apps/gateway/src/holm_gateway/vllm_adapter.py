"""vLLM HTTP Runtime Adapter producing only canonical inference events."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncGenerator, AsyncIterator, Iterable
from dataclasses import dataclass, replace
from uuid import UUID

import httpx
from holm_domain import ErrorCode, ModelInstanceStatus
from holm_protocols import (
    CanonicalEvent,
    CanonicalMessage,
    CanonicalRequest,
    Completed,
    ContentDelta,
    ImageContent,
    InferenceFailure,
    PlatformError,
    ResponseCompleted,
    ResponseFormat,
    ResponseFormatKind,
    StopReason,
    StreamEnded,
    StreamStarted,
    TextContent,
    ThinkingDelta,
    ToolArgumentsDelta,
    ToolCallStarted,
    ToolChoiceMode,
    ToolDefinition,
    UsageInfo,
    UsageReported,
)

from holm_gateway.openai_response import OpenAIResponseAssembler
from holm_gateway.openai_types import (
    DataSseLine,
    JsonObject,
    OpenAIResponseExtension,
    OpenAISseExtension,
    RawSseLine,
    freeze_json,
    thaw_json,
)
from holm_gateway.routing import GatewayRoute
from holm_gateway.stage_timing import current_stage_timings

_CHAT_PATH = "/v1/chat/completions"
_HEALTH_PATH = "/health"

type _SseEvent = (
    StreamStarted
    | StreamEnded
    | ContentDelta
    | ThinkingDelta
    | ToolCallStarted
    | ToolArgumentsDelta
    | Completed
    | UsageReported
)


def _failure(code: ErrorCode, *, counts_toward_breaker: bool = False) -> InferenceFailure:
    return InferenceFailure(PlatformError(code), counts_toward_breaker=counts_toward_breaker)


def _upstream_url(endpoint: str, path: str = _CHAT_PATH) -> str:
    if (
        not endpoint
        or endpoint != endpoint.strip()
        or any(character.isspace() for character in endpoint)
        or any(character in endpoint for character in "/?#@")
    ):
        raise _failure(ErrorCode.BACKEND_UNAVAILABLE)
    return f"http://{endpoint}{path}"


def _raise_for_status(status_code: int) -> None:
    if 200 <= status_code < 300:
        return
    if status_code in {400, 422}:
        raise _failure(ErrorCode.INVALID_REQUEST)
    raise _failure(
        ErrorCode.BACKEND_UNAVAILABLE,
        counts_toward_breaker=500 <= status_code < 600,
    )


_MAX_ERROR_BYTES = 16 * 1024
_CONTEXT_LIMIT = re.compile(
    r"maximum context length is ([0-9]+) tokens|max_model_len=(?:max_total_tokens=)?([0-9]+)"
)


def _context_error(limit: str | None) -> PlatformError:
    # Conversion can reject excessive digit counts; never retain runtime prose.
    try:
        maximum = int(limit) if limit is not None else None
    except ValueError:
        maximum = None
    return PlatformError(
        ErrorCode.INVALID_REQUEST,
        reason="context_length_exceeded",
        max_context_tokens=maximum if maximum is not None and maximum > 0 else None,
    )


def _vllm_context_error(value: object) -> PlatformError:
    error = value.get("error") if isinstance(value, dict) else None
    if isinstance(error, dict) and error.get("param") in (
        "input_text",
        "input_tokens",
        "max_tokens",
        "max_completion_tokens",
    ):
        message = error.get("message")
        match = _CONTEXT_LIMIT.search(message) if isinstance(message, str) else None
        return _context_error(
            next((part for part in match.groups() if part), None) if match else None
        )
    return PlatformError(ErrorCode.INVALID_REQUEST)


async def _read_error_body(response: httpx.Response) -> object:
    # Only called after HTTP 400. No successful JSON/SSE response enters this path.
    body = bytearray()
    try:
        async for chunk in response.aiter_bytes():
            if len(body) + len(chunk) > _MAX_ERROR_BYTES:
                return None
            body.extend(chunk)
        return json.loads(body)
    except (httpx.HTTPError, ValueError, RecursionError):
        return None


def _map_httpx_failure(error: httpx.HTTPError) -> InferenceFailure:
    if isinstance(error, httpx.PoolTimeout):
        return _failure(ErrorCode.GATEWAY_OVERLOADED)
    if isinstance(error, httpx.ConnectTimeout):
        return _failure(ErrorCode.BACKEND_UNAVAILABLE, counts_toward_breaker=True)
    if isinstance(error, httpx.TimeoutException):
        return _failure(ErrorCode.INFERENCE_TIMEOUT)
    if isinstance(error, (httpx.NetworkError, httpx.RemoteProtocolError)):
        return _failure(ErrorCode.BACKEND_UNAVAILABLE, counts_toward_breaker=True)
    return _failure(ErrorCode.BACKEND_UNAVAILABLE)


def _append_bounded(buffer: bytearray, chunk: bytes, maximum_bytes: int) -> None:
    if len(buffer) + len(chunk) > maximum_bytes:
        raise _failure(ErrorCode.BACKEND_UNAVAILABLE)
    buffer.extend(chunk)


def _event_boundary(buffer: bytearray) -> tuple[int, int] | None:
    candidates = ((buffer.find(b"\n\n"), 2), (buffer.find(b"\r\n\r\n"), 4))
    present = [candidate for candidate in candidates if candidate[0] >= 0]
    return min(present) if present else None


def _nonnegative_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _usage(payload: dict[str, object]) -> UsageInfo | None:
    value = payload.get("usage")
    if not isinstance(value, dict):
        return None
    return UsageInfo(
        input_tokens=_nonnegative_int(value.get("prompt_tokens")),
        output_tokens=_nonnegative_int(value.get("completion_tokens")),
    )


def _stop_reason(payload: dict[str, object]) -> StopReason | None:
    choices = payload.get("choices")
    if not isinstance(choices, list):
        return None
    for value in choices:
        if not isinstance(value, dict) or value.get("finish_reason") is None:
            continue
        reason = value.get("finish_reason")
        if not isinstance(reason, str):
            return StopReason.OTHER
        try:
            return StopReason(reason)
        except ValueError:
            return StopReason.OTHER
    return None


def _tool_payload(tool: ToolDefinition) -> dict[str, object]:
    function: dict[str, object] = {"name": tool.name}
    if tool.description is not None:
        function["description"] = tool.description
    if tool.input_schema is not None:
        function["parameters"] = thaw_json(tool.input_schema)
    if tool.strict is not None:
        function["strict"] = tool.strict
    return {"type": "function", "function": function}


def _message_payload(message: CanonicalMessage) -> dict[str, object]:
    payload: dict[str, object] = {"role": message.role}
    if len(message.content) == 1 and isinstance(message.content[0], TextContent):
        payload["content"] = message.content[0].text
    elif message.content:
        blocks: list[dict[str, object]] = []
        for block in message.content:
            if isinstance(block, TextContent):
                blocks.append({"type": "text", "text": block.text})
            elif isinstance(block, ImageContent):
                blocks.append({"type": "image_url", "image_url": {"url": block.url}})
            else:
                raise _failure(ErrorCode.INVALID_REQUEST)
        payload["content"] = blocks
    else:
        payload["content"] = None
    if message.tool_calls:
        payload["tool_calls"] = [
            {
                "id": call.call_id,
                "type": "function",
                "function": {"name": call.name, "arguments": call.arguments},
            }
            for call in message.tool_calls
        ]
    if message.tool_call_id is not None and message.role == "tool":
        payload["tool_call_id"] = message.tool_call_id
    return payload


def _response_format_payload(format: ResponseFormat) -> dict[str, object]:
    payload: dict[str, object] = {"type": format.kind.value}
    if format.kind is ResponseFormatKind.JSON_SCHEMA:
        schema: dict[str, object] = {"name": format.name, "schema": thaw_json(format.schema)}
        if format.strict is not None:
            schema["strict"] = format.strict
        if format.description is not None:
            schema["description"] = format.description
        payload["json_schema"] = schema
    return payload


def _request_payload(request: CanonicalRequest, runtime_model_name: str) -> dict[str, object]:
    payload: dict[str, object] = {
        "model": runtime_model_name,
        "messages": [_message_payload(message) for message in request.messages],
        "stream": True,
    }
    sampling = request.sampling
    if sampling.top_k is not None:
        payload["top_k"] = sampling.top_k
    if sampling.temperature is not None:
        payload["temperature"] = sampling.temperature
    if sampling.nucleus_probability is not None:
        payload["top_p"] = sampling.nucleus_probability
    if sampling.stop_sequences:
        payload["stop"] = list(sampling.stop_sequences)
    if sampling.random_seed is not None:
        payload["seed"] = sampling.random_seed
    if sampling.maximum_output_tokens is not None:
        payload["max_tokens"] = sampling.maximum_output_tokens
    if request.reasoning is not None:
        payload["reasoning_effort"] = request.reasoning.effort.value
    if sampling.presence_penalty is not None:
        payload["presence_penalty"] = sampling.presence_penalty
    if sampling.frequency_penalty is not None:
        payload["frequency_penalty"] = sampling.frequency_penalty
    if request.tool_choice is not None:
        choice = request.tool_choice
        payload["tool_choice"] = (
            {"type": "function", "function": {"name": choice.function_name}}
            if choice.mode is ToolChoiceMode.FUNCTION
            else choice.mode.value
        )
    if request.parallel_tool_calls is not None:
        payload["parallel_tool_calls"] = request.parallel_tool_calls
    if request.response_format is not None:
        payload["response_format"] = _response_format_payload(request.response_format)
    if request.tools:
        payload["tools"] = [_tool_payload(tool) for tool in request.tools]
    return payload


def _typed_payload_events(
    payload: dict[str, object], *, streaming: bool, tool_indices: set[int]
) -> list[
    ContentDelta | ThinkingDelta | ToolCallStarted | ToolArgumentsDelta | Completed | UsageReported
]:
    """Extract each semantic value once; wire extensions never drive metering."""
    events: list[
        ContentDelta
        | ThinkingDelta
        | ToolCallStarted
        | ToolArgumentsDelta
        | Completed
        | UsageReported
    ] = []
    choices = payload.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            message = choice.get("delta" if streaming else "message")
            if isinstance(message, dict):
                reasoning = message.get("reasoning_content", message.get("reasoning"))
                if isinstance(reasoning, str) and reasoning:
                    events.append(ThinkingDelta(reasoning))
                text = message.get("content")
                if isinstance(text, str) and text:
                    events.append(ContentDelta(text))
                calls = message.get("tool_calls")
                if isinstance(calls, list):
                    for ordinal, call in enumerate(calls):
                        if not isinstance(call, dict):
                            raise _failure(ErrorCode.BACKEND_UNAVAILABLE)
                        index = call.get("index", ordinal)
                        function = call.get("function")
                        if type(index) is not int or index < 0 or not isinstance(function, dict):
                            raise _failure(ErrorCode.BACKEND_UNAVAILABLE)
                        name = function.get("name")
                        call_id = call.get("id")
                        if isinstance(name, str) and name and index not in tool_indices:
                            tool_indices.add(index)
                            events.append(
                                ToolCallStarted(
                                    index, call_id if isinstance(call_id, str) else None, name
                                )
                            )
                        arguments = function.get("arguments")
                        if isinstance(arguments, str) and arguments:
                            events.append(ToolArgumentsDelta(index, arguments))
            reason = _stop_reason({"choices": [choice]})
            if reason is not None:
                sequence = choice.get("stop_reason")
                events.append(
                    Completed(reason, stop_sequence=sequence if isinstance(sequence, str) else None)
                )
    usage = _usage(payload)
    if usage is not None:
        events.append(UsageReported(usage))
    return events


def _classify_sse_event(
    event: bytes,
    *,
    suppress_injected_usage: bool,
    public_model_name: str,
    tool_indices: set[int],
) -> tuple[_SseEvent, ...]:
    lines: list[RawSseLine | DataSseLine] = []
    first_payload: dict[str, object] | None = None
    done = False
    for line in event.replace(b"\r\n", b"\n").split(b"\n"):
        if not line.startswith(b"data:"):
            lines.append(RawSseLine(line))
            continue
        data = line[5:].strip()
        if data == b"[DONE]":
            done = True
            lines.append(DataSseLine(done=True))
            continue
        try:
            value: object = json.loads(data)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise _failure(ErrorCode.BACKEND_UNAVAILABLE) from None
        if not isinstance(value, dict):
            raise _failure(ErrorCode.BACKEND_UNAVAILABLE)
        typed = freeze_json(value)
        if not isinstance(typed, JsonObject):
            raise _failure(ErrorCode.BACKEND_UNAVAILABLE)
        lines.append(DataSseLine(payload=typed))
        if first_payload is None:
            first_payload = value
    extension = OpenAISseExtension(tuple(lines), public_model_name)
    if done and first_payload is None:
        return (StreamEnded(extension),)
    if first_payload is None:
        return (StreamStarted(extension),)
    events = _typed_payload_events(first_payload, streaming=True, tool_indices=tool_indices)
    private_usage = suppress_injected_usage and first_payload.get("choices") == []
    if not events:
        return (StreamStarted(extension),)
    if not private_usage:
        events[0] = replace(events[0], extension=extension)
    return tuple(events)


async def _sse_events(
    chunks: AsyncIterator[bytes],
    *,
    maximum_event_bytes: int,
    suppress_injected_usage: bool,
    public_model_name: str,
) -> AsyncIterator[_SseEvent]:
    buffer = bytearray()
    tool_indices: set[int] = set()
    async for chunk in chunks:
        _append_bounded(buffer, chunk, maximum_event_bytes)
        while (boundary := _event_boundary(buffer)) is not None:
            index, separator_length = boundary
            event = bytes(buffer[:index])
            del buffer[: index + separator_length]
            if event:
                canonical = _classify_sse_event(
                    event,
                    suppress_injected_usage=suppress_injected_usage,
                    public_model_name=public_model_name,
                    tool_indices=tool_indices,
                )
                if len(tool_indices) * 64 > maximum_event_bytes:
                    raise _failure(ErrorCode.BACKEND_UNAVAILABLE)
                for item in canonical:
                    yield item
    if buffer:
        canonical = _classify_sse_event(
            bytes(buffer),
            suppress_injected_usage=suppress_injected_usage,
            public_model_name=public_model_name,
            tool_indices=tool_indices,
        )
        if len(tool_indices) * 64 > maximum_event_bytes:
            raise _failure(ErrorCode.BACKEND_UNAVAILABLE)
        for item in canonical:
            yield item


class VllmRuntimeAdapter:
    """Translate vLLM HTTP/JSON/SSE into request-local canonical events."""

    __slots__ = (
        "_client",
        "_clients",
        "_clients_lock",
        "_managed_endpoints",
        "_max_response_bytes",
        "_max_sse_event_bytes",
        "_connect_timeout_seconds",
        "_pool_max_connections",
        "_pool_timeout_seconds",
    )

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        *,
        max_response_bytes: int,
        max_sse_event_bytes: int,
        pool_max_connections: int = 64,
        pool_timeout_seconds: float = 0.1,
        connect_timeout_seconds: float | None = None,
    ) -> None:
        if (
            pool_max_connections < 1
            or pool_timeout_seconds <= 0
            or (connect_timeout_seconds is not None and connect_timeout_seconds <= 0)
        ):
            raise ValueError("Runtime pool limits must be positive")
        self._client = client
        self._clients: dict[str, _EndpointClient] = {}
        self._clients_lock = asyncio.Lock()
        self._managed_endpoints: frozenset[str] | None = None
        self._max_response_bytes = max_response_bytes
        self._max_sse_event_bytes = max_sse_event_bytes
        self._pool_max_connections = pool_max_connections
        self._pool_timeout_seconds = pool_timeout_seconds
        self._connect_timeout_seconds = connect_timeout_seconds

    def _new_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=httpx.Timeout(
                None,
                connect=self._connect_timeout_seconds,
                pool=self._pool_timeout_seconds,
            ),
            limits=httpx.Limits(
                max_connections=self._pool_max_connections,
                max_keepalive_connections=min(32, self._pool_max_connections),
                keepalive_expiry=None,
            ),
            follow_redirects=False,
            trust_env=False,
        )

    async def sync_routes(self, routes: Iterable[GatewayRoute]) -> None:
        """Pre-create clients for usable routes and retire clients no longer routed."""

        if self._client is not None:
            return
        desired = frozenset(
            route.internal_endpoint
            for route in routes
            if route.enabled
            and route.instance_status == ModelInstanceStatus.RUNNING.value
            and route.internal_endpoint is not None
        )
        clients_to_close: list[httpx.AsyncClient] = []
        async with self._clients_lock:
            self._managed_endpoints = desired
            for endpoint in desired:
                entry = self._clients.get(endpoint)
                if entry is None:
                    self._clients[endpoint] = _EndpointClient(self._new_client())
                else:
                    entry.retiring = False
            for endpoint, entry in tuple(self._clients.items()):
                if endpoint in desired:
                    continue
                entry.retiring = True
                if entry.active == 0:
                    clients_to_close.append(entry.client)
                    del self._clients[endpoint]
        for client in clients_to_close:
            await client.aclose()

    async def _acquire_client(self, endpoint: str) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client
        async with self._clients_lock:
            entry = self._clients.get(endpoint)
            if entry is None:
                entry = _EndpointClient(self._new_client())
                entry.retiring = (
                    self._managed_endpoints is not None and endpoint not in self._managed_endpoints
                )
                self._clients[endpoint] = entry
            entry.active += 1
            return entry.client

    async def _release_client(self, endpoint: str) -> None:
        if self._client is not None:
            return
        client: httpx.AsyncClient | None = None
        async with self._clients_lock:
            entry = self._clients.get(endpoint)
            if entry is None or entry.active < 1:
                raise RuntimeError("Runtime client released without acquisition")
            entry.active -= 1
            if entry.active == 0 and entry.retiring:
                client = entry.client
                del self._clients[endpoint]
        if client is not None:
            await client.aclose()

    async def aclose(self) -> None:
        """Close adapter-owned clients; an injected client remains caller-owned."""

        if self._client is not None:
            return
        async with self._clients_lock:
            clients = tuple(entry.client for entry in self._clients.values())
            self._clients.clear()
        for client in clients:
            await client.aclose()

    async def health(self, endpoint: str) -> bool:
        """Probe the Runtime without exposing its response body or endpoint."""

        response: httpx.Response | None = None
        client = await self._acquire_client(endpoint)
        try:
            response = await client.send(
                client.build_request("GET", _upstream_url(endpoint, _HEALTH_PATH)),
                stream=True,
            )
            if not 200 <= response.status_code < 300:
                raise _failure(
                    ErrorCode.BACKEND_UNAVAILABLE,
                    counts_toward_breaker=500 <= response.status_code < 600,
                )
            return True
        except InferenceFailure:
            raise
        except httpx.HTTPError as error:
            raise _map_httpx_failure(error) from None
        finally:
            try:
                if response is not None:
                    await response.aclose()
            finally:
                await self._release_client(endpoint)

    _context_error = staticmethod(_vllm_context_error)

    async def stream(
        self,
        endpoint: str,
        request_id: UUID,
        canonical_request: CanonicalRequest,
    ) -> AsyncGenerator[CanonicalEvent, None]:
        runtime_model_name = canonical_request.service_model.runtime_model_name
        if runtime_model_name is None:
            raise _failure(ErrorCode.BACKEND_UNAVAILABLE)
        payload = _request_payload(canonical_request, runtime_model_name)
        payload["stream_options"] = {"include_usage": True}
        suppress_injected_usage = (
            canonical_request.stream and not canonical_request.include_stream_usage
        )
        response: httpx.Response | None = None
        endpoint_url = _upstream_url(endpoint)
        timings = current_stage_timings()
        pool_started_at = None if timings is None else timings.now()
        client = await self._acquire_client(endpoint)
        if timings is not None and pool_started_at is not None:
            timings.finish_runtime_pool_wait(pool_started_at)
        try:
            connect_started_at = None if timings is None else timings.now()
            try:
                response = await client.send(
                    client.build_request(
                        "POST",
                        endpoint_url,
                        json=payload,
                        headers={"x-request-id": str(request_id)},
                    ),
                    stream=True,
                )
            finally:
                if timings is not None and connect_started_at is not None:
                    timings.finish_runtime_connect(connect_started_at)
            if response.status_code == 400:
                raise InferenceFailure(self._context_error(await _read_error_body(response)))
            _raise_for_status(response.status_code)
            chunks = response.aiter_bytes()
            first_event_started_at = None if timings is None else timings.now()
            try:
                first_chunk = await anext(chunks)
            except StopAsyncIteration:
                raise _failure(ErrorCode.BACKEND_UNAVAILABLE) from None
            finally:
                if timings is not None and first_event_started_at is not None:
                    timings.finish_upstream_first_event(first_event_started_at)

            async def with_first() -> AsyncIterator[bytes]:
                total = len(first_chunk)
                if not canonical_request.stream and total > self._max_response_bytes:
                    raise _failure(ErrorCode.BACKEND_UNAVAILABLE)
                yield first_chunk
                async for chunk in chunks:
                    total += len(chunk)
                    if not canonical_request.stream and total > self._max_response_bytes:
                        raise _failure(ErrorCode.BACKEND_UNAVAILABLE)
                    yield chunk

            assembled = None if canonical_request.stream else OpenAIResponseAssembler()
            async for event in _sse_events(
                with_first(),
                maximum_event_bytes=self._max_sse_event_bytes,
                suppress_injected_usage=suppress_injected_usage,
                public_model_name=canonical_request.service_model.public_model_name,
            ):
                if assembled is not None:
                    assembled.observe(event.extension)
                    # Only the completed response is encoded downstream; semantic
                    # deltas still reach metering immediately, exactly once.
                    event = replace(event, extension=None)
                yield event
            if assembled is not None:
                yield ResponseCompleted(
                    OpenAIResponseExtension(
                        assembled.finish(),
                        canonical_request.service_model.public_model_name,
                    )
                )
        except InferenceFailure:
            raise
        except httpx.HTTPError as error:
            raise _map_httpx_failure(error) from None
        finally:
            try:
                if response is not None:
                    await response.aclose()
            finally:
                await self._release_client(endpoint)


@dataclass(slots=True)
class _EndpointClient:
    client: httpx.AsyncClient
    active: int = 0
    retiring: bool = False


__all__ = ["VllmRuntimeAdapter"]
