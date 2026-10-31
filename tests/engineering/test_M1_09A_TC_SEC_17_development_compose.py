"""M1-09A / TC-SEC-17 fail-closed development Compose checks."""

from __future__ import annotations

import copy
import importlib.util
import json
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load_compose_validator() -> ModuleType:
    script_path = PROJECT_ROOT / "scripts" / "verify_development_compose.py"
    spec = importlib.util.spec_from_file_location("m1_09a_verify_development_compose", script_path)
    if spec is None or spec.loader is None:
        raise AssertionError("could not load scripts/verify_development_compose.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_COMPOSE_VALIDATOR = _load_compose_validator()
COMPOSE_PATH = _COMPOSE_VALIDATOR.COMPOSE_PATH
ComposeValidationError = _COMPOSE_VALIDATOR.ComposeValidationError
validate_compose_file = _COMPOSE_VALIDATOR.validate_compose_file
validate_compose_text = _COMPOSE_VALIDATOR.validate_compose_text


def _document() -> dict[str, object]:
    text = COMPOSE_PATH.read_text(encoding="utf-8")
    payload = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    parsed = json.loads(payload)
    assert isinstance(parsed, dict)
    return parsed


def _mutated(mutation: Callable[[dict[str, object]], None]) -> str:
    original = COMPOSE_PATH.read_text(encoding="utf-8")
    comments = "\n".join(line for line in original.splitlines() if line.lstrip().startswith("#"))
    document = copy.deepcopy(_document())
    mutation(document)
    return f"{comments}\n{json.dumps(document)}"


def _service(document: dict[str, object], name: str) -> dict[str, object]:
    services = document["services"]
    assert isinstance(services, dict)
    service = services[name]
    assert isinstance(service, dict)
    return service


def test_M1_09A_TC_SEC_17_compose_scans_the_exact_nonempty_service_set(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert validate_compose_file() == ("control-api", "migrate", "postgres")

    assert _COMPOSE_VALIDATOR.main() == 0
    assert capsys.readouterr().out.strip() == (
        "validated 3 services: control-api, migrate, postgres"
    )


@pytest.mark.parametrize(
    "mutation",
    (
        lambda document: _service(document, "postgres")["volumes"].append(
            {
                "type": "bind",
                "source": "/var/run/docker.sock",
                "target": "/var/run/docker.sock",
            }
        ),
        lambda document: _service(document, "postgres")["volumes"].append(
            {
                "type": "bind",
                "source": "/run/holm",
                "target": "/run/holm",
                "read_only": True,
            }
        ),
        lambda document: _service(document, "control-api")["volumes"][-1].update(
            {
                "source": "/run/holm/agent.sock",
                "target": "/run/holm/agent.sock",
            }
        ),
        lambda document: _service(document, "migrate").pop("cap_drop"),
        lambda document: _service(document, "control-api").update(
            {"image": "ghcr.io/astral-sh/uv:latest"}
        ),
        lambda document: _service(document, "postgres")["environment"].update(
            {"POSTGRES_PASSWORD": "must-not-exist"}
        ),
        lambda document: _service(document, "postgres").update({"ports": ["127.0.0.1:5432:5432"]}),
        lambda document: _service(document, "control-api").update({"ports": ["8000:8000"]}),
    ),
)
def test_M1_09A_TC_SEC_17_each_required_compose_mutation_fails_loudly(
    mutation: Callable[[dict[str, object]], None],
) -> None:
    with pytest.raises(ComposeValidationError):
        validate_compose_text(_mutated(mutation))


def test_M1_09A_TC_SEC_17_compose_empty_gate_controls_fail_loudly(tmp_path: Path) -> None:
    with pytest.raises(ComposeValidationError, match="missing"):
        validate_compose_file(tmp_path / "missing.yaml")
    with pytest.raises(ComposeValidationError, match="parsed"):
        validate_compose_text("not valid JSON/YAML for this checked profile")
    with pytest.raises(ComposeValidationError, match="parsed"):
        validate_compose_text("name: holm\nservices:\n  postgres: {}")
    with pytest.raises(ComposeValidationError, match="must not be empty"):
        validate_compose_text('{"name":"holm","services":{}}')


def test_M1_09A_TC_SEC_17_extra_writable_tmpfs_are_documented_and_tmp_remains_required() -> None:
    text = COMPOSE_PATH.read_text(encoding="utf-8")
    assert validate_compose_text(text) == ("control-api", "migrate", "postgres")

    without_comment = text.replace(
        "# writable-tmpfs postgres /var/run/postgresql: PostgreSQL Unix socket and lock files\n",
        "",
        1,
    )
    with pytest.raises(ComposeValidationError, match="document the purpose"):
        validate_compose_text(without_comment)

    def remove_postgres_tmp(document: dict[str, object]) -> None:
        postgres = _service(document, "postgres")
        tmpfs = postgres["tmpfs"]
        assert isinstance(tmpfs, list)
        postgres["tmpfs"] = [item for item in tmpfs if not str(item).startswith("/tmp:")]

    with pytest.raises(ComposeValidationError, match="independent /tmp"):
        validate_compose_text(_mutated(remove_postgres_tmp))
