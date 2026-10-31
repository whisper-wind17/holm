"""FX-SYS-03: real PostgreSQL, all instance states, FX-PROTO-01 model/artifact seed."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID

from conftest import EphemeralPostgres
from holm_domain import ModelInstanceStatus, RuntimeType
from holm_persistence import (
    SystemStatusRepository,
    create_database_engine,
    create_session_factory,
)
from holm_persistence.model_instances import MODEL_INSTANCES
from migration_test_support import upgrade_head
from sqlalchemy import event
from test_PROTO_01_model_instances import READY_ARTIFACT_ID, _seed_model_and_artifacts


def test_SYS_03_TC_HEALTH_27_counts_only_active_states_by_runtime(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    upgrade_head(ephemeral_postgres.engine)
    _seed_model_and_artifacts(ephemeral_postgres.engine)
    now = datetime.now(UTC)
    rows: list[dict[str, object]] = []
    for runtime in RuntimeType:
        for status in ModelInstanceStatus:
            index = len(rows) + 1
            rows.append(
                {
                    "id": UUID(int=index),
                    "artifact_id": READY_ARTIFACT_ID,
                    "runtime_type": runtime.value,
                    "status": status.value,
                    "expected_gpu_uuids": [f"GPU-FX-SYS-03-{index}"],
                    "tensor_parallel_size": 1,
                    "context_length": 4096,
                    "gpu_memory_utilization": 0.8,
                    "name": f"FX-SYS-03-{index}",
                    "internal_model_name": f"mi_{index:012x}",
                    "config_version": 1,
                    "created_at": now,
                    "updated_at": now,
                }
            )
    # A second running vLLM distinguishes grouping from a global count.
    rows.append(
        {
            **rows[0],
            "id": UUID(int=100),
            "runtime_type": "vllm",
            "status": "Running",
            "name": "FX-SYS-03-100",
            "internal_model_name": "mi_000000000100",
        }
    )
    with ephemeral_postgres.engine.begin() as connection:
        connection.execute(MODEL_INSTANCES.insert(), rows)
    engine = create_database_engine(ephemeral_postgres.engine.url)
    factory = create_session_factory(engine)
    statements: list[str] = []

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def capture(_conn, _cursor, statement, _parameters, _context, _executemany) -> None:  # type: ignore[no-untyped-def]
        statements.append(statement)

    async def scenario() -> None:
        async with factory() as session, session.begin():
            snapshot = await SystemStatusRepository(session).read()
        assert dict(snapshot.active_instance_counts) == {"vllm": 6, "sglang": 5}
        # Existing heartbeat, job, server-version and revision SELECTs: still four.
        assert len([sql for sql in statements if sql.lstrip().upper().startswith("SELECT")]) == 4
        with ephemeral_postgres.engine.begin() as connection:
            connection.execute(MODEL_INSTANCES.update().values(status="Stopped"))
        async with factory() as session, session.begin():
            empty = await SystemStatusRepository(session).read()
        assert dict(empty.active_instance_counts) == {}

    try:
        asyncio.run(scenario())
    finally:
        asyncio.run(engine.dispose())
