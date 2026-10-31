"""Active API Key reference counts used by Pepper rotation safeguards."""

from __future__ import annotations

import sqlalchemy as sa
from holm_domain import ApiKeyStatus
from sqlalchemy.ext.asyncio import AsyncSession

from holm_persistence.ai_services import API_KEYS


class ApiKeyPepperReferenceRepository:
    """Count credentials that Gateway authentication can still accept."""

    __slots__ = ("_session",)

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def active_reference_counts(self) -> dict[str, int]:
        """Group active, unexpired API Keys by their persisted Pepper ID."""

        statement = (
            sa.select(
                API_KEYS.c.pepper_key_id,
                sa.func.count().label("active_reference_count"),
            )
            .where(
                API_KEYS.c.status == ApiKeyStatus.ACTIVE.value,
                sa.or_(
                    API_KEYS.c.expires_at.is_(None),
                    API_KEYS.c.expires_at > sa.func.clock_timestamp(),
                ),
            )
            .group_by(API_KEYS.c.pepper_key_id)
            .order_by(API_KEYS.c.pepper_key_id)
        )
        rows = (await self._session.execute(statement)).tuples().all()
        return {str(key_id): int(count) for key_id, count in rows}


__all__ = ["ApiKeyPepperReferenceRepository"]
