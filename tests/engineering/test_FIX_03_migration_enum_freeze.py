"""FIX-03 guards and real-PostgreSQL checks for frozen Migration enums."""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Final, Protocol

from alembic import command
from alembic.config import Config
from holm_domain import (
    GATEWAY_ERROR_CODES,
    ApiKeyStatus,
    ArtifactStorageType,
    DownloadSubscriberType,
    DownloadTaskStatus,
    GpuAllocationStatus,
    MetadataStatus,
    MeteringSource,
    MetricsCapability,
    ModelArtifactStatus,
    ModelInstanceStatus,
    ModelOriginType,
    ModelSourceType,
    ModelSourceValidationStatus,
    RequestFinishReason,
    RequestRecordState,
    RequestRecordStatus,
    RuntimeType,
)
from holm_ledger import EventType, RequestOperation, RequestProtocol
from integration.conftest import EphemeralPostgres
from integration.conftest import ephemeral_postgres as ephemeral_postgres  # noqa: F401
from integration.migration_test_support import ALEMBIC_CONFIG_PATH
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

ROOT: Final[Path] = Path(__file__).resolve().parents[2]
MIGRATIONS_DIR: Final[Path] = ROOT / "migrations" / "versions"
ALIGN_REVISION: Final[str] = "20260921_0023"
PREVIOUS_REVISION: Final[str] = "20260921_0022"
RUNTIME_REVISION: Final[str] = "20260915_0012"
_QUOTED_TEXT_VALUE: Final[re.Pattern[str]] = re.compile(r"'((?:''|[^'])*)'::text")


class _StringValue(Protocol):
    value: str


type ConstraintKey = tuple[str, str, str]


def _values(items: Iterable[_StringValue]) -> frozenset[str]:
    return frozenset(item.value for item in items)


ENUM_CONTRACTS: Final[dict[ConstraintKey, frozenset[str]]] = {
    ("core", "model_artifacts", "ck_model_artifacts_storage_type"): _values(ArtifactStorageType),
    ("core", "model_artifacts", "ck_model_artifacts_status"): _values(ModelArtifactStatus),
    ("core", "download_tasks", "ck_download_tasks_status"): _values(DownloadTaskStatus),
    (
        "core",
        "download_subscriptions",
        "ck_download_subscriptions_subscriber_type",
    ): _values(DownloadSubscriberType),
    ("core", "model_sources", "ck_model_sources_type"): _values(ModelSourceType),
    (
        "core",
        "model_sources",
        "ck_model_sources_verification_status",
    ): _values(ModelSourceValidationStatus),
    ("core", "models", "ck_models_source_type"): _values(ModelOriginType),
    ("core", "models", "ck_models_metadata_status"): _values(MetadataStatus),
    ("core", "model_instances", "ck_model_instances_runtime_type"): _values(RuntimeType),
    ("core", "model_instances", "ck_model_instances_status"): _values(ModelInstanceStatus),
    ("core", "gpu_allocations", "ck_gpu_allocations_status"): _values(GpuAllocationStatus),
    ("core", "api_keys", "ck_api_keys_status"): _values(ApiKeyStatus),
    ("core", "request_records", "ck_request_records_status"): _values(RequestRecordStatus),
    ("core", "request_records", "ck_request_records_error_code"): _values(GATEWAY_ERROR_CODES),
    ("core", "request_records", "ck_request_records_finish_reason"): _values(RequestFinishReason),
    ("usage", "request_records", "ck_request_records_record_state"): _values(RequestRecordState),
    ("usage", "request_records", "ck_request_records_protocol"): _values(RequestProtocol),
    ("usage", "request_records", "ck_request_records_operation"): _values(RequestOperation),
    ("usage", "request_records", "ck_request_records_finish_reason"): _values(RequestFinishReason),
    ("usage", "request_records", "ck_request_records_error_code"): _values(GATEWAY_ERROR_CODES),
    (
        "usage",
        "request_records",
        "ck_request_records_last_confirmed_event_type",
    ): _values(EventType),
    (
        "usage",
        "request_event_quarantine",
        "ck_request_event_quarantine_event_type",
    ): _values(EventType),
    ("core", "request_records", "ck_request_records_metering_source"): _values(MeteringSource),
    ("core", "request_records", "ck_request_records_metrics_capability"): _values(
        MetricsCapability
    ),
    ("usage", "request_records", "ck_request_records_metering_source"): _values(MeteringSource),
    ("usage", "request_records", "ck_request_records_metrics_capability"): _values(
        MetricsCapability
    ),
}


# FIX-03 asserts a historical revision, whose vocabulary must not grow when
# a later Migration adds a protocol. M4-19 separately checks the current head.
ALIGN_ENUM_CONTRACTS: Final[dict[ConstraintKey, frozenset[str]]] = {
    **ENUM_CONTRACTS,
    ("usage", "request_records", "ck_request_records_protocol"): frozenset({"openai"}),
    ("usage", "request_records", "ck_request_records_operation"): frozenset({"chat.completions"}),
}


def _domain_imports(source: str, *, filename: str) -> tuple[str, ...]:
    tree = ast.parse(source, filename=filename)
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module is not None:
            if node.module == "holm_domain" or node.module.startswith("holm_domain."):
                imports.append(f"line {node.lineno}: from {node.module}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "holm_domain" or alias.name.startswith("holm_domain."):
                    imports.append(f"line {node.lineno}: import {alias.name}")
    return tuple(imports)


def _config(connection: Connection) -> Config:
    return Config(ALEMBIC_CONFIG_PATH, attributes={"connection": connection})


def _migrate(engine: Engine, revision: str) -> None:
    with engine.connect() as connection:
        command.upgrade(_config(connection), revision)


def _downgrade(engine: Engine, revision: str) -> None:
    with engine.connect() as connection:
        command.downgrade(_config(connection), revision)


def _constraint_definitions(engine: Engine) -> dict[ConstraintKey, str]:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                """
                SELECT
                    namespace.nspname,
                    relation.relname,
                    constraint_record.conname,
                    pg_get_constraintdef(constraint_record.oid, true)
                FROM pg_constraint AS constraint_record
                JOIN pg_class AS relation ON relation.oid = constraint_record.conrelid
                JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
                WHERE constraint_record.contype = 'c'
                  AND namespace.nspname IN ('core', 'usage')
                ORDER BY namespace.nspname, relation.relname, constraint_record.conname
                """
            )
        ).tuples()
    definitions: dict[ConstraintKey, str] = {}
    for schema, table, name, definition in rows:
        key = (str(schema), str(table), str(name))
        if key in ENUM_CONTRACTS:
            definitions[key] = str(definition)
    return definitions


def _constraint_values(engine: Engine) -> dict[ConstraintKey, frozenset[str]]:
    return {
        key: frozenset(value.replace("''", "'") for value in _QUOTED_TEXT_VALUE.findall(sql))
        for key, sql in _constraint_definitions(engine).items()
    }


def test_FIX_03_migrations_do_not_import_live_domain_enums() -> None:
    migration_files = tuple(sorted(MIGRATIONS_DIR.glob("*.py")))
    assert migration_files, "Migration discovery returned no files"
    violations = {
        path.name: imports
        for path in migration_files
        if (imports := _domain_imports(path.read_text(encoding="utf-8"), filename=str(path)))
    }
    assert violations == {}


def test_FIX_03_guard_rejects_a_live_domain_import() -> None:
    mutant = "from holm_domain import RuntimeType\n"
    assert _domain_imports(mutant, filename="mutant_migration.py") == ("line 1: from holm_domain",)


def test_FIX_03_TC_DB_11_alignment_upgrade_downgrade_upgrade_is_exact(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    engine = ephemeral_postgres.engine
    _downgrade(engine, "base")
    _migrate(engine, PREVIOUS_REVISION)
    before = _constraint_definitions(engine)
    assert set(before) == set(ENUM_CONTRACTS)

    _migrate(engine, ALIGN_REVISION)
    first_upgrade = _constraint_definitions(engine)
    assert _constraint_values(engine) == ALIGN_ENUM_CONTRACTS

    _downgrade(engine, PREVIOUS_REVISION)
    assert _constraint_definitions(engine) == first_upgrade == before

    _migrate(engine, ALIGN_REVISION)
    assert _constraint_definitions(engine) == first_upgrade
    assert _constraint_values(engine) == ALIGN_ENUM_CONTRACTS


def test_FIX_03_old_runtime_constraint_is_repaired_for_sglang(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    engine = ephemeral_postgres.engine
    _downgrade(engine, "base")
    _migrate(engine, RUNTIME_REVISION)
    with engine.begin() as connection:
        connection.execute(
            text("ALTER TABLE core.model_instances DROP CONSTRAINT ck_model_instances_runtime_type")
        )
        connection.execute(
            text(
                "ALTER TABLE core.model_instances "
                "ADD CONSTRAINT ck_model_instances_runtime_type "
                "CHECK (runtime_type IN ('vllm'))"
            )
        )

    _migrate(engine, ALIGN_REVISION)
    runtime_key = ("core", "model_instances", "ck_model_instances_runtime_type")
    assert _constraint_values(engine)[runtime_key] == _values(RuntimeType)
    assert "sglang" in _constraint_values(engine)[runtime_key]


def test_M4_19_TC_GW_46_current_head_enums_match_live_contract(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    engine = ephemeral_postgres.engine
    _migrate(engine, "head")
    assert _constraint_values(engine) == ENUM_CONTRACTS
