"""Fail-closed Host Agent authorization derived from kernel peer credentials."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final, Protocol, cast

import grpc
from google.protobuf.descriptor import (  # type: ignore[import-untyped]
    MethodDescriptor,
    ServiceDescriptor,
)
from google.protobuf.message import Message  # type: ignore[import-untyped]
from grpc import aio
from holm_contracts import ReleaseManifest
from holm_contracts.agent.v1 import agent_pb2

_SERVICE_DESCRIPTOR: Final[ServiceDescriptor] = agent_pb2.DESCRIPTOR.services_by_name["HostAgent"]
_METHOD_PATH_PATTERN: Final[re.Pattern[str]] = re.compile(
    rf"^/{re.escape(_SERVICE_DESCRIPTOR.full_name)}/([A-Za-z][A-Za-z0-9]{{0,63}})$"
)
_ACCESS_DENIED_DETAIL: Final[str] = "Host Agent RPC access denied"
_REQUEST_REJECTED_DETAIL: Final[str] = "Host Agent RPC request rejected"


def _method(name: str) -> str:
    """Resolve policy entries through the protobuf descriptor, never a second catalog."""
    return cast(str, _SERVICE_DESCRIPTOR.methods_by_name[name].name)


ROLE_METHOD_ALLOWLIST: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {
        "control-api": frozenset(
            {_method("GetHostInfo"), _method("ListGPUs"), _method("StreamLogs")}
        ),
        "download-worker": frozenset({_method("GetHostInfo"), _method("ValidatePath")}),
        "instance-controller": frozenset(
            {
                _method("EnsureRuntime"),
                _method("StopRuntime"),
                _method("RemoveRuntime"),
                _method("InspectRuntime"),
                _method("ListRuntimes"),
                _method("ListGPUs"),
                _method("StreamLogs"),
            }
        ),
    }
)


class AuthorizationDenialReason(StrEnum):
    """Stable reason categories for authorization denials."""

    UNKNOWN_UID = "unknown_uid"
    CONTAINER_ROOT = "container_root"
    METHOD_NOT_ALLOWED = "method_not_allowed"
    MANIFEST_UNAVAILABLE = "manifest_unavailable"


class ProtocolRejectionReason(StrEnum):
    """Stable reason for a request rejected before application handling."""

    UNKNOWN_FIELDS = "unknown_fields"


DenialReason = AuthorizationDenialReason | ProtocolRejectionReason


class DenialAuditor(Protocol):
    """Minimal sink boundary; the Agent deliberately has no database dependency."""

    def record(self, *, peer_uid: int, rpc_method: str, reason: DenialReason) -> None:
        """Record one content-free denial."""


class StructuredDenialAuditor:
    """Write one machine-parseable JSON object for every rejected RPC."""

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._logger = logger or logging.getLogger("holm_host_agent.authorization")

    def record(self, *, peer_uid: int, rpc_method: str, reason: DenialReason) -> None:
        payload = {
            "event": "host_agent_rpc_denied",
            "peer_uid": peer_uid,
            "rpc_method": rpc_method,
            "reason": reason.value,
            "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        }
        self._logger.warning(json.dumps(payload, separators=(",", ":"), sort_keys=True))


@dataclass(frozen=True, slots=True)
class IdentityCatalog:
    """Validated UID-to-service mapping or an explicit unavailable state."""

    available: bool
    services_by_uid: Mapping[int, str]

    @classmethod
    def unavailable(cls) -> IdentityCatalog:
        return cls(available=False, services_by_uid=MappingProxyType({}))

    @classmethod
    def from_pairs(cls, identities: Iterable[tuple[int, str]]) -> IdentityCatalog:
        services_by_uid: dict[int, str] = {}
        for uid, service in identities:
            if uid in services_by_uid:
                return cls.unavailable()
            services_by_uid[uid] = service
        if not services_by_uid:
            return cls.unavailable()
        return cls(available=True, services_by_uid=MappingProxyType(services_by_uid))


def load_identity_catalog(path: Path) -> IdentityCatalog:
    """Load one immutable Release Manifest; any failure becomes deny-all state."""
    try:
        payload: object = json.loads(path.read_text(encoding="utf-8"))
        manifest = ReleaseManifest.model_validate(payload)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return IdentityCatalog.unavailable()
    protected = frozenset(ROLE_METHOD_ALLOWLIST) | {"runtime"}
    identities = manifest.service_identities
    protected_uids = {identity.uid for identity in identities if identity.service in protected}
    if any(
        identity.uid in protected_uids and identity.service not in protected
        for identity in identities
    ):
        return IdentityCatalog.unavailable()
    return IdentityCatalog.from_pairs(
        (identity.uid, identity.service) for identity in identities if identity.service in protected
    )


@dataclass(frozen=True, slots=True)
class AuthorizationDecision:
    """One explicit allow or categorized deny result."""

    allowed: bool
    reason: AuthorizationDenialReason | None = None


def allowed_methods_for_service(service: str) -> frozenset[str]:
    """Return no privileges for every service without an explicit role policy."""
    return ROLE_METHOD_ALLOWLIST.get(service, frozenset())


def authorize_rpc(
    *, peer_uid: int, rpc_method: str, identities: IdentityCatalog
) -> AuthorizationDecision:
    """Authorize solely from the kernel-bound UID and the Release Manifest catalog."""
    if peer_uid == 0:
        return AuthorizationDecision(False, AuthorizationDenialReason.CONTAINER_ROOT)
    if not identities.available:
        return AuthorizationDecision(False, AuthorizationDenialReason.MANIFEST_UNAVAILABLE)
    service = identities.services_by_uid.get(peer_uid)
    if service is None:
        return AuthorizationDecision(False, AuthorizationDenialReason.UNKNOWN_UID)
    if rpc_method not in allowed_methods_for_service(service):
        return AuthorizationDecision(False, AuthorizationDenialReason.METHOD_NOT_ALLOWED)
    return AuthorizationDecision(True)


def descriptor_method_names(
    service_descriptor: ServiceDescriptor = _SERVICE_DESCRIPTOR,
) -> frozenset[str]:
    """Return the RPC surface directly from the protobuf service descriptor."""
    return frozenset(method.name for method in service_descriptor.methods)


def assert_method_policy_complete(
    *,
    descriptor_methods: frozenset[str] | None = None,
    policy: Mapping[str, frozenset[str]] = ROLE_METHOD_ALLOWLIST,
) -> None:
    """Fail loudly when any descriptor method lacks an explicit role classification."""
    methods = descriptor_method_names() if descriptor_methods is None else descriptor_methods
    classified = frozenset().union(*policy.values()) if policy else frozenset()
    missing = methods - classified
    unexpected = classified - methods
    if missing or unexpected:
        raise AssertionError(
            f"Host Agent method policy drift: missing={sorted(missing)}, "
            f"unexpected={sorted(unexpected)}"
        )


_FORBIDDEN_REQUEST_FIELD_NAMES: Final[frozenset[str]] = frozenset(
    {"args", "command", "docker_args", "entrypoint", "env"}
)


def assert_protocol_request_surface_safe(
    service_descriptor: ServiceDescriptor = _SERVICE_DESCRIPTOR,
    *,
    extra_field_names: Iterable[str] = (),
) -> None:
    """Reject free-form execution channels and arbitrary maps in every request type."""
    fields = [field for method in service_descriptor.methods for field in method.input_type.fields]
    names = {field.name for field in fields}
    names.update(extra_field_names)
    forbidden = names.intersection(_FORBIDDEN_REQUEST_FIELD_NAMES)
    arbitrary_maps = {
        field.full_name
        for field in fields
        if field.message_type is not None and field.message_type.GetOptions().map_entry
    }
    if forbidden or arbitrary_maps:
        raise AssertionError(
            f"unsafe Host Agent request surface: forbidden={sorted(forbidden)}, "
            f"maps={sorted(arbitrary_maps)}"
        )


def _rpc_method_name(method_path: str) -> str:
    match = _METHOD_PATH_PATTERN.fullmatch(method_path)
    return match.group(1) if match is not None else "unknown"


@dataclass(frozen=True, slots=True)
class _ParsedRequest:
    message: Any
    has_unknown_fields: bool


def _strict_deserializer(
    deserialize: Callable[[bytes], Any],
) -> Callable[[bytes], _ParsedRequest]:
    def parse(payload: bytes) -> _ParsedRequest:
        message = deserialize(payload)
        if not isinstance(message, Message):
            raise TypeError("Host Agent request deserializer did not return protobuf")
        known_only = message.__class__()
        known_only.CopyFrom(message)
        known_only.DiscardUnknownFields()
        has_unknown_fields = message.SerializeToString(
            deterministic=True
        ) != known_only.SerializeToString(deterministic=True)
        return _ParsedRequest(message=message, has_unknown_fields=has_unknown_fields)

    return parse


class AuthorizationInterceptor(aio.ServerInterceptor):  # type: ignore[misc]
    """Bind one accepted connection's kernel UID to all of its RPC decisions."""

    def __init__(
        self,
        *,
        peer_uid: int,
        identities: IdentityCatalog,
        auditor: DenialAuditor,
    ) -> None:
        self._peer_uid = peer_uid
        self._identities = identities
        self._auditor = auditor

    async def intercept_service(
        self,
        continuation: Callable[[grpc.HandlerCallDetails], Any],
        handler_call_details: grpc.HandlerCallDetails,
    ) -> grpc.RpcMethodHandler:
        rpc_method = _rpc_method_name(handler_call_details.method)
        decision = authorize_rpc(
            peer_uid=self._peer_uid,
            rpc_method=rpc_method,
            identities=self._identities,
        )
        handler = cast(
            grpc.RpcMethodHandler | None,
            await continuation(handler_call_details),
        )
        if not decision.allowed:
            assert decision.reason is not None
            self._auditor.record(
                peer_uid=self._peer_uid,
                rpc_method=rpc_method,
                reason=decision.reason,
            )
            return _denied_handler()
        if handler is None:
            self._auditor.record(
                peer_uid=self._peer_uid,
                rpc_method=rpc_method,
                reason=AuthorizationDenialReason.METHOD_NOT_ALLOWED,
            )
            return _denied_handler()
        return _strict_request_handler(
            handler,
            peer_uid=self._peer_uid,
            rpc_method=rpc_method,
            auditor=self._auditor,
        )


def _denied_handler() -> grpc.RpcMethodHandler:
    async def deny_unary(request: Any, context: aio.ServicerContext) -> None:
        del request
        await context.abort(grpc.StatusCode.PERMISSION_DENIED, _ACCESS_DENIED_DETAIL)

    return grpc.unary_unary_rpc_method_handler(
        deny_unary,
        request_deserializer=lambda payload: payload,
        response_serializer=None,
    )


def _strict_request_handler(
    handler: grpc.RpcMethodHandler,
    *,
    peer_uid: int,
    rpc_method: str,
    auditor: DenialAuditor,
) -> grpc.RpcMethodHandler:
    if handler.request_streaming or handler.request_deserializer is None:
        raise RuntimeError("Host Agent methods must have protobuf unary requests")
    deserialize = _strict_deserializer(handler.request_deserializer)

    async def require_known_fields(request: _ParsedRequest, context: aio.ServicerContext) -> Any:
        if request.has_unknown_fields:
            auditor.record(
                peer_uid=peer_uid,
                rpc_method=rpc_method,
                reason=ProtocolRejectionReason.UNKNOWN_FIELDS,
            )
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, _REQUEST_REJECTED_DETAIL)
        assert handler.unary_unary is not None
        return await handler.unary_unary(request.message, context)

    async def require_known_fields_stream(
        request: _ParsedRequest, context: aio.ServicerContext
    ) -> Any:
        if request.has_unknown_fields:
            auditor.record(
                peer_uid=peer_uid,
                rpc_method=rpc_method,
                reason=ProtocolRejectionReason.UNKNOWN_FIELDS,
            )
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, _REQUEST_REJECTED_DETAIL)
        assert handler.unary_stream is not None
        async for response in handler.unary_stream(request.message, context):
            yield response

    if handler.response_streaming:
        return grpc.unary_stream_rpc_method_handler(
            require_known_fields_stream,
            request_deserializer=deserialize,
            response_serializer=handler.response_serializer,
        )
    return grpc.unary_unary_rpc_method_handler(
        require_known_fields,
        request_deserializer=deserialize,
        response_serializer=handler.response_serializer,
    )


def method_descriptor(name: str) -> MethodDescriptor:
    """Expose the canonical descriptor lookup for deterministic security tests."""
    return _SERVICE_DESCRIPTOR.methods_by_name[name]
