"""Production entry point: backup retention, Artifact coordination and heartbeats."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from holm_common import SecretFileError, startup_failure_code
from holm_persistence import create_database_engine, create_session_factory

from holm_maintenance_worker.configuration import (
    MaintenanceConfigurationError,
    load_database_url,
)
from holm_maintenance_worker.worker import (
    DEFAULT_ARTIFACT_CHECK_SECONDS,
    DEFAULT_RETENTION_SECONDS,
    MaintenanceWorker,
    run_until_signalled,
)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url-file", type=Path, required=True)
    parser.add_argument("--backup-root", type=Path, required=True)
    # Files are checked by the existing Download Worker with its configured roots.
    # Maintenance coordinates only through the restricted database function.
    parser.add_argument("--model-root", type=Path, required=True)
    parser.add_argument("--external-model-root", type=Path, action="append", required=True)
    parser.add_argument("--instance-id", default="maintenance-worker")
    parser.add_argument("--build-version", default="0.0.0")
    parser.add_argument("--retention-seconds", type=float, default=DEFAULT_RETENTION_SECONDS)
    parser.add_argument("--heartbeat-seconds", type=float, default=10.0)
    parser.add_argument(
        "--artifact-check-seconds", type=float, default=DEFAULT_ARTIFACT_CHECK_SECONDS
    )
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> None:
    engine = create_database_engine(
        load_database_url(args.database_url_file), connect_timeout_seconds=3
    )
    try:
        await run_until_signalled(
            MaintenanceWorker(
                backup_root=args.backup_root,
                session_factory=create_session_factory(engine),
                instance_id=args.instance_id,
                build_version=args.build_version,
                retention_seconds=args.retention_seconds,
                heartbeat_seconds=args.heartbeat_seconds,
                artifact_check_seconds=args.artifact_check_seconds,
            )
        )
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    try:
        asyncio.run(_run(_parse_args(argv)))
    except KeyboardInterrupt:
        return
    except Exception as error:
        code = startup_failure_code(error, (MaintenanceConfigurationError, SecretFileError))
        print(f"maintenance worker startup failed: {code}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
