"""Shared Artifact command lock usable by roles with SELECT-only inventory access."""

import hashlib
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession


async def lock_artifact(session: AsyncSession, artifact_id: UUID) -> None:
    key = int.from_bytes(
        hashlib.sha256(b"artifact:" + artifact_id.bytes).digest()[:8], "big", signed=True
    )
    await session.execute(sa.select(sa.func.pg_advisory_xact_lock(key)))
