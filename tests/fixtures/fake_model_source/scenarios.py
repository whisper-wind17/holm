"""Construct FX-MS-01 through FX-MS-07 without network or disk side effects."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from .models import (
    GIB_BYTES,
    AccessOutcome,
    CredentialCase,
    CredentialReference,
    CredentialScenario,
    DescribedEntryKind,
    IncompleteMetadataScenario,
    InterruptionKind,
    Knowledge,
    KnownValue,
    MetadataField,
    ModelMetadata,
    OriginResumeRequest,
    PathAttackKind,
    PathAttackSample,
    RemoteFileDescriptor,
    RemoteFileRole,
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

_SHARED_COMMIT: Final = "1" * 40
_NEXT_COMMIT: Final = "2" * 40

_SHARDS: Final = tuple(
    RemoteFileDescriptor(
        path=f"model-{ordinal:05d}-of-00004.safetensors",
        logical_size_bytes=3 * GIB_BYTES,
        role=RemoteFileRole.SHARD,
    )
    for ordinal in range(1, 5)
)
_SHARD_INDEX: Final = ShardIndexDescriptor(
    path="model.safetensors.index.json",
    shard_paths=tuple(shard.path for shard in _SHARDS),
    tensor_to_shard=MappingProxyType(
        {f"fixture.layer.{ordinal}": shard.path for ordinal, shard in enumerate(_SHARDS)}
    ),
)
_SMALL_REPOSITORY_FILES: Final = (
    RemoteFileDescriptor(
        path="config.json",
        logical_size_bytes=2_048,
        role=RemoteFileRole.CONFIG,
    ),
    RemoteFileDescriptor(
        path="tokenizer.json",
        logical_size_bytes=8_192,
        role=RemoteFileRole.TOKENIZER,
    ),
    RemoteFileDescriptor(
        path=_SHARD_INDEX.path,
        logical_size_bytes=4_096,
        role=RemoteFileRole.SHARD_INDEX,
    ),
)
_REPOSITORY_FILES: Final = _SMALL_REPOSITORY_FILES + _SHARDS
_SYNTHETIC_LOGICAL_SIZE_BYTES: Final = sum(
    descriptor.logical_size_bytes for descriptor in _REPOSITORY_FILES
)


def revision_scenario() -> RevisionScenario:
    """Build branches/tags where distinct references may share one commit."""

    return RevisionScenario(
        repository_id="synthetic-owner/revision-fixture",
        aliases={
            "stable": _SHARED_COMMIT,
            "v1.0": _SHARED_COMMIT,
            "next": _NEXT_COMMIT,
        },
        reference_kinds={
            "stable": RevisionReferenceKind.BRANCH,
            "v1.0": RevisionReferenceKind.TAG,
            "next": RevisionReferenceKind.BRANCH,
        },
        default_reference="stable",
    )


def large_repository_scenario(
    *,
    file_list_known: bool,
    total_size_known: bool,
) -> SyntheticLargeRepository:
    """Configure independently known/unknown remote metadata for a logical 12 GiB model."""

    reported_file_list: Knowledge[tuple[RemoteFileDescriptor, ...]]
    reported_total_size: Knowledge[int]
    reported_file_list = KnownValue(_REPOSITORY_FILES) if file_list_known else UnknownValue()
    reported_total_size = (
        KnownValue(_SYNTHETIC_LOGICAL_SIZE_BYTES) if total_size_known else UnknownValue()
    )
    return SyntheticLargeRepository(
        repository_id="synthetic-owner/large-sharded-fixture",
        synthetic_logical_size_bytes=_SYNTHETIC_LOGICAL_SIZE_BYTES,
        shards=_SHARDS,
        shard_index=_SHARD_INDEX,
        reported_file_list=reported_file_list,
        reported_total_size_bytes=reported_total_size,
    )


def resume_scenarios() -> tuple[ResumeScenario, ...]:
    """Build independent transfer interruption, process kill, and disconnection probes."""

    reusable_entries = (
        ReusableStagingEntry(
            relative_path="partial/model-00002-of-00004.safetensors.partial",
            kind=ReusableEntryKind.PARTIAL,
            retained_bytes=GIB_BYTES,
        ),
        ReusableStagingEntry(
            relative_path="cache/model-00003-of-00004.safetensors.cache",
            kind=ReusableEntryKind.CACHE,
            retained_bytes=512 * 1024**2,
        ),
    )
    resume_requests = (
        OriginResumeRequest(
            path="model-00002-of-00004.safetensors",
            start_offset_bytes=GIB_BYTES,
        ),
        OriginResumeRequest(
            path="model-00003-of-00004.safetensors",
            start_offset_bytes=512 * 1024**2,
        ),
        OriginResumeRequest(
            path="model-00004-of-00004.safetensors",
            start_offset_bytes=0,
        ),
    )
    return tuple(
        ResumeScenario(
            interruption=interruption,
            completed_file_paths=("config.json", "model-00001-of-00004.safetensors"),
            reusable_staging_entries=reusable_entries,
            origin_requests_after_resume=resume_requests,
        )
        for interruption in InterruptionKind
    )


_CREDENTIAL_SCENARIOS: Final[Mapping[CredentialCase, CredentialScenario]] = MappingProxyType(
    {
        CredentialCase.PUBLIC: CredentialScenario(
            case=CredentialCase.PUBLIC,
            credential_reference=CredentialReference.NOT_SUPPLIED,
            outcome=AccessOutcome.ACCESSIBLE,
        ),
        CredentialCase.PRIVATE_ACCESSIBLE: CredentialScenario(
            case=CredentialCase.PRIVATE_ACCESSIBLE,
            credential_reference=CredentialReference.SYNTHETIC_AUTHORIZED,
            outcome=AccessOutcome.ACCESSIBLE,
        ),
        CredentialCase.INVALID_CREDENTIAL: CredentialScenario(
            case=CredentialCase.INVALID_CREDENTIAL,
            credential_reference=CredentialReference.SYNTHETIC_INVALID,
            outcome=AccessOutcome.INVALID_CREDENTIAL,
        ),
        CredentialCase.EXPIRED_CREDENTIAL: CredentialScenario(
            case=CredentialCase.EXPIRED_CREDENTIAL,
            credential_reference=CredentialReference.SYNTHETIC_EXPIRED,
            outcome=AccessOutcome.INVALID_CREDENTIAL,
        ),
        CredentialCase.PERMISSION_DENIED: CredentialScenario(
            case=CredentialCase.PERMISSION_DENIED,
            credential_reference=CredentialReference.SYNTHETIC_UNAUTHORIZED,
            outcome=AccessOutcome.PERMISSION_DENIED,
        ),
        CredentialCase.REPOSITORY_NOT_FOUND: CredentialScenario(
            case=CredentialCase.REPOSITORY_NOT_FOUND,
            credential_reference=CredentialReference.SYNTHETIC_AUTHORIZED,
            outcome=AccessOutcome.NOT_FOUND,
        ),
        CredentialCase.ANONYMOUS_INDISTINGUISHABLE: CredentialScenario(
            case=CredentialCase.ANONYMOUS_INDISTINGUISHABLE,
            credential_reference=CredentialReference.NOT_SUPPLIED,
            # Explicitly no verification status: "missing or invisible, and the
            # platform cannot tell which" is a conclusion no
            # ``ModelSourceValidationStatus`` carries (see ``AccessOutcome``).
            outcome=None,
        ),
        CredentialCase.RATE_LIMITED: CredentialScenario(
            case=CredentialCase.RATE_LIMITED,
            credential_reference=CredentialReference.SYNTHETIC_AUTHORIZED,
            outcome=AccessOutcome.RATE_LIMITED,
        ),
    }
)


def credential_scenario(case: CredentialCase) -> CredentialScenario:
    """Return one immutable behavior category without returning credential material."""

    return _CREDENTIAL_SCENARIOS[case]


_VALIDATION_FAILURES: Final[Mapping[ValidationFailureKind, ValidationFailureSample]] = (
    MappingProxyType(
        {
            ValidationFailureKind.HASH_MISMATCH: ValidationFailureSample(
                kind=ValidationFailureKind.HASH_MISMATCH,
                affected_paths=("model-00001-of-00004.safetensors",),
                expected_sha256=f"sha256:{'a' * 64}",
                observed_sha256=f"sha256:{'b' * 64}",
            ),
            ValidationFailureKind.SHARD_MISSING: ValidationFailureSample(
                kind=ValidationFailureKind.SHARD_MISSING,
                affected_paths=("model-00003-of-00004.safetensors",),
            ),
            ValidationFailureKind.SAFETENSORS_HEADER_CORRUPT: ValidationFailureSample(
                kind=ValidationFailureKind.SAFETENSORS_HEADER_CORRUPT,
                affected_paths=("model-00002-of-00004.safetensors",),
                header_state=SafetensorsHeaderState.CORRUPT,
            ),
            ValidationFailureKind.CONFIG_OR_TOKENIZER_MISSING: ValidationFailureSample(
                kind=ValidationFailureKind.CONFIG_OR_TOKENIZER_MISSING,
                affected_paths=("config.json", "tokenizer.json"),
            ),
        }
    )
)


def validation_failure_sample(kind: ValidationFailureKind) -> ValidationFailureSample:
    """Return one of the four independently asserted validation failures."""

    return _VALIDATION_FAILURES[kind]


_PATH_ATTACK_SAMPLES: Final = (
    PathAttackSample(
        kind=PathAttackKind.PARENT_TRAVERSAL,
        untrusted_value="weights/../../synthetic-outside",
        entry_kind=DescribedEntryKind.REGULAR_FILE,
    ),
    PathAttackSample(
        kind=PathAttackKind.ABSOLUTE_PATH,
        untrusted_value="/synthetic/absolute-entry",
        entry_kind=DescribedEntryKind.REGULAR_FILE,
    ),
    PathAttackSample(
        kind=PathAttackKind.ESCAPING_SYMBOLIC_LINK,
        untrusted_value="weights/escape-link",
        entry_kind=DescribedEntryKind.SYMBOLIC_LINK,
        symbolic_link_target="../../synthetic-outside",
    ),
    PathAttackSample(
        kind=PathAttackKind.DEVICE_FILE,
        untrusted_value="weights/described-device",
        entry_kind=DescribedEntryKind.DEVICE,
    ),
    PathAttackSample(
        kind=PathAttackKind.FIFO,
        untrusted_value="weights/described-fifo",
        entry_kind=DescribedEntryKind.FIFO,
    ),
    PathAttackSample(
        kind=PathAttackKind.SOCKET,
        untrusted_value="weights/described-socket",
        entry_kind=DescribedEntryKind.SOCKET,
    ),
    PathAttackSample(
        kind=PathAttackKind.SETUID_FILE,
        untrusted_value="weights/described-setuid",
        entry_kind=DescribedEntryKind.REGULAR_FILE,
        mode_bits=0o4755,
    ),
    PathAttackSample(
        kind=PathAttackKind.INVALID_REPOSITORY_ID,
        untrusted_value="synthetic-owner//invalid-repository",
        entry_kind=None,
    ),
)


def path_attack_samples() -> tuple[PathAttackSample, ...]:
    """Return the descriptions; building them creates nothing on disk.

    ``materialization.materialize_path_attacks`` is the one way an entry is
    created, and only inside a directory its caller supplies.
    """

    return _PATH_ATTACK_SAMPLES


def incomplete_metadata_scenarios() -> tuple[IncompleteMetadataScenario, ...]:
    """Build one explicit unrecognized state for each required metadata field."""

    scenarios: list[IncompleteMetadataScenario] = []
    for unknown_field in MetadataField:
        architecture: Knowledge[str] = (
            UnknownValue()
            if unknown_field is MetadataField.ARCHITECTURE
            else KnownValue("SyntheticCausalArchitecture")
        )
        modality: Knowledge[str] = (
            UnknownValue() if unknown_field is MetadataField.MODALITY else KnownValue("text")
        )
        precision: Knowledge[str] = (
            UnknownValue() if unknown_field is MetadataField.PRECISION else KnownValue("bf16")
        )
        context: Knowledge[int] = (
            UnknownValue() if unknown_field is MetadataField.CONTEXT else KnownValue(32_768)
        )
        scenarios.append(
            IncompleteMetadataScenario(
                unknown_field=unknown_field,
                metadata=ModelMetadata(
                    architecture=architecture,
                    modality=modality,
                    precision=precision,
                    context=context,
                ),
            )
        )
    return tuple(scenarios)
