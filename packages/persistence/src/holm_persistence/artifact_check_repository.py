"""Restricted maintenance coordination; completion is written only by the verifier."""

from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

ARTIFACT_LIGHT_VERIFY_JOB_TYPE = "artifact.light_verify"


@dataclass(frozen=True, slots=True)
class ArtifactCheckSchedule:
    scheduled: int
    pending: bool
    failed: bool


class ArtifactCheckRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def schedule_due(self) -> ArtifactCheckSchedule:
        row = (
            await self._session.execute(
                sa.text(
                    "SELECT scheduled, pending, failed FROM core.holm_schedule_artifact_checks()"
                )
            )
        ).one()
        return ArtifactCheckSchedule(int(row.scheduled), bool(row.pending), bool(row.failed))
