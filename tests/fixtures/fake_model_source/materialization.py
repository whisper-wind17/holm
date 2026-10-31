"""Project one ``FX-MS-06`` description onto a real, caller-owned directory.

Until M2-07A the path-attack samples were descriptions and nothing else, so no
test in this repository had ever put an escaping symbolic link, a FIFO or a
setuid file on a filesystem and watched production code refuse it.  This module
closes that gap and nothing more:

* **The description is the only truth source.**  Every entry created here comes
  from ``PathAttackSample`` -- its ``kind``, ``untrusted_value``,
  ``entry_kind``, ``symbolic_link_target`` and ``mode_bits``.  No attack shape
  is invented that the description does not already state.
* **Everything lands under a caller-supplied base directory**, one isolated
  sandbox per sample, each with its own allowed root.  Nothing is written to
  the real ``storage.external_model_roots`` default (``/srv/models``), and the
  whole tree is reclaimed with the caller's ``tmp_path``.
* **A sample that cannot be created is suspended, not skipped.**  A character
  device needs ``CAP_MKNOD``, which an unprivileged container does not have.
  Such a sample comes back as ``BLOCKED_ENV`` with the reason that blocked it,
  and the caller is expected to report it as suspended rather than as a pass
  (the integration-test policy).
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import socket
import stat
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Final

from .models import (
    DescribedEntryKind,
    FixtureId,
    PathAttackKind,
    PathAttackSample,
    ValidationFailureKind,
    ValidationFailureSample,
)

_CHARACTER_DEVICE_MAJOR: Final[int] = 1
_CHARACTER_DEVICE_MINOR: Final[int] = 3
"""``/dev/null``'s device numbers.  ``mknod`` needs ``CAP_MKNOD`` regardless of
which numbers are asked for, so these only decide what the node *is* when the
capability happens to be available."""

_ENTRY_MODE: Final[int] = 0o644
_DIRECTORY_MODE: Final[int] = 0o755


class MaterializationOutcome(StrEnum):
    """What happened to one sample.  There is no fourth, implicit state."""

    MATERIALIZED = "materialized"
    BLOCKED_ENV = "blocked_env"
    NOT_A_FILESYSTEM_ENTRY = "not_a_filesystem_entry"


class MaterializationBlockedReason(StrEnum):
    """Why an environment could not create a described entry."""

    PRIVILEGE_REQUIRED = "privilege_required"
    UNSUPPORTED_BY_FILESYSTEM = "unsupported_by_filesystem"


@dataclass(frozen=True, slots=True)
class MaterializedPathAttack:
    """One description, and either the entry it became or why it could not.

    ``allowed_root`` and ``attack_path`` are present exactly when the outcome
    is ``MATERIALIZED``; ``blocked_reason`` exactly when it is ``BLOCKED_ENV``.
    The constructor enforces both, so "blocked" can never be read as "created
    at an unknown place" and a blocked sample can never be mistaken for a pass.
    """

    sample: PathAttackSample
    outcome: MaterializationOutcome
    allowed_root: Path | None = None
    attack_path: Path | None = None
    blocked_reason: MaterializationBlockedReason | None = None
    fixture_id: FixtureId = field(default=FixtureId.FX_MS_06, init=False)

    def __post_init__(self) -> None:
        created = self.outcome is MaterializationOutcome.MATERIALIZED
        if created != (self.allowed_root is not None and self.attack_path is not None):
            raise ValueError("a materialized sample has both a root and an entry")
        blocked = self.outcome is MaterializationOutcome.BLOCKED_ENV
        if blocked != (self.blocked_reason is not None):
            raise ValueError("a blocked sample carries exactly one blocking reason")


@dataclass(frozen=True, slots=True)
class MaterializedValidationFile:
    relative_path: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class MaterializedValidationFailure:
    """One FX-MS-05 description projected onto real files."""

    sample: ValidationFailureSample
    root: Path
    expected_files: tuple[MaterializedValidationFile, ...]
    fixture_id: FixtureId = field(default=FixtureId.FX_MS_05, init=False)


def materialize_path_attack(sample: PathAttackSample, base: Path) -> MaterializedPathAttack:
    """Create the entry ``sample`` describes inside its own sandbox under ``base``."""

    if sample.kind is PathAttackKind.INVALID_REPOSITORY_ID:
        # It describes a Repository ID, not a filesystem entry; there is
        # nothing to create and that is a conclusion, not a failure.
        return MaterializedPathAttack(
            sample=sample,
            outcome=MaterializationOutcome.NOT_A_FILESYSTEM_ENTRY,
        )

    sandbox = base / sample.kind.value
    allowed_root = sandbox / "root"
    allowed_root.mkdir(parents=True, exist_ok=True)
    outside = sandbox / "synthetic-outside"

    if sample.kind is PathAttackKind.PARENT_TRAVERSAL:
        # "weights/../../synthetic-outside" read from inside the root climbs
        # out of it.  The described entry kind is a regular file.
        (allowed_root / "weights").mkdir(exist_ok=True)
        _write_regular_file(outside)
        return _materialized(sample, allowed_root, allowed_root / sample.untrusted_value)

    if sample.kind is PathAttackKind.ABSOLUTE_PATH:
        # The described value is an absolute path outside any allowed root.
        # It is reproduced inside the sandbox rather than at "/synthetic",
        # which the test process may not write to and must not pollute.
        target = sandbox / Path(sample.untrusted_value).relative_to("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        _write_regular_file(target)
        return _materialized(sample, allowed_root, target)

    if sample.kind is PathAttackKind.ESCAPING_SYMBOLIC_LINK:
        if sample.symbolic_link_target is None:  # pragma: no cover - refused by the sample
            raise ValueError("an escaping symbolic link description requires a target")
        # The escape target is a real directory, so the *only* reason the
        # judgement can refuse this path is that it leaves the allowed root.
        outside.mkdir(mode=_DIRECTORY_MODE, exist_ok=True)
        link = allowed_root / sample.untrusted_value
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(sample.symbolic_link_target)
        return _materialized(sample, allowed_root, link)

    entry = allowed_root / sample.untrusted_value
    entry.parent.mkdir(parents=True, exist_ok=True)

    if sample.kind is PathAttackKind.DEVICE_FILE:
        return _materialize_device(sample, allowed_root, entry)
    if sample.kind is PathAttackKind.FIFO:
        return _materialize_fifo(sample, allowed_root, entry)
    if sample.kind is PathAttackKind.SOCKET:
        return _materialize_socket(sample, allowed_root, entry)
    if sample.kind is PathAttackKind.SETUID_FILE:
        return _materialize_setuid_file(sample, allowed_root, entry)
    # A new FX-MS-06 kind must be described here deliberately.  Falling through
    # to "whatever the last branch did" is how a sample silently stops being
    # the shape it claims to be.
    raise ValueError("FX-MS-06 gained a kind this materializer does not describe")


def materialize_path_attacks(
    samples: tuple[PathAttackSample, ...],
    base: Path,
) -> tuple[MaterializedPathAttack, ...]:
    """Materialize every sample, each in its own sandbox."""

    return tuple(materialize_path_attack(sample, base) for sample in samples)


def _safetensors_bytes() -> bytes:
    header = json.dumps(
        {
            "fixture.weight": {
                "data_offsets": [0, 4],
                "dtype": "F32",
                "shape": [1],
            }
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return len(header).to_bytes(8, byteorder="little") + header + b"\x00\x00\x00\x00"


def _write_validation_baseline(root: Path) -> None:
    root.mkdir(parents=True)
    (root / "config.json").write_text(
        '{"architectures":["FixtureForCausalLM"],"model_type":"fixture"}',
        encoding="utf-8",
    )
    (root / "tokenizer.json").write_text('{"version":"1.0"}', encoding="utf-8")
    for ordinal in range(1, 5):
        (root / f"model-{ordinal:05d}-of-00004.safetensors").write_bytes(_safetensors_bytes())
    (root / "model.safetensors.index.json").write_text(
        json.dumps(
            {
                "metadata": {},
                "weight_map": {
                    f"fixture.layer.{ordinal}": (f"model-{ordinal + 1:05d}-of-00004.safetensors")
                    for ordinal in range(4)
                },
            },
            separators=(",", ":"),
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def _validation_snapshot(root: Path) -> tuple[MaterializedValidationFile, ...]:
    values: list[MaterializedValidationFile] = []
    for path in sorted(candidate for candidate in root.rglob("*") if candidate.is_file()):
        content = path.read_bytes()
        values.append(
            MaterializedValidationFile(
                relative_path=path.relative_to(root).as_posix(),
                size_bytes=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
            )
        )
    return tuple(values)


def materialize_validation_failure(
    sample: ValidationFailureSample,
    base: Path,
) -> MaterializedValidationFailure:
    """Materialize the exact FX-MS-05 failure kind under a caller-owned root."""

    root = base / sample.kind.value
    _write_validation_baseline(root)

    if sample.kind is ValidationFailureKind.HASH_MISMATCH:
        expected = _validation_snapshot(root)
        target = root / sample.affected_paths[0]
        target.parent.mkdir(parents=True, exist_ok=True)
        content = target.read_bytes()
        target.write_bytes(bytes([content[0] ^ 0x01]) + content[1:])
    elif sample.kind is ValidationFailureKind.SHARD_MISSING:
        missing = sample.affected_paths[0]
        (root / missing).unlink(missing_ok=True)
        index = {
            "metadata": {},
            "weight_map": {
                "fixture.layer.0": "model-00001-of-00004.safetensors",
                "fixture.layer.missing": missing,
            },
        }
        (root / "model.safetensors.index.json").write_text(
            json.dumps(index, separators=(",", ":"), sort_keys=True),
            encoding="utf-8",
        )
        expected = _validation_snapshot(root)
    elif sample.kind is ValidationFailureKind.SAFETENSORS_HEADER_CORRUPT:
        target = root / sample.affected_paths[0]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"corrupt-safetensors-header")
        expected = _validation_snapshot(root)
    elif sample.kind is ValidationFailureKind.CONFIG_OR_TOKENIZER_MISSING:
        for relative in sample.affected_paths:
            (root / relative).unlink(missing_ok=True)
        expected = _validation_snapshot(root)
    else:  # pragma: no cover - enum additions must gain an explicit projection
        raise ValueError("FX-MS-05 gained a kind without a materializer")

    return MaterializedValidationFailure(sample=sample, root=root, expected_files=expected)


def _materialized(
    sample: PathAttackSample,
    allowed_root: Path,
    attack_path: Path,
) -> MaterializedPathAttack:
    return MaterializedPathAttack(
        sample=sample,
        outcome=MaterializationOutcome.MATERIALIZED,
        allowed_root=allowed_root,
        attack_path=attack_path,
    )


def _blocked(
    sample: PathAttackSample,
    reason: MaterializationBlockedReason,
) -> MaterializedPathAttack:
    return MaterializedPathAttack(
        sample=sample,
        outcome=MaterializationOutcome.BLOCKED_ENV,
        blocked_reason=reason,
    )


def _write_regular_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    path.chmod(_ENTRY_MODE)


def _materialize_device(
    sample: PathAttackSample,
    allowed_root: Path,
    entry: Path,
) -> MaterializedPathAttack:
    try:
        os.mknod(
            entry,
            mode=stat.S_IFCHR | 0o600,
            device=os.makedev(_CHARACTER_DEVICE_MAJOR, _CHARACTER_DEVICE_MINOR),
        )
    except PermissionError:
        return _blocked(sample, MaterializationBlockedReason.PRIVILEGE_REQUIRED)
    except OSError as failure:
        if failure.errno in (errno.EPERM, errno.EACCES):
            return _blocked(sample, MaterializationBlockedReason.PRIVILEGE_REQUIRED)
        return _blocked(sample, MaterializationBlockedReason.UNSUPPORTED_BY_FILESYSTEM)
    return _materialized(sample, allowed_root, entry)


def _materialize_fifo(
    sample: PathAttackSample,
    allowed_root: Path,
    entry: Path,
) -> MaterializedPathAttack:
    try:
        os.mkfifo(entry, mode=0o600)
    except OSError:
        return _blocked(sample, MaterializationBlockedReason.UNSUPPORTED_BY_FILESYSTEM)
    return _materialized(sample, allowed_root, entry)


def _materialize_socket(
    sample: PathAttackSample,
    allowed_root: Path,
    entry: Path,
) -> MaterializedPathAttack:
    try:
        # ``mknod`` of a socket needs no capability on Linux; binding one is
        # the portable fallback but caps the path at ``sun_path``'s length.
        os.mknod(entry, mode=stat.S_IFSOCK | 0o600)
    except OSError:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                listener.bind(str(entry))
        except OSError:
            return _blocked(sample, MaterializationBlockedReason.UNSUPPORTED_BY_FILESYSTEM)
    return _materialized(sample, allowed_root, entry)


def _materialize_setuid_file(
    sample: PathAttackSample,
    allowed_root: Path,
    entry: Path,
) -> MaterializedPathAttack:
    if sample.mode_bits is None:  # pragma: no cover - refused by the sample
        raise ValueError("a setuid description requires mode metadata")
    _write_regular_file(entry)
    try:
        entry.chmod(sample.mode_bits)
    except OSError:
        return _blocked(sample, MaterializationBlockedReason.PRIVILEGE_REQUIRED)
    if not entry.lstat().st_mode & stat.S_ISUID:
        # Some filesystems drop the bit silently; that is an environment
        # limitation and must be reported, not quietly accepted.
        return _blocked(sample, MaterializationBlockedReason.UNSUPPORTED_BY_FILESYSTEM)
    return _materialized(sample, allowed_root, entry)


def described_entry_kind_matches(materialized: MaterializedPathAttack) -> bool:
    """Confirm the created entry is the kind the description named.

    Used so a test can prove the materializer produced the described shape
    rather than something easier to create.
    """

    if materialized.outcome is not MaterializationOutcome.MATERIALIZED:
        return False
    if materialized.attack_path is None:  # pragma: no cover - refused by construction
        raise ValueError("a materialized sample has an entry")
    if materialized.sample.kind is PathAttackKind.PARENT_TRAVERSAL:
        # The described entry is the escape *target*; the value itself is the
        # climbing path that reaches it.
        mode = os.lstat(os.path.realpath(materialized.attack_path)).st_mode
    else:
        mode = materialized.attack_path.lstat().st_mode
    expected = materialized.sample.entry_kind
    if expected is DescribedEntryKind.REGULAR_FILE:
        return stat.S_ISREG(mode)
    if expected is DescribedEntryKind.SYMBOLIC_LINK:
        return stat.S_ISLNK(mode)
    if expected is DescribedEntryKind.DEVICE:
        return stat.S_ISCHR(mode) or stat.S_ISBLK(mode)
    if expected is DescribedEntryKind.FIFO:
        return stat.S_ISFIFO(mode)
    if expected is DescribedEntryKind.SOCKET:
        return stat.S_ISSOCK(mode)
    raise ValueError("a filesystem sample must describe an entry kind")


__all__ = [
    "MaterializationBlockedReason",
    "MaterializationOutcome",
    "MaterializedPathAttack",
    "MaterializedValidationFailure",
    "MaterializedValidationFile",
    "described_entry_kind_matches",
    "materialize_path_attack",
    "materialize_path_attacks",
    "materialize_validation_failure",
]
