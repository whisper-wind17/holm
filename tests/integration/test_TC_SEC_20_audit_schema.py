"""TC-SEC-20: migrated audit schema matches the frozen domain catalog."""

from __future__ import annotations

import re
from enum import StrEnum
from pathlib import Path
from typing import Protocol, cast

import pytest
import sqlalchemy as sa
from fixtures.source_mutation import load_mutated_module
from holm_domain import AuditAction, AuditActorType, AuditObjectType, AuditResult
from migration_test_support import upgrade_head
from sqlalchemy.engine import Engine

ROOT = Path(__file__).resolve().parents[2]
AUDIT_SOURCE = ROOT / "packages/domain/src/holm_domain/audit.py"


class PostgresFixture(Protocol):
    @property
    def engine(self) -> Engine: ...


def _constraint_values(sqltext: str) -> frozenset[str]:
    return frozenset(re.findall(r"'([^']+)'", sqltext))


def _assert_enumeration_constraint(
    checks: dict[str, str], enumeration: type[StrEnum], constraint_name: str
) -> None:
    expected = frozenset(member.value for member in enumeration)
    assert _constraint_values(checks[constraint_name]) == expected


def test_TC_SEC_20_audit_schema_columns_constraints_and_indexes(
    ephemeral_postgres: PostgresFixture,
) -> None:
    upgrade_head(ephemeral_postgres.engine)
    inspector = sa.inspect(ephemeral_postgres.engine)

    columns = inspector.get_columns("audit_events", schema="core")
    assert [(column["name"], column["nullable"]) for column in columns] == [
        ("id", False),
        ("actor_type", False),
        ("actor_id", True),
        ("action", False),
        ("object_type", False),
        ("object_id", True),
        ("result", False),
        ("request_id", False),
        ("details", False),
        ("created_at", False),
    ]
    assert str(columns[8]["type"]) == "JSONB"
    created_at_type = columns[9]["type"]
    assert isinstance(created_at_type, sa.DateTime)
    assert bool(created_at_type.timezone) is True
    primary_key = inspector.get_pk_constraint("audit_events", schema="core")
    assert primary_key["name"] == "pk_audit_events"
    assert primary_key["constrained_columns"] == ["id"]

    checks: dict[str, str] = {
        cast(str, constraint["name"]): str(constraint["sqltext"])
        for constraint in inspector.get_check_constraints("audit_events", schema="core")
    }
    assert set(checks) == {
        "ck_audit_events_action",
        "ck_audit_events_actor_type",
        "ck_audit_events_details_object",
        "ck_audit_events_object_type",
        "ck_audit_events_result",
        "ck_audit_events_user_actor_has_id",
    }
    for enumeration, constraint_name in (
        (AuditActorType, "ck_audit_events_actor_type"),
        (AuditAction, "ck_audit_events_action"),
        (AuditObjectType, "ck_audit_events_object_type"),
        (AuditResult, "ck_audit_events_result"),
    ):
        _assert_enumeration_constraint(checks, enumeration, constraint_name)

    indexes = {
        index["name"]: tuple(index["column_names"])
        for index in inspector.get_indexes("audit_events", schema="core")
    }
    assert indexes == {
        "ix_audit_events_actor_id_created_at": ("actor_id", "created_at"),
        "ix_audit_events_created_at": ("created_at",),
        "ix_audit_events_object_type_object_id_created_at": (
            "object_type",
            "object_id",
            "created_at",
        ),
        "ix_audit_events_request_id": ("request_id",),
    }
    print("TC_SEC_20 schema: columns=10 checks=6 indexes=4 primary_key=pk_audit_events")


def test_TC_SEC_20_positive_control_unregistered_audit_action_fails_loudly(
    ephemeral_postgres: PostgresFixture,
    tmp_path: Path,
) -> None:
    upgrade_head(ephemeral_postgres.engine)
    checks: dict[str, str] = {
        cast(str, constraint["name"]): str(constraint["sqltext"])
        for constraint in sa.inspect(ephemeral_postgres.engine).get_check_constraints(
            "audit_events", schema="core"
        )
    }
    marker = '    SYSTEM_CONFIG_CHANGE = "system_config.change"'
    mutated = load_mutated_module(
        AUDIT_SOURCE,
        [(marker, marker + '\n    UNREGISTERED = "audit.unregistered"')],
        module_name="_m2_09b_unregistered_audit_action",
        destination=tmp_path / "unregistered_audit_action.py",
    )
    with pytest.raises(AssertionError):
        _assert_enumeration_constraint(
            checks,
            mutated.AuditAction,
            "ck_audit_events_action",
        )
    print("TC-SEC-20 positive control 10: unregistered AuditAction failed exact CHECK match")
