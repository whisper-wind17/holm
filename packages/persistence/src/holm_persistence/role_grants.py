"""Reviewed runtime PostgreSQL grants, keyed by every mapped table.

Letters are SELECT, INSERT, UPDATE and DELETE. An empty decision deliberately
means that no runtime component receives privileges on the table.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

import sqlalchemy as sa

CONTROL: Final = "holm_control"
GATEWAY: Final = "holm_gateway"
DOWNLOAD: Final = "holm_download_worker"
INSTANCE: Final = "holm_instance_controller"
LEDGER: Final = "holm_ledger_writer"

MAINTENANCE: Final = "holm_maintenance_worker"

M7_07_RUNTIME_ROLES: Final[tuple[str, ...]] = (CONTROL, GATEWAY, DOWNLOAD, INSTANCE, LEDGER)
RUNTIME_ROLES: Final[tuple[str, ...]] = (*M7_07_RUNTIME_ROLES, MAINTENANCE)

# These names are literal SQL objects. No user input or identifier interpolation
# enters the grant builder. The metadata coverage guard runs before any grant.
# This revision snapshot must remain stable when later migrations add tables.
# A later migration extends TABLE_GRANTS with a fresh mapping and applies only
# its new entries; replay of the original migration still sees exactly these pre-0027 objects.
M7_07_TABLE_GRANTS: Final[Mapping[str, Mapping[str, str]]] = {
    "core.users": {CONTROL: "SIU", GATEWAY: "S"},
    "core.sessions": {CONTROL: "SIUD"},
    "core.login_protection": {CONTROL: "SIUD"},
    "core.bootstrap_tokens": {CONTROL: "SIU"},
    "core.system_initialization": {CONTROL: "SI"},
    "core.models": {CONTROL: "SIUD"},
    "core.model_sources": {CONTROL: "SIU", DOWNLOAD: "S"},
    "core.model_artifacts": {CONTROL: "S", GATEWAY: "S", DOWNLOAD: "SIU", INSTANCE: "S"},
    "core.download_tasks": {CONTROL: "SIU", DOWNLOAD: "SU"},
    "core.download_subscriptions": {CONTROL: "SID"},
    "core.jobs": {CONTROL: "SI", DOWNLOAD: "SU"},
    "core.component_heartbeats": {CONTROL: "SIU", DOWNLOAD: "SIU", INSTANCE: "SIU", LEDGER: "SIU"},
    "core.domain_events": {CONTROL: "SI", GATEWAY: "S", DOWNLOAD: "SI", INSTANCE: "SI"},
    "core.idempotency_records": {CONTROL: "SIU"},
    "core.model_instances": {CONTROL: "SIUD", GATEWAY: "S", INSTANCE: "SU"},
    "core.gpu_allocations": {CONTROL: "SIU", INSTANCE: "SIU"},
    "core.model_instance_events": {CONTROL: "SI", INSTANCE: "SI"},
    "core.ai_services": {CONTROL: "SIU", GATEWAY: "S"},
    "core.ai_service_backends": {CONTROL: "SIU", GATEWAY: "S", INSTANCE: "S"},
    "core.api_keys": {CONTROL: "SIU", GATEWAY: "S", LEDGER: "S"},
    "core.api_key_service_scopes": {CONTROL: "SI", GATEWAY: "S"},
    "core.user_service_permissions": {CONTROL: "SID", GATEWAY: "S"},
    "core.request_records": {GATEWAY: "I"},
    "core.audit_events": {CONTROL: "SI"},
    "usage.request_id_registry": {LEDGER: "SIU"},
    "usage.request_records": {CONTROL: "S", LEDGER: "SIU"},
    "usage.request_event_quarantine": {LEDGER: "I"},
    "usage.usage_15m": {CONTROL: "S", LEDGER: "SIUD"},
    "telemetry.instance_metrics_1m": {CONTROL: "SIU"},
    "telemetry.service_metrics_1m": {CONTROL: "SIU"},
    "telemetry.gpu_metrics_1m": {CONTROL: "SIU"},
    "telemetry.gateway_service_metric_samples": {CONTROL: "S", GATEWAY: "SIU"},
}

_OPERATIONS: Final = frozenset("SIUD")

# Column grants keep Gateway's snapshot away from unrelated Core fields and
# keep Ledger Writer away from API Key HMAC material. All other SELECT grants
# above refer to complete repository records or aggregate source tables.
M7_07_COLUMN_SELECTS: Final[Mapping[tuple[str, str], tuple[str, ...]]] = {
    ("core.users", GATEWAY): ("id", "status", "role", "deleted_at"),
    ("core.ai_services", GATEWAY): (
        "id",
        "public_model_name",
        "enabled",
        "route_version",
        "deleted_at",
    ),
    ("core.ai_service_backends", GATEWAY): (
        "service_id",
        "backend_key",
        "enabled",
        "retired_at",
        "instance_id",
    ),
    ("core.api_keys", GATEWAY): (
        "id",
        "user_id",
        "lookup_id",
        "pepper_key_id",
        "secret_hmac",
        "status",
        "expires_at",
    ),
    ("core.api_keys", LEDGER): ("id", "created_at"),
    ("core.api_key_service_scopes", GATEWAY): ("key_id", "service_id"),
    ("core.user_service_permissions", GATEWAY): ("user_id", "service_id"),
    ("core.model_instances", GATEWAY): (
        "id",
        "artifact_id",
        "status",
        "runtime_type",
        "internal_endpoint",
        "internal_model_name",
    ),
    ("core.model_artifacts", GATEWAY): (
        "id",
        "storage_type",
        "tokenizer_bundle_path",
        "tokenizer_bundle_hash",
    ),
}

M7_08B_TABLE_GRANTS: Final[Mapping[str, Mapping[str, str]]] = {
    "core.component_heartbeats": {MAINTENANCE: "SIU"},
}

# Grant only the columns needed for model reasoning policy lookup.
FIX_22_COLUMN_SELECTS: Final[Mapping[tuple[str, str], tuple[str, ...]]] = {
    ("core.models", GATEWAY): ("id", "architecture"),
    ("core.model_artifacts", GATEWAY): ("model_id",),
}

TABLE_GRANTS: Final[Mapping[str, Mapping[str, str]]] = {
    **M7_07_TABLE_GRANTS,
    "core.models": {**M7_07_TABLE_GRANTS["core.models"], GATEWAY: "S"},
    # Retain the historical migration snapshot; runtime direct writes are retired.
    "core.request_records": {},
    "core.component_heartbeats": {
        **M7_07_TABLE_GRANTS["core.component_heartbeats"],
        **M7_08B_TABLE_GRANTS["core.component_heartbeats"],
    },
    "core.model_artifacts": {CONTROL: "SD", GATEWAY: "S", DOWNLOAD: "SIUD", INSTANCE: "S"},
    "core.model_instances": {CONTROL: "SIUD", GATEWAY: "S", DOWNLOAD: "S", INSTANCE: "SU"},
    "core.download_tasks": {CONTROL: "SIUD", DOWNLOAD: "SU"},
    "core.jobs": {CONTROL: "SI", DOWNLOAD: "SIU"},
    "core.audit_events": {CONTROL: "SI", DOWNLOAD: "I"},
}
COLUMN_SELECTS: Final[Mapping[tuple[str, str], tuple[str, ...]]] = {
    **M7_07_COLUMN_SELECTS,
    **{
        key: (*M7_07_COLUMN_SELECTS.get(key, ()), *columns)
        for key, columns in FIX_22_COLUMN_SELECTS.items()
    },
    ("core.user_service_permissions", GATEWAY): (
        *M7_07_COLUMN_SELECTS[("core.user_service_permissions", GATEWAY)],
        "max_concurrent_requests",
        "max_requests_per_minute",
    ),
}


def verify_table_grants(metadata: sa.MetaData) -> None:
    """Fail before migration when a mapped table lacks an explicit decision."""

    mapped = set(metadata.tables)
    decided = set(TABLE_GRANTS)
    if mapped != decided:
        raise ValueError(
            f"Table grants differ from metadata: missing={sorted(mapped - decided)}, "
            f"unknown={sorted(decided - mapped)}"
        )
    for table, grants in TABLE_GRANTS.items():
        for role, operations in grants.items():
            if role not in RUNTIME_ROLES or not operations or set(operations) - _OPERATIONS:
                raise ValueError(f"Invalid grant decision for {table}")
    for (table, role), columns in COLUMN_SELECTS.items():
        if "S" not in TABLE_GRANTS.get(table, {}).get(role, ""):
            raise ValueError(f"Column grant has no SELECT decision: {table}/{role}")
        if not columns or set(columns) - set(metadata.tables[table].columns.keys()):
            raise ValueError(f"Invalid SELECT columns: {table}/{role}")


__all__ = [
    "CONTROL",
    "DOWNLOAD",
    "GATEWAY",
    "INSTANCE",
    "LEDGER",
    "MAINTENANCE",
    "M7_07_RUNTIME_ROLES",
    "M7_08B_TABLE_GRANTS",
    "RUNTIME_ROLES",
    "M7_07_COLUMN_SELECTS",
    "M7_07_TABLE_GRANTS",
    "FIX_22_COLUMN_SELECTS",
    "COLUMN_SELECTS",
    "TABLE_GRANTS",
    "verify_table_grants",
]
