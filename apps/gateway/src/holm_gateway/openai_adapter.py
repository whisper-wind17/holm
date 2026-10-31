"""OpenAI-compatible HTTP Protocol Adapter."""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Never, cast
from uuid import UUID

from fastapi import FastAPI
from holm_contracts import ErrorBody, ErrorEnvelope
from holm_domain import ErrorCode
from holm_protocols import (
    CanonicalEvent,
    CanonicalMessage,
    CanonicalRequest,
    ImageContent,
    InferenceCore,
    InferenceFailure,
    MappedError,
    PlatformError,
    ProtocolRoute,
    ReasoningControl,
    ReasoningEffort,
    ResponseCompleted,
    ResponseFormat,
    ResponseFormatKind,
    SamplingParameters,
    ServiceModelReference,
    TextContent,
    ToolCall,
    ToolChoice,
    ToolChoiceMode,
    ToolDefinition,
)
from holm_runtimes import RuntimeRequestCapabilities
from starlette.exceptions import HTTPException as StarletteHttpException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from holm_gateway.errors import _ERRORS, describe_error
from holm_gateway.openai_types import (
    DataSseLine,
    JsonObject,
    OpenAIRequestExtension,
    OpenAIResponseExtension,
    OpenAISseExtension,
    RawSseLine,
    freeze_json,
    thaw_json,
)
from holm_gateway.request_context import request_id, request_lifecycle

if TYPE_CHECKING:
    from holm_gateway.anthropic_adapter import AnthropicProtocolAdapter

_CHAT_PATH: Final[str] = "/v1/chat/completions"
_MODELS_PATH: Final[str] = "/v1/models"


def _fail(code: ErrorCode) -> Never:
    raise InferenceFailure(PlatformError(code))


def _dict(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        return None
    return cast(dict[str, object], value)


def _messages(value: object) -> tuple[CanonicalMessage, ...]:
    if not isinstance(value, list):
        _fail(ErrorCode.INVALID_REQUEST)
    messages: list[CanonicalMessage] = []
    for raw_message in value:
        message = _dict(raw_message)
        if message is None or not isinstance(message.get("role"), str):
            _fail(ErrorCode.INVALID_REQUEST)
        blocks: list[TextContent | ImageContent] = []
        content = message.get("content")
        if isinstance(content, str):
            blocks.append(TextContent(content))
        elif isinstance(content, list):
            for raw_block in content:
                block = _dict(raw_block)
                if block is None:
                    _fail(ErrorCode.INVALID_REQUEST)
                if block.get("type") == "text" and isinstance(block.get("text"), str):
                    blocks.append(TextContent(cast(str, block["text"])))
                    continue
                image_url = _dict(block.get("image_url"))
                if (
                    block.get("type") == "image_url"
                    and image_url is not None
                    and isinstance(image_url.get("url"), str)
                ):
                    blocks.append(ImageContent(cast(str, image_url["url"])))
                    continue
                _fail(ErrorCode.INVALID_REQUEST)
        elif content is not None:
            _fail(ErrorCode.INVALID_REQUEST)
        tool_calls: list[ToolCall] = []
        raw_calls = message.get("tool_calls")
        if raw_calls is not None and not isinstance(raw_calls, list):
            _fail(ErrorCode.INVALID_REQUEST)
        if isinstance(raw_calls, list):
            for raw_call in raw_calls:
                call = _dict(raw_call)
                function = None if call is None else _dict(call.get("function"))
                if (
                    call is not None
                    and function is not None
                    and isinstance(call.get("id"), str)
                    and isinstance(function.get("name"), str)
                    and isinstance(function.get("arguments"), str)
                ):
                    tool_calls.append(
                        ToolCall(
                            cast(str, call["id"]),
                            cast(str, function["name"]),
                            cast(str, function["arguments"]),
                        )
                    )
                    continue
                _fail(ErrorCode.INVALID_REQUEST)
        tool_call_id: str | None = None
        if "tool_call_id" in message:
            raw_tool_call_id = message["tool_call_id"]
            if not isinstance(raw_tool_call_id, str):
                _fail(ErrorCode.INVALID_REQUEST)
            tool_call_id = raw_tool_call_id
        messages.append(
            CanonicalMessage(
                role=cast(str, message["role"]),
                content=tuple(blocks),
                tool_calls=tuple(tool_calls),
                tool_call_id=tool_call_id if message["role"] == "tool" else None,
            )
        )
    return tuple(messages)


def _schema_object(value: object) -> JsonObject:
    if _dict(value) is None:
        _fail(ErrorCode.INVALID_REQUEST)
    frozen = freeze_json(value)
    assert isinstance(frozen, JsonObject)
    return frozen


def _optional_bool(payload: Mapping[str, object], name: str) -> bool | None:
    if name not in payload:
        return None
    value = payload[name]
    if not isinstance(value, bool):
        _fail(ErrorCode.INVALID_REQUEST)
    return value


def _tool_choice(value: object) -> ToolChoice:
    if isinstance(value, str) and value in {"none", "auto", "required"}:
        return ToolChoice(ToolChoiceMode(value))
    choice = _dict(value)
    function = None if choice is None else _dict(choice.get("function"))
    if (
        choice is None
        or set(choice) != {"type", "function"}
        or choice.get("type") != "function"
        or function is None
        or set(function) != {"name"}
        or not isinstance(function.get("name"), str)
    ):
        _fail(ErrorCode.INVALID_REQUEST)
    return ToolChoice(ToolChoiceMode.FUNCTION, cast(str, function["name"]))


def _response_format(value: object) -> ResponseFormat:
    value = _dict(value)
    if value is None:
        _fail(ErrorCode.INVALID_REQUEST)
    kind = value.get("type")
    if isinstance(kind, str) and kind in {"text", "json_object"} and set(value) == {"type"}:
        return ResponseFormat(ResponseFormatKind(kind))
    schema = _dict(value.get("json_schema"))
    if (
        kind != "json_schema"
        or set(value) != {"type", "json_schema"}
        or schema is None
        or set(schema) - {"name", "schema", "strict", "description"}
        or not isinstance(schema.get("name"), str)
        or ("description" in schema and not isinstance(schema["description"], str))
    ):
        _fail(ErrorCode.INVALID_REQUEST)
    return ResponseFormat(
        ResponseFormatKind.JSON_SCHEMA,
        name=cast(str, schema["name"]),
        schema=_schema_object(schema.get("schema")),
        strict=_optional_bool(schema, "strict"),
        description=cast(str | None, schema.get("description")),
    )


def _tools(value: object) -> tuple[ToolDefinition, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        _fail(ErrorCode.INVALID_REQUEST)
    tools: list[ToolDefinition] = []
    for raw_tool in value:
        tool = _dict(raw_tool)
        function = None if tool is None else _dict(tool.get("function"))
        if function is None or not isinstance(function.get("name"), str):
            _fail(ErrorCode.INVALID_REQUEST)
        tools.append(
            ToolDefinition(
                name=cast(str, function["name"]),
                description=(
                    cast(str, function["description"])
                    if isinstance(function.get("description"), str)
                    else None
                ),
                input_schema=(
                    _schema_object(function["parameters"]) if "parameters" in function else None
                ),
                strict=_optional_bool(function, "strict"),
            )
        )
    return tuple(tools)


def _render_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _rewrite_model(payload: JsonObject, public_model_name: str) -> bytes:
    value = thaw_json(payload)
    if not isinstance(value, dict):
        _fail(ErrorCode.BACKEND_UNAVAILABLE)
    if "model" in value:
        value["model"] = public_model_name
    choices = value.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            for field in ("delta", "message"):
                message = choice.get(field)
                if isinstance(message, dict) and "reasoning" in message:
                    reasoning = message.pop("reasoning")
                    message.setdefault("reasoning_content", reasoning)
    return _render_json(value)


@dataclass(frozen=True, slots=True)
class _DecodedRequest:
    payload: dict[str, object]


def _json_depth(value: object) -> int:
    maximum = 1
    pending: list[tuple[object, int]] = [(value, 1)]
    while pending:
        current, depth = pending.pop()
        maximum = max(maximum, depth)
        if isinstance(current, dict):
            pending.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            pending.extend((item, depth + 1) for item in current)
    return maximum


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        result = float(value)
    except OverflowError:
        return None
    return result if math.isfinite(result) else None


def _optional_number(
    payload: Mapping[str, object],
    name: str,
    *,
    minimum: float,
    maximum: float,
) -> float | None:
    if name not in payload:
        return None
    result = _number(payload[name])
    if result is None or not minimum <= result <= maximum:
        _fail(ErrorCode.INVALID_REQUEST)
    return result


def _optional_integer(
    payload: Mapping[str, object], name: str, *, minimum: int | None = None
) -> int | None:
    if name not in payload:
        return None
    result = payload[name]
    if isinstance(result, bool) or not isinstance(result, int):
        _fail(ErrorCode.INVALID_REQUEST)
    if minimum is not None and result < minimum:
        _fail(ErrorCode.INVALID_REQUEST)
    return result


def _stop_sequences(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return tuple(value)
    _fail(ErrorCode.INVALID_REQUEST)


def _validate_content_type(request: Request) -> None:
    raw = request.headers.get("content-type")
    if raw is None or raw.split(";", maxsplit=1)[0].strip().casefold() != "application/json":
        _fail(ErrorCode.INVALID_REQUEST)


class OpenAIProtocolAdapter:
    """Own OpenAI routes, credentials, parsing, encoders, and error mapping."""

    __slots__ = ("_maximum_json_depth", "_runtime_capabilities")

    def __init__(
        self,
        runtime_capabilities: Mapping[str, RuntimeRequestCapabilities],
        *,
        maximum_json_depth: int,
    ) -> None:
        if not runtime_capabilities:
            raise ValueError("Runtime request capabilities are required")
        if maximum_json_depth < 1:
            raise ValueError("maximum JSON depth must be positive")
        self._runtime_capabilities = dict(runtime_capabilities)
        self._maximum_json_depth = maximum_json_depth

    def routes(self) -> tuple[ProtocolRoute, ...]:
        return (
            ProtocolRoute("GET", _MODELS_PATH, "list_models"),
            ProtocolRoute("POST", _CHAT_PATH, "chat_completions"),
        )

    def extract_credential(self, headers: Mapping[str, str]) -> str | None:
        authorization = headers.get("authorization")
        parts = () if authorization is None else tuple(authorization.split())
        return parts[1] if len(parts) == 2 and parts[0].casefold() == "bearer" else None

    def _decode_request(self, body: bytes) -> _DecodedRequest:
        try:
            value: object = json.loads(
                body,
                parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
            )
        except (json.JSONDecodeError, RecursionError, UnicodeDecodeError, ValueError):
            _fail(ErrorCode.INVALID_REQUEST)
        payload = _dict(value)
        if payload is None or _json_depth(payload) > self._maximum_json_depth:
            _fail(ErrorCode.INVALID_REQUEST)
        return _DecodedRequest(payload)

    @staticmethod
    def _model_name(document: _DecodedRequest) -> str:
        model = document.payload.get("model")
        if not isinstance(model, str) or not model:
            _fail(ErrorCode.INVALID_REQUEST)
        return model

    def parse_request(self, body: bytes, *, runtime_name: str) -> CanonicalRequest:
        return self._parse_document(self._decode_request(body), runtime_name=runtime_name)

    def _parse_document(self, document: _DecodedRequest, *, runtime_name: str) -> CanonicalRequest:
        capabilities = self._runtime_capabilities.get(runtime_name)
        if capabilities is None:
            _fail(ErrorCode.BACKEND_UNAVAILABLE)
        payload = document.payload
        unsupported = frozenset(payload) - capabilities.openai_chat_completions
        if unsupported:
            raise InferenceFailure(
                PlatformError(
                    ErrorCode.INVALID_REQUEST,
                    unsupported_fields=tuple(name[:64] for name in sorted(unsupported)[:8]),
                )
            )
        model = payload.get("model")
        stream = payload.get("stream", False)
        if not isinstance(model, str) or not model or not isinstance(stream, bool):
            _fail(ErrorCode.INVALID_REQUEST)
        if "n" in payload and (
            isinstance(payload["n"], bool) or not isinstance(payload["n"], int) or payload["n"] != 1
        ):
            _fail(ErrorCode.INVALID_REQUEST)
        stream_options = payload.get("stream_options")
        if stream_options is not None and not isinstance(stream_options, dict):
            _fail(ErrorCode.INVALID_REQUEST)
        options = _dict(stream_options)
        if options is not None and (
            frozenset(options) - {"include_usage"}
            or ("include_usage" in options and not isinstance(options["include_usage"], bool))
            or not stream
        ):
            _fail(ErrorCode.INVALID_REQUEST)
        requested_usage = options is not None and options.get("include_usage") is True
        reasoning = None
        effort = payload.get("reasoning_effort")
        if effort is not None:
            if not isinstance(effort, str):
                _fail(ErrorCode.INVALID_REQUEST)
            try:
                reasoning = ReasoningControl(ReasoningEffort(effort))
            except ValueError:
                _fail(ErrorCode.INVALID_REQUEST)
        if "user" in payload and not isinstance(payload["user"], str):
            _fail(ErrorCode.INVALID_REQUEST)
        max_tokens = _optional_integer(payload, "max_tokens", minimum=0)
        max_completion_tokens = _optional_integer(payload, "max_completion_tokens", minimum=0)
        if (
            max_tokens is not None
            and max_completion_tokens is not None
            and max_tokens != max_completion_tokens
        ):
            _fail(ErrorCode.INVALID_REQUEST)
        sampling = SamplingParameters(
            temperature=_optional_number(payload, "temperature", minimum=0, maximum=2),
            nucleus_probability=_optional_number(payload, "top_p", minimum=0, maximum=1),
            stop_sequences=(_stop_sequences(payload["stop"]) if "stop" in payload else ()),
            random_seed=_optional_integer(payload, "seed"),
            maximum_output_tokens=(
                max_completion_tokens if max_completion_tokens is not None else max_tokens
            ),
            presence_penalty=_optional_number(payload, "presence_penalty", minimum=-2, maximum=2),
            frequency_penalty=_optional_number(payload, "frequency_penalty", minimum=-2, maximum=2),
        )
        return CanonicalRequest(
            service_model=ServiceModelReference(model),
            stream=stream,
            sampling=sampling,
            messages=_messages(payload.get("messages")),
            tools=_tools(payload.get("tools")),
            extension=OpenAIRequestExtension(requested_usage),
            include_stream_usage=requested_usage,
            reasoning=reasoning,
            tool_choice=_tool_choice(payload["tool_choice"]) if "tool_choice" in payload else None,
            parallel_tool_calls=_optional_bool(payload, "parallel_tool_calls"),
            response_format=(
                _response_format(payload["response_format"])
                if "response_format" in payload
                else None
            ),
        )

    def encode_stream_event(self, event: CanonicalEvent) -> bytes | None:
        extension = getattr(event, "extension", None)
        if extension is None:
            return None
        if not isinstance(extension, OpenAISseExtension):
            _fail(ErrorCode.BACKEND_UNAVAILABLE)
        output: list[bytes] = []
        for line in extension.lines:
            if isinstance(line, RawSseLine):
                output.append(line.value)
            elif line.done:
                output.append(b"data: [DONE]")
            elif isinstance(line, DataSseLine) and isinstance(line.payload, JsonObject):
                output.append(b"data: " + _rewrite_model(line.payload, extension.public_model_name))
            else:
                _fail(ErrorCode.BACKEND_UNAVAILABLE)
        return b"\n".join(output) + b"\n\n"

    def encode_non_stream(self, events: tuple[CanonicalEvent, ...]) -> bytes:
        for event in events:
            if not isinstance(event, ResponseCompleted):
                continue
            extension = event.extension
            if isinstance(extension, OpenAIResponseExtension):
                return _rewrite_model(extension.payload, extension.public_model_name)
        _fail(ErrorCode.BACKEND_UNAVAILABLE)

    def map_error(self, error: PlatformError, request_id_value: UUID) -> MappedError:
        status_code, message, limit_details = describe_error(error)
        envelope = ErrorEnvelope(
            error=ErrorBody(
                code=error.code,
                message=message,
                details=(
                    {
                        **(
                            {
                                "supported_reasoning_efforts": [
                                    effort.value for effort in error.supported_reasoning_efforts
                                ]
                            }
                            if error.supported_reasoning_efforts
                            else {}
                        ),
                        **({"reason": error.reason} if error.reason is not None else {}),
                        **(
                            {"max_context_tokens": error.max_context_tokens}
                            if error.reason is not None and error.max_context_tokens is not None
                            else {}
                        ),
                        **(
                            {"unsupported_fields": list(error.unsupported_fields)}
                            if error.unsupported_fields
                            else {}
                        ),
                    }
                    if error.code is ErrorCode.INVALID_REQUEST
                    else limit_details
                ),
                request_id=request_id_value,
            )
        )
        return MappedError(
            status_code=status_code,
            body=_render_json(envelope.model_dump(mode="json")),
            media_type="application/json",
            headers=(("Cache-Control", "no-store"),)
            + (
                (("Retry-After", str(max(1, error.retry_after or 1))),)
                if error.code is ErrorCode.RATE_LIMIT_EXCEEDED
                else ()
            ),
        )

    def failure_response(self, error: PlatformError, request_id_value: UUID) -> Response:
        mapped = self.map_error(error, request_id_value)
        return Response(
            content=mapped.body,
            status_code=mapped.status_code,
            media_type=mapped.media_type,
            headers=dict(mapped.headers),
        )

    @staticmethod
    def failure_status(error: PlatformError) -> int:
        try:
            return _ERRORS[error.code][0]
        except KeyError:
            raise ValueError("unsupported Gateway error code") from None

    def install(
        self,
        application: FastAPI,
        core: InferenceCore,
        *,
        max_request_body_bytes: int,
        current_limits: Callable[[UUID, UUID], tuple[int | None, int | None]] | None = None,
        anthropic: AnthropicProtocolAdapter | None = None,
    ) -> None:
        def is_anthropic(request: Request) -> bool:
            return (
                request.url.path == "/v1/messages"
                or request.url.path.startswith("/v1/messages/")
                or (request.url.path == _MODELS_PATH and "anthropic-version" in request.headers)
            )

        @application.exception_handler(InferenceFailure)
        async def handle_inference_failure(request: Request, failure: InferenceFailure) -> Response:
            context = request.scope.get("holm_gateway_request_record")
            if context is not None:
                await request_lifecycle(request).mark_error(failure.error)
            mapper = anthropic if anthropic is not None and is_anthropic(request) else self
            return mapper.failure_response(failure.error, request_id(request))

        @application.exception_handler(StarletteHttpException)
        async def handle_unknown_http_route(
            request: Request, error: StarletteHttpException
        ) -> Response:
            del error
            if anthropic is not None and is_anthropic(request):
                return anthropic.failure_response(
                    PlatformError(ErrorCode.MODEL_NOT_FOUND), request_id(request)
                )
            return self.failure_response(
                PlatformError(ErrorCode.INVALID_REQUEST), request_id(request)
            )

        @application.get(_MODELS_PATH)
        async def list_models(request: Request) -> JSONResponse:
            if anthropic is not None and is_anthropic(request):
                models = await core.list_models(anthropic.extract_credential(request.headers))
                return anthropic.models_response(models)
            models = await core.list_models(self.extract_credential(request.headers))
            return JSONResponse(
                content={
                    "object": "list",
                    "data": [
                        {
                            "id": model.public_model_name,
                            "object": "model",
                            "created": 0,
                            "owned_by": "holm",
                            "holm_status": ("available" if model.available else "unavailable"),
                        }
                        for model in models
                    ],
                }
            )

        @application.post(_CHAT_PATH)
        async def chat_completions(request: Request) -> Response:
            lifecycle = request_lifecycle(request)
            credential = self.extract_credential(request.headers)
            parse_started_at = lifecycle.stage_timings.now()
            try:
                _validate_content_type(request)
                body = await _read_bounded_body(request, max_request_body_bytes)
                document = self._decode_request(body)
                model_name = self._model_name(document)
                raw_stream = document.payload.get("stream", False)
                if not isinstance(raw_stream, bool):
                    _fail(ErrorCode.INVALID_REQUEST)
            except InferenceFailure:
                # Preserve the established auth-before-body error precedence while
                # keeping protocol-invalid requests out of the Request Ledger.
                await core.authenticate(credential, lifecycle)
                raise
            lifecycle.stage_timings.finish_request_parse(parse_started_at)
            lifecycle.set_stream(raw_stream)
            # An Unavailable Ledger rejects here, before the first
            # Ledger write and before any routing, so the Runtime is untouched.
            lifecycle.raise_if_ledger_unavailable()
            lifecycle.begin_received(len(body))
            await lifecycle.raise_if_received_unavailable()
            auth_started_at = lifecycle.stage_timings.now()
            try:
                principal = await core.authenticate(credential, lifecycle)
                route = await core.authorize_route(
                    principal=principal,
                    public_model_name=model_name,
                    lifecycle=lifecycle,
                )
                canonical_request = self._parse_document(document, runtime_name=route.runtime_name)
            finally:
                lifecycle.stage_timings.finish_auth_route(auth_started_at)
            try:
                response = await core.infer_routed(
                    route=route,
                    principal=principal,
                    current_limits=current_limits,
                    disconnected=request.is_disconnected,
                    request_id=request_id(request),
                    request=canonical_request,
                    lifecycle=lifecycle,
                )
            except asyncio.CancelledError:
                if await request.is_disconnected():
                    return Response(status_code=499)
                raise
            if response.stream:
                return StreamingResponse(
                    self._stream_body(response.events),
                    media_type="text/event-stream",
                    headers={"Cache-Control": "no-store"},
                )
            events = tuple([event async for event in response.events])
            return Response(content=self.encode_non_stream(events), media_type="application/json")

    async def _stream_body(self, events: AsyncIterator[CanonicalEvent]) -> AsyncIterator[bytes]:
        async for event in events:
            encoded = self.encode_stream_event(event)
            if encoded is not None:
                yield encoded


async def _read_bounded_body(request: Request, maximum_bytes: int) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > maximum_bytes or int(content_length) < 0:
                _fail(ErrorCode.INVALID_REQUEST)
        except ValueError:
            _fail(ErrorCode.INVALID_REQUEST)
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > maximum_bytes:
            _fail(ErrorCode.INVALID_REQUEST)
        body.extend(chunk)
    return bytes(body)


__all__ = ["OpenAIProtocolAdapter"]
