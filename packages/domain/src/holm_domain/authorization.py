"""Framework-independent account roles and capability policy.

The tuple in this module is the single executable transcription of the eleven
capability rows. HTTP authorization, ``/api/v1/me`` and navigation all
derive from it; none of those consumers carries a second role matrix.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final
from uuid import UUID


class UserRole(StrEnum):
    """The two supported local-account roles."""

    ADMIN = "admin"
    USER = "user"


class UserStatus(StrEnum):
    """Local-account states; ``DELETED`` is logical and irreversible."""

    ACTIVE = "active"
    DISABLED = "disabled"
    DELETED = "deleted"


class Capability(StrEnum):
    """The eleven capabilities used by the authorization matrix."""

    VIEW_INFRASTRUCTURE = "view_infrastructure"
    MANAGE_LOCAL_MODELS = "manage_local_models"
    MANAGE_MODEL_INSTANCES = "manage_model_instances"
    MANAGE_AI_SERVICES = "manage_ai_services"
    VIEW_AI_SERVICES = "view_ai_services"
    MANAGE_USERS = "manage_users"
    MANAGE_API_KEYS = "manage_api_keys"
    VIEW_API_KEY_PLAINTEXT = "view_api_key_plaintext"
    VIEW_USAGE = "view_usage"
    VIEW_REQUEST_RECORDS = "view_request_records"
    VIEW_SYSTEM = "view_system"


class PermissionScope(StrEnum):
    """Stable machine-readable authorization scopes."""

    ALL = "all"
    ALLOWED = "allowed"
    AUTHORIZED = "authorized"
    OWN = "own"
    OWN_KEYS = "own_keys"
    CREATION_ONLY = "creation_only"
    CALL_METADATA = "call_metadata"
    HIDDEN = "hidden"
    DENIED = "denied"
    NOT_PROVIDED = "not_provided"

    @property
    def permits_use(self) -> bool:
        """Whether this matrix cell grants any form of the capability."""
        return self not in {
            PermissionScope.HIDDEN,
            PermissionScope.DENIED,
            PermissionScope.NOT_PROVIDED,
        }


@dataclass(frozen=True, slots=True)
class CapabilityRule:
    """One row of the role matrix."""

    capability: Capability
    administrator: PermissionScope
    user: PermissionScope

    def scope_for(self, role: UserRole) -> PermissionScope:
        return self.administrator if role is UserRole.ADMIN else self.user


# Do not duplicate this mapping in an endpoint, dependency or test.  Tests
# iterate these rows and exercise both roles through the public helpers below.
CAPABILITY_MATRIX: Final[tuple[CapabilityRule, ...]] = (
    CapabilityRule(Capability.VIEW_INFRASTRUCTURE, PermissionScope.ALL, PermissionScope.HIDDEN),
    CapabilityRule(Capability.MANAGE_LOCAL_MODELS, PermissionScope.ALL, PermissionScope.HIDDEN),
    CapabilityRule(
        Capability.MANAGE_MODEL_INSTANCES,
        PermissionScope.ALLOWED,
        PermissionScope.DENIED,
    ),
    CapabilityRule(
        Capability.MANAGE_AI_SERVICES,
        PermissionScope.ALLOWED,
        PermissionScope.DENIED,
    ),
    CapabilityRule(
        Capability.VIEW_AI_SERVICES,
        PermissionScope.ALL,
        PermissionScope.AUTHORIZED,
    ),
    CapabilityRule(Capability.MANAGE_USERS, PermissionScope.ALLOWED, PermissionScope.DENIED),
    CapabilityRule(Capability.MANAGE_API_KEYS, PermissionScope.OWN_KEYS, PermissionScope.OWN_KEYS),
    CapabilityRule(
        Capability.VIEW_API_KEY_PLAINTEXT,
        PermissionScope.CREATION_ONLY,
        PermissionScope.CREATION_ONLY,
    ),
    CapabilityRule(Capability.VIEW_USAGE, PermissionScope.ALL, PermissionScope.OWN),
    CapabilityRule(
        Capability.VIEW_REQUEST_RECORDS,
        PermissionScope.CALL_METADATA,
        PermissionScope.NOT_PROVIDED,
    ),
    CapabilityRule(Capability.VIEW_SYSTEM, PermissionScope.ALLOWED, PermissionScope.DENIED),
)

_RULE_BY_CAPABILITY: Final[dict[Capability, CapabilityRule]] = {
    rule.capability: rule for rule in CAPABILITY_MATRIX
}


def permission_scope(role: UserRole, capability: Capability) -> PermissionScope:
    """Return the authoritative matrix cell for one role and capability."""
    return _RULE_BY_CAPABILITY[Capability(capability)].scope_for(UserRole(role))


def granted_capabilities(role: UserRole) -> tuple[Capability, ...]:
    """Return permitted capabilities in capability declaration order."""
    normalized_role = UserRole(role)
    return tuple(
        rule.capability for rule in CAPABILITY_MATRIX if rule.scope_for(normalized_role).permits_use
    )


@dataclass(frozen=True, slots=True)
class AuthorizationSubject:
    """Identity and grants that every protected Repository query must receive.

    Service grants remain empty until service-permission rows exist.
    The set fixes the query boundary without inventing absent permissions.
    """

    user_id: UUID
    role: UserRole
    authorized_service_ids: frozenset[UUID] = field(default_factory=frozenset)

    def has(self, capability: Capability) -> bool:
        return permission_scope(self.role, capability).permits_use


class NavigationId(StrEnum):
    OVERVIEW = "overview"
    AI_SERVICES = "ai_services"
    MODELS = "models"
    USERS = "users"
    USAGE = "usage"
    SYSTEM = "system"
    API_KEYS = "api_keys"


@dataclass(frozen=True, slots=True)
class NavigationItem:
    """One server-derived first-level navigation item."""

    id: NavigationId
    label: str
    required_capability: Capability


_ADMIN_NAVIGATION: Final[tuple[NavigationItem, ...]] = (
    NavigationItem(NavigationId.OVERVIEW, "总览", Capability.VIEW_INFRASTRUCTURE),
    NavigationItem(NavigationId.AI_SERVICES, "AI 服务", Capability.VIEW_AI_SERVICES),
    NavigationItem(NavigationId.MODELS, "模型", Capability.MANAGE_LOCAL_MODELS),
    NavigationItem(NavigationId.USERS, "用户", Capability.MANAGE_USERS),
    NavigationItem(NavigationId.USAGE, "用量", Capability.VIEW_USAGE),
    NavigationItem(NavigationId.SYSTEM, "系统", Capability.VIEW_SYSTEM),
)

_USER_NAVIGATION: Final[tuple[NavigationItem, ...]] = (
    NavigationItem(NavigationId.AI_SERVICES, "AI 服务", Capability.VIEW_AI_SERVICES),
    NavigationItem(NavigationId.API_KEYS, "API 密钥", Capability.MANAGE_API_KEYS),
    NavigationItem(NavigationId.USAGE, "用量", Capability.VIEW_USAGE),
)


def navigation_for(role: UserRole) -> tuple[NavigationItem, ...]:
    """Derive visible navigation and re-check every item against the matrix."""
    normalized_role = UserRole(role)
    candidates = _ADMIN_NAVIGATION if normalized_role is UserRole.ADMIN else _USER_NAVIGATION
    return tuple(
        item
        for item in candidates
        if permission_scope(normalized_role, item.required_capability).permits_use
    )


__all__ = [
    "CAPABILITY_MATRIX",
    "AuthorizationSubject",
    "Capability",
    "CapabilityRule",
    "NavigationId",
    "NavigationItem",
    "PermissionScope",
    "UserRole",
    "UserStatus",
    "granted_capabilities",
    "navigation_for",
    "permission_scope",
]
