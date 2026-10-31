"""FIX-22 / FX-FIX-22: reuse M4-04's service graph in a real isolated PostgreSQL."""

from __future__ import annotations

import asyncio

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from conftest import EphemeralPostgres
from holm_gateway.route_snapshot import (  # type: ignore[import-untyped]
    PostgresRouteSnapshotSource,
)
from holm_gateway.routing import InferenceRouteDirectory  # type: ignore[import-untyped]
from holm_persistence import METADATA, create_session_factory
from holm_persistence.role_grants import (
    COLUMN_SELECTS,
    FIX_22_COLUMN_SELECTS,
    GATEWAY,
    M7_07_COLUMN_SELECTS,
)
from migration_test_support import ALEMBIC_CONFIG_PATH, upgrade_head
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from test_M4_04_snapshot_integration import MODEL_ID, _async_url, _environment, _seed


def _assert_column_grants(engine: sa.Engine, *, upgraded: bool) -> None:
    expected = COLUMN_SELECTS if upgraded else M7_07_COLUMN_SELECTS
    with engine.connect() as connection:
        for table, role in FIX_22_COLUMN_SELECTS:
            for column in METADATA.tables[table].columns:
                allowed = connection.scalar(
                    sa.text("SELECT has_column_privilege(:role, :table, :column, 'SELECT')"),
                    {"role": role, "table": table, "column": column.name},
                )
                assert allowed == (column.name in expected.get((table, role), ()))


def test_FIX_22_TC_DP_13_upgrade_downgrade_only_adds_reviewed_columns(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    upgrade_head(ephemeral_postgres.engine)
    _assert_column_grants(ephemeral_postgres.engine, upgraded=True)
    config = Config(ALEMBIC_CONFIG_PATH)
    try:
        with ephemeral_postgres.engine.begin() as connection:
            config.attributes["connection"] = connection
            command.downgrade(config, "20261014_0040")
        _assert_column_grants(ephemeral_postgres.engine, upgraded=False)
    finally:
        upgrade_head(ephemeral_postgres.engine)
    _assert_column_grants(ephemeral_postgres.engine, upgraded=True)


def test_FIX_22_TC_DP_13_gateway_loads_architecture_but_cannot_read_other_model_columns(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    _seed(ephemeral_postgres)

    async def scenario() -> None:
        async with _environment(ephemeral_postgres, listen=False) as environment:
            async with environment.factory() as session, session.begin():
                await session.execute(
                    sa.text("UPDATE core.models SET architecture = :architecture WHERE id = :id"),
                    {"architecture": "Qwen3_5ForConditionalGeneration", "id": MODEL_ID},
                )
            engine = create_async_engine(
                _async_url(ephemeral_postgres),
                connect_args={"options": f"-c role={GATEWAY}"},
            )
            try:
                factory = create_session_factory(engine)
                async with factory() as session:
                    assert await session.scalar(sa.text("SELECT current_user")) == GATEWAY
                snapshot = await PostgresRouteSnapshotSource(factory).load(loaded_at=1.0)
                route = snapshot.routes_by_model["m4-04-model"]
                assert route.model_architecture == "Qwen3_5ForConditionalGeneration"
                assert (
                    InferenceRouteDirectory._adapt(
                        InferenceRouteDirectory(environment.manager), route
                    ).model_architecture
                    == route.model_architecture
                )
                for column in METADATA.tables["core.models"].columns:
                    if column.name in COLUMN_SELECTS[("core.models", GATEWAY)]:
                        continue
                    async with factory() as session:
                        with pytest.raises(DBAPIError) as error:
                            await session.execute(sa.select(column).limit(0))
                        assert getattr(error.value.orig, "sqlstate", None) == "42501"
            finally:
                await engine.dispose()

    asyncio.run(asyncio.wait_for(scenario(), timeout=20))
