"""Per-launch handshake using existing least-privilege event grants.

Requests use the instance aggregate index. Replies use a fresh request UUID and
fixed versions (queued=1, completed=2, authorized=3), independent of SSE replay.
Only Download reads Jobs; Instance consumes the exact request's typed reply.
"""

from uuid import UUID

import sqlalchemy as sa
from holm_common import new_uuid7
from holm_domain import PRODUCTION_EVENT_REGISTRY, DomainEventType, ModelInstanceStatus
from holm_domain.launch_verification import LaunchVerification, LaunchVerificationResult
from sqlalchemy.ext.asyncio import AsyncSession

from holm_persistence.domain_event_repository import DomainEventRepository
from holm_persistence.domain_events import _DOMAIN_EVENTS
from holm_persistence.job_repository import JobDraft, JobRepository
from holm_persistence.jobs import JOBS
from holm_persistence.model_domain import MODEL_ARTIFACTS
from holm_persistence.model_instances import MODEL_INSTANCES

LAUNCH_VERIFY_JOB_TYPE = "artifact.verify_launch"
_REQUEST = DomainEventType.INSTANCE_VERIFICATION_REQUESTED
_QUEUED = DomainEventType.INSTANCE_VERIFICATION_QUEUED
_COMPLETED = DomainEventType.INSTANCE_VERIFICATION_COMPLETED
_AUTHORIZED = DomainEventType.INSTANCE_LAUNCH_AUTHORIZED
_ACTIVE = (ModelInstanceStatus.STARTING.value, ModelInstanceStatus.RESTARTING.value)


class LaunchVerificationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def request(
        self,
        *,
        instance_id: UUID,
        artifact_id: UUID,
        config_version: int,
        status: ModelInstanceStatus,
        attempt: int = 0,
    ) -> None:
        if status.value not in _ACTIVE or attempt < 0:
            raise ValueError("invalid launch verification identity")
        events = DomainEventRepository(self._session)
        await self._append(
            _REQUEST,
            instance_id,
            await events.next_version(aggregate_id=instance_id),
            {
                "request_id": str(new_uuid7()),
                "artifact_id": str(artifact_id),
                "config_version": config_version,
                "status": status.value,
                "attempt": attempt,
            },
        )

    async def current(self, instance_id: UUID) -> LaunchVerification | None:
        instance = (
            (
                await self._session.execute(
                    sa.select(MODEL_INSTANCES).where(MODEL_INSTANCES.c.id == instance_id)
                )
            )
            .mappings()
            .one_or_none()
        )
        if instance is None or instance["status"] not in _ACTIVE:
            return None
        row = (
            (
                await self._session.execute(
                    sa.select(_DOMAIN_EVENTS)
                    .where(
                        _DOMAIN_EVENTS.c.aggregate_id == instance_id,
                        _DOMAIN_EVENTS.c.event == _REQUEST.value,
                    )
                    .order_by(_DOMAIN_EVENTS.c.version.desc())
                    .limit(1)
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        payload = row["payload"]
        if set(payload) != {"request_id", "artifact_id", "config_version", "status", "attempt"}:
            raise ValueError("invalid launch verification request")
        if not all(isinstance(payload[field], str) for field in ("request_id", "artifact_id")):
            raise ValueError("invalid launch verification UUID")
        request_id = UUID(payload["request_id"])
        artifact_id = UUID(payload["artifact_id"])
        config_version, attempt = payload["config_version"], payload["attempt"]
        if (
            type(config_version) is not int
            or config_version < 1
            or type(attempt) is not int
            or attempt < 0
        ):
            raise ValueError("invalid launch verification version")
        if (
            artifact_id != instance["artifact_id"]
            or config_version != instance["config_version"]
            or payload["status"] != instance["status"]
            or (instance["status"] == "Restarting" and attempt != instance["restart_count"])
        ):
            return None
        replies = (
            await self._session.execute(
                sa.select(
                    _DOMAIN_EVENTS.c.event, _DOMAIN_EVENTS.c.payload, _DOMAIN_EVENTS.c.version
                )
                .where(
                    _DOMAIN_EVENTS.c.aggregate_id == request_id,
                )
                .order_by(_DOMAIN_EVENTS.c.version)
                .limit(4)
            )
        ).all()
        if len(replies) > 3:
            raise ValueError("invalid launch verification reply sequence")
        result, authorized = LaunchVerificationResult.PENDING, False
        for expected_version, (event, data, version) in enumerate(replies, 1):
            expected_event = (_QUEUED.value, _COMPLETED.value, _AUTHORIZED.value)[
                expected_version - 1
            ]
            if version != expected_version or event != expected_event:
                raise ValueError("invalid launch verification reply sequence")
            if event == _QUEUED.value:
                if set(data) != {"job_id"} or not isinstance(data["job_id"], str):
                    raise ValueError("invalid launch verification job")
                UUID(data["job_id"])
            elif event == _COMPLETED.value:
                if set(data) != {"result"}:
                    raise ValueError("invalid launch verification result")
                result = LaunchVerificationResult(data["result"])
                if result is LaunchVerificationResult.PENDING:
                    raise ValueError("invalid terminal launch result")
            elif (
                event == _AUTHORIZED.value and not data and result is LaunchVerificationResult.READY
            ):
                authorized = True
            else:
                raise ValueError("unknown launch verification reply")
        return LaunchVerification(
            request_id,
            instance_id,
            artifact_id,
            config_version,
            ModelInstanceStatus(instance["status"]),
            attempt,
            row["occurred_at"],
            result,
            authorized,
        )

    async def authorize(self, expected: LaunchVerification) -> bool:
        # The row fence serializes authorization with Stop, retry and configuration
        # changes; no model files or Jobs are accessible through this method.
        await self._session.execute(
            sa.select(MODEL_INSTANCES.c.id)
            .where(
                MODEL_INSTANCES.c.id == expected.instance_id,
            )
            .with_for_update()
        )
        current = await self.current(expected.instance_id)
        if (
            current is None
            or current.request_id != expected.request_id
            or current.result is not LaunchVerificationResult.READY
        ):
            return False
        artifact_status = await self._session.scalar(
            sa.select(MODEL_ARTIFACTS.c.status).where(
                MODEL_ARTIFACTS.c.id == current.artifact_id,
            )
        )
        if artifact_status != "Ready":
            return False
        if not current.authorized:
            await self._append(_AUTHORIZED, current.request_id, 3, {})
        return True

    async def complete(self, request_id: UUID, result: LaunchVerificationResult) -> None:
        if result is LaunchVerificationResult.PENDING:
            raise ValueError("launch verification completion must be terminal")
        if not await DomainEventRepository(self._session).has_event(
            aggregate_id=request_id,
            event=_COMPLETED.value,
        ):
            await self._append(_COMPLETED, request_id, 2, {"result": result.value})

    async def dispatch_pending(self, *, limit: int = 32) -> None:
        # Iterate active instances using the existing status/aggregate indexes.
        # A terminal/old request cannot monopolize this bounded production pass.
        latest = (
            sa.select(_DOMAIN_EVENTS.c.payload)
            .where(
                _DOMAIN_EVENTS.c.aggregate_id == MODEL_INSTANCES.c.id,
                _DOMAIN_EVENTS.c.event == _REQUEST.value,
            )
            .order_by(_DOMAIN_EVENTS.c.version.desc())
            .limit(1)
            .lateral()
        )
        completed = _DOMAIN_EVENTS.alias("completed_launch_verification")
        queued = _DOMAIN_EVENTS.alias("queued_launch_verification")
        ids = (
            await self._session.execute(
                sa.select(MODEL_INSTANCES.c.id)
                .join(latest, sa.true())
                .where(
                    MODEL_INSTANCES.c.status.in_(_ACTIVE),
                    latest.c.payload["status"].astext == MODEL_INSTANCES.c.status,
                    latest.c.payload["artifact_id"].astext
                    == sa.cast(MODEL_INSTANCES.c.artifact_id, sa.Text),
                    latest.c.payload["config_version"].astext
                    == sa.cast(MODEL_INSTANCES.c.config_version, sa.Text),
                    sa.or_(
                        MODEL_INSTANCES.c.status == "Starting",
                        latest.c.payload["attempt"].astext
                        == sa.cast(MODEL_INSTANCES.c.restart_count, sa.Text),
                    ),
                    ~sa.exists(
                        sa.select(queued.c.id)
                        .join(
                            JOBS,
                            JOBS.c.id == sa.cast(queued.c.payload["job_id"].astext, sa.Uuid),
                        )
                        .where(
                            queued.c.aggregate_id
                            == sa.cast(latest.c.payload["request_id"].astext, sa.Uuid),
                            queued.c.event == _QUEUED.value,
                            JOBS.c.status.in_(("Queued", "Running")),
                        )
                    ),
                    ~sa.exists(
                        sa.select(completed.c.id).where(
                            completed.c.aggregate_id
                            == sa.cast(latest.c.payload["request_id"].astext, sa.Uuid),
                            completed.c.event == _COMPLETED.value,
                        )
                    ),
                )
                .order_by(MODEL_INSTANCES.c.id)
                .limit(limit)
            )
        ).scalars()
        processed = 0
        for instance_id in ids:
            current = await self.current(instance_id)
            if current is None or current.result is not LaunchVerificationResult.PENDING:
                continue
            # Both dispatch and handler serialize on this request only. DOWNLOAD
            # has SELECT on instances, so deliberately no row FOR UPDATE here.
            key = int.from_bytes(current.request_id.bytes[:8], "big", signed=True)
            locked = await self._session.scalar(sa.select(sa.func.pg_try_advisory_xact_lock(key)))
            if not locked:
                continue
            job_id = await self._session.scalar(
                sa.select(_DOMAIN_EVENTS.c.payload).where(
                    _DOMAIN_EVENTS.c.aggregate_id == current.request_id,
                    _DOMAIN_EVENTS.c.event == _QUEUED.value,
                )
            )
            jobs = JobRepository(self._session)
            if job_id is None:
                job = await jobs.enqueue(
                    JobDraft(
                        job_type=LAUNCH_VERIFY_JOB_TYPE,
                        target_object_id=current.artifact_id,
                        target_object_version=current.config_version,
                        payload={
                            "request_id": str(current.request_id),
                            "instance_id": str(instance_id),
                        },
                    )
                )
                await self._append(_QUEUED, current.request_id, 1, {"job_id": str(job.id)})
            else:
                existing_job = await jobs.get(UUID(job_id["job_id"]))
                if existing_job is None or existing_job.status.value in {"Failed", "Succeeded"}:
                    # A succeeded Job without its typed result is a protocol error,
                    # never a license to infer success from inventory Ready.
                    await self.complete(current.request_id, LaunchVerificationResult.FAILED)
            processed += 1
            if processed >= limit:
                break

    async def _append(
        self,
        event: DomainEventType,
        aggregate_id: UUID,
        version: int,
        payload: dict[str, object],
    ) -> None:
        await DomainEventRepository(self._session).append(
            PRODUCTION_EVENT_REGISTRY.build_draft(
                event.value,
                aggregate_id=aggregate_id,
                version=version,
                candidates=payload,
            )
        )
