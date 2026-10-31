"""Host Agent process entry point."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
from pathlib import Path

from holm_host_agent.runtime_process import (
    DEFAULT_RUNTIME_COMPAT_PATH,
    DEFAULT_RUNTIME_STATE_ROOT,
)
from holm_host_agent.server import (
    AGENT_SOCKET_PATH,
    DEFAULT_RELEASE_MANIFEST_PATH,
    serve_until_cancelled,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--socket",
        type=Path,
        default=AGENT_SOCKET_PATH,
        help="operator-selected UDS path (defaults to the production path)",
    )
    parser.add_argument(
        "--model-root",
        type=Path,
        action="append",
        default=[],
        dest="model_roots",
        metavar="DIR",
        help="allowed managed Model Artifact root; repeat for several",
    )
    parser.add_argument(
        "--runtime-compatibility",
        type=Path,
        default=DEFAULT_RUNTIME_COMPAT_PATH,
        help="validated Runtime compatibility catalog",
    )
    parser.add_argument(
        "--runtime-state-root",
        type=Path,
        default=DEFAULT_RUNTIME_STATE_ROOT,
        help="Agent-owned Runtime pid, endpoint and log directory",
    )
    parser.add_argument(
        "--release-manifest",
        type=Path,
        default=DEFAULT_RELEASE_MANIFEST_PATH,
        help="installed immutable Release Manifest path",
    )
    parser.add_argument(
        "--external-model-root",
        type=Path,
        action="append",
        default=[],
        dest="external_model_roots",
        metavar="DIR",
        help=(
            "allowed External Root for ValidatePath; repeat for several. "
            "With none configured every path is answered as not readable."
        ),
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(
            serve_until_cancelled(
                args.socket,
                args.release_manifest,
                tuple(args.external_model_roots),
                tuple(args.model_roots),
                args.runtime_compatibility,
                args.runtime_state_root,
            )
        )


if __name__ == "__main__":
    main()
