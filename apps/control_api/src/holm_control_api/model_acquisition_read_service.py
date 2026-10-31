"""Read-only Download Task and Model Artifact listings.

Both surfaces are strictly ``SELECT``-only: no audit event, no domain event, no
filesystem mutation, and no row touched.  Anything the platform cannot honestly
answer today is returned as ``None`` and never as ``0`` -- the four such facts
are named in the module's three connection points below.

Deriving rather than restating is the other rule this module follows.  The set
of commands a task currently accepts comes from ``DOWNLOAD_COMMAND_MATRIX``, the
stage a persisted error belongs to comes from ``DOWNLOAD_ERROR_STAGES``, and the
"in progress" task states come from ``ACTIVE_DOWNLOAD_STATUSES``.  Each of those
already exists and is the sole definition of its vocabulary; a second copy here
would drift while both copies' happy paths stayed green.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final, Protocol
from uuid import UUID

from holm_common import DiskSpaceSnapshot, as_utc
from holm_contracts import CursorPageParams
from holm_domain import (
    DownloadFailureStage,
    DownloadTaskStatus,
    download_error_stage,
)
from holm_persistence import (
    AsyncSessionFactory,
    DownloadTaskListRecord,
    KeysetPosition,
    ModelAcquisitionReadRepository,
    ModelArtifactFilters,
    ModelArtifactListRecord,
)
from holm_persistence.artifact_deletion_repository import ArtifactDeletionRepository
from holm_persistence.artifact_verification_repository import ArtifactVerificationRepository
from sqlalchemy.ext.asyncio import AsyncSession

from holm_control_api.artifact_verification_service import ArtifactVerificationService
from holm_control_api.download_command_service import (
    DownloadCommand,
    DownloadCommandService,
    available_commands_for,
)
from holm_control_api.model_resolution_service import ACTIVE_DOWNLOAD_STATUSES

CURSOR_VERSION: Final[int] = 1
DOWNLOADS_CURSOR_KIND: Final[str] = "downloads"
ARTIFACTS_CURSOR_KIND: Final[str] = "artifacts"


class ModelAcquisitionCursorInvalid(ValueError):
    """The opaque cursor was not issued by the endpoint that received it."""


class ArtifactInstanceReferenceProbe(Protocol):
    """Connection point for the not-yet-created ``core.model_instances``."""

    async def count(self, *, session: AsyncSession, artifact_id: UUID) -> int: ...


class DatabaseArtifactInstanceReferenceProbe:
    async def count(self, *, session: AsyncSession, artifact_id: UUID) -> int:
        return await ArtifactDeletionRepository(session).count_references(artifact_id)


class StorageAvailabilityProbe(Protocol):
    """Free space on the Model storage filesystem, or ``None`` when unreadable."""

    async def available_bytes(self) -> int | None: ...


@dataclass(frozen=True, slots=True, repr=False)
class ModelRootAvailability:
    """Read free space from the real filesystem holding ``model_root``.

    ``model_root`` may not exist yet -- nothing in the current release creates it before the
    first download -- and it may be unreadable.  Both answer ``None``: a missing
    directory is not zero free bytes, and reporting ``0`` would show the
    administrator a full disk that is not full.
    """

    model_root: Path

    async def available_bytes(self) -> int | None:
        try:
            # ``statvfs`` is a blocking syscall; ASGI request handling must not
            # perform it on the event loop thread.
            snapshot = await asyncio.to_thread(DiskSpaceSnapshot.for_path, self.model_root)
        except OSError:
            snapshot = None
        if snapshot is None:
            # Raised state is discarded outside the ``except`` block so the
            # server path cannot survive in an exception chain.
            return None
        return snapshot.free_bytes

    def __repr__(self) -> str:
        return "ModelRootAvailability(model_root=<redacted>)"


@dataclass(frozen=True, slots=True)
class DownloadTaskItem:
    """One Download Task projection with its two derived vocabularies applied."""

    record: DownloadTaskListRecord
    failure_stage: DownloadFailureStage | None
    available_commands: tuple[DownloadCommand, ...]


@dataclass(frozen=True, slots=True)
class DownloadTaskPage:
    items: tuple[DownloadTaskItem, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class ModelArtifactItem:
    """One Model Artifact projection plus the facts the platform cannot supply."""

    record: ModelArtifactListRecord
    referencing_instances: int | None
    last_verified_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ModelArtifactSummary:
    managed_bytes: int
    storage_available_bytes: int | None
    active_download_tasks: int


@dataclass(frozen=True, slots=True)
class ModelArtifactPage:
    items: tuple[ModelArtifactItem, ...]
    next_cursor: str | None
    summary: ModelArtifactSummary


def encode_cursor(kind: str, *, created_at: datetime, row_id: UUID) -> str:
    """Encode the keyset position as an opaque, endpoint-tagged token.

    The token is tagged rather than raw so a cursor from another list -- or a
    hand-written primary key -- is rejected instead of silently restarting the
    page. This is encoding, not signing: it is not a tamper-resistance claim.
    """

    payload = json.dumps(
        {"v": CURSOR_VERSION, "k": kind, "t": as_utc(created_at).isoformat(), "i": str(row_id)},
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def decode_cursor(kind: str, cursor: str) -> KeysetPosition:
    """Accept only a token this endpoint issued; reject everything else."""

    decoded: object = None
    try:
        padding = "=" * (-len(cursor) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(cursor + padding).decode("utf-8"))
    except (ValueError, binascii.Error, UnicodeDecodeError):
        decoded = None
    position = _position_from_payload(kind, decoded)
    if position is None:
        # Raised outside every ``except`` above so the rejected cursor cannot be
        # recovered from ``__context__`` by an exception-chain walker.
        raise ModelAcquisitionCursorInvalid()
    return position


def _position_from_payload(kind: str, decoded: object) -> KeysetPosition | None:
    if not isinstance(decoded, dict) or set(decoded) != {"v", "k", "t", "i"}:
        return None
    if decoded["v"] != CURSOR_VERSION or decoded["k"] != kind:
        return None
    moment, row_id = decoded["t"], decoded["i"]
    if not isinstance(moment, str) or not isinstance(row_id, str):
        return None
    parsed: KeysetPosition | None = None
    try:
        created_at = datetime.fromisoformat(moment)
        # ``as_utc`` would happily stamp UTC onto a naive value; a cursor this
        # endpoint issued always carries an offset, so a naive one is forged.
        parsed = (
            None
            if created_at.tzinfo is None
            else KeysetPosition(created_at=as_utc(created_at), id=UUID(row_id))
        )
    except (ValueError, TypeError):
        parsed = None
    return parsed


class ModelAcquisitionReadService:
    """Serve both read-only lists from one injected session factory."""

    __slots__ = (
        "_artifact_instance_references",
        "_session_factory",
        "_storage_availability",
    )

    def __init__(
        self,
        *,
        session_factory: AsyncSessionFactory,
        storage_availability: StorageAvailabilityProbe,
        artifact_instance_references: ArtifactInstanceReferenceProbe | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._storage_availability = storage_availability
        self._artifact_instance_references = artifact_instance_references

    def enable_database_reference_count(self) -> None:
        if self._artifact_instance_references is None:
            self._artifact_instance_references = DatabaseArtifactInstanceReferenceProbe()

    async def verify_artifact(self, artifact_id: UUID) -> None:
        await ArtifactVerificationService(self._session_factory).verify(artifact_id)

    async def artifact_verification_status(
        self, artifact_id: UUID
    ) -> tuple[str | None, str | None, UUID | None]:
        record = await ArtifactVerificationService(self._session_factory).status(artifact_id)
        if record is None:
            return None, None, None
        error = record.last_error_code
        if error is not None and error not in {
            "artifact_verify_unsafe_path",
            "artifact_verify_failed",
        }:
            error = "artifact_verify_failed"
        return record.status.value, error, record.id

    async def delete_artifact(self, *, artifact_id: UUID, actor_id: UUID, request_id: UUID) -> bool:
        from holm_control_api.artifact_deletion_service import ArtifactDeletionService

        return await ArtifactDeletionService(self._session_factory).delete(
            artifact_id=artifact_id,
            actor_id=actor_id,
            request_id=request_id,
        )

    async def artifact_deletion_status(self, artifact_id: UUID) -> tuple[str, str | None] | None:
        from holm_control_api.artifact_deletion_service import ArtifactDeletionService

        record = await ArtifactDeletionService(self._session_factory).status(artifact_id)
        if record is None:
            return None
        error_code = record.last_error_code
        if error_code not in {
            "artifact_delete_move_failed",
            "artifact_delete_unsafe_path",
            "artifact_delete_source_missing",
            "artifact_delete_trash_conflict",
            "artifact_referenced",
            "artifact_delete_not_found",
        }:
            error_code = None
        return record.status.value, error_code

    async def delete_download_task(
        self, *, task_id: UUID, actor_id: UUID, request_id: UUID, cleanup_staging: bool
    ) -> None:
        await DownloadCommandService(session_factory=self._session_factory).delete(
            task_id=task_id,
            actor_id=actor_id,
            request_id=request_id,
            cleanup_staging=cleanup_staging,
        )

    async def download_status_counts(self) -> dict[DownloadTaskStatus, int]:
        async with self._session_factory() as session:
            return await ModelAcquisitionReadRepository(session).download_status_counts()

    async def artifact_detail(
        self, artifact_id: UUID
    ) -> tuple[ModelArtifactItem, dict[str, object]] | None:
        async with self._session_factory() as session:
            repository = ModelAcquisitionReadRepository(session)
            records = await repository.list_artifacts(
                after=None, page_size=1, filters=ModelArtifactFilters(), artifact_id=artifact_id
            )
            if not records:
                return None
            record = records[0]
            return ModelArtifactItem(
                record=record,
                referencing_instances=await self._artifact_reference_count(
                    session=session, artifact_id=artifact_id
                ),
                last_verified_at=await ArtifactVerificationRepository(session).last_completed_at(
                    artifact_id
                ),
            ), await repository.artifact_details(artifact_id)

    async def list_download_tasks(
        self, params: CursorPageParams, *, statuses: tuple[DownloadTaskStatus, ...] = ()
    ) -> DownloadTaskPage:
        after = (
            None if params.cursor is None else decode_cursor(DOWNLOADS_CURSOR_KIND, params.cursor)
        )
        async with self._session_factory() as session:
            records = await ModelAcquisitionReadRepository(session).list_download_tasks(
                after=after,
                page_size=params.page_size,
                statuses=statuses,
            )
        page = records[: params.page_size]
        items = tuple(
            DownloadTaskItem(
                record=record,
                # Raises ``UnknownDownloadErrorCode`` for a code outside both
                # persisted vocabularies: an unclassifiable row must fail the
                # response loudly rather than be handed on as-is.
                failure_stage=download_error_stage(record.error_code),
                available_commands=available_commands_for(
                    record.status, record.error_code, source_type=record.source_type
                ),
            )
            for record in page
        )
        return DownloadTaskPage(
            items=items,
            next_cursor=_next_cursor(
                DOWNLOADS_CURSOR_KIND,
                last=None if not page else (page[-1].created_at, page[-1].id),
                has_more=len(records) > params.page_size,
            ),
        )

    async def list_artifacts(
        self,
        params: CursorPageParams,
        *,
        filters: ModelArtifactFilters,
    ) -> ModelArtifactPage:
        after = (
            None if params.cursor is None else decode_cursor(ARTIFACTS_CURSOR_KIND, params.cursor)
        )
        async with self._session_factory() as session:
            repository = ModelAcquisitionReadRepository(session)
            records = await repository.list_artifacts(
                after=after,
                page_size=params.page_size,
                filters=filters,
            )
            page = records[: params.page_size]
            items: list[ModelArtifactItem] = []
            for record in page:
                items.append(
                    ModelArtifactItem(
                        record=record,
                        last_verified_at=(
                            await ArtifactVerificationRepository(session).last_completed_at(
                                record.id
                            )
                        )
                        or record.created_at,
                        referencing_instances=await self._artifact_reference_count(
                            session=session,
                            artifact_id=record.id,
                        ),
                    )
                )
            totals = await repository.storage_totals(
                active_download_statuses=ACTIVE_DOWNLOAD_STATUSES,
            )
        return ModelArtifactPage(
            items=tuple(items),
            next_cursor=_next_cursor(
                ARTIFACTS_CURSOR_KIND,
                last=None if not page else (page[-1].created_at, page[-1].id),
                has_more=len(records) > params.page_size,
            ),
            summary=ModelArtifactSummary(
                managed_bytes=totals.managed_bytes,
                storage_available_bytes=await self._storage_availability.available_bytes(),
                active_download_tasks=totals.active_download_tasks,
            ),
        )

    async def _artifact_reference_count(
        self,
        *,
        session: AsyncSession,
        artifact_id: UUID,
    ) -> int | None:
        # The persistence adapter replaces the absent probe with the real core.model_instances
        # query. Until then the count is unknown, which is not the same fact as
        # "no instance references this Artifact".
        if self._artifact_instance_references is None:
            return None
        return await self._artifact_instance_references.count(
            session=session,
            artifact_id=artifact_id,
        )


def _next_cursor(
    kind: str,
    *,
    last: tuple[datetime, UUID] | None,
    has_more: bool,
) -> str | None:
    if not has_more or last is None:
        return None
    created_at, row_id = last
    return encode_cursor(kind, created_at=created_at, row_id=row_id)


__all__ = [
    "ARTIFACTS_CURSOR_KIND",
    "CURSOR_VERSION",
    "DOWNLOADS_CURSOR_KIND",
    "ArtifactInstanceReferenceProbe",
    "DownloadTaskItem",
    "DownloadTaskPage",
    "ModelAcquisitionCursorInvalid",
    "ModelAcquisitionReadService",
    "ModelArtifactItem",
    "ModelArtifactPage",
    "ModelArtifactSummary",
    "ModelRootAvailability",
    "StorageAvailabilityProbe",
    "available_commands_for",
    "decode_cursor",
    "encode_cursor",
]
