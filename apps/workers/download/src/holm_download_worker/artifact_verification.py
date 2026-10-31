"""Read-only full verification on the Download Worker, with bounded job retries."""

from uuid import UUID

from holm_common.config import StorageConfig
from holm_domain.launch_verification import LaunchVerificationResult
from holm_persistence import AsyncSessionFactory, JobHandlerContext
from holm_persistence.job_executor import JobFailure, LeaseLostError
from holm_persistence.launch_verification_repository import LaunchVerificationRepository

from holm_download_worker.artifact_publishing import ArtifactRevalidator


class ArtifactVerificationHandler:
    def __init__(self, *, session_factory: AsyncSessionFactory, storage: StorageConfig) -> None:
        self._session_factory = session_factory
        self._revalidator = ArtifactRevalidator(session_factory=session_factory)
        self._storage = storage

    async def __call__(self, context: JobHandlerContext) -> None:
        context.ensure_lease()
        try:
            await self._revalidator.revalidate(
                context.job.target_object_id,
                full=True,
                storage=self._storage,
                context=context,
            )
        except LeaseLostError:
            raise
        except ValueError:
            raise JobFailure("artifact_verify_unsafe_path") from None
        except Exception:
            raise JobFailure("artifact_verify_failed") from None

    async def verify_light(self, context: JobHandlerContext) -> None:
        """Periodic metadata checks never replace explicit full verification."""
        context.ensure_lease()
        try:
            if context.job.payload:
                raise ValueError("unexpected light verification payload")
            await self._revalidator.revalidate(
                context.job.target_object_id,
                full=False,
                storage=self._storage,
                context=context,
                light_check=True,
            )
        except LeaseLostError:
            raise
        except ValueError:
            raise JobFailure("artifact_verify_unsafe_path") from None
        except Exception:
            raise JobFailure("artifact_verify_failed") from None

    async def verify_launch(self, context: JobHandlerContext) -> None:
        """Publish an exact-attempt result only while still holding the Job lease."""
        context.ensure_lease()
        try:
            if set(context.job.payload) != {"request_id", "instance_id"}:
                raise ValueError("invalid launch verification job")
            request_id = UUID(str(context.job.payload["request_id"]))
            instance_id = UUID(str(context.job.payload["instance_id"]))
            async with self._session_factory() as session:
                current = await LaunchVerificationRepository(session).current(instance_id)
            if current is None or current.request_id != request_id:
                return
            if (
                current.artifact_id != context.job.target_object_id
                or current.config_version != context.job.target_object_version
            ):
                raise ValueError("invalid launch verification target")
            if current.result is not LaunchVerificationResult.PENDING:
                return
            await self._revalidator.revalidate(
                current.artifact_id,
                full=True,
                storage=self._storage,
                context=context,
                launch_request_id=request_id,
            )
        except LeaseLostError:
            raise
        except ValueError:
            raise JobFailure("artifact_verify_unsafe_path") from None
        except Exception:
            raise JobFailure("artifact_verify_failed") from None
