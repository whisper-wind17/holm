"""Asynchronous Model Instance lifecycle reconciliation."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Final
from uuid import UUID

from holm_domain import Component, ModelInstanceStatus
from holm_domain.instance_cleanup import CleanupFailure
from holm_domain.launch_verification import LaunchVerification, LaunchVerificationResult
from holm_persistence import ControllerModelInstanceRecord
from holm_runtimes import RuntimeCompatibility, RuntimeCompatibilityCatalog
from sqlalchemy.exc import DBAPIError, DisconnectionError
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError

from holm_instance_controller.agent_client import (
    ObservationKnowledgeState,
    RuntimeAgentClient,
    RuntimeAgentError,
    RuntimeLaunchRequest,
    RuntimeObservation,
    RuntimeProcessState,
    RuntimeSummary,
)
from holm_instance_controller.health import RuntimeHealthProbe
from holm_instance_controller.heartbeat import ControllerHeartbeat
from holm_instance_controller.store import ControllerStore

_RUNTIME_ENDPOINT: Final[re.Pattern[str]] = re.compile(
    r"(127\.0\.0\.1|runtime-[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}):([0-9]{1,5})"
)
_ACTIVE_STATUSES: Final[tuple[ModelInstanceStatus, ...]] = (
    ModelInstanceStatus.STARTING,
    ModelInstanceStatus.STOPPING,
    ModelInstanceStatus.RUNNING,
    ModelInstanceStatus.UNHEALTHY,
    ModelInstanceStatus.RESTARTING,
)
_TERMINAL_STATUSES: Final[frozenset[ModelInstanceStatus]] = frozenset(
    {ModelInstanceStatus.STOPPED, ModelInstanceStatus.FAILED}
)
_GPU_RECONCILIATION_STATUSES: Final[frozenset[ModelInstanceStatus]] = frozenset(
    {
        ModelInstanceStatus.STARTING,
        ModelInstanceStatus.RUNNING,
        ModelInstanceStatus.RESTARTING,
    }
)
_RESTART_BACKOFF_SECONDS: Final[tuple[float, ...]] = (5.0, 15.0, 45.0)
_DATABASE_RETRY_BACKOFF_SECONDS: Final[tuple[float, ...]] = (1.0, 2.0, 4.0, 5.0)
DEFAULT_STOP_DRAIN_SECONDS: Final[float] = 30.0
DEFAULT_STOP_GRACE_SECONDS: Final[int] = 60
_DATABASE_ERRORS: Final[tuple[type[BaseException], ...]] = (
    DBAPIError,
    DisconnectionError,
    SQLAlchemyTimeoutError,
    OSError,
)
_LOGGER: Final[logging.Logger] = logging.getLogger("holm.instance_controller")
_RUNTIME_NOT_CERTIFIED: Final[str] = "runtime_not_certified"
_RUNTIME_OUT_OF_MEMORY: Final[str] = "runtime_out_of_memory"


def _trusted_endpoint(value: str, instance_id: UUID) -> str | None:
    match = _RUNTIME_ENDPOINT.fullmatch(value)
    if match is None or match.group(1) not in {"127.0.0.1", f"runtime-{instance_id}"}:
        return None
    port = int(match.group(2))
    return value if 0 < port <= 65535 else None


def _runtime_exit_error(observation: RuntimeObservation, fallback: str) -> str:
    if observation.state is RuntimeProcessState.EXITED and observation.oom_killed is True:
        return _RUNTIME_OUT_OF_MEMORY
    return fallback


class InstanceController:
    """Drive each active instance independently so one long startup cannot block others."""

    _runtime_lookup: Callable[[str], RuntimeCompatibility | None]

    __slots__ = (
        "_agent",
        "_cleanup_cursor",
        "_dependency_timeout_seconds",
        "_dispatch_seconds",
        "_health",
        "_heartbeat",
        "_health_seconds",
        "_initial_reconciliation_window_seconds",
        "_monotonic",
        "_reconciliation_seconds",
        "_restart_window_seconds",
        "_runtime_lookup",
        "_sleep",
        "_start_poll_seconds",
        "_start_timeout_seconds",
        "_stop_drain_seconds",
        "_stop_grace_seconds",
        "_store",
        "_tasks",
    )

    def __init__(
        self,
        *,
        store: ControllerStore,
        agent: RuntimeAgentClient,
        health: RuntimeHealthProbe,
        heartbeat: ControllerHeartbeat | None = None,
        runtime_catalog: RuntimeCompatibilityCatalog | None = None,
        runtime: RuntimeCompatibility | None = None,
        start_timeout_seconds: float = 30 * 60,
        start_poll_seconds: float = 1.0,
        health_seconds: float = 5.0,
        dispatch_seconds: float = 0.25,
        stop_drain_seconds: float = DEFAULT_STOP_DRAIN_SECONDS,
        stop_grace_seconds: int = DEFAULT_STOP_GRACE_SECONDS,
        reconciliation_seconds: float = 30.0,
        dependency_timeout_seconds: float = 5.0,
        initial_reconciliation_window_seconds: float = 60.0,
        restart_window_seconds: int = 10 * 60,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if (
            min(
                start_timeout_seconds,
                start_poll_seconds,
                health_seconds,
                dispatch_seconds,
                stop_drain_seconds,
                float(stop_grace_seconds),
                reconciliation_seconds,
                dependency_timeout_seconds,
                initial_reconciliation_window_seconds,
                float(restart_window_seconds),
            )
            <= 0
        ):
            raise ValueError("controller timing values must be positive")
        if (runtime_catalog is None) == (runtime is None):
            raise ValueError("provide exactly one Runtime catalog or compatibility entry")
        if runtime_catalog is not None:
            self._runtime_lookup = runtime_catalog.runtime
        else:
            assert runtime is not None

            def lookup_single_runtime(name: str) -> RuntimeCompatibility | None:
                return runtime if runtime.name == name else None

            self._runtime_lookup = lookup_single_runtime
        self._store = store
        self._agent = agent
        self._cleanup_cursor: UUID | None = None
        self._health = health
        self._heartbeat = heartbeat
        self._start_timeout_seconds = start_timeout_seconds
        self._start_poll_seconds = start_poll_seconds
        self._health_seconds = health_seconds
        self._dispatch_seconds = dispatch_seconds
        self._stop_drain_seconds = stop_drain_seconds
        self._stop_grace_seconds = stop_grace_seconds
        self._reconciliation_seconds = reconciliation_seconds
        self._dependency_timeout_seconds = dependency_timeout_seconds
        self._initial_reconciliation_window_seconds = initial_reconciliation_window_seconds
        self._restart_window_seconds = restart_window_seconds
        self._sleep = sleep
        self._monotonic = monotonic
        self._tasks: dict[UUID, asyncio.Task[None]] = {}

    async def run(self, stop_event: asyncio.Event) -> None:
        heartbeat = (
            None
            if self._heartbeat is None
            else asyncio.create_task(self._heartbeat.run(stop_event))
        )
        try:
            await self._run_control_loop(stop_event)
        finally:
            if heartbeat is not None:
                # Let the bounded final write finish, including when the control loop fails.
                stop_event.set()
                await heartbeat

    async def _run_control_loop(self, stop_event: asyncio.Event) -> None:
        await self._initial_reconciliation(stop_event)
        if stop_event.is_set():
            return
        reconciliation = asyncio.create_task(self._periodic_reconciliation(stop_event))
        database_failure_count = 0
        try:
            while not stop_event.is_set():
                self._harvest()
                try:
                    await self.dispatch_once()
                except _DATABASE_ERRORS:
                    self._log_recovery_failure("database_unavailable")
                    delay = _DATABASE_RETRY_BACKOFF_SECONDS[
                        min(database_failure_count, len(_DATABASE_RETRY_BACKOFF_SECONDS) - 1)
                    ]
                    database_failure_count += 1
                    if await self._wait_or_stop(stop_event, delay):
                        break
                    continue
                database_failure_count = 0
                if stop_event.is_set():
                    break
                if await self._wait_or_stop(stop_event, self._dispatch_seconds):
                    break
        finally:
            reconciliation.cancel()
            await asyncio.gather(reconciliation, return_exceptions=True)
            tasks = tuple(self._tasks.values())
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            self._tasks.clear()

    async def recover_once(self) -> None:
        """Converge one active snapshot while locking only each affected instance."""

        # A failed listing must not leave cleanup running into the next retry.
        results = await asyncio.gather(
            self._cleanup_pass(), self._recover_instances_once(), return_exceptions=True
        )
        for result in results:
            if isinstance(result, BaseException):
                raise result

    async def _cleanup_pass(self) -> None:
        try:
            await self.cleanup_once()
        except TimeoutError:
            self._log_recovery_failure("cleanup_dependency_timeout")
        except _DATABASE_ERRORS:
            self._log_recovery_failure("cleanup_database_unavailable")

    async def _recover_instances_once(self) -> None:
        items_task = asyncio.create_task(self._store.list_for_reconciliation())
        release_task = asyncio.create_task(self._store.release_terminal_allocations())
        runtimes_task = asyncio.create_task(self._agent.list())
        gpu_inventory_task = asyncio.create_task(self._list_gpu_inventory_or_unknown())
        try:
            items, _, runtimes, inventory_gpu_uuids = await asyncio.gather(
                asyncio.wait_for(items_task, timeout=self._dependency_timeout_seconds),
                asyncio.wait_for(release_task, timeout=self._dependency_timeout_seconds),
                asyncio.wait_for(runtimes_task, timeout=self._dependency_timeout_seconds),
                gpu_inventory_task,
            )
        except RuntimeAgentError as error:
            raise RuntimeError("runtime listing failed") from error
        active_ids = {item.instance.id for item in items}
        orphan_candidates = tuple(
            runtime
            for runtime in runtimes
            if runtime.state is RuntimeProcessState.RUNNING
            and runtime.instance_id not in active_ids
        )
        results = await asyncio.gather(
            *(self._reconcile_item(item, inventory_gpu_uuids) for item in items),
            *(self._reconcile_orphan(runtime) for runtime in orphan_candidates),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, _DATABASE_ERRORS):
                raise result
            if isinstance(result, BaseException):
                # Any other per-item failure keeps the existing contract: surface it as a
                # reconciliation failure so the periodic loop logs it and runs the next cycle.
                raise RuntimeError("instance reconciliation dependency failed") from result

    async def cleanup_once(self) -> None:
        """Round-robin eight durable requests with two concurrent bounded RPCs.

        The cursor only provides fairness. Restarting it loses no request because
        unfinished work remains in the database until explicit completion.
        """
        async with asyncio.timeout(self._dependency_timeout_seconds):
            ids = await self._store.pending_cleanup(self._cleanup_cursor)
            if not ids and self._cleanup_cursor is not None:
                ids = await self._store.pending_cleanup(None)
        self._cleanup_cursor = ids[-1] if ids else None
        # Each lifecycle lock holds one DB connection while store calls use a
        # second. Keep capacity for normal instance reconciliation and peers.
        semaphore = asyncio.Semaphore(2)

        async def process(instance_id: UUID) -> None:
            async with semaphore:
                await self._bounded_cleanup_item(instance_id)

        await asyncio.gather(*(process(instance_id) for instance_id in ids))

    async def _bounded_cleanup_item(self, instance_id: UUID) -> None:
        try:
            async with asyncio.timeout(self._dependency_timeout_seconds * 2):
                await self._cleanup_item(instance_id)
        except TimeoutError:
            self._log_recovery_failure("cleanup_dependency_timeout")
        except _DATABASE_ERRORS:
            self._log_recovery_failure("cleanup_database_unavailable")

    async def _cleanup_item(self, instance_id: UUID) -> None:
        async with self._store.lifecycle_lock(instance_id):
            failure: CleanupFailure | None = None
            if await self._store.status(instance_id) is not None:
                failure = CleanupFailure.INSTANCE_EXISTS
            else:
                try:
                    async with asyncio.timeout(self._dependency_timeout_seconds):
                        await self._agent.remove(str(instance_id))
                except RuntimeAgentError as error:
                    try:
                        failure = CleanupFailure(error.code)
                    except ValueError:
                        failure = CleanupFailure.INVALID_RESPONSE
                except TimeoutError:
                    failure = CleanupFailure.AGENT_UNAVAILABLE
            await self._store.cleanup_result(instance_id, failure)

    async def _list_gpu_inventory_or_unknown(self) -> frozenset[str] | None:
        try:
            return await asyncio.wait_for(
                self._agent.list_gpus(),
                timeout=self._dependency_timeout_seconds,
            )
        except (RuntimeAgentError, TimeoutError):
            return None

    async def _reconcile_item(
        self,
        item: ControllerModelInstanceRecord,
        inventory_gpu_uuids: frozenset[str] | None = None,
    ) -> None:
        instance_id = item.instance.id
        status = item.instance.status
        async with asyncio.timeout(self._dependency_timeout_seconds):
            async with self._store.lifecycle_lock(instance_id):
                if status is ModelInstanceStatus.UNHEALTHY:
                    self._track(item)
                    return
                if (
                    status in _GPU_RECONCILIATION_STATUSES
                    and inventory_gpu_uuids is not None
                    and not frozenset(item.active_gpu_uuids).issubset(inventory_gpu_uuids)
                ):
                    await self._store.mark_unhealthy(
                        instance_id,
                        expected_status=status,
                        error_code="gpu_device_missing",
                    )
                    return
                try:
                    observation = await self._agent.inspect(str(instance_id))
                except RuntimeAgentError as error:
                    raise RuntimeError("runtime inspection failed") from error
                if inventory_gpu_uuids is not None and await self._isolate_gpu_mismatch(
                    item, observation
                ):
                    return
                if status is ModelInstanceStatus.RESTARTING:
                    self._track(item)
                    return
                if status is ModelInstanceStatus.STARTING:
                    if observation.state is RuntimeProcessState.RUNNING:
                        endpoint = _trusted_endpoint(observation.endpoint, item.instance.id)
                        if endpoint is not None:
                            await self._store.set_endpoint(
                                instance_id,
                                expected_status=status,
                                endpoint=endpoint,
                            )
                        self._track(item)
                    elif (
                        observation.state is RuntimeProcessState.NOT_FOUND
                        and (verification := await self._store.launch_verification(instance_id))
                        is not None
                        and not verification.authorized
                    ):
                        self._track(item)
                    else:
                        await self._store.mark_unhealthy(
                            instance_id,
                            expected_status=status,
                            error_code=_runtime_exit_error(
                                observation,
                                "runtime_start_failed",
                            ),
                        )
                    return
                if status is ModelInstanceStatus.STOPPING:
                    if observation.state is RuntimeProcessState.RUNNING:
                        self._track(item)
                    else:
                        await self._store.mark_stopped(instance_id)
                    return
                if observation.state is RuntimeProcessState.RUNNING:
                    endpoint = _trusted_endpoint(observation.endpoint, item.instance.id)
                    if endpoint is not None:
                        await self._store.set_endpoint(
                            instance_id,
                            expected_status=status,
                            endpoint=endpoint,
                        )
                    self._track(item)
                    return
                await self._store.mark_unhealthy(
                    instance_id,
                    expected_status=ModelInstanceStatus.RUNNING,
                    error_code=_runtime_exit_error(observation, "runtime_exited"),
                )

    async def _reconcile_orphan(self, runtime: RuntimeSummary) -> None:
        async with asyncio.timeout(self._dependency_timeout_seconds):
            async with self._store.lifecycle_lock(runtime.instance_id):
                status = await self._store.status(runtime.instance_id)
                if status is not None and status not in _TERMINAL_STATUSES:
                    return
                if not await self._stop_runtime(runtime.instance_id):
                    raise RuntimeError("orphan Runtime stop failed")
                await self._store.record_orphan_isolated(runtime.instance_id)

    async def _isolate_gpu_mismatch(
        self,
        item: ControllerModelInstanceRecord,
        observation: RuntimeObservation,
    ) -> bool:
        status = item.instance.status
        if (
            status not in _GPU_RECONCILIATION_STATUSES
            or observation.state is not RuntimeProcessState.RUNNING
            or observation.gpu_uuids.state is not ObservationKnowledgeState.KNOWN
            or frozenset(observation.gpu_uuids.values) == frozenset(item.active_gpu_uuids)
            # With the process-launcher deviation, a Runtime can exist before
            # CUDA registers any process in NVML. An empty observation is not
            # conflict evidence while the instance is still becoming ready.
            or (
                status in (ModelInstanceStatus.STARTING, ModelInstanceStatus.RESTARTING)
                and not observation.gpu_uuids.values
            )
        ):
            return False
        if not await self._stop_runtime(item.instance.id):
            raise RuntimeError("GPU-mismatched Runtime stop failed")
        await self._store.mark_failed(
            item.instance.id,
            expected_status=status,
            error_code="runtime_gpu_mismatch",
        )
        return True

    async def _initial_reconciliation(self, stop_event: asyncio.Event | None = None) -> None:
        attempt = 0
        while stop_event is None or not stop_event.is_set():
            try:
                await self.recover_once()
                return
            except TimeoutError:
                self._log_recovery_failure("reconciliation_failed")
            except _DATABASE_ERRORS:
                self._log_recovery_failure("database_unavailable")
            except RuntimeError:
                self._log_recovery_failure("reconciliation_failed")
            delay = min(
                _DATABASE_RETRY_BACKOFF_SECONDS[
                    min(attempt, len(_DATABASE_RETRY_BACKOFF_SECONDS) - 1)
                ],
                self._initial_reconciliation_window_seconds,
            )
            if stop_event is None:
                await self._sleep(delay)
            elif await self._wait_or_stop(stop_event, delay):
                return
            attempt += 1

    async def _periodic_reconciliation(self, stop_event: asyncio.Event | None = None) -> None:
        delay = self._reconciliation_seconds
        database_failure_count = 0
        while stop_event is None or not stop_event.is_set():
            if stop_event is None:
                await self._sleep(delay)
            elif await self._wait_or_stop(stop_event, delay):
                return
            try:
                await self.recover_once()
            except TimeoutError:
                self._log_recovery_failure("reconciliation_failed")
                delay = self._reconciliation_seconds
                database_failure_count = 0
            except _DATABASE_ERRORS:
                self._log_recovery_failure("database_unavailable")
                delay = _DATABASE_RETRY_BACKOFF_SECONDS[
                    min(database_failure_count, len(_DATABASE_RETRY_BACKOFF_SECONDS) - 1)
                ]
                database_failure_count += 1
            except RuntimeError:
                self._log_recovery_failure("reconciliation_failed")
                delay = self._reconciliation_seconds
                database_failure_count = 0
            else:
                delay = self._reconciliation_seconds
                database_failure_count = 0

    async def _wait_or_stop(self, stop_event: asyncio.Event, delay: float) -> bool:
        if stop_event.is_set():
            return True

        async def sleep_once() -> None:
            await self._sleep(delay)

        sleeping = asyncio.create_task(sleep_once())
        stopping = asyncio.create_task(stop_event.wait())
        try:
            done, _ = await asyncio.wait({sleeping, stopping}, return_when=asyncio.FIRST_COMPLETED)
            return stopping in done and stopping.result()
        finally:
            for task in (sleeping, stopping):
                if not task.done():
                    task.cancel()
            await asyncio.gather(sleeping, stopping, return_exceptions=True)

    @staticmethod
    def _log_recovery_failure(error_code: str) -> None:
        _LOGGER.warning(
            "component=%s error_code=%s",
            Component.INSTANCE_CONTROLLER.value,
            error_code,
            extra={
                "component": Component.INSTANCE_CONTROLLER.value,
                "error_code": error_code,
            },
        )

    async def dispatch_once(self) -> None:
        excluded = tuple(self._tasks)
        for status in _ACTIVE_STATUSES:
            while item := await self._store.claim(status, excluded_ids=excluded):
                self._track(item)
                excluded = tuple(self._tasks)

    def _track(self, item: ControllerModelInstanceRecord) -> None:
        instance_id = item.instance.id
        if instance_id in self._tasks:
            return
        if item.instance.status is ModelInstanceStatus.STARTING:
            operation = self.process_starting(item)
        elif item.instance.status is ModelInstanceStatus.STOPPING:
            operation = self.process_stopping(item)
        elif item.instance.status is ModelInstanceStatus.RUNNING:
            operation = self.process_running(item)
        elif item.instance.status is ModelInstanceStatus.UNHEALTHY:
            operation = self.process_unhealthy(item)
        else:
            operation = self.process_restarting(item)
        self._tasks[instance_id] = asyncio.create_task(operation)

    def _harvest(self) -> None:
        for instance_id, task in tuple(self._tasks.items()):
            if not task.done():
                continue
            self._tasks.pop(instance_id)
            try:
                task.result()
            except asyncio.CancelledError:
                pass
            except _DATABASE_ERRORS:
                self._log_recovery_failure("database_unavailable")
            except Exception:  # noqa: BLE001 - one instance's failure must not stop the controller
                self._log_recovery_failure("instance_operation_failed")

    def _launch_request(self, item: ControllerModelInstanceRecord) -> RuntimeLaunchRequest | None:
        instance = item.instance
        runtime = self._runtime_lookup(instance.runtime_type.value)
        if runtime is None:
            return None
        expected = tuple(sorted(instance.expected_gpu_uuids))
        active = tuple(sorted(item.active_gpu_uuids))
        if (
            active != expected
            or len(active) != instance.tensor_parallel_size
            or len(set(active)) != len(active)
        ):
            return None
        return RuntimeLaunchRequest(
            instance_id=str(instance.id),
            runtime_name=runtime.name,
            model_path=item.artifact_path,
            gpu_uuids=active,
            tensor_parallel_size=instance.tensor_parallel_size,
            context_length=instance.context_length,
            gpu_memory_utilization=instance.gpu_memory_utilization,
            served_model_name=instance.internal_model_name,
            config_version=f"instance-{instance.config_version}",
        )

    async def process_starting(self, item: ControllerModelInstanceRecord) -> None:
        request = self._launch_request(item)
        if request is None:
            error_code = (
                _RUNTIME_NOT_CERTIFIED
                if self._runtime_lookup(item.instance.runtime_type.value) is None
                else "runtime_start_failed"
            )
            await self._fail_starting(item.instance.id, error_code)
            return
        try:
            verification = await self._store.launch_verification(item.instance.id)
        except ValueError:
            await self._fail_starting(item.instance.id, "artifact_verification_failed")
            return
        requested_at = (
            item.instance.updated_at if verification is None else verification.requested_at
        )
        elapsed = max(0.0, (datetime.now(UTC) - requested_at).total_seconds())
        deadline = self._monotonic() + max(0.0, self._start_timeout_seconds - elapsed)
        if self._monotonic() >= deadline:
            verification = await self._store.launch_verification(item.instance.id)
            code = (
                "runtime_start_timeout"
                if verification is not None and verification.authorized
                else "artifact_verification_timeout"
            )
            await self._fail_starting(item.instance.id, code)
            return
        already_authorized = verification is not None and verification.authorized
        if not already_authorized and not await self._verify_launch(item, deadline=deadline):
            return
        try:
            if already_authorized:
                observation = await self._agent.inspect(str(item.instance.id))
                if observation.state is not RuntimeProcessState.RUNNING:
                    await self._store.mark_unhealthy(
                        item.instance.id,
                        expected_status=ModelInstanceStatus.STARTING,
                        error_code=_runtime_exit_error(observation, "runtime_start_failed"),
                    )
                    return
            else:
                observation = await self._agent.ensure(request)
        except RuntimeAgentError:
            try:
                inspected_after_error = await self._agent.inspect(str(item.instance.id))
            except RuntimeAgentError:
                inspected_after_error = None
            error_code = (
                "runtime_start_failed"
                if inspected_after_error is None
                else _runtime_exit_error(inspected_after_error, "runtime_start_failed")
            )
            await self._fail_starting(item.instance.id, error_code)
            return
        if observation.state is not RuntimeProcessState.RUNNING:
            await self._fail_starting(
                item.instance.id,
                _runtime_exit_error(observation, "runtime_start_failed"),
            )
            return
        endpoint = _trusted_endpoint(observation.endpoint, item.instance.id)
        if endpoint is None:
            await self._fail_starting(item.instance.id, "runtime_start_failed")
            return
        published = await self._store.set_endpoint(
            item.instance.id,
            expected_status=ModelInstanceStatus.STARTING,
            endpoint=endpoint,
        )
        if not published:
            await self._stop_runtime(item.instance.id)
            return

        consecutive_successes = 0
        while self._monotonic() < deadline:
            if await self._store.status(item.instance.id) is not ModelInstanceStatus.STARTING:
                await self._stop_runtime(item.instance.id)
                return
            try:
                inspected = await self._agent.inspect(str(item.instance.id))
            except RuntimeAgentError:
                inspected = None
            if inspected is not None and inspected.state is not RuntimeProcessState.RUNNING:
                await self._fail_starting(
                    item.instance.id,
                    _runtime_exit_error(inspected, "runtime_start_failed"),
                )
                return
            if inspected is not None:
                inspected_endpoint = _trusted_endpoint(inspected.endpoint, item.instance.id)
                if inspected_endpoint is None:
                    consecutive_successes = 0
                else:
                    endpoint = inspected_endpoint
                    await self._store.set_endpoint(
                        item.instance.id,
                        expected_status=ModelInstanceStatus.STARTING,
                        endpoint=endpoint,
                    )
                    if await self._health.ready(endpoint, item.instance.internal_model_name):
                        consecutive_successes += 1
                    else:
                        consecutive_successes = 0
            else:
                consecutive_successes = 0
            if consecutive_successes >= 2:
                if await self._health.warmup(endpoint, item.instance.internal_model_name):
                    if not await self._store.mark_running(item.instance.id):
                        await self._stop_runtime(item.instance.id)
                    return
                consecutive_successes = 0
            await self._sleep(self._start_poll_seconds)
        await self._fail_starting(item.instance.id, "runtime_start_timeout")

    async def _verify_launch(
        self,
        item: ControllerModelInstanceRecord,
        *,
        deadline: float,
    ) -> bool:
        expected_status = item.instance.status
        initial: LaunchVerification | None = None
        while self._monotonic() < deadline:
            if await self._store.status(item.instance.id) is not expected_status:
                return False
            try:
                current = await self._store.launch_verification(item.instance.id)
            except ValueError:
                await self._fail_verification(item, "artifact_verification_failed")
                return False
            if current is None:
                await self._fail_verification(item, "artifact_verification_failed")
                return False
            if (
                current.artifact_id != item.instance.artifact_id
                or current.config_version != item.instance.config_version
            ):
                return False
            if initial is None:
                initial = current
                elapsed = max(0.0, (datetime.now(UTC) - current.requested_at).total_seconds())
                deadline = min(
                    deadline,
                    self._monotonic()
                    + max(
                        0.0,
                        self._start_timeout_seconds - elapsed,
                    ),
                )
            if current.request_id != initial.request_id:
                return False
            if current.result is LaunchVerificationResult.READY:
                if self._monotonic() >= deadline:
                    break
                authorized = await self._store.authorize_launch(current)
                if not authorized:
                    latest = await self._store.launch_verification(item.instance.id)
                    if latest is not None and latest.request_id == current.request_id:
                        await self._fail_verification(item, "artifact_verification_failed")
                return authorized
            if current.result is not LaunchVerificationResult.PENDING:
                await self._fail_verification(item, "artifact_verification_failed")
                return False
            await self._sleep(self._start_poll_seconds)
        await self._fail_verification(item, "artifact_verification_timeout")
        return False

    async def _fail_verification(self, item: ControllerModelInstanceRecord, code: str) -> None:
        if item.instance.status is ModelInstanceStatus.STARTING:
            await self._fail_starting(item.instance.id, code)
        else:
            await self._fail_restarting(item.instance.id, code)

    async def process_stopping(self, item: ControllerModelInstanceRecord) -> None:
        # ``updated_at`` is written by the Running -> Stopping transition.  Basing the
        # remaining delay on that durable timestamp means a controller restart cannot
        # restart the full drain window.  asyncio.sleep uses a monotonic clock for the
        # actual wait, so wall-clock adjustment cannot lengthen an in-progress drain.
        elapsed = max(0.0, (datetime.now(UTC) - item.instance.updated_at).total_seconds())
        remaining = max(0.0, self._stop_drain_seconds - elapsed)
        if remaining > 0:
            await self._sleep(remaining)
        if await self._store.status(item.instance.id) is not ModelInstanceStatus.STOPPING:
            return
        if await self._stop_runtime(item.instance.id):
            await self._store.mark_stopped(item.instance.id)

    async def process_running(self, item: ControllerModelInstanceRecord) -> None:
        failures = 0
        endpoint = _trusted_endpoint(item.instance.internal_endpoint or "", item.instance.id)
        while await self._store.status(item.instance.id) is ModelInstanceStatus.RUNNING:
            try:
                observation: RuntimeObservation | None = await self._agent.inspect(
                    str(item.instance.id)
                )
            except RuntimeAgentError:
                observation = None
            if observation is not None and observation.state is not RuntimeProcessState.RUNNING:
                await self._store.mark_unhealthy(
                    item.instance.id,
                    expected_status=ModelInstanceStatus.RUNNING,
                    error_code=_runtime_exit_error(observation, "runtime_exited"),
                )
                return
            if observation is not None:
                endpoint = _trusted_endpoint(observation.endpoint, item.instance.id)
                if endpoint is not None:
                    await self._store.set_endpoint(
                        item.instance.id,
                        expected_status=ModelInstanceStatus.RUNNING,
                        endpoint=endpoint,
                    )
            healthy = endpoint is not None and await self._health.ready(
                endpoint, item.instance.internal_model_name
            )
            failures = 0 if healthy else failures + 1
            if failures >= 3:
                if await self._stop_runtime(item.instance.id):
                    await self._store.mark_unhealthy(
                        item.instance.id,
                        expected_status=ModelInstanceStatus.RUNNING,
                        error_code="runtime_health_failed",
                    )
                return
            await self._sleep(self._health_seconds)

    async def process_unhealthy(self, item: ControllerModelInstanceRecord) -> None:
        if not await self._stop_runtime(item.instance.id):
            return
        await self._store.begin_restart(
            item.instance.id,
            restart_window_seconds=self._restart_window_seconds,
        )

    async def process_restarting(self, item: ControllerModelInstanceRecord) -> None:
        request = self._launch_request(item)
        if request is None:
            error_code = (
                _RUNTIME_NOT_CERTIFIED
                if self._runtime_lookup(item.instance.runtime_type.value) is None
                else "runtime_restart_failed"
            )
            await self._fail_restarting(item.instance.id, error_code)
            return
        completed_attempts = item.instance.restart_count
        previous = await self._store.launch_verification(item.instance.id)
        resume = previous is not None and not previous.authorized
        if previous is not None and previous.authorized and completed_attempts > 0:
            # Preserve the durable attempt identity before deciding whether Ensure
            # already launched its process. A new request would make it look old.
            observation = await self._inspect_restart(item.instance.id, completed_attempts)
            if observation is None:
                return
            resume = observation.state is RuntimeProcessState.RUNNING
        while resume or completed_attempts < len(_RESTART_BACKOFF_SECONDS):
            if not resume:
                await self._sleep(_RESTART_BACKOFF_SECONDS[completed_attempts])
                if await self._store.status(item.instance.id) is not ModelInstanceStatus.RESTARTING:
                    return
                attempt = await self._store.record_restart_attempt(item.instance.id)
                if attempt is None:
                    return
                completed_attempts = attempt
            resume = False
            verification = await self._store.launch_verification(item.instance.id)
            elapsed = (
                0.0
                if verification is None
                else max(0.0, (datetime.now(UTC) - verification.requested_at).total_seconds())
            )
            deadline = self._monotonic() + max(0.0, self._start_timeout_seconds - elapsed)
            observation = await self._inspect_restart(item.instance.id, completed_attempts)
            if observation is None:
                return
            if not await self._verify_launch(item, deadline=deadline):
                return
            assert verification is not None
            endpoint = await self._restart_until_ready(
                item,
                request,
                observation=observation,
                requested_at=verification.requested_at,
                deadline=deadline,
            )
            if endpoint is not None:
                await self._store.mark_restarted_running(
                    item.instance.id,
                    endpoint=endpoint,
                )
                return
            # Stop owns Runtime cleanup once the restart has been cancelled.
            if await self._store.status(item.instance.id) is not ModelInstanceStatus.RESTARTING:
                return
            stopped = await self._stop_runtime(item.instance.id)
            if completed_attempts >= len(_RESTART_BACKOFF_SECONDS):
                if stopped:
                    error_code = (
                        _RUNTIME_OUT_OF_MEMORY
                        if item.instance.error_code == _RUNTIME_OUT_OF_MEMORY
                        else "runtime_restart_failed"
                    )
                    await self._store.mark_failed(
                        item.instance.id,
                        expected_status=ModelInstanceStatus.RESTARTING,
                        error_code=error_code,
                    )
                return

    async def _inspect_restart(
        self,
        instance_id: UUID,
        completed_attempts: int,
    ) -> RuntimeObservation | None:
        try:
            observation = await self._agent.inspect(str(instance_id))
            if observation.state is RuntimeProcessState.RUNNING and observation.started_at is None:
                raise RuntimeAgentError("runtime_agent_invalid_response")
            return observation
        except (RuntimeAgentError, TimeoutError):
            self._log_recovery_failure("runtime_restart_inspection_unavailable")
            # Unknown process identity must not enter failure cleanup (which Stops).
            # Retain this request for the next dispatch, including its GPU ownership.
            await self._sleep(_RESTART_BACKOFF_SECONDS[min(completed_attempts, 2)])
            return None

    async def _restart_until_ready(
        self,
        item: ControllerModelInstanceRecord,
        request: RuntimeLaunchRequest,
        *,
        observation: RuntimeObservation,
        requested_at: datetime,
        deadline: float,
    ) -> str | None:
        if await self._store.status(item.instance.id) is not ModelInstanceStatus.RESTARTING:
            return None
        if observation.state is RuntimeProcessState.RUNNING:
            assert observation.started_at is not None
            # Both timestamps come from the same host. Bias the one-second
            # tolerance toward retaining a newly launched process on recovery.
            if observation.started_at < requested_at - timedelta(seconds=1):
                if not await self._stop_runtime(item.instance.id):
                    return None
                observation = RuntimeObservation(RuntimeProcessState.NOT_FOUND)
        if observation.state is not RuntimeProcessState.RUNNING:
            if await self._store.status(item.instance.id) is not ModelInstanceStatus.RESTARTING:
                return None
            try:
                observation = await self._agent.ensure(request)
            except (RuntimeAgentError, TimeoutError):
                return None
        endpoint = _trusted_endpoint(observation.endpoint, item.instance.id)
        if observation.state is not RuntimeProcessState.RUNNING or endpoint is None:
            return None
        successes = 0
        while self._monotonic() < deadline:
            if await self._store.status(item.instance.id) is not ModelInstanceStatus.RESTARTING:
                return None
            try:
                inspected = await self._agent.inspect(str(item.instance.id))
            except RuntimeAgentError:
                inspected = None
            if inspected is not None and inspected.state is not RuntimeProcessState.RUNNING:
                return None
            inspected_endpoint = (
                None
                if inspected is None
                else _trusted_endpoint(inspected.endpoint, item.instance.id)
            )
            if inspected_endpoint is not None:
                endpoint = inspected_endpoint
            healthy = endpoint is not None and await self._health.ready(
                endpoint,
                item.instance.internal_model_name,
            )
            successes = successes + 1 if healthy else 0
            if successes >= 2:
                if await self._health.warmup(endpoint, item.instance.internal_model_name):
                    return endpoint
                successes = 0
            await self._sleep(self._start_poll_seconds)
        return None

    async def _fail_restarting(self, instance_id: UUID, error_code: str) -> None:
        if await self._stop_runtime(instance_id):
            await self._store.mark_failed(
                instance_id,
                expected_status=ModelInstanceStatus.RESTARTING,
                error_code=error_code,
            )

    async def _fail_starting(self, instance_id: UUID, error_code: str) -> None:
        if await self._stop_runtime(instance_id):
            await self._store.mark_failed(
                instance_id,
                expected_status=ModelInstanceStatus.STARTING,
                error_code=error_code,
            )

    async def _stop_runtime(self, instance_id: UUID) -> bool:
        try:
            observation = await self._agent.stop(
                str(instance_id),
                grace_seconds=self._stop_grace_seconds,
            )
        except (RuntimeAgentError, TimeoutError):
            return False
        return observation.state in {
            RuntimeProcessState.EXITED,
            RuntimeProcessState.NOT_FOUND,
        }


__all__ = [
    "DEFAULT_STOP_DRAIN_SECONDS",
    "DEFAULT_STOP_GRACE_SECONDS",
    "InstanceController",
]
