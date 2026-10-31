"""Request-local Anthropic content block state machine over canonical events."""

from __future__ import annotations

import json
from uuid import UUID

from holm_domain import ErrorCode
from holm_protocols import (
    CanonicalEvent,
    CanonicalRequest,
    Completed,
    ContentDelta,
    Failed,
    InferenceFailure,
    PlatformError,
    StopReason,
    ThinkingDelta,
    ToolArgumentsDelta,
    ToolCallStarted,
    UsageInfo,
    UsageReported,
)

from holm_gateway.anthropic_request import AnthropicRequestExtension
from holm_gateway.errors import describe_error


def render(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()


def sse(kind: str, value: dict[str, object]) -> bytes:
    return b"event: " + kind.encode() + b"\ndata: " + render({"type": kind, **value}) + b"\n\n"


def error_body(error: PlatformError, request_id: UUID) -> dict[str, object]:
    status, message, details = describe_error(error)
    kind = {
        400: "invalid_request_error",
        401: "authentication_error",
        403: "permission_error",
        404: "not_found_error",
        429: "rate_limit_error",
    }.get(status, "overloaded_error" if error.code is ErrorCode.GATEWAY_OVERLOADED else "api_error")
    return {
        "type": "error",
        "error": {"type": kind, "message": message, **({"details": details} if details else {})},
        "request_id": str(request_id),
    }


class AnthropicResponseEncoder:
    """Stream one block at a time; only tool JSON needs bounded validation storage."""

    def __init__(self, request_id: UUID, request: CanonicalRequest, *, maximum_bytes: int) -> None:
        self.request_id = request_id
        self.model = request.service_model.public_model_name
        self.omit = (
            isinstance(request.extension, AnthropicRequestExtension)
            and request.extension.omit_thinking
        )
        self.maximum_bytes = maximum_bytes
        self.stop_sequences = request.sampling.stop_sequences
        self.index = -1
        self.kind: str | None = None
        self.tool_index: int | None = None
        self.seen_tools: set[int] = set()
        self.tool_metadata_bytes = 0
        self.arguments: list[str] = []
        self.argument_bytes = 0
        self.started = False
        self.completed = False
        self.reason: StopReason | None = None
        self.sequence: str | None = None
        self.usage = UsageInfo()

    def message(self) -> dict[str, object]:
        return {
            "id": "msg_" + str(self.request_id),
            "type": "message",
            "role": "assistant",
            "model": self.model,
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        }

    def _close(self) -> bytes:
        if self.kind is None:
            return b""
        result = b""
        if self.kind == "thinking":
            result += sse(
                "content_block_delta",
                {"index": self.index, "delta": {"type": "signature_delta", "signature": ""}},
            )
        if self.kind == "tool_use":
            try:
                value = json.loads(
                    "".join(self.arguments) or "{}",
                    parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
                )
            except (ValueError, RecursionError):
                self._fail()
            if not isinstance(value, dict):
                self._fail()
            self.arguments.clear()
            self.argument_bytes = 0
        result += sse("content_block_stop", {"index": self.index})
        self.kind = None
        self.tool_index = None
        return result

    def _open(self, kind: str, block: dict[str, object]) -> bytes:
        result = self._close()
        self.index += 1
        self.kind = kind
        return result + sse("content_block_start", {"index": self.index, "content_block": block})

    def encode(self, event: CanonicalEvent) -> bytes:
        if isinstance(event, Failed):
            raise InferenceFailure(event.error)
        result = b""
        if not self.started:
            self.started = True
            result = sse("message_start", {"message": self.message()})
        if isinstance(event, (ThinkingDelta, ContentDelta)):
            if isinstance(event, ThinkingDelta) and self.omit:
                return result
            kind = "thinking" if isinstance(event, ThinkingDelta) else "text"
            if self.kind != kind:
                result += self._open(kind, {"type": kind, kind: ""})
            result += sse(
                "content_block_delta",
                {"index": self.index, "delta": {"type": kind + "_delta", kind: event.text}},
            )
        elif isinstance(event, ToolCallStarted):
            if event.index in self.seen_tools or not event.name:
                self._fail()
            self.tool_metadata_bytes += (
                64 + len(event.name.encode()) + len((event.call_id or "").encode())
            )
            if self.tool_metadata_bytes > self.maximum_bytes:
                self._fail()
            self.seen_tools.add(event.index)
            result += self._open(
                "tool_use",
                {
                    "type": "tool_use",
                    "id": event.call_id or (f"toolu_{self.request_id.hex}_{event.index}"),
                    "name": event.name,
                    "input": {},
                },
            )
            self.tool_index = event.index
        elif isinstance(event, ToolArgumentsDelta):
            if self.kind != "tool_use" or self.tool_index != event.index:
                self._fail()
            self.argument_bytes += len(event.text.encode())
            if self.argument_bytes > self.maximum_bytes:
                self._fail()
            self.arguments.append(event.text)
            result += sse(
                "content_block_delta",
                {
                    "index": self.index,
                    "delta": {"type": "input_json_delta", "partial_json": event.text},
                },
            )
        elif isinstance(event, UsageReported):
            self.usage = event.usage
        elif isinstance(event, Completed):
            result += self._close()
            self.completed = True
            self.reason = event.stop_reason
            self.sequence = (
                event.stop_sequence if event.stop_sequence in self.stop_sequences else None
            )
        return result

    def finish(self) -> bytes:
        if not self.completed:
            self._fail()
        reason = {
            StopReason.LENGTH: "max_tokens",
            StopReason.TOOL_CALLS: "tool_use",
            StopReason.CONTENT_FILTER: "refusal",
        }.get(self.reason or StopReason.OTHER, "end_turn")
        if self.reason is StopReason.STOP and self.sequence is not None:
            reason = "stop_sequence"
        # Unmeasured usage stays null; an absent measurement is never a false zero.
        return (
            self._close()
            + sse(
                "message_delta",
                {
                    "delta": {"stop_reason": reason, "stop_sequence": self.sequence},
                    "usage": {
                        "input_tokens": self.usage.input_tokens,
                        "output_tokens": self.usage.output_tokens,
                    },
                },
            )
            + sse("message_stop", {})
        )

    def non_stream(self, events: tuple[CanonicalEvent, ...]) -> bytes:
        # Both encodings consume the same state machine. The Runtime already
        # bounds the input response, and this additional cap bounds wire growth.
        size = 0
        parts: list[bytes] = []
        for event in events:
            part = self.encode(event)
            size += len(part)
            if size > self.maximum_bytes:
                self._fail()
            parts.append(part)
        parts.append(self.finish())
        message = self.message()
        blocks: list[dict[str, object]] = []
        arguments = ""
        for frame in b"".join(parts).split(b"\n\n"):
            if not frame:
                continue
            value = json.loads(frame.split(b"\ndata: ", 1)[1])
            kind = value["type"]
            if kind == "content_block_start":
                blocks.append(value["content_block"])
                arguments = ""
            elif kind == "content_block_delta":
                delta = value["delta"]
                if delta["type"] == "input_json_delta":
                    arguments += delta["partial_json"]
                elif delta["type"] == "signature_delta":
                    blocks[-1]["signature"] = ""
                else:
                    key = "thinking" if delta["type"] == "thinking_delta" else "text"
                    blocks[-1][key] = str(blocks[-1][key]) + delta[key]
            elif kind == "content_block_stop" and blocks[-1]["type"] == "tool_use":
                blocks[-1]["input"] = json.loads(arguments or "{}")
            elif kind == "message_delta":
                message.update(value["delta"])
                message["usage"] = value["usage"]
        message["content"] = blocks
        return render(message)

    @staticmethod
    def _fail() -> None:
        raise InferenceFailure(PlatformError(ErrorCode.BACKEND_UNAVAILABLE))
