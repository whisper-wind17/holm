"""Request-local assembly of Runtime SSE into an OpenAI non-stream response."""

from __future__ import annotations

from typing import NoReturn

from holm_domain import ErrorCode
from holm_protocols import InferenceFailure, PlatformError, ProtocolExtension

from holm_gateway.openai_types import (
    DataSseLine,
    JsonObject,
    OpenAISseExtension,
    freeze_json,
    thaw_json,
)


class OpenAIResponseAssembler:
    """Retain wire values in memory only, bounded by the Runtime adapter's cap."""

    def __init__(self) -> None:
        self._payload: dict[str, object] = {}
        self._choices: dict[int, dict[str, object]] = {}
        self._tools: dict[int, dict[int, dict[str, object]]] = {}

    def observe(self, extension: ProtocolExtension | None) -> None:
        if not isinstance(extension, OpenAISseExtension):
            return
        for line in extension.lines:
            if isinstance(line, DataSseLine) and isinstance(line.payload, JsonObject):
                value = thaw_json(line.payload)
                if not isinstance(value, dict):
                    self._fail()
                self._merge_payload(value)

    def _merge_payload(self, value: dict[str, object]) -> None:
        for key, item in value.items():
            if key != "choices":
                if item is not None or key not in self._payload:
                    self._payload[key] = "chat.completion" if key == "object" else item
                continue
            self._payload.setdefault("choices", [])
            if not isinstance(item, list):
                self._fail()
            for ordinal, part in enumerate(item):
                if not isinstance(part, dict):
                    self._fail()
                index = part.get("index", ordinal)
                if type(index) is not int or index < 0:
                    self._fail()
                choice = self._choices.setdefault(index, {})
                for field, fragment in part.items():
                    if field == "delta":
                        if not isinstance(fragment, dict):
                            self._fail()
                        message = choice.setdefault("message", {})
                        if not isinstance(message, dict):
                            self._fail()
                        self._merge_message(index, message, fragment)
                    elif fragment is not None or field not in choice:
                        choice[field] = fragment

    def _merge_message(
        self, choice_index: int, message: dict[str, object], delta: dict[str, object]
    ) -> None:
        for key, value in delta.items():
            if key == "tool_calls":
                if not isinstance(value, list):
                    self._fail()
                tools = self._tools.setdefault(choice_index, {})
                for ordinal, fragment in enumerate(value):
                    if not isinstance(fragment, dict):
                        self._fail()
                    index = fragment.get("index", ordinal)
                    if type(index) is not int or index < 0:
                        self._fail()
                    tool = tools.setdefault(index, {})
                    for field, item in fragment.items():
                        if field == "index":
                            continue
                        if field == "function":
                            if not isinstance(item, dict):
                                self._fail()
                            function = tool.setdefault("function", {})
                            if not isinstance(function, dict):
                                self._fail()
                            for name, text in item.items():
                                self._append(function, name, text)
                        elif item is not None:
                            tool[field] = item
                message[key] = [tools[index] for index in sorted(tools)]
            elif key in {"content", "reasoning", "reasoning_content", "refusal"}:
                self._append(message, key, value)
            elif value is not None or key not in message:
                message[key] = value

    @staticmethod
    def _append(target: dict[str, object], key: str, value: object) -> None:
        previous = target.get(key)
        if isinstance(value, str) and isinstance(previous, str):
            target[key] = previous + value
        elif value is not None or key not in target:
            target[key] = value

    def finish(self) -> JsonObject:
        if "choices" not in self._payload:
            self._fail()
        self._payload["choices"] = [self._choices[index] for index in sorted(self._choices)]
        payload = freeze_json(self._payload)
        if not isinstance(payload, JsonObject):
            self._fail()
        return payload

    @staticmethod
    def _fail() -> NoReturn:
        raise InferenceFailure(PlatformError(ErrorCode.BACKEND_UNAVAILABLE))
