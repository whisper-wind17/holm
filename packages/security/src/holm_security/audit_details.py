"""Default-deny construction of security-audit detail objects.

This module does not remove suspicious names or redact a completed object.
Instead, it starts with an empty result and copies only fields explicitly
approved for the selected action.  Request bodies and headers therefore have
no implicit path into persisted audit details.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from types import MappingProxyType
from typing import Final

type AuditDetailScalar = str | int | bool | None
type AuditDetailValue = AuditDetailScalar | list[str]

_MAX_TEXT_LENGTH: Final[int] = 256
_MAX_LIST_ITEMS: Final[int] = 32

# Keys are AuditAction values.  Keeping strings here avoids reversing the
# packages -> domain dependency direction merely for a pure security primitive.
# A cross-package test proves that every AuditAction has exactly one entry.
AUDIT_DETAIL_ALLOWLIST: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {
        "auth.login": frozenset({"reason_code"}),
        "auth.logout": frozenset({"reason_code"}),
        "account.password_change": frozenset({"changed_fields", "reason_code"}),
        "account.change": frozenset({"changed_fields", "reason_code", "operation"}),
        "permission.change": frozenset(
            {
                "changed_fields",
                "reason_code",
                "operation",
                "added_service_ids",
                "removed_service_ids",
                "limits_before",
                "limits_after",
            }
        ),
        "permission.authorization_check": frozenset(
            {"capability", "reason_code", "route_template"}
        ),
        # Future producers begin with no detail fields.  A later milestone must
        # explicitly approve a field before that field can be persisted.
        "api_key.change": frozenset({"operation"}),
        "model_source.credential_change": frozenset(
            {
                "old_key_id",
                "operation",
                "reason_code",
                "reencrypted_rows",
                "referenced_rows",
                "scanned_rows",
                "source_type",
                "validation_status",
                "writable_key_id",
            }
        ),
        "download.change": frozenset({"operation", "reason_code", "source_type"}),
        "model_artifact.delete": frozenset(),
        "model_instance.lifecycle": frozenset(),
        "ai_service.change": frozenset(),
        "system_config.change": frozenset({"changed_fields", "reason_code", "source"}),
    }
)


class AuditDetailError(ValueError):
    """An approved detail field had an unsafe shape, without echoing its value."""


class AuditDetails(Mapping[str, AuditDetailValue]):
    """Read-only output that proves details passed through the Allowlist builder."""

    __slots__ = ("_values",)

    audit_allowlisted: Final[bool] = True

    def __init__(self, values: Mapping[str, AuditDetailValue]) -> None:
        self._values = MappingProxyType(dict(values))

    def __getitem__(self, key: str) -> AuditDetailValue:
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __repr__(self) -> str:
        return repr(dict(self._values))

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Mapping):
            return dict(self._values) == dict(other)
        return NotImplemented


def _safe_value(field: str, value: object) -> AuditDetailValue:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        if len(value) > _MAX_TEXT_LENGTH:
            raise AuditDetailError(f"audit detail field is too long: {field}")
        return value
    if isinstance(value, (list, tuple)):
        if len(value) > _MAX_LIST_ITEMS or not all(isinstance(item, str) for item in value):
            raise AuditDetailError(f"audit detail list has an unsafe shape: {field}")
        copied = list(value)
        if any(len(item) > _MAX_TEXT_LENGTH for item in copied):
            raise AuditDetailError(f"audit detail list item is too long: {field}")
        return copied
    raise AuditDetailError(f"audit detail field has an unsafe type: {field}")


def build_audit_details(
    action: str,
    candidates: Mapping[str, object],
) -> AuditDetails:
    """Construct details by copying approved fields into a new empty object.

    An unknown action is rejected rather than treated as having a permissive
    default.  Unknown candidate fields are silently dropped so their names and
    values do not appear in a leak-prone error path.
    """
    try:
        allowed_fields = AUDIT_DETAIL_ALLOWLIST[action]
    except KeyError:
        raise AuditDetailError("audit action has no detail allowlist") from None
    return AuditDetails(
        {
            field: _safe_value(field, candidates[field])
            for field in sorted(allowed_fields)
            if field in candidates
        }
    )


__all__ = [
    "AUDIT_DETAIL_ALLOWLIST",
    "AuditDetailError",
    "AuditDetailScalar",
    "AuditDetailValue",
    "AuditDetails",
    "build_audit_details",
]
