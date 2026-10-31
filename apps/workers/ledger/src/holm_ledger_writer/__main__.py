"""Request Ledger Writer process entry point."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Final

from holm_common import SecretFileError, startup_failure_code
from holm_domain import (
    LEDGER_DELAYED_AFTER_SECONDS,
    LEDGER_UNAVAILABLE_AFTER_SECONDS,
)
from holm_ledger import ConnectionRole, open_ledger
from holm_persistence import create_database_engine, create_session_factory

from holm_ledger_writer.configuration import LedgerWriterConfigurationError, load_database_url
from holm_ledger_writer.writer import (
    DEFAULT_ACK_RETENTION_SECONDS,
    DEFAULT_BACKLOG_HEALTH_SECONDS,
    DEFAULT_MAINTENANCE_SECONDS,
    DEFAULT_UNKNOWN_AFTER_SECONDS,
    DEFAULT_UNKNOWN_SCAN_SECONDS,
    DEFAULT_USAGE_AGGREGATE_SECONDS,
    LedgerWriter,
    run_until_signalled,
)

DEFAULT_RELEASE_MANIFEST: Final[Path] = Path("/opt/holm/release-manifest.json")


def _load_request_records_retention_days(path: Path) -> int:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        retention = document["retention"]
        value = retention["request_records_days"]
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError):
        raise ValueError("release manifest retention is unavailable") from None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("release manifest Request Record retention is invalid")
    return value


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url-file", required=True, type=Path)
    parser.add_argument("--request-ledger-path", required=True, type=Path)
    parser.add_argument("--release-manifest", type=Path, default=DEFAULT_RELEASE_MANIFEST)
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument("--heartbeat-seconds", type=float, default=10.0)
    parser.add_argument("--instance-id", default="ledger-writer")
    parser.add_argument("--build-version", default="0.0.0")
    parser.add_argument(
        "--unknown-after-seconds", type=float, default=DEFAULT_UNKNOWN_AFTER_SECONDS
    )
    parser.add_argument("--unknown-scan-seconds", type=float, default=DEFAULT_UNKNOWN_SCAN_SECONDS)
    parser.add_argument(
        "--usage-aggregate-seconds",
        type=float,
        default=DEFAULT_USAGE_AGGREGATE_SECONDS,
    )
    parser.add_argument(
        "--ledger-delayed-after-seconds",
        type=float,
        default=LEDGER_DELAYED_AFTER_SECONDS,
    )
    parser.add_argument(
        "--ledger-unavailable-after-seconds",
        type=float,
        default=LEDGER_UNAVAILABLE_AFTER_SECONDS,
    )
    parser.add_argument(
        "--backlog-health-seconds",
        type=float,
        default=DEFAULT_BACKLOG_HEALTH_SECONDS,
    )
    parser.add_argument(
        "--ack-retention-seconds",
        type=float,
        default=DEFAULT_ACK_RETENTION_SECONDS,
    )
    parser.add_argument(
        "--maintenance-seconds",
        type=float,
        default=DEFAULT_MAINTENANCE_SECONDS,
    )
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> None:
    request_records_retention_days = _load_request_records_retention_days(args.release_manifest)
    engine = create_database_engine(
        load_database_url(args.database_url_file),
        connect_timeout_seconds=5,
    )
    ledger = await asyncio.to_thread(
        open_ledger,
        args.request_ledger_path,
        ConnectionRole.LEDGER_WRITER,
    )
    try:
        await run_until_signalled(
            LedgerWriter(
                ledger=ledger,
                session_factory=create_session_factory(engine),
                engine=engine,
                poll_seconds=args.poll_seconds,
                heartbeat_seconds=args.heartbeat_seconds,
                instance_id=args.instance_id,
                build_version=args.build_version,
                unknown_after_seconds=args.unknown_after_seconds,
                unknown_scan_seconds=args.unknown_scan_seconds,
                usage_aggregate_seconds=args.usage_aggregate_seconds,
                delayed_after_seconds=args.ledger_delayed_after_seconds,
                unavailable_after_seconds=args.ledger_unavailable_after_seconds,
                backlog_health_seconds=args.backlog_health_seconds,
                ack_retention_seconds=args.ack_retention_seconds,
                maintenance_seconds=args.maintenance_seconds,
                request_records_retention_days=request_records_retention_days,
            )
        )
    finally:
        await asyncio.to_thread(ledger.close)
        await engine.dispose()


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    try:
        asyncio.run(_run(_parse_args(argv)))
    except KeyboardInterrupt:
        return
    except Exception as error:
        code = startup_failure_code(error, (LedgerWriterConfigurationError, SecretFileError))
        print(f"ledger writer startup failed: {code}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
