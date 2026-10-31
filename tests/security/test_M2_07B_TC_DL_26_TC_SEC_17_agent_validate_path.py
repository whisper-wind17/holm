"""TC-DL-26/TC-SEC-17: what the Agent's ``ValidatePath`` answers, and what it never says.

The Host Agent derives Runtime readability from POSIX mode bits.  It cannot
demonstrate it -- opening a path as another UID needs ``CAP_SETUID``, which the
owner refused on 2026-09-04 -- so the thing under test here **is the
derivation**.

That matters for how these cases are built.  The development container runs as
**root**, and root bypasses the mode bits entirely: a case that removed ``+x``
and then checked that ``open`` really fails would pass here for the wrong
reason, and would go on passing if the derivation were deleted.  So every case
below constructs an owner/group/mode shape for a Runtime UID that is **not**
this process's UID and asserts the derived conclusion.  No case opens anything.
A real-behaviour cross-check is only meaningful as a non-root user and is
recorded as ``BLOCKED_ENV`` rather than skipped.

Every positive control breaks the **production** module and reloads it, then
runs the same judgement against the mutated copy (``LESSONS.md``: M1-09B,
M1-01G, M2-01B).  The four conclusions are four different pieces of code that
log four different lines, so each one is leak-scanned and each one has its own
control -- M2-07A shipped a scan proven on the refusal branch only, and a
``realpath`` written into the *accepted* branch went unseen by 849 passing
cases (``LESSONS.md``).
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import errno
import hashlib
import json
import logging
import os
import stat
import sys
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import grpc
import pytest
from fixtures.source_mutation import load_mutated_module, mutate_source
from holm_contracts.agent.v1 import agent_pb2
from holm_contracts.external_paths import ExternalPathPolicy
from holm_host_agent import path_validation as agent_path_validation
from holm_host_agent import server as server_module
from holm_host_agent.authorization import (
    ROLE_METHOD_ALLOWLIST,
    AuthorizationDenialReason,
    IdentityCatalog,
    StructuredDenialAuditor,
    allowed_methods_for_service,
    assert_method_policy_complete,
    assert_protocol_request_surface_safe,
    authorize_rpc,
)
from holm_host_agent.path_validation import (
    RUNTIME_SERVICE_IDENTITY,
    RuntimeIdentity,
    RuntimeReadabilityConclusion,
    RuntimeReadabilityJudgement,
    RuntimeReadabilityUnknownReason,
    judge_runtime_readability,
    load_runtime_identity,
)
from holm_host_agent.server import HostAgentServer, readability_to_proto

REPOSITORY_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
AGENT_JUDGEMENT_SOURCE: Final[Path] = (
    REPOSITORY_ROOT / "apps/host_agent/src/holm_host_agent/path_validation.py"
)
CONTROL_PLANE_SOURCE: Final[Path] = (
    REPOSITORY_ROOT / "apps/control_api/src/holm_control_api/external_path_resolution.py"
)
SHARED_RULE_SOURCE: Final[Path] = (
    REPOSITORY_ROOT / "packages/contracts/src/holm_contracts/external_paths.py"
)
HOST_AGENT_SERVER_SOURCE: Final[Path] = (
    REPOSITORY_ROOT / "apps/host_agent/src/holm_host_agent/server.py"
)
HASH_A: Final[str] = "a" * 64
HASH_B: Final[str] = "b" * 64

# Deliberately not this process's identity.  Under root the mode bits mean
# nothing, so a derivation aimed at the running process would be untestable here.
RUNTIME_UID: Final[int] = 4_242
RUNTIME_GID: Final[int] = 4_343
STRANGER_UID: Final[int] = 5_252
STRANGER_GID: Final[int] = 5_353
RUNTIME_IDENTITY: Final[RuntimeIdentity] = RuntimeIdentity(uid=RUNTIME_UID, gid=RUNTIME_GID)

EXPECTED_ROLE_METHOD_ALLOWLIST: Final[Mapping[str, frozenset[str]]] = {
    "control-api": frozenset({"GetHostInfo", "ListGPUs", "StreamLogs"}),
    "download-worker": frozenset({"GetHostInfo", "ValidatePath"}),
    "instance-controller": frozenset(
        {
            "EnsureRuntime",
            "StopRuntime",
            "RemoveRuntime",
            "InspectRuntime",
            "ListRuntimes",
            "ListGPUs",
            "StreamLogs",
        }
    ),
}


# ------------------------------------------------------------------ fixtures
@dataclass(frozen=True, slots=True)
class ExternalTree:
    """One allowed root and one model directory inside it."""

    allowed_root: Path
    model: Path

    @property
    def policy(self) -> ExternalPathPolicy:
        return ExternalPathPolicy.for_configured_roots((self.allowed_root,))


def _assign(path: Path, *, uid: int, gid: int, mode: int) -> None:
    """Give one entry a foreign owner and an exact mode, or suspend the case.

    Assigning an owner needs privilege.  Where it is unavailable the shape
    cannot be built at all, which is ``BLOCKED_ENV`` -- never a skip that gets
    counted as a pass (the integration-test policy).
    """

    try:
        os.chown(path, uid, gid)
    except PermissionError as error:
        pytest.xfail(f"BLOCKED_ENV: cannot assign a foreign owner here ({error.strerror})")
    os.chmod(path, mode)


@contextlib.contextmanager
def traversable_ancestors(path: Path) -> Iterator[None]:
    """Grant ``o+x`` above ``path`` so the tree under test decides the answer.

    ``pytest``'s temporary root is ``0700`` and owned by this process, so
    without this every derivation for a foreign UID would stop at an ancestor
    the case did not build and each conclusion would be right by accident.
    Nothing inside the External Root is touched; the modes are restored.
    """

    restored: list[tuple[Path, int]] = []
    try:
        for ancestor in path.parents:
            if ancestor == Path("/"):
                continue
            mode = stat.S_IMODE(ancestor.lstat().st_mode)
            if mode & stat.S_IXOTH:
                continue
            restored.append((ancestor, mode))
            os.chmod(ancestor, mode | stat.S_IXOTH)
        yield
    finally:
        for ancestor, mode in reversed(restored):
            os.chmod(ancestor, mode)


def _readable_tree(tmp_path: Path, *, name: str = "models") -> ExternalTree:
    """A model directory the Runtime identity owns, reachable from the root."""

    allowed_root = tmp_path / name
    model = allowed_root / "synthetic-model"
    model.mkdir(parents=True)
    os.chmod(allowed_root, 0o755)
    _assign(model, uid=RUNTIME_UID, gid=RUNTIME_GID, mode=0o750)
    return ExternalTree(allowed_root=allowed_root, model=model)


def _manifest_data(service_identities: list[dict[str, object]]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "build_version": "0.0.0+gm207b0000000",
        "commit": "7" * 40,
        "image_digests": [{"name": "platform", "digest": f"sha256:{HASH_A}"}],
        "artifact_hashes": {
            "bundle": HASH_A,
            "compose": HASH_B,
            "caddyfile": HASH_A,
            "systemd_units": [{"name": "holm.service", "sha256": HASH_B}],
        },
        "service_identities": service_identities,
        "default_resource_limits": [
            {
                "service": "gateway",
                "cpu_millicores": 1000,
                "memory_mib": 512,
                "pids": 256,
                "log_rotation_mib": 10,
                "tmpfs_mib": 64,
            }
        ],
        "runtime_compat_hash": HASH_A,
        "python_lockfile_hash": HASH_A,
        "node_lockfile_hash": HASH_B,
        "gateway": {
            "max_inflight_requests": 512,
            "runtime_pool_max_connections_per_instance": 512,
            "boundary_semantics": "reject_when_current_is_greater_than_or_equal_to_limit",
        },
        "retention": {
            "request_records_days": 30,
            "download_tasks_days": 30,
            "telemetry_raw_seconds": 3600,
            "telemetry_aggregate_days": 30,
        },
        "default_timezone": "UTC",
    }


def _write_manifest(path: Path, service_identities: list[dict[str, object]]) -> Path:
    path.write_text(json.dumps(_manifest_data(service_identities)), encoding="utf-8")
    return path


# ---------------------------------------------------------------- judgements
def assert_agent_discloses_nothing(sentinels: Iterable[str], surfaces: Iterable[str]) -> None:
    """No Agent surface may quote the path, its ``realpath`` or an allowed root."""

    for sentinel in sentinels:
        for surface in surfaces:
            assert sentinel not in surface, f"{sentinel!r} escaped into an Agent surface"


def agent_surfaces(
    judgement: RuntimeReadabilityJudgement,
    captured: str,
) -> tuple[str, ...]:
    """Every place an answer can reach: the log, the value and the wire message."""

    response = readability_to_proto(judgement)
    return (
        captured,
        repr(judgement),
        str(judgement),
        str(response),
        response.SerializeToString().decode("latin-1"),
    )


def assert_runtime_identity_absence_is_unknown(
    module: Any,
    *,
    path: str,
    policy: ExternalPathPolicy,
) -> None:
    """A Manifest without a Runtime identity yields unknown, never a derived answer."""

    judgement = module.judge_runtime_readability(path, policy=policy, identity=None)
    assert judgement.conclusion is RuntimeReadabilityConclusion.UNKNOWN, (
        f"a missing Runtime identity produced {judgement.conclusion.value!r}"
    )
    assert judgement.unknown_reason is not None
    assert (
        judgement.unknown_reason.value
        == RuntimeReadabilityUnknownReason.RUNTIME_IDENTITY_UNAVAILABLE.value
    )


def assert_ancestor_without_execute_is_not_readable(
    module: Any,
    *,
    path: str,
    policy: ExternalPathPolicy,
) -> None:
    """An ancestor the Runtime cannot traverse makes the target unreachable."""

    judgement = module.judge_runtime_readability(
        path, policy=policy, identity=module.RuntimeIdentity(uid=RUNTIME_UID, gid=RUNTIME_GID)
    )
    assert judgement.conclusion.value == RuntimeReadabilityConclusion.NOT_READABLE.value, (
        f"an unreachable ancestor produced {judgement.conclusion.value!r}"
    )


def assert_extended_attributes_make_it_unknown(
    module: Any,
    *,
    path: str,
    policy: ExternalPathPolicy,
) -> None:
    """A derived-readable chain carrying an xattr is unknown, never readable."""

    judgement = module.judge_runtime_readability(
        path, policy=policy, identity=module.RuntimeIdentity(uid=RUNTIME_UID, gid=RUNTIME_GID)
    )
    assert judgement.conclusion.value == RuntimeReadabilityConclusion.UNKNOWN.value, (
        f"an ACL-bearing chain was answered {judgement.conclusion.value!r}"
    )
    assert judgement.unknown_reason is not None
    assert judgement.unknown_reason.value == RuntimeReadabilityUnknownReason.ACL_PRESENT.value


def assert_allowlist_is_verbatim(policy: Mapping[str, frozenset[str]]) -> None:
    """``ROLE_METHOD_ALLOWLIST`` is exactly what M1-07B/M1-09A froze."""

    assert dict(policy) == dict(EXPECTED_ROLE_METHOD_ALLOWLIST), "the role Allowlist changed"


def directory_tree_snapshot(root: Path) -> tuple[str, ...]:
    """Every entry under ``root`` with the metadata any write would disturb."""

    entries: list[str] = []
    for path in sorted([root, *root.rglob("*")]):
        info = path.lstat()
        parts = [
            str(path.relative_to(root.parent)),
            oct(info.st_mode),
            str(info.st_uid),
            str(info.st_gid),
            str(info.st_size),
            str(info.st_mtime_ns),
            str(info.st_ctime_ns),
        ]
        if stat.S_ISLNK(info.st_mode):
            parts.append(os.readlink(path))
        elif stat.S_ISREG(info.st_mode):
            parts.append(hashlib.sha256(path.read_bytes()).hexdigest())
        entries.append("|".join(parts))
    return tuple(entries)


def assert_no_external_root_writes(before: tuple[str, ...], after: tuple[str, ...]) -> None:
    """The single judgement shared by the real case and its positive control."""

    assert after == before, "the judgement modified the External Root"


# --------------------------------------------------- the derivation's subject
def test_TC_DL_26_the_derivation_is_about_a_uid_that_is_not_this_process() -> None:
    """Root bypasses the mode bits, so a case aimed at this process proves nothing.

    This is the reason no case here opens the path to cross-check the derived
    answer: in the development container that check would succeed no matter
    what the mode bits say.  A real-behaviour comparison needs a non-root
    process and is suspended, not skipped.
    """

    assert os.getuid() != RUNTIME_UID
    assert os.getgid() != RUNTIME_GID
    assert RUNTIME_UID != STRANGER_UID


def test_TC_DL_26_a_real_open_cross_check_is_blocked_env() -> None:
    if os.getuid() != 0:  # pragma: no cover - the container runs as root
        pytest.fail("this environment is no longer root; enable the real-behaviour check")
    pytest.xfail(
        "BLOCKED_ENV: root bypasses POSIX mode bits, so opening the path as a "
        "cross-check of the derivation cannot fail here; it needs a non-root runner"
    )


# ------------------------------------------------------------ UID
def test_TC_DL_26_a_manifest_without_a_runtime_identity_is_unknown(tmp_path: Path) -> None:
    """Today's Manifest names no Runtime, and the answer says so instead of guessing."""

    manifest = _write_manifest(
        tmp_path / "release-manifest.json",
        [{"service": "gateway", "uid": 21_001, "gid": 21_001}],
    )
    assert load_runtime_identity(manifest) is None

    tree = _readable_tree(tmp_path)
    with traversable_ancestors(tree.allowed_root):
        assert_runtime_identity_absence_is_unknown(
            agent_path_validation,
            path=str(tree.model),
            policy=tree.policy,
        )


def test_TC_DL_26_the_runtime_identity_comes_from_the_manifest(tmp_path: Path) -> None:
    manifest = _write_manifest(
        tmp_path / "release-manifest.json",
        [
            {"service": "gateway", "uid": 21_001, "gid": 21_001},
            {"service": RUNTIME_SERVICE_IDENTITY, "uid": RUNTIME_UID, "gid": RUNTIME_GID},
        ],
    )

    assert load_runtime_identity(manifest) == RUNTIME_IDENTITY
    # An unreadable, absent or malformed Manifest is an absence, not a default.
    assert load_runtime_identity(tmp_path / "absent.json") is None
    (tmp_path / "broken.json").write_text("{", encoding="utf-8")
    assert load_runtime_identity(tmp_path / "broken.json") is None


def test_TC_DL_26_a_runtime_identity_can_never_be_root() -> None:
    """``ServiceIdentity`` forbids UID 0, so the derivation never models root's bypass."""

    with pytest.raises(ValueError):
        RuntimeIdentity(uid=0, gid=RUNTIME_GID)
    with pytest.raises(ValueError):
        RuntimeIdentity(uid=RUNTIME_UID, gid=0)


def test_TC_DL_26_positive_control_falling_back_to_the_agent_uid_fails(tmp_path: Path) -> None:
    """Let the missing branch judge with this process's own UID; the guard must fail."""

    tree = _readable_tree(tmp_path)
    module = load_mutated_module(
        AGENT_JUDGEMENT_SOURCE,
        [
            (
                "    if identity is None:\n        return _unknown_runtime_identity()",
                "    if identity is None:\n"
                "        identity = RuntimeIdentity(uid=max(os.getuid(), 1), "
                "gid=max(os.getgid(), 1))",
            )
        ],
        module_name="_agent_uid_fallback_path_validation",
        destination=tmp_path / "uid_fallback.py",
    )

    with traversable_ancestors(tree.allowed_root), pytest.raises(AssertionError):
        assert_runtime_identity_absence_is_unknown(
            module,
            path=str(tree.model),
            policy=tree.policy,
        )


# --------------------------------------------------- the three states
def test_TC_DL_26_an_ancestor_without_execute_is_not_readable(tmp_path: Path) -> None:
    """The confident side: a mode bit that withholds permission withholds it."""

    allowed_root = tmp_path / "models"
    outer = allowed_root / "outer"
    model = outer / "synthetic-model"
    model.mkdir(parents=True)
    os.chmod(allowed_root, 0o755)
    _assign(model, uid=RUNTIME_UID, gid=RUNTIME_GID, mode=0o750)
    # The ancestor is owned by the Runtime but has no ``+x`` for anybody.
    _assign(outer, uid=RUNTIME_UID, gid=RUNTIME_GID, mode=0o600)

    with traversable_ancestors(allowed_root):
        assert_ancestor_without_execute_is_not_readable(
            agent_path_validation,
            path=str(model),
            policy=ExternalPathPolicy.for_configured_roots((allowed_root,)),
        )


@pytest.mark.parametrize(
    ("label", "uid", "gid", "mode"),
    [
        ("target_without_read", RUNTIME_UID, RUNTIME_GID, 0o310),
        ("target_without_execute", RUNTIME_UID, RUNTIME_GID, 0o600),
        ("owned_by_a_stranger", STRANGER_UID, STRANGER_GID, 0o750),
    ],
)
def test_TC_DL_26_a_target_the_mode_bits_refuse_is_not_readable(
    label: str,
    uid: int,
    gid: int,
    mode: int,
    tmp_path: Path,
) -> None:
    """Owner, group and other are one class each; POSIX does not fall through."""

    del label
    allowed_root = tmp_path / "models"
    model = allowed_root / "synthetic-model"
    model.mkdir(parents=True)
    os.chmod(allowed_root, 0o755)
    _assign(model, uid=uid, gid=gid, mode=mode)

    with traversable_ancestors(allowed_root):
        judgement = judge_runtime_readability(
            str(model),
            policy=ExternalPathPolicy.for_configured_roots((allowed_root,)),
            identity=RUNTIME_IDENTITY,
        )

    assert judgement.conclusion is RuntimeReadabilityConclusion.NOT_READABLE
    assert judgement.unknown_reason is None


def assert_owner_class_precedes_group_class(
    module: Any,
    *,
    path: str,
    policy: ExternalPathPolicy,
) -> None:
    judgement = module.judge_runtime_readability(
        path,
        policy=policy,
        identity=module.RuntimeIdentity(uid=RUNTIME_UID, gid=RUNTIME_GID),
    )
    assert judgement.conclusion.value == RuntimeReadabilityConclusion.NOT_READABLE.value


def test_TC_DL_26_owner_class_precedes_group_when_uid_and_gid_both_match(
    tmp_path: Path,
) -> None:
    tree = _readable_tree(tmp_path)
    # Owner has no permission; group has r-x.  POSIX selects owner and stops.
    _assign(tree.model, uid=RUNTIME_UID, gid=RUNTIME_GID, mode=0o057)

    with traversable_ancestors(tree.allowed_root):
        assert_owner_class_precedes_group_class(
            agent_path_validation,
            path=str(tree.model),
            policy=tree.policy,
        )


def test_TC_DL_26_positive_control_group_before_owner_fails(
    tmp_path: Path,
) -> None:
    tree = _readable_tree(tmp_path)
    _assign(tree.model, uid=RUNTIME_UID, gid=RUNTIME_GID, mode=0o057)
    module = load_mutated_module(
        AGENT_JUDGEMENT_SOURCE,
        [
            (
                "    if entry.st_uid == identity.uid:\n"
                "        return (entry.st_mode & stat.S_IRWXU) >> 6\n"
                "    if entry.st_gid == identity.gid:\n"
                "        return (entry.st_mode & stat.S_IRWXG) >> 3",
                "    if entry.st_gid == identity.gid:\n"
                "        return (entry.st_mode & stat.S_IRWXG) >> 3\n"
                "    if entry.st_uid == identity.uid:\n"
                "        return (entry.st_mode & stat.S_IRWXU) >> 6",
            )
        ],
        module_name="_group_before_owner_path_validation",
        destination=tmp_path / "group_before_owner.py",
    )

    with traversable_ancestors(tree.allowed_root), pytest.raises(AssertionError):
        assert_owner_class_precedes_group_class(
            module,
            path=str(tree.model),
            policy=tree.policy,
        )


def test_TC_DL_26_a_derived_readable_chain_without_attributes_is_readable(
    tmp_path: Path,
) -> None:
    """The refusals mean something only because the readable side is reachable."""

    tree = _readable_tree(tmp_path)
    with traversable_ancestors(tree.allowed_root):
        judgement = judge_runtime_readability(
            str(tree.model), policy=tree.policy, identity=RUNTIME_IDENTITY
        )

    assert judgement.conclusion is RuntimeReadabilityConclusion.READABLE
    assert judgement.unknown_reason is None


def _tree_with_extended_attribute(tmp_path: Path, *, name: str = "models") -> ExternalTree:
    tree = _readable_tree(tmp_path, name=name)
    try:
        os.setxattr(tree.model, b"user.m2-07b-probe", b"1")
    except OSError as error:
        pytest.xfail(f"BLOCKED_ENV: extended attributes unavailable here ({error.strerror})")
    return tree


def test_TC_DL_26_extended_attributes_make_a_readable_chain_unknown(tmp_path: Path) -> None:
    """The derivation says readable; an xattr means it may be overruled, so: unknown."""

    tree = _tree_with_extended_attribute(tmp_path)
    with traversable_ancestors(tree.allowed_root):
        assert_extended_attributes_make_it_unknown(
            agent_path_validation,
            path=str(tree.model),
            policy=tree.policy,
        )


def test_TC_DL_26_an_ancestor_attribute_makes_a_readable_chain_unknown(
    tmp_path: Path,
) -> None:
    """Every ancestor is part of the derivation, not just the target directory."""

    tree = _readable_tree(tmp_path)
    try:
        os.setxattr(tree.allowed_root, b"user.m2-07b-ancestor-probe", b"1")
    except OSError as error:
        pytest.xfail(f"BLOCKED_ENV: extended attributes unavailable here ({error.strerror})")

    with traversable_ancestors(tree.allowed_root):
        assert_extended_attributes_make_it_unknown(
            agent_path_validation,
            path=str(tree.model),
            policy=tree.policy,
        )


def test_TC_DL_26_positive_control_scanning_only_the_target_for_attributes_fails(
    tmp_path: Path,
) -> None:
    tree = _readable_tree(tmp_path)
    try:
        os.setxattr(tree.allowed_root, b"user.m2-07b-ancestor-control", b"1")
    except OSError as error:
        pytest.xfail(f"BLOCKED_ENV: extended attributes unavailable here ({error.strerror})")
    module = load_mutated_module(
        AGENT_JUDGEMENT_SOURCE,
        [
            (
                "    for element in _path_chain(resolved):",
                "    for element in (resolved,):",
            )
        ],
        module_name="_target_only_xattr_path_validation",
        destination=tmp_path / "target_only_xattr.py",
    )

    with traversable_ancestors(tree.allowed_root), pytest.raises(AssertionError):
        assert_extended_attributes_make_it_unknown(
            module,
            path=str(tree.model),
            policy=tree.policy,
        )


def test_TC_DL_26_positive_control_treating_an_acl_as_readable_fails(tmp_path: Path) -> None:
    """Fold the unknown into readable in production; the guard must fail."""

    tree = _tree_with_extended_attribute(tmp_path)
    module = load_mutated_module(
        AGENT_JUDGEMENT_SOURCE,
        [
            (
                "    if _chain_has_extended_attributes(resolved):\n"
                "        return _unknown_acl_present()",
                "    if _chain_has_extended_attributes(resolved):\n        return _readable()",
            )
        ],
        module_name="_acl_is_readable_path_validation",
        destination=tmp_path / "acl_readable.py",
    )

    with traversable_ancestors(tree.allowed_root), pytest.raises(AssertionError):
        assert_extended_attributes_make_it_unknown(
            module,
            path=str(tree.model),
            policy=tree.policy,
        )


def test_TC_DL_26_positive_control_skipping_the_ancestors_fails(tmp_path: Path) -> None:
    """Judge the target alone in production; the ancestor case must fail."""

    allowed_root = tmp_path / "models"
    outer = allowed_root / "outer"
    model = outer / "synthetic-model"
    model.mkdir(parents=True)
    os.chmod(allowed_root, 0o755)
    _assign(model, uid=RUNTIME_UID, gid=RUNTIME_GID, mode=0o750)
    _assign(outer, uid=RUNTIME_UID, gid=RUNTIME_GID, mode=0o600)
    module = load_mutated_module(
        AGENT_JUDGEMENT_SOURCE,
        [("    chain = _path_chain(resolved)", "    chain = (resolved,)")],
        module_name="_no_ancestor_path_validation",
        destination=tmp_path / "no_ancestors.py",
    )

    with traversable_ancestors(allowed_root), pytest.raises(AssertionError):
        assert_ancestor_without_execute_is_not_readable(
            module,
            path=str(model),
            policy=ExternalPathPolicy.for_configured_roots((allowed_root,)),
        )


# ------------------------- the fail-closed fallbacks inside the derivation
#
# The derivation has three places where it cannot finish what it started, and
# all three are the dangerous direction if they guess: "I could not list the
# extended attributes" must not become "there are none", and "I could not stat
# this directory" must not become "the Runtime can traverse it".  Each of the
# three was reachable but **unseen** by the suite until now -- flipping any of
# them left 378 passed / exit 0 -- so each gets its own case and its own
# control below.  The two other fail-closed branches (`policy is None` and the
# NUL-byte `ValueError`) were already covered; flipping those to `_readable()`
# fails 3 and 2 cases respectively, so they are deliberately not duplicated
# here.


@contextlib.contextmanager
def os_call_failing_for(
    name: str,
    target: Path,
    *,
    error: OSError,
    callers: frozenset[str] | None = None,
) -> Iterator[None]:
    """Make one ``os`` call fail for one path, from one call site, and nowhere else.

    These branches fire when an inspection the Agent already began cannot be
    finished: a filesystem without extended attributes answers ``ENOTSUP``, a
    directory can answer ``EACCES``, and an entry can vanish between the shared
    rule's ``realpath`` and the derivation's own ``lstat``.  Building a real
    ``ENOTSUP`` mount needs privileges unavailable in this test environment, and pretending
    to build one would be worse than injecting the errno at the call site --
    the branch under test is the Agent's *reaction* to the error, not the
    kernel's production of it.

    ``callers`` names the production function whose failure path is being
    covered, which is what lets the *target's* second stat fail while the
    bounds check's first stat succeeds.  If that function is ever renamed the
    injection stops firing and the case fails loudly rather than covering
    nothing.
    """

    real = getattr(os, name)

    def fake(path: Any, *arguments: Any, **keywords: Any) -> Any:
        try:
            same_path = os.fspath(path) == str(target)
        except TypeError:  # pragma: no cover - a bytes path never reaches here
            same_path = False
        if same_path and (callers is None or sys._getframe(1).f_code.co_name in callers):
            raise error
        return real(path, *arguments, **keywords)

    setattr(os, name, fake)
    try:
        yield
    finally:
        setattr(os, name, real)


def _runtime_identity_of(module: Any) -> Any:
    return module.RuntimeIdentity(uid=RUNTIME_UID, gid=RUNTIME_GID)


def assert_unlistable_attributes_are_unknown(
    module: Any,
    *,
    path: str,
    policy: ExternalPathPolicy,
    unlistable: Path,
) -> None:
    """Not being able to rule an ACL out is not the same as there being none."""

    with os_call_failing_for(
        "listxattr",
        unlistable,
        error=OSError(errno.ENOTSUP, "extended attributes not supported"),
    ):
        judgement = module.judge_runtime_readability(
            path, policy=policy, identity=_runtime_identity_of(module)
        )

    assert judgement.conclusion.value == RuntimeReadabilityConclusion.UNKNOWN.value, (
        f"an unlistable chain element was answered {judgement.conclusion.value!r}"
    )
    assert judgement.unknown_reason is not None
    assert judgement.unknown_reason.value == RuntimeReadabilityUnknownReason.ACL_PRESENT.value


def test_TC_DL_26_an_unlistable_extended_attribute_is_unknown_not_readable(
    tmp_path: Path,
) -> None:
    """``listxattr`` failing must not be read as "this chain carries no ACL"."""

    tree = _readable_tree(tmp_path)
    with traversable_ancestors(tree.allowed_root):
        assert_unlistable_attributes_are_unknown(
            agent_path_validation,
            path=str(tree.model),
            policy=tree.policy,
            unlistable=tree.model,
        )


def test_TC_DL_26_positive_control_an_unlistable_probe_read_as_no_acl_fails(
    tmp_path: Path,
) -> None:
    """Turn the probe's own failure path into "no ACL"; this case must fail.

    The existing control breaks the ``_unknown_acl_present`` *return*; this one
    breaks the *probe*, which is a different branch and was invisible to the
    whole suite.
    """

    tree = _readable_tree(tmp_path)
    module = load_mutated_module(
        AGENT_JUDGEMENT_SOURCE,
        [
            (
                "            if os.listxattr(element, follow_symlinks=False):\n"
                "                return True\n"
                "        except OSError:\n"
                "            return True",
                "            if os.listxattr(element, follow_symlinks=False):\n"
                "                return True\n"
                "        except OSError:\n"
                "            return False",
            )
        ],
        module_name="_unlistable_is_clean_path_validation",
        destination=tmp_path / "unlistable_is_clean.py",
    )

    with traversable_ancestors(tree.allowed_root), pytest.raises(AssertionError):
        assert_unlistable_attributes_are_unknown(
            module,
            path=str(tree.model),
            policy=tree.policy,
            unlistable=tree.model,
        )


def assert_uninspectable_ancestor_is_not_readable(
    module: Any,
    *,
    path: str,
    policy: ExternalPathPolicy,
    uninspectable: Path,
) -> None:
    """An ancestor the Agent cannot stat is not an ancestor the Runtime can traverse."""

    with os_call_failing_for(
        "lstat",
        uninspectable,
        error=OSError(errno.EACCES, "permission denied"),
        callers=frozenset({"_runtime_can_read"}),
    ):
        judgement = module.judge_runtime_readability(
            path, policy=policy, identity=_runtime_identity_of(module)
        )

    assert judgement.conclusion.value == RuntimeReadabilityConclusion.NOT_READABLE.value, (
        f"an uninspectable ancestor was answered {judgement.conclusion.value!r}"
    )


def test_TC_DL_26_an_ancestor_that_cannot_be_inspected_is_not_readable(tmp_path: Path) -> None:
    tree = _readable_tree(tmp_path)
    with traversable_ancestors(tree.allowed_root):
        assert_uninspectable_ancestor_is_not_readable(
            agent_path_validation,
            path=str(tree.model),
            policy=tree.policy,
            uninspectable=tree.allowed_root,
        )


def test_TC_DL_26_positive_control_an_uninspectable_ancestor_read_as_traversable_fails(
    tmp_path: Path,
) -> None:
    tree = _readable_tree(tmp_path)
    module = load_mutated_module(
        AGENT_JUDGEMENT_SOURCE,
        [
            (
                "            entry = os.lstat(ancestor)\n"
                "        except OSError:\n"
                "            return False",
                "            entry = os.lstat(ancestor)\n"
                "        except OSError:\n"
                "            return True",
            )
        ],
        module_name="_uninspectable_ancestor_path_validation",
        destination=tmp_path / "uninspectable_ancestor.py",
    )

    with traversable_ancestors(tree.allowed_root), pytest.raises(AssertionError):
        assert_uninspectable_ancestor_is_not_readable(
            module,
            path=str(tree.model),
            policy=tree.policy,
            uninspectable=tree.allowed_root,
        )


def assert_vanishing_target_is_not_readable(
    module: Any,
    *,
    path: str,
    policy: ExternalPathPolicy,
    vanishing: Path,
) -> None:
    """The entry passed the bounds check and was gone by the time it was derived.

    The bounds check's ``lstat`` still succeeds -- only the derivation's own
    stat fails -- so this reaches the second fallback rather than being refused
    earlier for the ordinary reason.
    """

    with os_call_failing_for(
        "lstat",
        vanishing,
        error=OSError(errno.ENOENT, "no such file or directory"),
        callers=frozenset({"_runtime_can_read"}),
    ):
        judgement = module.judge_runtime_readability(
            path, policy=policy, identity=_runtime_identity_of(module)
        )

    assert judgement.conclusion.value == RuntimeReadabilityConclusion.NOT_READABLE.value, (
        f"a target that vanished was answered {judgement.conclusion.value!r}"
    )


def test_TC_DL_26_a_target_that_vanishes_after_the_bounds_check_is_not_readable(
    tmp_path: Path,
) -> None:
    tree = _readable_tree(tmp_path)
    with traversable_ancestors(tree.allowed_root):
        assert_vanishing_target_is_not_readable(
            agent_path_validation,
            path=str(tree.model),
            policy=tree.policy,
            vanishing=tree.model,
        )


def test_TC_DL_26_positive_control_a_vanished_target_read_as_readable_fails(
    tmp_path: Path,
) -> None:
    tree = _readable_tree(tmp_path)
    module = load_mutated_module(
        AGENT_JUDGEMENT_SOURCE,
        [
            (
                "        target = os.lstat(chain[-1])\n    except OSError:\n        return False",
                "        target = os.lstat(chain[-1])\n    except OSError:\n        return True",
            )
        ],
        module_name="_vanished_target_path_validation",
        destination=tmp_path / "vanished_target.py",
    )

    with traversable_ancestors(tree.allowed_root), pytest.raises(AssertionError):
        assert_vanishing_target_is_not_readable(
            module,
            path=str(tree.model),
            policy=tree.policy,
            vanishing=tree.model,
        )


def test_TC_DL_26_an_unknown_always_carries_a_reason() -> None:
    """The result type forbids the shape a missing case would take."""

    with pytest.raises(ValueError):
        RuntimeReadabilityJudgement(RuntimeReadabilityConclusion.UNKNOWN, None)
    with pytest.raises(ValueError):
        RuntimeReadabilityJudgement(
            RuntimeReadabilityConclusion.READABLE,
            RuntimeReadabilityUnknownReason.ACL_PRESENT,
        )
    assert set(RuntimeReadabilityUnknownReason) == {
        RuntimeReadabilityUnknownReason.RUNTIME_IDENTITY_UNAVAILABLE,
        RuntimeReadabilityUnknownReason.ACL_PRESENT,
    }


BRANCH_CONSTRUCTORS: Final[frozenset[str]] = frozenset(
    {"_readable", "_not_readable", "_unknown_runtime_identity", "_unknown_acl_present"}
)


def _judgement_constructing_functions(source_text: str, filename: str) -> frozenset[str]:
    tree = ast.parse(source_text, filename=filename)
    building: set[str] = set()
    for function in ast.walk(tree):
        if not isinstance(function, ast.FunctionDef):
            continue
        for node in ast.walk(function):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "RuntimeReadabilityJudgement"
            ):
                building.add(function.name)
    return frozenset(building)


def test_TC_DL_26_the_judgement_has_exactly_four_branches() -> None:
    """Set equality, so a fifth branch and a deleted branch both fail.

    A superset check would let an unused fifth conclusion appear without any
    case ever reaching it, which is how a branch escapes the leak scan below.
    """

    assert (
        _judgement_constructing_functions(
            AGENT_JUDGEMENT_SOURCE.read_text(encoding="utf-8"),
            str(AGENT_JUDGEMENT_SOURCE),
        )
        == BRANCH_CONSTRUCTORS
    )


def test_TC_DL_26_positive_control_a_fifth_branch_is_caught(tmp_path: Path) -> None:
    mutated = mutate_source(
        AGENT_JUDGEMENT_SOURCE,
        [
            (
                "def _readable() -> RuntimeReadabilityJudgement:",
                "def _probably_readable() -> RuntimeReadabilityJudgement:\n"
                "    return RuntimeReadabilityJudgement("
                "RuntimeReadabilityConclusion.READABLE, None)\n\n\n"
                "def _readable() -> RuntimeReadabilityJudgement:",
            )
        ],
    )
    destination = tmp_path / "fifth_branch.py"
    destination.write_text(mutated, encoding="utf-8")

    assert _judgement_constructing_functions(mutated, str(destination)) != BRANCH_CONSTRUCTORS


# ------------------------------------------- the request surface
def test_TC_SEC_17_validate_path_asks_for_a_path_and_nothing_else() -> None:
    """A caller that could name the UID would be choosing its own answer."""

    fields = {field.name for field in agent_pb2.ValidatePathRequest.DESCRIPTOR.fields}
    assert fields == {"path"}
    with pytest.raises(AssertionError):
        assert fields | {"uid"} == {"path"}

    assert_protocol_request_surface_safe()
    assert_method_policy_complete()


def test_TC_SEC_17_the_request_surface_guard_still_bites() -> None:
    """The two frozen guards pass on the new field and still reject the old shapes."""

    for forbidden in ("args", "command", "docker_args", "entrypoint", "env"):
        with pytest.raises(AssertionError, match=forbidden):
            assert_protocol_request_surface_safe(extra_field_names=(forbidden,))


def test_TC_SEC_17_the_response_states_unknown_without_a_value() -> None:
    """An unknown carries its reason and no value, exactly as ``ObservedString`` does."""

    unknown = readability_to_proto(
        RuntimeReadabilityJudgement(
            RuntimeReadabilityConclusion.UNKNOWN,
            RuntimeReadabilityUnknownReason.RUNTIME_IDENTITY_UNAVAILABLE,
        )
    ).runtime_readability
    assert unknown.state == agent_pb2.KNOWLEDGE_STATE_UNKNOWN
    assert not unknown.HasField("value")
    assert (
        unknown.unknown_reason
        == agent_pb2.RUNTIME_READABILITY_UNKNOWN_REASON_RUNTIME_IDENTITY_UNAVAILABLE
    )

    known = readability_to_proto(
        RuntimeReadabilityJudgement(RuntimeReadabilityConclusion.NOT_READABLE, None)
    ).runtime_readability
    assert known.state == agent_pb2.KNOWLEDGE_STATE_KNOWN
    assert known.HasField("value")
    assert known.value == agent_pb2.RUNTIME_READABILITY_NOT_READABLE


# ------------------------------------------------ the Allowlist
def test_TC_SEC_17_the_role_allowlist_is_unchanged() -> None:
    assert_allowlist_is_verbatim(ROLE_METHOD_ALLOWLIST)
    assert allowed_methods_for_service("control-api") == frozenset(
        {"GetHostInfo", "ListGPUs", "StreamLogs"}
    )
    assert "ValidatePath" in allowed_methods_for_service("download-worker")


def test_TC_SEC_17_positive_control_granting_validate_path_to_control_api_fails() -> None:
    """Implementing the method must not have widened who may call it."""

    widened = dict(ROLE_METHOD_ALLOWLIST)
    widened["control-api"] = widened["control-api"] | {"ValidatePath"}
    with pytest.raises(AssertionError):
        assert_allowlist_is_verbatim(widened)


def test_TC_SEC_17_control_api_is_still_denied_validate_path() -> None:
    catalog = IdentityCatalog.from_pairs(((21_004, "control-api"),))
    decision = authorize_rpc(peer_uid=21_004, rpc_method="ValidatePath", identities=catalog)

    assert not decision.allowed
    assert decision.reason is AuthorizationDenialReason.METHOD_NOT_ALLOWED


def test_TC_SEC_17_the_denial_audit_field_set_did_not_grow(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No path-shaped field joined the auditor because a path method now exists."""

    with caplog.at_level(logging.WARNING):
        StructuredDenialAuditor().record(
            peer_uid=21_004,
            rpc_method="ValidatePath",
            reason=AuthorizationDenialReason.METHOD_NOT_ALLOWED,
        )

    payload = json.loads(caplog.records[-1].message)
    assert payload.keys() == {"event", "peer_uid", "rpc_method", "reason", "timestamp"}


# ------------------------------------- one definition of the rule
def assert_external_path_rule_has_one_definition(
    sources: Mapping[Path, str],
) -> None:
    """Reject synchronous and asynchronous duplicate rule definitions alike."""

    assert sources, "the definition-source guard discovered zero Python files"
    defining: set[Path] = set()
    for source, text in sources.items():
        tree = ast.parse(text, filename=str(source))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in {
                "judge_external_path",
                "path_is_within",
            }:
                defining.add(source)
    assert defining == {SHARED_RULE_SOURCE}, (
        "the external path boundary rule must have exactly one definition source: "
        f"{sorted(map(str, defining))}"
    )


def test_TC_DL_26_the_out_of_bounds_rule_has_exactly_one_definition_source() -> None:
    """Both sides call the same function; neither redefines it."""

    source_paths = tuple(sorted((REPOSITORY_ROOT / "apps").rglob("*.py"))) + tuple(
        sorted((REPOSITORY_ROOT / "packages").rglob("*.py"))
    )
    assert_external_path_rule_has_one_definition(
        {source: source.read_text(encoding="utf-8") for source in source_paths}
    )

    for consumer in (CONTROL_PLANE_SOURCE, AGENT_JUDGEMENT_SOURCE):
        text = consumer.read_text(encoding="utf-8")
        assert "from holm_contracts.external_paths import" in text, consumer


def test_TC_DL_26_positive_control_an_async_second_definition_source_fails(
    tmp_path: Path,
) -> None:
    duplicate = tmp_path / "async_duplicate.py"
    duplicate_source = (
        "async def judge_external_path(untrusted_path: str, policy: object) -> object:\n"
        "    return policy\n"
    )
    sources = {
        SHARED_RULE_SOURCE: SHARED_RULE_SOURCE.read_text(encoding="utf-8"),
        duplicate: duplicate_source,
    }

    with pytest.raises(AssertionError, match="exactly one definition source"):
        assert_external_path_rule_has_one_definition(sources)


def test_TC_DL_26_positive_control_loosening_the_control_plane_leaves_the_agent_refusing(
    tmp_path: Path,
) -> None:
    """Make the control plane's copy accept everything; the Agent must still refuse.

    This is the shape the shared rule exists to prevent.  Before the extraction
    a second Agent-side implementation could drift from this one silently; now
    loosening one side cannot move the other, and the Agent -- the
    higher-privileged side -- redoes the judgement itself.
    """

    tree = _readable_tree(tmp_path)
    outside = tmp_path / "elsewhere" / "synthetic-model"
    outside.mkdir(parents=True)
    loosened = load_mutated_module(
        CONTROL_PLANE_SOURCE,
        [
            (
                "BLOCKING_WORKER_LIMIT: Final[int] = 4",
                "def judge_external_path(untrusted_path: str, policy: object) "
                "-> ExternalPathVerdict:\n"
                "    return ExternalPathVerdict(accepted=True, rule=None)\n\n\n"
                "BLOCKING_WORKER_LIMIT: Final[int] = 4",
            )
        ],
        module_name="_loosened_external_path_resolution",
        destination=tmp_path / "loosened_control_plane.py",
    )

    assert loosened.judge_external_path(str(outside), tree.policy).accepted, (
        "the control must actually have loosened the control plane"
    )
    with traversable_ancestors(tree.allowed_root):
        judgement = judge_runtime_readability(
            str(outside), policy=tree.policy, identity=RUNTIME_IDENTITY
        )
    assert judgement.conclusion is RuntimeReadabilityConclusion.NOT_READABLE


def assert_a_retargeted_second_resolution_is_refused(
    module: Any,
    *,
    untrusted: Path,
    first_resolution: Path,
    second_resolution: Path,
    policy: ExternalPathPolicy,
) -> None:
    """The boundary and permission derivation must agree on the resolved path."""

    real_realpath = os.path.realpath
    resolutions = iter((str(first_resolution), str(second_resolution)))
    calls = 0

    def changing_realpath(path: Any, *arguments: Any, **keywords: Any) -> str:
        nonlocal calls
        if os.fspath(path) == str(untrusted):
            calls += 1
            return next(resolutions)
        return real_realpath(path, *arguments, **keywords)

    os.path.realpath = changing_realpath
    try:
        judgement = module.judge_runtime_readability(
            str(untrusted),
            policy=policy,
            identity=module.RuntimeIdentity(uid=RUNTIME_UID, gid=RUNTIME_GID),
        )
    finally:
        os.path.realpath = real_realpath

    assert calls == 2, "the probe must exercise both resolutions exactly once"
    assert judgement.conclusion.value == RuntimeReadabilityConclusion.NOT_READABLE.value


def test_TC_DL_26_a_retargeted_second_resolution_cannot_escape_the_allowed_root(
    tmp_path: Path,
) -> None:
    tree = _readable_tree(tmp_path)
    outside = tmp_path / "outside" / "synthetic-model"
    outside.mkdir(parents=True)
    _assign(outside, uid=RUNTIME_UID, gid=RUNTIME_GID, mode=0o750)
    untrusted = tree.allowed_root / "moving-link"

    with traversable_ancestors(tree.allowed_root):
        assert_a_retargeted_second_resolution_is_refused(
            agent_path_validation,
            untrusted=untrusted,
            first_resolution=tree.model,
            second_resolution=outside,
            policy=tree.policy,
        )


def test_TC_DL_26_positive_control_omitting_the_second_boundary_check_fails(
    tmp_path: Path,
) -> None:
    tree = _readable_tree(tmp_path)
    outside = tmp_path / "outside" / "synthetic-model"
    outside.mkdir(parents=True)
    _assign(outside, uid=RUNTIME_UID, gid=RUNTIME_GID, mode=0o750)
    untrusted = tree.allowed_root / "moving-link"
    module = load_mutated_module(
        AGENT_JUDGEMENT_SOURCE,
        [
            (
                "    if not any(path_is_within(resolved, root) for root in policy.allowed_roots):\n"
                "        return _not_readable()\n",
                "",
            )
        ],
        module_name="_missing_second_boundary_check_path_validation",
        destination=tmp_path / "missing_second_boundary_check.py",
    )

    with traversable_ancestors(tree.allowed_root), pytest.raises(AssertionError):
        assert_a_retargeted_second_resolution_is_refused(
            module,
            untrusted=untrusted,
            first_resolution=tree.model,
            second_resolution=outside,
            policy=tree.policy,
        )


@pytest.mark.parametrize(
    "untrusted",
    [
        "relative/model",
        "",
        "/nonexistent-root/model",
        "/etc",
    ],
)
def test_TC_DL_26_paths_outside_the_configured_roots_are_refused(
    untrusted: str,
    tmp_path: Path,
) -> None:
    tree = _readable_tree(tmp_path)
    judgement = judge_runtime_readability(untrusted, policy=tree.policy, identity=RUNTIME_IDENTITY)

    assert judgement.conclusion is RuntimeReadabilityConclusion.NOT_READABLE


def test_TC_DL_26_a_path_with_a_null_byte_is_refused_not_raised_or_logged(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``realpath`` raises on an embedded NUL; the Agent answers instead of failing."""

    tree = _readable_tree(tmp_path)
    untrusted = f"{tree.model}\x00m2-07b-null-branch-sentinel"
    with caplog.at_level(logging.DEBUG):
        judgement = judge_runtime_readability(
            untrusted, policy=tree.policy, identity=RUNTIME_IDENTITY
        )

    assert judgement.conclusion is RuntimeReadabilityConclusion.NOT_READABLE
    assert caplog.text, "the NUL-byte refusal must actually log something to scan"
    assert_agent_discloses_nothing(
        ("m2-07b-null-branch-sentinel", str(tree.allowed_root)),
        agent_surfaces(judgement, caplog.text),
    )


def test_TC_DL_26_positive_control_a_null_byte_refusal_log_leak_is_caught(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    tree = _readable_tree(tmp_path)
    module = _leaking_module(
        tmp_path,
        label="null_byte",
        anchor=(
            "    except ValueError:\n"
            "        # An embedded NUL byte cannot name a directory; ``realpath`` raises on\n"
            "        # it rather than returning.  It is a refusal, not an exception the\n"
            "        # caller sees.\n"
            "        return _not_readable()"
        ),
        leaked="untrusted_path",
    )
    untrusted = f"{tree.model}\x00m2-07b-null-control-sentinel"

    with caplog.at_level(logging.DEBUG):
        judgement = module.judge_runtime_readability(
            untrusted,
            policy=tree.policy,
            identity=module.RuntimeIdentity(uid=RUNTIME_UID, gid=RUNTIME_GID),
        )

    with pytest.raises(AssertionError):
        assert_agent_discloses_nothing(
            ("m2-07b-null-control-sentinel",),
            agent_surfaces(judgement, caplog.text),
        )


def test_TC_DL_26_an_unconfigured_agent_refuses_every_path_without_logging_it(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No allowed root is not "every root"; it is no root."""

    tree = _readable_tree(tmp_path, name="m2-07b-no-policy-branch-sentinel")
    with traversable_ancestors(tree.allowed_root), caplog.at_level(logging.DEBUG):
        judgement = judge_runtime_readability(
            str(tree.model), policy=None, identity=RUNTIME_IDENTITY
        )

    assert judgement.conclusion is RuntimeReadabilityConclusion.NOT_READABLE
    assert caplog.text, "the unconfigured-policy refusal must actually log something to scan"
    assert_agent_discloses_nothing(
        ("m2-07b-no-policy-branch-sentinel", str(tree.allowed_root)),
        agent_surfaces(judgement, caplog.text),
    )
    with pytest.raises(ValueError):
        ExternalPathPolicy(allowed_roots=())


def test_TC_DL_26_positive_control_an_unconfigured_policy_log_leak_is_caught(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    tree = _readable_tree(tmp_path, name="m2-07b-no-policy-control-sentinel")
    module = _leaking_module(
        tmp_path,
        label="no_policy",
        anchor=(
            "    if policy is None:\n"
            "        # No allowed root is configured, so no path is inside one.  Refusing is\n"
            "        # the fail-closed reading; there is no branch where an unconfigured\n"
            '        # Agent answers "readable".\n'
            "        return _not_readable()"
        ),
        leaked="untrusted_path",
    )

    with caplog.at_level(logging.DEBUG):
        judgement = module.judge_runtime_readability(
            str(tree.model),
            policy=None,
            identity=module.RuntimeIdentity(uid=RUNTIME_UID, gid=RUNTIME_GID),
        )

    with pytest.raises(AssertionError):
        assert_agent_discloses_nothing(
            ("m2-07b-no-policy-control-sentinel",),
            agent_surfaces(judgement, caplog.text),
        )


def test_TC_DL_26_the_ancestor_test_is_still_by_path_segment(tmp_path: Path) -> None:
    """``/srv/models-evil/x`` is not inside ``/srv/models``, on the Agent side too."""

    allowed_root = tmp_path / "models"
    (allowed_root / "inside").mkdir(parents=True)
    sibling = tmp_path / "models-evil" / "x"
    sibling.mkdir(parents=True)
    os.chmod(allowed_root, 0o755)
    _assign(allowed_root / "inside", uid=RUNTIME_UID, gid=RUNTIME_GID, mode=0o750)
    _assign(sibling, uid=RUNTIME_UID, gid=RUNTIME_GID, mode=0o750)
    policy = ExternalPathPolicy.for_configured_roots((allowed_root,))

    with traversable_ancestors(allowed_root):
        inside = judge_runtime_readability(
            str(allowed_root / "inside"), policy=policy, identity=RUNTIME_IDENTITY
        )
        outside = judge_runtime_readability(str(sibling), policy=policy, identity=RUNTIME_IDENTITY)

    assert inside.conclusion is RuntimeReadabilityConclusion.READABLE
    assert outside.conclusion is RuntimeReadabilityConclusion.NOT_READABLE


# ------------------------- four branches, four leak scans, four controls
def test_TC_DL_26_a_readable_answer_discloses_nothing(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    tree = _readable_tree(tmp_path, name="m2-07b-readable-root-sentinel")
    sentinels = (
        "m2-07b-readable-root-sentinel",
        "synthetic-model",
        os.path.realpath(tree.model),
        str(tree.allowed_root),
    )

    with traversable_ancestors(tree.allowed_root), caplog.at_level(logging.DEBUG):
        judgement = judge_runtime_readability(
            str(tree.model), policy=tree.policy, identity=RUNTIME_IDENTITY
        )
        captured = caplog.text

    assert judgement.conclusion is RuntimeReadabilityConclusion.READABLE
    assert captured, "the readable branch must actually have logged something to scan"
    assert_agent_discloses_nothing(sentinels, agent_surfaces(judgement, captured))


def test_TC_DL_26_a_not_readable_answer_discloses_nothing(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Both refusal sites are scanned: out of bounds, and the mode bits."""

    tree = _readable_tree(tmp_path, name="m2-07b-refused-root-sentinel")
    _assign(tree.model, uid=STRANGER_UID, gid=STRANGER_GID, mode=0o700)
    outside = tmp_path / "m2-07b-outside-sentinel" / "m2-07b-leaf-sentinel"
    outside.mkdir(parents=True)
    sentinels = (
        "m2-07b-refused-root-sentinel",
        "m2-07b-outside-sentinel",
        "m2-07b-leaf-sentinel",
        os.path.realpath(outside),
        str(tree.allowed_root),
    )

    with traversable_ancestors(tree.allowed_root), caplog.at_level(logging.DEBUG):
        refused_by_bounds = judge_runtime_readability(
            str(outside), policy=tree.policy, identity=RUNTIME_IDENTITY
        )
        refused_by_mode = judge_runtime_readability(
            str(tree.model), policy=tree.policy, identity=RUNTIME_IDENTITY
        )
        captured = caplog.text

    assert refused_by_bounds.conclusion is RuntimeReadabilityConclusion.NOT_READABLE
    assert refused_by_mode.conclusion is RuntimeReadabilityConclusion.NOT_READABLE
    # The two refusals are the same answer: a caller cannot tell "outside the
    # roots" from "the mode bits refuse" and map the server one probe at a time.
    assert repr(refused_by_bounds) == repr(refused_by_mode)
    assert captured, "the refusal branch must actually have logged something to scan"
    assert_agent_discloses_nothing(
        sentinels,
        (*agent_surfaces(refused_by_bounds, captured), *agent_surfaces(refused_by_mode, "")),
    )


def test_TC_DL_26_an_unknown_runtime_identity_answer_discloses_nothing(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    tree = _readable_tree(tmp_path, name="m2-07b-unknown-identity-root-sentinel")
    sentinels = (
        "m2-07b-unknown-identity-root-sentinel",
        os.path.realpath(tree.model),
        str(tree.allowed_root),
    )

    with traversable_ancestors(tree.allowed_root), caplog.at_level(logging.DEBUG):
        judgement = judge_runtime_readability(str(tree.model), policy=tree.policy, identity=None)
        captured = caplog.text

    assert judgement.unknown_reason is RuntimeReadabilityUnknownReason.RUNTIME_IDENTITY_UNAVAILABLE
    assert captured, "the unknown-identity branch must actually have logged something to scan"
    assert_agent_discloses_nothing(sentinels, agent_surfaces(judgement, captured))


def test_TC_DL_26_an_unknown_acl_answer_discloses_nothing(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    tree = _tree_with_extended_attribute(tmp_path, name="m2-07b-unknown-acl-root-sentinel")
    sentinels = (
        "m2-07b-unknown-acl-root-sentinel",
        os.path.realpath(tree.model),
        str(tree.allowed_root),
    )

    with traversable_ancestors(tree.allowed_root), caplog.at_level(logging.DEBUG):
        judgement = judge_runtime_readability(
            str(tree.model), policy=tree.policy, identity=RUNTIME_IDENTITY
        )
        captured = caplog.text

    assert judgement.unknown_reason is RuntimeReadabilityUnknownReason.ACL_PRESENT
    assert captured, "the unknown-ACL branch must actually have logged something to scan"
    assert_agent_discloses_nothing(sentinels, agent_surfaces(judgement, captured))


def _leaking_module(
    tmp_path: Path,
    *,
    label: str,
    anchor: str,
    leaked: str,
) -> Any:
    """Make one branch log the path in production, leaving the others intact."""

    indented = anchor.rsplit("\n", maxsplit=1)[-1]
    prefix = indented[: len(indented) - len(indented.lstrip())]
    replacement = anchor.replace(
        indented,
        f'{prefix}_LOGGER.warning("m2-07b leak %s", {leaked})\n{indented}',
    )
    return load_mutated_module(
        AGENT_JUDGEMENT_SOURCE,
        [(anchor, replacement)],
        module_name=f"_leaky_path_validation_{label}",
        destination=tmp_path / f"leaky_{label}.py",
    )


def test_TC_DL_26_positive_control_a_leak_on_the_readable_branch_is_caught(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    tree = _readable_tree(tmp_path, name="m2-07b-readable-control-sentinel")
    module = _leaking_module(
        tmp_path,
        label="readable",
        anchor="    return _readable()",
        leaked="resolved",
    )

    with traversable_ancestors(tree.allowed_root), caplog.at_level(logging.DEBUG):
        judgement = module.judge_runtime_readability(
            str(tree.model),
            policy=tree.policy,
            identity=module.RuntimeIdentity(uid=RUNTIME_UID, gid=RUNTIME_GID),
        )
        captured = caplog.text

    assert judgement.conclusion.value == RuntimeReadabilityConclusion.READABLE.value
    with pytest.raises(AssertionError):
        assert_agent_discloses_nothing(("m2-07b-readable-control-sentinel",), (captured,))


def test_TC_DL_26_positive_control_a_leak_on_the_out_of_bounds_branch_is_caught(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    tree = _readable_tree(tmp_path, name="m2-07b-bounds-control-root")
    outside = tmp_path / "m2-07b-bounds-control-sentinel"
    outside.mkdir()
    module = _leaking_module(
        tmp_path,
        label="bounds",
        anchor=(
            "    if not verdict.accepted:\n"
            "        # UID-independent refusals: outside the roots, absent, not a directory,\n"
            "        # or unreadable by the Agent itself.\n"
            "        return _not_readable()"
        ),
        leaked="untrusted_path",
    )

    with caplog.at_level(logging.DEBUG):
        judgement = module.judge_runtime_readability(
            str(outside),
            policy=tree.policy,
            identity=module.RuntimeIdentity(uid=RUNTIME_UID, gid=RUNTIME_GID),
        )
        captured = caplog.text

    assert judgement.conclusion.value == RuntimeReadabilityConclusion.NOT_READABLE.value
    with pytest.raises(AssertionError):
        assert_agent_discloses_nothing(("m2-07b-bounds-control-sentinel",), (captured,))


def test_TC_DL_26_positive_control_a_leak_on_the_not_readable_branch_is_caught(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    tree = _readable_tree(tmp_path, name="m2-07b-mode-control-sentinel")
    _assign(tree.model, uid=STRANGER_UID, gid=STRANGER_GID, mode=0o700)
    module = _leaking_module(
        tmp_path,
        label="mode",
        anchor=(
            "        # The mode bits withhold permission from the Runtime identity."
            "  This is\n"
            "        # the side of the judgement that can be trusted.\n"
            "        return _not_readable()"
        ),
        leaked="resolved",
    )

    with traversable_ancestors(tree.allowed_root), caplog.at_level(logging.DEBUG):
        judgement = module.judge_runtime_readability(
            str(tree.model),
            policy=tree.policy,
            identity=module.RuntimeIdentity(uid=RUNTIME_UID, gid=RUNTIME_GID),
        )
        captured = caplog.text

    assert judgement.conclusion.value == RuntimeReadabilityConclusion.NOT_READABLE.value
    with pytest.raises(AssertionError):
        assert_agent_discloses_nothing(("m2-07b-mode-control-sentinel",), (captured,))


def test_TC_DL_26_positive_control_a_leak_on_the_unknown_identity_branch_is_caught(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    tree = _readable_tree(tmp_path, name="m2-07b-identity-control-sentinel")
    module = _leaking_module(
        tmp_path,
        label="identity",
        anchor="    if identity is None:\n        return _unknown_runtime_identity()",
        leaked="untrusted_path",
    )

    with traversable_ancestors(tree.allowed_root), caplog.at_level(logging.DEBUG):
        judgement = module.judge_runtime_readability(
            str(tree.model), policy=tree.policy, identity=None
        )
        captured = caplog.text

    assert judgement.conclusion.value == RuntimeReadabilityConclusion.UNKNOWN.value
    with pytest.raises(AssertionError):
        assert_agent_discloses_nothing(("m2-07b-identity-control-sentinel",), (captured,))


def test_TC_DL_26_positive_control_a_leak_on_the_unknown_acl_branch_is_caught(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    tree = _tree_with_extended_attribute(tmp_path, name="m2-07b-acl-control-sentinel")
    module = _leaking_module(
        tmp_path,
        label="acl",
        anchor=(
            "    if _chain_has_extended_attributes(resolved):\n"
            "        return _unknown_acl_present()"
        ),
        leaked="resolved",
    )

    with traversable_ancestors(tree.allowed_root), caplog.at_level(logging.DEBUG):
        judgement = module.judge_runtime_readability(
            str(tree.model),
            policy=tree.policy,
            identity=module.RuntimeIdentity(uid=RUNTIME_UID, gid=RUNTIME_GID),
        )
        captured = caplog.text

    assert judgement.conclusion.value == RuntimeReadabilityConclusion.UNKNOWN.value
    with pytest.raises(AssertionError):
        assert_agent_discloses_nothing(("m2-07b-acl-control-sentinel",), (captured,))


SCANNED_BRANCH_CASES: Final[frozenset[str]] = frozenset(
    {
        "test_TC_DL_26_a_readable_answer_discloses_nothing",
        "test_TC_DL_26_a_not_readable_answer_discloses_nothing",
        "test_TC_DL_26_an_unknown_runtime_identity_answer_discloses_nothing",
        "test_TC_DL_26_an_unknown_acl_answer_discloses_nothing",
        "test_TC_DL_26_positive_control_a_leak_on_the_readable_branch_is_caught",
        "test_TC_DL_26_positive_control_a_leak_on_the_out_of_bounds_branch_is_caught",
        "test_TC_DL_26_positive_control_a_leak_on_the_not_readable_branch_is_caught",
        "test_TC_DL_26_positive_control_a_leak_on_the_unknown_identity_branch_is_caught",
        "test_TC_DL_26_positive_control_a_leak_on_the_unknown_acl_branch_is_caught",
        "test_TC_DL_26_a_path_with_a_null_byte_is_refused_not_raised_or_logged",
        "test_TC_DL_26_positive_control_a_null_byte_refusal_log_leak_is_caught",
        "test_TC_DL_26_an_unconfigured_agent_refuses_every_path_without_logging_it",
        "test_TC_DL_26_positive_control_an_unconfigured_policy_log_leak_is_caught",
    }
)


def test_TC_DL_26_every_answering_branch_stays_inside_the_leak_scan() -> None:
    """Four conclusions, four scans, one control each.

    A scan proven on one branch says nothing about another: they are different
    code and log different lines.  M2-07A shipped exactly that gap, so this
    case reads *this file* and requires ``assert_agent_discloses_nothing`` to be
    reached from every branch and every control.  Set equality, because a
    superset would let a deleted scan pass unnoticed.
    """

    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"), filename=__file__)
    scanning: set[str] = set()
    for function in ast.walk(tree):
        if not isinstance(function, ast.FunctionDef):
            continue
        for node in ast.walk(function):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "assert_agent_discloses_nothing"
            ):
                scanning.add(function.name)

    assert scanning == SCANNED_BRANCH_CASES


# --------------------------------------------- no writes, no reads
def test_TC_DL_26_the_judgement_writes_nothing_into_the_external_root(tmp_path: Path) -> None:
    """Every branch is exercised against one tree, and the tree is unchanged."""

    tree = _tree_with_extended_attribute(tmp_path)
    (tree.model / "config.json").write_text("{}", encoding="utf-8")
    probes = (
        str(tree.model),
        str(tree.allowed_root),
        str(tree.allowed_root / "absent-model"),
        str(tree.model / "config.json"),
        str(tmp_path / "outside"),
    )

    with traversable_ancestors(tree.allowed_root):
        before = directory_tree_snapshot(tree.allowed_root)
        for probe in probes:
            judge_runtime_readability(probe, policy=tree.policy, identity=RUNTIME_IDENTITY)
            judge_runtime_readability(probe, policy=tree.policy, identity=None)
        after = directory_tree_snapshot(tree.allowed_root)

    assert_no_external_root_writes(before, after)


def test_TC_DL_26_positive_control_a_probe_written_during_the_judgement_is_caught(
    tmp_path: Path,
) -> None:
    tree = _readable_tree(tmp_path)
    module = load_mutated_module(
        AGENT_JUDGEMENT_SOURCE,
        [
            (
                "    chain = _path_chain(resolved)",
                '    Path(f"{resolved}/m2-07b-probe").mkdir()\n    chain = _path_chain(resolved)',
            )
        ],
        module_name="_writing_path_validation",
        destination=tmp_path / "writing_path_validation.py",
    )

    with traversable_ancestors(tree.allowed_root):
        before = directory_tree_snapshot(tree.allowed_root)
        module.judge_runtime_readability(
            str(tree.model),
            policy=tree.policy,
            identity=module.RuntimeIdentity(uid=RUNTIME_UID, gid=RUNTIME_GID),
        )
        after = directory_tree_snapshot(tree.allowed_root)

    with pytest.raises(AssertionError):
        assert_no_external_root_writes(before, after)


JUDGEMENT_SOURCES: Final[tuple[Path, ...]] = (AGENT_JUDGEMENT_SOURCE, SHARED_RULE_SOURCE)
"""Every file that runs while one path is judged.

The Agent's answer is produced by ``path_validation`` *and* by the shared rule
it must call.  Scanning only the first would leave the second free to
open an entry the judgement is never allowed to read, which is exactly how a
guard ends up narrower than the property it claims to hold.
"""

CONTENT_READING_CALLS: Final[frozenset[str]] = frozenset(
    {"open", "read_bytes", "read_text", "listdir", "scandir", "walk"}
)

EXEMPT_CONTENT_READS: Final[frozenset[tuple[str, str, str]]] = frozenset(
    {(str(AGENT_JUDGEMENT_SOURCE), "load_runtime_identity", "read_text")}
)
"""The one content read the judgement surface may perform.

It is keyed to its **call site**, not to the method name: ``read_text`` is
allowed where ``load_runtime_identity`` reads the Release Manifest, which is
platform data written by the release, and nowhere else.  Exempting the name
globally would let the same call read a file inside an External Root without
the guard noticing.
"""


def _calls_with_enclosing_function(node: ast.AST, enclosing: str) -> Iterator[tuple[ast.AST, str]]:
    """Every node paired with the innermost function that contains it."""

    yield node, enclosing
    for child in ast.iter_child_nodes(node):
        inner = (
            child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else enclosing
        )
        yield from _calls_with_enclosing_function(child, inner)


def _content_reading_call_sites(
    sources: Mapping[Path, str],
) -> frozenset[tuple[str, str, str]]:
    found: set[tuple[str, str, str]] = set()
    for source, source_text in sources.items():
        tree = ast.parse(source_text, filename=str(source))
        for node, enclosing in _calls_with_enclosing_function(tree, "<module>"):
            if not isinstance(node, ast.Call):
                continue
            called: str | None = None
            if isinstance(node.func, ast.Name) and node.func.id in CONTENT_READING_CALLS:
                called = node.func.id
            elif isinstance(node.func, ast.Attribute) and node.func.attr in CONTENT_READING_CALLS:
                called = node.func.attr
            if called is not None:
                found.add((str(source), enclosing, called))
    return frozenset(found)


def assert_the_judgement_reads_no_entry_content(sources: Mapping[Path, str]) -> None:
    """Only ``lstat``, ``realpath`` and ``listxattr``; nothing opens an entry."""

    assert sources, "the content-read guard discovered zero judgement sources"
    assert frozenset(sources) == frozenset(JUDGEMENT_SOURCES), (
        "the content-read guard must scan the whole judgement surface: "
        f"{sorted(map(str, frozenset(JUDGEMENT_SOURCES) - frozenset(sources)))}"
    )
    call_sites = _content_reading_call_sites(sources)
    assert call_sites == EXEMPT_CONTENT_READS, (
        "the judgement surface reads content outside the Release Manifest: "
        f"{sorted(call_sites - EXEMPT_CONTENT_READS)}"
    )


def test_TC_DL_26_the_judgement_never_reads_file_content() -> None:
    """The scan covers both files that decide an answer, and exempts one call site."""

    assert_the_judgement_reads_no_entry_content(
        {source: source.read_text(encoding="utf-8") for source in JUDGEMENT_SOURCES}
    )


def test_TC_DL_26_positive_control_reading_an_external_entry_is_caught() -> None:
    """Read a file inside the External Root during the judgement; the guard must fail.

    The read uses ``read_text``, the one method the old guard exempted by name.
    """

    sources = {
        AGENT_JUDGEMENT_SOURCE: mutate_source(
            AGENT_JUDGEMENT_SOURCE,
            [
                (
                    "    if _chain_has_extended_attributes(resolved):",
                    '    probe = Path(resolved) / "config.json"\n'
                    "    if probe.is_file():\n"
                    '        _ = probe.read_text(encoding="utf-8")\n'
                    "    if _chain_has_extended_attributes(resolved):",
                )
            ],
        ),
        SHARED_RULE_SOURCE: SHARED_RULE_SOURCE.read_text(encoding="utf-8"),
    }

    with pytest.raises(AssertionError, match="outside the Release Manifest"):
        assert_the_judgement_reads_no_entry_content(sources)


def test_TC_DL_26_positive_control_a_content_read_in_the_shared_rule_is_caught() -> None:
    """The widened scan is what catches this one: it is not in ``path_validation``."""

    sources = {
        AGENT_JUDGEMENT_SOURCE: AGENT_JUDGEMENT_SOURCE.read_text(encoding="utf-8"),
        SHARED_RULE_SOURCE: mutate_source(
            SHARED_RULE_SOURCE,
            [
                (
                    '    _LOGGER.info("external path judgement: accepted")',
                    '    _ = (resolved / "config.json").read_bytes()\n'
                    '    _LOGGER.info("external path judgement: accepted")',
                )
            ],
        ),
    }

    with pytest.raises(AssertionError, match="outside the Release Manifest"):
        assert_the_judgement_reads_no_entry_content(sources)


# ------------------------------------------------------ the real UDS surface
def _uds_channel(socket_path: Path) -> grpc.aio.Channel:
    return grpc.aio.insecure_channel(
        f"unix:{socket_path}",
        options=(("grpc.use_local_subchannel_pool", 1),),
    )


def test_TC_DL_26_validate_path_over_the_real_socket_answers_and_leaks_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The method is implemented, reachable by ``download-worker``, and silent about paths."""

    tree = _readable_tree(tmp_path, name="m2-07b-uds-root-sentinel")

    async def scenario() -> None:
        monkeypatch.setattr(server_module, "_read_peer_uid", lambda _socket: 21_001)
        socket_path = tmp_path / "agent.sock"
        server = HostAgentServer(
            socket_path,
            identity_catalog=IdentityCatalog.from_pairs(((21_001, "download-worker"),)),
            runtime_identity=RUNTIME_IDENTITY,
            external_model_roots=(tree.allowed_root,),
        )
        await server.start()
        try:
            async with _uds_channel(socket_path) as channel:
                await asyncio.wait_for(channel.channel_ready(), timeout=2)
                call = channel.unary_unary(
                    "/holm.agent.v1.HostAgent/ValidatePath",
                    request_serializer=agent_pb2.ValidatePathRequest.SerializeToString,
                    response_deserializer=agent_pb2.ValidatePathResponse.FromString,
                )
                accepted = await call(
                    agent_pb2.ValidatePathRequest(path=str(tree.model)), timeout=5
                )
                refused = await call(
                    agent_pb2.ValidatePathRequest(path=str(tmp_path / "outside")), timeout=5
                )
        finally:
            await server.stop(grace=0)

        assert accepted.runtime_readability.state == agent_pb2.KNOWLEDGE_STATE_KNOWN
        assert accepted.runtime_readability.value == agent_pb2.RUNTIME_READABILITY_READABLE
        assert refused.runtime_readability.value == agent_pb2.RUNTIME_READABILITY_NOT_READABLE
        for response in (accepted, refused):
            assert "m2-07b-uds-root-sentinel" not in str(response)
            assert "m2-07b-uds-root-sentinel" not in response.SerializeToString().decode("latin-1")

    with traversable_ancestors(tree.allowed_root), caplog.at_level(logging.DEBUG):
        asyncio.run(scenario())

    assert "m2-07b-uds-root-sentinel" not in caplog.text


# --------------------- through the real production assembly (R6)
def _world_readable_tree(tmp_path: Path, *, name: str) -> ExternalTree:
    """A model directory any UID derives as readable, owned by a stranger.

    The shape matters for the control below: if the assembly ever substituted
    this process's own UID for the Runtime's, the derivation would not merely
    answer differently, it would answer ``READABLE`` -- the one answer that must never be invented.
    """

    allowed_root = tmp_path / name
    model = allowed_root / "synthetic-model"
    model.mkdir(parents=True)
    os.chmod(allowed_root, 0o755)
    _assign(model, uid=STRANGER_UID, gid=STRANGER_GID, mode=0o755)
    return ExternalTree(allowed_root=allowed_root, model=model)


async def _validate_path_through_the_assembly(
    module: Any,
    *,
    socket_path: Path,
    manifest_path: Path,
    tree: ExternalTree,
    monkeypatch: pytest.MonkeyPatch,
) -> agent_pb2.ValidatePathResponse:
    """Ask one path over the real socket, with the Runtime identity *not* injected.

    Every other socket case here passes ``runtime_identity=RUNTIME_IDENTITY``,
    which is convenient and skips the only line that reads the Release
    Manifest.  Omitting it is the point: the Manifest branch is production
    assembly, and until this case existed it was held up by a comment alone.
    An allowed root is configured explicitly, because the default set is empty
    and an unconfigured Agent refuses every path before the identity is ever
    consulted.
    """

    monkeypatch.setattr(module, "_read_peer_uid", lambda _socket: 21_001)
    server = module.HostAgentServer(
        socket_path,
        release_manifest_path=manifest_path,
        identity_catalog=IdentityCatalog.from_pairs(((21_001, "download-worker"),)),
        external_model_roots=(tree.allowed_root,),
    )
    await server.start()
    try:
        async with _uds_channel(socket_path) as channel:
            await asyncio.wait_for(channel.channel_ready(), timeout=2)
            call = channel.unary_unary(
                "/holm.agent.v1.HostAgent/ValidatePath",
                request_serializer=agent_pb2.ValidatePathRequest.SerializeToString,
                response_deserializer=agent_pb2.ValidatePathResponse.FromString,
            )
            return await call(agent_pb2.ValidatePathRequest(path=str(tree.model)), timeout=5)
    finally:
        await server.stop(grace=0)


def assert_the_assembly_reports_the_identity_unavailable(
    response: agent_pb2.ValidatePathResponse,
) -> None:
    """A Manifest naming no Runtime yields unknown on the wire, not a derived answer."""

    observed = response.runtime_readability
    assert observed.state == agent_pb2.KNOWLEDGE_STATE_UNKNOWN, (
        "the assembled Agent answered a Manifest without a Runtime identity with "
        f"state {observed.state!r}"
    )
    assert (
        observed.unknown_reason
        == agent_pb2.RUNTIME_READABILITY_UNKNOWN_REASON_RUNTIME_IDENTITY_UNAVAILABLE
    )
    assert not observed.HasField("value"), "an unknown carried a readability value"


def test_TC_DL_26_the_assembled_agent_never_substitutes_its_own_uid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Manifest branch, end to end, with nothing injected in its place."""

    manifest = _write_manifest(
        tmp_path / "release-manifest.json",
        [{"service": "gateway", "uid": 21_001, "gid": 21_001}],
    )
    tree = _world_readable_tree(tmp_path, name="m2-07b-assembly-root")

    with traversable_ancestors(tree.allowed_root):
        response = asyncio.run(
            _validate_path_through_the_assembly(
                server_module,
                socket_path=tmp_path / "agent.sock",
                manifest_path=manifest,
                tree=tree,
                monkeypatch=monkeypatch,
            )
        )

    assert_the_assembly_reports_the_identity_unavailable(response)


def test_TC_DL_26_positive_control_an_assembly_falling_back_to_this_uid_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Give the assembly the fallback its comment forbids; the case must fail loudly."""

    manifest = _write_manifest(
        tmp_path / "release-manifest.json",
        [{"service": "gateway", "uid": 21_001, "gid": 21_001}],
    )
    tree = _world_readable_tree(tmp_path, name="m2-07b-assembly-control-root")
    module = load_mutated_module(
        HOST_AGENT_SERVER_SOURCE,
        [
            (
                "                else load_runtime_identity(self.release_manifest_path)\n"
                "            )",
                "                else load_runtime_identity(self.release_manifest_path)\n"
                "                or RuntimeIdentity("
                "uid=max(os.getuid(), 1), gid=max(os.getgid(), 1))\n"
                "            )",
            )
        ],
        module_name="_manifest_fallback_server",
        destination=tmp_path / "manifest_fallback_server.py",
    )

    with traversable_ancestors(tree.allowed_root):
        response = asyncio.run(
            _validate_path_through_the_assembly(
                module,
                socket_path=tmp_path / "agent.sock",
                manifest_path=manifest,
                tree=tree,
                monkeypatch=monkeypatch,
            )
        )

    with pytest.raises(AssertionError):
        assert_the_assembly_reports_the_identity_unavailable(response)
