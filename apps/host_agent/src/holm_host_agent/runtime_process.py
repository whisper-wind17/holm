"""Typed, allowlisted process lifecycle for prototype Runtime servers."""

from __future__ import annotations

import asyncio
import errno
import json
import logging
import math
import os
import re
import signal
import socket
import subprocess
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Final
from uuid import UUID

from holm_contracts.external_paths import ExternalPathPolicy, judge_external_path
from holm_runtimes import (
    RuntimeCompatibility,
    RuntimeCompatibilityCatalog,
    RuntimeCompatibilityError,
    load_runtime_compatibility,
    validate_runtime_launch_policy,
)

from holm_host_agent.gpu_discovery import (
    DiscoveryState,
    NvmlCallError,
    NvmlErrorKind,
    NvmlLibrary,
    TopologyState,
    discover_gpus,
)
from holm_host_agent.path_validation import RuntimeIdentity
from holm_host_agent.runtime_logs import RuntimeLogPage, RuntimeLogRequestError

DEFAULT_RUNTIME_COMPAT_PATH: Final[Path] = Path("/opt/holm/runtime-compat.yaml")
DEFAULT_RUNTIME_STATE_ROOT: Final[Path] = Path("/run/holm/runtimes")
DEFAULT_STOP_GRACE_SECONDS: Final[int] = 60
MAX_STOP_GRACE_SECONDS: Final[int] = 300
MAX_RUNTIME_PORT_LAUNCH_ATTEMPTS: Final[int] = 3
MAX_RUNTIME_PORT_RETRY_SECONDS: Final[float] = 1.0
_SERVED_MODEL_PATTERN: Final = re.compile(r"^mi_[a-z0-9]{6,32}$")
_CONFIG_VERSION_PATTERN: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_LOOPBACK_HOST: Final[str] = "127.0.0.1"
_EXIT_CONFIRM_SECONDS: Final[float] = 5.0
MAX_OOM_LOG_TAIL_BYTES: Final[int] = 64 * 1024
_MAX_MODEL_CONFIG_BYTES: Final[int] = 1024 * 1024
_LOGGER: Final[logging.Logger] = logging.getLogger(__name__)


class RuntimeRequestError(RuntimeError):
    """A sanitized, stable rejection suitable for a gRPC status detail."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class RuntimeStateValue(StrEnum):
    RUNNING = "running"
    EXITED = "exited"
    NOT_FOUND = "not_found"


class ObservationKnowledgeState(StrEnum):
    KNOWN = "known"
    UNKNOWN = "unknown"


class GpuObservationUnknownReason(StrEnum):
    NOT_COLLECTED = "not_collected"
    SOURCE_UNAVAILABLE = "source_unavailable"
    QUERY_FAILED = "query_failed"


@dataclass(frozen=True, slots=True)
class ObservedGpuUuids:
    state: ObservationKnowledgeState
    values: tuple[str, ...] = ()
    unknown_reason: GpuObservationUnknownReason | None = None

    def __post_init__(self) -> None:
        if self.state is ObservationKnowledgeState.KNOWN:
            if self.unknown_reason is not None:
                raise ValueError("known GPU observation cannot have an unknown reason")
            if len(set(self.values)) != len(self.values):
                raise ValueError("observed GPU UUIDs must be unique")
            if any(
                not value.startswith("GPU-") or any(character.isspace() for character in value)
                for value in self.values
            ):
                raise ValueError("observed GPU UUIDs must be canonical tokens")
        elif self.values or self.unknown_reason is None:
            raise ValueError("unknown GPU observation requires only an unknown reason")


_GPU_NOT_COLLECTED: Final[ObservedGpuUuids] = ObservedGpuUuids(
    ObservationKnowledgeState.UNKNOWN,
    unknown_reason=GpuObservationUnknownReason.NOT_COLLECTED,
)


@dataclass(frozen=True, slots=True)
class RuntimeLaunchSpec:
    instance_id: str
    runtime_name: str
    model_path: str
    gpu_uuids: tuple[str, ...]
    tensor_parallel_size: int
    context_length: int
    gpu_memory_utilization: float
    served_model_name: str
    config_version: str


@dataclass(frozen=True, slots=True)
class RuntimeObservation:
    state: RuntimeStateValue
    endpoint: str = ""
    started_at: str = ""
    exit_code: int | None = None
    gpu_uuids: ObservedGpuUuids = _GPU_NOT_COLLECTED
    oom_killed: bool | None = None


@dataclass(frozen=True, slots=True)
class RuntimeSummary:
    instance_id: str
    state: RuntimeStateValue
    gpu_uuids: ObservedGpuUuids


@dataclass(frozen=True, slots=True)
class _PersistedRuntime:
    instance_id: str
    pid: int
    start_ticks: int
    config_version: str
    endpoint: str
    started_at: str
    state: str
    exit_code: int | None
    # Previously written states remain readable; without this value the
    # Runtime-specific OOM criterion is honestly reported as not collected.
    runtime_name: str | None = None


def _model_architectures(model_path: str) -> tuple[str, ...]:
    """Read optional model metadata without exposing paths or file contents in logs."""
    try:
        with (Path(model_path) / "config.json").open("rb") as source:
            if os.fstat(source.fileno()).st_size > _MAX_MODEL_CONFIG_BYTES:
                raise ValueError("config too large")
            payload = source.read(_MAX_MODEL_CONFIG_BYTES + 1)
        if len(payload) > _MAX_MODEL_CONFIG_BYTES:
            raise ValueError("config too large")
        value = json.loads(payload)
        architectures = value.get("architectures") if isinstance(value, dict) else None
        if not isinstance(architectures, list) or not all(
            isinstance(item, str) for item in architectures
        ):
            raise ValueError("invalid architectures")
        return tuple(architectures)
    except (OSError, ValueError, RecursionError):
        _LOGGER.warning("runtime_model_config_unavailable")
        return ()


def build_runtime_command(
    runtime: RuntimeCompatibility,
    spec: RuntimeLaunchSpec,
    port: int,
    *,
    host: str = _LOOPBACK_HOST,
) -> tuple[str, ...]:
    """Build argv solely from trusted catalog tokens and typed values."""

    arguments = runtime.arguments
    command = [str(runtime.executable), arguments.serve]
    if arguments.model_path is None:
        command.append(spec.model_path)
    else:
        command.extend((arguments.model_path, spec.model_path))
    command.extend(
        (
            arguments.host,
            host,
            arguments.port,
            str(port),
            arguments.tensor_parallel_size,
            str(spec.tensor_parallel_size),
            arguments.max_model_len,
            str(spec.context_length),
            arguments.gpu_memory_utilization,
            str(spec.gpu_memory_utilization),
            arguments.served_model_name,
            spec.served_model_name,
        )
    )
    if arguments.disable_request_logging is not None:
        command.append(arguments.disable_request_logging)
    if arguments.enable_metrics is not None:
        command.append(arguments.enable_metrics)
    architectures = _model_architectures(spec.model_path)
    for architecture in architectures:
        parser = runtime.reasoning_parsers.get(architecture)
        if parser is not None:
            command.extend((arguments.reasoning_parser, parser))
            break
    for architecture in architectures:
        parser = runtime.tool_call_parsers.get(architecture)
        if parser is not None:
            if arguments.enable_auto_tool_choice is not None:
                command.append(arguments.enable_auto_tool_choice)
            command.extend((arguments.tool_call_parser, parser))
            break
    if "--trust-remote-code" in command:
        raise RuntimeRequestError("runtime_command_forbidden")
    return tuple(command)


def build_runtime_environment(
    runtime: RuntimeCompatibility,
    spec: RuntimeLaunchSpec,
    runtime_home: Path,
) -> dict[str, str]:
    """Return the complete child environment; parent variables are never inherited."""

    return {
        "CUDA_VISIBLE_DEVICES": ",".join(spec.gpu_uuids),
        "HF_HUB_OFFLINE": "1",
        "HOME": str(runtime_home),
        "PATH": (
            f"{runtime.executable.parent}:"
            "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        ),
    }


class RuntimeManager:
    """Serialize process mutations and preserve inspectable state across Agent restarts."""

    def __init__(
        self,
        *,
        compatibility_path: Path,
        state_root: Path,
        model_path_policy: ExternalPathPolicy | None,
        runtime_identity: RuntimeIdentity | None,
        nvml_factory: Callable[[], NvmlLibrary],
    ) -> None:
        self._compatibility_path = compatibility_path
        self._state_root = state_root
        self._model_path_policy = model_path_policy
        self._runtime_identity = runtime_identity
        self._nvml_factory = nvml_factory
        self._catalog: RuntimeCompatibilityCatalog | None = None
        self._processes: dict[str, subprocess.Popen[bytes]] = {}
        self._watch_tasks: set[asyncio.Task[None]] = set()
        self._lock = asyncio.Lock()
        from holm_host_agent.runtime_container import (
            DEFAULT_RUNTIME_CACHE_ROOT,
            ContainerRuntimeManager,
        )

        self._container = ContainerRuntimeManager(
            cache_root=DEFAULT_RUNTIME_CACHE_ROOT,
            runtime_identity=runtime_identity,
        )

    async def ensure(self, spec: RuntimeLaunchSpec) -> RuntimeObservation:
        async with self._lock:
            canonical_id = _canonical_instance_id(spec.instance_id)
            if self._launcher_for(spec.runtime_name) == "container":
                persisted = await asyncio.to_thread(self._read_state, canonical_id)
                if persisted is not None and await asyncio.to_thread(
                    _persisted_is_running, persisted
                ):
                    raise RuntimeRequestError("runtime_config_conflict")
                runtime = await asyncio.to_thread(self._validate, spec)
                return await asyncio.to_thread(self._container.ensure, canonical_id, runtime, spec)
            existing = await asyncio.to_thread(self._read_state, canonical_id)
            if existing is not None and await asyncio.to_thread(_persisted_is_running, existing):
                if existing.config_version != spec.config_version:
                    raise RuntimeRequestError("runtime_config_conflict")
                return _observation(existing, RuntimeStateValue.RUNNING)

            if existing is not None:
                await self._terminate_persisted(
                    canonical_id,
                    existing,
                    grace_seconds=float(DEFAULT_STOP_GRACE_SECONDS),
                )

            runtime = await asyncio.to_thread(self._validate, spec)
            return await self._launch(canonical_id, runtime, spec)

    async def stop(self, instance_id: str, grace_seconds: int) -> RuntimeObservation:
        async with self._lock:
            canonical_id = _canonical_instance_id(instance_id)
            if grace_seconds < 0 or grace_seconds > MAX_STOP_GRACE_SECONDS:
                raise RuntimeRequestError("grace_seconds_invalid")
            grace = grace_seconds or DEFAULT_STOP_GRACE_SECONDS
            persisted = await asyncio.to_thread(self._read_state, canonical_id)
            if persisted is not None:
                exited = await self._terminate_persisted(
                    canonical_id,
                    persisted,
                    grace_seconds=float(grace),
                )
                return _observation(exited, RuntimeStateValue.EXITED)
            if self._container_enabled():
                return await asyncio.to_thread(self._container.stop, canonical_id, grace_seconds)
            return RuntimeObservation(RuntimeStateValue.NOT_FOUND)

    async def remove(self, instance_id: str) -> None:
        async with self._lock:
            canonical_id = _canonical_instance_id(instance_id)
            if await asyncio.to_thread(self._read_state, canonical_id) is not None:
                # Process state and logs are not Docker containers; deleting them
                # needs a separate retention contract.
                raise RuntimeRequestError("runtime_remove_unsupported")
            if self._container_enabled():
                await asyncio.to_thread(self._container.remove, canonical_id)

    async def inspect(self, instance_id: str) -> RuntimeObservation:
        async with self._lock:
            canonical_id = _canonical_instance_id(instance_id)
            persisted = await asyncio.to_thread(self._read_state, canonical_id)
            if persisted is None:
                if self._container_enabled():
                    return await asyncio.to_thread(self._container.inspect, canonical_id)
                return RuntimeObservation(RuntimeStateValue.NOT_FOUND)
            if await asyncio.to_thread(_persisted_is_running, persisted):
                gpu_uuids = await asyncio.to_thread(
                    _observe_runtime_gpu_uuids,
                    self._new_nvml(),
                    persisted.pid,
                )
                return _observation(
                    persisted,
                    RuntimeStateValue.RUNNING,
                    gpu_uuids=gpu_uuids,
                )
            exited = self._mark_exited(persisted)
            oom_killed = await asyncio.to_thread(self._observe_oom_killed, exited)
            return _observation(
                exited,
                RuntimeStateValue.EXITED,
                oom_killed=oom_killed,
            )

    async def list(self) -> tuple[RuntimeSummary, ...]:
        """List every persisted platform Runtime under the Agent-owned state root."""

        async with self._lock:
            instance_ids = await asyncio.to_thread(self._persisted_instance_ids)
            summaries: list[RuntimeSummary] = []
            for instance_id in instance_ids:
                persisted = await asyncio.to_thread(self._read_state, instance_id)
                if persisted is None:
                    continue
                if await asyncio.to_thread(_persisted_is_running, persisted):
                    gpu_uuids = await asyncio.to_thread(
                        _observe_runtime_gpu_uuids,
                        self._new_nvml(),
                        persisted.pid,
                    )
                    state = RuntimeStateValue.RUNNING
                else:
                    persisted = self._mark_exited(persisted)
                    gpu_uuids = _GPU_NOT_COLLECTED
                    state = RuntimeStateValue.EXITED
                summaries.append(
                    RuntimeSummary(
                        instance_id=instance_id,
                        state=state,
                        gpu_uuids=gpu_uuids,
                    )
                )
            if self._container_enabled():
                summaries.extend(await asyncio.to_thread(self._container.list))
            return tuple(summaries)

    def _container_enabled(self) -> bool:
        if self._catalog is None and not self._compatibility_path.is_file():
            return False
        try:
            catalog = self._catalog or load_runtime_compatibility(self._compatibility_path)
        except RuntimeCompatibilityError as error:
            raise RuntimeRequestError("runtime_configuration_unavailable") from error
        self._catalog = catalog
        self._container.set_runtimes(catalog.runtimes)
        return any(runtime.launcher == "container" for runtime in catalog.runtimes.values())

    def _launcher_for(self, runtime_name: str) -> str | None:
        if self._catalog is None and not self._compatibility_path.is_file():
            return None
        try:
            catalog = self._catalog or load_runtime_compatibility(self._compatibility_path)
        except RuntimeCompatibilityError as error:
            raise RuntimeRequestError("runtime_configuration_unavailable") from error
        self._catalog = catalog
        self._container.set_runtimes(catalog.runtimes)
        runtime = catalog.runtime(runtime_name)
        return None if runtime is None else runtime.launcher

    async def container_log_page(
        self,
        instance_id: str,
        *,
        cursor_offset: int,
        page_size: int,
        max_log_line_characters: int,
    ) -> RuntimeLogPage | None:
        canonical_id = _canonical_instance_id(instance_id)
        # Legacy file-log readers may run without any Runtime catalog at all.
        if self._catalog is None and not self._compatibility_path.is_file():
            return None
        if await asyncio.to_thread(self._read_state, canonical_id) is not None:
            return None
        if not self._container_enabled():
            return None
        try:
            observation = await asyncio.to_thread(self._container.inspect, canonical_id)
            if observation.state is RuntimeStateValue.NOT_FOUND:
                return None
            return await asyncio.to_thread(
                self._container.log_page,
                canonical_id,
                cursor_offset=cursor_offset,
                page_size=page_size,
                max_log_line_characters=max_log_line_characters,
            )
        except RuntimeRequestError as error:
            raise RuntimeLogRequestError("runtime_log_unavailable") from error

    def _persisted_instance_ids(self) -> tuple[str, ...]:
        try:
            entries = tuple(self._state_root.iterdir())
        except FileNotFoundError:
            return ()
        except OSError as error:
            raise RuntimeRequestError("runtime_state_unavailable") from error
        instance_ids: list[str] = []
        for entry in entries:
            if not entry.is_dir() or not (entry / "runtime.json").is_file():
                continue
            try:
                instance_ids.append(_canonical_instance_id(entry.name))
            except RuntimeRequestError:
                continue
        return tuple(sorted(instance_ids))

    def _validate(self, spec: RuntimeLaunchSpec) -> RuntimeCompatibility:
        if self._runtime_identity is None:
            raise RuntimeRequestError("runtime_identity_unavailable")
        if _CONFIG_VERSION_PATTERN.fullmatch(spec.config_version) is None:
            raise RuntimeRequestError("config_version_invalid")
        if _SERVED_MODEL_PATTERN.fullmatch(spec.served_model_name) is None:
            raise RuntimeRequestError("served_model_name_invalid")
        try:
            catalog = self._catalog or load_runtime_compatibility(self._compatibility_path)
        except RuntimeCompatibilityError as error:
            raise RuntimeRequestError("runtime_configuration_unavailable") from error
        self._catalog = catalog
        runtime = catalog.runtime(spec.runtime_name)
        if runtime is None:
            raise RuntimeRequestError("runtime_not_allowed")
        if runtime.launcher not in ("process", "container"):
            raise RuntimeRequestError("runtime_launcher_unavailable")
        if runtime.launcher == "process" and (
            not runtime.executable.is_file() or not os.access(runtime.executable, os.X_OK)
        ):
            raise RuntimeRequestError("runtime_executable_unavailable")
        policy_rejection = validate_runtime_launch_policy(
            runtime,
            selected_gpu_count=len(spec.gpu_uuids),
            context_length=spec.context_length,
            tensor_parallel_size=spec.tensor_parallel_size,
        )
        if policy_rejection is not None:
            raise RuntimeRequestError(policy_rejection)
        if len(set(spec.gpu_uuids)) != len(spec.gpu_uuids):
            raise RuntimeRequestError("gpu_set_invalid")
        if (
            not math.isfinite(spec.gpu_memory_utilization)
            or spec.gpu_memory_utilization < runtime.gpu_memory_utilization_minimum
            or spec.gpu_memory_utilization > runtime.gpu_memory_utilization_maximum
        ):
            raise RuntimeRequestError("gpu_memory_utilization_out_of_range")
        if self._model_path_policy is None:
            raise RuntimeRequestError("model_path_not_allowed")
        try:
            path_verdict = judge_external_path(spec.model_path, self._model_path_policy)
        except ValueError as error:
            raise RuntimeRequestError("model_path_not_allowed") from error
        if not path_verdict.accepted:
            raise RuntimeRequestError("model_path_not_allowed")
        inventory = discover_gpus(self._new_nvml())
        if inventory.state is not DiscoveryState.READY:
            raise RuntimeRequestError("gpu_inventory_unavailable")
        available = {device.uuid for device in inventory.devices}
        if any(gpu_uuid not in available for gpu_uuid in spec.gpu_uuids):
            raise RuntimeRequestError("gpu_not_found")
        selected_devices = tuple(
            device for device in inventory.devices if device.uuid in spec.gpu_uuids
        )
        if (
            runtime.gpu_policy.require_homogeneous_devices
            and len({(device.model, device.memory_bytes) for device in selected_devices}) != 1
        ):
            raise RuntimeRequestError("unsupported_gpu_topology")
        if runtime.gpu_policy.p2p_topology == "required":
            selected_uuids = frozenset(spec.gpu_uuids)
            selected_links = tuple(
                link
                for link in inventory.topology_links
                if link.gpu_a_uuid in selected_uuids and link.gpu_b_uuid in selected_uuids
            )
            expected_link_count = len(selected_devices) * (len(selected_devices) - 1) // 2
            if len(selected_links) != expected_link_count or any(
                link.p2p_read_state is not TopologyState.PRESENT for link in selected_links
            ):
                raise RuntimeRequestError("unsupported_gpu_topology")
        return runtime

    def _new_nvml(self) -> NvmlLibrary:
        return self._nvml_factory()

    def _observe_oom_killed(self, persisted: _PersistedRuntime) -> bool | None:
        if persisted.exit_code is None:
            return None
        # Popen uses negative return codes for signal termination.  Neither a
        # signal nor a clean exit is evidence of Runtime-reported GPU OOM.
        if persisted.exit_code <= 0:
            return False
        if persisted.runtime_name is None:
            return None
        try:
            catalog = self._catalog or load_runtime_compatibility(self._compatibility_path)
        except RuntimeCompatibilityError:
            return None
        self._catalog = catalog
        runtime = catalog.runtime(persisted.runtime_name)
        if runtime is None:
            return None
        return _detect_out_of_memory(
            self._state_root / persisted.instance_id / "runtime.log",
            exit_code=persisted.exit_code,
            signatures=runtime.failure_signatures.out_of_memory,
        )

    async def _terminate_persisted(
        self,
        canonical_id: str,
        persisted: _PersistedRuntime,
        *,
        grace_seconds: float,
    ) -> _PersistedRuntime:
        await _terminate_process_group_if_live(persisted.pid, grace_seconds)
        process = self._processes.pop(canonical_id, None)
        exit_code = None
        if process is not None:
            exit_code = await asyncio.to_thread(process.wait)
        return self._mark_exited(persisted, exit_code=exit_code)

    async def _launch(
        self,
        canonical_id: str,
        runtime: RuntimeCompatibility,
        spec: RuntimeLaunchSpec,
    ) -> RuntimeObservation:
        identity = self._runtime_identity
        assert identity is not None
        instance_root = self._state_root / canonical_id
        runtime_home = instance_root / "home"
        await asyncio.to_thread(_prepare_instance_root, instance_root, runtime_home, identity)
        log_path = instance_root / "runtime.log"
        environment = build_runtime_environment(runtime, spec, runtime_home)
        retry_deadline = time.monotonic() + MAX_RUNTIME_PORT_RETRY_SECONDS
        for attempt in range(MAX_RUNTIME_PORT_LAUNCH_ATTEMPTS):
            port = await asyncio.to_thread(_unused_loopback_port)
            command = await asyncio.to_thread(build_runtime_command, runtime, spec, port)
            launch_stage = "spawn"
            process = None
            try:
                with log_path.open("ab", buffering=0) as log:
                    os.fchmod(log.fileno(), 0o600)
                    process = subprocess.Popen(  # noqa: S603 - argv is closed and typed above.
                        command,
                        stdin=subprocess.DEVNULL,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        env=environment,
                        close_fds=True,
                        start_new_session=True,
                        user=identity.uid,
                        group=identity.gid,
                        extra_groups=(),
                    )
                launch_stage = "identity"
                start_ticks = _process_identity(process.pid)[1]
            except (OSError, PermissionError, ProcessLookupError, ValueError) as error:
                # Only closed classifications and numeric exit codes may leave
                # this boundary; exception text can contain paths or secrets.
                _LOGGER.warning(
                    json.dumps(
                        {
                            "event": "runtime_launch_failure",
                            "reason": launch_stage,
                            "exit_code": process.poll() if process is not None else None,
                            "port_probe": "not_run",
                        }
                    )
                )
                raise RuntimeRequestError("runtime_start_failed") from error

            started_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            persisted = _PersistedRuntime(
                instance_id=canonical_id,
                pid=process.pid,
                start_ticks=start_ticks,
                config_version=spec.config_version,
                endpoint=f"{_LOOPBACK_HOST}:{port}",
                started_at=started_at,
                state=RuntimeStateValue.RUNNING.value,
                exit_code=None,
                runtime_name=runtime.name,
            )
            try:
                await asyncio.to_thread(self._write_state, persisted)
            except OSError as error:
                _signal_group(process.pid, signal.SIGKILL)
                await asyncio.to_thread(process.wait)
                raise RuntimeRequestError("runtime_state_unavailable") from error
            self._processes[canonical_id] = process
            watcher = asyncio.create_task(self._watch(canonical_id, process, start_ticks))
            self._watch_tasks.add(watcher)
            watcher.add_done_callback(self._watch_tasks.discard)
            await asyncio.sleep(0.05)
            if process.poll() is None:
                return _observation(persisted, RuntimeStateValue.RUNNING)

            self._mark_exited(persisted, exit_code=process.returncode)
            self._processes.pop(canonical_id, None)
            port_is_occupied = await asyncio.to_thread(_loopback_port_is_occupied, port)
            _LOGGER.warning(
                json.dumps(
                    {
                        "event": "runtime_launch_failure",
                        "reason": "early_exit",
                        "exit_code": process.returncode,
                        "port_probe": "occupied" if port_is_occupied else "free",
                    }
                )
            )
            attempts_exhausted = attempt + 1 >= MAX_RUNTIME_PORT_LAUNCH_ATTEMPTS
            retry_time_exhausted = time.monotonic() >= retry_deadline
            if not port_is_occupied or attempts_exhausted or retry_time_exhausted:
                raise RuntimeRequestError("runtime_start_failed")

        raise AssertionError("bounded Runtime port launch loop exhausted without a result")

    async def _watch(
        self,
        instance_id: str,
        process: subprocess.Popen[bytes],
        start_ticks: int,
    ) -> None:
        while (exit_code := process.poll()) is None:
            await asyncio.sleep(0.25)
        async with self._lock:
            try:
                persisted = await asyncio.to_thread(self._read_state, instance_id)
            except RuntimeRequestError:
                if self._processes.get(instance_id) is process:
                    self._processes.pop(instance_id, None)
                return
            if (
                persisted is not None
                and persisted.pid == process.pid
                and persisted.start_ticks == start_ticks
            ):
                try:
                    self._mark_exited(persisted, exit_code=exit_code)
                except RuntimeRequestError:
                    if self._processes.get(instance_id) is process:
                        self._processes.pop(instance_id, None)
                    return
            if self._processes.get(instance_id) is process:
                self._processes.pop(instance_id, None)

    def _mark_exited(
        self,
        persisted: _PersistedRuntime,
        *,
        exit_code: int | None = None,
    ) -> _PersistedRuntime:
        if persisted.state == RuntimeStateValue.EXITED.value and exit_code is None:
            return persisted
        process = self._processes.get(persisted.instance_id)
        if exit_code is None and process is not None:
            exit_code = process.poll()
        exited = _PersistedRuntime(
            **{
                **asdict(persisted),
                "state": RuntimeStateValue.EXITED.value,
                "exit_code": exit_code,
            }
        )
        try:
            self._write_state(exited)
        except OSError:
            raise RuntimeRequestError("runtime_state_unavailable") from None
        return exited

    def _state_path(self, canonical_id: str) -> Path:
        return self._state_root / canonical_id / "runtime.json"

    def _read_state(self, canonical_id: str) -> _PersistedRuntime | None:
        try:
            payload: object = json.loads(self._state_path(canonical_id).read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError
            persisted = _PersistedRuntime(**payload)
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as error:
            raise RuntimeRequestError("runtime_state_unavailable") from error
        if persisted.instance_id != canonical_id or persisted.state not in {
            RuntimeStateValue.RUNNING.value,
            RuntimeStateValue.EXITED.value,
        }:
            raise RuntimeRequestError("runtime_state_unavailable")
        exit_code_invalid = persisted.exit_code is not None and (
            isinstance(persisted.exit_code, bool) or not isinstance(persisted.exit_code, int)
        )
        if (
            isinstance(persisted.pid, bool)
            or not isinstance(persisted.pid, int)
            or persisted.pid <= 0
            or isinstance(persisted.start_ticks, bool)
            or not isinstance(persisted.start_ticks, int)
            or persisted.start_ticks <= 0
            or not isinstance(persisted.config_version, str)
            or not isinstance(persisted.endpoint, str)
            or not isinstance(persisted.started_at, str)
            or (
                persisted.runtime_name is not None
                and (
                    not isinstance(persisted.runtime_name, str)
                    or _CONFIG_VERSION_PATTERN.fullmatch(persisted.runtime_name) is None
                )
            )
            or exit_code_invalid
        ):
            raise RuntimeRequestError("runtime_state_unavailable")
        return persisted

    def _write_state(self, persisted: _PersistedRuntime) -> None:
        path = self._state_path(persisted.instance_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".new")
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        try:
            remaining = memoryview(
                (json.dumps(asdict(persisted), separators=(",", ":")) + "\n").encode("utf-8")
            )
            while remaining:
                remaining = remaining[os.write(descriptor, remaining) :]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, path)


def _canonical_instance_id(value: str) -> str:
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError) as error:
        raise RuntimeRequestError("instance_id_invalid") from error
    canonical = str(parsed)
    if value != canonical:
        raise RuntimeRequestError("instance_id_invalid")
    return canonical


def _prepare_instance_root(
    instance_root: Path,
    runtime_home: Path,
    identity: RuntimeIdentity,
) -> None:
    instance_root.mkdir(parents=True, exist_ok=True)
    # Runtime may traverse to its HOME, but cannot list or modify Agent state.
    os.chmod(instance_root, 0o711)
    runtime_home.mkdir(exist_ok=True)
    os.chmod(runtime_home, 0o700)
    if os.geteuid() == 0:
        os.chown(runtime_home, identity.uid, identity.gid)


def _unused_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((_LOOPBACK_HOST, 0))
        return int(probe.getsockname()[1])


def _loopback_port_is_occupied(port: int) -> bool:
    """Return only whether another process currently prevents binding this port."""

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind((_LOOPBACK_HOST, port))
    except OSError as error:
        return error.errno == errno.EADDRINUSE
    return False


def _process_identity(pid: int) -> tuple[str, int, int]:
    content = (Path("/proc") / str(pid) / "stat").read_text(encoding="ascii")
    tail = content[content.rfind(")") + 2 :].split()
    return tail[0], int(tail[19]), int(tail[2])


def _persisted_is_running(persisted: _PersistedRuntime) -> bool:
    if persisted.state != RuntimeStateValue.RUNNING.value:
        return False
    try:
        process_state, start_ticks, process_group = _process_identity(persisted.pid)
    except (FileNotFoundError, OSError, ValueError, IndexError):
        return False
    return (
        process_state != "Z"
        and start_ticks == persisted.start_ticks
        and process_group == persisted.pid
    )


def _group_has_live_processes(process_group: int) -> bool:
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            state, _start_ticks, group = _process_identity(int(entry.name))
        except (FileNotFoundError, OSError, ValueError, IndexError):
            continue
        if group == process_group and state != "Z":
            return True
    return False


def _process_group_pids(process_group: int) -> frozenset[int]:
    pids: set[int] = set()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal():
            continue
        pid = int(entry.name)
        try:
            state, _start_ticks, group = _process_identity(pid)
        except (FileNotFoundError, OSError, ValueError, IndexError):
            continue
        if group == process_group and state != "Z":
            pids.add(pid)
    return frozenset(pids)


def _gpu_unknown_reason(error: NvmlCallError) -> GpuObservationUnknownReason:
    if error.kind in {
        NvmlErrorKind.LIBRARY_UNAVAILABLE,
        NvmlErrorKind.DRIVER_UNAVAILABLE,
        NvmlErrorKind.PERMISSION_DENIED,
        NvmlErrorKind.NOT_SUPPORTED,
        NvmlErrorKind.FUNCTION_UNAVAILABLE,
    }:
        return GpuObservationUnknownReason.SOURCE_UNAVAILABLE
    return GpuObservationUnknownReason.QUERY_FAILED


def _observe_runtime_gpu_uuids(
    library: NvmlLibrary,
    process_group: int,
) -> ObservedGpuUuids:
    try:
        library.initialize()
    except NvmlCallError as error:
        return ObservedGpuUuids(
            ObservationKnowledgeState.UNKNOWN,
            unknown_reason=_gpu_unknown_reason(error),
        )
    try:
        runtime_pids = _process_group_pids(process_group)
        if not runtime_pids:
            return ObservedGpuUuids(
                ObservationKnowledgeState.UNKNOWN,
                unknown_reason=GpuObservationUnknownReason.QUERY_FAILED,
            )
        observed: set[str] = set()
        for index in range(library.device_get_count()):
            handle = library.device_get_handle_by_index(index)
            gpu_uuid = library.device_get_uuid(handle)
            if not gpu_uuid.startswith("GPU-") or any(
                character.isspace() for character in gpu_uuid
            ):
                raise NvmlCallError(NvmlErrorKind.OTHER)
            if runtime_pids.intersection(library.device_get_compute_running_process_pids(handle)):
                observed.add(gpu_uuid)
        return ObservedGpuUuids(
            ObservationKnowledgeState.KNOWN,
            values=tuple(sorted(observed)),
        )
    except NvmlCallError as error:
        return ObservedGpuUuids(
            ObservationKnowledgeState.UNKNOWN,
            unknown_reason=_gpu_unknown_reason(error),
        )
    except OSError:
        return ObservedGpuUuids(
            ObservationKnowledgeState.UNKNOWN,
            unknown_reason=GpuObservationUnknownReason.QUERY_FAILED,
        )
    finally:
        with suppress(NvmlCallError):
            library.shutdown()


def _signal_group(process_group: int, selected_signal: signal.Signals) -> None:
    try:
        os.killpg(process_group, selected_signal)
    except ProcessLookupError:
        return


def _detect_out_of_memory(
    log_path: Path,
    *,
    exit_code: int | None,
    signatures: tuple[str, ...],
) -> bool | None:
    """Inspect only a bounded log tail and return no log content or path."""

    if exit_code is None:
        return None
    if exit_code <= 0:
        return False
    try:
        with log_path.open("rb") as log:
            log.seek(0, os.SEEK_END)
            length = log.tell()
            log.seek(max(0, length - MAX_OOM_LOG_TAIL_BYTES))
            tail = log.read(MAX_OOM_LOG_TAIL_BYTES)
    except OSError:
        return None
    return any(signature.encode("utf-8") in tail for signature in signatures)


async def _terminate_process_group_if_live(process_group: int, grace_seconds: float) -> None:
    if not await asyncio.to_thread(_group_has_live_processes, process_group):
        return
    await asyncio.to_thread(_signal_group, process_group, signal.SIGTERM)
    if await _wait_for_group_exit(process_group, grace_seconds):
        return
    await asyncio.to_thread(_signal_group, process_group, signal.SIGKILL)
    if not await _wait_for_group_exit(process_group, _EXIT_CONFIRM_SECONDS):
        raise RuntimeRequestError("runtime_stop_failed")


async def _wait_for_group_exit(process_group: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not await asyncio.to_thread(_group_has_live_processes, process_group):
            return True
        await asyncio.sleep(0.05)
    return not await asyncio.to_thread(_group_has_live_processes, process_group)


def _observation(
    persisted: _PersistedRuntime,
    state: RuntimeStateValue,
    *,
    gpu_uuids: ObservedGpuUuids = _GPU_NOT_COLLECTED,
    oom_killed: bool | None = None,
) -> RuntimeObservation:
    return RuntimeObservation(
        state=state,
        endpoint=persisted.endpoint,
        started_at=persisted.started_at,
        exit_code=persisted.exit_code,
        gpu_uuids=gpu_uuids,
        oom_killed=oom_killed,
    )
