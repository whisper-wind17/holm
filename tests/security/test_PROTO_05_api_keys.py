"""PROTO-05: strict API Key format, Pepper file, and shared validator tests."""

from __future__ import annotations

import asyncio
import base64
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from holm_security import (
    ApiKeyFormatError,
    ApiKeyPepperKeyring,
    ApiKeyPepperKeyringError,
    ApiKeyValidationError,
    ApiKeyValidationSnapshot,
    issue_api_key,
    load_api_key_pepper_keyring,
    parse_api_key,
    validate_api_key,
)


def _keyring() -> ApiKeyPepperKeyring:
    return ApiKeyPepperKeyring(
        current_key_id="pepper-current",
        keys={"pepper-current": b"P" * 32},
    )


def _encoded_secret(plaintext: str) -> str:
    _, separator, secret = plaintext.removeprefix("holm_sk_").partition("_")
    assert separator
    return secret


def test_PROTO_05_key_format_round_trip_and_rejects_noncanonical_forms() -> None:
    issued = issue_api_key(_keyring())
    parsed = parse_api_key(issued.plaintext)

    assert parsed.lookup_id == issued.lookup_id
    assert issued.plaintext.startswith("holm_sk_")
    assert issued.display_prefix == "holm_sk_"
    assert len(issued.secret_hmac) == 32
    assert "plaintext" not in repr(issued)
    assert "secret_hmac" not in repr(issued)
    for invalid in (
        "",
        "holm_sk_missing",
        issued.plaintext.lower(),
        issued.plaintext + "=",
        issued.plaintext.replace("holm_sk_", "other_", 1),
    ):
        with pytest.raises(ApiKeyFormatError, match="invalid_api_key"):
            parse_api_key(invalid)


def test_PROTO_05_pepper_keyring_loads_only_closed_0600_file(tmp_path: Path) -> None:
    path = tmp_path / "api-key-peppers.json"
    path.write_text(
        json.dumps(
            {
                "current_key_id": "pepper-current",
                "keys": {
                    "pepper-current": base64.b64encode(os.urandom(32)).decode("ascii"),
                    "pepper-old": base64.b64encode(os.urandom(32)).decode("ascii"),
                },
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)
    assert load_api_key_pepper_keyring(path).current_key_id == "pepper-current"

    path.chmod(0o400)
    with pytest.raises(ApiKeyPepperKeyringError, match="secret_file_file_permissions"):
        load_api_key_pepper_keyring(path)


def test_PROTO_05_shared_validator_accepts_correct_key_and_rejects_wrong_or_unknown() -> None:
    async def scenario() -> None:
        ring = _keyring()
        issued = issue_api_key(ring)
        parsed = parse_api_key(issued.plaintext)
        user_id = uuid4()
        service_id = uuid4()
        now = datetime.now(UTC)
        snapshot = ApiKeyValidationSnapshot(
            user_id=user_id,
            api_key_id=uuid4(),
            pepper_key_id=issued.pepper_key_id,
            secret_hmac=issued.secret_hmac,
            revoked=False,
            expired_status=False,
            expires_at=now + timedelta(hours=1),
            user_active=True,
            scoped_service_ids=frozenset({service_id}),
            database_now=now,
            user_is_admin=True,
            authorized_service_ids=frozenset(),
        )

        async def lookup(lookup_id: str) -> ApiKeyValidationSnapshot | None:
            return snapshot if lookup_id == parsed.lookup_id else None

        validated = await validate_api_key(issued.plaintext, lookup=lookup, keyring=ring)
        assert validated.user_id == user_id
        assert validated.scoped_service_ids == frozenset({service_id})
        assert not validated.unrestricted

        lookup_id = issued.plaintext.removeprefix("holm_sk_").partition("_")[0]
        wrong = f"holm_sk_{lookup_id}_{_encoded_secret(issue_api_key(ring).plaintext)}"
        for candidate in (wrong, issue_api_key(ring).plaintext):
            with pytest.raises(ApiKeyValidationError, match="invalid_api_key") as failure:
                await validate_api_key(candidate, lookup=lookup, keyring=ring)
            assert failure.value.code == "invalid_api_key"

    asyncio.run(scenario())


def test_PROTO_05_shared_validator_rejects_revoked_expired_and_inactive() -> None:
    async def scenario() -> None:
        ring = _keyring()
        issued = issue_api_key(ring)
        now = datetime.now(UTC)

        async def validate_snapshot(snapshot: ApiKeyValidationSnapshot) -> str | None:
            async def lookup(_: str) -> ApiKeyValidationSnapshot | None:
                return snapshot

            try:
                await validate_api_key(issued.plaintext, lookup=lookup, keyring=ring)
            except ApiKeyValidationError as failure:
                return failure.code
            return None

        base = {
            "user_id": uuid4(),
            "api_key_id": uuid4(),
            "pepper_key_id": issued.pepper_key_id,
            "secret_hmac": issued.secret_hmac,
            "expired_status": False,
            "scoped_service_ids": frozenset(),
            "database_now": now,
            "user_is_admin": True,
            "authorized_service_ids": frozenset(),
        }
        unrestricted_snapshot = ApiKeyValidationSnapshot(
            **base,
            revoked=False,
            expires_at=None,
            user_active=True,
        )

        async def unrestricted_lookup(_: str) -> ApiKeyValidationSnapshot | None:
            return unrestricted_snapshot

        unrestricted = await validate_api_key(
            issued.plaintext,
            lookup=unrestricted_lookup,
            keyring=ring,
        )
        assert unrestricted.unrestricted
        assert (
            await validate_snapshot(
                ApiKeyValidationSnapshot(
                    **base,
                    revoked=True,
                    expires_at=None,
                    user_active=True,
                )
            )
            == "invalid_api_key"
        )
        assert (
            await validate_snapshot(
                ApiKeyValidationSnapshot(
                    **base,
                    revoked=False,
                    expires_at=now - timedelta(microseconds=1),
                    user_active=True,
                )
            )
            == "api_key_expired"
        )
        assert (
            await validate_snapshot(
                ApiKeyValidationSnapshot(
                    **base,
                    revoked=False,
                    expires_at=None,
                    user_active=False,
                )
            )
            == "invalid_api_key"
        )

    asyncio.run(scenario())
