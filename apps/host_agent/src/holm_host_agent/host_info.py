"""Read host facts through bounded, read-only host queries."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Callable
from contextlib import suppress
from typing import Final, Protocol

from holm_contracts.agent.v1 import agent_pb2

from holm_host_agent.gpu_discovery import NvmlCallError, NvmlLibrary, read_driver_version

HOST_VERSION_QUERY_TIMEOUT_SECONDS: Final = 2.0
DOCKER_VERSION_COMMAND: Final = (
    "docker",
    "version",
    "--format",
    "Docker version {{.Server.Version}}, build {{.Server.GitCommit}}",
)
COMPOSE_VERSION_COMMAND: Final = ("docker", "compose", "version", "--short")
TOOLKIT_VERSION_COMMAND: Final = ("nvidia-container-runtime", "--version")

_VERSION_TOKEN = r"([0-9]+(?:\.[0-9]+){1,3}(?:[-+][0-9A-Za-z.-]+)?)"
_COMPOSE_VERSION_PATTERN = re.compile(rf"^v?{_VERSION_TOKEN}$")
_DOCKER_VERSION_PATTERN = re.compile(rf"\bDocker\s+version\s+{_VERSION_TOKEN}\b", re.IGNORECASE)
_TOOLKIT_VERSION_PATTERNS = (
    re.compile(rf"\bNVIDIA\s+Container\s+Runtime\s+version\s+{_VERSION_TOKEN}\b", re.IGNORECASE),
    re.compile(
        rf"\bNVIDIA\s+Container\s+Toolkit(?:\s+CLI)?\s+version\s+{_VERSION_TOKEN}\b",
        re.IGNORECASE,
    ),
)


class VersionCommandRunner(Protocol):
    """Execute one fixed version query and return its untranslated process result."""

    def __call__(
        self,
        command: tuple[str, ...],
        *,
        timeout_seconds: float,
    ) -> subprocess.CompletedProcess[str]: ...


def _run_version_command(
    command: tuple[str, ...],
    *,
    timeout_seconds: float,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=False,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_seconds,
    )


def _unknown_string(reason: int) -> agent_pb2.ObservedString:
    return agent_pb2.ObservedString(
        state=agent_pb2.KNOWLEDGE_STATE_UNKNOWN,
        unknown_reason=reason,
    )


def _command_version(
    command: tuple[str, ...],
    patterns: tuple[re.Pattern[str], ...],
    command_runner: VersionCommandRunner,
) -> agent_pb2.ObservedString:
    try:
        result = command_runner(
            command,
            timeout_seconds=HOST_VERSION_QUERY_TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        return _unknown_string(agent_pb2.OBSERVATION_UNKNOWN_REASON_SOURCE_UNAVAILABLE)
    except Exception:
        # The process boundary may surface permission, timeout, or platform-specific
        # failures. None may make the independent host observations unavailable.
        return _unknown_string(agent_pb2.OBSERVATION_UNKNOWN_REASON_QUERY_FAILED)

    if result.returncode != 0:
        return _unknown_string(agent_pb2.OBSERVATION_UNKNOWN_REASON_QUERY_FAILED)

    output = "\n".join((result.stdout, result.stderr)).strip()
    if not output:
        return _unknown_string(agent_pb2.OBSERVATION_UNKNOWN_REASON_EMPTY_VALUE)
    for pattern in patterns:
        match = pattern.search(output)
        if match is not None:
            return agent_pb2.ObservedString(
                state=agent_pb2.KNOWLEDGE_STATE_KNOWN,
                value=match.group(1),
            )
    return _unknown_string(agent_pb2.OBSERVATION_UNKNOWN_REASON_EMPTY_VALUE)


def collect_docker_version(
    command_runner: VersionCommandRunner = _run_version_command,
) -> agent_pb2.ObservedString:
    """Collect the Docker Engine version without accepting caller-controlled input."""
    return _command_version(
        DOCKER_VERSION_COMMAND,
        (_DOCKER_VERSION_PATTERN,),
        command_runner,
    )


def collect_toolkit_version(
    command_runner: VersionCommandRunner = _run_version_command,
) -> agent_pb2.ObservedString:
    """Collect the NVIDIA Container Toolkit runtime-wrapper version."""
    return _command_version(
        TOOLKIT_VERSION_COMMAND,
        _TOOLKIT_VERSION_PATTERNS,
        command_runner,
    )


def collect_compose_version(
    command_runner: VersionCommandRunner = _run_version_command,
) -> agent_pb2.ObservedString:
    """Collect the Compose plugin version, normalizing its optional leading v."""
    return _command_version(
        COMPOSE_VERSION_COMMAND,
        (_COMPOSE_VERSION_PATTERN,),
        command_runner,
    )


def _observed_uint64(read: Callable[[], int | None]) -> agent_pb2.ObservedUInt64:
    try:
        value = read()
    except (OSError, ValueError):
        value = None
    if value is None or value < 0:
        return agent_pb2.ObservedUInt64(state=agent_pb2.KNOWLEDGE_STATE_UNKNOWN)
    return agent_pb2.ObservedUInt64(
        state=agent_pb2.KNOWLEDGE_STATE_KNOWN,
        value=value,
    )


def _memory_bytes() -> int:
    page_size = os.sysconf("SC_PAGE_SIZE")
    page_count = os.sysconf("SC_PHYS_PAGES")
    return page_size * page_count


def _disk_total_bytes() -> int:
    return shutil.disk_usage("/").total


def _disk_available_bytes() -> int:
    return shutil.disk_usage("/").free


def _cuda_driver_version(nvml_library: NvmlLibrary) -> agent_pb2.ObservedString:
    try:
        nvml_library.initialize()
    except NvmlCallError:
        return agent_pb2.ObservedString(
            state=agent_pb2.KNOWLEDGE_STATE_UNKNOWN,
            unknown_reason=agent_pb2.OBSERVATION_UNKNOWN_REASON_SOURCE_UNAVAILABLE,
        )
    try:
        encoded = nvml_library.system_get_cuda_driver_version()
        if encoded <= 0:
            return agent_pb2.ObservedString(
                state=agent_pb2.KNOWLEDGE_STATE_UNKNOWN,
                unknown_reason=agent_pb2.OBSERVATION_UNKNOWN_REASON_EMPTY_VALUE,
            )
        major, remainder = divmod(encoded, 1000)
        minor = remainder // 10
        return agent_pb2.ObservedString(
            state=agent_pb2.KNOWLEDGE_STATE_KNOWN,
            value=f"{major}.{minor}",
        )
    except NvmlCallError:
        return agent_pb2.ObservedString(
            state=agent_pb2.KNOWLEDGE_STATE_UNKNOWN,
            unknown_reason=agent_pb2.OBSERVATION_UNKNOWN_REASON_QUERY_FAILED,
        )
    finally:
        with suppress(NvmlCallError):
            nvml_library.shutdown()


def get_host_info(
    nvml_library: NvmlLibrary,
    version_command_runner: VersionCommandRunner = _run_version_command,
) -> agent_pb2.GetHostInfoResponse:
    """Return CPU, memory, and root-filesystem capacity with explicit knowledge state."""
    driver = read_driver_version(nvml_library)
    if driver.value is None:
        driver_version = agent_pb2.ObservedString(
            state=agent_pb2.KNOWLEDGE_STATE_UNKNOWN,
            unknown_reason=driver.unknown_reason,
        )
    else:
        driver_version = agent_pb2.ObservedString(
            state=agent_pb2.KNOWLEDGE_STATE_KNOWN,
            value=driver.value,
        )
    return agent_pb2.GetHostInfoResponse(
        logical_cpu_count=_observed_uint64(os.cpu_count),
        total_memory_bytes=_observed_uint64(_memory_bytes),
        total_disk_bytes=_observed_uint64(_disk_total_bytes),
        available_disk_bytes=_observed_uint64(_disk_available_bytes),
        driver_version=driver_version,
        docker_version=collect_docker_version(version_command_runner),
        toolkit_version=collect_toolkit_version(version_command_runner),
        cuda_driver_version=_cuda_driver_version(nvml_library),
        compose_version=collect_compose_version(version_command_runner),
    )
