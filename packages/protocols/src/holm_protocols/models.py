"""Request-local, protocol-independent inference values."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal

from holm_domain import ErrorCode

type JsonScalar = None | bool | int | float | str
type JsonValue = JsonScalar | JsonArray | JsonObject


@dataclass(frozen=True, slots=True)
class JsonMember:
    name: str = field(repr=False)
    value: JsonValue = field(repr=False)


@dataclass(frozen=True, slots=True)
class JsonArray:
    items: tuple[JsonValue, ...] = field(repr=False)


@dataclass(frozen=True, slots=True)
class JsonObject:
    members: tuple[JsonMember, ...] = field(repr=False)


def freeze_json(value: object) -> JsonValue:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, list):
        return JsonArray(tuple(freeze_json(item) for item in value))
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return JsonObject(tuple(JsonMember(key, freeze_json(item)) for key, item in value.items()))
    raise ValueError("value is not JSON-compatible")


def thaw_json(value: JsonValue) -> object:
    if isinstance(value, JsonArray):
        return [thaw_json(item) for item in value.items]
    if isinstance(value, JsonObject):
        return {member.name: thaw_json(member.value) for member in value.members}
    return value


class ProtocolExtension:
    """Marker base for a typed adapter-to-adapter extension.

    The inference core may carry this value but deliberately exposes no API for
    inspecting it.  Concrete extension types live beside their protocol and
    Runtime adapters.
    """

    __slots__ = ()


@dataclass(frozen=True, slots=True)
class ServiceModelReference:
    public_model_name: str
    runtime_model_name: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class TextContent:
    text: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class ImageContent:
    url: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class ToolCall:
    call_id: str = field(repr=False)
    name: str = field(repr=False)
    arguments: str = field(repr=False)


type ContentBlock = TextContent | ImageContent


@dataclass(frozen=True, slots=True)
class CanonicalMessage:
    role: str = field(repr=False)
    content: tuple[ContentBlock, ...] = field(repr=False)
    tool_calls: tuple[ToolCall, ...] = field(default=(), repr=False)
    tool_call_id: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str = field(repr=False)
    description: str | None = field(repr=False)
    input_schema: JsonObject | None = field(repr=False)
    strict: bool | None = None


@dataclass(frozen=True, slots=True)
class SamplingParameters:
    """Protocol-neutral generation controls for one request."""

    temperature: float | None = None
    nucleus_probability: float | None = None
    stop_sequences: tuple[str, ...] = field(default=(), repr=False)
    random_seed: int | None = None
    maximum_output_tokens: int | None = None
    presence_penalty: float | None = None
    frequency_penalty: float | None = None
    top_k: int | None = None


class ToolChoiceMode(StrEnum):
    NONE = "none"
    AUTO = "auto"
    REQUIRED = "required"
    FUNCTION = "function"


@dataclass(frozen=True, slots=True)
class ToolChoice:
    mode: ToolChoiceMode
    function_name: str | None = field(default=None, repr=False)


class ResponseFormatKind(StrEnum):
    TEXT = "text"
    JSON_OBJECT = "json_object"
    JSON_SCHEMA = "json_schema"


@dataclass(frozen=True, slots=True)
class ResponseFormat:
    """Request-local output constraint, independent of a transport envelope."""

    kind: ResponseFormatKind
    name: str | None = field(default=None, repr=False)
    schema: JsonObject | None = field(default=None, repr=False)
    strict: bool | None = None
    description: str | None = field(default=None, repr=False)


class ReasoningEffort(StrEnum):
    NONE = "none"
    MINIMAL = "minimal"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"


@dataclass(frozen=True, slots=True)
class ModelReasoningPolicy:
    """Trusted architecture policy; request values never become error details."""

    efforts: tuple[ReasoningEffort, ...]
    aliases: Mapping[ReasoningEffort, ReasoningEffort]
    can_disable: bool

    def resolve(self, effort: ReasoningEffort) -> ReasoningEffort:
        if effort is ReasoningEffort.NONE:
            if self.can_disable:
                return effort
        elif effort in self.efforts:
            return effort
        elif effort in self.aliases:
            return self.aliases[effort]
        supported = self.efforts + ((ReasoningEffort.NONE,) if self.can_disable else ())
        raise InferenceFailure(
            PlatformError(ErrorCode.INVALID_REQUEST, supported_reasoning_efforts=supported)
        )

    def resolve_nearest(self, effort: ReasoningEffort) -> ReasoningEffort | None:
        try:
            return self.resolve(effort)
        except InferenceFailure:
            supported = self.efforts + ((ReasoningEffort.NONE,) if self.can_disable else ())
            order = tuple(ReasoningEffort)
            return (
                min(supported, key=lambda item: abs(order.index(item) - order.index(effort)))
                if supported
                else None
            )


@dataclass(frozen=True, slots=True)
class ReasoningControl:
    """Request-local thinking control; NONE disables thinking."""

    effort: ReasoningEffort
    nearest_supported: bool = False


@dataclass(frozen=True, slots=True)
class CanonicalRequest:
    service_model: ServiceModelReference
    stream: bool
    sampling: SamplingParameters
    messages: tuple[CanonicalMessage, ...] = field(repr=False)
    tools: tuple[ToolDefinition, ...] = field(repr=False)
    extension: ProtocolExtension | None = field(default=None, repr=False)
    reasoning: ReasoningControl | None = None
    tool_choice: ToolChoice | None = field(default=None, repr=False)
    parallel_tool_calls: bool | None = None
    response_format: ResponseFormat | None = field(default=None, repr=False)
    include_stream_usage: bool = False


@dataclass(frozen=True, slots=True)
class ServiceModelSummary:
    public_model_name: str
    available: bool


@dataclass(frozen=True, slots=True)
class UsageInfo:
    input_tokens: int | None = None
    output_tokens: int | None = None


class StopReason(StrEnum):
    STOP = "stop"
    LENGTH = "length"
    TOOL_CALLS = "tool_calls"
    CONTENT_FILTER = "content_filter"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class PlatformError:
    """Stable failure classification without transport status or wire shape."""

    code: ErrorCode
    supported_reasoning_efforts: tuple[ReasoningEffort, ...] = ()
    unsupported_fields: tuple[str, ...] = field(default=(), repr=False)
    reason: Literal["context_length_exceeded"] | None = field(default=None, repr=False)
    max_context_tokens: int | None = field(default=None, repr=False)
    limit_type: str | None = None
    limit: int | None = None
    retry_after: int | None = None
    queue_timed_out: bool = False


class InferenceFailure(RuntimeError):
    """Carry only a sanitized platform classification across core boundaries."""

    def __init__(self, error: PlatformError, *, counts_toward_breaker: bool = False) -> None:
        self.error = error
        self.counts_toward_breaker = counts_toward_breaker
        super().__init__("inference request rejected")


@dataclass(frozen=True, slots=True)
class StreamStarted:
    extension: ProtocolExtension | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class ResponseStarted:
    extension: ProtocolExtension | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class ContentDelta:
    text: str = field(repr=False)
    extension: ProtocolExtension | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class ThinkingDelta:
    text: str = field(repr=False)
    extension: ProtocolExtension | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class ToolCallStarted:
    index: int
    call_id: str | None = field(repr=False)
    name: str = field(repr=False)
    extension: ProtocolExtension | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class ToolArgumentsDelta:
    index: int
    text: str = field(repr=False)
    extension: ProtocolExtension | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class UsageReported:
    usage: UsageInfo
    extension: ProtocolExtension | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class Completed:
    stop_reason: StopReason | None
    extension: ProtocolExtension | None = field(default=None, repr=False)
    stop_sequence: str | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class ResponseCompleted:
    extension: ProtocolExtension = field(repr=False)


@dataclass(frozen=True, slots=True)
class StreamEnded:
    extension: ProtocolExtension | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class Failed:
    error: PlatformError


type CanonicalEvent = (
    StreamStarted
    | ResponseStarted
    | ContentDelta
    | ThinkingDelta
    | ToolCallStarted
    | ToolArgumentsDelta
    | UsageReported
    | Completed
    | ResponseCompleted
    | StreamEnded
    | Failed
)
