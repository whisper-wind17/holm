"""TC-DB-05: connection configuration stays isolated by database role."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from typing import Protocol

import pytest
from holm_persistence.database import DatabaseRole, RoleConnectionFactory
from sqlalchemy.engine import Engine


class PostgresFixture(Protocol):
    @property
    def engine(self) -> Engine: ...


def test_TC_DB_05_role_configurations_are_isolated_frozen_and_use_distinct_credentials(
    ephemeral_postgres: PostgresFixture,
) -> None:
    with ephemeral_postgres.engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT 1").scalar_one() == 1

    base_url = ephemeral_postgres.engine.url
    role_urls = {
        DatabaseRole.MIGRATION: base_url.set(username="holm_migration"),
        DatabaseRole.CONTROL: base_url.set(username="holm_control"),
        DatabaseRole.GATEWAY: base_url.set(username="holm_gateway"),
    }
    factory = RoleConnectionFactory(role_urls)

    migration = factory.configuration_for(DatabaseRole.MIGRATION)
    control = factory.configuration_for(DatabaseRole.CONTROL)
    gateway = factory.configuration_for(DatabaseRole.GATEWAY)
    role_urls[DatabaseRole.MIGRATION] = base_url.set(username="caller_mutation")

    assert migration is not control
    assert control is not gateway
    assert migration.role is DatabaseRole.MIGRATION
    assert migration.url.username == "holm_migration"
    assert {migration.url.username, control.url.username, gateway.url.username} == {
        "holm_migration",
        "holm_control",
        "holm_gateway",
    }
    # The guarantee under test is the frozen dataclass's own __setattr__, which
    # rejects every write at runtime. A plain `migration.role = ...` cannot express
    # it: the type checker rejects the static write to a read-only field before the
    # test can run, so the rejecting hook is invoked directly instead.
    with pytest.raises(FrozenInstanceError):
        migration.__setattr__("role", DatabaseRole.GATEWAY)

    shared_credential = base_url.set(username="holm_shared")
    with pytest.raises(ValueError, match="distinct credential identity"):
        RoleConnectionFactory(
            {
                DatabaseRole.MIGRATION: shared_credential,
                DatabaseRole.CONTROL: shared_credential,
            }
        )
