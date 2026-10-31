"""Image-side Secret provisioning; plaintext never crosses stdout."""

from __future__ import annotations

import base64
import json
import os
import secrets
import stat
from collections.abc import Mapping
from pathlib import Path

from holm_security import generate_token
from holm_security.credentials import AES_256_KEY_BYTES, KEYRING_FORMAT_VERSION, MasterKeyState
from sqlalchemy.engine import URL, make_url

from holm_persistence.install_roles import LOGIN_GROUPS


class InstallSecretError(RuntimeError):
    pass


def checked_read(path: Path, uid: int, gid: int, mode: int) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != uid
            or metadata.st_gid != gid
            or stat.S_IMODE(metadata.st_mode) != mode
            or metadata.st_size > 65536
        ):
            raise InstallSecretError("unsafe_existing_secret")
        return os.read(fd, 65537)
    finally:
        os.close(fd)


def ensure_secret(path: Path, content: bytes, uid: int, gid: int, mode: int) -> bool:
    """Existing files are immutable inputs, including during partial-stage resume."""
    if path.exists() or path.is_symlink():
        checked_read(path, uid, gid, mode)
        return False
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        os.fchown(fd, uid, gid)
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb", closefd=False) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(fd)
    finally:
        os.close(fd)
    return True


def provision_secrets(
    root: Path,
    identities: Mapping[str, tuple[int, int]],
    *,
    database: str,
    bootstrap_user: str,
    bootstrap_name: str,
) -> tuple[str, ...]:
    created: list[str] = []

    def put(name: str, content: bytes, identity: tuple[int, int], mode: int = 0o400) -> None:
        if ensure_secret(root / name, content, *identity, mode):
            created.append(name)

    put("postgres_password", secrets.token_urlsafe(32).encode(), identities["postgres"])
    postgres_password = checked_read(
        root / "postgres_password", *identities["postgres"], 0o400
    ).decode()
    url = URL.create(
        "postgresql+psycopg",
        username=bootstrap_user,
        password=postgres_password,
        host="postgres",
        database=database,
    )
    put(
        "migrate_database_url",
        url.render_as_string(hide_password=False).encode(),
        identities["migrate"],
    )
    for secret_name, group in LOGIN_GROUPS.items():
        service = {
            "control": "control-api",
            "download": "download-worker",
            "instance": "instance-controller",
            "ledger": "ledger-writer",
            "maintenance": "maintenance-worker",
        }.get(secret_name.split("_", 1)[0], "gateway")
        login_url = url.set(username=group + "_login", password=secrets.token_urlsafe(32))
        put(
            secret_name,
            login_url.render_as_string(hide_password=False).encode(),
            identities[service],
        )
        observed = make_url(checked_read(root / secret_name, *identities[service], 0o400).decode())
        if (
            observed.username != group + "_login"
            or observed.host != "postgres"
            or observed.database != database
            or not observed.password
        ):
            raise InstallSecretError("invalid_existing_database_secret")
    master = {
        "version": KEYRING_FORMAT_VERSION,
        "keys": [
            {
                "id": "initial",
                "state": MasterKeyState.WRITABLE.value,
                "material": base64.b64encode(secrets.token_bytes(AES_256_KEY_BYTES)).decode(),
            }
        ],
    }
    put("master_keyring", json.dumps(master).encode(), (0, identities["control-api"][1]), 0o440)
    pepper = {
        "current_key_id": "initial",
        "keys": {"initial": base64.b64encode(secrets.token_bytes(32)).decode()},
    }
    # Whichever copy survived a partial stage is authoritative; never mint a new pair.
    first, second = root / "api_key_pepper_keyring", root / "gateway_api_key_pepper_keyring"
    if first.exists():
        material = checked_read(first, *identities["control-api"], 0o600)
    elif second.exists():
        material = checked_read(second, *identities["gateway"], 0o600)
    else:
        material = json.dumps(pepper).encode()
    put(first.name, material, identities["control-api"], 0o600)
    put(second.name, material, identities["gateway"], 0o600)
    if checked_read(first, *identities["control-api"], 0o600) != checked_read(
        second, *identities["gateway"], 0o600
    ):
        raise InstallSecretError("pepper_copies_differ")
    put("idempotency_hmac_secret", secrets.token_bytes(32), identities["control-api"])
    # Production source credentials reside encrypted in PostgreSQL. The legacy
    # Compose placeholder has no consumer and carries no invented JSON format.
    put("model_source_credentials", b"", identities["download-worker"])
    put(bootstrap_name, (generate_token(entropy_bits=256) + "\n").encode(), (0, 0))
    return tuple(created)
