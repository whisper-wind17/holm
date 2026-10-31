"""Closed Docker Engine API adapter for platform-owned Runtime containers."""

from __future__ import annotations

import hashlib
import http.client
import io
import json
import os
import socket
import stat
from collections import deque
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Any, Final, cast
from urllib.parse import quote
from uuid import UUID, uuid4

from holm_contracts.release import PRODUCTION_SERVICE_IDS
from holm_runtimes import RuntimeCompatibility

from holm_host_agent.path_validation import RuntimeIdentity
from holm_host_agent.runtime_logs import (
    DEFAULT_MAX_LOG_LINE_CHARACTERS,
    MAX_LOG_LINE_CHARACTERS,
    MAX_LOG_PAGE_SIZE,
    MAX_RETAINED_LOG_LINES,
    RuntimeLogEntry,
    RuntimeLogPage,
    RuntimeLogRequestError,
    _iter_bounded_lines,
)
from holm_host_agent.runtime_process import (
    DEFAULT_STOP_GRACE_SECONDS,
    MAX_OOM_LOG_TAIL_BYTES,
    MAX_STOP_GRACE_SECONDS,
    GpuObservationUnknownReason,
    ObservationKnowledgeState,
    ObservedGpuUuids,
    RuntimeLaunchSpec,
    RuntimeObservation,
    RuntimeRequestError,
    RuntimeStateValue,
    RuntimeSummary,
    build_runtime_command,
)

DOCKER_SOCKET: Final[Path] = Path("/var/run/docker.sock")
DEFAULT_RUNTIME_CACHE_ROOT: Final[Path] = Path("/var/lib/holm/runtime-cache")
_API: Final[str] = "/v1.47"
_OWNER_LABEL: Final[str] = "holm.managed"
_INSTANCE_LABEL: Final[str] = "holm.instance_id"
_RUNTIME_LABEL: Final[str] = "holm.runtime_name"
_LAUNCH_HASH_LABEL: Final[str] = "holm.launch_hash"
_NETWORK_LABEL: Final[str] = "com.docker.compose.network"
_NETWORK_NAME: Final[str] = "holm-runtime"
_MAX_RESPONSE_BYTES: Final[int] = 1024 * 1024
_UNOBSERVED_GPUS: Final[ObservedGpuUuids] = ObservedGpuUuids(
    ObservationKnowledgeState.UNKNOWN,
    unknown_reason=GpuObservationUnknownReason.NOT_COLLECTED,
)


# Both certified Runtime images provide python3. No caller-controlled code or paths
# enter this program. Preflight is read-only; deletion repeats it before mutation.
_CACHE_CLEANUP_SCRIPT: Final[str] = """
import os
import stat
import sys

class Unsafe(Exception):
    pass

def validate(fd, device):
    if os.fstat(fd).st_dev != device:
        raise Unsafe()
    with os.scandir(fd) as entries:
        for entry in entries:
            info = entry.stat(follow_symlinks=False)
            if info.st_dev != device:
                raise Unsafe()
            if stat.S_ISDIR(info.st_mode):
                child = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    validate(child, device)
                finally:
                    os.close(child)
            elif not stat.S_ISREG(info.st_mode):
                raise Unsafe()

def erase(fd):
    with os.scandir(fd) as entries:
        for entry in entries:
            if entry.is_dir(follow_symlinks=False):
                child = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    os.fchmod(child, stat.S_IMODE(os.fstat(child).st_mode) | 0o700)
                    erase(child)
                finally:
                    os.close(child)
                os.rmdir(entry.name, dir_fd=fd)
            else:
                os.unlink(entry.name, dir_fd=fd)

try:
    fd = os.open('/var/cache/holm', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        validate(fd, os.fstat(fd).st_dev)
        if sys.argv[1] == 'delete':
            erase(fd)
    finally:
        os.close(fd)
except Unsafe:
    sys.exit(3)
except Exception:
    sys.exit(1)
"""


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: Path, *, timeout: float = 5.0) -> None:
        super().__init__("docker", timeout=timeout)
        self._socket_path = socket_path

    def connect(self) -> None:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(self.timeout)
        connection.connect(str(self._socket_path))
        self.sock = connection


class DockerEngine:
    """One bounded request per connection; raw daemon errors never cross RPC."""

    def __init__(self, socket_path: Path = DOCKER_SOCKET) -> None:
        self._socket_path = socket_path

    def request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, object] | None = None,
        *,
        timeout: float = 5.0,
    ) -> tuple[int, object]:
        body = None if payload is None else json.dumps(payload, separators=(",", ":")).encode()
        connection = _UnixConnection(self._socket_path, timeout=timeout)
        try:
            connection.request(
                method,
                _API + path,
                body=body,
                headers={"Content-Type": "application/json"} if body is not None else {},
            )
            response = connection.getresponse()
            content = response.read(_MAX_RESPONSE_BYTES + 1)
            if len(content) > _MAX_RESPONSE_BYTES:
                raise RuntimeRequestError("runtime_engine_unavailable")
            if not content:
                return response.status, None
            try:
                return response.status, json.loads(content)
            except (UnicodeError, json.JSONDecodeError) as error:
                raise RuntimeRequestError("runtime_engine_unavailable") from error
        except (OSError, http.client.HTTPException) as error:
            raise RuntimeRequestError("runtime_engine_unavailable") from error
        finally:
            connection.close()

    def log_tail(self, instance_id: str) -> bytes:
        connection = _UnixConnection(self._socket_path)
        try:
            connection.request(
                "GET",
                _API + f"/containers/runtime-{instance_id}/logs?stdout=1&stderr=1&tail=500",
            )
            response = connection.getresponse()
            if response.status != 200:
                raise RuntimeRequestError("runtime_log_unavailable")
            content = response.read(_MAX_RESPONSE_BYTES + 1)
            if len(content) > _MAX_RESPONSE_BYTES:
                raise RuntimeRequestError("runtime_log_unavailable")
            return content
        except (OSError, http.client.HTTPException) as error:
            raise RuntimeRequestError("runtime_log_unavailable") from error
        finally:
            connection.close()


def _docker_log_text(content: bytes) -> bytes:
    """Strip Docker's optional eight-byte stream frames without losing text."""
    if not content or content[0] not in (1, 2) or len(content) < 8:
        return content
    result = bytearray()
    offset = 0
    while offset + 8 <= len(content):
        size = int.from_bytes(content[offset + 4 : offset + 8], "big")
        if content[offset] not in (1, 2) or offset + 8 + size > len(content):
            return content
        result.extend(content[offset + 8 : offset + 8 + size])
        offset += 8 + size
    return bytes(result) if offset == len(content) else content


def _object(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeRequestError("runtime_engine_unavailable")
    return cast(dict[str, Any], value)


def _list(value: object) -> list[Any]:
    if not isinstance(value, list):
        raise RuntimeRequestError("runtime_engine_unavailable")
    return value


class ContainerRuntimeManager:
    """Only containers with both platform ownership labels are observable or mutable."""

    def __init__(
        self,
        *,
        cache_root: Path,
        runtime_identity: RuntimeIdentity | None,
        engine: DockerEngine | None = None,
    ) -> None:
        self._cache_root = cache_root
        self._identity = runtime_identity
        self._engine = engine or DockerEngine()
        self._runtimes: Mapping[str, RuntimeCompatibility] = {}

    def set_runtimes(self, runtimes: Mapping[str, RuntimeCompatibility]) -> None:
        self._runtimes = runtimes

    def _owned(self, value: Mapping[str, Any], instance_id: str) -> bool:
        labels = _object(_object(value.get("Config")).get("Labels") or {})
        return labels.get(_OWNER_LABEL) == "true" and labels.get(_INSTANCE_LABEL) == instance_id

    def _inspect(self, instance_id: str, *, reject_unowned: bool = False) -> dict[str, Any] | None:
        status, value = self._engine.request("GET", f"/containers/runtime-{instance_id}/json")
        if status == 404:
            return None
        if status != 200:
            raise RuntimeRequestError("runtime_engine_unavailable")
        inspected = _object(value)
        if self._owned(inspected, instance_id):
            return inspected
        if reject_unowned:
            raise RuntimeRequestError("runtime_ownership_conflict")
        return None

    def _network(self) -> str:
        status, value = self._engine.request("GET", "/networks")
        if status != 200:
            raise RuntimeRequestError("runtime_network_unavailable")
        matches = [
            network
            for network in _list(value)
            if isinstance(network, dict)
            and _object(network.get("Labels") or {}).get(_NETWORK_LABEL) == _NETWORK_NAME
            and network.get("Internal") is True
        ]
        if len(matches) != 1 or not isinstance(matches[0].get("Id"), str):
            raise RuntimeRequestError("runtime_network_unavailable")
        return cast(str, matches[0]["Id"])

    def ensure(
        self, instance_id: str, runtime: RuntimeCompatibility, spec: RuntimeLaunchSpec
    ) -> RuntimeObservation:
        identity = self._identity
        if identity is None or (identity.uid, identity.gid) != PRODUCTION_SERVICE_IDS["runtime"]:
            raise RuntimeRequestError("runtime_identity_unavailable")
        if runtime.image_digest is None or runtime.container_resources is None:
            raise RuntimeRequestError("runtime_configuration_unavailable")
        self._runtimes = {**self._runtimes, runtime.name: runtime}
        existing = self._inspect(instance_id, reject_unowned=True)
        launch_hash = hashlib.sha256(json.dumps(asdict(spec), sort_keys=True).encode()).hexdigest()
        if existing is not None:
            if _object(existing["State"]).get("Running") is True:
                labels = _object(_object(existing["Config"])["Labels"])
                if (
                    labels.get("config_version") != spec.config_version
                    or labels.get(_RUNTIME_LABEL) != runtime.name
                    or labels.get(_LAUNCH_HASH_LABEL) != launch_hash
                    or existing.get("Image") != runtime.image_digest
                    or tuple(self._gpu_ids(existing)) != spec.gpu_uuids
                ):
                    raise RuntimeRequestError("runtime_config_conflict")
                return self._observation(existing, runtime)
            self._remove_owned(instance_id, existing)

        network_id = self._network()
        cache = self._cache_root / instance_id
        try:
            if not self._cache_root.is_dir() or self._cache_root.is_symlink():
                raise RuntimeRequestError("runtime_state_unavailable")
            cache.mkdir(mode=0o770, exist_ok=True)
            if cache.is_symlink() or cache.stat().st_gid != identity.gid:
                raise RuntimeRequestError("runtime_state_unavailable")
            cache.chmod(0o770)
        except OSError as error:
            raise RuntimeRequestError("runtime_state_unavailable") from error
        resources = runtime.container_resources
        count = len(spec.gpu_uuids)
        command = build_runtime_command(runtime, spec, resources.port, host="0.0.0.0")
        name = f"runtime-{instance_id}"
        payload: dict[str, object] = {
            "Image": runtime.image_digest,
            "Entrypoint": [command[0]],
            "Cmd": list(command[1:]),
            "User": f"{identity.uid}:{identity.gid}",
            "WorkingDir": "/var/cache/holm",
            "Env": [
                "HF_HUB_OFFLINE=1",
                "HOME=/var/cache/holm",
                "HF_HOME=/var/cache/holm/huggingface",
                "XDG_CACHE_HOME=/var/cache/holm",
            ],
            "Labels": {
                _OWNER_LABEL: "true",
                _INSTANCE_LABEL: instance_id,
                _RUNTIME_LABEL: runtime.name,
                _LAUNCH_HASH_LABEL: launch_hash,
                "config_version": spec.config_version,
            },
            "HostConfig": {
                "NetworkMode": network_id,
                "Mounts": [
                    {
                        "Type": "bind",
                        "Source": spec.model_path,
                        "Target": spec.model_path,
                        "ReadOnly": True,
                    },
                    {
                        "Type": "bind",
                        "Source": str(cache),
                        "Target": "/var/cache/holm",
                        "ReadOnly": False,
                    },
                ],
                "DeviceRequests": [
                    {
                        "Driver": "nvidia",
                        "Count": 0,
                        "DeviceIDs": list(spec.gpu_uuids),
                        "Capabilities": [["gpu"]],
                    }
                ],
                "ReadonlyRootfs": True,
                "CapDrop": ["ALL"],
                "SecurityOpt": ["no-new-privileges:true"],
                "Tmpfs": {"/tmp": f"rw,nosuid,nodev,size={resources.tmpfs_bytes},mode=1777"},
                "ShmSize": resources.shm_bytes_per_gpu * count,
                "Memory": resources.memory_bytes_per_gpu * count,
                "NanoCpus": resources.nano_cpus_per_gpu * count,
                "PidsLimit": resources.pids_limit,
                "LogConfig": {"Type": "local", "Config": {"max-size": "20m", "max-file": "5"}},
                "RestartPolicy": {"Name": "no"},
                "Privileged": False,
                "PublishAllPorts": False,
            },
            "NetworkingConfig": {"EndpointsConfig": {network_id: {"Aliases": [name]}}},
        }
        status, _ = self._engine.request("POST", f"/containers/create?name={quote(name)}", payload)
        if status != 201:
            raise RuntimeRequestError("runtime_start_failed")
        status, _ = self._engine.request("POST", f"/containers/{quote(name)}/start")
        if status not in (204, 304):
            raise RuntimeRequestError("runtime_start_failed")
        created = self._inspect(instance_id)
        if created is None:
            raise RuntimeRequestError("runtime_start_failed")
        return self._observation(created, runtime)

    def _observation(
        self, value: Mapping[str, Any], runtime: RuntimeCompatibility | None = None
    ) -> RuntimeObservation:
        state = _object(value["State"])
        running = state.get("Running") is True
        exit_code = None if running else state.get("ExitCode")
        if exit_code is not None and (
            isinstance(exit_code, bool) or not isinstance(exit_code, int)
        ):
            raise RuntimeRequestError("runtime_engine_unavailable")
        labels = _object(_object(value["Config"])["Labels"])
        runtime = runtime or self._runtimes.get(str(labels.get(_RUNTIME_LABEL)))
        if runtime is None or runtime.container_resources is None:
            raise RuntimeRequestError("runtime_configuration_unavailable")
        name = f"runtime-{labels[_INSTANCE_LABEL]}"
        port = runtime.container_resources.port
        oom_killed: bool | None = None
        if not running and exit_code is not None:
            if exit_code <= 0:
                oom_killed = False
            else:
                try:
                    tail = _docker_log_text(self._engine.log_tail(str(labels[_INSTANCE_LABEL])))
                except RuntimeRequestError:
                    pass
                else:
                    tail = tail[-MAX_OOM_LOG_TAIL_BYTES:]
                    oom_killed = any(
                        signature.encode() in tail
                        for signature in runtime.failure_signatures.out_of_memory
                    )
        return RuntimeObservation(
            state=RuntimeStateValue.RUNNING if running else RuntimeStateValue.EXITED,
            endpoint=f"{name}:{port}",
            started_at=str(state.get("StartedAt", "")) if running else "",
            exit_code=exit_code,
            gpu_uuids=_UNOBSERVED_GPUS,
            oom_killed=oom_killed,
        )

    def _gpu_ids(self, value: Mapping[str, Any]) -> list[str]:
        requests = _list(_object(value["HostConfig"])["DeviceRequests"])
        if len(requests) != 1:
            raise RuntimeRequestError("runtime_engine_unavailable")
        ids = _list(_object(requests[0])["DeviceIDs"])
        if any(not isinstance(item, str) for item in ids):
            raise RuntimeRequestError("runtime_engine_unavailable")
        return cast(list[str], ids)

    def inspect(
        self, instance_id: str, runtime: RuntimeCompatibility | None = None
    ) -> RuntimeObservation:
        value = self._inspect(instance_id)
        return (
            RuntimeObservation(RuntimeStateValue.NOT_FOUND)
            if value is None
            else self._observation(value, runtime)
        )

    def stop(self, instance_id: str, grace_seconds: int) -> RuntimeObservation:
        if grace_seconds < 0 or grace_seconds > MAX_STOP_GRACE_SECONDS:
            raise RuntimeRequestError("grace_seconds_invalid")
        value = self._inspect(instance_id)
        if value is None:
            return RuntimeObservation(RuntimeStateValue.NOT_FOUND)
        if _object(value["State"]).get("Running") is True:
            seconds = grace_seconds or DEFAULT_STOP_GRACE_SECONDS
            status, _ = self._engine.request(
                "POST", f"/containers/runtime-{instance_id}/stop?t={seconds}", timeout=seconds + 10
            )
            if status not in (204, 304):
                raise RuntimeRequestError("runtime_stop_failed")
            value = self._inspect(instance_id)
            if value is None:
                raise RuntimeRequestError("runtime_stop_failed")
        return self._observation(value)

    def _remove_owned(self, instance_id: str, value: Mapping[str, Any]) -> None:
        if _object(value["State"]).get("Running") is True:
            raise RuntimeRequestError("runtime_state_conflict")
        status, _ = self._engine.request("DELETE", f"/containers/runtime-{instance_id}")
        if status not in (204, 404):
            raise RuntimeRequestError("runtime_remove_failed")

    def remove(self, instance_id: str) -> None:
        try:
            if str(UUID(instance_id)) != instance_id:
                raise ValueError("noncanonical UUID")
        except ValueError:
            raise RuntimeRequestError("instance_id_invalid") from None
        value = self._inspect(instance_id, reject_unowned=True)
        # Validate before any filesystem mutation, including cache preflight.
        if value is not None and _object(value["State"]).get("Running") is not False:
            raise RuntimeRequestError("runtime_state_conflict")
        try:
            if self._cache_root.resolve(strict=True) != self._cache_root.absolute():
                raise RuntimeRequestError("runtime_cache_unsafe")
            root_fd = os.open(self._cache_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                try:
                    cache_fd = os.open(
                        instance_id,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=root_fd,
                    )
                except FileNotFoundError:
                    cache_fd = None
                except OSError:
                    raise RuntimeRequestError("runtime_cache_unsafe") from None
                if cache_fd is not None:
                    try:
                        if os.fstat(cache_fd).st_dev != os.fstat(root_fd).st_dev:
                            raise RuntimeRequestError("runtime_cache_unsafe")
                        self._validate_cache_tree(cache_fd, os.fstat(root_fd).st_dev)
                        image = self._cleanup_image(value)
                        self._check_cache_boundary(root_fd, cache_fd, instance_id)
                        self._run_cache_cleanup(instance_id, image, delete=False)
                        self._check_cache_boundary(root_fd, cache_fd, instance_id)
                        if value is not None:
                            self._remove_owned(instance_id, value)
                        self._run_cache_cleanup(instance_id, image, delete=True)
                        self._check_cache_boundary(root_fd, cache_fd, instance_id)
                        os.rmdir(instance_id, dir_fd=root_fd)
                    finally:
                        os.close(cache_fd)
                elif value is not None:
                    self._remove_owned(instance_id, value)
            finally:
                os.close(root_fd)
        except OSError:
            raise RuntimeRequestError("runtime_cache_cleanup_failed") from None

    def _check_cache_boundary(self, root_fd: int, cache_fd: int, instance_id: str) -> None:
        root = self._cache_root.stat(follow_symlinks=False)
        cache = (self._cache_root / instance_id).stat(follow_symlinks=False)
        if (
            self._cache_root.resolve(strict=True) != self._cache_root.absolute()
            or not os.path.samestat(root, os.fstat(root_fd))
            or not os.path.samestat(cache, os.fstat(cache_fd))
        ):
            raise RuntimeRequestError("runtime_cache_unsafe")

    def _cleanup_image(self, value: Mapping[str, Any] | None) -> str:
        identity = self._identity
        if identity is None or (identity.uid, identity.gid) != PRODUCTION_SERVICE_IDS["runtime"]:
            raise RuntimeRequestError("runtime_cache_cleanup_failed")
        # An owned Runtime's immutable image also works after a catalog refresh.
        image = value.get("Image") if value is not None else None
        if image is None:
            image = next((r.image_digest for r in self._runtimes.values() if r.image_digest), None)
        if not isinstance(image, str):
            raise RuntimeRequestError("runtime_cache_cleanup_failed")
        digest = image.rsplit("@", 1)[-1]
        if (
            not digest.startswith("sha256:")
            or len(digest) != 71
            or any(c not in "0123456789abcdef" for c in digest[7:])
        ):
            raise RuntimeRequestError("runtime_cache_cleanup_failed")
        return image

    def _run_cache_cleanup(self, instance_id: str, image: str, *, delete: bool) -> None:
        uid, gid = PRODUCTION_SERVICE_IDS["runtime"]
        name = f"runtime-cache-cleanup-{uuid4()}"
        payload: dict[str, object] = {
            "Image": image,
            "User": f"{uid}:{gid}",
            "Entrypoint": ["python3", "-I", "-B", "-c", _CACHE_CLEANUP_SCRIPT],
            "Cmd": ["delete" if delete else "check"],
            # Cleanup must bypass a host's NVIDIA default and the image's GPU defaults.
            "Env": ["NVIDIA_VISIBLE_DEVICES=void"],
            "Labels": {_OWNER_LABEL: "true", _INSTANCE_LABEL: instance_id},
            "HostConfig": {
                "Runtime": "runc",
                "NetworkMode": "none",
                "Mounts": [
                    {
                        "Type": "bind",
                        "Source": str(self._cache_root / instance_id),
                        "Target": "/var/cache/holm",
                        "ReadOnly": not delete,
                    }
                ],
                "ReadonlyRootfs": True,
                "CapDrop": ["ALL"],
                "SecurityOpt": ["no-new-privileges:true"],
                "Privileged": False,
                "PublishAllPorts": False,
                "RestartPolicy": {"Name": "no"},
                "LogConfig": {"Type": "none"},
                "Memory": 128 * 1024 * 1024,
                "NanoCpus": 10**9,
                "PidsLimit": 16,
            },
        }
        created = False
        try:
            status, _ = self._engine.request("POST", f"/containers/create?name={name}", payload)
            if status != 201:
                raise RuntimeRequestError("runtime_cache_cleanup_failed")
            created = True
            status, _ = self._engine.request("POST", f"/containers/{name}/start")
            if status not in (204, 304):
                raise RuntimeRequestError("runtime_cache_cleanup_failed")
            status, result = self._engine.request(
                "POST", f"/containers/{name}/wait?condition=not-running", timeout=60.0
            )
            if status != 200 or not isinstance(result, dict) or result.get("Error"):
                raise RuntimeRequestError("runtime_cache_cleanup_failed")
            code = result.get("StatusCode")
            if type(code) is not int or code != 0:
                raise RuntimeRequestError(
                    "runtime_cache_unsafe"
                    if type(code) is int and code == 3
                    else "runtime_cache_cleanup_failed"
                )
        except RuntimeRequestError as error:
            if str(error) == "runtime_cache_unsafe":
                raise
            raise RuntimeRequestError("runtime_cache_cleanup_failed") from None
        finally:
            if created:
                try:
                    status, _ = self._engine.request("DELETE", f"/containers/{name}?force=1")
                    if status not in (204, 404):
                        raise RuntimeRequestError("runtime_cache_cleanup_failed")
                except RuntimeRequestError:
                    raise RuntimeRequestError("runtime_cache_cleanup_failed") from None

    @staticmethod
    def _validate_cache_tree(directory_fd: int, device: int) -> None:
        """Reject links, special files and cross-device directories before deletion."""
        if os.fstat(directory_fd).st_dev != device:
            raise RuntimeRequestError("runtime_cache_unsafe")
        with os.scandir(directory_fd) as entries:
            for entry in entries:
                metadata = entry.stat(follow_symlinks=False)
                if stat.S_ISDIR(metadata.st_mode):
                    try:
                        child = os.open(
                            entry.name,
                            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=directory_fd,
                        )
                    except PermissionError:
                        # The read-only helper checks this subtree as its Runtime owner.
                        continue
                    try:
                        ContainerRuntimeManager._validate_cache_tree(child, device)
                    finally:
                        os.close(child)
                elif not stat.S_ISREG(metadata.st_mode) or metadata.st_dev != device:
                    raise RuntimeRequestError("runtime_cache_unsafe")

    def list(self) -> tuple[RuntimeSummary, ...]:
        status, value = self._engine.request("GET", "/containers/json?all=1")
        if status != 200:
            raise RuntimeRequestError("runtime_engine_unavailable")
        summaries: list[RuntimeSummary] = []
        seen: set[str] = set()
        for item in _list(value):
            record = _object(item)
            labels = _object(record.get("Labels") or {})
            instance_id = labels.get(_INSTANCE_LABEL)
            if labels.get(_OWNER_LABEL) != "true" or not isinstance(instance_id, str):
                continue
            try:
                if str(UUID(instance_id)) != instance_id:
                    continue
            except ValueError:
                continue
            if instance_id in seen:
                continue
            # Cleanup and stray containers can share the formal Runtime's labels.
            seen.add(instance_id)
            inspected = self._inspect(instance_id)
            if inspected is None:
                continue
            observation = self._observation(inspected)
            summaries.append(RuntimeSummary(instance_id, observation.state, observation.gpu_uuids))
        return tuple(sorted(summaries, key=lambda item: item.instance_id))

    def log_page(
        self,
        instance_id: str,
        *,
        cursor_offset: int,
        page_size: int,
        max_log_line_characters: int = DEFAULT_MAX_LOG_LINE_CHARACTERS,
    ) -> RuntimeLogPage:
        if not 1 <= page_size <= MAX_LOG_PAGE_SIZE:
            raise RuntimeLogRequestError("log_page_size_invalid")
        if cursor_offset < 0:
            raise RuntimeLogRequestError("log_cursor_invalid")
        if not 1 <= max_log_line_characters <= MAX_LOG_LINE_CHARACTERS:
            raise RuntimeLogRequestError("log_line_limit_invalid")
        if self._inspect(instance_id) is None:
            return RuntimeLogPage((), 0, False)
        content = _docker_log_text(self._engine.log_tail(instance_id))
        retained = tuple(
            deque(
                _iter_bounded_lines(io.BytesIO(content), max_log_line_characters),
                maxlen=MAX_RETAINED_LOG_LINES,
            )
        )
        if cursor_offset > len(retained):
            raise RuntimeLogRequestError("log_cursor_invalid")
        end = min(cursor_offset + page_size, len(retained))
        entries = tuple(
            RuntimeLogEntry(index, line.text, line.truncated, line.original_character_count)
            for index, line in enumerate(retained[cursor_offset:end], start=cursor_offset)
        )
        return RuntimeLogPage(entries, end, end < len(retained))
