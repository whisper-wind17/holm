#!/usr/bin/env python3
"""Small, sanitized container health probes used by the production Compose."""

from __future__ import annotations

import argparse
import socket
import sys
import urllib.request
from pathlib import Path


def _http(url: str) -> None:
    with urllib.request.urlopen(url, timeout=2) as response:  # noqa: S310
        if response.status != 200:
            raise RuntimeError("health endpoint is not ready")


def _tcp(host: str, port: int) -> None:
    with socket.create_connection((host, port), timeout=2):
        return


def _heartbeat(
    database_url_file: Path,
    component: str,
    instance_id: str,
    maximum_age_seconds: float,
    required_socket: Path | None,
) -> None:
    if required_socket is not None and not required_socket.is_socket():
        raise RuntimeError("required Agent Socket is not ready")

    import psycopg

    database_url = database_url_file.read_text(encoding="utf-8").strip()
    if not database_url:
        raise RuntimeError("database credential file is empty")
    scheme, separator, address = database_url.partition("://")
    if not separator or not (
        scheme == "postgresql"
        or (scheme.startswith("postgresql+") and scheme.removeprefix("postgresql+"))
    ):
        raise RuntimeError("unsupported database credential scheme")
    database_url = f"postgresql://{address}"
    query = """
        SELECT status, EXTRACT(EPOCH FROM clock_timestamp() - last_heartbeat_at)
          FROM core.component_heartbeats
         WHERE component = %s AND instance_id = %s
    """
    with psycopg.connect(database_url, connect_timeout=2) as connection:
        with connection.cursor() as cursor:
            cursor.execute(query, (component, instance_id))
            row = cursor.fetchone()
    if row is None:
        raise RuntimeError("component heartbeat is missing")
    status, age_seconds = row
    if status != "Healthy" or float(age_seconds) > maximum_age_seconds:
        raise RuntimeError("component heartbeat is not healthy and fresh")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="probe", required=True)

    http = subparsers.add_parser("http")
    http.add_argument("url")

    tcp = subparsers.add_parser("tcp")
    tcp.add_argument("host")
    tcp.add_argument("port", type=int)

    heartbeat = subparsers.add_parser("heartbeat")
    heartbeat.add_argument("--database-url-file", required=True, type=Path)
    heartbeat.add_argument("--component", required=True)
    heartbeat.add_argument("--instance-id", required=True)
    heartbeat.add_argument("--maximum-age-seconds", type=float, default=30.0)
    heartbeat.add_argument("--require-socket", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.probe == "http":
            _http(args.url)
        elif args.probe == "tcp":
            _tcp(args.host, args.port)
        else:
            _heartbeat(
                args.database_url_file,
                args.component,
                args.instance_id,
                args.maximum_age_seconds,
                args.require_socket,
            )
    except Exception:
        print("container health check failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
