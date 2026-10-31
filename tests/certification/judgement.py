"""TC-DL-22 resume judgement for the M2-14D certification run.

Resume validation reduces "this phase resumed instead of starting over" to
three checks, each standing on its own:

1. ``bytes_added_on_disk == delivered_bytes`` -- every byte the SDK reported was
   new on disk.  A partial file that is truncated and fetched again reports more
   than it adds.  Exact, with no ratio tolerance: the probe measured zero noise in
   all three phases, and the only non-zero source it found (76 bytes of ModelScope
   cache and session metadata) is removed by pruning, not absorbed by slack.
2. ``delivered_bytes <= expected_total - bytes already on disk``, with equality
   when the phase runs to completion.  This is the check that sees a
   re-transfer: a completed file deleted and fetched again, or a restart from an
   emptied staging, both still score a ratio of 1.0, but both deliver more than
   was missing.  "Delivered close to the full size" is NOT the criterion: the
   probe's legitimate final phase delivered 71 % of the full size.
3. A completed file never changes once completed -- same size, inode and mtime.
   This is the second, weaker layer: a file deleted and recreated within one
   timestamp tick can get its inode and mtime back (observed on the
   container's overlay filesystem), so it must never be the only thing that
   catches a re-transfer.  It exists for what check 2 cannot see: an SDK
   rewriting a completed file in place without reporting the bytes.

Deliberately absent: the probe's ``reused_existing_bytes``.  It was defined as the
before-snapshot's size, so "it equals the previous phase's bytes on disk" holds by
construction and proves nothing.
"""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

# Imported rather than restated: the snapshot must prune exactly what Artifact
# publication prunes, or the two disagree about which bytes count (one definition source).
from holm_download_worker.artifact_publishing import _is_tagged_cache

# ModelScope's two partial-file shapes: the whole-file ``<name>.incomplete`` and
# the parallel range part ``<name>_<start>_<end>`` (``_modelscope_resumed_bytes``
# in download_subprocess.py counts both).
_PARALLEL_RANGE_PART = re.compile(r".+_\d+_\d+")


@dataclass(frozen=True, slots=True)
class FileState:
    size_bytes: int
    inode: int
    mtime_ns: int


@dataclass(frozen=True, slots=True)
class StagingSnapshot:
    complete: Mapping[str, FileState]
    partial: Mapping[str, FileState]

    @property
    def bytes_on_disk(self) -> int:
        states = (*self.complete.values(), *self.partial.values())
        return sum(state.size_bytes for state in states)


@dataclass(frozen=True, slots=True)
class PhaseObservation:
    name: str
    delivered_bytes: int
    before: StagingSnapshot
    after: StagingSnapshot
    ran_to_completion: bool


def is_partial(relative_path: str) -> bool:
    name = relative_path.rsplit("/", 1)[-1]
    return name.endswith(".incomplete") or _PARALLEL_RANGE_PART.fullmatch(name) is not None


def snapshot(staging: Path) -> StagingSnapshot:
    """File-level snapshot of staging, pruned the way Artifact publication prunes."""

    complete: dict[str, FileState] = {}
    partial: dict[str, FileState] = {}
    for current_raw, directory_names, file_names in os.walk(
        staging, topdown=True, followlinks=False
    ):
        current = Path(current_raw)
        retained: list[str] = []
        for name in sorted(directory_names):
            candidate = current / name
            if stat.S_ISLNK(candidate.lstat().st_mode):
                raise AssertionError(f"staging holds a directory symlink: {candidate}")
            if not _is_tagged_cache(candidate):
                retained.append(name)
        directory_names[:] = retained
        for name in file_names:
            path = current / name
            metadata = path.lstat()
            relative = path.relative_to(staging).as_posix()
            if not stat.S_ISREG(metadata.st_mode):
                raise AssertionError(f"staging holds a non-regular entry: {relative}")
            state = FileState(metadata.st_size, metadata.st_ino, metadata.st_mtime_ns)
            (partial if is_partial(relative) else complete)[relative] = state
    return StagingSnapshot(complete, partial)


def resume_violations(
    phase: PhaseObservation,
    *,
    previous_after: StagingSnapshot | None,
    expected_total_bytes: int,
) -> tuple[str, ...]:
    """Every way this phase failed to resume -- empty means it resumed.

    ``previous_after`` is the previous phase's closing snapshot, not this phase's
    opening one: anything done to staging between the two must not be able to
    reset the baseline that completed files are held to.
    """

    if expected_total_bytes <= 0 or phase.delivered_bytes < 0:
        raise ValueError("expected_total_bytes must be positive and delivered_bytes non-negative")
    found: list[str] = []

    added = phase.after.bytes_on_disk - phase.before.bytes_on_disk
    if added != phase.delivered_bytes:
        found.append(
            f"{phase.name}: bytes_added_on_disk={added} != delivered_bytes={phase.delivered_bytes}"
        )

    baseline = phase.before if previous_after is None else previous_after
    for relative, state in sorted(baseline.complete.items()):
        now = phase.after.complete.get(relative)
        if now != state:
            found.append(f"{phase.name}: completed file {relative} changed: {state} -> {now}")

    still_missing = expected_total_bytes - baseline.bytes_on_disk
    if phase.delivered_bytes > still_missing:
        found.append(
            f"{phase.name}: delivered_bytes={phase.delivered_bytes} exceeds the "
            f"{still_missing} bytes still missing -- it started over"
        )
    if phase.ran_to_completion:
        if phase.after.partial:
            found.append(f"{phase.name}: partial files left: {sorted(phase.after.partial)}")
        if phase.after.bytes_on_disk != expected_total_bytes:
            found.append(
                f"{phase.name}: {phase.after.bytes_on_disk} bytes on disk at completion, "
                f"expected {expected_total_bytes}"
            )
        if phase.delivered_bytes != still_missing:
            found.append(
                f"{phase.name}: completed after delivering {phase.delivered_bytes} bytes, "
                f"but {still_missing} were missing"
            )
    return tuple(found)
