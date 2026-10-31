"""Read and clean the dynamically registered Model catalog."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from holm_contracts import CursorPageParams
from holm_domain import LocalAvailability
from holm_persistence import (
    AsyncSessionFactory,
    ModelInstanceReferenceRepository,
    ModelRecord,
    ModelRepository,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from holm_control_api.model_resolution_service import ACTIVE_DOWNLOAD_STATUSES


class ModelCatalogCursorInvalid(ValueError):
    """The opaque catalog cursor is not one issued by this endpoint."""


class ModelCatalogNotFound(LookupError):
    """The requested Model definition does not exist."""


class ModelCatalogReferenced(RuntimeError):
    """The requested Model definition still has a blocking reference."""


class ModelInstanceReferenceProbe(Protocol):
    """Connection point for the not-yet-materialized instance relation."""

    async def count(self, *, session: AsyncSession, model_id: UUID) -> int: ...


@dataclass(frozen=True, slots=True)
class ModelCatalogReferences:
    artifacts: int
    download_tasks: int
    model_instances: int | None


@dataclass(frozen=True, slots=True)
class ModelCatalogItem:
    model: ModelRecord
    local_availability: LocalAvailability
    resolved_revision: str | None
    references: ModelCatalogReferences


@dataclass(frozen=True, slots=True)
class ModelCatalogPage:
    items: tuple[ModelCatalogItem, ...]
    next_cursor: str | None


class ModelCatalogService:
    """Catalog queries and reference-guarded Model definition cleanup."""

    __slots__ = ("_instance_references", "_session_factory")
    _instance_references: ModelInstanceReferenceProbe | None

    def __init__(
        self,
        *,
        session_factory: AsyncSessionFactory,
        instance_references: ModelInstanceReferenceProbe | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._instance_references = instance_references or ModelInstanceReferenceRepository()

    async def list(self, params: CursorPageParams) -> ModelCatalogPage:
        cursor = self._parse_cursor(params.cursor)
        async with self._session_factory() as session:
            records = await ModelRepository(session).list_catalog(
                cursor=cursor,
                page_size=params.page_size,
                active_download_statuses=ACTIVE_DOWNLOAD_STATUSES,
            )
            items: list[ModelCatalogItem] = []
            for record in records[: params.page_size]:
                instance_count = await self._instance_reference_count(
                    session=session,
                    model_id=record.model.id,
                )
                items.append(
                    ModelCatalogItem(
                        model=record.model,
                        local_availability=record.local_availability,
                        resolved_revision=record.resolved_revision,
                        references=ModelCatalogReferences(
                            artifacts=record.artifact_reference_count,
                            download_tasks=record.download_task_reference_count,
                            model_instances=instance_count,
                        ),
                    )
                )
        next_cursor = str(items[-1].model.id) if len(records) > params.page_size else None
        return ModelCatalogPage(items=tuple(items), next_cursor=next_cursor)

    async def delete(
        self,
        model_id: UUID,
        *,
        actor_id: UUID,
    ) -> None:
        async with self._session_factory() as session:
            repository = ModelRepository(session)
            references = await repository.reference_counts(model_id)
            if references is None:
                raise ModelCatalogNotFound()
            instance_count = await self._instance_reference_count(
                session=session,
                model_id=model_id,
            )
            if (
                references.artifacts > 0
                or references.download_tasks > 0
                or (instance_count is not None and instance_count > 0)
            ):
                raise ModelCatalogReferenced()
            integrity_conflict = False
            try:
                deleted = await repository.delete_if_unreferenced(model_id)
                if not deleted:
                    raise ModelCatalogReferenced()
                await session.commit()
            except IntegrityError:
                await session.rollback()
                integrity_conflict = True
            if integrity_conflict:
                # Keep database diagnostics out of any reachable exception chain.
                raise ModelCatalogReferenced()

    async def _instance_reference_count(
        self,
        *,
        session: AsyncSession,
        model_id: UUID,
    ) -> int | None:
        # Query the real core.model_instances relation when a persistence adapter is available.
        if self._instance_references is None:
            return None
        return await self._instance_references.count(session=session, model_id=model_id)

    @staticmethod
    def _parse_cursor(cursor: str | None) -> UUID | None:
        if cursor is None:
            return None
        try:
            parsed = UUID(cursor)
        except ValueError:
            parsed = None
        if parsed is None:
            # Raised after the parser exception leaves scope so the raw cursor
            # cannot be recovered through ``__context__``.
            raise ModelCatalogCursorInvalid()
        return parsed


__all__ = [
    "ModelCatalogCursorInvalid",
    "ModelCatalogItem",
    "ModelCatalogNotFound",
    "ModelCatalogPage",
    "ModelCatalogReferenced",
    "ModelCatalogReferences",
    "ModelCatalogService",
    "ModelInstanceReferenceProbe",
]
