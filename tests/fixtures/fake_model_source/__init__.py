"""Standard-library Fake Model Source fixture for M0-06E.

All scenarios are ``待真机复核``.  This package is test-only and performs no
network requests, model downloads, validation, publication or staging.

Constructing a scenario still touches no filesystem.  The one deliberate
exception is ``materialization``: since M2-07A a caller may ask it to project
an ``FX-MS-06`` path-attack *description* onto real entries inside a directory
the caller owns, so that the External Path judgement can be shown refusing an
escaping symbolic link, a FIFO or a setuid file that actually exists.  Nothing
is created unless that function is called, and never outside the given base
directory.
"""

from .anonymous_visibility import (
    ANONYMOUS_NOT_FOUND_OBSERVABLE,
    anonymous_not_found_observable,
)
from .materialization import (
    MaterializationBlockedReason,
    MaterializationOutcome,
    MaterializedPathAttack,
    MaterializedValidationFailure,
    MaterializedValidationFile,
    described_entry_kind_matches,
    materialize_path_attack,
    materialize_path_attacks,
    materialize_validation_failure,
)
from .models import (
    FIXTURE_REVIEW_STATUS,
    GIB_BYTES,
    MINIMUM_LARGE_REPOSITORY_BYTES,
    AccessOutcome,
    CredentialCase,
    CredentialReference,
    CredentialScenario,
    DescribedEntryKind,
    FixtureId,
    IncompleteMetadataScenario,
    InterruptionKind,
    KnowledgeState,
    KnownValue,
    MetadataField,
    ModelMetadata,
    OriginResumeRequest,
    PathAttackKind,
    PathAttackSample,
    RemoteFileDescriptor,
    RemoteFileRole,
    ResolvedRevision,
    ResumeScenario,
    ReusableEntryKind,
    ReusableStagingEntry,
    RevisionReferenceKind,
    RevisionScenario,
    SafetensorsHeaderState,
    ShardIndexDescriptor,
    SyntheticLargeRepository,
    UnknownValue,
    ValidationFailureKind,
    ValidationFailureSample,
)
from .scenarios import (
    credential_scenario,
    incomplete_metadata_scenarios,
    large_repository_scenario,
    path_attack_samples,
    resume_scenarios,
    revision_scenario,
    validation_failure_sample,
)

__all__ = [
    "ANONYMOUS_NOT_FOUND_OBSERVABLE",
    "FIXTURE_REVIEW_STATUS",
    "GIB_BYTES",
    "MINIMUM_LARGE_REPOSITORY_BYTES",
    "AccessOutcome",
    "CredentialCase",
    "CredentialReference",
    "CredentialScenario",
    "DescribedEntryKind",
    "FixtureId",
    "IncompleteMetadataScenario",
    "InterruptionKind",
    "KnownValue",
    "KnowledgeState",
    "MetadataField",
    "ModelMetadata",
    "OriginResumeRequest",
    "PathAttackKind",
    "MaterializationBlockedReason",
    "MaterializationOutcome",
    "MaterializedPathAttack",
    "MaterializedValidationFailure",
    "MaterializedValidationFile",
    "PathAttackSample",
    "RemoteFileDescriptor",
    "RemoteFileRole",
    "ResolvedRevision",
    "ResumeScenario",
    "ReusableEntryKind",
    "ReusableStagingEntry",
    "RevisionReferenceKind",
    "RevisionScenario",
    "SafetensorsHeaderState",
    "ShardIndexDescriptor",
    "SyntheticLargeRepository",
    "UnknownValue",
    "ValidationFailureKind",
    "ValidationFailureSample",
    "credential_scenario",
    "incomplete_metadata_scenarios",
    "large_repository_scenario",
    "anonymous_not_found_observable",
    "described_entry_kind_matches",
    "materialize_path_attack",
    "materialize_path_attacks",
    "materialize_validation_failure",
    "path_attack_samples",
    "resume_scenarios",
    "revision_scenario",
    "validation_failure_sample",
]
