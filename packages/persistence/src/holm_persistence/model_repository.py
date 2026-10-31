"""Repository for the dynamically registered Model catalog."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import sqlalchemy as sa
from holm_common import as_utc, new_uuid7
from holm_domain import (
    LocalAvailability,
    MetadataStatus,
    ModelArtifactStatus,
    ModelOriginType,
)
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.ext.asyncio import AsyncSession

from holm_persistence.model_domain import DOWNLOAD_TASKS, MODEL_ARTIFACTS, MODELS


@dataclass(frozen=True, slots=True)
class ModelUpsert:
    """Server-resolved fields accepted by the catalog write boundary."""

    source_id: UUID | None
    source_type: ModelOriginType
    source_key: str
    display_name: str
    repository_id: str | None
    external_path: str | None
    architecture: str | None
    modalities: tuple[str, ...] | None
    precision: str | None
    quantization: str | None
    context_length: int | None
    supported_runtimes: tuple[str, ...] | None
    estimated_size_bytes: int | None
    license: str | None
    metadata_status: MetadataStatus


@dataclass(frozen=True, slots=True, repr=False)
class ModelRecord:
    """One catalog row; an External Path never appears in its repr."""

    id: UUID
    source_id: UUID | None
    source_type: ModelOriginType
    source_key: str
    display_name: str
    repository_id: str | None
    external_path: str | None
    architecture: str | None
    modalities: tuple[str, ...] | None
    precision: str | None
    quantization: str | None
    context_length: int | None
    supported_runtimes: tuple[str, ...] | None
    estimated_size_bytes: int | None
    license: str | None
    metadata_status: MetadataStatus
    created_at: datetime
    updated_at: datetime

    def __repr__(self) -> str:
        return (
            "ModelRecord("
            f"id={self.id!r}, source_type={self.source_type!r}, "
            f"metadata_status={self.metadata_status!r}, external_path=<redacted>)"
        )


@dataclass(frozen=True, slots=True)
class ModelCatalogRecord:
    """One safe catalog projection plus database-derived reference facts."""

    model: ModelRecord
    local_availability: LocalAvailability
    resolved_revision: str | None
    artifact_reference_count: int
    download_task_reference_count: int


@dataclass(frozen=True, slots=True)
class ModelReferenceCounts:
    """Known references that must block Model definition cleanup."""

    artifacts: int
    download_tasks: int


def _optional_strings(value: object) -> tuple[str, ...] | None:
    if value is None:
        return None
    if not isinstance(value, (list, tuple)) or any(not isinstance(item, str) for item in value):
        raise RuntimeError("persisted Model string collection is malformed")
    return tuple(value)


def _record(mapping: sa.RowMapping) -> ModelRecord:
    return ModelRecord(
        id=mapping["id"],
        source_id=mapping["source_id"],
        source_type=ModelOriginType(mapping["source_type"]),
        source_key=str(mapping["source_key"]),
        display_name=str(mapping["display_name"]),
        repository_id=mapping["repository_id"],
        external_path=mapping["external_path"],
        architecture=mapping["architecture"],
        modalities=_optional_strings(mapping["modalities"]),
        precision=mapping["precision"],
        quantization=mapping["quantization"],
        context_length=mapping["context_length"],
        supported_runtimes=_optional_strings(mapping["supported_runtimes"]),
        estimated_size_bytes=mapping["estimated_size_bytes"],
        license=mapping["license"],
        metadata_status=MetadataStatus(mapping["metadata_status"]),
        created_at=as_utc(mapping["created_at"]),
        updated_at=as_utc(mapping["updated_at"]),
    )


class ModelRepository:
    """Upsert Model metadata and let ``uq_model_source_key`` serialize races."""

    __slots__ = ("_session",)

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(self, value: ModelUpsert, *, now: datetime) -> ModelRecord:
        persisted = {
            "source_id": value.source_id,
            "source_type": value.source_type.value,
            "source_key": value.source_key,
            "display_name": value.display_name,
            "repository_id": value.repository_id,
            "external_path": value.external_path,
            "architecture": value.architecture,
            "modalities": value.modalities,
            "precision": value.precision,
            "quantization": value.quantization,
            "context_length": value.context_length,
            "supported_runtimes": value.supported_runtimes,
            "estimated_size_bytes": value.estimated_size_bytes,
            "license": value.license,
            "metadata_status": value.metadata_status.value,
            "updated_at": as_utc(now),
        }
        insert = postgresql_insert(MODELS).values(
            id=new_uuid7(),
            created_at=as_utc(now),
            **persisted,
        )
        statement = insert.on_conflict_do_update(
            index_elements=[MODELS.c.source_type, MODELS.c.source_key],
            set_=persisted,
        ).returning(*MODELS.c)
        return _record((await self._session.execute(statement)).mappings().one())

    async def get(self, model_id: UUID) -> ModelRecord | None:
        mapping = (
            (await self._session.execute(sa.select(MODELS).where(MODELS.c.id == model_id)))
            .mappings()
            .one_or_none()
        )
        return None if mapping is None else _record(mapping)

    async def list_catalog(
        self,
        *,
        cursor: UUID | None,
        page_size: int,
        active_download_statuses: tuple[str, ...],
    ) -> tuple[ModelCatalogRecord, ...]:
        """Return one extra row so the caller can issue an opaque next cursor."""

        ready_artifact = (
            sa.select(MODEL_ARTIFACTS.c.resolved_revision)
            .where(
                MODEL_ARTIFACTS.c.model_id == MODELS.c.id,
                MODEL_ARTIFACTS.c.status == ModelArtifactStatus.READY.value,
            )
            .order_by(MODEL_ARTIFACTS.c.updated_at.desc(), MODEL_ARTIFACTS.c.id.desc())
            .limit(1)
            .correlate(MODELS)
        )
        active_download = (
            sa.select(DOWNLOAD_TASKS.c.resolved_revision)
            .where(
                DOWNLOAD_TASKS.c.model_id == MODELS.c.id,
                DOWNLOAD_TASKS.c.status.in_(active_download_statuses),
            )
            .order_by(DOWNLOAD_TASKS.c.updated_at.desc(), DOWNLOAD_TASKS.c.id.desc())
            .limit(1)
            .correlate(MODELS)
        )
        has_active_download = sa.exists(
            sa.select(DOWNLOAD_TASKS.c.id).where(
                DOWNLOAD_TASKS.c.model_id == MODELS.c.id,
                DOWNLOAD_TASKS.c.status.in_(active_download_statuses),
            )
        )
        artifact_count = (
            sa.select(sa.func.count())
            .select_from(MODEL_ARTIFACTS)
            .where(MODEL_ARTIFACTS.c.model_id == MODELS.c.id)
            .correlate(MODELS)
            .scalar_subquery()
        )
        download_count = (
            sa.select(sa.func.count())
            .select_from(DOWNLOAD_TASKS)
            .where(DOWNLOAD_TASKS.c.model_id == MODELS.c.id)
            .correlate(MODELS)
            .scalar_subquery()
        )
        statement = sa.select(
            MODELS,
            ready_artifact.scalar_subquery().label("ready_revision"),
            active_download.scalar_subquery().label("active_download_revision"),
            has_active_download.label("has_active_download"),
            artifact_count.label("artifact_reference_count"),
            download_count.label("download_task_reference_count"),
        ).order_by(MODELS.c.id)
        if cursor is not None:
            statement = statement.where(MODELS.c.id > cursor)
        mappings = (await self._session.execute(statement.limit(page_size + 1))).mappings().all()
        records: list[ModelCatalogRecord] = []
        for mapping in mappings:
            ready_revision = mapping["ready_revision"]
            active_revision = mapping["active_download_revision"]
            resolved_revision: str | None
            if ready_revision is not None:
                availability = LocalAvailability.ARTIFACT_READY
                resolved_revision = str(ready_revision)
            elif bool(mapping["has_active_download"]):
                availability = LocalAvailability.DOWNLOAD_ACTIVE
                resolved_revision = None if active_revision is None else str(active_revision)
            else:
                availability = LocalAvailability.ABSENT
                resolved_revision = None
            records.append(
                ModelCatalogRecord(
                    model=_record(mapping),
                    local_availability=availability,
                    resolved_revision=resolved_revision,
                    artifact_reference_count=int(mapping["artifact_reference_count"]),
                    download_task_reference_count=int(mapping["download_task_reference_count"]),
                )
            )
        return tuple(records)

    async def reference_counts(self, model_id: UUID) -> ModelReferenceCounts | None:
        """Read all currently materialized Model reference relations."""

        artifact_count = (
            sa.select(sa.func.count())
            .select_from(MODEL_ARTIFACTS)
            .where(MODEL_ARTIFACTS.c.model_id == MODELS.c.id)
            .correlate(MODELS)
            .scalar_subquery()
        )
        download_count = (
            sa.select(sa.func.count())
            .select_from(DOWNLOAD_TASKS)
            .where(DOWNLOAD_TASKS.c.model_id == MODELS.c.id)
            .correlate(MODELS)
            .scalar_subquery()
        )
        row = (
            (
                await self._session.execute(
                    sa.select(
                        artifact_count.label("artifact_count"),
                        download_count.label("download_count"),
                    ).where(MODELS.c.id == model_id)
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        return ModelReferenceCounts(
            artifacts=int(row["artifact_count"]),
            download_tasks=int(row["download_count"]),
        )

    async def delete_if_unreferenced(self, model_id: UUID) -> bool:
        """Atomically delete only when both existing child relations are empty."""

        statement = (
            sa.delete(MODELS)
            .where(
                MODELS.c.id == model_id,
                ~sa.exists(
                    sa.select(MODEL_ARTIFACTS.c.id).where(MODEL_ARTIFACTS.c.model_id == MODELS.c.id)
                ),
                ~sa.exists(
                    sa.select(DOWNLOAD_TASKS.c.id).where(DOWNLOAD_TASKS.c.model_id == MODELS.c.id)
                ),
            )
            .returning(MODELS.c.id)
        )
        return (await self._session.execute(statement)).scalar_one_or_none() is not None


__all__ = [
    "ModelCatalogRecord",
    "ModelRecord",
    "ModelReferenceCounts",
    "ModelRepository",
    "ModelUpsert",
]
