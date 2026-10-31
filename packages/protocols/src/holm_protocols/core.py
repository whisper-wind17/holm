"""Protocol-independent authentication, routing, admission, and lifecycle orchestration."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Final, Never
from uuid import UUID

from holm_domain import ErrorCode

from holm_protocols.admission import (
    AdmissionController as AdmissionController,
)
from holm_protocols.admission import (
    AdmissionPermit as AdmissionPermit,
)
from holm_protocols.models import (
    CanonicalEvent,
    CanonicalRequest,
    InferenceFailure,
    ModelReasoningPolicy,
    PlatformError,
    ReasoningControl,
    ServiceModelSummary,
)
from holm_protocols.ports import (
    AuthenticatedPrincipal,
    AuthenticationPort,
    RequestLifecycle,
    RouteDirectory,
    RuntimeAdapter,
    ServiceRoute,
)


class CircuitBreakerPhase(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass(slots=True)
class _CircuitState:
    phase: CircuitBreakerPhase
    failures: int
    window_started_at: float
    opened_at: float | None = None


class CircuitBreaker:
    """Instance-scoped failure gate with one HalfOpen probe and bounded state."""

    __slots__ = (
        "_clock",
        "_cooldown_seconds",
        "_failure_threshold",
        "_failure_window_seconds",
        "_lock",
        "_routed_instances",
        "_states",
    )

    _MAX_THRESHOLD: Final[int] = 100
    _MAX_SECONDS: Final[float] = 300.0

    def __init__(
        self,
        *,
        failure_threshold: int = 3,
        failure_window_seconds: float = 10.0,
        cooldown_seconds: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if (
            not isinstance(failure_threshold, int)
            or isinstance(failure_threshold, bool)
            or not 1 <= failure_threshold <= self._MAX_THRESHOLD
        ):
            raise ValueError("Circuit Breaker threshold is out of bounds")
        if (
            not isinstance(failure_window_seconds, (int, float))
            or isinstance(failure_window_seconds, bool)
            or not 0.1 <= failure_window_seconds <= self._MAX_SECONDS
        ):
            raise ValueError("Circuit Breaker failure window is out of bounds")
        if (
            not isinstance(cooldown_seconds, (int, float))
            or isinstance(cooldown_seconds, bool)
            or not 0.1 <= cooldown_seconds <= self._MAX_SECONDS
        ):
            raise ValueError("Circuit Breaker cooldown is out of bounds")
        self._failure_threshold = failure_threshold
        self._failure_window_seconds = failure_window_seconds
        self._cooldown_seconds = cooldown_seconds
        self._clock = clock
        self._states: dict[UUID, _CircuitState] = {}
        self._routed_instances: frozenset[UUID] | None = None
        self._lock = asyncio.Lock()

    async def before_request(self, instance_id: UUID) -> bool | None:
        """Return whether this is the sole Probe, or None when routing is blocked."""

        async with self._lock:
            state = self._states.get(instance_id)
            if state is None or state.phase is CircuitBreakerPhase.CLOSED:
                return False
            if state.phase is CircuitBreakerPhase.HALF_OPEN:
                return None
            opened_at = state.opened_at
            if opened_at is None or self._clock() - opened_at < self._cooldown_seconds:
                return None
            state.phase = CircuitBreakerPhase.HALF_OPEN
            return True

    async def record_success(self, instance_id: UUID) -> None:
        """A responsive Runtime closes the Breaker and resets the consecutive streak."""

        async with self._lock:
            self._states.pop(instance_id, None)

    async def record_failure(self, instance_id: UUID) -> None:
        """Record one classified connection/5xx failure in completion order."""

        async with self._lock:
            if self._routed_instances is not None and instance_id not in self._routed_instances:
                return
            now = self._clock()
            state = self._states.get(instance_id)
            if state is not None and state.phase is CircuitBreakerPhase.HALF_OPEN:
                state.phase = CircuitBreakerPhase.OPEN
                state.failures = self._failure_threshold
                state.window_started_at = now
                state.opened_at = now
                return
            if state is not None and state.phase is CircuitBreakerPhase.OPEN:
                state.opened_at = now
                return
            if state is None or now - state.window_started_at > self._failure_window_seconds:
                state = _CircuitState(CircuitBreakerPhase.CLOSED, 1, now)
                self._states[instance_id] = state
            else:
                state.failures += 1
            if state.failures >= self._failure_threshold:
                state.phase = CircuitBreakerPhase.OPEN
                state.opened_at = now

    async def cancel_probe(self, instance_id: UUID, *, was_probe: bool) -> None:
        """Release a Probe not dispatched to Runtime without treating it as an outcome."""

        if not was_probe:
            return
        async with self._lock:
            state = self._states.get(instance_id)
            if state is not None and state.phase is CircuitBreakerPhase.HALF_OPEN:
                state.phase = CircuitBreakerPhase.OPEN

    async def sync_instances(self, instance_ids: Iterable[UUID]) -> None:
        """Reclaim state as soon as an instance leaves the usable route set."""

        routed = frozenset(instance_ids)
        async with self._lock:
            self._routed_instances = routed
            for instance_id in self._states.keys() - routed:
                del self._states[instance_id]

    async def tracked_instances(self) -> frozenset[UUID]:
        """Return a diagnostic snapshot without exposing mutable state."""

        async with self._lock:
            return frozenset(self._states)


@dataclass(frozen=True, slots=True)
class InferenceResponse:
    stream: bool
    events: AsyncIterator[CanonicalEvent]


class InferenceCore:
    """Run shared inference policy without knowing HTTP or a wire protocol."""

    __slots__ = (
        "_admission",
        "_authenticator",
        "_breaker",
        "_clock",
        "_first_token_timeout",
        "_routes",
        "_reasoning_models",
        "_runtime",
        "_total_timeout",
    )

    def __init__(
        self,
        *,
        authenticator: AuthenticationPort,
        routes: RouteDirectory,
        runtime: RuntimeAdapter,
        admission: AdmissionController,
        first_token_timeout_seconds: float,
        total_timeout_seconds: float,
        circuit_breaker: CircuitBreaker | None = None,
        reasoning_models: Mapping[str, ModelReasoningPolicy] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._authenticator = authenticator
        self._routes = routes
        self._reasoning_models = dict(reasoning_models or {})
        self._runtime = runtime
        self._admission = admission
        self._breaker = circuit_breaker
        self._first_token_timeout = first_token_timeout_seconds
        self._total_timeout = total_timeout_seconds
        self._clock = clock

    async def list_models(self, credential: str | None) -> tuple[ServiceModelSummary, ...]:
        principal = await self._authenticator.authenticate(credential)
        return tuple(
            ServiceModelSummary(route.public_model_name, route.ready)
            for route in await self._routes.list_enabled()
            if principal.allows_service(route.service_id)
        )

    async def authorize_route(
        self,
        *,
        principal: AuthenticatedPrincipal,
        public_model_name: str,
        lifecycle: RequestLifecycle,
    ) -> ServiceRoute:
        """Resolve policy before protocol validation needs target Runtime facts."""

        try:
            route = await self._routes.find(public_model_name)
            if route is None:
                self._fail(ErrorCode.MODEL_NOT_FOUND)
            lifecycle.set_route(route)
            if not principal.allows_service(route.service_id):
                self._fail(ErrorCode.SERVICE_NOT_AUTHORIZED)
            if not route.enabled:
                self._fail(ErrorCode.SERVICE_DISABLED)
            if not route.ready:
                self._fail(ErrorCode.BACKEND_UNAVAILABLE)
            return route
        except InferenceFailure as failure:
            await lifecycle.mark_error(failure.error)
            raise

    async def authenticate(
        self,
        credential: str | None,
        lifecycle: RequestLifecycle | None = None,
    ) -> AuthenticatedPrincipal:
        """Authenticate before protocol parsing while keeping policy in the core."""

        try:
            principal = await self._authenticator.authenticate(credential)
        except InferenceFailure as failure:
            if lifecycle is not None:
                await lifecycle.mark_error(failure.error)
            raise
        if lifecycle is not None:
            lifecycle.set_authenticated(principal)
        return principal

    async def infer(
        self,
        *,
        credential: str | None,
        request_id: UUID,
        request: CanonicalRequest,
        lifecycle: RequestLifecycle,
    ) -> InferenceResponse:
        lifecycle.set_stream(request.stream)
        principal = await self.authenticate(credential, lifecycle)
        return await self.infer_authenticated(
            principal=principal,
            request_id=request_id,
            request=request,
            lifecycle=lifecycle,
        )

    async def infer_authenticated(
        self,
        *,
        principal: AuthenticatedPrincipal,
        request_id: UUID,
        request: CanonicalRequest,
        lifecycle: RequestLifecycle,
    ) -> InferenceResponse:
        lifecycle.set_stream(request.stream)
        route = await self.authorize_route(
            principal=principal,
            public_model_name=request.service_model.public_model_name,
            lifecycle=lifecycle,
        )
        return await self.infer_routed(
            route=route,
            principal=principal,
            request_id=request_id,
            request=request,
            lifecycle=lifecycle,
        )

    async def infer_routed(
        self,
        *,
        route: ServiceRoute,
        principal: AuthenticatedPrincipal | None = None,
        current_limits: Callable[[UUID, UUID], tuple[int | None, int | None]] | None = None,
        disconnected: Callable[[], Awaitable[bool]] | None = None,
        request_id: UUID,
        request: CanonicalRequest,
        lifecycle: RequestLifecycle,
    ) -> InferenceResponse:
        """Dispatch through a route already authorized for this request."""

        permit: AdmissionPermit | None = None
        lifecycle.set_stream(request.stream)
        try:
            if route.public_model_name != request.service_model.public_model_name:
                self._fail(ErrorCode.INVALID_REQUEST)
            if not route.ready or route.endpoint is None:
                self._fail(ErrorCode.BACKEND_UNAVAILABLE)
            if route.instance_id is None:
                self._fail(ErrorCode.BACKEND_UNAVAILABLE)
            policy = self._reasoning_models.get(route.model_architecture or "")
            if request.reasoning is not None:
                control = request.reasoning
                if control.nearest_supported:
                    effort = None if policy is None else policy.resolve_nearest(control.effort)
                    request = replace(
                        request, reasoning=None if effort is None else ReasoningControl(effort)
                    )
                elif policy is not None:
                    request = replace(
                        request, reasoning=ReasoningControl(policy.resolve(control.effort))
                    )
            if principal is None:
                permit = await self._admission.try_acquire(route.instance_id)
            else:
                permit = await self._admission.acquire(
                    route.instance_id,
                    user_id=principal.user_id,
                    service_id=route.service_id,
                    disconnected=disconnected,
                    limits=lambda: (
                        current_limits(principal.user_id, route.service_id)
                        if current_limits is not None
                        else principal.service_limits.get(route.service_id, (None, None))
                    ),
                )
            if permit is None:
                await lifecycle.mark_in_progress(request.sampling.maximum_output_tokens)
                self._fail(ErrorCode.GATEWAY_OVERLOADED)
            breaker_probe = False
            try:
                if self._breaker is not None:
                    breaker_decision = await self._breaker.before_request(route.instance_id)
                    if breaker_decision is None:
                        self._fail(ErrorCode.BACKEND_UNAVAILABLE)
                    breaker_probe = breaker_decision
            except BaseException:
                await permit.release()
                raise
            try:
                await lifecycle.mark_in_progress(request.sampling.maximum_output_tokens)
                await permit.mark_started()
            except BaseException:
                await permit.release()
                if self._breaker is not None:
                    await self._breaker.cancel_probe(route.instance_id, was_probe=breaker_probe)
                raise
            lifecycle.mark_forwarded()
            runtime_request = replace(
                request,
                service_model=replace(
                    request.service_model,
                    runtime_model_name=route.runtime_model_name,
                ),
            )
            started_at = self._clock()
            events = self._runtime.stream(route.endpoint, request_id, runtime_request)
            # Tokenizer fallback preparation starts only after
            # Runtime Dispatch and runs on a bounded worker pool.  This call
            # schedules work and returns; nothing below awaits it.
            lifecycle.begin_metering(request)
            try:
                first_event = await asyncio.wait_for(
                    anext(events), timeout=self._first_token_timeout
                )
            except TimeoutError:
                try:
                    await events.aclose()
                finally:
                    await permit.release()
                if self._breaker is not None:
                    await self._breaker.record_success(route.instance_id)
                self._fail(ErrorCode.INFERENCE_TIMEOUT)
            except StopAsyncIteration:
                try:
                    await events.aclose()
                finally:
                    await permit.release()
                if self._breaker is not None:
                    await self._breaker.record_success(route.instance_id)
                self._fail(ErrorCode.BACKEND_UNAVAILABLE)
            except InferenceFailure as failure:
                await permit.release()
                if self._breaker is not None:
                    if failure.counts_toward_breaker:
                        await self._breaker.record_failure(route.instance_id)
                    else:
                        await self._breaker.record_success(route.instance_id)
                raise
            except BaseException:
                await permit.release()
                if self._breaker is not None:
                    await self._breaker.cancel_probe(route.instance_id, was_probe=breaker_probe)
                raise
            if self._breaker is not None:
                await self._breaker.record_success(route.instance_id)
            remaining = self._total_timeout - (self._clock() - started_at)
            if remaining <= 0:
                try:
                    await events.aclose()
                finally:
                    await permit.release()
                self._fail(ErrorCode.INFERENCE_TIMEOUT)
            return InferenceResponse(
                stream=request.stream,
                events=self._managed_events(
                    first_event=first_event,
                    remaining_events=events,
                    remaining_seconds=remaining,
                    permit=permit,
                    lifecycle=lifecycle,
                    stream=request.stream,
                    instance_id=route.instance_id,
                ),
            )
        except InferenceFailure as failure:
            if permit is not None:
                await permit.release()
            await lifecycle.mark_error(failure.error)
            raise
        except asyncio.CancelledError:
            if permit is not None:
                await permit.release()
            await lifecycle.mark_aborted()
            raise
        except BaseException:
            if permit is not None:
                await permit.release()
            raise

    async def _managed_events(
        self,
        *,
        first_event: CanonicalEvent,
        remaining_events: AsyncGenerator[CanonicalEvent, None],
        remaining_seconds: float,
        permit: AdmissionPermit,
        lifecycle: RequestLifecycle,
        stream: bool,
        instance_id: UUID,
    ) -> AsyncIterator[CanonicalEvent]:
        completed = False
        try:
            async with asyncio.timeout(remaining_seconds):
                lifecycle.observe_event(first_event)
                yield first_event
                async for event in remaining_events:
                    lifecycle.observe_event(event)
                    yield event
            completed = True
        except (asyncio.CancelledError, GeneratorExit):
            await lifecycle.mark_aborted()
            raise
        except TimeoutError:
            failure = InferenceFailure(PlatformError(ErrorCode.INFERENCE_TIMEOUT))
            await lifecycle.mark_error(failure.error)
            if not stream:
                raise failure from None
        except InferenceFailure as failure:
            await lifecycle.mark_error(failure.error)
            if self._breaker is not None and failure.counts_toward_breaker:
                await self._breaker.record_failure(instance_id)
            if not stream:
                raise
        except Exception:
            backend_failure = InferenceFailure(PlatformError(ErrorCode.BACKEND_UNAVAILABLE))
            await lifecycle.mark_error(backend_failure.error)
            if not stream:
                raise backend_failure from None
        finally:
            try:
                await remaining_events.aclose()
            finally:
                await permit.release()
            if completed:
                await lifecycle.mark_completed()

    @staticmethod
    def _fail(code: ErrorCode) -> Never:
        raise InferenceFailure(PlatformError(code))
