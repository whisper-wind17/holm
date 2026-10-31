"""TC-SEC-20: audit classifications and default-deny detail construction."""

from __future__ import annotations

from uuid import UUID

import pytest
from holm_domain import AuditAction, AuditActorType, AuditObjectType, AuditResult
from holm_persistence import AuditEventDraft
from holm_security import AUDIT_DETAIL_ALLOWLIST, build_audit_details


def test_TC_SEC_20_every_action_has_one_allowlist_and_enums_are_the_only_catalog() -> None:
    assert set(AUDIT_DETAIL_ALLOWLIST) == {action.value for action in AuditAction}
    assert len({action.value for action in AuditAction}) == len(AuditAction)
    assert len({object_type.value for object_type in AuditObjectType}) == len(AuditObjectType)
    assert len({result.value for result in AuditResult}) == len(AuditResult)


def test_TC_SEC_20_unknown_detail_field_is_dropped_positive_control() -> None:
    details = build_audit_details(
        AuditAction.LOGIN.value,
        {
            "reason_code": "invalid_credentials",
            "future_unreviewed_field": "PWSSNTL-PASSWD-TCSEC20-ALLOWLIST",
        },
    )

    assert details == {"reason_code": "invalid_credentials"}
    print(f"TC_SEC_20 allowlist positive control: {details!r}")


def test_TC_SEC_20_future_action_starts_with_no_implicit_detail_fields() -> None:
    details = build_audit_details(
        AuditAction.API_KEY_CHANGE.value,
        {"next_milestone_guess": "must-not-cross-the-boundary"},
    )
    assert details == {}


def test_TC_SEC_20_persistence_draft_rejects_a_plain_mapping_that_bypasses_builder() -> None:
    with pytest.raises(
        ValueError,
        match="audit details must come from the default-deny builder",
    ):
        AuditEventDraft(
            actor_type=AuditActorType.SYSTEM,
            actor_id=None,
            action=AuditAction.SYSTEM_CONFIG_CHANGE,
            object_type=AuditObjectType.SYSTEM_CONFIG,
            object_id=None,
            result=AuditResult.SUCCEEDED,
            request_id=UUID("00000000-0000-7000-8000-000000000020"),
            details={"future_unreviewed_field": "must-not-persist"},
        )
