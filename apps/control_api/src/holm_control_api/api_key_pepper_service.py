"""Read-only API Key Pepper rotation state and startup protection."""

from __future__ import annotations

from holm_persistence import ApiKeyPepperReferenceRepository, AsyncSessionFactory
from holm_security import (
    API_KEY_PEPPER_REFERENCE_CHECK_ERROR_CODE,
    ApiKeyPepperKeyring,
    ApiKeyPepperKeyringAssessment,
    ApiKeyPepperStartupError,
    assess_api_key_pepper_keyring,
    require_api_key_pepper_references_available,
)
from sqlalchemy.exc import SQLAlchemyError


class ApiKeyPepperService:
    """Derive safe Keyring metadata from a fresh database reference scan."""

    __slots__ = ("_keyring", "_session_factory")

    def __init__(
        self,
        *,
        session_factory: AsyncSessionFactory,
        keyring: ApiKeyPepperKeyring,
    ) -> None:
        self._session_factory = session_factory
        self._keyring = keyring

    async def inspect(self) -> ApiKeyPepperKeyringAssessment:
        async with self._session_factory() as session:
            counts = await ApiKeyPepperReferenceRepository(session).active_reference_counts()
        return assess_api_key_pepper_keyring(self._keyring, counts)

    async def check_startup(self) -> None:
        """Reject startup without rendering database or credential details."""

        try:
            assessment = await self.inspect()
        except SQLAlchemyError:
            raise ApiKeyPepperStartupError(API_KEY_PEPPER_REFERENCE_CHECK_ERROR_CODE) from None
        require_api_key_pepper_references_available(assessment)


__all__ = ["ApiKeyPepperService"]
