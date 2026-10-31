"""Byte-faithful access to the FX-MS-08 recordings.

The recordings in ``recorded/`` are the only evidence this repository has about
how the real Hugging Face Hub answers: the development container cannot reach
``huggingface.co`` at all (see ``recorded/PROVENANCE.md``).  Nothing in this
module edits them; it only parses the stored status line, headers and body so
that :mod:`fake_hub` can put them back on the wire unchanged.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any, Final

RECORDING_DIRECTORY: Final[Path] = Path(__file__).resolve().parent / "recorded"

# Recorded status lines are HTTP/2 ("HTTP/2 200 "), which has no reason phrase.
_STATUS_PREFIX: Final[str] = "HTTP/"

# Hop-by-hop and transport-framing headers must not be replayed: the fake Hub
# speaks HTTP/1.1 over a fresh socket and computes its own framing.  Replaying
# the recorded ``content-length`` would also be wrong for the derived responses.
_TRANSPORT_HEADERS: Final[frozenset[str]] = frozenset(
    {
        "alt-svc",
        "connection",
        "content-length",
        "keep-alive",
        "transfer-encoding",
    }
)


@dataclass(frozen=True, slots=True)
class RecordedResponse:
    """One recorded Hub response: status, headers and body, exactly as stored."""

    name: str
    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes

    def header(self, name: str) -> str | None:
        wanted = name.lower()
        for key, value in self.headers:
            if key.lower() == wanted:
                return value
        return None

    def json_body(self) -> Any:
        return json.loads(self.body.decode("utf-8"))

    def replayable_headers(self) -> tuple[tuple[str, str], ...]:
        """The recorded headers minus the ones that frame this connection."""

        return tuple(
            (key, value) for key, value in self.headers if key.lower() not in _TRANSPORT_HEADERS
        )


def _parse_headers(text: str) -> tuple[int, tuple[tuple[str, str], ...]]:
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines or not lines[0].startswith(_STATUS_PREFIX):
        raise ValueError("a recording must start with an HTTP status line")
    status = int(lines[0].split(maxsplit=1)[1].split()[0])
    headers: list[tuple[str, str]] = []
    for line in lines[1:]:
        key, separator, value = line.partition(":")
        if not separator:
            raise ValueError(f"malformed recorded header line: {line!r}")
        headers.append((key.strip(), value.strip()))
    return status, tuple(headers)


@cache
def load_recording(name: str) -> RecordedResponse:
    """Read one recording by its file stem."""

    header_path = RECORDING_DIRECTORY / f"{name}.headers"
    status, headers = _parse_headers(header_path.read_text(encoding="utf-8"))
    body_path = RECORDING_DIRECTORY / f"{name}.json"
    body = body_path.read_bytes() if body_path.is_file() else b""
    return RecordedResponse(name=name, status=status, headers=headers, body=body)


def recording_names() -> tuple[str, ...]:
    """Every recording present on disk, so a test can prove none is ignored."""

    return tuple(sorted(path.stem for path in RECORDING_DIRECTORY.glob("*.headers")))


__all__ = [
    "RECORDING_DIRECTORY",
    "RecordedResponse",
    "load_recording",
    "recording_names",
]
