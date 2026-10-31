"""Authenticated backup envelopes with a key separate from platform keyrings."""

from __future__ import annotations

import os
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from holm_common import read_secret_file

# 存储格式标签，改名会让既有数据失效
_AAD = b"privatews:backup-secrets:v1"


class BackupCryptoError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("backup secret authentication failed")


def _key(path: Path) -> bytes:
    key = read_secret_file(path, max_bytes=32, allowed_modes={0o400, 0o600})
    if len(key) != 32:
        raise BackupCryptoError()
    return key


def seal_backup_secrets(plaintext: bytes, key_path: Path) -> bytes:
    nonce = os.urandom(12)
    return b"PWSB1" + nonce + AESGCM(_key(key_path)).encrypt(nonce, plaintext, _AAD)


def open_backup_secrets(envelope: bytes, key_path: Path) -> bytes:
    if not envelope.startswith(b"PWSB1") or len(envelope) < 5 + 12 + 16:
        raise BackupCryptoError()
    try:
        return AESGCM(_key(key_path)).decrypt(envelope[5:17], envelope[17:], _AAD)
    except (InvalidTag, ValueError, TypeError):
        raise BackupCryptoError() from None
