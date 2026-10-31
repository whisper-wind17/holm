"""M7-01B production entrypoint and seven-service subset contract checks."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import cast

import psycopg
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PATH = PROJECT_ROOT / "deploy" / "compose" / "compose.prod.yaml"


def _load_validator() -> ModuleType:
    path = PROJECT_ROOT / "scripts" / "verify_production_compose.py"
    spec = importlib.util.spec_from_file_location("m7_01b_verify_production", path)
    if spec is None or spec.loader is None:
        raise AssertionError("could not load scripts/verify_production_compose.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


VALIDATOR = _load_validator()


def _load_healthcheck() -> ModuleType:
    path = PROJECT_ROOT / "deploy" / "images" / "holm_healthcheck.py"
    spec = importlib.util.spec_from_file_location("m7_01b_healthcheck", path)
    if spec is None or spec.loader is None:
        raise AssertionError("could not load deploy/images/holm_healthcheck.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


HEALTHCHECK = _load_healthcheck()


def _heartbeat_args(database_url_file: Path) -> list[str]:
    return [
        "heartbeat",
        "--database-url-file",
        str(database_url_file),
        "--component",
        "ledger-writer",
        "--instance-id",
        "ledger-writer",
    ]


def test_M7_01B_heartbeat_converts_sqlalchemy_scheme_for_psycopg(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url_file = tmp_path / "database-url"
    database_url_file.write_text("postgresql+psycopg://u:secret@db:5432/x", encoding="utf-8")
    received: list[str] = []

    class Cursor:
        def __enter__(self) -> Cursor:
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def execute(self, query: str, params: tuple[str, str]) -> None:
            assert params == ("ledger-writer", "ledger-writer")

        def fetchone(self) -> tuple[str, int]:
            return ("Healthy", 0)

    class Connection:
        def __enter__(self) -> Connection:
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def cursor(self) -> Cursor:
            return Cursor()

    def connect(database_url: str, *, connect_timeout: int) -> Connection:
        received.append(database_url)
        assert connect_timeout == 2
        return Connection()

    monkeypatch.setattr(psycopg, "connect", connect)
    assert HEALTHCHECK.main(_heartbeat_args(database_url_file)) == 0
    assert received == ["postgresql://u:secret@db:5432/x"]


def test_M7_01B_heartbeat_connection_failure_sanitizes_password(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_url_file = tmp_path / "database-url"
    database_url_file.write_text("postgresql+psycopg://u:secret@db:5432/x", encoding="utf-8")

    def connect(*args: object, **kwargs: object) -> None:
        raise RuntimeError("connection failed: postgresql://u:secret@db:5432/x")

    monkeypatch.setattr(psycopg, "connect", connect)
    assert HEALTHCHECK.main(_heartbeat_args(database_url_file)) == 1
    assert capsys.readouterr().err == "container health check failed\n"


def test_M7_01B_heartbeat_rejects_non_postgresql_scheme(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_url_file = tmp_path / "database-url"
    database_url_file.write_text("mysql://u:secret@db:5432/x", encoding="utf-8")

    def connect(*args: object, **kwargs: object) -> None:
        raise AssertionError("unsupported scheme reached psycopg")

    monkeypatch.setattr(psycopg, "connect", connect)
    assert HEALTHCHECK.main(_heartbeat_args(database_url_file)) == 1
    assert capsys.readouterr().err == "container health check failed\n"


def _document() -> dict[str, object]:
    payload = "\n".join(
        line
        for line in COMPOSE_PATH.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )
    parsed = json.loads(payload)
    assert isinstance(parsed, dict)
    return cast(dict[str, object], parsed)


def _service(document: dict[str, object], name: str) -> dict[str, object]:
    services = document["services"]
    assert isinstance(services, dict)
    service = services[name]
    assert isinstance(service, dict)
    return cast(dict[str, object], service)


def test_M7_01B_control_api_runs_module_with_file_only_secret_arguments() -> None:
    document = _document()
    control = _service(document, "control-api")
    command = control["command"]
    secrets = control["secrets"]
    assert isinstance(command, list)
    assert isinstance(secrets, list)

    assert command[:3] == ["python", "-m", "holm_control_api"]
    assert {
        "--database-url-file",
        "--master-keyring-file",
        "--api-key-pepper-keyring-file",
        "--idempotency-hmac-secret-file",
    }.issubset(command)
    assert set(secrets) == {
        "control_database_url",
        "master_keyring",
        "api_key_pepper_keyring",
        "idempotency_hmac_secret",
    }
    assert "environment" not in control


def test_M7_01B_cached_postgres_variant_uses_its_fixed_nonroot_identity() -> None:
    postgres = _service(_document(), "postgres")

    assert postgres["image"] == "holm/postgres:16.15-alpine"
    assert postgres["user"] == "70:70"
    assert postgres["read_only"] is True
    assert postgres["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in postgres["security_opt"]


def test_M7_01B_application_bases_use_explicit_reachable_registry_versions() -> None:
    expected = {
        "application.Dockerfile": (
            "FROM ${HOLM_BASE_REGISTRY}/library/python:3.12.11-slim-bookworm@sha256:"
            "519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7"
        ),
        "edge.Dockerfile": (
            "FROM ${HOLM_BASE_REGISTRY}/library/caddy:2.10.2-alpine@sha256:"
            "4c6e91c6ed0e2fa03efd5b44747b625fec79bc9cd06ac5235a779726618e530d"
        ),
    }
    for filename, from_line in expected.items():
        text = (PROJECT_ROOT / "deploy" / "images" / filename).read_text(encoding="utf-8")
        assert from_line in text
        assert ":latest" not in text.lower()


def test_M7_01B_application_installs_libpq_and_checks_entrypoint_imports() -> None:
    application = (PROJECT_ROOT / "deploy/images/application.Dockerfile").read_text(
        encoding="utf-8"
    )
    assert "apt-get install -y --no-install-recommends libpq5" in application
    assert "rm -rf /var/lib/apt/lists/*" in application
    wheel_install = application.index("RUN python -m pip install")
    import_check = application.index('RUN python -c "import importlib, psycopg, psycopg.pq;')
    assert wheel_install < import_check
    assert "importlib.import_module(name)" in application[import_check:]
    for module in (
        "holm_control_api.__main__",
        "holm_gateway.__main__",
        "holm_download_worker.__main__",
        "holm_instance_controller.__main__",
        "holm_ledger_writer.__main__",
        "holm_maintenance_worker.__main__",
        "holm_host_agent.__main__",
    ):
        assert module in application[import_check:]
    assert "assert psycopg.pq.__impl__ in ('python', 'c', 'binary')" in application[import_check:]


def test_M7_01B_dockerfile_syntax_directive_guard_fails_loudly() -> None:
    application = (PROJECT_ROOT / "deploy/images/application.Dockerfile").read_text(
        encoding="utf-8"
    )
    edge = (PROJECT_ROOT / "deploy/images/edge.Dockerfile").read_text(encoding="utf-8")

    VALIDATOR.validate_dockerfile_texts(application, edge)
    with pytest.raises(VALIDATOR.ProductionComposeValidationError, match=r"must not use # syntax="):
        VALIDATOR.validate_dockerfile_texts("# syntax=docker/dockerfile:1\n\n" + application, edge)


def test_M7_01B_repository_ignores_local_secret_directories() -> None:
    ignore_lines = {
        line.strip()
        for line in (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    }
    assert "secrets/" in ignore_lines
