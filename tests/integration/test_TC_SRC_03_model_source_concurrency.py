"""TC-SRC-03: two-process production full-envelope CAS collision."""

from __future__ import annotations

import asyncio
import multiprocessing
import os
from datetime import UTC, datetime
from multiprocessing.connection import Connection as PipeConnection
from multiprocessing.context import SpawnProcess
from typing import Any, Final
from uuid import UUID, uuid4

import sqlalchemy as sa
from conftest import EphemeralPostgres
from holm_domain import CredentialType, ModelSourceType
from holm_persistence import (
    ModelSourceRepository,
    create_database_engine,
    create_session_factory,
)
from holm_security import MasterKey, MasterKeyring, MasterKeyState
from migration_test_support import upgrade_head
from sqlalchemy.engine import URL

KEYRING: Final = MasterKeyring((MasterKey("race-key", MasterKeyState.WRITABLE, b"R" * 32),))
TOKEN_TYPE: Final[str] = CredentialType.ACCESS_TOKEN.value
PROCESS_TIMEOUT_SECONDS: Final[int] = 20


def _repository_cas_worker(
    url: URL,
    source_id: UUID,
    replacement_plaintext: bytes,
    release: Any,
    sender: PipeConnection,
) -> None:
    async def scenario() -> None:
        engine = create_database_engine(url.set(drivername="postgresql+psycopg"))
        try:
            async with create_session_factory(engine)() as session:
                repository = ModelSourceRepository(session)
                current = await repository.get(source_id)
                assert current is not None and current.credential is not None
                replacement = KEYRING.encrypt(
                    replacement_plaintext,
                    source_id=source_id,
                    credential_type=TOKEN_TYPE,
                )
                backend_pid = await session.scalar(sa.text("SELECT pg_backend_pid()"))
                sender.send(("ready", os.getpid(), backend_pid))
                released = await asyncio.to_thread(release.wait, PROCESS_TIMEOUT_SECONDS)
                assert released, "CAS workers were not released"
                changed = await repository.compare_and_swap_full_credential(
                    source_id,
                    expected_key_id=current.credential.key_id,
                    expected_credential=current.credential,
                    match_full_envelope=True,
                    credential=replacement,
                    now=datetime.now(UTC),
                )
                await session.commit()
                sender.send(("result", os.getpid(), changed))
        finally:
            await engine.dispose()

    try:
        asyncio.run(scenario())
    except BaseException as error:
        sender.send(("error", os.getpid(), type(error).__name__))
    finally:
        sender.close()


def _wait_message(receiver: PipeConnection) -> tuple[object, ...]:
    assert receiver.poll(PROCESS_TIMEOUT_SECONDS), "CAS worker did not report"
    message = receiver.recv()
    assert isinstance(message, tuple)
    return message


def _stop_process(process: SpawnProcess) -> None:
    process.join(timeout=0)
    if process.is_alive():
        process.kill()
    process.join(timeout=5)
    assert not process.is_alive()


def test_TC_SRC_03_two_process_full_envelope_cas_reports_the_colliding_writer(
    ephemeral_postgres: EphemeralPostgres,
) -> None:
    upgrade_head(ephemeral_postgres.engine)
    source_id = uuid4()
    old_plaintext = b"old-race-credential"
    replacements = (b"replacement-from-process-a", b"replacement-from-process-b")
    old_envelope = KEYRING.encrypt(
        old_plaintext,
        source_id=source_id,
        credential_type=TOKEN_TYPE,
    )
    with ephemeral_postgres.engine.begin() as connection:
        connection.execute(sa.text("TRUNCATE core.models, core.model_sources CASCADE"))
        connection.execute(
            sa.text(
                "INSERT INTO core.model_sources ("
                "id, type, credential_key_id, credential_nonce, credential_ciphertext, "
                "credential_tag, created_at, updated_at) VALUES ("
                ":id, :type, :key_id, :nonce, :ciphertext, :tag, "
                "clock_timestamp(), clock_timestamp())"
            ),
            {
                "id": source_id,
                "type": ModelSourceType.HUGGING_FACE.value,
                "key_id": old_envelope.key_id,
                "nonce": old_envelope.nonce,
                "ciphertext": old_envelope.ciphertext,
                "tag": old_envelope.tag,
            },
        )

    context = multiprocessing.get_context("spawn")
    release = context.Event()
    pipes = tuple(context.Pipe(duplex=False) for _ in replacements)
    processes = tuple(
        context.Process(
            target=_repository_cas_worker,
            args=(
                ephemeral_postgres.engine.url,
                source_id,
                replacement,
                release,
                sender,
            ),
        )
        for replacement, (_, sender) in zip(replacements, pipes, strict=True)
    )
    try:
        for process in processes:
            process.start()
        ready = tuple(_wait_message(receiver) for receiver, _ in pipes)
        assert all(message[0] == "ready" for message in ready), ready
        assert len({message[1] for message in ready}) == 2
        assert len({message[2] for message in ready}) == 2
        release.set()
        results = tuple(_wait_message(receiver) for receiver, _ in pipes)
        assert all(message[0] == "result" for message in results), results
        assert [message[2] for message in results].count(True) == 1
        assert [message[2] for message in results].count(False) == 1
    finally:
        release.set()
        for process in processes:
            _stop_process(process)
        for receiver, _ in pipes:
            receiver.close()

    async def read_final_plaintext() -> bytes:
        engine = create_database_engine(ephemeral_postgres.engine.url)
        try:
            async with create_session_factory(engine)() as session:
                record = await ModelSourceRepository(session).get(source_id)
                assert record is not None and record.credential is not None
                return KEYRING.decrypt(
                    record.credential,
                    source_id=record.id,
                    credential_type=TOKEN_TYPE,
                )
        finally:
            await engine.dispose()

    final_plaintext = asyncio.run(read_final_plaintext())
    assert final_plaintext in replacements
    assert final_plaintext != old_plaintext
