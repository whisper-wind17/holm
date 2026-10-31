"""Download Worker process entry point."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from holm_common import SecretFileError, startup_failure_code
from holm_common.config import StorageConfig
from holm_persistence import create_database_engine, create_session_factory
from holm_security import KeyringError

from holm_download_worker.agent_client import GrpcPathValidator
from holm_download_worker.artifact_deletion import ArtifactDeletionHandler
from holm_download_worker.artifact_publishing import DownloadArtifactPublisher
from holm_download_worker.artifact_verification import ArtifactVerificationHandler
from holm_download_worker.configuration import (
    DEFAULT_DOWNLOAD_MAX_WORKERS,
    DownloadWorkerConfiguration,
    DownloadWorkerConfigurationError,
    load_database_url,
)
from holm_download_worker.download_subprocess import (
    ProductionDownloadProcess,
    SubprocessModelDownloader,
)
from holm_download_worker.resolution_subprocess import (
    ProductionResolutionProcess,
    SubprocessModelResolver,
)
from holm_download_worker.staging_lifecycle import DownloadStagingLifecycle
from holm_download_worker.worker import (
    DownloadResolutionJobHandler,
    DownloadWorker,
    run_until_signalled,
)

_DEFAULT_STORAGE = StorageConfig()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url-file", required=True, type=Path)
    parser.add_argument("--master-keyring-file", required=True, type=Path)
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--build-version", required=True)
    parser.add_argument("--external-model-root", action="append", type=Path, default=[])
    parser.add_argument("--scratch-root", type=Path)
    parser.add_argument("--agent-socket", type=Path)
    parser.add_argument("--hugging-face-endpoint", default="https://huggingface.co")
    parser.add_argument("--modelscope-endpoint", default="https://www.modelscope.cn")
    parser.add_argument("--lease-seconds", type=float, default=30.0)
    parser.add_argument("--heartbeat-seconds", type=float, default=10.0)
    parser.add_argument("--poll-seconds", type=float, default=0.25)
    parser.add_argument("--resolution-timeout-seconds", type=float, default=15.0)
    parser.add_argument("--download-max-workers", type=int, default=DEFAULT_DOWNLOAD_MAX_WORKERS)
    parser.add_argument("--model-root", type=Path, default=_DEFAULT_STORAGE.model_root)
    parser.add_argument("--tokenizer-root", type=Path, default=_DEFAULT_STORAGE.tokenizer_root)
    parser.add_argument(
        "--disk-warning-percent", type=int, default=_DEFAULT_STORAGE.disk_warning_percent
    )
    parser.add_argument(
        "--disk-critical-percent", type=int, default=_DEFAULT_STORAGE.disk_critical_percent
    )
    parser.add_argument(
        "--disk-critical-free-gib", type=int, default=_DEFAULT_STORAGE.disk_critical_free_gib
    )
    return parser.parse_args(argv)


def _configuration(args: argparse.Namespace) -> DownloadWorkerConfiguration:
    return DownloadWorkerConfiguration(
        database_url_file=args.database_url_file,
        master_keyring_file=args.master_keyring_file,
        instance_id=args.instance_id,
        build_version=args.build_version,
        external_model_roots=tuple(args.external_model_root),
        scratch_root=args.scratch_root,
        agent_socket=args.agent_socket,
        hugging_face_endpoint=args.hugging_face_endpoint,
        modelscope_endpoint=args.modelscope_endpoint,
        lease_seconds=args.lease_seconds,
        heartbeat_seconds=args.heartbeat_seconds,
        poll_seconds=args.poll_seconds,
        resolution_timeout_seconds=args.resolution_timeout_seconds,
        download_max_workers=args.download_max_workers,
        storage=StorageConfig(
            model_root=args.model_root,
            tokenizer_root=args.tokenizer_root,
            external_model_roots=tuple(args.external_model_root)
            or _DEFAULT_STORAGE.external_model_roots,
            disk_warning_percent=args.disk_warning_percent,
            disk_critical_percent=args.disk_critical_percent,
            disk_critical_free_gib=args.disk_critical_free_gib,
        ),
    )


async def _run(configuration: DownloadWorkerConfiguration) -> None:
    engine = create_database_engine(load_database_url(configuration.database_url_file))
    try:
        factory = create_session_factory(engine)
        resolver = SubprocessModelResolver(
            ProductionResolutionProcess(
                database_url_file=configuration.database_url_file,
                master_keyring_file=configuration.master_keyring_file,
                external_model_roots=configuration.external_model_roots,
                scratch_root=configuration.scratch_root,
                hugging_face_endpoint=configuration.hugging_face_endpoint,
                modelscope_endpoint=configuration.modelscope_endpoint,
            ),
            timeout_seconds=configuration.resolution_timeout_seconds,
        )
        handler = DownloadResolutionJobHandler(session_factory=factory, resolver=resolver)
        handler.attach_staging_lifecycle(
            DownloadStagingLifecycle(
                session_factory=factory,
                storage=configuration.storage,
            )
        )
        handler.attach_download_executor(
            SubprocessModelDownloader(
                ProductionDownloadProcess(
                    database_url_file=configuration.database_url_file,
                    master_keyring_file=configuration.master_keyring_file,
                    max_workers=configuration.download_max_workers,
                )
            ),
            hugging_face_endpoint=configuration.hugging_face_endpoint,
            modelscope_endpoint=configuration.modelscope_endpoint,
        )
        handler.attach_artifact_publisher(
            DownloadArtifactPublisher(
                session_factory=factory,
                storage=configuration.storage,
                path_validator=GrpcPathValidator(configuration.agent_socket)
                if configuration.agent_socket is not None
                else None,
            )
        )
        worker = DownloadWorker(
            session_factory=factory,
            handler=handler,
            artifact_verification=ArtifactVerificationHandler(
                session_factory=factory, storage=configuration.storage
            ),
            artifact_deletion=ArtifactDeletionHandler(
                session_factory=factory, storage=configuration.storage
            ),
            owner=configuration.instance_id,
            build_version=configuration.build_version,
            lease_seconds=configuration.lease_seconds,
            heartbeat_seconds=configuration.heartbeat_seconds,
            poll_seconds=configuration.poll_seconds,
        )
        await run_until_signalled(worker)
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> None:
    try:
        asyncio.run(_run(_configuration(_parse_args(argv))))
    except KeyboardInterrupt:
        return
    except Exception as error:
        code = startup_failure_code(
            error, (DownloadWorkerConfigurationError, SecretFileError, KeyringError)
        )
        print(f"download worker startup failed: {code}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
