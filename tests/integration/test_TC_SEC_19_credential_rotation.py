"""TC-SEC-19 real PostgreSQL credential envelope and rotation tests."""

from __future__ import annotations

import asyncio
import multiprocessing
import os
import time
from collections.abc import Iterable
from datetime import UTC, datetime
from multiprocessing.connection import Connection as PipeConnection
from multiprocessing.context import SpawnProcess
from typing import Any, Final
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from conftest import EphemeralPostgres
from fixtures.asgi_client import AsgiTestClient, LifespanError
from holm_contracts import VersionPayload
from holm_control_api.application import create_app
from holm_control_api.credential_rotation import (
    CredentialRotationService,
    DatabaseCredentialStartupCheck,
    RotationObserver,
)
from holm_control_api.readiness import DependencyCheck, ReadinessReason
from holm_domain import CredentialType, ModelSourceType
from holm_persistence import (
    MODEL_SOURCES,
    ModelSourceRepository,
    create_database_engine,
    create_session_factory,
)
from holm_security import (
    CredentialDecryptionError,
    CredentialEnvelope,
    KeyringError,
    MasterKey,
    MasterKeyring,
    MasterKeyState,
)
from migration_test_support import (
    ALEMBIC_CONFIG_PATH,
    assert_head_structure,
    database_structure,
    upgrade_head,
)
from sqlalchemy.engine import URL, Engine
from sqlalchemy.pool import NullPool

TOKEN_TYPE: Final[str] = CredentialType.ACCESS_TOKEN.value
PROCESS_TIMEOUT_SECONDS: Final[int] = 20


def _prepare_database(engine: Engine) -> None:
    upgrade_head(engine)
    with engine.begin() as connection:
        connection.execute(sa.text("DELETE FROM core.model_sources"))


def _keyring(new_material: bytes, old_material: bytes) -> MasterKeyring:
    return MasterKeyring(
        (
            MasterKey("new-key", MasterKeyState.WRITABLE, new_material),
            MasterKey("old-key", MasterKeyState.DECRYPT_ONLY, old_material),
        )
    )


def _old_keyring(old_material: bytes) -> MasterKeyring:
    return MasterKeyring((MasterKey("old-key", MasterKeyState.WRITABLE, old_material),))


def _async_url(url: URL) -> URL:
    return url.set(drivername="postgresql+psycopg")


async def _seed_sources(
    url: URL,
    *,
    old_material: bytes,
    plaintexts: tuple[bytes, bytes],
) -> tuple[UUID, UUID]:
    engine = create_database_engine(_async_url(url))
    factory = create_session_factory(engine)
    old_ring = _old_keyring(old_material)
    ids: list[UUID] = []
    try:
        async with factory() as session, session.begin():
            repository = ModelSourceRepository(session)
            for source_type, plaintext in zip(ModelSourceType, plaintexts, strict=True):
                record = await repository.create(
                    source_type=source_type,
                    source_id=uuid4(),
                    now=datetime.now(UTC),
                )
                envelope = old_ring.encrypt(
                    plaintext,
                    source_id=record.id,
                    credential_type=TOKEN_TYPE,
                )
                updated = await repository.set_credential(
                    record.id,
                    credential=envelope,
                    now=datetime.now(UTC),
                )
                assert updated is not None
                ids.append(record.id)
    finally:
        await engine.dispose()
    return ids[0], ids[1]


async def _read_records(url: URL) -> tuple[Any, ...]:
    engine = create_database_engine(_async_url(url))
    factory = create_session_factory(engine)
    try:
        async with factory() as session:
            return await ModelSourceRepository(session).list_configured()
    finally:
        await engine.dispose()


async def _rotate(
    url: URL,
    ring: MasterKeyring,
    *,
    batch_size: int = 64,
    observer: RotationObserver | None = None,
) -> None:
    engine = create_database_engine(_async_url(url))
    try:
        service = CredentialRotationService(create_session_factory(engine), ring, observer)
        await service.rotate(batch_size=batch_size)
    finally:
        await engine.dispose()


def _assert_sentinels_absent(scanned: bytes, sentinels: Iterable[bytes]) -> None:
    for sentinel in sentinels:
        assert sentinel not in scanned


def _reader_worker(
    url: URL,
    new_material: bytes,
    old_material: bytes,
    stop: Any,
    mixed_seen: Any,
    sender: PipeConnection,
) -> None:
    ring = _keyring(new_material, old_material)
    engine = sa.create_engine(url, poolclass=NullPool)
    reads = 0
    failures = 0
    ready_sent = False
    try:
        while not stop.is_set():
            with engine.connect() as connection:
                rows = tuple(
                    connection.execute(
                        sa.text(
                            """
                        SELECT id, credential_key_id, credential_nonce,
                               credential_ciphertext, credential_tag
                        FROM core.model_sources
                        WHERE credential_key_id IS NOT NULL
                        ORDER BY id
                        """
                        )
                    )
                    .mappings()
                    .all()
                )
                key_ids = {row["credential_key_id"] for row in rows}
                if {"new-key", "old-key"}.issubset(key_ids):
                    mixed_seen.set()
                for row in rows:
                    try:
                        ring.decrypt(
                            CredentialEnvelope(
                                key_id=row["credential_key_id"],
                                nonce=bytes(row["credential_nonce"]),
                                ciphertext=bytes(row["credential_ciphertext"]),
                                tag=bytes(row["credential_tag"]),
                            ),
                            source_id=row["id"],
                            credential_type=TOKEN_TYPE,
                        )
                        reads += 1
                    except Exception:
                        failures += 1
            if not ready_sent:
                sender.send(("ready", reads, failures))
                ready_sent = True
            time.sleep(0.002)
        sender.send((reads, failures))
    finally:
        engine.dispose()
        sender.close()


class _BlockingObserver(RotationObserver):
    def __init__(self, sender: PipeConnection, release: Any, stop_at: int) -> None:
        self._sender = sender
        self._release = release
        self._stop_at = stop_at

    async def row_decrypted(self, source_id: UUID, row_number: int) -> None:
        if row_number == self._stop_at:
            self._sender.send((row_number, str(source_id)))
            await asyncio.to_thread(self._release.wait)


class _MixedStateObserver(RotationObserver):
    def __init__(self, mixed_seen: Any) -> None:
        self._mixed_seen = mixed_seen

    async def row_decrypted(self, source_id: UUID, row_number: int) -> None:
        if row_number == 2:
            observed = await asyncio.to_thread(
                self._mixed_seen.wait,
                PROCESS_TIMEOUT_SECONDS,
            )
            assert observed, "reader did not observe the committed mixed-Key window"


def _paused_rotation_worker(
    url: URL,
    new_material: bytes,
    old_material: bytes,
    stop_at: int,
    release: Any,
    sender: PipeConnection,
    unconditional: bool = False,
) -> None:
    async def scenario() -> None:
        engine = create_database_engine(_async_url(url))
        try:
            if unconditional:
                ModelSourceRepository.compare_and_swap_credential = (  # type: ignore[method-assign]
                    _unconditional_compare_and_swap_credential
                )
            observer = _BlockingObserver(sender, release, stop_at)
            service = CredentialRotationService(
                create_session_factory(engine),
                _keyring(new_material, old_material),
                observer,
            )
            result = await service.rotate(batch_size=2)
            sender.send(("done", result.reencrypted_rows, result.concurrent_updates))
        finally:
            await engine.dispose()

    try:
        asyncio.run(scenario())
    finally:
        sender.close()


async def _unconditional_compare_and_swap_credential(
    self: ModelSourceRepository,
    source_id: UUID,
    *,
    expected_key_id: str,
    credential: CredentialEnvelope,
    now: datetime,
) -> bool:
    """Executable mutant: omit the stale-write predicate while keeping one atomic UPDATE."""

    del expected_key_id
    statement = (
        sa.update(MODEL_SOURCES)
        .where(MODEL_SOURCES.c.id == source_id)
        .values(
            credential_key_id=credential.key_id,
            credential_nonce=credential.nonce,
            credential_ciphertext=credential.ciphertext,
            credential_tag=credential.tag,
            updated_at=now,
        )
        .returning(MODEL_SOURCES.c.id)
    )
    return (await self._session.execute(statement)).scalar_one_or_none() is not None  # noqa: SLF001


def _split_write_mutant_worker(
    url: URL,
    source_id: UUID,
    replacement: CredentialEnvelope,
    sender: PipeConnection,
) -> None:
    engine = sa.create_engine(url, poolclass=NullPool)
    try:
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    """
                    UPDATE core.model_sources
                    SET credential_nonce = :nonce,
                        credential_ciphertext = :ciphertext,
                        credential_tag = :tag
                    WHERE id = :source_id
                    """
                ),
                {
                    "source_id": source_id,
                    "nonce": replacement.nonce,
                    "ciphertext": replacement.ciphertext,
                    "tag": replacement.tag,
                },
            )
        sender.send("first-statement-committed")
        time.sleep(PROCESS_TIMEOUT_SECONDS)
    finally:
        engine.dispose()
        sender.close()


def _wait_message(receiver: PipeConnection) -> Any:
    assert receiver.poll(PROCESS_TIMEOUT_SECONDS), "credential worker did not report"
    return receiver.recv()


def _stop_process(process: SpawnProcess) -> None:
    process.join(timeout=0)
    if process.is_alive():
        process.kill()
    process.join(timeout=5)
    assert not process.is_alive()


def test_TC_SEC_19_migration_downgrade_upgrade_is_exact(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    _prepare_database(ephemeral_postgres.engine)

    def source_snapshot() -> tuple[tuple[object, ...], ...]:
        with ephemeral_postgres.engine.connect() as connection:
            return tuple(
                tuple(row)
                for row in connection.execute(
                    sa.text(
                        """
                        SELECT object_kind, object_name, definition
                        FROM (
                            SELECT 'column'::text AS object_kind,
                                   column_name AS object_name,
                                   data_type || ':' || is_nullable AS definition
                            FROM information_schema.columns
                            WHERE table_schema = 'core' AND table_name = 'model_sources'
                            UNION ALL
                            SELECT 'constraint', constraint_record.conname,
                                   pg_get_constraintdef(constraint_record.oid, true)
                            FROM pg_constraint AS constraint_record
                            JOIN pg_class AS relation ON relation.oid = constraint_record.conrelid
                            JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
                            WHERE namespace.nspname = 'core' AND relation.relname = 'model_sources'
                            UNION ALL
                            SELECT 'index', indexname, indexdef
                            FROM pg_indexes
                            WHERE schemaname = 'core' AND tablename = 'model_sources'
                        ) AS objects
                        ORDER BY object_kind, object_name, definition
                        """
                    )
                ).tuples()
            )

    first = source_snapshot()
    assert first
    with ephemeral_postgres.engine.connect() as connection:
        config = Config(ALEMBIC_CONFIG_PATH, attributes={"connection": connection})
        command.downgrade(config, "20260902_0008")
    with ephemeral_postgres.engine.connect() as connection:
        assert (
            connection.execute(sa.text("SELECT to_regclass('core.model_sources')")).scalar_one()
            is None
        )
    with ephemeral_postgres.engine.connect() as connection:
        config = Config(ALEMBIC_CONFIG_PATH, attributes={"connection": connection})
        command.upgrade(config, "head")
    assert source_snapshot() == first
    assert_head_structure(database_structure(ephemeral_postgres.engine))


def test_TC_SEC_19_startup_and_disable_gate_fail_closed_on_real_references(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    _prepare_database(ephemeral_postgres.engine)
    engine = create_database_engine(_async_url(ephemeral_postgres.engine.url))
    factory = create_session_factory(engine)
    version = VersionPayload(
        build_version="2.16.0+g" + "a" * 10,
        commit="b" * 40,
        schema_version=1,
        runtime_compat_hash="c" * 64,
    )

    async def scenario() -> None:
        async def ready_probe() -> ReadinessReason:
            return ReadinessReason.OK

        readiness = [DependencyCheck(name="database", probe=ready_probe)]
        empty_check = DatabaseCredentialStartupCheck(factory, None)
        app = create_app(
            version_payload=version,
            readiness_checks=readiness,
            credential_startup_check=empty_check,
        )
        async with AsgiTestClient(app).lifespan():
            pass

        old_material = os.urandom(32)
        new_material = os.urandom(32)
        await _seed_sources(
            ephemeral_postgres.engine.url,
            old_material=old_material,
            plaintexts=(os.urandom(30), os.urandom(31)),
        )
        missing_app = create_app(
            version_payload=version,
            readiness_checks=readiness,
            credential_startup_check=DatabaseCredentialStartupCheck(factory, None),
        )
        with pytest.raises(LifespanError):
            async with AsgiTestClient(missing_app).lifespan():
                pass

        ring = _keyring(new_material, old_material)
        service = CredentialRotationService(factory, ring)
        with pytest.raises(KeyringError, match="key_still_referenced"):
            await service.require_key_can_be_disabled("old-key")
        await service.rotate()
        await service.require_key_can_be_disabled("old-key")

        with ephemeral_postgres.engine.begin() as connection:
            connection.execute(sa.text("DELETE FROM core.model_sources"))
        with pytest.raises(KeyringError, match="reference_scan_empty"):
            await service.require_key_can_be_disabled("old-key")

    try:
        asyncio.run(scenario())
    finally:
        asyncio.run(engine.dispose())


def test_TC_SEC_19_rotation_has_a_separate_continuous_reader_process(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    _prepare_database(ephemeral_postgres.engine)
    old_material = os.urandom(32)
    new_material = os.urandom(32)
    plaintexts = (os.urandom(35), os.urandom(36))
    asyncio.run(
        _seed_sources(
            ephemeral_postgres.engine.url, old_material=old_material, plaintexts=plaintexts
        )
    )
    context = multiprocessing.get_context("spawn")
    stop = context.Event()
    mixed_seen = context.Event()
    receiver, sender = context.Pipe(duplex=False)
    reader = context.Process(
        target=_reader_worker,
        args=(
            ephemeral_postgres.engine.url,
            new_material,
            old_material,
            stop,
            mixed_seen,
            sender,
        ),
    )
    reader.start()
    try:
        assert _wait_message(receiver)[0] == "ready"
        asyncio.run(
            _rotate(
                ephemeral_postgres.engine.url,
                _keyring(new_material, old_material),
                batch_size=1,
                observer=_MixedStateObserver(mixed_seen),
            )
        )
        assert mixed_seen.is_set()
        time.sleep(0.1)
        stop.set()
        reads, failures = _wait_message(receiver)
        reader.join(timeout=5)
        assert reader.exitcode == 0
        assert reads > 0
        assert failures == 0
        records = asyncio.run(_read_records(ephemeral_postgres.engine.url))
        assert {record.credential.key_id for record in records if record.credential} == {"new-key"}
    finally:
        stop.set()
        _stop_process(reader)
        receiver.close()


def test_TC_SEC_19_real_process_kills_resume_and_split_write_mutant_is_broken(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    _prepare_database(ephemeral_postgres.engine)
    old_material = os.urandom(32)
    new_material = os.urandom(32)
    plaintexts = (os.urandom(38), os.urandom(39))
    source_ids = asyncio.run(
        _seed_sources(
            ephemeral_postgres.engine.url, old_material=old_material, plaintexts=plaintexts
        )
    )
    context = multiprocessing.get_context("spawn")

    for stop_at in (1, 2):
        release = context.Event()
        receiver, sender = context.Pipe(duplex=False)
        worker = context.Process(
            target=_paused_rotation_worker,
            args=(
                ephemeral_postgres.engine.url,
                new_material,
                old_material,
                stop_at,
                release,
                sender,
            ),
        )
        worker.start()
        assert _wait_message(receiver)[0] == stop_at
        worker.kill()
        worker.join(timeout=5)
        assert worker.exitcode is not None and worker.exitcode != 0
        records = asyncio.run(_read_records(ephemeral_postgres.engine.url))
        ring = _keyring(new_material, old_material)
        assert all(
            ring.decrypt(
                record.credential,
                source_id=record.id,
                credential_type=TOKEN_TYPE,
            )
            for record in records
            if record.credential is not None
        )
        asyncio.run(_rotate(ephemeral_postgres.engine.url, ring))
        receiver.close()
        if stop_at == 1:
            old_ring = _old_keyring(old_material)
            with ephemeral_postgres.engine.begin() as connection:
                for source_id, plaintext in zip(source_ids, plaintexts, strict=True):
                    envelope = old_ring.encrypt(
                        plaintext, source_id=source_id, credential_type=TOKEN_TYPE
                    )
                    connection.execute(
                        sa.update(MODEL_SOURCES)
                        .where(MODEL_SOURCES.c.id == source_id)
                        .values(
                            credential_key_id=envelope.key_id,
                            credential_nonce=envelope.nonce,
                            credential_ciphertext=envelope.ciphertext,
                            credential_tag=envelope.tag,
                        )
                    )

    source_id = source_ids[0]
    old_envelope = _old_keyring(old_material).encrypt(
        plaintexts[0], source_id=source_id, credential_type=TOKEN_TYPE
    )
    with ephemeral_postgres.engine.begin() as connection:
        connection.execute(
            sa.update(MODEL_SOURCES)
            .where(MODEL_SOURCES.c.id == source_id)
            .values(
                credential_key_id=old_envelope.key_id,
                credential_nonce=old_envelope.nonce,
                credential_ciphertext=old_envelope.ciphertext,
                credential_tag=old_envelope.tag,
            )
        )
    replacement = _keyring(new_material, old_material).encrypt(
        plaintexts[0], source_id=source_id, credential_type=TOKEN_TYPE
    )
    receiver, sender = context.Pipe(duplex=False)
    mutant = context.Process(
        target=_split_write_mutant_worker,
        args=(ephemeral_postgres.engine.url, source_id, replacement, sender),
    )
    mutant.start()
    assert _wait_message(receiver) == "first-statement-committed"
    mutant.kill()
    mutant.join(timeout=5)
    broken = {
        record.id: record for record in asyncio.run(_read_records(ephemeral_postgres.engine.url))
    }[source_id]
    assert broken.credential is not None
    with pytest.raises(CredentialDecryptionError):
        _keyring(new_material, old_material).decrypt(
            broken.credential,
            source_id=broken.id,
            credential_type=TOKEN_TYPE,
        )
    receiver.close()


def _write_envelope(engine: Engine, source_id: UUID, envelope: CredentialEnvelope) -> None:
    with engine.begin() as connection:
        connection.execute(
            sa.update(MODEL_SOURCES)
            .where(MODEL_SOURCES.c.id == source_id)
            .values(
                credential_key_id=envelope.key_id,
                credential_nonce=envelope.nonce,
                credential_ciphertext=envelope.ciphertext,
                credential_tag=envelope.tag,
            )
        )


def _row_plaintext(engine: Engine, ring: MasterKeyring, source_id: UUID) -> bytes:
    record = {record.id: record for record in asyncio.run(_read_records(engine.url))}[source_id]
    assert record.credential is not None
    return ring.decrypt(record.credential, source_id=record.id, credential_type=TOKEN_TYPE)


def test_TC_SEC_19_conditional_write_does_not_overwrite_concurrent_process_update(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    _prepare_database(ephemeral_postgres.engine)
    engine = ephemeral_postgres.engine
    old_material = os.urandom(32)
    new_material = os.urandom(32)
    original = (os.urandom(40), os.urandom(41))
    source_ids = asyncio.run(
        _seed_sources(engine.url, old_material=old_material, plaintexts=original)
    )
    seeded = dict(zip(source_ids, original, strict=True))
    ring = _keyring(new_material, old_material)
    old_ring = _old_keyring(old_material)
    context = multiprocessing.get_context("spawn")

    def race(*, unconditional: bool, target: UUID | None) -> tuple[UUID, bytes]:
        """Pause rotation after the decrypt of one row, then update that same row."""

        release = context.Event()
        receiver, sender = context.Pipe(duplex=False)
        worker = context.Process(
            target=_paused_rotation_worker,
            args=(
                engine.url,
                new_material,
                old_material,
                1,
                release,
                sender,
                unconditional,
            ),
        )
        worker.start()
        try:
            paused_row, paused_id_text = _wait_message(receiver)
            assert paused_row == 1
            paused_id = UUID(paused_id_text)
            assert target is None or paused_id == target
            concurrent_plaintext = os.urandom(47)
            _write_envelope(
                engine,
                paused_id,
                ring.encrypt(
                    concurrent_plaintext,
                    source_id=paused_id,
                    credential_type=TOKEN_TYPE,
                ),
            )
            release.set()
            assert _wait_message(receiver)[0] == "done"
            worker.join(timeout=PROCESS_TIMEOUT_SECONDS)
            assert worker.exitcode == 0
        finally:
            _stop_process(worker)
            receiver.close()
        return paused_id, concurrent_plaintext

    paused_id, concurrent_plaintext = race(unconditional=False, target=None)
    assert _row_plaintext(engine, ring, paused_id) == concurrent_plaintext

    # Positive control for the stale-write predicate: rewind exactly the row the
    # rotation pauses on, then run the identical race against a mutant whose UPDATE
    # keeps one atomic statement but drops `credential_key_id = :expected`.
    _write_envelope(
        engine,
        paused_id,
        old_ring.encrypt(seeded[paused_id], source_id=paused_id, credential_type=TOKEN_TYPE),
    )
    mutant_id, mutant_concurrent_plaintext = race(unconditional=True, target=paused_id)
    assert mutant_id == paused_id
    regressed = _row_plaintext(engine, ring, paused_id)
    assert regressed == seeded[paused_id]
    with pytest.raises(AssertionError):
        assert regressed == mutant_concurrent_plaintext
