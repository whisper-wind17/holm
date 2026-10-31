"""Assertions and Alembic helpers shared only by TC-DB-01 and TC-DB-02."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol

from alembic.config import Config
from alembic.script import ScriptDirectory
from holm_persistence.migrations import downgrade_to_base as run_downgrade_to_base
from holm_persistence.migrations import upgrade_to_head as run_upgrade_to_head
from sqlalchemy import text
from sqlalchemy.engine import Engine

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
ALEMBIC_CONFIG_PATH: Final[Path] = PROJECT_ROOT / "migrations" / "alembic.ini"


class PostgresFixture(Protocol):
    """The narrow fixture surface used by Migration tests."""

    @property
    def engine(self) -> Engine: ...


@dataclass(frozen=True)
class DatabaseStructure:
    """All non-system Schemas and relations in a fresh task-owned cluster."""

    schemas: tuple[str, ...]
    relations: tuple[tuple[str, str, str], ...]
    alembic_versions: tuple[str, ...]


def head_revision() -> str:
    revision = ScriptDirectory.from_config(Config(ALEMBIC_CONFIG_PATH)).get_current_head()
    if revision is None:
        raise RuntimeError("Alembic has no head revision")
    return revision


def revision_count() -> int:
    """How many revisions one fresh upgrade from base to head applies.

    Derived rather than written down, so adding a Migration does not silently
    turn "one worker did all the work" into a failing literal.
    """
    script = ScriptDirectory.from_config(Config(ALEMBIC_CONFIG_PATH))
    return len(list(script.walk_revisions("base", "head")))


def upgrade_head(engine: Engine) -> None:
    with engine.connect() as connection:
        run_upgrade_to_head(connection, ALEMBIC_CONFIG_PATH)


def downgrade_base(engine: Engine) -> None:
    with engine.connect() as connection:
        run_downgrade_to_base(connection, ALEMBIC_CONFIG_PATH)


def database_structure(engine: Engine) -> DatabaseStructure:
    with engine.connect() as connection:
        schemas = tuple(
            connection.execute(
                text(
                    """
                    SELECT nspname
                    FROM pg_namespace
                    WHERE nspname NOT LIKE 'pg\\_%' ESCAPE '\\'
                      AND nspname <> 'information_schema'
                    ORDER BY nspname
                    """
                )
            ).scalars()
        )
        relations = tuple(
            connection.execute(
                text(
                    """
                    SELECT namespace.nspname, relation.relname, relation.relkind
                    FROM pg_class AS relation
                    JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
                    WHERE namespace.nspname NOT LIKE 'pg\\_%' ESCAPE '\\'
                      AND namespace.nspname <> 'information_schema'
                    ORDER BY namespace.nspname, relation.relname, relation.relkind
                    """
                )
            ).tuples()
        )
        version_table_exists = ("public", "alembic_version", "r") in relations
        versions = (
            tuple(
                connection.execute(
                    text("SELECT version_num FROM public.alembic_version ORDER BY version_num")
                ).scalars()
            )
            if version_table_exists
            else ()
        )

    return DatabaseStructure(
        schemas=schemas,
        relations=relations,
        alembic_versions=versions,
    )


#: Every relation head is expected to hold, in the order the catalog query sorts
#: them. Check constraints and foreign keys are absent on purpose: neither has a
#: ``pg_class`` entry. This list grows with each Migration that adds a table, and
#: TC-DB-01/TC-DB-02 keep their original meaning -- a fresh upgrade produces
#: exactly this, and a downgrade to base removes exactly this.
HEAD_RELATIONS: Final[tuple[tuple[str, str, str], ...]] = (
    ("core", "ai_service_backends", "r"),
    ("core", "ai_services", "r"),
    ("core", "api_key_service_scopes", "r"),
    ("core", "api_keys", "r"),
    ("core", "audit_events", "r"),
    ("core", "bootstrap_tokens", "r"),
    ("core", "component_heartbeats", "r"),
    ("core", "domain_events", "r"),
    ("core", "domain_events_id_seq", "S"),
    ("core", "download_subscriptions", "r"),
    ("core", "download_tasks", "r"),
    ("core", "gpu_allocations", "r"),
    ("core", "idempotency_records", "r"),
    ("core", "ix_ai_service_backends_instance_id", "i"),
    ("core", "ix_api_key_service_scopes_service_id", "i"),
    ("core", "ix_api_keys_user_id", "i"),
    ("core", "ix_audit_events_actor_id_created_at", "i"),
    ("core", "ix_audit_events_created_at", "i"),
    ("core", "ix_audit_events_object_type_object_id_created_at", "i"),
    ("core", "ix_audit_events_request_id", "i"),
    ("core", "ix_domain_events_occurred_at", "i"),
    ("core", "ix_gpu_allocations_instance_id", "i"),
    ("core", "ix_jobs_claim_order", "i"),
    ("core", "ix_login_protection_updated_at", "i"),
    ("core", "ix_model_instance_events_instance_occurred", "i"),
    ("core", "ix_request_records_received_at_desc", "i"),
    ("core", "ix_request_records_service_id_received_at", "i"),
    ("core", "ix_request_records_user_id_received_at", "i"),
    ("core", "ix_sessions_expires_at", "i"),
    ("core", "ix_sessions_user_id", "i"),
    ("core", "ix_user_service_permissions_service_id", "i"),
    ("core", "jobs", "r"),
    ("core", "login_protection", "r"),
    ("core", "model_artifacts", "r"),
    ("core", "model_instance_events", "r"),
    ("core", "model_instances", "r"),
    ("core", "model_sources", "r"),
    ("core", "models", "r"),
    ("core", "pk_ai_service_backends", "i"),
    ("core", "pk_ai_services", "i"),
    ("core", "pk_api_key_service_scopes", "i"),
    ("core", "pk_api_keys", "i"),
    ("core", "pk_audit_events", "i"),
    ("core", "pk_bootstrap_tokens", "i"),
    ("core", "pk_component_heartbeats", "i"),
    ("core", "pk_domain_events", "i"),
    ("core", "pk_download_subscriptions", "i"),
    ("core", "pk_download_tasks", "i"),
    ("core", "pk_gpu_allocations", "i"),
    ("core", "pk_idempotency_records", "i"),
    ("core", "pk_jobs", "i"),
    ("core", "pk_login_protection", "i"),
    ("core", "pk_model_artifacts", "i"),
    ("core", "pk_model_instance_events", "i"),
    ("core", "pk_model_instances", "i"),
    ("core", "pk_model_sources", "i"),
    ("core", "pk_models", "i"),
    ("core", "pk_request_records", "i"),
    ("core", "pk_sessions", "i"),
    ("core", "pk_system_initialization", "i"),
    ("core", "pk_user_service_permissions", "i"),
    ("core", "pk_users", "i"),
    ("core", "request_records", "r"),
    ("core", "sessions", "r"),
    ("core", "system_initialization", "r"),
    ("core", "uq_active_download_identity", "i"),
    ("core", "uq_active_service_backend_instance", "i"),
    ("core", "uq_active_service_backend_key", "i"),
    ("core", "uq_ai_services_active_public_model_name", "i"),
    ("core", "uq_ai_services_name", "i"),
    ("core", "uq_api_keys_lookup_id", "i"),
    ("core", "uq_bootstrap_tokens_token_hash", "i"),
    ("core", "uq_domain_events_aggregate_id_version", "i"),
    ("core", "uq_download_subscription_identity", "i"),
    ("core", "uq_gpu_allocations_active_gpu_uuid", "i"),
    ("core", "uq_idempotency_scope", "i"),
    ("core", "uq_model_artifact_revision", "i"),
    ("core", "uq_model_instances_internal_name", "i"),
    ("core", "uq_model_instances_name", "i"),
    ("core", "uq_model_source_key", "i"),
    ("core", "uq_model_sources_id_type", "i"),
    ("core", "uq_model_sources_type", "i"),
    ("core", "uq_pre_resolution_download_request", "i"),
    ("core", "uq_sessions_session_hash", "i"),
    ("core", "uq_users_username_norm", "i"),
    ("core", "user_service_permissions", "r"),
    ("core", "users", "r"),
    ("public", "alembic_version", "r"),
    ("public", "alembic_version_pkc", "i"),
    ("telemetry", "gateway_service_metric_samples", "r"),
    ("telemetry", "gateway_service_metric_samples_pkey", "i"),
    ("telemetry", "gpu_metrics_1m", "p"),
    ("telemetry", "instance_metrics_1m", "p"),
    ("telemetry", "pk_gpu_metrics_1m", "I"),
    ("telemetry", "pk_instance_metrics_1m", "I"),
    ("telemetry", "pk_service_metrics_1m", "I"),
    ("telemetry", "service_metrics_1m", "p"),
    ("usage", "ix_usage_request_records_received_at_desc", "I"),
    ("usage", "ix_usage_request_records_service_id_received_at", "I"),
    ("usage", "ix_usage_request_records_user_id_received_at", "I"),
    ("usage", "pk_request_event_quarantine", "i"),
    ("usage", "pk_request_id_registry", "i"),
    ("usage", "pk_usage_15m", "i"),
    ("usage", "request_event_quarantine", "r"),
    ("usage", "request_id_registry", "r"),
    ("usage", "request_records", "p"),
    ("usage", "uq_request_records_received_at_request_id", "I"),
    ("usage", "usage_15m", "r"),
)


def assert_head_structure(structure: DatabaseStructure) -> None:
    assert structure.schemas == ("core", "public", "telemetry", "usage")
    assert structure.relations == HEAD_RELATIONS
    assert structure.alembic_versions == (head_revision(),)
