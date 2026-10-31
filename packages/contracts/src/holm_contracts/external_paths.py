"""The single definition source for "is this server path inside an allowed root".

The Control API and Host Agent need the *same*
judgement. The Host Agent is the higher-privileged side and therefore
must redo it rather than trust a caller that already asked.
Two "equivalent" implementations would drift on symbolic-link semantics without
either side failing, so the rule lives here, in a package both sides already
depend on, and each side imports it.

The module deliberately holds nothing but the rule: stdlib only, no protobuf, no
FastAPI, no configuration. ``apps -> packages`` stays the only direction.

Three properties are structural rather than remembered:

* **Nothing here writes a byte.** The judgement uses ``realpath``, ``lstat``
  and ``access`` and never opens, creates, renames, chmods or deletes
  anything. The platform never modifies an
  External Root.
* **The ancestor test is by path segment.** ``/srv/models-evil/x`` shares a
  string prefix with ``/srv/models`` and is not inside it. The comparison
  runs on ``PurePosixPath`` parents, after ``realpath`` on both sides.
* **No path leaves this module.** ``ExternalPathVerdict`` carries a rule, not
  a location, so neither a response, a log line nor an exception has a place to
  put the input, its ``realpath`` or the allowed roots. A
  caller is told the path is unusable and nothing about the directory tree it
  probed.

``ExternalPathRejectionRule.NOT_READABLE_BY_CONTROL_PLANE`` keeps the name it
was delivered under. Read from the Host Agent it means "the process performing
the judgement cannot read it", not specifically the control plane; renaming it
would break compatibility between the two callers,
and the value never leaves the module -- it reaches a service log and nothing
else.

The conclusion is only about the instant it was reached. This module performs
no "use" of the path and therefore introduces no TOCTOU mitigation; the sites
that actually use a path -- registration, download, deployment
mount -- each judge it again.
"""

from __future__ import annotations

import logging
import os
import stat
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Final

_LOGGER: Final[logging.Logger] = logging.getLogger(__name__)


class ExternalPathRejectionRule(StrEnum):
    """Which judgement refused a path.

    This vocabulary is internal.  It reaches the service log so an operator can
    see *which rule* fired, and it never reaches the API response nor an Agent
    response: telling "does not exist" from "outside the allowed roots" apart,
    one probe at a time, is how a caller draws a map of the server.
    """

    NOT_ABSOLUTE = "not_absolute"
    OUTSIDE_ALLOWED_ROOTS = "outside_allowed_roots"
    NOT_FOUND = "not_found"
    UNRESOLVED_SYMBOLIC_LINK = "unresolved_symbolic_link"
    NOT_A_DIRECTORY = "not_a_directory"
    NOT_READABLE_BY_CONTROL_PLANE = "not_readable_by_control_plane"
    INSPECTION_FAILED = "inspection_failed"


@dataclass(frozen=True, slots=True)
class ExternalPathVerdict:
    """One judgement, carrying a rule rather than a location.

    There is no field a path could be assigned to, which is what makes "the
    rejection did not leak the path" a property of the type instead of a rule
    every call site has to remember.
    """

    accepted: bool
    rule: ExternalPathRejectionRule | None

    def __post_init__(self) -> None:
        if self.accepted != (self.rule is None):
            raise ValueError("a verdict is accepted exactly when no rule refused it")


@dataclass(frozen=True, slots=True)
class ExternalPathPolicy:
    """The configured allowed roots, each already reduced to its ``realpath``.

    The path check compares a candidate to the allowed roots *after* ``realpath``.
    Comparing a resolved candidate against an unresolved root would make a
    symbolic link anywhere above the root silently re-point the whole
    Allowlist, so both sides are resolved.
    ``StorageConfig.validate_external_roots`` refuses a configured root that is
    not already its own ``realpath``; resolving again here means a policy built
    from any other source -- a test's ``tmp_path``, the Host Agent's command
    line -- obeys the same rule rather than trusting its input.
    """

    allowed_roots: tuple[PurePosixPath, ...]

    def __post_init__(self) -> None:
        if not self.allowed_roots:
            raise ValueError("an External Path policy requires at least one allowed root")
        if any(not root.is_absolute() for root in self.allowed_roots):
            raise ValueError("every allowed root must be absolute")

    @classmethod
    def for_configured_roots(cls, roots: Iterable[Path]) -> ExternalPathPolicy:
        """Build a policy from ``storage.external_model_roots``."""

        declared = tuple(roots)
        if any(not root.is_absolute() for root in declared):
            # A relative root would be resolved against the process working
            # directory, which is not a configured value at all.
            raise ValueError("every configured External Root must be absolute")
        resolved = tuple(dict.fromkeys(PurePosixPath(os.path.realpath(root)) for root in declared))
        return cls(allowed_roots=resolved)


def path_is_within(candidate: PurePosixPath, root: PurePosixPath) -> bool:
    """Segment-level ancestor test, the single definition of "inside a root".

    ``PurePosixPath.parents`` compares whole segments, so ``/srv/models-evil``
    is not inside ``/srv/models`` even though the strings share a prefix.
    Both arguments must already be resolved; this function does no I/O.
    """

    return candidate == root or root in candidate.parents


def judge_external_path(untrusted_path: str, policy: ExternalPathPolicy) -> ExternalPathVerdict:
    """Decide one path against one policy, reading the filesystem and nothing else.

    Blocking; run it in a worker thread.  Every branch returns a verdict, so
    there is no path through this function that reports "unknown" as accepted.
    """

    candidate = PurePosixPath(untrusted_path)
    if not candidate.is_absolute():
        return _refused(ExternalPathRejectionRule.NOT_ABSOLUTE)

    # ``realpath`` resolves every symbolic link, ``.`` and ``..`` in the input.
    # It touches nothing and tolerates a missing tail, so a path that escapes
    # its root through a link is compared in its escaped form.
    resolved = PurePosixPath(os.path.realpath(untrusted_path))
    if not any(path_is_within(resolved, root) for root in policy.allowed_roots):
        return _refused(ExternalPathRejectionRule.OUTSIDE_ALLOWED_ROOTS)

    try:
        entry = os.lstat(resolved)
    except (FileNotFoundError, NotADirectoryError):
        return _refused(ExternalPathRejectionRule.NOT_FOUND)
    except PermissionError:
        return _refused(ExternalPathRejectionRule.NOT_READABLE_BY_CONTROL_PLANE)
    except OSError:
        # A symbolic-link loop, a stale mount or an I/O error.  It is a
        # refusal with its own rule rather than a branch that quietly falls
        # through to acceptance.
        return _refused(ExternalPathRejectionRule.INSPECTION_FAILED)

    if stat.S_ISLNK(entry.st_mode):
        # ``realpath`` returns a link-free path, so reaching this means the
        # link could not be followed to a real entry.
        return _refused(ExternalPathRejectionRule.UNRESOLVED_SYMBOLIC_LINK)
    if not stat.S_ISDIR(entry.st_mode):
        # A device node, FIFO, socket or regular file -- including a setuid
        # one -- is not a model directory.
        return _refused(ExternalPathRejectionRule.NOT_A_DIRECTORY)
    if not os.access(resolved, os.R_OK | os.X_OK):
        return _refused(ExternalPathRejectionRule.NOT_READABLE_BY_CONTROL_PLANE)

    _LOGGER.info("external path judgement: accepted")
    return ExternalPathVerdict(accepted=True, rule=None)


def _refused(rule: ExternalPathRejectionRule) -> ExternalPathVerdict:
    """One refusal, logged by rule only.  There is no path in scope to log."""

    _LOGGER.info("external path judgement: refused by %s", rule.value)
    return ExternalPathVerdict(accepted=False, rule=rule)


__all__ = [
    "ExternalPathPolicy",
    "ExternalPathRejectionRule",
    "ExternalPathVerdict",
    "judge_external_path",
    "path_is_within",
]
