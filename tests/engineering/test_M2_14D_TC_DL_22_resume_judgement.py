"""M2-14D: the resume judgement itself fails loudly when it should.

The certification run is hours long and cannot be repeated to check its own
judge, so the judge is certified here on real files: both positive controls the
card names (a completed file deleted between phases, staging emptied between
phases) act on real snapshots, not on hand-written numbers.
"""

from __future__ import annotations

import os
from pathlib import Path

from certification.judgement import (
    FileState,
    PhaseObservation,
    StagingSnapshot,
    is_partial,
    resume_violations,
    snapshot,
)
from holm_download_worker.artifact_publishing import CACHEDIR_SIGNATURE

SMALL = b"s" * 64
SHARD = b"w" * 300


def _append(path: Path, payload: bytes) -> int:
    with path.open("ab") as stream:
        stream.write(payload)
    return len(payload)


def _complete_partial(staging: Path, name: str, remainder: bytes) -> int:
    partial = staging / f"{name}.incomplete"
    _append(partial, remainder)
    partial.rename(staging / name)
    return len(remainder)


def _start(staging: Path) -> None:
    staging.mkdir()
    (staging / "config.json").write_bytes(SMALL)
    (staging / "model.safetensors.incomplete").write_bytes(SHARD[:100])


def _phase(
    name: str, staging: Path, before: StagingSnapshot, delivered: int, *, done: bool
) -> PhaseObservation:
    return PhaseObservation(name, delivered, before, snapshot(staging), done)


def test_M2_14D_TC_DL_22_an_honest_two_phase_resume_has_no_violations(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    _start(staging)
    total = len(SMALL) + len(SHARD)

    before_one = snapshot(staging)
    first = _phase(
        "interrupted",
        staging,
        before_one,
        _append(staging / "model.safetensors.incomplete", SHARD[100:180]),
        done=False,
    )
    second = _phase(
        "resumed",
        staging,
        first.after,
        _complete_partial(staging, "model.safetensors", SHARD[180:]),
        done=True,
    )

    assert first.after.bytes_on_disk > before_one.bytes_on_disk
    assert resume_violations(first, previous_after=None, expected_total_bytes=total) == ()
    assert resume_violations(second, previous_after=first.after, expected_total_bytes=total) == ()


def test_M2_14D_TC_DL_22_positive_control_a_completed_file_fetched_again_fails(
    tmp_path: Path,
) -> None:
    """control 1: the byte ratio alone stays 1.0 here."""

    staging = tmp_path / "staging"
    _start(staging)
    total = len(SMALL) + len(SHARD)
    first = _phase("interrupted", staging, snapshot(staging), 0, done=False)

    (staging / "config.json").unlink()
    before_two = snapshot(staging)
    replacement = staging / "config.json.incomplete"
    replacement.write_bytes(SMALL)
    replacement.rename(staging / "config.json")
    delivered = len(SMALL) + _complete_partial(staging, "model.safetensors", SHARD[100:])
    second = _phase("resumed", staging, before_two, delivered, done=True)

    added = second.after.bytes_on_disk - second.before.bytes_on_disk
    assert added == second.delivered_bytes  # the ratio cannot see it ...
    violations = resume_violations(second, previous_after=first.after, expected_total_bytes=total)
    # ... the byte accounting can.  Not the file-identity layer: a file recreated
    # within one timestamp tick may get its inode and mtime back.
    assert any("started over" in value for value in violations), violations


def test_M2_14D_TC_DL_22_positive_control_staging_emptied_between_phases_fails(
    tmp_path: Path,
) -> None:
    """control 2: a full restart also scores a ratio of 1.0."""

    staging = tmp_path / "staging"
    _start(staging)
    total = len(SMALL) + len(SHARD)
    first = _phase("interrupted", staging, snapshot(staging), 0, done=False)

    for path in staging.iterdir():
        path.unlink()
    before_two = snapshot(staging)
    (staging / "config.json").write_bytes(SMALL)
    (staging / "model.safetensors").write_bytes(SHARD)
    second = _phase("resumed", staging, before_two, total, done=True)

    added = second.after.bytes_on_disk - second.before.bytes_on_disk
    assert added == second.delivered_bytes  # ... and neither can it see this
    violations = resume_violations(second, previous_after=first.after, expected_total_bytes=total)
    assert any("started over" in value for value in violations), violations


def test_M2_14D_TC_DL_22_positive_control_a_completed_file_rewritten_in_place_fails(
    tmp_path: Path,
) -> None:
    """The file-identity layer on its own: bytes rewritten but never reported."""

    staging = tmp_path / "staging"
    _start(staging)
    total = len(SMALL) + len(SHARD)
    before = snapshot(staging)
    original = before.complete["config.json"]

    (staging / "config.json").write_bytes(SMALL)
    later = original.mtime_ns + 5_000_000_000
    os.utime(staging / "config.json", ns=(later, later))
    phase = _phase("interrupted", staging, before, 0, done=False)

    violations = resume_violations(phase, previous_after=None, expected_total_bytes=total)
    assert phase.after.bytes_on_disk == before.bytes_on_disk  # accounting sees nothing
    assert any("completed file config.json changed" in value for value in violations), violations


def test_M2_14D_TC_DL_22_truncated_partial_reports_more_than_it_adds(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    _start(staging)
    total = len(SMALL) + len(SHARD)
    before = snapshot(staging)

    (staging / "model.safetensors.incomplete").write_bytes(b"")
    _append(staging / "model.safetensors.incomplete", SHARD[:150])
    phase = _phase("interrupted", staging, before, 150, done=False)

    violations = resume_violations(phase, previous_after=None, expected_total_bytes=total)
    assert any("bytes_added_on_disk=50 != delivered_bytes=150" in v for v in violations), violations


def test_M2_14D_TC_DL_22_tagged_cache_is_pruned_and_untagged_is_not(tmp_path: Path) -> None:
    """The probe's 76-byte noise source: pruned only while CACHEDIR.TAG is valid."""

    staging = tmp_path / "staging"
    _start(staging)
    state = staging / ".cache" / "modelscope"
    (state / "home" / "credentials").mkdir(parents=True)
    (state / "CACHEDIR.TAG").write_bytes(CACHEDIR_SIGNATURE + b"\n")
    (state / "home" / "credentials" / "session").write_bytes(b"x" * 32)

    pruned = snapshot(staging)
    assert not any(name.startswith(".cache/") for name in pruned.complete), pruned.complete

    (state / "CACHEDIR.TAG").unlink()
    unpruned = snapshot(staging)
    assert ".cache/modelscope/home/credentials/session" in unpruned.complete
    assert unpruned.bytes_on_disk == pruned.bytes_on_disk + 32


def test_M2_14D_TC_DL_22_both_modelscope_partial_shapes_are_partial() -> None:
    assert is_partial("model-00001-of-00004.safetensors.incomplete")
    assert is_partial("model-00001-of-00004.safetensors_0_1048575")
    assert not is_partial("model-00001-of-00004.safetensors")
    assert not is_partial("model.safetensors.index.json")


def test_M2_14D_TC_DL_22_the_probe_final_phase_judges_clean() -> None:
    """2026-09-14 read-out, pruned of the 76-byte cache: a legitimate phase that
    delivered 71 % of the repository must not be judged a restart."""

    small = {"small-files": FileState(11_535_384, 1, 1)}
    shards = (3_945_441_440, 3_864_726_352, 3_864_726_424, 3_556_377_672)
    parts = (1_108_344_832, 1_060_110_336, 1_104_150_528, 1_082_130_432)
    names = [f"model-0000{index}-of-00004.safetensors" for index in range(1, 5)]
    before = StagingSnapshot(
        small,
        {
            f"{name}.incomplete": FileState(size, 10 + i, 1)
            for i, (name, size) in enumerate(zip(names, parts, strict=True))
        },
    )
    after = StagingSnapshot(
        {
            **small,
            **{
                name: FileState(size, 20 + i, 2)
                for i, (name, size) in enumerate(zip(names, shards, strict=True))
            },
        },
        {},
    )
    total = 15_242_807_272
    phase = PhaseObservation("resume_to_completion", 10_876_535_760, before, after, True)

    assert phase.delivered_bytes / total > 0.7
    assert resume_violations(phase, previous_after=before, expected_total_bytes=total) == ()


def test_M2_14D_TC_DL_22_snapshot_refuses_a_symlink(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    _start(staging)
    os.symlink(staging / "config.json", staging / "alias.json")
    try:
        snapshot(staging)
    except AssertionError as error:
        assert "alias.json" in str(error)
    else:
        raise AssertionError("a symlink in staging was silently counted")
