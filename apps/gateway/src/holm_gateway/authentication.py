"""Credential validation around the sole API Key validation primitive."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from holm_domain import ErrorCode
from holm_protocols import AuthenticatedPrincipal, InferenceFailure, PlatformError
from holm_security import (
    ApiKeyPepperKeyring,
    ApiKeyValidationError,
    ApiKeyValidationLookup,
    ValidatedApiKey,
    validate_api_key,
)

from holm_gateway.route_snapshot import RouteSnapshotUnavailable


class GatewayAuthenticator(Protocol):
    async def authenticate(self, credential: str | None) -> ValidatedApiKey:
        """Authenticate one extracted credential without persisting it."""
        ...


class ApiKeyAuthenticator:
    """Validate an already-extracted API Key without duplicating security policy."""

    __slots__ = ("_availability_check", "_keyring", "_lookup")

    def __init__(
        self,
        *,
        lookup: ApiKeyValidationLookup,
        keyring: ApiKeyPepperKeyring,
        availability_check: Callable[[], None] | None = None,
    ) -> None:
        self._lookup = lookup
        self._keyring = keyring
        self._availability_check = availability_check

    async def authenticate(self, credential: str | None) -> ValidatedApiKey:
        if self._availability_check is not None:
            try:
                self._availability_check()
            except RouteSnapshotUnavailable:
                raise InferenceFailure(PlatformError(ErrorCode.CONTROL_PLANE_UNAVAILABLE)) from None
        return await validate_api_key(credential or "", lookup=self._lookup, keyring=self._keyring)


class InferenceAuthenticator:
    """Map security-package results into the protocol-independent core port."""

    __slots__ = ("_authenticator",)

    def __init__(self, authenticator: GatewayAuthenticator) -> None:
        self._authenticator = authenticator

    async def authenticate(self, credential: str | None) -> AuthenticatedPrincipal:
        try:
            validated = await self._authenticator.authenticate(credential)
        except ApiKeyValidationError as error:
            code = (
                ErrorCode.API_KEY_EXPIRED
                if error.code == ErrorCode.API_KEY_EXPIRED.value
                else ErrorCode.INVALID_API_KEY
            )
            raise InferenceFailure(PlatformError(code)) from None
        return AuthenticatedPrincipal(
            user_id=validated.user_id,
            api_key_id=validated.api_key_id,
            scoped_service_ids=validated.scoped_service_ids,
            user_is_admin=validated.user_is_admin,
            authorized_service_ids=validated.authorized_service_ids,
            service_limits=validated.service_limits,
        )


__all__ = ["ApiKeyAuthenticator", "GatewayAuthenticator", "InferenceAuthenticator"]
