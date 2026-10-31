"""Versioned gRPC server on the privileged Host Agent Unix socket."""

from __future__ import annotations

import asyncio
import errno
import fcntl
import os
import socket
import stat
import struct
import tempfile
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from pathlib import Path
from types import MappingProxyType, TracebackType
from typing import Final, Self

import grpc
from grpc import aio
from holm_contracts.agent.v1 import agent_pb2, agent_pb2_grpc
from holm_contracts.external_paths import ExternalPathPolicy

from holm_host_agent.authorization import (
    AuthorizationInterceptor,
    DenialAuditor,
    IdentityCatalog,
    StructuredDenialAuditor,
    load_identity_catalog,
)
from holm_host_agent.gpu_discovery import (
    NvmlLibrary,
    PynvmlLibrary,
    discover_gpus,
    inventory_to_proto,
)
from holm_host_agent.host_info import get_host_info
from holm_host_agent.path_validation import (
    RuntimeIdentity,
    RuntimeReadabilityConclusion,
    RuntimeReadabilityJudgement,
    RuntimeReadabilityUnknownReason,
    judge_runtime_readability,
    load_runtime_identity,
)
from holm_host_agent.runtime_logs import (
    DEFAULT_MAX_LOG_LINE_CHARACTERS,
    MAX_LOG_LINE_CHARACTERS,
    RuntimeLogRequestError,
    read_runtime_log_page,
)
from holm_host_agent.runtime_process import (
    DEFAULT_RUNTIME_COMPAT_PATH,
    DEFAULT_RUNTIME_STATE_ROOT,
    GpuObservationUnknownReason,
    ObservationKnowledgeState,
    ObservedGpuUuids,
    RuntimeLaunchSpec,
    RuntimeManager,
    RuntimeObservation,
    RuntimeRequestError,
    RuntimeStateValue,
)

AGENT_SOCKET_PATH: Final[Path] = Path("/run/holm/agent.sock")
AGENT_SOCKET_MODE: Final[int] = 0o660
DEFAULT_RELEASE_MANIFEST_PATH: Final[Path] = Path("/opt/holm/release-manifest.json")
DEFAULT_MAX_PEER_CONNECTIONS: Final[int] = 64
_SOCKET_CREATION_UMASK: Final[int] = 0o117
_PEER_CREDENTIAL_FORMAT: Final[str] = "3i"
_RELAY_BUFFER_BYTES: Final[int] = 64 * 1024
_UNIMPLEMENTED_DETAIL: Final[str] = "RPC is reserved but not implemented"

_RUNTIME_STATE_VALUE: Final[Mapping[RuntimeStateValue, int]] = MappingProxyType(
    {
        RuntimeStateValue.RUNNING: agent_pb2.RUNTIME_PROCESS_STATE_RUNNING,
        RuntimeStateValue.EXITED: agent_pb2.RUNTIME_PROCESS_STATE_EXITED,
        RuntimeStateValue.NOT_FOUND: agent_pb2.RUNTIME_PROCESS_STATE_NOT_FOUND,
    }
)
_FAILED_PRECONDITION_RUNTIME_ERRORS: Final[frozenset[str]] = frozenset(
    {
        "runtime_config_conflict",
        "runtime_configuration_unavailable",
        "runtime_executable_unavailable",
        "runtime_identity_unavailable",
        "runtime_ownership_conflict",
        "runtime_remove_unsupported",
        "runtime_state_conflict",
    }
)
_GPU_UNKNOWN_REASON: Final[Mapping[GpuObservationUnknownReason, int]] = MappingProxyType(
    {
        GpuObservationUnknownReason.NOT_COLLECTED: (
            agent_pb2.OBSERVATION_UNKNOWN_REASON_NOT_COLLECTED
        ),
        GpuObservationUnknownReason.SOURCE_UNAVAILABLE: (
            agent_pb2.OBSERVATION_UNKNOWN_REASON_SOURCE_UNAVAILABLE
        ),
        GpuObservationUnknownReason.QUERY_FAILED: (
            agent_pb2.OBSERVATION_UNKNOWN_REASON_QUERY_FAILED
        ),
    }
)

# The wire vocabulary is a projection of the judgement's own three states; there
# is no member here that the judgement cannot produce and none it can produce
# without a member, so a new conclusion cannot be silently rendered as an old one.
_READABILITY_VALUE: Final[Mapping[RuntimeReadabilityConclusion, int]] = MappingProxyType(
    {
        RuntimeReadabilityConclusion.READABLE: agent_pb2.RUNTIME_READABILITY_READABLE,
        RuntimeReadabilityConclusion.NOT_READABLE: agent_pb2.RUNTIME_READABILITY_NOT_READABLE,
    }
)
_READABILITY_UNKNOWN_REASON: Final[Mapping[RuntimeReadabilityUnknownReason, int]] = (
    MappingProxyType(
        {
            RuntimeReadabilityUnknownReason.RUNTIME_IDENTITY_UNAVAILABLE: (
                agent_pb2.RUNTIME_READABILITY_UNKNOWN_REASON_RUNTIME_IDENTITY_UNAVAILABLE
            ),
            RuntimeReadabilityUnknownReason.ACL_PRESENT: (
                agent_pb2.RUNTIME_READABILITY_UNKNOWN_REASON_ACL_PRESENT
            ),
        }
    )
)


def readability_to_proto(
    judgement: RuntimeReadabilityJudgement,
) -> agent_pb2.ValidatePathResponse:
    """Project one judgement onto the service's existing known/unknown shape.

    An unknown carries its reason and *no* value, exactly as ``ObservedString``
    does, so a client cannot read a default-valued field as an answer.
    """

    observed = agent_pb2.ObservedRuntimeReadability()
    if judgement.conclusion is RuntimeReadabilityConclusion.UNKNOWN:
        assert judgement.unknown_reason is not None
        observed.state = agent_pb2.KNOWLEDGE_STATE_UNKNOWN
        observed.unknown_reason = _READABILITY_UNKNOWN_REASON[judgement.unknown_reason]
    else:
        observed.state = agent_pb2.KNOWLEDGE_STATE_KNOWN
        observed.value = _READABILITY_VALUE[judgement.conclusion]
    return agent_pb2.ValidatePathResponse(runtime_readability=observed)


class AgentAlreadyRunningError(RuntimeError):
    """Raised when the socket's live instance lock cannot be acquired."""


def _set_socket_permissions(socket_path: Path) -> None:
    os.chmod(socket_path, AGENT_SOCKET_MODE)
    actual_mode = stat.S_IMODE(socket_path.stat().st_mode)
    if actual_mode != AGENT_SOCKET_MODE:
        raise RuntimeError(
            f"Host Agent socket mode mismatch: expected {AGENT_SOCKET_MODE:o}, "
            f"found {actual_mode:o}"
        )


class SocketLease:
    """Own the per-socket process lock and safely recover only stale sockets."""

    def __init__(self, socket_path: Path) -> None:
        self.socket_path = socket_path
        self.lock_path = socket_path.with_name(f"{socket_path.name}.lock")
        self._lock_fd: int | None = None

    def acquire(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        lock_fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o660)
        os.fchmod(lock_fd, AGENT_SOCKET_MODE)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            os.close(lock_fd)
            raise AgentAlreadyRunningError(f"another Host Agent owns {self.lock_path}") from error

        try:
            self._recover_stale_socket()
        except BaseException:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
            raise
        self._lock_fd = lock_fd

    def _recover_stale_socket(self) -> None:
        try:
            socket_stat = self.socket_path.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISSOCK(socket_stat.st_mode):
            raise RuntimeError(f"refusing to replace non-socket path: {self.socket_path}")

        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(0.1)
        try:
            probe.connect(str(self.socket_path))
        except OSError as error:
            if error.errno not in {errno.ECONNREFUSED, errno.ENOENT}:
                raise RuntimeError(f"cannot establish whether socket is stale: {error}") from error
        else:
            raise AgentAlreadyRunningError(f"a process is listening on {self.socket_path}")
        finally:
            probe.close()
        self.socket_path.unlink(missing_ok=True)

    def release(self) -> None:
        if self._lock_fd is None:
            return
        fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
        os.close(self._lock_fd)
        self._lock_fd = None

    def __enter__(self) -> Self:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()


def _runtime_observation_to_proto(
    observation: RuntimeObservation,
) -> agent_pb2.InspectRuntimeResponse:
    response = agent_pb2.InspectRuntimeResponse(
        status=_RUNTIME_STATE_VALUE[observation.state],
        endpoint=observation.endpoint,
        started_at=observation.started_at,
        gpu_uuids=_observed_gpu_uuids_to_proto(observation.gpu_uuids),
    )
    if observation.exit_code is not None:
        response.exit_code = observation.exit_code
    if observation.oom_killed is not None:
        response.oom_killed = observation.oom_killed
    return response


def _observed_gpu_uuids_to_proto(observation: ObservedGpuUuids) -> agent_pb2.ObservedGpuUuids:
    if observation.state is ObservationKnowledgeState.KNOWN:
        return agent_pb2.ObservedGpuUuids(
            state=agent_pb2.KNOWLEDGE_STATE_KNOWN,
            values=observation.values,
        )
    assert observation.unknown_reason is not None
    return agent_pb2.ObservedGpuUuids(
        state=agent_pb2.KNOWLEDGE_STATE_UNKNOWN,
        unknown_reason=_GPU_UNKNOWN_REASON[observation.unknown_reason],
    )


async def _abort_runtime_error(
    context: aio.ServicerContext,
    error: RuntimeRequestError,
) -> None:
    if error.code == "runtime_stop_failed":
        status = grpc.StatusCode.DEADLINE_EXCEEDED
    elif error.code in _FAILED_PRECONDITION_RUNTIME_ERRORS:
        status = grpc.StatusCode.FAILED_PRECONDITION
    elif error.code in {"runtime_start_failed", "runtime_state_unavailable"}:
        status = grpc.StatusCode.INTERNAL
    else:
        status = grpc.StatusCode.INVALID_ARGUMENT
    await context.abort(status, error.code)


class HostAgentService:
    """Read-only host methods and the process Runtime lifecycle boundary."""

    def __init__(
        self,
        nvml_factory: Callable[[], NvmlLibrary] = PynvmlLibrary,
        *,
        external_path_policy: ExternalPathPolicy | None = None,
        runtime_identity: RuntimeIdentity | None = None,
        runtime_manager: RuntimeManager | None = None,
        runtime_state_root: Path = DEFAULT_RUNTIME_STATE_ROOT,
        max_log_line_characters: int = DEFAULT_MAX_LOG_LINE_CHARACTERS,
    ) -> None:
        if not 1 <= max_log_line_characters <= MAX_LOG_LINE_CHARACTERS:
            raise ValueError("max_log_line_characters exceeds the hard limit")
        self._nvml_factory = nvml_factory
        # Both are deliberately allowed to be absent, and absence is answered
        # explicitly rather than defaulted: no configured root refuses every
        # path, and no Manifest Runtime identity answers unknown.
        self._external_path_policy = external_path_policy
        self._runtime_identity = runtime_identity
        self._runtime_manager = runtime_manager or RuntimeManager(
            compatibility_path=DEFAULT_RUNTIME_COMPAT_PATH,
            state_root=runtime_state_root,
            model_path_policy=external_path_policy,
            runtime_identity=runtime_identity,
            nvml_factory=nvml_factory,
        )
        self._runtime_state_root = runtime_state_root
        self._max_log_line_characters = max_log_line_characters

    async def GetHostInfo(
        self,
        request: agent_pb2.GetHostInfoRequest,
        context: aio.ServicerContext,
    ) -> agent_pb2.GetHostInfoResponse:
        del request, context
        return await asyncio.to_thread(get_host_info, self._nvml_factory())

    async def ListGPUs(
        self, request: agent_pb2.ListGPUsRequest, context: aio.ServicerContext
    ) -> agent_pb2.ListGPUsResponse:
        del request, context
        inventory = await asyncio.to_thread(discover_gpus, self._nvml_factory())
        return inventory_to_proto(inventory)

    async def EnsureRuntime(
        self, request: agent_pb2.EnsureRuntimeRequest, context: aio.ServicerContext
    ) -> agent_pb2.EnsureRuntimeResponse:
        try:
            observation = await self._runtime_manager.ensure(
                RuntimeLaunchSpec(
                    instance_id=request.instance_id,
                    runtime_name=request.runtime_name,
                    model_path=request.model_path,
                    gpu_uuids=tuple(request.gpu_uuids),
                    tensor_parallel_size=request.tensor_parallel_size,
                    context_length=request.context_length,
                    gpu_memory_utilization=request.gpu_memory_utilization,
                    served_model_name=request.served_model_name,
                    config_version=request.config_version,
                )
            )
        except RuntimeRequestError as error:
            await _abort_runtime_error(context, error)
            raise AssertionError("context.abort must raise") from error
        return agent_pb2.EnsureRuntimeResponse(
            status=_RUNTIME_STATE_VALUE[observation.state],
            endpoint=observation.endpoint,
        )

    async def StopRuntime(
        self, request: agent_pb2.StopRuntimeRequest, context: aio.ServicerContext
    ) -> agent_pb2.StopRuntimeResponse:
        try:
            observation = await self._runtime_manager.stop(
                request.instance_id,
                request.grace_seconds,
            )
        except RuntimeRequestError as error:
            await _abort_runtime_error(context, error)
            raise AssertionError("context.abort must raise") from error
        return agent_pb2.StopRuntimeResponse(status=_RUNTIME_STATE_VALUE[observation.state])

    async def RemoveRuntime(
        self, request: agent_pb2.RemoveRuntimeRequest, context: aio.ServicerContext
    ) -> agent_pb2.RemoveRuntimeResponse:
        try:
            await self._runtime_manager.remove(request.instance_id)
        except RuntimeRequestError as error:
            await _abort_runtime_error(context, error)
            raise AssertionError("context.abort must raise") from error
        return agent_pb2.RemoveRuntimeResponse()

    async def InspectRuntime(
        self, request: agent_pb2.InspectRuntimeRequest, context: aio.ServicerContext
    ) -> agent_pb2.InspectRuntimeResponse:
        try:
            observation = await self._runtime_manager.inspect(request.instance_id)
        except RuntimeRequestError as error:
            await _abort_runtime_error(context, error)
            raise AssertionError("context.abort must raise") from error
        return _runtime_observation_to_proto(observation)

    async def ListRuntimes(
        self, request: agent_pb2.ListRuntimesRequest, context: aio.ServicerContext
    ) -> agent_pb2.ListRuntimesResponse:
        del request
        try:
            summaries = await self._runtime_manager.list()
        except RuntimeRequestError as error:
            await _abort_runtime_error(context, error)
            raise AssertionError("context.abort must raise") from error
        return agent_pb2.ListRuntimesResponse(
            runtimes=(
                agent_pb2.RuntimeSummary(
                    instance_id=summary.instance_id,
                    status=_RUNTIME_STATE_VALUE[summary.state],
                    gpu_uuids=_observed_gpu_uuids_to_proto(summary.gpu_uuids),
                )
                for summary in summaries
            )
        )

    async def StreamLogs(
        self, request: agent_pb2.StreamLogsRequest, context: aio.ServicerContext
    ) -> AsyncIterator[agent_pb2.StreamLogsResponse]:
        try:
            page = await self._runtime_manager.container_log_page(
                request.instance_id,
                cursor_offset=request.cursor_offset,
                page_size=request.page_size,
                max_log_line_characters=self._max_log_line_characters,
            )
            if page is None:
                page = await asyncio.to_thread(
                    read_runtime_log_page,
                    self._runtime_state_root,
                    request.instance_id,
                    cursor_offset=request.cursor_offset,
                    page_size=request.page_size,
                    max_log_line_characters=self._max_log_line_characters,
                )
        except RuntimeLogRequestError as error:
            status = (
                grpc.StatusCode.INTERNAL
                if error.code == "runtime_log_unavailable"
                else grpc.StatusCode.INVALID_ARGUMENT
            )
            await context.abort(status, error.code)
            raise AssertionError("context.abort must raise") from error
        yield agent_pb2.StreamLogsResponse(
            entries=(
                agent_pb2.LogEntry(
                    sequence=entry.sequence,
                    text=entry.text,
                    truncated=entry.truncated,
                    original_character_count=entry.original_character_count,
                )
                for entry in page.entries
            ),
            next_cursor_offset=page.next_cursor_offset,
            has_more=page.has_more,
        )

    async def ValidatePath(
        self, request: agent_pb2.ValidatePathRequest, context: aio.ServicerContext
    ) -> agent_pb2.ValidatePathResponse:
        """Answer whether the Runtime identity can read one path.

        The request names a path and nothing else.  The Runtime identity comes
        from the Release Manifest and the allowed roots from this process's own
        configuration, so a caller cannot choose the identity that decides its
        answer.  The filesystem calls block, so they run in a worker thread.
        """

        del context
        judgement = await asyncio.to_thread(
            judge_runtime_readability,
            request.path,
            policy=self._external_path_policy,
            identity=self._runtime_identity,
        )
        return readability_to_proto(judgement)


class HostAgentServer:
    """Own the kernel-authenticated UDS bridge and per-peer gRPC lifecycle."""

    def __init__(
        self,
        socket_path: Path = AGENT_SOCKET_PATH,
        *,
        release_manifest_path: Path = DEFAULT_RELEASE_MANIFEST_PATH,
        identity_catalog: IdentityCatalog | None = None,
        runtime_identity: RuntimeIdentity | None = None,
        external_model_roots: Sequence[Path] = (),
        model_roots: Sequence[Path] = (),
        runtime_compatibility_path: Path = DEFAULT_RUNTIME_COMPAT_PATH,
        runtime_state_root: Path = DEFAULT_RUNTIME_STATE_ROOT,
        auditor: DenialAuditor | None = None,
        max_peer_connections: int = DEFAULT_MAX_PEER_CONNECTIONS,
        nvml_factory: Callable[[], NvmlLibrary] = PynvmlLibrary,
    ) -> None:
        if max_peer_connections < 1:
            raise ValueError("max_peer_connections must be positive")
        self.socket_path = socket_path
        self.release_manifest_path = release_manifest_path
        self._lease = SocketLease(socket_path)
        self._identity_catalog_override = identity_catalog
        self._identity_catalog = IdentityCatalog.unavailable()
        self._runtime_identity_override = runtime_identity
        self._runtime_identity: RuntimeIdentity | None = None
        # An empty configured set is not an empty policy object: ``ExternalPathPolicy``
        # refuses to exist without a root, so "nothing configured" stays a
        # distinct ``None`` and can never be read as "every root allowed".
        self._external_path_policy = (
            ExternalPathPolicy.for_configured_roots(external_model_roots)
            if external_model_roots
            else None
        )
        runtime_model_roots = (*model_roots, *external_model_roots)
        self._runtime_model_path_policy = (
            ExternalPathPolicy.for_configured_roots(runtime_model_roots)
            if runtime_model_roots
            else None
        )
        self._runtime_compatibility_path = runtime_compatibility_path
        self._runtime_state_root = runtime_state_root
        self._runtime_manager: RuntimeManager | None = None
        self._auditor = auditor or StructuredDenialAuditor()
        self._max_peer_connections = max_peer_connections
        self._nvml_factory = nvml_factory
        self._server: asyncio.Server | None = None
        self._connection_tasks: set[asyncio.Task[None]] = set()
        self._backend_root: Path | None = None
        self._next_connection_id = 0
        self._accepting = False
        self._terminated = asyncio.Event()

    async def start(self) -> None:
        if self._server is not None:
            raise RuntimeError("Host Agent server is already started")
        self._accepting = False
        self._lease.acquire()
        server: asyncio.Server | None = None
        try:
            self._identity_catalog = (
                self._identity_catalog_override
                if self._identity_catalog_override is not None
                else load_identity_catalog(self.release_manifest_path)
            )
            # The Manifest is the only source of the Runtime UID.  When it does
            # not name one the value stays ``None`` and ``ValidatePath`` answers
            # unknown; it never falls back to this process's own UID.
            self._runtime_identity = (
                self._runtime_identity_override
                if self._runtime_identity_override is not None
                else load_runtime_identity(self.release_manifest_path)
            )
            self._runtime_manager = RuntimeManager(
                compatibility_path=self._runtime_compatibility_path,
                state_root=self._runtime_state_root,
                model_path_policy=self._runtime_model_path_policy,
                runtime_identity=self._runtime_identity,
                nvml_factory=self._nvml_factory,
            )
            self._terminated = asyncio.Event()
            self._backend_root = Path(tempfile.mkdtemp(prefix="holm-agent-backends-"))
            os.chmod(self._backend_root, 0o700)
            previous_umask = os.umask(_SOCKET_CREATION_UMASK)
            try:
                server = await asyncio.start_unix_server(
                    self._accept_connection,
                    path=self.socket_path,
                )
                _set_socket_permissions(self.socket_path)
            finally:
                os.umask(previous_umask)
        except BaseException:
            if server is not None:
                server.close()
                await server.wait_closed()
            self.socket_path.unlink(missing_ok=True)
            self._remove_backend_root()
            self._lease.release()
            raise
        assert server is not None
        self._server = server
        self._accepting = True

    async def stop(self, grace: float = 5.0) -> None:
        del grace
        public_server = self._server
        if public_server is None:
            return
        self._accepting = False
        try:
            public_server.close()
            tasks = tuple(self._connection_tasks)
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            await public_server.wait_closed()
        finally:
            try:
                self.socket_path.unlink(missing_ok=True)
                self._remove_backend_root()
            finally:
                self._lease.release()
                self._server = None
                self._terminated.set()

    async def wait_for_termination(self) -> None:
        if self._server is None:
            raise RuntimeError("Host Agent server is not started")
        await self._terminated.wait()

    def _accept_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        if not self._accepting or len(self._connection_tasks) >= self._max_peer_connections:
            writer.close()
            return
        task = asyncio.create_task(self._serve_connection(reader, writer))
        self._connection_tasks.add(task)
        task.add_done_callback(self._connection_tasks.discard)

    async def _serve_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        peer_socket = writer.get_extra_info("socket")
        if peer_socket is None:
            writer.close()
            await writer.wait_closed()
            return
        try:
            peer_uid = _read_peer_uid(peer_socket)
        except OSError:
            writer.close()
            await writer.wait_closed()
            return
        backend_root = self._backend_root
        runtime_manager = self._runtime_manager
        if backend_root is None or runtime_manager is None:
            writer.close()
            await writer.wait_closed()
            return
        self._next_connection_id += 1
        backend_path = backend_root / f"{self._next_connection_id}.sock"
        backend = aio.server(
            interceptors=(
                AuthorizationInterceptor(
                    peer_uid=peer_uid,
                    identities=self._identity_catalog,
                    auditor=self._auditor,
                ),
            )
        )
        agent_pb2_grpc.add_HostAgentServicer_to_server(
            HostAgentService(
                self._nvml_factory,
                external_path_policy=self._external_path_policy,
                runtime_identity=self._runtime_identity,
                runtime_manager=runtime_manager,
                runtime_state_root=self._runtime_state_root,
            ),
            backend,
        )
        backend_writer: asyncio.StreamWriter | None = None
        try:
            bound = backend.add_insecure_port(f"unix:{backend_path}")
            if bound == 0:
                raise RuntimeError("gRPC could not bind a private peer backend")
            await backend.start()
            backend_reader, backend_writer = await asyncio.open_unix_connection(backend_path)
            relays = {
                asyncio.create_task(_relay(reader, backend_writer)),
                asyncio.create_task(_relay(backend_reader, writer)),
            }
            _done, pending = await asyncio.wait(
                relays,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*relays, return_exceptions=True)
        finally:
            writer.close()
            if backend_writer is not None:
                backend_writer.close()
            await asyncio.gather(
                writer.wait_closed(),
                *(() if backend_writer is None else (backend_writer.wait_closed(),)),
                return_exceptions=True,
            )
            await backend.stop(grace=None)
            backend_path.unlink(missing_ok=True)

    def _remove_backend_root(self) -> None:
        backend_root = self._backend_root
        if backend_root is None:
            return
        backend_root.rmdir()
        self._backend_root = None


async def _relay(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    while data := await reader.read(_RELAY_BUFFER_BYTES):
        writer.write(data)
        await writer.drain()


def _read_peer_uid(peer_socket: socket.socket) -> int:
    """Read the UID attached by the kernel to one accepted Unix connection."""
    credentials = peer_socket.getsockopt(
        socket.SOL_SOCKET,
        socket.SO_PEERCRED,
        struct.calcsize(_PEER_CREDENTIAL_FORMAT),
    )
    _pid, peer_uid, _gid = struct.unpack(_PEER_CREDENTIAL_FORMAT, credentials)
    return int(peer_uid)


async def serve_until_cancelled(
    socket_path: Path = AGENT_SOCKET_PATH,
    release_manifest_path: Path = DEFAULT_RELEASE_MANIFEST_PATH,
    external_model_roots: Sequence[Path] = (),
    model_roots: Sequence[Path] = (),
    runtime_compatibility_path: Path = DEFAULT_RUNTIME_COMPAT_PATH,
    runtime_state_root: Path = DEFAULT_RUNTIME_STATE_ROOT,
) -> None:
    server = HostAgentServer(
        socket_path,
        release_manifest_path=release_manifest_path,
        external_model_roots=external_model_roots,
        model_roots=model_roots,
        runtime_compatibility_path=runtime_compatibility_path,
        runtime_state_root=runtime_state_root,
    )
    await server.start()
    try:
        await server.wait_for_termination()
    except asyncio.CancelledError:
        await server.stop()
        raise
