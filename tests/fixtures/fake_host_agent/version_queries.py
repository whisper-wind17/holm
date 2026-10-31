"""Raw FX-HA command results for M1-07C host-version translation tests."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

FIXTURE_REVIEW_STATUS = "待真机复核"


@dataclass(frozen=True, slots=True)
class RawVersionQueryFixture:
    """Untranslated stdout/stderr and exit status from a version command."""

    fixture_id: str
    source: str
    command: tuple[str, ...]
    stdout: str
    stderr: str = ""
    returncode: int = 0
    review_status: str = FIXTURE_REVIEW_STATUS

    def completed_process(self) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=self.command,
            returncode=self.returncode,
            stdout=self.stdout,
            stderr=self.stderr,
        )


FX_HA_DOCKER_VERSION = RawVersionQueryFixture(
    fixture_id="FX-HA-M1-07C-DOCKER-VERSION",
    source="M1-07C Docker Engine server-version output shape",
    command=(
        "docker",
        "version",
        "--format",
        "Docker version {{.Server.Version}}, build {{.Server.GitCommit}}",
    ),
    stdout="Docker version 27.3.1, build ce12230\n",
)

FX_HA_TOOLKIT_VERSION = RawVersionQueryFixture(
    fixture_id="FX-HA-M1-07C-TOOLKIT-VERSION",
    source="M1-07C NVIDIA Container Runtime output shape",
    command=("nvidia-container-runtime", "--version"),
    stdout="NVIDIA Container Runtime version 1.16.2\ncommit: fixture-revision\n",
)

FX_HA_COMPOSE_VERSION = RawVersionQueryFixture(
    fixture_id="FX-HA-SYS-04-COMPOSE-VERSION",
    source="认证宿主实测 2026-10-17：`docker compose version --short`",
    command=("docker", "compose", "version", "--short"),
    stdout="5.1.0\n",
)

FX_HA_COMPOSE_VERSION_PREFIXED = RawVersionQueryFixture(
    fixture_id="FX-HA-SYS-04-COMPOSE-VERSION-PREFIXED",
    source="SYS-04: optional v prefix output shape",
    command=("docker", "compose", "version", "--short"),
    stdout="v2.32.4\n",
)
