"""Configure gRPC before its native runtime is initialized."""

from __future__ import annotations

import os


def configure_exec_only_children() -> None:
    """Disable gRPC at-fork handlers for children that immediately exec.

    Runtime launches require ``fork`` because CPython applies the fixed UID,
    GID, empty supplementary groups, and new session between fork and exec.
    The Host Agent never calls gRPC from that child, so gRPC's at-fork repair
    is both unnecessary and unsafe while the parent's aio server is active.
    This must run before anything imports :mod:`grpc`.
    """

    os.environ["GRPC_ENABLE_FORK_SUPPORT"] = "false"
