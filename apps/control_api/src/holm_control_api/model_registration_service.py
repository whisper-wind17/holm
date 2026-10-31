"""Explicit Model catalog registration with transactional HTTP idempotency."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path
from typing import Final
from uuid import UUID

from holm_domain import (
    Identified,
    MetadataStatus,
    ModelOriginType,
    ModelSourceType,
    Unrecognized,
    to_persisted_column,
)
from holm_domain.model_layout import DeployableLayout, deployable_layout
from holm_persistence import (
    AsyncSessionFactory,
    IdempotencyRepository,
    ModelRecord,
    ModelRepository,
    ModelSourceRepository,
    ModelUpsert,
    database_utc_now,
    idempotency_digests,
    normalize_source_key,
)

from holm_control_api.model_resolution import (
    ExternalPathAccepted,
    ResolutionFailed,
    ResolutionSucceeded,
)
from holm_control_api.model_resolution_service import (
    ModelLayoutUnsupported,
    ModelResolutionService,
    ResolveInputRejected,
)

IDEMPOTENCY_LIFETIME: Final[timedelta] = timedelta(hours=24)
REGISTER_MODEL_OPERATION: Final[str] = "model.register"
_LOGGER: Final[logging.Logger] = logging.getLogger("holm.control_api.model_registration")


class IdempotencyConflict(RuntimeError):
    """The actor reused an unexpired key for a different normalized request."""

    def __init__(self) -> None:
        super().__init__("idempotency request conflicted")


@dataclass(frozen=True, slots=True)
class ModelRegistrationSuccess:
    """A committed catalog object, whether newly claimed or replayed."""

    model: ModelRecord
    replayed: bool
    requested_revision_norm: str | None


type ModelRegistrationResult = ModelRegistrationSuccess | ResolutionFailed


class ModelRegistrationService:
    """Resolve now, then atomically upsert a Model and claim the client key."""

    __slots__ = ("_hmac_secret", "_resolution", "_session_factory")

    def __init__(
        self,
        *,
        session_factory: AsyncSessionFactory,
        resolution: ModelResolutionService,
        idempotency_hmac_secret: bytes,
    ) -> None:
        self._session_factory = session_factory
        self._resolution = resolution
        self._hmac_secret = idempotency_hmac_secret

    async def external_candidate(
        self, external_path: str | None, *, repository_id: str | None, revision: str | None
    ) -> ModelUpsert | ResolutionFailed:
        """Recheck the canonical path before the caller's atomic registration transaction."""
        from holm_control_api.external_path_resolution import EXTERNAL_PATH_UNUSABLE_RESULT

        try:
            preview = await self._resolution.resolve(
                source=ModelOriginType.LOCAL_PATH,
                repository_id=repository_id,
                external_path=external_path,
                revision=revision,
            )
        except ResolveInputRejected:
            return ResolutionFailed(EXTERNAL_PATH_UNUSABLE_RESULT)
        if isinstance(preview.outcome, ResolutionFailed):
            return preview.outcome
        assert isinstance(preview.outcome, ExternalPathAccepted)
        assert external_path is not None
        try:
            canonical = await asyncio.to_thread(Path(external_path).resolve, strict=True)
        except (OSError, RuntimeError):
            return ResolutionFailed(EXTERNAL_PATH_UNUSABLE_RESULT)
        # Re-evaluate the canonical name too; never accept a newly escaped link target.
        checked = await self._resolution.resolve(
            source=ModelOriginType.LOCAL_PATH,
            repository_id=None,
            external_path=str(canonical),
            revision=None,
        )
        if isinstance(checked.outcome, ResolutionFailed):
            return checked.outcome
        candidate = self._registration_values(outcome=preview.outcome, external_path=str(canonical))
        # Filesystem identities must retain their spelling: NFKC is appropriate
        # for repository identifiers, but may name a different directory.
        return replace(candidate, source_key=str(canonical), display_name=canonical.name)

    async def register(
        self,
        *,
        actor_id: UUID,
        idempotency_key: str,
        source: ModelOriginType,
        repository_id: str | None,
        external_path: str | None,
        revision: str | None,
    ) -> ModelRegistrationResult:
        """Re-resolve this request; a failed resolution writes no row at all."""

        preview = await self._resolution.resolve(
            source=source,
            repository_id=repository_id,
            external_path=external_path,
            revision=revision,
        )
        if isinstance(preview.outcome, ResolutionFailed):
            _LOGGER.info("model registration: resolution failed")
            return preview.outcome

        if isinstance(preview.outcome, ResolutionSucceeded):
            files = preview.outcome.resolved.files
            if (
                deployable_layout(
                    (entry.path for entry in files.value) if isinstance(files, Identified) else None
                )
                is DeployableLayout.UNSUPPORTED
            ):
                raise ModelLayoutUnsupported()

        candidate = self._registration_values(
            outcome=preview.outcome,
            external_path=external_path,
        )
        normalized_request = json.dumps(
            {
                "source": candidate.source_type.value,
                "source_key": candidate.source_key,
                "requested_revision": (
                    None
                    if isinstance(preview.outcome, ExternalPathAccepted)
                    else preview.outcome.resolved.requested_revision_norm
                ),
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        digests = idempotency_digests(
            hmac_secret=self._hmac_secret,
            idempotency_key=idempotency_key,
            normalized_request=normalized_request,
        )

        async with self._session_factory() as session:
            now = await database_utc_now(session)
            repository = ModelRepository(session)
            if candidate.source_type is not ModelOriginType.LOCAL_PATH:
                source_record = await ModelSourceRepository(session).ensure(
                    source_type=ModelSourceType(candidate.source_type.value),
                    now=now,
                )
                candidate = ModelUpsert(
                    source_id=source_record.id,
                    source_type=candidate.source_type,
                    source_key=candidate.source_key,
                    display_name=candidate.display_name,
                    repository_id=candidate.repository_id,
                    external_path=candidate.external_path,
                    architecture=candidate.architecture,
                    modalities=candidate.modalities,
                    precision=candidate.precision,
                    quantization=candidate.quantization,
                    context_length=candidate.context_length,
                    supported_runtimes=candidate.supported_runtimes,
                    estimated_size_bytes=candidate.estimated_size_bytes,
                    license=candidate.license,
                    metadata_status=candidate.metadata_status,
                )
            model = await repository.upsert(candidate, now=now)
            claim = await IdempotencyRepository(session).claim(
                actor_id=actor_id,
                operation=REGISTER_MODEL_OPERATION,
                digests=digests,
                response_object_id=model.id,
                now=now,
                expires_before=now - IDEMPOTENCY_LIFETIME,
            )
            if not claim.claimed:
                await session.rollback()
                if not hmac.compare_digest(claim.record.request_hash, digests.request_hash):
                    raise IdempotencyConflict()
                replayed = await self._model(claim.record.response_object_id)
                _LOGGER.info("model registration: replayed")
                return ModelRegistrationSuccess(
                    model=replayed,
                    replayed=True,
                    requested_revision_norm=(
                        None
                        if isinstance(preview.outcome, ExternalPathAccepted)
                        else preview.outcome.resolved.requested_revision_norm
                    ),
                )
            await session.commit()

        _LOGGER.info("model registration: committed")
        return ModelRegistrationSuccess(
            model=model,
            replayed=False,
            requested_revision_norm=(
                None
                if isinstance(preview.outcome, ExternalPathAccepted)
                else preview.outcome.resolved.requested_revision_norm
            ),
        )

    async def _model(self, model_id: UUID) -> ModelRecord:
        async with self._session_factory() as session:
            record = await ModelRepository(session).get(model_id)
        if record is None:
            raise RuntimeError("idempotency response object is unavailable")
        return record

    @staticmethod
    def _registration_values(
        *,
        outcome: ResolutionSucceeded | ExternalPathAccepted,
        external_path: str | None,
    ) -> ModelUpsert:
        if isinstance(outcome, ResolutionSucceeded):
            resolved = outcome.resolved
            source_key = normalize_source_key(resolved.repository_id)
            return ModelUpsert(
                source_id=None,
                source_type=resolved.source,
                source_key=source_key,
                display_name=source_key,
                repository_id=source_key,
                external_path=None,
                architecture=to_persisted_column(resolved.architecture),
                modalities=to_persisted_column(resolved.modalities),
                precision=to_persisted_column(resolved.precision),
                quantization=to_persisted_column(resolved.quantization),
                context_length=to_persisted_column(resolved.context_length),
                supported_runtimes=to_persisted_column(resolved.candidate_runtimes),
                estimated_size_bytes=to_persisted_column(resolved.total_size_bytes),
                license=to_persisted_column(resolved.license),
                metadata_status=MetadataStatus.CONFIRMED,
            )
        if external_path is None:
            raise RuntimeError("accepted External Path lost its request identity")
        source_key = normalize_source_key(external_path)
        unrecognized = Unrecognized()
        return ModelUpsert(
            source_id=None,
            source_type=ModelOriginType.LOCAL_PATH,
            source_key=source_key,
            display_name=Path(source_key).name,
            repository_id=None,
            external_path=external_path,
            architecture=to_persisted_column(unrecognized),
            modalities=to_persisted_column(unrecognized),
            precision=to_persisted_column(unrecognized),
            quantization=to_persisted_column(unrecognized),
            context_length=to_persisted_column(unrecognized),
            supported_runtimes=to_persisted_column(unrecognized),
            estimated_size_bytes=to_persisted_column(unrecognized),
            license=to_persisted_column(unrecognized),
            metadata_status=MetadataStatus.UNCONFIRMED,
        )


__all__ = [
    "IDEMPOTENCY_LIFETIME",
    "REGISTER_MODEL_OPERATION",
    "IdempotencyConflict",
    "ModelRegistrationResult",
    "ModelRegistrationService",
    "ModelRegistrationSuccess",
]
