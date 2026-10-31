"""AES-256-GCM envelopes and fail-closed Master Keyring loading."""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Final, Never
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from holm_common import SecretFileError, read_secret_file

AES_256_KEY_BYTES: Final[int] = 32
GCM_NONCE_BYTES: Final[int] = 12
GCM_TAG_BYTES: Final[int] = 16
KEYRING_FORMAT_VERSION: Final[int] = 1
# 存储格式标签，改名会让既有数据失效
_AAD_PREFIX: Final[bytes] = b"privatews:model-source-credential:v1\x00"
_KEY_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class CredentialCryptoError(RuntimeError):
    """Sanitized credential-cryptography failure with a stable code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"credential cryptography rejected: {code}")


class CredentialDecryptionError(CredentialCryptoError):
    """An envelope cannot be authenticated under the caller-supplied identity."""

    def __init__(self) -> None:
        super().__init__("decryption_failed")


class KeyringError(CredentialCryptoError):
    """The Master Keyring is missing, malformed, ambiguous, or unsafe."""


class MasterKeyState(StrEnum):
    """Allowed Key states; unknown values never degrade to a default."""

    WRITABLE = "writable"
    DECRYPT_ONLY = "decrypt_only"
    DISABLED = "disabled"


class MasterKeyRotationPhase(StrEnum):
    """Four ordered credential rotation stages; progress is derived, never stored here."""

    NEW_KEY_WRITES = "new_key_writes"
    REENCRYPTING = "reencrypting"
    OLD_KEY_READ_ONLY = "old_key_read_only"
    COMPLETE = "complete"


ROTATION_TRANSITIONS: Final[Mapping[MasterKeyRotationPhase, MasterKeyRotationPhase]] = (
    MappingProxyType(
        {
            MasterKeyRotationPhase.NEW_KEY_WRITES: MasterKeyRotationPhase.REENCRYPTING,
            MasterKeyRotationPhase.REENCRYPTING: MasterKeyRotationPhase.OLD_KEY_READ_ONLY,
            MasterKeyRotationPhase.OLD_KEY_READ_ONLY: MasterKeyRotationPhase.COMPLETE,
        }
    )
)


@dataclass(frozen=True, slots=True)
class CredentialEnvelope:
    """The four independently persisted fields."""

    key_id: str
    nonce: bytes = field(repr=False)
    ciphertext: bytes = field(repr=False)
    tag: bytes = field(repr=False)

    def __post_init__(self) -> None:
        _validate_key_id(self.key_id)
        if len(self.nonce) != GCM_NONCE_BYTES:
            raise CredentialCryptoError("invalid_nonce")
        if not self.ciphertext:
            raise CredentialCryptoError("empty_ciphertext")
        if len(self.tag) != GCM_TAG_BYTES:
            raise CredentialCryptoError("invalid_tag")


@dataclass(frozen=True, slots=True)
class MasterKey:
    """One Keyring entry; material is intentionally absent from repr."""

    key_id: str
    state: MasterKeyState
    material: bytes = field(repr=False)

    def __post_init__(self) -> None:
        _validate_key_id(self.key_id)
        if not isinstance(self.state, MasterKeyState):
            raise KeyringError("invalid_key_state")
        if not isinstance(self.material, bytes) or len(self.material) != AES_256_KEY_BYTES:
            raise KeyringError("invalid_key_length")


def _validate_key_id(key_id: str) -> str:
    if not isinstance(key_id, str) or _KEY_ID_PATTERN.fullmatch(key_id) is None:
        raise KeyringError("invalid_key_id")
    return key_id


def _frame(value: bytes) -> bytes:
    return len(value).to_bytes(4, "big") + value


def credential_aad(*, source_id: UUID, credential_type: str, key_id: str) -> bytes:
    """Bind an envelope to identity supplied by its caller, not its storage row."""

    if not isinstance(source_id, UUID):
        raise CredentialCryptoError("invalid_source_id")
    if not isinstance(credential_type, str) or not credential_type:
        raise CredentialCryptoError("invalid_credential_type")
    encoded_type = credential_type.encode("utf-8")
    encoded_key_id = _validate_key_id(key_id).encode("ascii")
    return _AAD_PREFIX + _frame(source_id.bytes) + _frame(encoded_type) + _frame(encoded_key_id)


class MasterKeyring:
    """Immutable Keyring with exactly one Key authorized for new encryption."""

    __slots__ = ("_keys", "_writable_key_id")

    def __init__(self, keys: tuple[MasterKey, ...]) -> None:
        by_id: dict[str, MasterKey] = {}
        for key in keys:
            if key.key_id in by_id:
                raise KeyringError("duplicate_key_id")
            by_id[key.key_id] = key
        writable = tuple(
            key.key_id for key in by_id.values() if key.state is MasterKeyState.WRITABLE
        )
        if len(writable) != 1:
            raise KeyringError("writable_key_count")
        self._keys = MappingProxyType(by_id)
        self._writable_key_id = writable[0]

    @property
    def writable_key_id(self) -> str:
        return self._writable_key_id

    @property
    def key_states(self) -> Mapping[str, MasterKeyState]:
        """Expose immutable Key metadata without exposing any Key material."""

        return MappingProxyType({key_id: key.state for key_id, key in self._keys.items()})

    def can_decrypt(self, key_id: str) -> bool:
        key = self._keys.get(key_id)
        return key is not None and key.state in {
            MasterKeyState.WRITABLE,
            MasterKeyState.DECRYPT_ONLY,
        }

    def encrypt(
        self,
        plaintext: bytes,
        *,
        source_id: UUID,
        credential_type: str,
        key_id: str | None = None,
    ) -> CredentialEnvelope:
        """Encrypt under the sole writable Key, or reject an explicit non-writable Key."""

        selected_id = self._writable_key_id if key_id is None else _validate_key_id(key_id)
        selected = self._keys.get(selected_id)
        if selected is None or selected.state is not MasterKeyState.WRITABLE:
            raise KeyringError("key_not_writable")
        if not isinstance(plaintext, bytes) or not plaintext:
            raise CredentialCryptoError("invalid_plaintext")
        nonce = os.urandom(GCM_NONCE_BYTES)
        combined = AESGCM(selected.material).encrypt(
            nonce,
            plaintext,
            credential_aad(
                source_id=source_id,
                credential_type=credential_type,
                key_id=selected_id,
            ),
        )
        return CredentialEnvelope(
            key_id=selected_id,
            nonce=nonce,
            ciphertext=combined[:-GCM_TAG_BYTES],
            tag=combined[-GCM_TAG_BYTES:],
        )

    def decrypt(
        self,
        envelope: CredentialEnvelope,
        *,
        source_id: UUID,
        credential_type: str,
    ) -> bytes:
        """Authenticate and decrypt, mapping every failure to one sanitized error."""

        key = self._keys.get(envelope.key_id)
        if key is None or key.state not in {
            MasterKeyState.WRITABLE,
            MasterKeyState.DECRYPT_ONLY,
        }:
            raise CredentialDecryptionError()
        try:
            return AESGCM(key.material).decrypt(
                envelope.nonce,
                envelope.ciphertext + envelope.tag,
                credential_aad(
                    source_id=source_id,
                    credential_type=credential_type,
                    key_id=envelope.key_id,
                ),
            )
        except (InvalidTag, ValueError, TypeError):
            raise CredentialDecryptionError() from None

    def require_key_can_be_disabled(
        self,
        key_id: str,
        *,
        referenced_rows: int,
        scanned_rows: int,
    ) -> None:
        """Fail closed unless a real non-empty database scan proves zero references."""

        selected = self._keys.get(_validate_key_id(key_id))
        if selected is None:
            raise KeyringError("unknown_key")
        if selected.state is MasterKeyState.WRITABLE:
            raise KeyringError("writable_key_cannot_be_disabled")
        if scanned_rows <= 0:
            raise KeyringError("reference_scan_empty")
        if referenced_rows < 0 or referenced_rows > scanned_rows:
            raise KeyringError("invalid_reference_count")
        if referenced_rows:
            raise KeyringError("key_still_referenced")

    def begin_rotation(self, old_key_id: str) -> MasterKeyRotationPhase:
        """Validate the Keyring stage that directs all new writes to the new Key."""

        old_key = self._keys.get(_validate_key_id(old_key_id))
        if old_key is None:
            raise KeyringError("unknown_key")
        if (
            old_key.key_id == self._writable_key_id
            or old_key.state is not MasterKeyState.DECRYPT_ONLY
        ):
            raise KeyringError("rotation_key_state")
        return MasterKeyRotationPhase.NEW_KEY_WRITES

    def rotation_phase_from_references(
        self,
        old_key_id: str,
        *,
        referenced_rows: int,
        scanned_rows: int,
    ) -> MasterKeyRotationPhase:
        """Derive restart-safe progress only from Key state and real row references."""

        old_key = self._keys.get(_validate_key_id(old_key_id))
        if old_key is None:
            raise KeyringError("unknown_key")
        if scanned_rows <= 0:
            raise KeyringError("reference_scan_empty")
        if referenced_rows < 0 or referenced_rows > scanned_rows:
            raise KeyringError("invalid_reference_count")
        if old_key.state is MasterKeyState.DECRYPT_ONLY:
            if referenced_rows:
                return MasterKeyRotationPhase.REENCRYPTING
            return MasterKeyRotationPhase.OLD_KEY_READ_ONLY
        if old_key.state is MasterKeyState.DISABLED and referenced_rows == 0:
            return MasterKeyRotationPhase.COMPLETE
        if old_key.state is MasterKeyState.DISABLED:
            raise KeyringError("disabled_key_still_referenced")
        raise KeyringError("rotation_key_state")


def require_rotation_transition(
    current: MasterKeyRotationPhase,
    following: MasterKeyRotationPhase,
) -> None:
    """Reject skips, reversals, and transitions from the terminal stage."""

    if ROTATION_TRANSITIONS.get(current) is not following:
        raise KeyringError("invalid_rotation_transition")


def _fail_keyring(code: str) -> Never:
    raise KeyringError(code)


def _parse_master_key(raw: object) -> MasterKey:
    if not isinstance(raw, Mapping) or set(raw) != {"id", "state", "material"}:
        _fail_keyring("invalid_key_entry")
    key_id = raw["id"]
    state_value = raw["state"]
    material_value = raw["material"]
    if not isinstance(key_id, str) or not isinstance(state_value, str):
        _fail_keyring("invalid_key_entry")
    if not isinstance(material_value, str):
        _fail_keyring("invalid_key_material")
    try:
        state = MasterKeyState(state_value)
        material = base64.b64decode(material_value, validate=True)
    except (ValueError, binascii.Error):
        _fail_keyring("invalid_key_entry")
    return MasterKey(key_id=key_id, state=state, material=material)


def load_master_keyring(
    path: str | Path,
    *,
    expected_gid: int | None = None,
) -> MasterKeyring:
    """Load a closed JSON Keyring only through the repository Secret reader."""

    try:
        payload = read_secret_file(
            path,
            expected_uid=0,
            expected_gid=expected_gid,
        )
    except SecretFileError as error:
        raise KeyringError(f"secret_file_{error.code}") from None
    try:
        raw = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        _fail_keyring("invalid_keyring_document")
    if not isinstance(raw, Mapping) or set(raw) != {"version", "keys"}:
        _fail_keyring("invalid_keyring_document")
    if raw["version"] != KEYRING_FORMAT_VERSION or not isinstance(raw["keys"], list):
        _fail_keyring("invalid_keyring_document")
    return MasterKeyring(tuple(_parse_master_key(item) for item in raw["keys"]))


__all__ = [
    "AES_256_KEY_BYTES",
    "GCM_NONCE_BYTES",
    "GCM_TAG_BYTES",
    "CredentialCryptoError",
    "CredentialDecryptionError",
    "CredentialEnvelope",
    "KeyringError",
    "MasterKey",
    "MasterKeyRotationPhase",
    "MasterKeyState",
    "MasterKeyring",
    "ROTATION_TRANSITIONS",
    "credential_aad",
    "load_master_keyring",
    "require_rotation_transition",
]
