"""Gateway startup guard for active API Key Pepper references."""

from __future__ import annotations

from holm_persistence import ApiKeyPepperReferenceRepository, AsyncSessionFactory
from holm_security import (
    API_KEY_PEPPER_REFERENCE_CHECK_ERROR_CODE,
    ApiKeyPepperKeyring,
    ApiKeyPepperStartupError,
    assess_api_key_pepper_keyring,
    require_api_key_pepper_references_available,
)
from sqlalchemy.exc import SQLAlchemyError


async def check_api_key_pepper_references(
    session_factory: AsyncSessionFactory,
    keyring: ApiKeyPepperKeyring,
) -> None:
    """Reject startup if the database requires a Pepper absent from the Keyring."""

    try:
        async with session_factory() as session:
            counts = await ApiKeyPepperReferenceRepository(session).active_reference_counts()
    except SQLAlchemyError:
        raise ApiKeyPepperStartupError(API_KEY_PEPPER_REFERENCE_CHECK_ERROR_CODE) from None
    require_api_key_pepper_references_available(assess_api_key_pepper_keyring(keyring, counts))


__all__ = ["check_api_key_pepper_references"]
