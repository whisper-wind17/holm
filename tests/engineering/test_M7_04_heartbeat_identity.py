"""FX-M7-04: production Compose probes versus actual heartbeat writer sources."""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest
from holm_domain import Component

ROOT = Path(__file__).resolve().parents[2]
WRITERS = {
    "download-worker": "download/src/holm_download_worker/worker.py",
    "instance-controller": "instance/src/holm_instance_controller/heartbeat.py",
    "ledger-writer": "ledger/src/holm_ledger_writer/writer.py",
    "maintenance-worker": "maintenance/src/holm_maintenance_worker/worker.py",
}


def _services() -> dict[str, Any]:
    text = (ROOT / "deploy/compose/compose.prod.yaml").read_text()
    return json.loads("\n".join(line for line in text.splitlines() if not line.startswith("#")))[
        "services"
    ]


def _option(command: list[str], name: str) -> str:
    assert command.count(name) == 1, f"missing or duplicate {name}"
    return command[command.index(name) + 1]


def _check_identities(services: dict[str, Any]) -> None:
    checked = set()
    for name, service in services.items():
        probe = service.get("healthcheck", {}).get("test", [])
        if probe[:3] != ["CMD", "holm-healthcheck", "heartbeat"]:
            continue
        assert name in WRITERS, f"unknown heartbeat writer: {name}"
        tree = ast.parse((ROOT / "apps/workers" / WRITERS[name]).read_text())
        components = [
            keyword.value.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            for keyword in node.keywords
            if keyword.arg == "component"
            and isinstance(keyword.value, ast.Attribute)
            and isinstance(keyword.value.value, ast.Name)
            and keyword.value.value.id == "Component"
        ]
        assert len(components) == 1, f"cannot identify heartbeat component: {name}"
        assert _option(probe, "--component") == Component[components[0]].value, name
        if name == "instance-controller":
            entry = ROOT / "apps/workers/instance/src/holm_instance_controller/__main__.py"
            ids = [
                ast.literal_eval(keyword.value)
                for node in ast.walk(ast.parse(entry.read_text()))
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "ControllerHeartbeat"
                for keyword in node.keywords
                if keyword.arg == "instance_id"
            ]
            assert len(ids) == 1, "cannot identify controller heartbeat instance"
            instance_id = ids[0]
        else:
            instance_id = _option(service["command"], "--instance-id")
        assert _option(probe, "--instance-id") == instance_id, name
        checked.add(name)
    assert checked == set(WRITERS), "heartbeat probes missing"


def test_M7_04_heartbeat_identity_matches_writers() -> None:
    _check_identities(_services())


@pytest.mark.parametrize("service", WRITERS)
@pytest.mark.parametrize("option", ["--instance-id", "--component"])
def test_M7_04_heartbeat_identity_rejects_wrong_value(service: str, option: str) -> None:
    # FX-M7-04 positive control: mutate only the probe, leaving its writer unchanged.
    services = _services()
    probe = services[service]["healthcheck"]["test"]
    probe[probe.index(option) + 1] += "-wrong"
    with pytest.raises(AssertionError, match=service):
        _check_identities(services)
