"""Contract checks for all Fake Model Source capabilities in TST section 3.4."""

from __future__ import annotations

import stat
from dataclasses import fields
from pathlib import Path
from typing import Protocol

import pytest
from fixtures.fake_model_source import (
    FIXTURE_REVIEW_STATUS,
    MINIMUM_LARGE_REPOSITORY_BYTES,
    AccessOutcome,
    CredentialCase,
    CredentialReference,
    CredentialScenario,
    DescribedEntryKind,
    FixtureId,
    InterruptionKind,
    KnowledgeState,
    KnownValue,
    MaterializationBlockedReason,
    MaterializationOutcome,
    MaterializedPathAttack,
    MetadataField,
    PathAttackKind,
    PathAttackSample,
    RemoteFileRole,
    ReusableEntryKind,
    RevisionReferenceKind,
    SafetensorsHeaderState,
    UnknownValue,
    ValidationFailureKind,
    credential_scenario,
    described_entry_kind_matches,
    incomplete_metadata_scenarios,
    large_repository_scenario,
    materialize_path_attack,
    path_attack_samples,
    resume_scenarios,
    revision_scenario,
    validation_failure_sample,
)

_MAX_FIXTURE_FILE_BYTES = 512 * 1024


class _FixtureScenario(Protocol):
    fixture_id: FixtureId
    review_status: str


def _assert_fixture(scenario: _FixtureScenario, fixture_id: FixtureId) -> None:
    assert scenario.fixture_id is fixture_id
    assert scenario.review_status == FIXTURE_REVIEW_STATUS == "待真机复核"


def _attack_sample(kind: PathAttackKind) -> PathAttackSample:
    matches = tuple(sample for sample in path_attack_samples() if sample.kind is kind)
    assert len(matches) == 1
    sample = matches[0]
    _assert_fixture(sample, FixtureId.FX_MS_06)
    assert sample.description_only
    return sample


def test_FX_MS_01_branch_and_tag_independently_resolve_to_the_same_immutable_commit() -> None:
    scenario = revision_scenario()
    _assert_fixture(scenario, FixtureId.FX_MS_01)

    branch = scenario.resolve("stable")
    tag = scenario.resolve("v1.0")

    assert scenario.reference_kinds["stable"] is RevisionReferenceKind.BRANCH
    assert scenario.reference_kinds["v1.0"] is RevisionReferenceKind.TAG
    assert branch.requested_revision == "stable"
    assert tag.requested_revision == "v1.0"
    assert branch.resolved_commit == tag.resolved_commit
    assert len(branch.resolved_commit) == 40
    assert set(branch.resolved_commit) <= set("0123456789abcdef")
    assert scenario.resolve("next").resolved_commit != branch.resolved_commit


def test_FX_MS_01_omitted_revision_is_normalized_to_default_before_resolution() -> None:
    scenario = revision_scenario()
    _assert_fixture(scenario, FixtureId.FX_MS_01)

    omitted = scenario.resolve(None)
    explicit_default_branch = scenario.resolve(scenario.default_reference)

    assert omitted.requested_revision == "@default"
    assert omitted.resolved_commit == explicit_default_branch.resolved_commit
    with pytest.raises(ValueError, match="empty revision"):
        scenario.resolve("")


def test_FX_MS_02_large_repository_is_metadata_only_with_multiple_shards_and_index() -> None:
    scenario = large_repository_scenario(file_list_known=True, total_size_known=True)
    _assert_fixture(scenario, FixtureId.FX_MS_02)

    assert scenario.synthetic_logical_size_bytes >= MINIMUM_LARGE_REPOSITORY_BYTES
    assert scenario.materialized_payload_bytes == 0
    assert len(scenario.shards) == 4
    assert all(shard.role is RemoteFileRole.SHARD for shard in scenario.shards)
    assert scenario.shard_index.shard_paths == tuple(shard.path for shard in scenario.shards)
    assert set(scenario.shard_index.tensor_to_shard.values()) == set(
        scenario.shard_index.shard_paths
    )
    assert isinstance(scenario.reported_file_list, KnownValue)
    assert isinstance(scenario.reported_total_size_bytes, KnownValue)
    assert scenario.reported_total_size_bytes.value == scenario.synthetic_logical_size_bytes


def test_FX_MS_02_file_list_can_be_explicitly_unknown_while_total_size_is_known() -> None:
    scenario = large_repository_scenario(file_list_known=False, total_size_known=True)
    _assert_fixture(scenario, FixtureId.FX_MS_02)

    assert isinstance(scenario.reported_file_list, UnknownValue)
    assert scenario.reported_file_list.state is KnowledgeState.UNRECOGNIZED
    assert isinstance(scenario.reported_total_size_bytes, KnownValue)


def test_FX_MS_02_total_size_can_be_explicitly_unknown_while_file_list_is_known() -> None:
    scenario = large_repository_scenario(file_list_known=True, total_size_known=False)
    _assert_fixture(scenario, FixtureId.FX_MS_02)

    assert isinstance(scenario.reported_file_list, KnownValue)
    assert isinstance(scenario.reported_total_size_bytes, UnknownValue)
    assert scenario.reported_total_size_bytes.state is KnowledgeState.UNRECOGNIZED


def test_FX_MS_02_file_list_and_total_size_can_both_be_explicitly_unknown() -> None:
    scenario = large_repository_scenario(file_list_known=False, total_size_known=False)
    _assert_fixture(scenario, FixtureId.FX_MS_02)

    assert isinstance(scenario.reported_file_list, UnknownValue)
    assert isinstance(scenario.reported_total_size_bytes, UnknownValue)
    assert scenario.materialized_payload_bytes == 0


_RESUME_SCENARIOS = {scenario.interruption: scenario for scenario in resume_scenarios()}


@pytest.mark.parametrize(
    "interruption",
    tuple(InterruptionKind),
    ids=("transfer-interrupted", "process-killed", "network-disconnected"),
)
def test_FX_MS_03_each_interruption_reuses_partial_and_cache_without_completed_retransmit(
    interruption: InterruptionKind,
) -> None:
    scenario = _RESUME_SCENARIOS[interruption]
    _assert_fixture(scenario, FixtureId.FX_MS_03)

    assert scenario.interruption is interruption
    assert {entry.kind for entry in scenario.reusable_staging_entries} == {
        ReusableEntryKind.PARTIAL,
        ReusableEntryKind.CACHE,
    }
    assert all(entry.retained_bytes > 0 for entry in scenario.reusable_staging_entries)
    resume_offsets = {
        request.path: request.start_offset_bytes
        for request in scenario.origin_requests_after_resume
    }
    assert resume_offsets["model-00002-of-00004.safetensors"] > 0
    assert resume_offsets["model-00003-of-00004.safetensors"] > 0
    assert resume_offsets["model-00004-of-00004.safetensors"] == 0
    assert set(scenario.completed_file_paths).isdisjoint(resume_offsets)
    assert scenario.retransmitted_completed_file_paths == ()


def test_FX_MS_04_public_repository_is_accessible_without_credentials() -> None:
    scenario = credential_scenario(CredentialCase.PUBLIC)
    _assert_fixture(scenario, FixtureId.FX_MS_04)

    assert scenario.credential_reference is CredentialReference.NOT_SUPPLIED
    assert scenario.outcome is AccessOutcome.ACCESSIBLE


def test_FX_MS_04_private_repository_is_accessible_with_synthetic_reference() -> None:
    scenario = credential_scenario(CredentialCase.PRIVATE_ACCESSIBLE)
    _assert_fixture(scenario, FixtureId.FX_MS_04)

    assert scenario.credential_reference is CredentialReference.SYNTHETIC_AUTHORIZED
    assert scenario.outcome is AccessOutcome.ACCESSIBLE


def test_FX_MS_04_incorrect_credential_has_an_independent_invalid_outcome() -> None:
    scenario = credential_scenario(CredentialCase.INVALID_CREDENTIAL)
    _assert_fixture(scenario, FixtureId.FX_MS_04)

    assert scenario.credential_reference is CredentialReference.SYNTHETIC_INVALID
    assert scenario.outcome is AccessOutcome.INVALID_CREDENTIAL


def test_FX_MS_04_expired_credential_is_independently_injectable_but_observably_invalid() -> None:
    invalid = credential_scenario(CredentialCase.INVALID_CREDENTIAL)
    expired = credential_scenario(CredentialCase.EXPIRED_CREDENTIAL)
    _assert_fixture(expired, FixtureId.FX_MS_04)

    assert expired.credential_reference is CredentialReference.SYNTHETIC_EXPIRED
    assert expired.case is not invalid.case
    assert expired.outcome is invalid.outcome is AccessOutcome.INVALID_CREDENTIAL


def test_FX_MS_04_missing_permission_has_an_independent_denied_outcome() -> None:
    scenario = credential_scenario(CredentialCase.PERMISSION_DENIED)
    _assert_fixture(scenario, FixtureId.FX_MS_04)

    assert scenario.credential_reference is CredentialReference.SYNTHETIC_UNAUTHORIZED
    assert scenario.outcome is AccessOutcome.PERMISSION_DENIED


def test_FX_MS_04_missing_repository_has_an_independent_not_found_outcome() -> None:
    scenario = credential_scenario(CredentialCase.REPOSITORY_NOT_FOUND)
    _assert_fixture(scenario, FixtureId.FX_MS_04)

    assert scenario.credential_reference is CredentialReference.SYNTHETIC_AUTHORIZED
    assert scenario.outcome is AccessOutcome.NOT_FOUND


def test_FX_MS_04_anonymous_missing_or_invisible_is_one_explicit_outcome() -> None:
    scenario = credential_scenario(CredentialCase.ANONYMOUS_INDISTINGUISHABLE)
    _assert_fixture(scenario, FixtureId.FX_MS_04)

    assert scenario.credential_reference is CredentialReference.NOT_SUPPLIED
    # Explicitly no verification status: no ``AccessOutcome`` can carry this
    # conclusion, and ``None`` here says so rather than borrowing ``NOT_FOUND``.
    assert scenario.outcome is None
    assert scenario.outcome not in set(AccessOutcome)


def test_FX_MS_04_rate_limit_has_an_independent_limited_outcome() -> None:
    scenario = credential_scenario(CredentialCase.RATE_LIMITED)
    _assert_fixture(scenario, FixtureId.FX_MS_04)

    assert scenario.credential_reference is CredentialReference.SYNTHETIC_AUTHORIZED
    assert scenario.outcome is AccessOutcome.RATE_LIMITED


def test_FX_MS_04_scenarios_store_only_opaque_states_and_no_credential_material() -> None:
    assert {field_definition.name for field_definition in fields(CredentialScenario)} == {
        "case",
        "credential_reference",
        "outcome",
        "fixture_id",
        "review_status",
    }
    assert {credential_scenario(case).case for case in CredentialCase} == set(CredentialCase)


def test_FX_MS_05_hash_mismatch_is_an_independent_failure_sample() -> None:
    sample = validation_failure_sample(ValidationFailureKind.HASH_MISMATCH)
    _assert_fixture(sample, FixtureId.FX_MS_05)

    assert sample.kind is ValidationFailureKind.HASH_MISMATCH
    assert sample.expected_sha256 is not None
    assert sample.observed_sha256 is not None
    assert sample.expected_sha256 != sample.observed_sha256
    assert sample.affected_paths == ("model-00001-of-00004.safetensors",)


def test_FX_MS_05_missing_shard_is_an_independent_failure_sample() -> None:
    sample = validation_failure_sample(ValidationFailureKind.SHARD_MISSING)
    repository = large_repository_scenario(file_list_known=True, total_size_known=True)
    _assert_fixture(sample, FixtureId.FX_MS_05)

    assert sample.kind is ValidationFailureKind.SHARD_MISSING
    assert sample.affected_paths == ("model-00003-of-00004.safetensors",)
    assert set(sample.affected_paths).issubset(repository.shard_index.shard_paths)


def test_FX_MS_05_corrupt_safetensors_header_is_an_independent_failure_sample() -> None:
    sample = validation_failure_sample(ValidationFailureKind.SAFETENSORS_HEADER_CORRUPT)
    _assert_fixture(sample, FixtureId.FX_MS_05)

    assert sample.kind is ValidationFailureKind.SAFETENSORS_HEADER_CORRUPT
    assert sample.header_state is SafetensorsHeaderState.CORRUPT
    assert sample.affected_paths[0].endswith(".safetensors")


def test_FX_MS_05_missing_config_and_tokenizer_is_an_independent_failure_sample() -> None:
    sample = validation_failure_sample(ValidationFailureKind.CONFIG_OR_TOKENIZER_MISSING)
    _assert_fixture(sample, FixtureId.FX_MS_05)

    assert sample.kind is ValidationFailureKind.CONFIG_OR_TOKENIZER_MISSING
    assert set(sample.affected_paths) == {"config.json", "tokenizer.json"}


def test_FX_MS_06_parent_traversal_is_description_only() -> None:
    sample = _attack_sample(PathAttackKind.PARENT_TRAVERSAL)

    assert ".." in sample.untrusted_value.split("/")
    assert sample.entry_kind is DescribedEntryKind.REGULAR_FILE


def test_FX_MS_06_absolute_path_is_description_only() -> None:
    sample = _attack_sample(PathAttackKind.ABSOLUTE_PATH)

    assert sample.untrusted_value.startswith("/")
    assert sample.entry_kind is DescribedEntryKind.REGULAR_FILE


def test_FX_MS_06_escaping_symbolic_link_is_description_only() -> None:
    sample = _attack_sample(PathAttackKind.ESCAPING_SYMBOLIC_LINK)

    assert sample.entry_kind is DescribedEntryKind.SYMBOLIC_LINK
    assert sample.symbolic_link_target is not None
    assert ".." in sample.symbolic_link_target.split("/")


def test_FX_MS_06_device_file_is_description_only() -> None:
    sample = _attack_sample(PathAttackKind.DEVICE_FILE)

    assert sample.entry_kind is DescribedEntryKind.DEVICE


def test_FX_MS_06_FIFO_is_description_only() -> None:
    sample = _attack_sample(PathAttackKind.FIFO)

    assert sample.entry_kind is DescribedEntryKind.FIFO


def test_FX_MS_06_Socket_is_description_only() -> None:
    sample = _attack_sample(PathAttackKind.SOCKET)

    assert sample.entry_kind is DescribedEntryKind.SOCKET


def test_FX_MS_06_Setuid_file_is_description_only() -> None:
    sample = _attack_sample(PathAttackKind.SETUID_FILE)

    assert sample.entry_kind is DescribedEntryKind.REGULAR_FILE
    assert sample.mode_bits is not None
    assert sample.mode_bits & stat.S_ISUID


def test_FX_MS_06_invalid_repository_ID_is_description_only() -> None:
    sample = _attack_sample(PathAttackKind.INVALID_REPOSITORY_ID)

    assert "//" in sample.untrusted_value
    assert sample.entry_kind is None


def test_FX_MS_06_fixture_tree_contains_only_small_regular_files_and_directories() -> None:
    fixture_root = Path(__file__).parents[1] / "fixtures" / "fake_model_source"

    assert fixture_root.is_dir()
    for entry in fixture_root.rglob("*"):
        mode = entry.lstat().st_mode
        assert not stat.S_ISLNK(mode), entry
        assert stat.S_ISREG(mode) or stat.S_ISDIR(mode), entry
        assert mode & stat.S_ISUID == 0, entry
        if stat.S_ISREG(mode):
            assert entry.stat().st_size <= _MAX_FIXTURE_FILE_BYTES, entry


_INCOMPLETE_METADATA_SCENARIOS = {
    scenario.unknown_field: scenario for scenario in incomplete_metadata_scenarios()
}


@pytest.mark.parametrize(
    "unknown_field",
    tuple(MetadataField),
    ids=("architecture", "modality", "precision", "context"),
)
def test_FX_MS_07_each_missing_field_remains_explicitly_unrecognized_without_a_guess(
    unknown_field: MetadataField,
) -> None:
    scenario = _INCOMPLETE_METADATA_SCENARIOS[unknown_field]
    _assert_fixture(scenario, FixtureId.FX_MS_07)

    unknown = scenario.metadata.value_for(unknown_field)
    assert isinstance(unknown, UnknownValue)
    assert unknown.state is KnowledgeState.UNRECOGNIZED
    assert not hasattr(unknown, "value")
    for identified_field in MetadataField:
        if identified_field is unknown_field:
            continue
        identified = scenario.metadata.value_for(identified_field)
        assert isinstance(identified, KnownValue)
        assert identified.value is not None
        assert identified.value != ""
        assert identified.value != "unknown"


# ------------------------------------------------- FX-MS-06 materialization
FILESYSTEM_ATTACK_KINDS = tuple(
    sorted(kind for kind in PathAttackKind if kind is not PathAttackKind.INVALID_REPOSITORY_ID)
)


@pytest.mark.parametrize("kind", FILESYSTEM_ATTACK_KINDS)
def test_FX_MS_06_a_description_becomes_the_entry_it_describes_or_is_suspended(
    kind: PathAttackKind,
    tmp_path: Path,
) -> None:
    """Materializing produces the described kind, or reports why it could not."""

    materialized = materialize_path_attack(_attack_sample(kind), tmp_path)

    if materialized.outcome is MaterializationOutcome.BLOCKED_ENV:
        assert materialized.blocked_reason is not None
        assert materialized.attack_path is None
        assert materialized.allowed_root is None
        pytest.xfail(
            f"BLOCKED_ENV: FX-MS-06 {kind.value} cannot be created here "
            f"({materialized.blocked_reason.value})"
        )

    assert materialized.outcome is MaterializationOutcome.MATERIALIZED
    assert materialized.attack_path is not None
    assert materialized.allowed_root is not None
    assert described_entry_kind_matches(materialized), "the entry is not the described kind"
    # Everything created stays inside the caller's directory; the real default
    # External Root is never touched.
    assert tmp_path in materialized.attack_path.parents
    assert tmp_path in materialized.allowed_root.parents


def test_FX_MS_06_the_repository_id_sample_is_not_a_filesystem_entry(tmp_path: Path) -> None:
    materialized = materialize_path_attack(
        _attack_sample(PathAttackKind.INVALID_REPOSITORY_ID), tmp_path
    )

    assert materialized.outcome is MaterializationOutcome.NOT_A_FILESYSTEM_ENTRY
    assert materialized.attack_path is None
    assert materialized.blocked_reason is None
    assert list(tmp_path.iterdir()) == []


def test_FX_MS_06_a_suspended_sample_can_never_be_read_as_a_created_one() -> None:
    """The blocked state cannot carry a path, and a created one cannot lack it."""

    sample = _attack_sample(PathAttackKind.FIFO)
    blocked = MaterializedPathAttack(
        sample=sample,
        outcome=MaterializationOutcome.BLOCKED_ENV,
        blocked_reason=MaterializationBlockedReason.PRIVILEGE_REQUIRED,
    )
    assert blocked.attack_path is None
    assert blocked.fixture_id is FixtureId.FX_MS_06

    with pytest.raises(ValueError):
        MaterializedPathAttack(
            sample=sample,
            outcome=MaterializationOutcome.BLOCKED_ENV,
            allowed_root=Path("/synthetic/root"),
            attack_path=Path("/synthetic/root/entry"),
            blocked_reason=MaterializationBlockedReason.PRIVILEGE_REQUIRED,
        )
    with pytest.raises(ValueError):
        MaterializedPathAttack(sample=sample, outcome=MaterializationOutcome.MATERIALIZED)
    with pytest.raises(ValueError):
        MaterializedPathAttack(sample=sample, outcome=MaterializationOutcome.BLOCKED_ENV)
