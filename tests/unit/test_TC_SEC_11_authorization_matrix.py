"""TC-SEC-11: the PDD chapter 4 matrix is executable and singular."""

from __future__ import annotations

from holm_domain import (
    CAPABILITY_MATRIX,
    Capability,
    NavigationId,
    UserRole,
    granted_capabilities,
    navigation_for,
    permission_scope,
)


def test_TC_SEC_11_all_eleven_matrix_rows_drive_both_role_decisions() -> None:
    assert len(CAPABILITY_MATRIX) == len(Capability) == 11
    assert {rule.capability for rule in CAPABILITY_MATRIX} == set(Capability)

    for rule in CAPABILITY_MATRIX:
        for role in UserRole:
            actual_scope = permission_scope(role, rule.capability)
            assert actual_scope is rule.scope_for(role), (rule.capability, role)
            assert (rule.capability in granted_capabilities(role)) is actual_scope.permits_use


def test_TC_SEC_11_navigation_is_server_derived_from_the_same_capability_matrix() -> None:
    admin_navigation = navigation_for(UserRole.ADMIN)
    user_navigation = navigation_for(UserRole.USER)

    assert tuple(item.id for item in admin_navigation) == (
        NavigationId.OVERVIEW,
        NavigationId.AI_SERVICES,
        NavigationId.MODELS,
        NavigationId.USERS,
        NavigationId.USAGE,
        NavigationId.SYSTEM,
    )
    assert tuple(item.id for item in user_navigation) == (
        NavigationId.AI_SERVICES,
        NavigationId.API_KEYS,
        NavigationId.USAGE,
    )
    for role, items in (
        (UserRole.ADMIN, admin_navigation),
        (UserRole.USER, user_navigation),
    ):
        assert all(permission_scope(role, item.required_capability).permits_use for item in items)
