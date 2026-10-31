"""Offline, metadata-only backup and restore for one installation.

The caller must stop platform writers before restore and restart components only
after the database step. PostgreSQL commands run under the caller's DDL owner
identity; only a restricted passfile path is inherited by libpq.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
import stat
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import psycopg
from holm_domain import deserialize_artifact_manifest
from holm_security.backup import open_backup_secrets, seal_backup_secrets
from psycopg import sql

from holm_persistence.backup_format import (
    BACKUP_FILES as _FILES,
)
from holm_persistence.backup_format import (
    BACKUP_FORMAT_VERSION as _FORMAT,
)
from holm_persistence.backup_format import (
    BACKUP_NAME as _NAME,
)
from holm_persistence.backup_format import (
    BackupError as BackupError,
)
from holm_persistence.backup_format import (
    validate_backup_manifest,
)

_MAX_SECRET_BYTES: Final = 8 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class PostgresTarget:
    host: str
    port: int
    database: str
    ddl_owner: str
    passfile: Path | None = None

    def __post_init__(self) -> None:
        if not self.host or not self.database or not self.ddl_owner or not 1 <= self.port <= 65535:
            raise BackupError("invalid_postgres_target")

    def connection(self) -> psycopg.Connection[Any]:
        return psycopg.connect(
            host=self.host,
            port=self.port,
            dbname=self.database,
            user=self.ddl_owner,
            passfile=str(self.passfile) if self.passfile is not None else None,
            connect_timeout=5,
        )

    def command_args(self) -> list[str]:
        return [
            "--host",
            self.host,
            "--port",
            str(self.port),
            "--username",
            self.ddl_owner,
            "--dbname",
            self.database,
            "--no-password",
        ]

    def command_env(self) -> dict[str, str]:
        env = {"PATH": os.environ.get("PATH", ""), "LANG": "C", "PGCONNECT_TIMEOUT": "5"}
        if self.passfile is not None:
            env["PGPASSFILE"] = str(self.passfile)
        return env


@dataclass(frozen=True, slots=True)
class RestoreResult:
    artifact_manifests: Mapping[str, bytes]
    migration_revision: str


def _run(arguments: list[str], target: PostgresTarget) -> str:
    try:
        completed = subprocess.run(
            arguments,
            env=target.command_env(),
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise BackupError("postgres_command_unavailable") from None
    if completed.returncode:
        # pg tools can echo connection strings and credentials in stderr.
        raise BackupError("postgres_command_failed")
    return completed.stdout


def _regular(path: Path) -> None:
    try:
        mode = path.lstat().st_mode
    except OSError:
        raise BackupError("missing_item") from None
    if not stat.S_ISREG(mode) or stat.S_ISLNK(mode):
        raise BackupError("unsafe_item")


def _write_private(path: Path, content: bytes, *, mode: int = 0o400) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(content)
    path.chmod(mode)


def _hash(path: Path) -> str:
    _regular(path)
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _relative_secret_name(path: Path) -> str:
    name = path.as_posix()
    if not path.parts or any(_NAME.fullmatch(part) is None for part in path.parts):
        raise BackupError("unsafe_secret_name")
    return name


def _secret_archive(config_dir: Path, secret_dir: Path) -> bytes:
    result: dict[str, dict[str, dict[str, str | int]]] = {}
    for label, root in (("config", config_dir), ("secrets", secret_dir)):
        if not root.is_dir() or root.is_symlink() or stat.S_IMODE(root.stat().st_mode) != 0o700:
            raise BackupError("invalid_secret_directory")
        files: dict[str, dict[str, str | int]] = {}
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                raise BackupError("unsafe_secret_item")
            name = _relative_secret_name(path.relative_to(root))
            if path.is_dir():
                if stat.S_IMODE(path.stat().st_mode) != 0o700:
                    raise BackupError("unsafe_secret_mode")
                continue
            if not path.is_file():
                raise BackupError("unsafe_secret_item")
            mode = stat.S_IMODE(path.stat().st_mode)
            if mode not in {0o400, 0o600}:
                raise BackupError("unsafe_secret_mode")
            raw = path.read_bytes()
            if len(raw) > _MAX_SECRET_BYTES:
                raise BackupError("secret_item_too_large")
            files[name] = {"content": base64.b64encode(raw).decode("ascii"), "mode": mode}
        result[label] = files
    return json.dumps(result, sort_keys=True, separators=(",", ":")).encode()


def _decode_secrets(raw: bytes) -> dict[str, dict[str, tuple[bytes, int]]]:
    try:
        value = json.loads(raw)
        if not isinstance(value, dict) or set(value) != {"config", "secrets"}:
            raise ValueError
        result: dict[str, dict[str, tuple[bytes, int]]] = {}
        for label, files in value.items():
            if not isinstance(files, dict):
                raise ValueError
            decoded: dict[str, tuple[bytes, int]] = {}
            for name, item in files.items():
                if (
                    not isinstance(name, str)
                    or _relative_secret_name(Path(name)) != name
                    or not isinstance(item, dict)
                    or set(item) != {"content", "mode"}
                    or not isinstance(item["content"], str)
                    or item["mode"] not in {0o400, 0o600}
                ):
                    raise ValueError
                content = base64.b64decode(item["content"], validate=True)
                if len(content) > _MAX_SECRET_BYTES:
                    raise ValueError
                decoded[name] = (content, item["mode"])
            result[label] = decoded
        return result
    except (ValueError, TypeError, KeyError):
        raise BackupError("invalid_secret_archive") from None


def _spool_backup(source: Path, destination: Path) -> None:
    _regular(source)
    source_uri = f"file:{source}?mode=ro"
    with sqlite3.connect(source_uri, uri=True) as reader, sqlite3.connect(destination) as writer:
        reader.backup(writer)
    destination.chmod(0o400)
    _verify_spool(destination)


def _verify_spool(path: Path) -> None:
    _regular(path)
    try:
        with sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True) as connection:
            if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise BackupError("invalid_spool")
            if connection.execute("PRAGMA user_version").fetchone() != (1,):
                raise BackupError("invalid_spool")
    except sqlite3.Error:
        raise BackupError("invalid_spool") from None


def _artifact_archive(paths: Mapping[str, Path]) -> bytes:
    result: dict[str, str] = {}
    for name, path in sorted(paths.items()):
        if _NAME.fullmatch(name) is None or path.name != "manifest.json":
            raise BackupError("invalid_artifact_manifest")
        _regular(path)
        raw = path.read_bytes()
        try:
            deserialize_artifact_manifest(raw)
        except (ValueError, RuntimeError):
            raise BackupError("invalid_artifact_manifest") from None
        result[name] = base64.b64encode(raw).decode("ascii")
    return json.dumps(result, sort_keys=True, separators=(",", ":")).encode()


def _decode_artifacts(raw: bytes) -> dict[str, bytes]:
    try:
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError
        result: dict[str, bytes] = {}
        for name, encoded in value.items():
            if (
                not isinstance(name, str)
                or _NAME.fullmatch(name) is None
                or not isinstance(encoded, str)
            ):
                raise ValueError
            content = base64.b64decode(encoded, validate=True)
            deserialize_artifact_manifest(content)
            result[name] = content
        return result
    except (ValueError, TypeError, RuntimeError):
        raise BackupError("invalid_artifact_manifest") from None


def create_backup(
    *,
    destination: Path,
    postgres: PostgresTarget,
    config_dir: Path,
    secret_dir: Path,
    spool: Path,
    artifact_manifests: Mapping[str, Path],
    backup_key: Path,
    release: str,
) -> None:
    """Create one private backup directory. Existing destinations are never reused."""
    key_location = backup_key.resolve()
    if not release or any(
        key_location.is_relative_to(path.resolve())
        for path in (destination, config_dir, secret_dir)
    ):
        raise BackupError("invalid_backup_configuration")
    destination.mkdir(mode=0o700)
    if stat.S_IMODE(destination.stat().st_mode) != 0o700:
        raise BackupError("unsafe_backup_directory")
    with postgres.connection() as connection:
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        artifact_rows = connection.execute(
            "SELECT id, storage_type, tokenizer_bundle_path FROM core.model_artifacts"
        ).fetchall()
    if revision is None or not isinstance(revision[0], str):
        raise BackupError("migration_revision_unavailable")
    required_manifests: set[str] = set()
    for artifact_id, storage_type, bundle_path in artifact_rows:
        if storage_type == "External":
            continue
        if storage_type != "Managed":
            raise BackupError("unknown_artifact_storage_type")
        required_manifests.add(str(artifact_id))
        if bundle_path is not None:
            required_manifests.add(f"bundle-{artifact_id}")
    if set(artifact_manifests) != required_manifests:
        raise BackupError("artifact_manifest_set_mismatch")
    dump = destination / "database.dump"
    _run(["pg_dump", *postgres.command_args(), "--format=custom", "--file", str(dump)], postgres)
    dump.chmod(0o400)
    if not _run(["pg_restore", "--list", str(dump)], postgres).strip():
        raise BackupError("invalid_database_dump")
    _write_private(
        destination / "secrets.enc",
        seal_backup_secrets(_secret_archive(config_dir, secret_dir), backup_key),
    )
    _spool_backup(spool, destination / "spool.sqlite3")
    _write_private(destination / "artifacts.json", _artifact_archive(artifact_manifests))
    manifest = {
        "format_version": _FORMAT,
        "release": release,
        "migration_revision": revision[0],
        "created_at": datetime.now(UTC).isoformat(),
        "sha256": {name: _hash(destination / name) for name in sorted(_FILES)},
    }
    _write_private(
        destination / "manifest.json",
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode(),
    )


def _validate_backup(
    source: Path, backup_key: Path, postgres: PostgresTarget
) -> tuple[dict[str, Any], dict[str, dict[str, tuple[bytes, int]]], dict[str, bytes]]:
    if (
        source.is_symlink()
        or not source.is_dir()
        or backup_key.resolve().is_relative_to(source.resolve())
    ):
        raise BackupError("invalid_backup_directory")
    manifest = validate_backup_manifest(source)
    if not _run(["pg_restore", "--list", str(source / "database.dump")], postgres).strip():
        raise BackupError("invalid_database_dump")
    secrets = _decode_secrets(
        open_backup_secrets((source / "secrets.enc").read_bytes(), backup_key)
    )
    _verify_spool(source / "spool.sqlite3")
    artifacts = _decode_artifacts((source / "artifacts.json").read_bytes())
    return manifest, secrets, artifacts


def _nonempty_database(target: PostgresTarget) -> bool:
    with target.connection() as connection:
        row = connection.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname NOT IN "
            "('public', 'information_schema') AND left(nspname, 3) <> 'pg_') "
            "OR EXISTS (SELECT 1 FROM pg_class AS c JOIN pg_namespace AS n "
            "ON n.oid = c.relnamespace WHERE n.nspname = 'public' "
            "AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f'))"
        ).fetchone()
        if row is None:
            raise BackupError("database_inspection_failed")
        return bool(row[0])


def _preflight_existing_database(target: PostgresTarget) -> None:
    with target.connection() as connection:
        schemas = {
            row[0]
            for row in connection.execute(
                "SELECT nspname FROM pg_namespace WHERE nspname NOT IN "
                "('public', 'information_schema') AND left(nspname, 3) <> 'pg_'"
            ).fetchall()
        }
        public_relations = {
            row[0]
            for row in connection.execute(
                "SELECT c.relname FROM pg_class AS c JOIN pg_namespace AS n "
                "ON n.oid = c.relnamespace WHERE n.nspname = 'public' "
                "AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')"
            ).fetchall()
        }
    if not schemas <= {"core", "telemetry", "usage"} or public_relations - {"alembic_version"}:
        raise BackupError("unsupported_restore_target")


def _clear_platform_database(target: PostgresTarget) -> None:
    # pg_restore --clean cannot drop inherited constraints on partition children.
    # Drop only the known platform namespaces in one DDL-owner transaction.
    with target.connection() as connection:
        for name in ("usage", "telemetry", "core"):
            connection.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(name))
            )
        connection.execute("DROP TABLE IF EXISTS public.alembic_version CASCADE")


def _preflight_private_directory(path: Path) -> None:
    if (path.exists() or path.is_symlink()) and (
        path.is_symlink() or not path.is_dir() or stat.S_IMODE(path.stat().st_mode) != 0o700
    ):
        raise BackupError("unsafe_restore_directory")


def restore_backup(
    *,
    source: Path,
    postgres: PostgresTarget,
    config_dir: Path,
    secret_dir: Path,
    spool: Path,
    backup_key: Path,
    confirm_nonempty: bool = False,
    after_database: Callable[[], None] | None = None,
) -> RestoreResult:
    """Validate every member before the first write; call the platform hook in order.

    The caller owns component stop/start and post-restore Artifact/Runtime repair.
    """
    manifest, secrets, artifacts = _validate_backup(source, backup_key, postgres)
    roots = (config_dir.resolve(), secret_dir.resolve(), spool.resolve())
    if any(path.is_relative_to(source.resolve()) for path in roots):
        raise BackupError("invalid_restore_target")
    if any(backup_key.resolve().is_relative_to(path) for path in roots[:2]):
        raise BackupError("invalid_restore_target")
    if config_dir.resolve() == secret_dir.resolve():
        raise BackupError("invalid_restore_target")
    for label, target_dir in (("config", config_dir), ("secrets", secret_dir)):
        _preflight_private_directory(target_dir)
        if target_dir.exists() and any(target_dir.iterdir()) and not confirm_nonempty:
            raise BackupError("nonempty_restore_target")
        for name in secrets[label]:
            destination = target_dir / name
            for parent in Path(name).parents:
                if parent != Path("."):
                    _preflight_private_directory(target_dir / parent)
            if destination.exists() or destination.is_symlink():
                _regular(destination)
    if (spool.exists() or spool.is_symlink()) and not confirm_nonempty:
        raise BackupError("nonempty_restore_target")
    if spool.exists() or spool.is_symlink():
        _regular(spool)
    _preflight_private_directory(spool.parent)
    nonempty_database = _nonempty_database(postgres)
    if nonempty_database:
        if not confirm_nonempty:
            raise BackupError("nonempty_restore_target")
        _preflight_existing_database(postgres)
    # Secrets and Pepper must exist before database rows referencing their IDs return.
    for label, directory in (("config", config_dir), ("secrets", secret_dir)):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if stat.S_IMODE(directory.stat().st_mode) != 0o700:
            raise BackupError("unsafe_restore_directory")
        for name, (content, mode) in secrets[label].items():
            destination = directory / name
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if confirm_nonempty and destination.exists():
                destination.unlink()
            _write_private(destination, content, mode=mode)
    if nonempty_database:
        _clear_platform_database(postgres)
    command = [
        "pg_restore",
        *postgres.command_args(),
        "--exit-on-error",
        "--single-transaction",
        "--no-owner",
    ]
    command.append(str(source / "database.dump"))
    _run(command, postgres)
    if after_database is not None:
        after_database()
    spool.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if confirm_nonempty and spool.exists():
        spool.unlink()
    _write_private(spool, (source / "spool.sqlite3").read_bytes(), mode=0o600)
    return RestoreResult(artifacts, manifest["migration_revision"])
