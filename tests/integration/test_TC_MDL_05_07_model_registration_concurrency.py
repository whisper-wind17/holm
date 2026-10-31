"""TC-MDL-05/07: real two-process Model and idempotency convergence."""

from __future__ import annotations

import asyncio
import importlib.util
import multiprocessing
import os
import sys
from dataclasses import dataclass
from multiprocessing.connection import Connection as PipeConnection
from pathlib import Path
from typing import Any, Final, cast
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from conftest import PROCESS_TIMEOUT_SECONDS, EphemeralPostgres
from fixtures.source_mutation import mutate_source
from holm_control_api.model_registration_service import (
    ModelRegistrationService,
    ModelRegistrationSuccess,
)
from holm_control_api.model_resolution import (
    ModelResolutionRegistry,
    ResolutionOutcome,
    ResolutionRequest,
    ResolutionSucceeded,
)
from holm_control_api.model_resolution_service import ModelResolutionService
from holm_domain import (
    Identified,
    ModelOriginType,
    RemoteFile,
    ResolvedModel,
    RevisionImmutability,
)
from holm_persistence import create_database_engine, create_session_factory
from holm_security import MasterKey, MasterKeyring, MasterKeyState
from migration_test_support import upgrade_head
from sqlalchemy.engine import URL, Engine

REPOSITORY_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
SERVICE_SOURCE: Final[Path] = REPOSITORY_ROOT / (
    "apps/control_api/src/holm_control_api/model_registration_service.py"
)
HMAC_SECRET: Final[bytes] = b"C" * 32
LOGGER_DECLARATION: Final[str] = (
    '_LOGGER: Final[logging.Logger] = logging.getLogger("holm.control_api.model_registration")'
)
KEYRING: Final[MasterKeyring] = MasterKeyring(
    (MasterKey("concurrency-key", MasterKeyState.WRITABLE, b"K" * 32),)
)


@dataclass(frozen=True, slots=True)
class RaceResult:
    process_pid: int
    outcome: str
    model_id: str | None


class _RaceAdapter:
    async def resolve(
        self,
        request: ResolutionRequest,
        *,
        credential: bytes | None,
    ) -> ResolutionOutcome:
        del credential
        return ResolutionSucceeded(
            ResolvedModel(
                source=request.source,
                repository_id=request.repository_id,
                requested_revision_norm=request.requested_revision_norm,
                resolved_revision="7" * 40,
                revision_immutability=RevisionImmutability.IMMUTABLE,
                files=Identified(
                    (
                        RemoteFile(path="config.json", size_bytes=2),
                        RemoteFile("tokenizer.json", 2),
                        RemoteFile("model.bin", 4),
                    )
                ),
                total_size_bytes=Identified(8),
                architecture=Identified("RaceArchitecture"),
                modalities=Identified(("text",)),
                precision=Identified("bf16"),
                quantization=Identified("none"),
                context_length=Identified(4_096),
                candidate_runtimes=Identified(("vllm",)),
                license=Identified("apache-2.0"),
            )
        )


def _load_service(path: Path | None) -> type[ModelRegistrationService]:
    if path is None:
        return ModelRegistrationService
    module_name = f"_mutated_model_registration_{os.getpid()}"
    specification = importlib.util.spec_from_file_location(module_name, path)
    if specification is None or specification.loader is None:
        raise RuntimeError("mutated registration service could not be loaded")
    module = importlib.util.module_from_spec(specification)
    sys.modules[module_name] = module
    try:
        specification.loader.exec_module(module)
    finally:
        sys.modules.pop(module_name, None)
    return cast(type[ModelRegistrationService], module.ModelRegistrationService)


def _race_worker(
    url: URL,
    actor_id: UUID,
    repository_id: str,
    barrier: Any,
    sender: PipeConnection,
    service_source: Path | None,
) -> None:
    async def run() -> RaceResult:
        engine = create_database_engine(url, connect_timeout_seconds=5)
        try:
            session_factory = create_session_factory(engine)
            resolution = ModelResolutionService(
                session_factory=session_factory,
                keyring=KEYRING,
                registry=ModelResolutionRegistry({ModelOriginType.HUGGING_FACE: _RaceAdapter()}),
            )
            service = _load_service(service_source)(
                session_factory=session_factory,
                resolution=resolution,
                idempotency_hmac_secret=HMAC_SECRET,
            )
            barrier.wait(timeout=PROCESS_TIMEOUT_SECONDS)
            try:
                result = await service.register(
                    actor_id=actor_id,
                    idempotency_key="TC-MDL-07-shared-key",
                    source=ModelOriginType.HUGGING_FACE,
                    repository_id=repository_id,
                    external_path=None,
                    revision=None,
                )
            except Exception as error:  # noqa: BLE001 - result is intentionally classified
                return RaceResult(os.getpid(), type(error).__name__, None)
            if not isinstance(result, ModelRegistrationSuccess):
                return RaceResult(os.getpid(), type(result).__name__, None)
            return RaceResult(os.getpid(), type(result).__name__, str(result.model.id))
        finally:
            await engine.dispose()

    try:
        sender.send(asyncio.run(run()))
    finally:
        sender.close()


def _prepare(engine: Engine) -> UUID:
    upgrade_head(engine)
    actor_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "TRUNCATE core.idempotency_records, core.models, core.model_sources, "
                "core.users CASCADE"
            )
        )
        connection.execute(
            sa.text(
                "INSERT INTO core.users (id, username_norm, display_name, role, status, "
                "password_hash, must_change_password, created_at, updated_at) VALUES ("
                ":id, 'mdl-race-admin', 'MDL Race Admin', 'admin', 'active', "
                "'$argon2id$fixture', false, clock_timestamp(), clock_timestamp())"
            ),
            {"id": actor_id},
        )
        connection.execute(
            sa.text(
                "INSERT INTO core.model_sources (id, type, created_at, updated_at) VALUES ("
                ":id, 'Hugging Face', clock_timestamp(), clock_timestamp())"
            ),
            {"id": uuid4()},
        )
    return actor_id


def _run_race(
    fixture: EphemeralPostgres,
    *,
    repository_ids: tuple[str, str],
    service_source: Path | None = None,
) -> tuple[RaceResult, RaceResult]:
    actor_id = _prepare(fixture.engine)
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    processes: list[Any] = []
    receivers: list[PipeConnection] = []
    for repository_id in repository_ids:
        receiver, sender = context.Pipe(duplex=False)
        process = context.Process(
            target=_race_worker,
            args=(
                fixture.engine.url,
                actor_id,
                repository_id,
                barrier,
                sender,
                service_source,
            ),
        )
        process.start()
        sender.close()
        processes.append(process)
        receivers.append(receiver)
    try:
        results: list[RaceResult] = []
        for receiver in receivers:
            if not receiver.poll(PROCESS_TIMEOUT_SECONDS):
                pytest.fail("registration race worker timed out")
            results.append(cast(RaceResult, receiver.recv()))
        for process in processes:
            process.join(PROCESS_TIMEOUT_SECONDS)
            assert process.exitcode == 0
        assert len({result.process_pid for result in results}) == 2
        return results[0], results[1]
    finally:
        for process in processes:
            if process.is_alive():
                process.kill()
                process.join(5)
        for receiver in receivers:
            receiver.close()


def _counts(engine: Engine) -> tuple[int, int]:
    with engine.connect() as connection:
        return (
            int(connection.exec_driver_sql("SELECT count(*) FROM core.models").scalar_one()),
            int(
                connection.exec_driver_sql(
                    "SELECT count(*) FROM core.idempotency_records"
                ).scalar_one()
            ),
        )


def _assert_converged(engine: Engine) -> None:
    assert _counts(engine) == (1, 1)


def test_TC_MDL_05_two_processes_same_source_converge_to_one_model(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    results = _run_race(
        ephemeral_postgres,
        repository_ids=("race-owner/same-model", "race-owner/same-model"),
    )
    assert {result.outcome for result in results} == {"ModelRegistrationSuccess"}
    assert len({result.model_id for result in results}) == 1
    _assert_converged(ephemeral_postgres.engine)


def test_TC_MDL_07_two_processes_same_key_different_payload_roll_back_the_loser(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    results = _run_race(
        ephemeral_postgres,
        repository_ids=("race-owner/model-a", "race-owner/model-b"),
    )
    assert sorted(result.outcome for result in results) == [
        "IdempotencyConflict",
        "ModelRegistrationSuccess",
    ]
    _assert_converged(ephemeral_postgres.engine)


def test_TC_MDL_07_positive_control_record_outside_business_transaction_fails(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
) -> None:
    mutant = tmp_path / "record_outside_transaction.py"
    mutant.write_text(
        mutate_source(
            SERVICE_SOURCE,
            [
                (
                    "            model = await repository.upsert(candidate, now=now)",
                    "            model = await repository.upsert(candidate, now=now)\n"
                    "            await session.commit()",
                )
            ],
        ),
        encoding="utf-8",
    )
    _run_race(
        ephemeral_postgres,
        repository_ids=("race-owner/outside-a", "race-owner/outside-b"),
        service_source=mutant,
    )
    with pytest.raises(AssertionError):
        _assert_converged(ephemeral_postgres.engine)


def test_TC_MDL_05_positive_control_preselect_without_unique_claim_fails(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
) -> None:
    replacement = (
        LOGGER_DECLARATION
        + """


class _PreselectOnlyRepository:
    def __init__(self, session: object) -> None:
        self._session = session

    async def claim(self, **values: object) -> object:
        query = __import__("sqlalchemy").text(
            "SELECT response_object_id FROM core.idempotency_records "
            "WHERE actor_id = :actor_id AND operation = :operation"
        )
        found = (await self._session.execute(query, {
        "actor_id": values["actor_id"], "operation": values["operation"]
        })).scalar_one_or_none()
        await __import__("asyncio").sleep(0.25)
        if found is not None:
            return type("Observed", (), {"claimed": False, "record": None})()
        return type("Claimed", (), {"claimed": True})()


IdempotencyRepository = _PreselectOnlyRepository"""
    )
    mutant = tmp_path / "preselect_only.py"
    mutant.write_text(
        mutate_source(
            SERVICE_SOURCE,
            [
                (
                    LOGGER_DECLARATION,
                    replacement,
                )
            ],
        ),
        encoding="utf-8",
    )
    _run_race(
        ephemeral_postgres,
        repository_ids=("race-owner/preselect-a", "race-owner/preselect-b"),
        service_source=mutant,
    )
    with pytest.raises(AssertionError):
        _assert_converged(ephemeral_postgres.engine)
