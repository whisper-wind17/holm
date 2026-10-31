"""API Key issuance, strict parsing, Pepper Keyring loading, and validation."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
import secrets
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Final, Never
from uuid import UUID

from holm_common import SecretFileError, as_utc, read_secret_file

API_KEY_PREFIX: Final[str] = "holm_sk_"
API_KEY_LOOKUP_BYTES: Final[int] = 16
API_KEY_SECRET_BYTES: Final[int] = 32
API_KEY_HMAC_BYTES: Final[int] = hashlib.sha256().digest_size
API_KEY_PEPPER_BYTES: Final[int] = 32
_LOOKUP_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Z2-7]{26}$")
_SECRET_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_-]{43}$")
_KEY_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class ApiKeyError(RuntimeError):
    """Sanitized API Key failure with a stable machine-readable code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"API key operation rejected: {code}")


class ApiKeyFormatError(ApiKeyError):
    """A presented API Key does not have the canonical wire form."""

    def __init__(self) -> None:
        super().__init__("invalid_api_key")


class ApiKeyPepperKeyringError(ApiKeyError):
    """The Pepper Keyring file or requested key is unusable."""


class ApiKeyValidationError(ApiKeyError):
    """Authentication failed without retaining the presented credential."""

    def __init__(self, code: str) -> None:
        if code not in {"invalid_api_key", "api_key_expired"}:
            raise ValueError("unsupported API key validation failure")
        super().__init__(code)


API_KEY_PEPPER_MISSING_ERROR_CODE: Final[str] = "api_key_pepper_missing"
API_KEY_PEPPER_REFERENCE_CHECK_ERROR_CODE: Final[str] = "api_key_pepper_reference_check_unavailable"


class ApiKeyPepperRemovalReason(StrEnum):
    """Stable reasons why a Pepper cannot be removed from a Keyring."""

    CURRENT_PEPPER = "current_pepper"
    ACTIVE_KEY_REFERENCES = "active_key_references"
    PEPPER_NOT_FOUND = "pepper_not_found"


class ApiKeyPepperStartupError(RuntimeError):
    """Sanitized startup failure containing only a code and Pepper IDs."""

    def __init__(self, code: str, *, missing_key_ids: tuple[str, ...] = ()) -> None:
        self.code = code
        self.missing_key_ids = tuple(sorted(missing_key_ids))
        suffix = "" if not self.missing_key_ids else ":" + ",".join(self.missing_key_ids)
        super().__init__(f"{code}{suffix}")


@dataclass(frozen=True, slots=True)
class ApiKeyPepperRemovalAssessment:
    """Safe removal metadata for one Pepper ID; it never carries key material."""

    pepper_key_id: str
    is_current: bool
    active_reference_count: int
    can_remove: bool
    reason: ApiKeyPepperRemovalReason | None


@dataclass(frozen=True, slots=True)
class ApiKeyPepperKeyringAssessment:
    """Safe Keyring metadata plus referenced Pepper IDs absent from the file."""

    peppers: tuple[ApiKeyPepperRemovalAssessment, ...]
    missing_referenced_key_ids: tuple[str, ...]

    def for_key(self, pepper_key_id: str) -> ApiKeyPepperRemovalAssessment:
        """Return an explicit assessment, including for an unknown Pepper ID."""

        for pepper in self.peppers:
            if pepper.pepper_key_id == pepper_key_id:
                return pepper
        return ApiKeyPepperRemovalAssessment(
            pepper_key_id=pepper_key_id,
            is_current=False,
            active_reference_count=0,
            can_remove=False,
            reason=ApiKeyPepperRemovalReason.PEPPER_NOT_FOUND,
        )


@dataclass(frozen=True, slots=True)
class ParsedApiKey:
    lookup_id: str
    secret: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class IssuedApiKey:
    """One newly issued key; plaintext and digest are absent from repr."""

    plaintext: str = field(repr=False)
    lookup_id: str
    display_prefix: str
    last4: str
    pepper_key_id: str
    secret_hmac: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class ApiKeyValidationSnapshot:
    """Safe database facts consumed by the sole validation function."""

    user_id: UUID
    api_key_id: UUID
    pepper_key_id: str
    secret_hmac: bytes = field(repr=False)
    revoked: bool
    expired_status: bool
    expires_at: datetime | None
    user_active: bool
    scoped_service_ids: frozenset[UUID]
    database_now: datetime
    user_is_admin: bool
    authorized_service_ids: frozenset[UUID]
    service_limits: Mapping[UUID, tuple[int | None, int | None]] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ValidatedApiKey:
    user_id: UUID
    api_key_id: UUID
    scoped_service_ids: frozenset[UUID]
    user_is_admin: bool
    authorized_service_ids: frozenset[UUID]
    service_limits: Mapping[UUID, tuple[int | None, int | None]] = field(default_factory=dict)

    @property
    def unrestricted(self) -> bool:
        """An empty Key scope means all otherwise-authorized services."""

        return not self.scoped_service_ids

    def allows_service(self, service_id: UUID) -> bool:
        """Apply Key scope and account grants without another Gateway lookup."""

        key_allows = self.unrestricted or service_id in self.scoped_service_ids
        user_allows = service_id in self.authorized_service_ids
        return key_allows and user_allows


type ApiKeyValidationLookup = Callable[[str], Awaitable[ApiKeyValidationSnapshot | None]]


def _fail_keyring(code: str) -> Never:
    raise ApiKeyPepperKeyringError(code)


def _validate_key_id(key_id: object) -> str:
    if not isinstance(key_id, str) or _KEY_ID_PATTERN.fullmatch(key_id) is None:
        _fail_keyring("invalid_key_id")
    return key_id


def _base32_without_padding(value: bytes) -> str:
    return base64.b32encode(value).decode("ascii").rstrip("=")


def _base64url_without_padding(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def parse_api_key(presented: str) -> ParsedApiKey:
    """Parse only the canonical ``holm_sk_<base32>_<base64url>`` form."""

    if not isinstance(presented, str):
        raise ApiKeyFormatError()
    if not presented.startswith(API_KEY_PREFIX):
        raise ApiKeyFormatError()
    lookup_id, separator, encoded_secret = presented[len(API_KEY_PREFIX) :].partition("_")
    if (
        not separator
        or _LOOKUP_PATTERN.fullmatch(lookup_id) is None
        or _SECRET_PATTERN.fullmatch(encoded_secret) is None
    ):
        raise ApiKeyFormatError()
    try:
        lookup_bytes = base64.b32decode(lookup_id + "=" * 6, casefold=False)
        secret = base64.b64decode(encoded_secret + "=", altchars=b"-_", validate=True)
    except (ValueError, binascii.Error):
        raise ApiKeyFormatError() from None
    if (
        len(lookup_bytes) != API_KEY_LOOKUP_BYTES
        or len(secret) != API_KEY_SECRET_BYTES
        or _base32_without_padding(lookup_bytes) != lookup_id
        or _base64url_without_padding(secret) != encoded_secret
    ):
        raise ApiKeyFormatError()
    return ParsedApiKey(lookup_id=lookup_id, secret=secret)


class ApiKeyPepperKeyring:
    """Immutable Pepper Keyring with one current key for new HMACs."""

    __slots__ = ("_current_key_id", "_keys")

    def __init__(self, *, current_key_id: str, keys: Mapping[str, bytes]) -> None:
        selected = _validate_key_id(current_key_id)
        copied: dict[str, bytes] = {}
        for raw_key_id, material in keys.items():
            key_id = _validate_key_id(raw_key_id)
            if key_id in copied:
                _fail_keyring("duplicate_key_id")
            if not isinstance(material, bytes) or len(material) != API_KEY_PEPPER_BYTES:
                _fail_keyring("invalid_key_material")
            copied[key_id] = material
        if selected not in copied:
            _fail_keyring("current_key_missing")
        self._current_key_id = selected
        self._keys = MappingProxyType(copied)

    @property
    def current_key_id(self) -> str:
        return self._current_key_id

    @property
    def key_ids(self) -> frozenset[str]:
        """Expose only identifiers needed for rotation checks, never materials."""

        return frozenset(self._keys)

    def digest(self, secret: bytes, *, key_id: str | None = None) -> bytes:
        if not isinstance(secret, bytes) or len(secret) != API_KEY_SECRET_BYTES:
            raise ApiKeyError("invalid_secret")
        selected_id = self._current_key_id if key_id is None else key_id
        material = self._keys.get(selected_id)
        if material is None:
            raise ApiKeyPepperKeyringError("unknown_key")
        return hmac.new(material, secret, hashlib.sha256).digest()

    def matches(self, secret: bytes, *, key_id: str, expected_hmac: bytes) -> bool:
        """Compare every usable stored digest with the constant-time primitive."""

        if not isinstance(expected_hmac, bytes) or len(expected_hmac) != API_KEY_HMAC_BYTES:
            return False
        try:
            actual = self.digest(secret, key_id=key_id)
        except ApiKeyError:
            return False
        return hmac.compare_digest(actual, expected_hmac)


def load_api_key_pepper_keyring(path: str | Path) -> ApiKeyPepperKeyring:
    """Load a closed JSON Keyring from an owner-only ``0600`` file."""

    try:
        payload = read_secret_file(path, allowed_modes=frozenset({0o600}))
    except SecretFileError as error:
        raise ApiKeyPepperKeyringError(f"secret_file_{error.code}") from None
    try:
        raw = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        _fail_keyring("invalid_keyring_document")
    if not isinstance(raw, Mapping) or set(raw) != {"current_key_id", "keys"}:
        _fail_keyring("invalid_keyring_document")
    raw_keys = raw["keys"]
    if not isinstance(raw_keys, Mapping):
        _fail_keyring("invalid_keyring_document")
    decoded: dict[str, bytes] = {}
    for raw_key_id, encoded in raw_keys.items():
        key_id = _validate_key_id(raw_key_id)
        if not isinstance(encoded, str):
            _fail_keyring("invalid_key_material")
        try:
            decoded[key_id] = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            _fail_keyring("invalid_key_material")
    current_key_id = raw["current_key_id"]
    return ApiKeyPepperKeyring(
        current_key_id=_validate_key_id(current_key_id),
        keys=decoded,
    )


def assess_api_key_pepper_keyring(
    keyring: ApiKeyPepperKeyring,
    active_reference_counts: Mapping[str, int],
) -> ApiKeyPepperKeyringAssessment:
    """Derive deletion safety from Key IDs and active-reference counts only."""

    counts: dict[str, int] = {}
    for raw_key_id, raw_count in active_reference_counts.items():
        key_id = _validate_key_id(raw_key_id)
        if isinstance(raw_count, bool) or not isinstance(raw_count, int) or raw_count < 0:
            raise ValueError("active API key reference counts must be non-negative integers")
        counts[key_id] = raw_count

    peppers: list[ApiKeyPepperRemovalAssessment] = []
    for key_id in sorted(keyring.key_ids):
        count = counts.get(key_id, 0)
        is_current = key_id == keyring.current_key_id
        reason = (
            ApiKeyPepperRemovalReason.CURRENT_PEPPER
            if is_current
            else (ApiKeyPepperRemovalReason.ACTIVE_KEY_REFERENCES if count > 0 else None)
        )
        peppers.append(
            ApiKeyPepperRemovalAssessment(
                pepper_key_id=key_id,
                is_current=is_current,
                active_reference_count=count,
                can_remove=reason is None,
                reason=reason,
            )
        )
    return ApiKeyPepperKeyringAssessment(
        peppers=tuple(peppers),
        missing_referenced_key_ids=tuple(
            sorted(
                key_id
                for key_id, count in counts.items()
                if count > 0 and key_id not in keyring.key_ids
            )
        ),
    )


def require_api_key_pepper_references_available(
    assessment: ApiKeyPepperKeyringAssessment,
) -> None:
    """Fail startup when an active API Key references an absent Pepper ID."""

    if assessment.missing_referenced_key_ids:
        raise ApiKeyPepperStartupError(
            API_KEY_PEPPER_MISSING_ERROR_CODE,
            missing_key_ids=assessment.missing_referenced_key_ids,
        )


def issue_api_key(keyring: ApiKeyPepperKeyring) -> IssuedApiKey:
    """Create one credential whose plaintext must go directly to one response."""

    lookup_id = _base32_without_padding(secrets.token_bytes(API_KEY_LOOKUP_BYTES))
    secret = secrets.token_bytes(API_KEY_SECRET_BYTES)
    encoded_secret = _base64url_without_padding(secret)
    return IssuedApiKey(
        plaintext=f"{API_KEY_PREFIX}{lookup_id}_{encoded_secret}",
        lookup_id=lookup_id,
        display_prefix=API_KEY_PREFIX,
        last4=encoded_secret[-4:],
        pepper_key_id=keyring.current_key_id,
        secret_hmac=keyring.digest(secret),
    )


async def validate_api_key(
    presented: str,
    *,
    lookup: ApiKeyValidationLookup,
    keyring: ApiKeyPepperKeyring,
) -> ValidatedApiKey:
    """Validate format, HMAC, lifecycle, expiry, user state, and Key scope."""

    try:
        parsed = parse_api_key(presented)
    except ApiKeyFormatError:
        raise ApiKeyValidationError("invalid_api_key") from None
    snapshot = await lookup(parsed.lookup_id)
    if snapshot is None or not keyring.matches(
        parsed.secret,
        key_id=snapshot.pepper_key_id,
        expected_hmac=snapshot.secret_hmac,
    ):
        raise ApiKeyValidationError("invalid_api_key")
    database_now = as_utc(snapshot.database_now)
    expires_at = None if snapshot.expires_at is None else as_utc(snapshot.expires_at)
    if snapshot.expired_status or (expires_at is not None and database_now >= expires_at):
        raise ApiKeyValidationError("api_key_expired")
    if snapshot.revoked or not snapshot.user_active:
        raise ApiKeyValidationError("invalid_api_key")
    return ValidatedApiKey(
        user_id=snapshot.user_id,
        api_key_id=snapshot.api_key_id,
        scoped_service_ids=snapshot.scoped_service_ids,
        user_is_admin=snapshot.user_is_admin,
        authorized_service_ids=snapshot.authorized_service_ids,
        service_limits=snapshot.service_limits,
    )


__all__ = [
    "API_KEY_HMAC_BYTES",
    "API_KEY_LOOKUP_BYTES",
    "API_KEY_PEPPER_BYTES",
    "API_KEY_PREFIX",
    "API_KEY_SECRET_BYTES",
    "API_KEY_PEPPER_MISSING_ERROR_CODE",
    "API_KEY_PEPPER_REFERENCE_CHECK_ERROR_CODE",
    "ApiKeyError",
    "ApiKeyFormatError",
    "ApiKeyPepperKeyring",
    "ApiKeyPepperKeyringAssessment",
    "ApiKeyPepperKeyringError",
    "ApiKeyPepperRemovalAssessment",
    "ApiKeyPepperRemovalReason",
    "ApiKeyPepperStartupError",
    "ApiKeyValidationLookup",
    "ApiKeyValidationSnapshot",
    "ApiKeyValidationError",
    "IssuedApiKey",
    "ParsedApiKey",
    "ValidatedApiKey",
    "issue_api_key",
    "assess_api_key_pepper_keyring",
    "load_api_key_pepper_keyring",
    "parse_api_key",
    "require_api_key_pepper_references_available",
    "validate_api_key",
]
