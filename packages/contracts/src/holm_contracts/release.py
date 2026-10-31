"""Release Manifest and framework-independent version payload contracts."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Annotated, Final, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

_PLACEHOLDER_VALUES: Final[frozenset[str]] = frozenset(
    {
        "change-me",
        "changeme",
        "n/a",
        "none",
        "not-set",
        "placeholder",
        "tbd",
        "todo",
        "unknown",
        "unset",
    }
)


def _reject_placeholder(value: str) -> str:
    normalized = value.strip()
    if not normalized or normalized.casefold() in _PLACEHOLDER_VALUES:
        raise ValueError("value must be concrete, not empty or a placeholder")
    return normalized


ConcreteString = Annotated[
    str,
    Field(strict=True, min_length=1),
    AfterValidator(_reject_placeholder),
]
Sha256 = Annotated[str, Field(strict=True, pattern=r"^[0-9a-f]{64}$")]
OciDigest = Annotated[str, Field(strict=True, pattern=r"^sha256:[0-9a-f]{64}$")]
GitCommit = Annotated[str, Field(strict=True, pattern=r"^[0-9a-f]{40}$")]
PositiveInt = Annotated[int, Field(strict=True, gt=0)]

PRODUCTION_SERVICE_IDS: Final[Mapping[str, tuple[int, int]]] = MappingProxyType(
    {
        "postgres": (70, 70),
        "edge": (10001, 10001),
        "migrate": (10002, 10002),
        "control-api": (10003, 10003),
        "gateway": (10004, 10004),
        "ledger-writer": (10004, 10004),
        "download-worker": (10005, 10003),
        "instance-controller": (10006, 10006),
        "maintenance-worker": (10007, 10007),
        "runtime": (10008, 10008),
    }
)
PRODUCTION_AGENT_SOCKET_GID: Final[int] = 10009


class _ClosedContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class NamedDigest(_ClosedContract):
    """One immutable image name and OCI digest pair."""

    name: ConcreteString
    digest: OciDigest


class NamedHash(_ClosedContract):
    """One immutable release file name and SHA-256 pair."""

    name: ConcreteString
    sha256: Sha256


class ReleaseArtifactHashes(_ClosedContract):
    """Hashes for the release bundle and deployment definitions."""

    bundle: Sha256
    compose: Sha256
    caddyfile: Sha256
    systemd_units: tuple[NamedHash, ...] = Field(min_length=1)


class ServiceIdentity(_ClosedContract):
    """Fixed non-root identity for a release service."""

    service: ConcreteString
    uid: PositiveInt
    gid: PositiveInt


class ResourceLimits(_ClosedContract):
    """Default bounded resources for one service profile."""

    service: ConcreteString
    cpu_millicores: PositiveInt
    memory_mib: PositiveInt
    pids: PositiveInt
    log_rotation_mib: PositiveInt
    tmpfs_mib: PositiveInt


class GatewayReleaseLimits(_ClosedContract):
    """Frozen Gateway concurrency limits and their inclusive boundary rule."""

    max_inflight_requests: PositiveInt
    runtime_pool_max_connections_per_instance: PositiveInt
    boundary_semantics: Literal["reject_when_current_is_greater_than_or_equal_to_limit"]


class DataRetention(_ClosedContract):
    """Frozen metadata and telemetry retention periods."""

    request_records_days: PositiveInt
    download_tasks_days: PositiveInt
    telemetry_raw_seconds: PositiveInt
    telemetry_aggregate_days: PositiveInt


DottedVersion = Annotated[str, Field(strict=True, pattern=r"^[0-9]+(?:\.[0-9]+){0,3}$")]


class HostRequirements(_ClosedContract):
    """Optional host minimums; omitted values remain undeclared."""

    nvidia_driver: DottedVersion | None = None
    docker: DottedVersion | None = None
    compose: DottedVersion | None = None
    nvidia_container_toolkit: DottedVersion | None = None


# Shared release policy; keep the optional HostRequirements contract unchanged.
DEFAULT_HOST_REQUIREMENTS: Final[HostRequirements] = HostRequirements(
    nvidia_driver="580.65.06",
    docker="24.0",
    compose="2.20",
    nvidia_container_toolkit="1.16",
)


class ReleaseManifest(_ClosedContract):
    """Machine-readable release versions and compatibility metadata."""

    schema_version: PositiveInt
    build_version: ConcreteString
    commit: GitCommit
    image_digests: tuple[NamedDigest, ...] = Field(min_length=1)
    artifact_hashes: ReleaseArtifactHashes
    service_identities: tuple[ServiceIdentity, ...] = Field(min_length=1)
    default_resource_limits: tuple[ResourceLimits, ...] = Field(min_length=1)
    runtime_compat_hash: Sha256
    python_lockfile_hash: Sha256
    node_lockfile_hash: Sha256
    gateway: GatewayReleaseLimits
    retention: DataRetention
    host_requirements: HostRequirements | None = None
    default_timezone: ConcreteString

    @field_validator("default_timezone")
    @classmethod
    def validate_default_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as error:
            raise ValueError("default_timezone must be an installed IANA timezone") from error
        return value

    @model_validator(mode="after")
    def validate_named_collections(self) -> ReleaseManifest:
        collections = (
            ("image_digests", tuple(item.name for item in self.image_digests)),
            (
                "artifact_hashes.systemd_units",
                tuple(item.name for item in self.artifact_hashes.systemd_units),
            ),
            ("service_identities", tuple(item.service for item in self.service_identities)),
            (
                "default_resource_limits",
                tuple(item.service for item in self.default_resource_limits),
            ),
        )
        for field_name, names in collections:
            if len(names) != len(set(names)):
                raise ValueError(f"{field_name} names must be unique")
        return self


class VersionPayload(_ClosedContract):
    """Frozen response body for the future ``/internal/version`` endpoint."""

    build_version: ConcreteString
    commit: GitCommit
    schema_version: PositiveInt
    runtime_compat_hash: Sha256


def version_payload_from_manifest(manifest: ReleaseManifest) -> VersionPayload:
    """Project version fields from the validated Release Manifest source of truth."""
    return VersionPayload(
        build_version=manifest.build_version,
        commit=manifest.commit,
        schema_version=manifest.schema_version,
        runtime_compat_hash=manifest.runtime_compat_hash,
    )
