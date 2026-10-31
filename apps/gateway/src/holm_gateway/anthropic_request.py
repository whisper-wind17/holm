"""Anthropic input mapping. Content and ignored client metadata stay request-local."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Never

from holm_domain import ErrorCode
from holm_protocols import (
    CanonicalMessage,
    CanonicalRequest,
    ContentBlock,
    ImageContent,
    InferenceFailure,
    JsonObject,
    PlatformError,
    ProtocolExtension,
    ReasoningControl,
    ReasoningEffort,
    SamplingParameters,
    ServiceModelReference,
    TextContent,
    ToolCall,
    ToolChoice,
    ToolChoiceMode,
    ToolDefinition,
    freeze_json,
)

from holm_gateway.openai_adapter import _optional_integer, _optional_number

_FIELDS = frozenset(
    {
        "model",
        "max_tokens",
        "messages",
        "system",
        "stream",
        "temperature",
        "top_p",
        "top_k",
        "stop_sequences",
        "tools",
        "tool_choice",
        "thinking",
        "output_config",
        "metadata",
        "context_management",
        "service_tier",
        "safeguards",
        "cache_control",
    }
)


@dataclass(frozen=True, slots=True)
class AnthropicRequestExtension(ProtocolExtension):
    omit_thinking: bool = False


def invalid(*names: str) -> Never:
    raise InferenceFailure(
        PlatformError(
            ErrorCode.INVALID_REQUEST,
            unsupported_fields=tuple(sorted(name[:64] for name in names)[:8]),
        )
    )


def obj(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        invalid()
    return dict(value)


def string(value: object) -> str:
    if not isinstance(value, str) or not value:
        invalid()
    return value


def blocks(value: object) -> list[dict[str, object]]:
    if isinstance(value, str):
        return [{"type": "text", "text": value}]
    if not isinstance(value, list):
        invalid()
    return [obj(item) for item in value]


def text_blocks(value: object) -> str:
    result: list[str] = []
    for block in blocks(value):
        if block.get("type") != "text" or not isinstance(block.get("text"), str):
            invalid()
        result.append(str(block["text"]))
    return "\n".join(result)


def content(block: dict[str, object]) -> ContentBlock:
    kind = block.get("type")
    if kind == "text":
        text = block.get("text")
        if not isinstance(text, str):
            invalid()
        return TextContent(text)
    if kind == "image":
        source = obj(block.get("source"))
        if source.get("type") == "url":
            return ImageContent(string(source.get("url")))
        if source.get("type") == "base64":
            media = string(source.get("media_type"))
            if media not in {"image/jpeg", "image/png", "image/gif", "image/webp"}:
                invalid()
            return ImageContent(f"data:{media};base64,{string(source.get('data'))}")
    invalid(str(kind))


@dataclass(slots=True)
class _Message:
    role: str
    blocks: list[dict[str, object]] = field(repr=False)


def messages(payload: dict[str, object]) -> tuple[CanonicalMessage, ...]:
    raw = payload.get("messages")
    if not isinstance(raw, list) or not raw:
        invalid()
    parsed: list[_Message] = []
    for item in raw:
        raw_message = obj(item)
        role = string(raw_message.get("role"))
        if role not in {"system", "user", "assistant"}:
            invalid()
        parsed.append(_Message(role, blocks(raw_message.get("content"))))
    system = [text_blocks(payload["system"])] if "system" in payload else []
    while parsed and parsed[0].role == "system":
        system.append(text_blocks(parsed.pop(0).blocks))
    # Work on original user messages before tool_result expansion, so reminders
    # never become non-initial system messages or contaminate tool results.
    prefixes: dict[int, list[dict[str, object]]] = {}
    for index, message in enumerate(parsed):
        if message.role != "system":
            continue
        reminder: dict[str, object] = {"type": "text", "text": text_blocks(message.blocks)}
        target = next((m for m in parsed[index + 1 :] if m.role == "user"), None)
        if target is not None:
            prefixes.setdefault(id(target), []).append(reminder)
        else:
            target = next((m for m in reversed(parsed[:index]) if m.role == "user"), None)
            if target is not None:
                target.blocks.append(reminder)
            else:
                message.role = "user"
                message.blocks = [reminder]
    for message in parsed:
        message.blocks[:0] = prefixes.get(id(message), [])
    result: list[CanonicalMessage] = []
    if system:
        result.append(CanonicalMessage("system", (TextContent("\n".join(system)),)))
    for message in parsed:
        if message.role == "system":
            continue
        ordinary: list[ContentBlock] = []
        calls: list[ToolCall] = []
        for block in message.blocks:
            kind = block.get("type")
            if kind in {"thinking", "redacted_thinking"} and message.role == "assistant":
                continue
            if kind == "tool_use" and message.role == "assistant":
                arguments = obj(block.get("input"))
                calls.append(
                    ToolCall(
                        string(block.get("id")),
                        string(block.get("name")),
                        json.dumps(arguments, ensure_ascii=False, separators=(",", ":")),
                    )
                )
            elif kind == "tool_result" and message.role == "user":
                call_id = string(block.get("tool_use_id"))
                if "is_error" in block and not isinstance(block["is_error"], bool):
                    invalid()
                parts = [content(item) for item in blocks(block.get("content", ""))]
                result.append(
                    CanonicalMessage(
                        "tool",
                        (
                            TextContent(
                                "\n".join(
                                    part.text for part in parts if isinstance(part, TextContent)
                                )
                            ),
                        ),
                        tool_call_id=call_id,
                    )
                )
                images = tuple(part for part in parts if isinstance(part, ImageContent))
                if images:
                    result.append(CanonicalMessage("user", images))
            else:
                ordinary.append(content(block))
        if ordinary or calls:
            result.append(CanonicalMessage(message.role, tuple(ordinary), tuple(calls)))
    return tuple(result)


def parse(payload: dict[str, object]) -> CanonicalRequest:
    if unsupported := frozenset(payload) - _FIELDS:
        invalid(*unsupported)
    model = string(payload.get("model"))
    stream = payload.get("stream", False)
    if not isinstance(stream, bool):
        invalid()
    maximum = _optional_integer(payload, "max_tokens", minimum=1)
    if maximum is None:
        invalid()
    stop = payload.get("stop_sequences", [])
    if not isinstance(stop, list) or not all(isinstance(item, str) for item in stop):
        invalid()
    tools: list[ToolDefinition] = []
    raw_tools = payload.get("tools", [])
    if not isinstance(raw_tools, list):
        invalid()
    for raw_tool in raw_tools:
        tool = obj(raw_tool)
        if "type" in tool:
            invalid(string(tool["type"]))
        schema = freeze_json(obj(tool.get("input_schema")))
        assert isinstance(schema, JsonObject)
        description = tool.get("description")
        if description is not None and not isinstance(description, str):
            invalid()
        tools.append(ToolDefinition(string(tool.get("name")), description, schema))
    choice = None
    parallel = None
    if "tool_choice" in payload:
        raw_choice = obj(payload["tool_choice"])
        modes = {
            "auto": ToolChoiceMode.AUTO,
            "any": ToolChoiceMode.REQUIRED,
            "tool": ToolChoiceMode.FUNCTION,
            "none": ToolChoiceMode.NONE,
        }
        mode = modes.get(str(raw_choice.get("type")))
        if mode is None:
            invalid()
        name = string(raw_choice.get("name")) if mode is ToolChoiceMode.FUNCTION else None
        choice = ToolChoice(mode, name)
        if "disable_parallel_tool_use" in raw_choice:
            if not isinstance(raw_choice["disable_parallel_tool_use"], bool):
                invalid()
            parallel = not raw_choice["disable_parallel_tool_use"]
    reasoning = None
    omit = False
    if "thinking" in payload:
        thinking = obj(payload["thinking"])
        kind = thinking.get("type")
        omit = thinking.get("display") == "omitted"
        if kind == "disabled":
            effort = ReasoningEffort.NONE
        elif kind in {"enabled", "adaptive"}:
            config = obj(payload.get("output_config", {}))
            if "effort" in config:
                try:
                    effort = ReasoningEffort(string(config["effort"]))
                except ValueError:
                    invalid()
            elif kind == "enabled":
                budget = _optional_integer(thinking, "budget_tokens", minimum=1)
                if budget is None:
                    invalid()
                effort = (
                    ReasoningEffort.LOW
                    if budget < 4096
                    else (ReasoningEffort.MEDIUM if budget < 16384 else ReasoningEffort.HIGH)
                )
            else:
                effort = ReasoningEffort.MEDIUM
        else:
            invalid()
        reasoning = ReasoningControl(effort, nearest_supported=True)
    return CanonicalRequest(
        service_model=ServiceModelReference(model),
        stream=stream,
        sampling=SamplingParameters(
            maximum_output_tokens=maximum,
            temperature=_optional_number(payload, "temperature", minimum=0, maximum=1),
            nucleus_probability=_optional_number(payload, "top_p", minimum=0, maximum=1),
            top_k=_optional_integer(payload, "top_k", minimum=0),
            stop_sequences=tuple(stop),
        ),
        messages=messages(payload),
        tools=tuple(tools),
        tool_choice=choice,
        parallel_tool_calls=parallel,
        reasoning=reasoning,
        include_stream_usage=True,
        extension=AnthropicRequestExtension(omit),
    )
