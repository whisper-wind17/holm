"""Read-only projections behind ``GET /api/v1/downloads`` and ``/artifacts``.

Every statement here is a ``SELECT``. The strongest invariant is that the
two read endpoints leave ``core.models``, ``core.model_artifacts``,
``core.download_tasks`` and ``core.download_subscriptions`` byte-for-byte
unchanged, and the cheapest way to keep that true is for the repository that
serves them to contain no write construct at all.

Ordering is ``created_at DESC, id DESC`` on both lists. ``id`` is a UUIDv7 for
rows this platform creates, but ``core.model_artifacts`` rows are created with
``gen_random_uuid()`` in places, so primary-key order is not time order and an
administrator would not see the most recent work first.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import sqlalchemy as sa
from holm_common import as_utc
from holm_domain import (
    ArtifactStorageType,
    DownloadTaskStatus,
    ModelArtifactStatus,
    ModelOriginType,
)
from sqlalchemy.ext.asyncio import AsyncSession

from holm_persistence.model_domain import DOWNLOAD_TASKS, MODEL_ARTIFACTS, MODELS
from holm_persistence.model_instances import MODEL_INSTANCES

_LIKE_ESCAPE: str = "\\"


@dataclass(frozen=True, slots=True)
class KeysetPosition:
    """The ``(created_at, id)`` pair a page continues after."""

    created_at: datetime
    id: UUID


@dataclass(frozen=True, slots=True, repr=False)
class DownloadTaskListRecord:
    """One Download Task row plus its Model display name.

    ``staging_path`` is a server path, deliberately projected so the Download Task
    table can show the temporary file. The custom
    ``__repr__`` keeps it out of exception text and log records, which is where
    a dataclass repr normally leaks.
    """

    id: UUID
    model_id: UUID
    display_name: str
    source_type: ModelOriginType
    repository_id: str
    resolved_revision: str | None
    status: DownloadTaskStatus
    attempt: int
    downloaded_bytes: int
    total_bytes: int | None
    staging_path: str | None
    error_code: str | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None

    def __repr__(self) -> str:
        return (
            f"DownloadTaskListRecord(id={self.id!r}, status={self.status!r}, "
            "staging_path=<redacted>)"
        )


@dataclass(frozen=True, slots=True, repr=False)
class ModelArtifactListRecord:
    """One Model Artifact row plus its Model display name; ``path`` is redacted."""

    id: UUID
    model_id: UUID
    display_name: str
    source_type: ModelOriginType
    repository_id: str
    resolved_revision: str
    status: ModelArtifactStatus
    storage_type: ArtifactStorageType
    size_bytes: int
    path: str
    created_at: datetime
    updated_at: datetime

    def __repr__(self) -> str:
        return (
            f"ModelArtifactListRecord(id={self.id!r}, status={self.status!r}, "
            f"storage_type={self.storage_type!r}, path=<redacted>)"
        )


@dataclass(frozen=True, slots=True)
class ModelArtifactFilters:
    """Already-validated filter values; ``None`` means "do not restrict"."""

    search: str | None = None
    source: ModelOriginType | None = None
    status: ModelArtifactStatus | None = None
    storage_type: ArtifactStorageType | None = None


@dataclass(frozen=True, slots=True)
class ModelArtifactStorageTotals:
    """Whole-system storage facts, deliberately independent of the filters.

    The Local models page shows these above the table, so they describe the
    platform rather than the current query: narrowing the table to one source
    must not make the reported occupancy shrink.
    """

    managed_bytes: int
    active_download_tasks: int


def _escaped_like_term(term: str) -> str:
    escaped = term.replace(_LIKE_ESCAPE, _LIKE_ESCAPE * 2)
    for wildcard in ("%", "_"):
        escaped = escaped.replace(wildcard, f"{_LIKE_ESCAPE}{wildcard}")
    return f"%{escaped}%"


def _keyset_order(
    statement: sa.Select[tuple[object, ...]],
    table: sa.Table,
    *,
    after: KeysetPosition | None,
    page_size: int,
) -> sa.Select[tuple[object, ...]]:
    ordered = statement.order_by(table.c.created_at.desc(), table.c.id.desc())
    if after is not None:
        ordered = ordered.where(
            sa.tuple_(table.c.created_at, table.c.id)
            < sa.tuple_(sa.literal(as_utc(after.created_at)), sa.literal(after.id))
        )
    return ordered.limit(page_size + 1)


class ModelAcquisitionReadRepository:
    """The only query surface for the two read-only acquisition lists."""

    __slots__ = ("_session",)

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_download_tasks(
        self,
        *,
        after: KeysetPosition | None,
        page_size: int,
        statuses: tuple[DownloadTaskStatus, ...] = (),
    ) -> tuple[DownloadTaskListRecord, ...]:
        """Return one row more than requested so the caller can issue a cursor."""

        statement = _keyset_order(
            sa.select(
                DOWNLOAD_TASKS.c.id,
                DOWNLOAD_TASKS.c.model_id,
                MODELS.c.display_name,
                DOWNLOAD_TASKS.c.source_type,
                DOWNLOAD_TASKS.c.repository_id,
                DOWNLOAD_TASKS.c.resolved_revision,
                DOWNLOAD_TASKS.c.status,
                DOWNLOAD_TASKS.c.attempt,
                DOWNLOAD_TASKS.c.downloaded_bytes,
                DOWNLOAD_TASKS.c.total_bytes,
                DOWNLOAD_TASKS.c.staging_path,
                DOWNLOAD_TASKS.c.error_code,
                DOWNLOAD_TASKS.c.created_at,
                DOWNLOAD_TASKS.c.updated_at,
                DOWNLOAD_TASKS.c.completed_at,
            ).join(MODELS, MODELS.c.id == DOWNLOAD_TASKS.c.model_id),
            DOWNLOAD_TASKS,
            after=after,
            page_size=page_size,
        )
        if statuses:
            statement = statement.where(DOWNLOAD_TASKS.c.status.in_([s.value for s in statuses]))
        mappings = (await self._session.execute(statement)).mappings().all()
        return tuple(
            DownloadTaskListRecord(
                id=mapping["id"],
                model_id=mapping["model_id"],
                display_name=str(mapping["display_name"]),
                source_type=ModelOriginType(mapping["source_type"]),
                repository_id=str(mapping["repository_id"]),
                resolved_revision=(
                    None
                    if mapping["resolved_revision"] is None
                    else str(mapping["resolved_revision"])
                ),
                status=DownloadTaskStatus(mapping["status"]),
                attempt=int(mapping["attempt"]),
                downloaded_bytes=int(mapping["downloaded_bytes"]),
                total_bytes=(
                    None if mapping["total_bytes"] is None else int(mapping["total_bytes"])
                ),
                staging_path=(
                    None if mapping["staging_path"] is None else str(mapping["staging_path"])
                ),
                error_code=None if mapping["error_code"] is None else str(mapping["error_code"]),
                created_at=as_utc(mapping["created_at"]),
                updated_at=as_utc(mapping["updated_at"]),
                completed_at=(
                    None if mapping["completed_at"] is None else as_utc(mapping["completed_at"])
                ),
            )
            for mapping in mappings
        )

    async def list_artifacts(
        self,
        *,
        after: KeysetPosition | None,
        page_size: int,
        filters: ModelArtifactFilters,
        artifact_id: UUID | None = None,
    ) -> tuple[ModelArtifactListRecord, ...]:
        """Return one row more than requested so the caller can issue a cursor."""

        statement = sa.select(
            MODEL_ARTIFACTS.c.id,
            MODEL_ARTIFACTS.c.model_id,
            MODELS.c.display_name,
            MODEL_ARTIFACTS.c.source_type,
            MODEL_ARTIFACTS.c.repository_id,
            MODEL_ARTIFACTS.c.resolved_revision,
            MODEL_ARTIFACTS.c.status,
            MODEL_ARTIFACTS.c.storage_type,
            MODEL_ARTIFACTS.c.size_bytes,
            MODEL_ARTIFACTS.c.path,
            MODEL_ARTIFACTS.c.created_at,
            MODEL_ARTIFACTS.c.updated_at,
        ).join(MODELS, MODELS.c.id == MODEL_ARTIFACTS.c.model_id)
        if artifact_id is not None:
            statement = statement.where(MODEL_ARTIFACTS.c.id == artifact_id)
        if filters.search is not None:
            statement = statement.where(
                MODELS.c.display_name.ilike(
                    _escaped_like_term(filters.search),
                    escape=_LIKE_ESCAPE,
                )
            )
        if filters.source is not None:
            statement = statement.where(MODEL_ARTIFACTS.c.source_type == filters.source.value)
        if filters.status is not None:
            statement = statement.where(MODEL_ARTIFACTS.c.status == filters.status.value)
        if filters.storage_type is not None:
            statement = statement.where(
                MODEL_ARTIFACTS.c.storage_type == filters.storage_type.value
            )
        mappings = (
            (
                await self._session.execute(
                    _keyset_order(
                        statement,
                        MODEL_ARTIFACTS,
                        after=after,
                        page_size=page_size,
                    )
                )
            )
            .mappings()
            .all()
        )
        return tuple(
            ModelArtifactListRecord(
                id=mapping["id"],
                model_id=mapping["model_id"],
                display_name=str(mapping["display_name"]),
                source_type=ModelOriginType(mapping["source_type"]),
                repository_id=str(mapping["repository_id"]),
                resolved_revision=str(mapping["resolved_revision"]),
                status=ModelArtifactStatus(mapping["status"]),
                storage_type=ArtifactStorageType(mapping["storage_type"]),
                size_bytes=int(mapping["size_bytes"]),
                path=str(mapping["path"]),
                created_at=as_utc(mapping["created_at"]),
                updated_at=as_utc(mapping["updated_at"]),
            )
            for mapping in mappings
        )

    async def download_status_counts(self) -> dict[DownloadTaskStatus, int]:
        rows = await self._session.execute(
            sa.select(DOWNLOAD_TASKS.c.status, sa.func.count()).group_by(DOWNLOAD_TASKS.c.status)
        )
        counts = dict.fromkeys(DownloadTaskStatus, 0)
        counts.update({DownloadTaskStatus(status): int(count) for status, count in rows})
        return counts

    async def artifact_details(self, artifact_id: UUID) -> dict[str, object]:
        model = (
            (
                await self._session.execute(
                    sa.select(
                        MODELS.c.architecture,
                        MODELS.c.modalities,
                        MODELS.c.precision,
                        MODELS.c.quantization,
                        MODELS.c.context_length,
                        MODELS.c.license,
                    )
                    .join(MODEL_ARTIFACTS, MODEL_ARTIFACTS.c.model_id == MODELS.c.id)
                    .where(MODEL_ARTIFACTS.c.id == artifact_id)
                )
            )
            .mappings()
            .one()
        )
        files = await self._session.scalar(
            sa.select(MODEL_ARTIFACTS.c.manifest_files).where(MODEL_ARTIFACTS.c.id == artifact_id)
        )
        tasks = (
            (
                await self._session.execute(
                    sa.select(
                        DOWNLOAD_TASKS.c.id,
                        DOWNLOAD_TASKS.c.status,
                        DOWNLOAD_TASKS.c.created_at,
                        DOWNLOAD_TASKS.c.completed_at,
                    )
                    .where(DOWNLOAD_TASKS.c.artifact_id == artifact_id)
                    .order_by(DOWNLOAD_TASKS.c.created_at.desc(), DOWNLOAD_TASKS.c.id.desc())
                )
            )
            .mappings()
            .all()
        )
        instances = (
            (
                await self._session.execute(
                    sa.select(
                        MODEL_INSTANCES.c.id, MODEL_INSTANCES.c.name, MODEL_INSTANCES.c.status
                    )
                    .where(MODEL_INSTANCES.c.artifact_id == artifact_id)
                    .order_by(MODEL_INSTANCES.c.created_at.desc(), MODEL_INSTANCES.c.id.desc())
                )
            )
            .mappings()
            .all()
        )
        return {
            "model": dict(model),
            "files": None if files is None else sorted(files, key=lambda entry: entry["path"]),
            "download_tasks": [dict(task) for task in tasks],
            "instances": [dict(instance) for instance in instances],
        }

    async def storage_totals(
        self,
        *,
        active_download_statuses: tuple[str, ...],
    ) -> ModelArtifactStorageTotals:
        """Sum only platform-managed bytes and count only in-progress tasks.

        ``active_download_statuses`` is injected rather than spelled here so the
        "in progress" vocabulary keeps exactly one definition,
        ``ACTIVE_DOWNLOAD_STATUSES``.

        External Artifacts are excluded from the sum on purpose: the platform
        does not own those bytes, and they may live on a different filesystem
        than the one whose free space is reported next to this number.
        """

        managed_bytes = (
            sa.select(sa.func.coalesce(sa.func.sum(MODEL_ARTIFACTS.c.size_bytes), 0))
            .where(MODEL_ARTIFACTS.c.storage_type == ArtifactStorageType.MANAGED.value)
            .scalar_subquery()
        )
        active_tasks = (
            sa.select(sa.func.count())
            .select_from(DOWNLOAD_TASKS)
            .where(DOWNLOAD_TASKS.c.status.in_(active_download_statuses))
            .scalar_subquery()
        )
        row = (
            (
                await self._session.execute(
                    sa.select(
                        managed_bytes.label("managed_bytes"),
                        active_tasks.label("active_download_tasks"),
                    )
                )
            )
            .mappings()
            .one()
        )
        return ModelArtifactStorageTotals(
            managed_bytes=int(row["managed_bytes"]),
            active_download_tasks=int(row["active_download_tasks"]),
        )


__all__ = [
    "DownloadTaskListRecord",
    "KeysetPosition",
    "ModelAcquisitionReadRepository",
    "ModelArtifactFilters",
    "ModelArtifactListRecord",
    "ModelArtifactStorageTotals",
]
