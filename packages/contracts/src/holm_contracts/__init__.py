"""Pydantic and OpenAPI boundary contracts."""

from holm_contracts.errors import (
    ErrorBody,
    ErrorEnvelope,
    error_envelope_openapi_components,
)
from holm_contracts.events import DomainEventEnvelope
from holm_contracts.pagination import CursorPage, CursorPageParams
from holm_contracts.release import (
    DataRetention,
    GatewayReleaseLimits,
    NamedDigest,
    NamedHash,
    ReleaseArtifactHashes,
    ReleaseManifest,
    ResourceLimits,
    ServiceIdentity,
    VersionPayload,
    version_payload_from_manifest,
)

PACKAGE_NAME = "holm-contracts"

__all__ = [
    "PACKAGE_NAME",
    "CursorPage",
    "CursorPageParams",
    "DomainEventEnvelope",
    "ErrorBody",
    "ErrorEnvelope",
    "DataRetention",
    "GatewayReleaseLimits",
    "NamedDigest",
    "NamedHash",
    "ReleaseArtifactHashes",
    "ReleaseManifest",
    "ResourceLimits",
    "ServiceIdentity",
    "VersionPayload",
    "error_envelope_openapi_components",
    "version_payload_from_manifest",
]
