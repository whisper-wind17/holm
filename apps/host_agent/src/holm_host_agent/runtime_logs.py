"""Bounded Runtime log paging with mandatory credential redaction."""

from __future__ import annotations

import codecs
import os
import re
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Final
from uuid import UUID

MAX_RETAINED_LOG_LINES: Final[int] = 500
MAX_LOG_PAGE_SIZE: Final[int] = 100
DEFAULT_LOG_PAGE_SIZE: Final[int] = 50
DEFAULT_MAX_LOG_LINE_CHARACTERS: Final[int] = 4_096
MAX_LOG_LINE_CHARACTERS: Final[int] = 65_536
REDACTED_VALUE: Final[str] = "[REDACTED]"
_READ_CHUNK_BYTES: Final[int] = 64 * 1024

# This table is the single definition of the Runtime-log redaction policy.
_AUTHORIZATION_PATTERN: Final[re.Pattern[str]] = re.compile(r"(?i)\bauthorization\s*:.*$")
_PLATFORM_KEY_PATTERN: Final[re.Pattern[str]] = re.compile(r"\bholm_sk_[A-Za-z0-9_-]{8,}\b")
_OPENAI_KEY_PATTERN: Final[re.Pattern[str]] = re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{8,}\b")
_DATABASE_URL_PASSWORD_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?i)(\bpostgres(?:ql)?(?:\+[A-Za-z0-9_]+)?://[^\s:/@]+:)([^\s/@]+)(@)"
)
_API_KEY_ARGUMENT_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?i)(--api-key(?:=|\s+))(?P<quote>['\"]?)[^\s'\"]+(?P=quote)"
)


class RuntimeLogRequestError(ValueError):
    """A sanitized, stable rejection safe to expose as a gRPC detail."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class RuntimeLogEntry:
    sequence: int
    text: str
    truncated: bool
    original_character_count: int


@dataclass(frozen=True, slots=True)
class RuntimeLogPage:
    entries: tuple[RuntimeLogEntry, ...]
    next_cursor_offset: int
    has_more: bool


def redact_runtime_log_line(value: str) -> str:
    """Remove credential-bearing forms without interpreting ANSI sequences."""

    redacted = _AUTHORIZATION_PATTERN.sub(f"Authorization: {REDACTED_VALUE}", value)
    redacted = _DATABASE_URL_PASSWORD_PATTERN.sub(rf"\1{REDACTED_VALUE}\3", redacted)
    redacted = _API_KEY_ARGUMENT_PATTERN.sub(rf"\1{REDACTED_VALUE}", redacted)
    redacted = _PLATFORM_KEY_PATTERN.sub(REDACTED_VALUE, redacted)
    return _OPENAI_KEY_PATTERN.sub(REDACTED_VALUE, redacted)


def read_runtime_log_page(
    state_root: Path,
    instance_id: str,
    *,
    cursor_offset: int,
    page_size: int,
    max_log_line_characters: int = DEFAULT_MAX_LOG_LINE_CHARACTERS,
) -> RuntimeLogPage:
    """Read one page from the most recent retained lines of one Runtime log."""

    canonical_id = _canonical_instance_id(instance_id)
    if not 1 <= page_size <= MAX_LOG_PAGE_SIZE:
        raise RuntimeLogRequestError("log_page_size_invalid")
    if cursor_offset < 0:
        raise RuntimeLogRequestError("log_cursor_invalid")
    if not 1 <= max_log_line_characters <= MAX_LOG_LINE_CHARACTERS:
        raise RuntimeLogRequestError("log_line_limit_invalid")

    log_path = state_root / canonical_id / "runtime.log"
    try:
        descriptor = os.open(log_path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as stream:
            retained = deque(
                _iter_bounded_lines(stream, max_log_line_characters),
                maxlen=MAX_RETAINED_LOG_LINES,
            )
    except FileNotFoundError:
        retained = deque(maxlen=MAX_RETAINED_LOG_LINES)
    except OSError as error:
        raise RuntimeLogRequestError("runtime_log_unavailable") from error

    entries = tuple(retained)
    if cursor_offset > len(entries):
        raise RuntimeLogRequestError("log_cursor_invalid")
    end = min(cursor_offset + page_size, len(entries))
    page_entries = tuple(
        RuntimeLogEntry(
            sequence=sequence,
            text=entry.text,
            truncated=entry.truncated,
            original_character_count=entry.original_character_count,
        )
        for sequence, entry in enumerate(entries[cursor_offset:end], start=cursor_offset)
    )
    return RuntimeLogPage(
        entries=page_entries,
        next_cursor_offset=end,
        has_more=end < len(entries),
    )


@dataclass(frozen=True, slots=True)
class _BoundedLine:
    text: str
    truncated: bool
    original_character_count: int


def _iter_bounded_lines(stream: BinaryIO, limit: int) -> Iterator[_BoundedLine]:
    """Scan arbitrarily long UTF-8 lines while retaining only their bounded prefix."""

    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    prefix: list[str] = []
    character_count = 0
    previous_character: str | None = None

    def consume(text: str) -> Iterator[_BoundedLine]:
        nonlocal character_count, previous_character
        for character in text:
            if character == "\n":
                if previous_character == "\r":
                    character_count -= 1
                    if prefix and prefix[-1] == "\r":
                        prefix.pop()
                bounded = "".join(prefix)
                yield _BoundedLine(
                    text=redact_runtime_log_line(bounded),
                    truncated=character_count > len(bounded),
                    original_character_count=character_count,
                )
                prefix.clear()
                character_count = 0
                previous_character = None
                continue
            character_count += 1
            if len(prefix) < limit:
                prefix.append(character)
            previous_character = character

    while chunk := stream.read(_READ_CHUNK_BYTES):
        yield from consume(decoder.decode(chunk))
    yield from consume(decoder.decode(b"", final=True))
    if character_count or prefix:
        bounded = "".join(prefix)
        yield _BoundedLine(
            text=redact_runtime_log_line(bounded),
            truncated=character_count > len(bounded),
            original_character_count=character_count,
        )


def _canonical_instance_id(value: str) -> str:
    try:
        canonical = str(UUID(value))
    except (ValueError, AttributeError) as error:
        raise RuntimeLogRequestError("instance_id_invalid") from error
    if value != canonical:
        raise RuntimeLogRequestError("instance_id_invalid")
    return canonical


__all__ = [
    "DEFAULT_LOG_PAGE_SIZE",
    "DEFAULT_MAX_LOG_LINE_CHARACTERS",
    "MAX_LOG_LINE_CHARACTERS",
    "MAX_LOG_PAGE_SIZE",
    "MAX_RETAINED_LOG_LINES",
    "REDACTED_VALUE",
    "RuntimeLogEntry",
    "RuntimeLogPage",
    "RuntimeLogRequestError",
    "read_runtime_log_page",
    "redact_runtime_log_line",
]
