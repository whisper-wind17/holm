"""M3-08 bounded Runtime log paging and server-side redaction."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest
from grpc import aio
from holm_contracts.agent.v1 import agent_pb2
from holm_host_agent.runtime_logs import (
    REDACTED_VALUE,
    RuntimeLogRequestError,
    read_runtime_log_page,
)
from holm_host_agent.server import HostAgentService

INSTANCE_ID = UUID("01990000-0000-7000-8000-000000000308")


def _write_log(root: Path, lines: list[str]) -> None:
    instance_root = root / str(INSTANCE_ID)
    instance_root.mkdir(parents=True)
    (instance_root / "runtime.log").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _stream(root: Path, *, max_characters: int = 4_096) -> HostAgentService:
    return HostAgentService(
        runtime_state_root=root,
        max_log_line_characters=max_characters,
    )


async def _page(
    service: HostAgentService,
    *,
    offset: int,
    page_size: int,
) -> agent_pb2.StreamLogsResponse:
    responses = [
        response
        async for response in service.StreamLogs(
            agent_pb2.StreamLogsRequest(
                instance_id=str(INSTANCE_ID),
                cursor_offset=offset,
                page_size=page_size,
            ),
            cast(aio.ServicerContext, object()),
        )
    ]
    assert len(responses) == 1
    return responses[0]


def test_M3_08_FX_HA_05_real_StreamLogs_retains_500_and_pages_without_duplicates(
    tmp_path: Path,
) -> None:
    _write_log(tmp_path, [f"line-{index}" for index in range(620)])
    service = _stream(tmp_path)

    async def scenario() -> tuple[list[int], list[str]]:
        offset = 0
        sequences: list[int] = []
        text: list[str] = []
        while True:
            page = await _page(service, offset=offset, page_size=100)
            sequences.extend(entry.sequence for entry in page.entries)
            text.extend(entry.text for entry in page.entries)
            offset = page.next_cursor_offset
            if not page.has_more:
                break
        return sequences, text

    sequences, text = asyncio.run(scenario())
    assert sequences == list(range(500))
    assert len(sequences) == len(set(sequences))
    assert text == [f"line-{index}" for index in range(120, 620)]


def test_M3_08_FX_HA_05_real_StreamLogs_bounds_long_line_and_keeps_ANSI_plain(
    tmp_path: Path,
) -> None:
    ansi = "\x1b[31mred text\x1b[0m"
    _write_log(tmp_path, [ansi, "x" * 1_000_000])
    page = asyncio.run(_page(_stream(tmp_path, max_characters=128), offset=0, page_size=2))

    assert page.entries[0].text == ansi
    assert "\x1b[31m" in page.entries[0].text
    assert not page.entries[0].truncated
    assert page.entries[1].text == "x" * 128
    assert page.entries[1].truncated
    assert page.entries[1].original_character_count == 1_000_000


def test_M3_08_runtime_logs_redact_keys_headers_database_passwords_and_arguments(
    tmp_path: Path,
) -> None:
    secrets = (
        "sk-" + "proj-" + "abcdefghijklmnopqrstuv",
        "holm_sk_platform-secret-value",
        "header-secret-value",
        "database-password",
        "argument-secret",
    )
    _write_log(
        tmp_path,
        [
            f"key={secrets[0]}",
            f"platform={secrets[1]}",
            f"Authorization: Basic {secrets[2]}",
            f"postgresql://control:{secrets[3]}@database/holm",
            f"runtime --api-key={secrets[4]}",
        ],
    )

    page = asyncio.run(_page(_stream(tmp_path), offset=0, page_size=5))
    rendered = "\n".join(entry.text for entry in page.entries)
    assert REDACTED_VALUE in rendered
    assert all(secret not in rendered for secret in secrets)


def test_M3_08_runtime_logs_missing_file_is_an_empty_page(tmp_path: Path) -> None:
    page = asyncio.run(_page(_stream(tmp_path), offset=0, page_size=100))
    assert list(page.entries) == []
    assert page.next_cursor_offset == 0
    assert not page.has_more


@pytest.mark.parametrize(("offset", "page_size"), ((501, 100), (0, 101), (0, 0)))
def test_M3_08_runtime_logs_reject_out_of_range_cursor_or_page_size(
    tmp_path: Path,
    offset: int,
    page_size: int,
) -> None:
    with pytest.raises(RuntimeLogRequestError):
        read_runtime_log_page(
            tmp_path,
            str(INSTANCE_ID),
            cursor_offset=offset,
            page_size=page_size,
        )
