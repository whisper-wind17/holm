"""Query-layer authorization primitives shared by protected repositories."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

import sqlalchemy as sa
from holm_domain import AuthorizationSubject, UserRole
from sqlalchemy.sql.elements import ColumnElement


@dataclass(frozen=True, slots=True)
class RepositoryPage[ItemT]:
    """A page whose count and indices are computed after authorization filters."""

    items: tuple[ItemT, ...]
    total: int
    page: int
    page_size: int

    def __post_init__(self) -> None:
        if self.total < 0:
            raise ValueError("total must not be negative")
        if self.page < 1:
            raise ValueError("page must be positive")
        if not 1 <= self.page_size <= 100:
            raise ValueError("page_size must be between 1 and 100")

    @property
    def total_pages(self) -> int:
        if self.total == 0:
            return 0
        return (self.total + self.page_size - 1) // self.page_size


def owned_or_admin_clause(
    subject: AuthorizationSubject,
    owner_id: ColumnElement[UUID],
) -> ColumnElement[bool]:
    """Restrict a protected row to its owner unless the subject is Admin."""
    if subject.role is UserRole.ADMIN:
        return sa.true()
    return owner_id == subject.user_id


def authorized_service_clause(
    subject: AuthorizationSubject,
    service_id: ColumnElement[UUID],
) -> ColumnElement[bool]:
    """Restrict a future service query to the subject's persisted grant set."""
    if subject.role is UserRole.ADMIN:
        return sa.true()
    if not subject.authorized_service_ids:
        return sa.false()
    return service_id.in_(tuple(subject.authorized_service_ids))


__all__ = [
    "RepositoryPage",
    "authorized_service_clause",
    "owned_or_admin_clause",
]
