"""TC-SEC-19 AES-GCM identity binding, Keyring, and fail-closed tests."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from holm_common import ConfigLoadError, load_config
from holm_security import (
    CredentialCryptoError,
    CredentialDecryptionError,
    CredentialEnvelope,
    KeyringError,
    MasterKey,
    MasterKeyring,
    MasterKeyRotationPhase,
    MasterKeyState,
    load_master_keyring,
    require_rotation_transition,
)

TOKEN_TYPE = "access_token"


def _material() -> bytes:
    return os.urandom(32)


def _ring() -> MasterKeyring:
    return MasterKeyring(
        (
            MasterKey("key-new", MasterKeyState.WRITABLE, _material()),
            MasterKey("key-old", MasterKeyState.DECRYPT_ONLY, _material()),
        )
    )


def _assert_decryption_fails(
    ring: MasterKeyring,
    envelope: CredentialEnvelope,
    source_id: UUID,
    credential_type: str,
) -> None:
    try:
        ring.decrypt(envelope, source_id=source_id, credential_type=credential_type)
    except CredentialDecryptionError as error:
        assert error.code == "decryption_failed"
        return
    raise AssertionError("invalid envelope did not fail closed")


def _assert_unique_encryptions(envelopes: tuple[CredentialEnvelope, ...]) -> None:
    assert len({item.nonce for item in envelopes}) == len(envelopes)
    assert len({item.ciphertext for item in envelopes}) == len(envelopes)
    assert all(len(item.nonce) == 12 for item in envelopes)


def test_TC_SEC_19_aad_identity_transplants_all_fail_with_one_error() -> None:
    source_a = uuid4()
    source_b = uuid4()
    shared_material = _material()
    ring = MasterKeyring(
        (
            MasterKey("key-new", MasterKeyState.WRITABLE, shared_material),
            MasterKey("key-old", MasterKeyState.DECRYPT_ONLY, shared_material),
        )
    )
    plaintext = os.urandom(41)
    envelope = ring.encrypt(plaintext, source_id=source_a, credential_type=TOKEN_TYPE)

    assert ring.decrypt(envelope, source_id=source_a, credential_type=TOKEN_TYPE) == plaintext
    _assert_decryption_fails(ring, envelope, source_b, TOKEN_TYPE)
    _assert_decryption_fails(ring, envelope, source_a, "not-in-domain-enum")
    _assert_decryption_fails(
        ring,
        CredentialEnvelope(
            key_id="key-old",
            nonce=envelope.nonce,
            ciphertext=envelope.ciphertext,
            tag=envelope.tag,
        ),
        source_a,
        TOKEN_TYPE,
    )


def test_TC_SEC_19_tamper_unknown_key_and_empty_fallback_are_loud(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_id = uuid4()
    ring = _ring()
    envelope = ring.encrypt(os.urandom(29), source_id=source_id, credential_type=TOKEN_TYPE)
    tampered_ciphertext = bytearray(envelope.ciphertext)
    tampered_ciphertext[0] ^= 1
    tampered_tag = bytearray(envelope.tag)
    tampered_tag[-1] ^= 1

    invalid = (
        CredentialEnvelope(
            envelope.key_id,
            envelope.nonce,
            bytes(tampered_ciphertext),
            envelope.tag,
        ),
        CredentialEnvelope(
            envelope.key_id,
            envelope.nonce,
            envelope.ciphertext,
            bytes(tampered_tag),
        ),
        CredentialEnvelope(
            "key-unknown",
            envelope.nonce,
            envelope.ciphertext,
            envelope.tag,
        ),
    )
    for candidate in invalid:
        _assert_decryption_fails(ring, candidate, source_id, TOKEN_TYPE)

    monkeypatch.setattr(MasterKeyring, "decrypt", lambda *args, **kwargs: b"")
    with pytest.raises(AssertionError, match="fail closed"):
        _assert_decryption_fails(ring, invalid[0], source_id, TOKEN_TYPE)


def test_TC_SEC_19_random_nonce_distribution_and_fixed_nonce_positive_control(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_id = uuid4()
    ring = _ring()
    plaintext = os.urandom(37)
    envelopes = tuple(
        ring.encrypt(plaintext, source_id=source_id, credential_type=TOKEN_TYPE) for _ in range(64)
    )
    _assert_unique_encryptions(envelopes)

    monkeypatch.setattr("holm_security.credentials.os.urandom", lambda size: b"N" * size)
    fixed = tuple(
        ring.encrypt(plaintext, source_id=source_id, credential_type=TOKEN_TYPE) for _ in range(2)
    )
    with pytest.raises(AssertionError):
        _assert_unique_encryptions(fixed)


def test_TC_SEC_19_keyring_rejects_bad_counts_lengths_states_and_read_only_write() -> None:
    with pytest.raises(KeyringError, match="writable_key_count"):
        MasterKeyring((MasterKey("only-old", MasterKeyState.DECRYPT_ONLY, _material()),))
    with pytest.raises(KeyringError, match="writable_key_count"):
        MasterKeyring(
            (
                MasterKey("one", MasterKeyState.WRITABLE, _material()),
                MasterKey("two", MasterKeyState.WRITABLE, _material()),
            )
        )
    for size in (31, 33):
        with pytest.raises(KeyringError, match="invalid_key_length"):
            MasterKey("bad", MasterKeyState.WRITABLE, os.urandom(size))
    with pytest.raises(KeyringError, match="invalid_key_state"):
        MasterKey("bad", "future-state", _material())  # type: ignore[arg-type]
    with pytest.raises(KeyringError, match="invalid_key_length"):
        MasterKey("bad", MasterKeyState.WRITABLE, bytearray(_material()))  # type: ignore[arg-type]

    ring = _ring()
    with pytest.raises(KeyringError, match="key_not_writable"):
        ring.encrypt(
            os.urandom(20),
            source_id=uuid4(),
            credential_type=TOKEN_TYPE,
            key_id="key-old",
        )
    assert (
        ring.encrypt(os.urandom(20), source_id=uuid4(), credential_type=TOKEN_TYPE).key_id
        == "key-new"
    )
    with pytest.raises(KeyringError, match="reference_scan_empty"):
        ring.require_key_can_be_disabled("key-old", referenced_rows=0, scanned_rows=0)
    with pytest.raises(KeyringError, match="key_still_referenced"):
        ring.require_key_can_be_disabled("key-old", referenced_rows=1, scanned_rows=2)
    ring.require_key_can_be_disabled("key-old", referenced_rows=0, scanned_rows=2)


def test_TC_SEC_19_rotation_has_exact_four_restart_safe_stages() -> None:
    ring = _ring()
    assert ring.begin_rotation("key-old") is MasterKeyRotationPhase.NEW_KEY_WRITES
    assert (
        ring.rotation_phase_from_references(
            "key-old",
            referenced_rows=1,
            scanned_rows=2,
        )
        is MasterKeyRotationPhase.REENCRYPTING
    )
    assert (
        ring.rotation_phase_from_references(
            "key-old",
            referenced_rows=0,
            scanned_rows=2,
        )
        is MasterKeyRotationPhase.OLD_KEY_READ_ONLY
    )
    disabled_ring = MasterKeyring(
        (
            MasterKey("key-new", MasterKeyState.WRITABLE, _material()),
            MasterKey("key-old", MasterKeyState.DISABLED, _material()),
        )
    )
    assert (
        disabled_ring.rotation_phase_from_references(
            "key-old",
            referenced_rows=0,
            scanned_rows=2,
        )
        is MasterKeyRotationPhase.COMPLETE
    )

    ordered = tuple(MasterKeyRotationPhase)
    for current, following in zip(ordered, ordered[1:], strict=False):
        require_rotation_transition(current, following)
    for current, following in (
        (MasterKeyRotationPhase.NEW_KEY_WRITES, MasterKeyRotationPhase.OLD_KEY_READ_ONLY),
        (MasterKeyRotationPhase.REENCRYPTING, MasterKeyRotationPhase.NEW_KEY_WRITES),
        (MasterKeyRotationPhase.COMPLETE, MasterKeyRotationPhase.COMPLETE),
    ):
        with pytest.raises(KeyringError, match="invalid_rotation_transition"):
            require_rotation_transition(current, following)


def _write_keyring(path: Path, *, material: bytes, state: str = "writable") -> None:
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "keys": [
                    {
                        "id": "runtime-generated",
                        "state": state,
                        "material": base64.b64encode(material).decode("ascii"),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o400)


def test_TC_SEC_19_keyring_loads_only_via_root_only_secret_file(tmp_path: Path) -> None:
    secret = tmp_path / "master-keyring.json"
    material = _material()
    _write_keyring(secret, material=material)

    ring = load_master_keyring(secret)
    assert ring.writable_key_id == "runtime-generated"
    rendered = repr(ring)
    assert base64.b64encode(material).decode("ascii") not in rendered
    assert str(secret) not in rendered

    secret.chmod(0o600)
    with pytest.raises(KeyringError) as captured:
        load_master_keyring(secret)
    error_text = f"{captured.value!s} {captured.value!r}"
    assert str(secret) not in error_text
    assert base64.b64encode(material).decode("ascii") not in error_text


def test_TC_SEC_19_keyring_unknown_state_and_sensitive_environment_fail_closed(
    tmp_path: Path,
) -> None:
    secret = tmp_path / "master-keyring.json"
    material = _material()
    _write_keyring(secret, material=material, state="future-state")
    with pytest.raises(KeyringError, match="invalid_key_entry"):
        load_master_keyring(secret)

    with pytest.raises(ConfigLoadError, match="sensitive_environment_field"):
        load_config(
            {"public_base_url": "https://workspace.invalid"},
            environ={"HOLM__MASTER_KEY": base64.b64encode(material).decode("ascii")},
        )


def test_TC_SEC_19_envelope_repr_and_errors_never_render_sensitive_bytes() -> None:
    ring = _ring()
    source_id = uuid4()
    plaintext = os.urandom(32)
    envelope = ring.encrypt(plaintext, source_id=source_id, credential_type=TOKEN_TYPE)
    rendered = repr(envelope)
    assert plaintext.hex() not in rendered
    assert envelope.nonce.hex() not in rendered
    assert envelope.ciphertext.hex() not in rendered
    assert envelope.tag.hex() not in rendered
    with pytest.raises(CredentialCryptoError) as captured:
        CredentialEnvelope("key-new", b"short", plaintext, os.urandom(16))
    assert plaintext.hex() not in f"{captured.value!s} {captured.value!r}"
