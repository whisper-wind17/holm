"""Can the *Runtime's* UID read this server path?

The Control API answers the other three Local Path judgements and leaves this
one explicitly unverified, because it cannot ask: its Host Agent
method set is restricted to read-only methods.  ``ValidatePath`` belongs to
``download-worker`` and this module is the Agent side of it.

**The answer is derived, never demonstrated.**  Opening the path as another UID
would need ``fork`` plus ``setresuid``, and the Agent runs as
``User=holm-agent`` with ``NoNewPrivileges=true``; granting it
``CAP_SETUID`` would widen the capability surface of the most privileged
component in this repository. So the
judgement walks the resolved path and its ancestors and reads owner, group and
mode bits.

**That makes the two sides unequal, and the code is shaped around it.**  A mode
bit that withholds permission withholds it, so ``NOT_READABLE`` can be trusted.
A mode bit that grants permission can still be overruled by an ACL, a SELinux
label or a mount option that this derivation cannot see, so ``READABLE`` is
only reported when the whole path chain carries no extended attributes at all;
otherwise the answer is ``UNKNOWN(acl_present)``.  Reversing that -- treating
"an ACL exists" as "probably readable" -- is the single most dangerous shape
this module could take, so no branch here folds an unknown into a readable.

Two known imprecisions both point the safe way, at ``NOT_READABLE``:

* The Release Manifest states one primary GID per service.  A Runtime that
  reaches a directory through a *supplementary* group is derived as unable to
  read it.
* When the Agent itself cannot ``lstat`` an element of the chain, or cannot
  list its extended attributes, the derivation cannot complete.  An incomplete
  derivation refuses (or, for the extended-attribute half, answers unknown).
  It never reports readable.

A wrong ``NOT_READABLE`` costs the operator a usable path; a wrong ``READABLE``
sends a download to a directory the Runtime will later fail to open.

**Nothing here writes, opens or names anything.**  The judgement uses
``lstat``, ``realpath`` and ``listxattr`` only; the read-only policy requires that
the platform never modify an External Root, and it never reads file content
either.  The result type carries a conclusion and, when unknown, a reason --
there is no field a path could be written into, and none of the four log lines
has a path in scope.
"""

from __future__ import annotations

import json
import logging
import os
import stat
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Final

from holm_contracts import ReleaseManifest
from holm_contracts.external_paths import (
    ExternalPathPolicy,
    judge_external_path,
    path_is_within,
)

_LOGGER: Final[logging.Logger] = logging.getLogger(__name__)

RUNTIME_SERVICE_IDENTITY: Final[str] = "runtime"
"""The Release Manifest ``service_identities`` entry naming the Runtime.

A Manifest without this entry produces ``UNKNOWN``. The name is resolved
against the Manifest rather than hardcoded to a number, so the configured
Runtime identity is the only authority.
"""

_MODE_READ: Final[int] = 0b100
_MODE_EXECUTE: Final[int] = 0b001


@dataclass(frozen=True, slots=True)
class RuntimeIdentity:
    """The Runtime's fixed non-root UID/GID, as stated by the Release Manifest.

    ``ServiceIdentity`` constrains both to positive integers, so a Runtime
    identity can never be UID 0 and the derivation never has to model root's
    bypass of the mode bits.
    """

    uid: int
    gid: int

    def __post_init__(self) -> None:
        if self.uid <= 0 or self.gid <= 0:
            raise ValueError("a Runtime identity must be a fixed non-root UID/GID")


def load_runtime_identity(path: Path) -> RuntimeIdentity | None:
    """Read the Runtime identity from the immutable Release Manifest, or ``None``.

    ``None`` means "the Manifest does not state it".  It is never a licence to
    substitute the Agent's own UID, root, or an assumption of readability: the
    caller turns it into an explicit unknown.
    """

    try:
        payload: object = json.loads(path.read_text(encoding="utf-8"))
        manifest = ReleaseManifest.model_validate(payload)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return None
    for identity in manifest.service_identities:
        if identity.service == RUNTIME_SERVICE_IDENTITY:
            return RuntimeIdentity(uid=identity.uid, gid=identity.gid)
    return None


class RuntimeReadabilityConclusion(StrEnum):
    """The three states this judgement can reach."""

    READABLE = "readable"
    NOT_READABLE = "not_readable"
    UNKNOWN = "unknown"


class RuntimeReadabilityUnknownReason(StrEnum):
    """Why an answer is unknown.  There is no "unspecified" member.

    An unknown without a reason would be exactly the shape that lets a missing
    case pass as an answer, so the reason is required by the result type.
    """

    RUNTIME_IDENTITY_UNAVAILABLE = "runtime_identity_unavailable"
    ACL_PRESENT = "acl_present"


@dataclass(frozen=True, slots=True)
class RuntimeReadabilityJudgement:
    """One conclusion, carrying a reason rather than a location.

    Like ``ExternalPathVerdict``, there is no field a path could be assigned
    to, which makes "the answer did not leak the path" a property of the type
    instead of a rule every call site must remember.
    """

    conclusion: RuntimeReadabilityConclusion
    unknown_reason: RuntimeReadabilityUnknownReason | None

    def __post_init__(self) -> None:
        is_unknown = self.conclusion is RuntimeReadabilityConclusion.UNKNOWN
        if is_unknown != (self.unknown_reason is not None):
            raise ValueError("a judgement is unknown exactly when it states a reason")


def _readable() -> RuntimeReadabilityJudgement:
    """Branch 1 of 4.  Derived readable and no extended attribute in the chain."""

    _LOGGER.info("runtime readability judgement: readable")
    return RuntimeReadabilityJudgement(RuntimeReadabilityConclusion.READABLE, None)


def _not_readable() -> RuntimeReadabilityJudgement:
    """Branch 2 of 4.  The confident side: out of bounds, absent, or mode bits refuse."""

    _LOGGER.info("runtime readability judgement: not readable")
    return RuntimeReadabilityJudgement(RuntimeReadabilityConclusion.NOT_READABLE, None)


def _unknown_runtime_identity() -> RuntimeReadabilityJudgement:
    """Branch 3 of 4.  The Manifest states no Runtime identity, so there is no UID."""

    _LOGGER.info("runtime readability judgement: unknown, runtime identity unavailable")
    return RuntimeReadabilityJudgement(
        RuntimeReadabilityConclusion.UNKNOWN,
        RuntimeReadabilityUnknownReason.RUNTIME_IDENTITY_UNAVAILABLE,
    )


def _unknown_acl_present() -> RuntimeReadabilityJudgement:
    """Branch 4 of 4.  Derived readable, but an extended attribute may overrule it."""

    _LOGGER.info("runtime readability judgement: unknown, extended attributes present")
    return RuntimeReadabilityJudgement(
        RuntimeReadabilityConclusion.UNKNOWN,
        RuntimeReadabilityUnknownReason.ACL_PRESENT,
    )


def judge_runtime_readability(
    untrusted_path: str,
    *,
    policy: ExternalPathPolicy | None,
    identity: RuntimeIdentity | None,
) -> RuntimeReadabilityJudgement:
    """Answer one path for the Runtime identity.  Blocking; call it off the event loop.

    The order is deliberate.  "Outside the allowed roots", "does not exist" and
    "is not a directory" are refusals no UID could overcome, so they are
    settled first and collapse onto the same ``NOT_READABLE`` as a mode-bit
    refusal: a caller must not be able to tell them apart and map the server
    one probe at a time.  Only a path that survives all of
    them needs a Runtime identity at all.

    The out-of-bounds half is *redone here* rather than trusted from the
    control plane, using the same shared rule the control plane calls -- the
    Agent is the higher-privileged side, and a second "equivalent"
    implementation would drift on symbolic-link semantics without either side
    failing.

    **The response collapses those refusals; the log deliberately does not.**
    Out of bounds, absent inside a root, and refused by the mode bits each
    write a different first line to this host's journal -- ``refused by
    outside_allowed_roots``, ``refused by not_found``, and ``accepted``
    followed by this module's ``not readable`` -- while all three answer the
    caller with the same reason-free ``NOT_READABLE``.  That asymmetry is
    intended: the privacy boundary constrains what a *caller*
    can distinguish, and the caller cannot distinguish them.  The Agent's log
    goes to the local journal, never into a response, and carries no path or
    allowed root; its reader is an operator diagnosing a misconfigured External
    Root, who otherwise could not tell a typo from a permission problem.
    Collapsing the log too would cost that diagnosis and buy nothing, because
    the log is not a surface the caller can read.
    """

    if policy is None:
        # No allowed root is configured, so no path is inside one.  Refusing is
        # the fail-closed reading; there is no branch where an unconfigured
        # Agent answers "readable".
        return _not_readable()
    try:
        verdict = judge_external_path(untrusted_path, policy)
    except ValueError:
        # An embedded NUL byte cannot name a directory; ``realpath`` raises on
        # it rather than returning.  It is a refusal, not an exception the
        # caller sees.
        return _not_readable()
    if not verdict.accepted:
        # UID-independent refusals: outside the roots, absent, not a directory,
        # or unreadable by the Agent itself.
        return _not_readable()

    if identity is None:
        return _unknown_runtime_identity()

    resolved = PurePosixPath(os.path.realpath(untrusted_path))
    # The shared judgement and the mode-bit derivation each resolve the
    # untrusted name.  A symbolic link can be retargeted between those calls,
    # so the result used below must independently remain inside an allowed
    # root.  Keeping the resolved path local preserves the path-free verdict
    # type and the indistinguishable external refusal response.
    if not any(path_is_within(resolved, root) for root in policy.allowed_roots):
        return _not_readable()
    if not _runtime_can_read(resolved, identity):
        # The mode bits withhold permission from the Runtime identity.  This is
        # the side of the judgement that can be trusted.
        return _not_readable()
    if _chain_has_extended_attributes(resolved):
        return _unknown_acl_present()
    return _readable()


def _path_chain(resolved: PurePosixPath) -> tuple[PurePosixPath, ...]:
    """Every ancestor from the filesystem root down, then the target itself."""

    return (*reversed(resolved.parents), resolved)


def _permission_bits(entry: os.stat_result, identity: RuntimeIdentity) -> int:
    """The rwx triple POSIX would apply to this identity, as three low bits.

    POSIX picks exactly one class: owner, else group, else other.  It does not
    fall through, so a directory owned by the Runtime with mode ``0077`` is
    *not* readable by it -- taking the maximum of the three classes instead
    would invent access the kernel would refuse.
    """

    if entry.st_uid == identity.uid:
        return (entry.st_mode & stat.S_IRWXU) >> 6
    if entry.st_gid == identity.gid:
        return (entry.st_mode & stat.S_IRWXG) >> 3
    return entry.st_mode & stat.S_IRWXO


def _runtime_can_read(resolved: PurePosixPath, identity: RuntimeIdentity) -> bool:
    """Require ``+x`` on every ancestor and ``+r`` plus ``+x`` on the target.

    The target is always a directory here (the shared rule already refused
    anything else).  A directory with ``r--`` can have its entries listed but
    none of them opened, so requiring ``+x`` as well as ``+r`` is what makes
    "readable" mean the Runtime can actually reach the model files.  It is also
    the same test the shared rule applies to the judging process itself.
    """

    chain = _path_chain(resolved)
    for ancestor in chain[:-1]:
        try:
            entry = os.lstat(ancestor)
        except OSError:
            return False
        if not _permission_bits(entry, identity) & _MODE_EXECUTE:
            return False
    try:
        target = os.lstat(chain[-1])
    except OSError:
        return False
    granted = _permission_bits(target, identity)
    return bool(granted & _MODE_READ) and bool(granted & _MODE_EXECUTE)


def _chain_has_extended_attributes(resolved: PurePosixPath) -> bool:
    """Whether anything on the chain carries an extended attribute.

    An ACL is stored as one, and so are SELinux and Smack labels, so a
    non-empty list means the mode bits are not the whole story.  A chain
    element whose attributes cannot be listed counts as carrying one: not being
    able to rule an ACL out is not the same as there being none.
    """

    for element in _path_chain(resolved):
        try:
            if os.listxattr(element, follow_symlinks=False):
                return True
        except OSError:
            return True
    return False


__all__ = [
    "RUNTIME_SERVICE_IDENTITY",
    "RuntimeIdentity",
    "RuntimeReadabilityConclusion",
    "RuntimeReadabilityJudgement",
    "RuntimeReadabilityUnknownReason",
    "judge_runtime_readability",
    "load_runtime_identity",
]
