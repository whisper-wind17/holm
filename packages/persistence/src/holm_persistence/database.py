"""Async PostgreSQL engine, session, and role-specific configuration construction."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

DEFAULT_CONNECT_TIMEOUT_SECONDS: Final[int] = 10
POSTGRESQL_PSYCOPG_DRIVER: Final[str] = "postgresql+psycopg"
type AsyncSessionFactory = async_sessionmaker[AsyncSession]


class DatabaseRole(StrEnum):
    """Connection roles separated by the Compose service boundary."""

    MIGRATION = "migration"
    CONTROL = "control"
    GATEWAY = "gateway"
    DOWNLOAD_WORKER = "download_worker"
    INSTANCE_CONTROLLER = "instance_controller"
    LEDGER_WRITER = "ledger_writer"
    MAINTENANCE_WORKER = "maintenance_worker"


@dataclass(frozen=True, slots=True)
class RoleConnectionConfiguration:
    """An immutable role-to-credential configuration without rendering its URL."""

    role: DatabaseRole
    url: URL = field(repr=False)


class RoleConnectionFactory:
    """Return immutable, role-isolated connection configurations."""

    __slots__ = ("_configurations",)

    def __init__(self, role_urls: Mapping[DatabaseRole, URL]) -> None:
        if not role_urls:
            raise ValueError("at least one database role configuration is required")

        configurations: dict[DatabaseRole, RoleConnectionConfiguration] = {}
        credential_identities: set[str] = set()
        for role, url in role_urls.items():
            if not isinstance(role, DatabaseRole):
                raise TypeError("database role keys must be DatabaseRole values")
            if url.drivername != POSTGRESQL_PSYCOPG_DRIVER:
                raise ValueError(f"database driver must be {POSTGRESQL_PSYCOPG_DRIVER}")
            if not url.username:
                raise ValueError("each database role requires a credential identity")
            if url.username in credential_identities:
                raise ValueError("each database role requires a distinct credential identity")
            credential_identities.add(url.username)
            configurations[role] = RoleConnectionConfiguration(role=role, url=url)

        self._configurations = MappingProxyType(configurations)

    def configuration_for(self, role: DatabaseRole) -> RoleConnectionConfiguration:
        """Return the frozen configuration for exactly one database role."""
        if not isinstance(role, DatabaseRole):
            raise TypeError("role must be a DatabaseRole value")
        try:
            return self._configurations[role]
        except KeyError:
            raise KeyError(f"database role is not configured: {role.value}") from None


def create_database_engine(
    url: URL,
    *,
    connect_timeout_seconds: int = DEFAULT_CONNECT_TIMEOUT_SECONDS,
) -> AsyncEngine:
    """Create the async PostgreSQL engine without rendering or logging its URL."""
    if url.drivername != POSTGRESQL_PSYCOPG_DRIVER:
        raise ValueError(f"database driver must be {POSTGRESQL_PSYCOPG_DRIVER}")
    if connect_timeout_seconds < 1:
        raise ValueError("connect_timeout_seconds must be at least one second")

    return create_async_engine(
        url,
        connect_args={
            "connect_timeout": connect_timeout_seconds,
            "options": "-c timezone=UTC",
        },
        echo=False,
        pool_pre_ping=True,
    )


def create_session_factory(engine: AsyncEngine) -> AsyncSessionFactory:
    """Create the shared async session factory for repository implementations."""
    return async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
