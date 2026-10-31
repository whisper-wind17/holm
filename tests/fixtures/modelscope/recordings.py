"""Byte-faithful access to the FX-MS-09 recordings.

The files in ``recorded/`` are the only evidence this repository has about how
the real ModelScope Hub answers: they were recorded on the workstation and the
development container's reachability of ``modelscope.cn`` is unverified (see
``recorded/PROVENANCE.md``).  Nothing here edits them; it parses the stored
status line, headers and body so that :mod:`fake_hub` can put them back on the
wire unchanged.

Shape note: FX-MS-08 stores its bodies as ``<name>.json``; the ModelScope
recorder writes ``<name>.body`` (one recording is an HTML 302 page, not JSON).
That is the only difference between the two loaders.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any, Final

RECORDING_DIRECTORY: Final[Path] = Path(__file__).resolve().parent / "recorded"

_STATUS_PREFIX: Final[str] = "HTTP/"

# Hop-by-hop and transport-framing headers must not be replayed: the fake Hub
# speaks HTTP/1.1 over a fresh socket and computes its own framing.
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

    status, headers = _parse_headers(
        (RECORDING_DIRECTORY / f"{name}.headers").read_text(encoding="utf-8")
    )
    body_path = RECORDING_DIRECTORY / f"{name}.body"
    body = body_path.read_bytes() if body_path.is_file() else b""
    return RecordedResponse(name=name, status=status, headers=headers, body=body)


def recording_names() -> tuple[str, ...]:
    """Every recording present on disk, so a test can prove none is ignored."""

    return tuple(sorted(path.stem for path in RECORDING_DIRECTORY.glob("*.headers")))


def body_without_request_id(name: str) -> str:
    """One recorded body with its ``RequestId`` removed.

    ``RequestId`` differs on every request by construction, so it is the only
    thing that keeps two otherwise identical answers from comparing equal.
    Blanking it is what lets a test state "these two responses are the same
    answer" without editing the recordings themselves.
    """

    document = load_recording(name).json_body()
    if not isinstance(document, dict):
        raise TypeError(f"recording {name} is not a JSON object")
    return json.dumps({key: value for key, value in document.items() if key != "RequestId"})


__all__ = [
    "RECORDING_DIRECTORY",
    "RecordedResponse",
    "body_without_request_id",
    "load_recording",
    "recording_names",
]
