"""M4-06 standard Gateway error envelope snapshots."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final
from uuid import UUID

from holm_domain import GATEWAY_ERROR_CODES, ErrorCode
from holm_gateway.openai_adapter import OpenAIProtocolAdapter
from holm_protocols import PlatformError
from holm_runtimes import load_runtime_compatibility

REPOSITORY_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
SNAPSHOT: Final[Path] = REPOSITORY_ROOT / "tests/contract/snapshots/m4_06_gateway_errors.json"
REQUEST_ID: Final[UUID] = UUID("00000000-0000-7000-8000-000000004620")


def test_M4_06_TC_GW_20_standard_error_snapshot_is_stable_and_sanitized() -> None:
    runtime = load_runtime_compatibility(REPOSITORY_ROOT / "runtime-compat.yaml").runtime("vllm")
    assert runtime is not None
    adapter = OpenAIProtocolAdapter(
        {"vllm": runtime.request_capabilities},
        maximum_json_depth=32,
    )

    actual: dict[str, object] = {}
    for code in sorted(GATEWAY_ERROR_CODES, key=lambda item: item.value):
        error = (
            PlatformError(code, limit_type="per_minute", limit=60, retry_after=1)
            if code is ErrorCode.RATE_LIMIT_EXCEEDED
            else PlatformError(code)
        )
        mapped = adapter.map_error(error, REQUEST_ID)
        actual[code.value] = {
            "status": mapped.status_code,
            "media_type": mapped.media_type,
            "headers": dict(mapped.headers),
            "body": json.loads(mapped.body),
        }

    assert actual == json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    rendered = json.dumps(actual, ensure_ascii=False)
    assert str(REQUEST_ID) in rendered
    assert all(
        forbidden not in rendered
        for forbidden in (
            "instance-secret",
            "10.0.0.8:8000",
            "GPU-0000",
            "container-id",
            "SENTINEL_REQUEST_BODY",
        )
    )
