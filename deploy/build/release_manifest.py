"""Generate the shared unsigned source-build / host-verification Manifest in an image."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def generate(repo: Path, work: Path, commit: str | None = None) -> None:
    sys.path.insert(0, str(repo / "scripts"))
    from build_artifacts import _release_artifact_hashes, _release_manifest
    from holm_contracts.release import DEFAULT_HOST_REQUIREMENTS, PRODUCTION_SERVICE_IDS

    images = json.loads((work / "images.json").read_text())
    source = {
        "schema_version": 1,
        "host_requirements": DEFAULT_HOST_REQUIREMENTS,
        "image_digests": [{"name": name, "digest": digest} for name, digest in images.items()],
        "service_identities": [
            {"service": name, "uid": pair[0], "gid": pair[1]}
            for name, pair in PRODUCTION_SERVICE_IDS.items()
        ],
        "default_resource_limits": [
            {
                "service": "runtime",
                "cpu_millicores": 8000,
                "memory_mib": 16384,
                "pids": 4096,
                "log_rotation_mib": 20,
                "tmpfs_mib": 1024,
            }
        ],
        "gateway": {
            "max_inflight_requests": 512,
            "runtime_pool_max_connections_per_instance": 512,
            "boundary_semantics": "reject_when_current_is_greater_than_or_equal_to_limit",
        },
        "retention": {
            "request_records_days": 30,
            "download_tasks_days": 30,
            "telemetry_raw_seconds": 3600,
            "telemetry_aggregate_days": 30,
        },
        "default_timezone": "Asia/Shanghai",
    }
    previous = os.environ.get("HOLM_BUILD_COMMIT")
    try:
        if commit is not None:
            os.environ["HOLM_BUILD_COMMIT"] = commit
        manifest = _release_manifest(source, _release_artifact_hashes())
        (work / "release-manifest.json").write_text(manifest.model_dump_json(indent=2) + "\n")
    finally:
        if previous is None:
            os.environ.pop("HOLM_BUILD_COMMIT", None)
        else:
            os.environ["HOLM_BUILD_COMMIT"] = previous


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--commit")
    args = parser.parse_args()
    generate(args.repo, args.work, args.commit)
    print("Unsigned release manifest created from actual image and artifact hashes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
