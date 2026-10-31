"""FX-SYS-02: deployment assertions reuse M4-10 blocks and M0-08 manifest."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from holm_contracts import ReleaseManifest, version_payload_from_manifest
from holm_gateway.__main__ import _load_version, _parse_args
from holm_gateway.configuration import GatewayConfigurationError
from test_m0_08_release_manifest import _valid_manifest_data
from test_M4_10_caddyfile import _block, _directives
from test_M7_01A_production_compose import _document, _service


def test_SYS_02_TC_HEALTH_10_edge_private_listener_and_compose_wiring() -> None:
    text = _directives()
    block = _block(text, "http://:8081 {")
    assert _block(block, "@live {").split() == [
        "@live",
        "{",
        "method",
        "GET",
        "path",
        "/internal/health/live",
        "}",
    ]
    assert 'respond `{"status":"live"}` 200' in _block(block, "handle @live {")
    assert "header Content-Type application/json" in block
    assert _block(block, "handle {").split() == ["handle", "{", "respond", "404", "}"]
    assert "{{" not in block
    assert "reverse_proxy" not in block
    document = _document()
    assert _service(document, "edge")["ports"] == ["80:8080", "443:8443"]
    command = _service(document, "control-api")["command"]
    assert isinstance(command, list)
    for flag, value in (
        ("--gateway-health-url", "http://gateway:8001"),
        ("--edge-health-url", "http://edge:8081"),
    ):
        assert command[command.index(flag) + 1] == value
    assert _service(document, "gateway")["healthcheck"]["test"] == [
        "CMD",
        "holm-healthcheck",
        "http",
        "http://127.0.0.1:8001/internal/health/live",
    ]


def test_SYS_02_TC_HEALTH_07_manifest_source_and_fail_closed_metadata(tmp_path: Path) -> None:
    manifest = ReleaseManifest.model_validate(_valid_manifest_data())
    path = tmp_path / "platform.json"
    for payload in (
        manifest.model_dump(mode="json"),
        {"release_manifest": manifest.model_dump(mode="json")},
    ):
        path.write_text(json.dumps(payload))
        assert _load_version(path) == version_payload_from_manifest(manifest)
    path.write_text('{"build_version":"pretend"}')
    with pytest.raises(GatewayConfigurationError) as error:
        _load_version(path)
    assert error.value.code == "release_manifest_invalid"
    args = _parse_args(
        [
            "--database-url-file",
            "db",
            "--api-key-pepper-keyring-file",
            "pepper",
            "--runtime-compatibility",
            "runtime",
            "--request-ledger-path",
            "ledger",
        ]
    )
    assert args.release_manifest == Path("/etc/holm/platform.yaml")
