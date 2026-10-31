"""M7-08A / TC-DP-12: isolated restore against real PostgreSQL and SQLite WAL."""

from __future__ import annotations

import base64
import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from conftest import EphemeralPostgres
from holm_domain import (
    ArtifactManifestEntry,
    build_artifact_manifest,
    serialize_artifact_manifest,
)
from holm_ledger.models import ReceivedEvent, RequestOperation, RequestProtocol
from holm_ledger.store import ConnectionRole, SQLiteLedger
from holm_persistence.backup_restore import (
    BackupError,
    PostgresTarget,
    create_backup,
    restore_backup,
)
from holm_security.api_keys import (
    ApiKeyPepperKeyring,
    issue_api_key,
    load_api_key_pepper_keyring,
    parse_api_key,
)
from holm_security.backup import BackupCryptoError
from holm_security.passwords import Argon2idParameters, PasswordHasher
from migration_test_support import upgrade_head
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL


def _private_file(path: Path, data: bytes) -> None:
    path.write_bytes(data)
    path.chmod(0o600)


def test_M7_08A_TC_DP_12_isolated_restore_and_prewrite_guards(
    ephemeral_postgres: EphemeralPostgres, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # FX-M7-08A-01: task-owned PostgreSQL cluster, WAL spool, and private directories.
    upgrade_head(ephemeral_postgres.engine)
    root = tmp_path / "source"
    root.mkdir(mode=0o700)
    config = root / "config"
    secrets = root / "secrets"
    config.mkdir(mode=0o700)
    secrets.mkdir(mode=0o700)
    backup_key = tmp_path / "backup.key"
    _private_file(backup_key, os.urandom(32))
    pepper = os.urandom(32)
    pepper_doc = json.dumps(
        {"current_key_id": "pepper-1", "keys": {"pepper-1": base64.b64encode(pepper).decode()}}
    ).encode()
    _private_file(secrets / "pepper.json", pepper_doc)
    _private_file(secrets / "master.json", b"master-keyring-private-material")
    _private_file(secrets / "database-dsn", b"postgresql-private-dsn")
    _private_file(config / "settings.json", b'{"private":true}')
    (config / "nested").mkdir(mode=0o700)
    _private_file(config / "nested" / "more.json", b'{"nested":true}')
    keyring = ApiKeyPepperKeyring(current_key_id="pepper-1", keys={"pepper-1": pepper})
    issued = issue_api_key(keyring)
    hasher = PasswordHasher(Argon2idParameters(memory_cost_kib=19456, time_cost=2))
    password = "M7-08A-correct-password"
    user, model, artifact, external_artifact, instance, service, key_id, request = (
        uuid4() for _ in range(8)
    )
    now = datetime.now(UTC)
    manifest_bytes = serialize_artifact_manifest(
        build_artifact_manifest((ArtifactManifestEntry("weights.safetensors", 12, "a" * 64),))
    )
    artifact_dir = root / "artifact"
    artifact_dir.mkdir()
    (artifact_dir / "manifest.json").write_bytes(manifest_bytes)
    bundle_dir = root / "bundle"
    bundle_dir.mkdir()
    (bundle_dir / "manifest.json").write_bytes(manifest_bytes)
    with ephemeral_postgres.engine.begin() as connection:
        connection.execute(
            text("""INSERT INTO core.users
            (id, username_norm, display_name, role, status, password_hash,
             must_change_password, created_at, updated_at)
            VALUES (:id, 'restore-user', 'Restore User', 'admin', 'active', :hash,
                    false, :now, :now)"""),
            {"id": user, "hash": hasher.hash(password), "now": now},
        )
        connection.execute(
            text("""INSERT INTO core.models
            (id, source_type, source_key, display_name, external_path, modalities,
             supported_runtimes, metadata_status, created_at, updated_at)
            VALUES (:id, 'Local Path', '/models/restore', 'Restore Model', '/models/restore',
                    '[]'::jsonb, '[]'::jsonb, 'CONFIRMED', :now, :now)"""),
            {"id": model, "now": now},
        )
        connection.execute(
            text("""INSERT INTO core.model_artifacts
            (id, model_id, source_type, repository_id, resolved_revision, storage_type,
             status, path, manifest_hash, tokenizer_bundle_hash, tokenizer_bundle_path,
             size_bytes, created_at, updated_at)
            VALUES (:id, :model, 'Local Path', '/models/restore', 'revision1', 'Managed',
                    'Ready', '/managed/model', :hash, :hash, '/managed/bundle',
                    12, :now, :now)"""),
            {"id": artifact, "model": model, "hash": "a" * 64, "now": now},
        )
        connection.execute(
            text("""INSERT INTO core.model_artifacts
            (id, model_id, source_type, repository_id, resolved_revision, storage_type,
             status, path, manifest_hash, size_bytes, created_at, updated_at)
            VALUES (:id, :model, 'Local Path', '/models/restore', 'revision2', 'External',
                    'Ready', '/external/model', :hash, 12, :now, :now)"""),
            {"id": external_artifact, "model": model, "hash": "b" * 64, "now": now},
        )
        connection.execute(
            text("""INSERT INTO core.model_instances
            (id, artifact_id, runtime_type, expected_gpu_uuids, tensor_parallel_size,
             context_length, gpu_memory_utilization, name, internal_model_name, status,
             config_version, created_at, updated_at)
            VALUES (:id, :artifact, 'vllm', '["GPU-test"]'::jsonb, 1, 4096, 0.8,
                    'FX backup instance', 'mi_abcdef123456', 'Stopped', 1, :now, :now)"""),
            {"id": instance, "artifact": artifact, "now": now},
        )
        connection.execute(
            text("""INSERT INTO core.ai_services
            (id, name, public_model_name, description, enabled,
             route_version, created_at, updated_at)
            VALUES (:id, 'restore-model', 'restore-model', '', true, 1, :now, :now)"""),
            {"id": service, "now": now},
        )
        connection.execute(
            text("""INSERT INTO core.ai_service_backends
            (id, service_id, instance_id, instance_name_snapshot, runtime_snapshot,
             backend_key, weight, enabled, health_gate_passed, joined_at)
            VALUES (:id, :service, :instance, 'mi_abcdef123456', 'vllm',
                    'primary', 100, true, false, :now)"""),
            {"id": uuid4(), "service": service, "instance": instance, "now": now},
        )
        connection.execute(
            text("""INSERT INTO core.user_service_permissions
            (user_id, service_id, created_at, created_by)
            VALUES (:user, :service, :now, :user)"""),
            {"user": user, "service": service, "now": now},
        )
        connection.execute(
            text("""INSERT INTO core.api_keys
            (id, user_id, name, lookup_id, display_prefix, last4, pepper_key_id,
             secret_hmac, status, created_at)
            VALUES (:id, :user, 'restore-key', :lookup, :prefix, :last4, :pepper,
                    :hmac, 'Active', :now)"""),
            {
                "id": key_id,
                "user": user,
                "lookup": issued.lookup_id,
                "prefix": issued.display_prefix,
                "last4": issued.last4,
                "pepper": issued.pepper_key_id,
                "hmac": issued.secret_hmac,
                "now": now,
            },
        )
        connection.execute(
            text("""INSERT INTO usage.usage_15m
            (bucket_start, user_id, api_key_id, service_id, requests, input_tokens,
             output_tokens, error_requests, aborted_requests, partial_token_requests, updated_at)
            VALUES (date_bin(INTERVAL '15 minutes', :now, TIMESTAMPTZ '1970-01-01'),
                    :user, :key, :service, 1, 7, 11, 0, 0, 0, :now)"""),
            {"now": now, "user": user, "key": key_id, "service": service},
        )
        connection.execute(
            text("""SELECT core.holm_ensure_month_partition(
            'usage.request_records', date_trunc('month', :now)::date)"""),
            {"now": now},
        )
        connection.execute(
            text("""INSERT INTO usage.request_records
            (received_at, request_id, event_seq, record_state, user_id, api_key_id,
             service_id, input_tokens, output_tokens, updated_at)
            VALUES (:now, :request, 2, 'completed', :user, :key, :service, 7, 11, :now)"""),
            {"now": now, "request": request, "user": user, "key": key_id, "service": service},
        )
    spool_dir = root / "spool"
    spool_dir.mkdir(mode=0o700)
    spool = spool_dir / "events.sqlite3"
    ledger = SQLiteLedger.open(spool, ConnectionRole.GATEWAY_SPOOL_WRITER)
    ledger.append(
        ReceivedEvent(
            request_id=request,
            event_seq=1,
            created_at=now,
            received_at=now,
            protocol=RequestProtocol.OPENAI,
            operation=RequestOperation.CHAT_COMPLETIONS,
            endpoint="/v1/chat/completions",
            stream=False,
            input_bytes=25,
        )
    )
    assert Path(f"{spool}-wal").stat().st_size > 0
    target = PostgresTarget(
        str(ephemeral_postgres.socket_directory), ephemeral_postgres.port, "postgres", "ubuntu"
    )
    bundle = tmp_path / "backup"
    try:
        with pytest.raises(BackupError, match="artifact_manifest_set_mismatch"):
            create_backup(
                destination=tmp_path / "external-attempt",
                postgres=target,
                config_dir=config,
                secret_dir=secrets,
                spool=spool,
                artifact_manifests={
                    str(artifact): artifact_dir / "manifest.json",
                    f"bundle-{artifact}": bundle_dir / "manifest.json",
                    str(external_artifact): artifact_dir / "manifest.json",
                },
                backup_key=backup_key,
                release="test-release",
            )
        create_backup(
            destination=bundle,
            postgres=target,
            config_dir=config,
            secret_dir=secrets,
            spool=spool,
            artifact_manifests={
                str(artifact): artifact_dir / "manifest.json",
                f"bundle-{artifact}": bundle_dir / "manifest.json",
            },
            backup_key=backup_key,
            release="test-release",
        )
    finally:
        ledger.close()
    sentinel = b"BODY-SENTINEL-M7-08A"
    for item in bundle.iterdir():
        assert sentinel not in item.read_bytes()
        assert pepper not in item.read_bytes()
        assert backup_key.read_bytes() not in item.read_bytes()
        assert item.stat().st_mode & 0o077 == 0
    assert b"database-dsn" not in (bundle / "secrets.enc").read_bytes()
    assert (bundle / "secrets.enc").stat().st_mode & 0o777 == 0o400
    assert not any("backup.key" in record.message for record in caplog.records)
    with ephemeral_postgres.engine.connect().execution_options(
        isolation_level="AUTOCOMMIT"
    ) as connection:
        connection.execute(text("CREATE DATABASE restore_target"))
    restored_db = PostgresTarget(
        str(ephemeral_postgres.socket_directory),
        ephemeral_postgres.port,
        "restore_target",
        "ubuntu",
    )
    restored_root = tmp_path / "restored"
    restored_config = restored_root / "config"
    restored_secrets = restored_root / "secrets"
    restored_spool = restored_root / "spool" / "events.sqlite3"
    wrong_key = tmp_path / "wrong.key"
    _private_file(wrong_key, os.urandom(32))
    with pytest.raises(BackupCryptoError):
        restore_backup(
            source=bundle,
            postgres=restored_db,
            config_dir=restored_config,
            secret_dir=restored_secrets,
            spool=restored_spool,
            backup_key=wrong_key,
        )
    assert not restored_root.exists()
    invalid = tmp_path / "invalid"
    shutil.copytree(bundle, invalid)
    (invalid / "artifacts.json").write_bytes(b"corrupt")
    with pytest.raises(BackupError, match="backup_hash_mismatch"):
        restore_backup(
            source=invalid,
            postgres=restored_db,
            config_dir=restored_config,
            secret_dir=restored_secrets,
            spool=restored_spool,
            backup_key=backup_key,
        )
    assert not restored_root.exists()
    (invalid / "artifacts.json").unlink()
    with pytest.raises(BackupError, match="unexpected_backup_items"):
        restore_backup(
            source=invalid,
            postgres=restored_db,
            config_dir=restored_config,
            secret_dir=restored_secrets,
            spool=restored_spool,
            backup_key=backup_key,
        )
    assert not restored_root.exists()
    stages: list[str] = []
    result = restore_backup(
        source=bundle,
        postgres=restored_db,
        config_dir=restored_config,
        secret_dir=restored_secrets,
        spool=restored_spool,
        backup_key=backup_key,
        after_database=lambda: stages.append("platform"),
    )
    assert stages == ["platform"]
    assert result.artifact_manifests[str(artifact)] == manifest_bytes
    assert result.artifact_manifests[f"bundle-{artifact}"] == manifest_bytes
    assert str(external_artifact) not in result.artifact_manifests
    assert (restored_config / "nested" / "more.json").read_bytes() == b'{"nested":true}'
    assert (restored_secrets / "pepper.json").stat().st_mode & 0o777 == 0o600
    restored_keyring = load_api_key_pepper_keyring(restored_secrets / "pepper.json")
    parsed = parse_api_key(issued.plaintext)
    url = URL.create(
        "postgresql+psycopg",
        username="ubuntu",
        host=str(ephemeral_postgres.socket_directory),
        port=ephemeral_postgres.port,
        database="restore_target",
    )
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            row = connection.execute(
                text("""SELECT password_hash, role FROM core.users
                WHERE id = :id"""),
                {"id": user},
            ).one()
            assert hasher.verify(row.password_hash, password) and row.role == "admin"
            assert (
                connection.execute(
                    text("""SELECT count(*) FROM core.user_service_permissions
                WHERE user_id = :user AND service_id = :service"""),
                    {"user": user, "service": service},
                ).scalar_one()
                == 1
            )
            saved_hmac = connection.execute(
                text("SELECT secret_hmac FROM core.api_keys WHERE id=:id"), {"id": key_id}
            ).scalar_one()
            assert restored_keyring.matches(
                parsed.secret, key_id="pepper-1", expected_hmac=saved_hmac
            )
            assert (
                connection.execute(
                    text("SELECT status FROM core.model_artifacts WHERE id=:id"), {"id": artifact}
                ).scalar_one()
                == "Ready"
            )
            assert (
                connection.execute(
                    text("SELECT sum(input_tokens + output_tokens) FROM usage.usage_15m")
                ).scalar_one()
                == 18
            )
            assert (
                connection.execute(
                    text("SELECT count(*) FROM usage.request_records WHERE request_id=:id"),
                    {"id": request},
                ).scalar_one()
                == 1
            )
    finally:
        engine.dispose()
    with SQLiteLedger.open(restored_spool, ConnectionRole.READER) as ledger:
        assert ledger.list_unacked()[0].event.request_id == request
    with pytest.raises(BackupError, match="nonempty_restore_target"):
        restore_backup(
            source=bundle,
            postgres=restored_db,
            config_dir=restored_config,
            secret_dir=restored_secrets,
            spool=restored_spool,
            backup_key=backup_key,
        )
    restore_backup(
        source=bundle,
        postgres=restored_db,
        config_dir=restored_config,
        secret_dir=restored_secrets,
        spool=restored_spool,
        backup_key=backup_key,
        confirm_nonempty=True,
    )
    with SQLiteLedger.open(restored_spool, ConnectionRole.READER) as ledger:
        assert ledger.list_unacked()[0].event.request_id == request
