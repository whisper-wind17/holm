"""Model Instance controller process entry point."""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
from pathlib import Path
from typing import Final

from holm_common import SecretFileError, startup_failure_code
from holm_contracts import ReleaseManifest
from holm_persistence import create_database_engine, create_session_factory
from holm_runtimes import load_runtime_compatibility

from holm_instance_controller.agent_client import GrpcRuntimeAgentClient
from holm_instance_controller.configuration import (
    InstanceControllerConfigurationError,
    load_database_url,
)
from holm_instance_controller.controller import InstanceController
from holm_instance_controller.health import HttpRuntimeHealthProbe
from holm_instance_controller.heartbeat import ControllerHeartbeat
from holm_instance_controller.store import PostgresControllerStore

DEFAULT_AGENT_SOCKET: Final[Path] = Path("/run/holm/agent.sock")
DEFAULT_RUNTIME_COMPATIBILITY: Final[Path] = Path("/opt/holm/runtime-compat.yaml")
DEFAULT_RELEASE_MANIFEST: Final[Path] = Path("/opt/holm/release-manifest.json")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url-file", required=True, type=Path)
    parser.add_argument("--agent-socket", type=Path, default=DEFAULT_AGENT_SOCKET)
    parser.add_argument(
        "--runtime-compatibility",
        type=Path,
        default=DEFAULT_RUNTIME_COMPATIBILITY,
    )
    parser.add_argument("--release-manifest", type=Path, default=DEFAULT_RELEASE_MANIFEST)
    parser.add_argument("--start-timeout-seconds", type=float, default=30 * 60)
    parser.add_argument("--start-poll-seconds", type=float, default=1.0)
    parser.add_argument("--health-seconds", type=float, default=5.0)
    parser.add_argument("--health-timeout-seconds", type=float, default=2.0)
    parser.add_argument("--dispatch-seconds", type=float, default=0.25)
    parser.add_argument("--stop-grace-seconds", type=int, default=60)
    parser.add_argument("--reconciliation-seconds", type=float, default=30.0)
    parser.add_argument("--dependency-timeout-seconds", type=float, default=5.0)
    parser.add_argument("--initial-reconciliation-window-seconds", type=float, default=60.0)
    parser.add_argument("--restart-window-seconds", type=int, default=10 * 60)
    return parser.parse_args(argv)


def _become_instance_controller(manifest: ReleaseManifest) -> None:
    matches = [
        identity
        for identity in manifest.service_identities
        if identity.service == "instance-controller"
    ]
    if len(matches) != 1:
        raise RuntimeError("release manifest has no unique instance-controller identity")
    identity = matches[0]
    if os.geteuid() == 0:
        os.setgroups([])
        os.setgid(identity.gid)
        os.setuid(identity.uid)
    if os.geteuid() != identity.uid or os.getegid() != identity.gid:
        raise RuntimeError("process does not match the instance-controller identity")


async def _run(args: argparse.Namespace) -> None:
    database_url = load_database_url(args.database_url_file)
    manifest = ReleaseManifest.model_validate_json(
        args.release_manifest.read_text(encoding="utf-8")
    )
    catalog = load_runtime_compatibility(args.runtime_compatibility)
    _become_instance_controller(manifest)

    engine = create_database_engine(database_url, connect_timeout_seconds=5)
    try:
        factory = create_session_factory(engine)
        controller = InstanceController(
            store=PostgresControllerStore(factory),
            heartbeat=ControllerHeartbeat(
                factory,
                instance_id="instance-controller",
                build_version=manifest.build_version,
            ),
            agent=GrpcRuntimeAgentClient(
                args.agent_socket,
                timeout_seconds=max(75.0, float(args.stop_grace_seconds) + 15.0),
            ),
            health=HttpRuntimeHealthProbe(timeout_seconds=args.health_timeout_seconds),
            runtime_catalog=catalog,
            start_timeout_seconds=args.start_timeout_seconds,
            start_poll_seconds=args.start_poll_seconds,
            health_seconds=args.health_seconds,
            dispatch_seconds=args.dispatch_seconds,
            stop_grace_seconds=args.stop_grace_seconds,
            reconciliation_seconds=args.reconciliation_seconds,
            dependency_timeout_seconds=args.dependency_timeout_seconds,
            initial_reconciliation_window_seconds=args.initial_reconciliation_window_seconds,
            restart_window_seconds=args.restart_window_seconds,
        )
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for selected_signal in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(selected_signal, stop_event.set)
        await controller.run(stop_event)
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> None:
    try:
        asyncio.run(_run(_parse_args(argv)))
    except KeyboardInterrupt:
        return
    except Exception as error:
        code = startup_failure_code(error, (InstanceControllerConfigurationError, SecretFileError))
        print(f"instance controller startup failed: {code}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
