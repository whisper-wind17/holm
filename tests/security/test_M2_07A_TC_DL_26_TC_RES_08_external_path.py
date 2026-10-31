"""TC-DL-26/TC-RES-08: what the External Path judgement refuses, and what it never leaks.

Every filesystem sample in ``FX-MS-06`` is **materialized** here -- an escaping
symbolic link, a FIFO, a socket, a character device and a setuid file are
created on a real filesystem inside ``tmp_path`` and fed to the production
Adapter.  Before M2-07A those five were descriptions only, so no test had ever
watched production code refuse one that exists.

A sample the container cannot create (a character device needs ``CAP_MKNOD``)
is reported as ``BLOCKED_ENV`` through ``pytest.xfail`` so it appears as
suspended, never as a pass and never as a skip (the integration-test policy).

Every positive control breaks the **production** module and reloads it, then
runs the same judgement function against the mutated copy.  Feeding a
hand-built structure to the checking function would only prove the checking
function still runs (``LESSONS.md``: M1-09B, M1-01G, M2-01B).
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import logging
import os
import stat
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path, PurePosixPath
from types import ModuleType
from typing import Any, Final

import pytest
from fixtures.fake_model_source import (
    MaterializationOutcome,
    MaterializedPathAttack,
    PathAttackKind,
    described_entry_kind_matches,
    materialize_path_attack,
    path_attack_samples,
)
from fixtures.fake_model_source.models import PathAttackSample
from fixtures.source_mutation import load_mutated_module
from holm_control_api import external_path_resolution
from holm_control_api.external_path_resolution import (
    EXTERNAL_PATH_UNUSABLE_RESULT,
    ExternalPathPolicy,
    ExternalPathRejectionRule,
    ExternalPathResolutionAdapter,
    ExternalPathVerdict,
    judge_external_path,
    path_is_within,
)
from holm_control_api.model_resolution import (
    ExternalPathAccepted,
    ExternalPathRequest,
    ModelResolutionRegistry,
    ResolutionFailed,
    RuntimeReadability,
    RuntimeReadabilityUnverifiedCause,
)
from holm_control_api.model_resolution_routes import ResolveRequest, render_preview
from holm_control_api.model_resolution_service import (
    ModelResolutionService,
    ResolutionPreview,
    ResolveInputRejected,
)
from holm_domain import (
    ModelOriginType,
    ModelSourceValidationAction,
    ModelSourceValidationReason,
    ModelSourceValidationStatus,
)
from holm_security import MasterKey, MasterKeyring, MasterKeyState

REPOSITORY_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
# M2-07B moved the rule itself into ``packages/`` so the Host Agent judges with
# the same code rather than a second "equivalent" copy.  The controls below
# mutate the rule where it now lives; pointing them at the re-exporting module
# would mutate nothing and pass for the ordinary reason.
JUDGEMENT_SOURCE: Final[Path] = (
    REPOSITORY_ROOT / "packages/contracts/src/holm_contracts/external_paths.py"
)
ROUTES_SOURCE: Final[Path] = (
    REPOSITORY_ROOT / "apps/control_api/src/holm_control_api/model_resolution_routes.py"
)
KEYRING: Final = MasterKeyring((MasterKey("tc-dl-26a", MasterKeyState.WRITABLE, b"P" * 32),))

# FX-MS-06's two halves.  The Repository ID sample is refused by the identity
# rule TC-RES-08 already covers; the other seven describe filesystem entries
# and are covered here.
REPOSITORY_ID_SAMPLE_KINDS: Final[frozenset[PathAttackKind]] = frozenset(
    {PathAttackKind.INVALID_REPOSITORY_ID}
)
FILESYSTEM_SAMPLE_KINDS: Final[frozenset[PathAttackKind]] = (
    frozenset(PathAttackKind) - REPOSITORY_ID_SAMPLE_KINDS
)


def _exploding_session_factory() -> object:
    def factory(*arguments: object, **keywords: object) -> object:
        raise AssertionError("a rejected path must never reach the database")

    return factory


def _service(registry: ModelResolutionRegistry) -> ModelResolutionService:
    return ModelResolutionService(
        session_factory=_exploding_session_factory(),  # type: ignore[arg-type]
        keyring=KEYRING,
        registry=registry,
    )


def _registry_for(roots: Sequence[Path]) -> ModelResolutionRegistry:
    return ModelResolutionRegistry(
        external_path_adapters={
            ModelOriginType.LOCAL_PATH: ExternalPathResolutionAdapter(
                policy=ExternalPathPolicy.for_configured_roots(roots)
            )
        }
    )


def _sample(kind: PathAttackKind) -> PathAttackSample:
    matches = tuple(item for item in path_attack_samples() if item.kind is kind)
    assert len(matches) == 1
    return matches[0]


# --------------------------------------------------------------- judgements
def assert_external_path_is_refused(outcome: object) -> None:
    """The single judgement shared by every refusal case and its controls."""

    assert isinstance(outcome, ResolutionFailed), "an unusable path produced a preview"
    result = outcome.result
    assert result.status is ModelSourceValidationStatus.NOT_FOUND
    assert result.reason_code is ModelSourceValidationReason.SOURCE_NOT_FOUND
    assert result.next_action is ModelSourceValidationAction.CHECK_SOURCE_REFERENCE
    assert result == EXTERNAL_PATH_UNUSABLE_RESULT, "a refusal took a distinguishable shape"


def assert_segment_level_ancestor_judgement(
    module: ModuleType,
    *,
    allowed_root: Path,
    sibling_entry: Path,
    inside_entry: Path,
) -> None:
    """``/srv/models-evil/x`` is not inside ``/srv/models``; ``/srv/models/x`` is."""

    policy = module.ExternalPathPolicy.for_configured_roots((allowed_root,))
    inside = module.judge_external_path(str(inside_entry), policy)
    assert inside.accepted, "a directory inside the allowed root was refused"

    sibling = module.judge_external_path(str(sibling_entry), policy)
    assert not sibling.accepted, "a sibling root sharing a string prefix was accepted"
    assert sibling.rule is not None
    assert sibling.rule.value == ExternalPathRejectionRule.OUTSIDE_ALLOWED_ROOTS.value


ACCEPTED_RESPONSE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "outcome",
        "control_plane_readable",
        "runtime_readability",
        "runtime_readability_cause",
    }
)


def assert_runtime_readability_is_explicitly_unverified(payload: dict[str, Any]) -> None:
    """The Runtime half is stated as unverified and never rendered as readable.

    The key set is asserted as well as the values, so a second field that
    *does* assert readability cannot be added beside the honest one.
    """

    assert frozenset(payload) == ACCEPTED_RESPONSE_KEYS
    assert payload["outcome"] == "external_path_accepted"
    assert payload["control_plane_readable"] is True, (
        "the half the control plane did verify must be stated as verified"
    )
    assert payload["runtime_readability"] == RuntimeReadability.UNVERIFIED.value, (
        f"the Runtime half was rendered as {payload['runtime_readability']!r}"
    )
    assert (
        payload["runtime_readability_cause"]
        == RuntimeReadabilityUnverifiedCause.RUNTIME_IDENTITY_CHECK_UNAVAILABLE.value
    )


def directory_tree_snapshot(root: Path) -> tuple[str, ...]:
    """Every entry under ``root`` with the metadata a write would disturb.

    ``st_ctime_ns`` moves on ``chmod``/``chown``/rename, ``st_mtime_ns`` on a
    content or directory change, and regular files carry a content hash, so a
    probe that is created and deleted again still shows up through its parent
    directory's timestamps.
    """

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


def assert_nothing_discloses(sentinels: Iterable[str], surfaces: Iterable[str]) -> None:
    """No surface may quote the input, its ``realpath`` or an allowed root."""

    for sentinel in sentinels:
        for surface in surfaces:
            assert sentinel not in surface, f"{sentinel!r} escaped into an external surface"


# ------------------------------------------------------------------ TC-DL-26
def test_TC_DL_26_fx_ms_06_partition_is_exhaustive() -> None:
    kinds = {sample.kind for sample in path_attack_samples()}
    assert kinds == set(PathAttackKind)
    assert kinds == REPOSITORY_ID_SAMPLE_KINDS | FILESYSTEM_SAMPLE_KINDS
    assert not (REPOSITORY_ID_SAMPLE_KINDS & FILESYSTEM_SAMPLE_KINDS)


@pytest.mark.parametrize("kind", sorted(FILESYSTEM_SAMPLE_KINDS))
def test_TC_DL_26_materialized_fx_ms_06_entries_are_refused(
    kind: PathAttackKind,
    tmp_path: Path,
) -> None:
    """Create the described entry for real, then watch production refuse it."""

    materialized: MaterializedPathAttack = materialize_path_attack(_sample(kind), tmp_path)
    if materialized.outcome is MaterializationOutcome.BLOCKED_ENV:
        assert materialized.blocked_reason is not None
        pytest.xfail(
            f"BLOCKED_ENV: FX-MS-06 {kind.value} cannot be created here "
            f"({materialized.blocked_reason.value})"
        )
    assert materialized.outcome is MaterializationOutcome.MATERIALIZED
    assert materialized.allowed_root is not None
    assert materialized.attack_path is not None
    assert described_entry_kind_matches(materialized), "the entry is not the described kind"

    service = _service(_registry_for((materialized.allowed_root,)))

    async def scenario() -> None:
        preview = await service.resolve(
            source=ModelOriginType.LOCAL_PATH,
            repository_id=None,
            external_path=str(materialized.attack_path),
            revision=None,
        )
        assert_external_path_is_refused(preview.outcome)
        assert preview.local_availability is None

    asyncio.run(scenario())


def test_TC_DL_26_a_real_model_directory_inside_the_root_is_accepted(tmp_path: Path) -> None:
    """The refusals above mean something only because acceptance is reachable."""

    root = tmp_path / "models"
    model = root / "synthetic-model"
    model.mkdir(parents=True)
    service = _service(_registry_for((root,)))

    async def scenario() -> None:
        preview = await service.resolve(
            source=ModelOriginType.LOCAL_PATH,
            repository_id=None,
            external_path=str(model),
            revision=None,
        )
        assert isinstance(preview.outcome, ExternalPathAccepted)
        assert preview.local_availability is None

    asyncio.run(scenario())


def test_TC_DL_26_a_symbolic_link_that_stays_inside_the_root_is_accepted(tmp_path: Path) -> None:
    """``realpath`` decides, so an inside link is not refused for being a link."""

    root = tmp_path / "models"
    (root / "real-model").mkdir(parents=True)
    link = root / "aliased-model"
    link.symlink_to("real-model")
    policy = ExternalPathPolicy.for_configured_roots((root,))

    assert judge_external_path(str(link), policy).accepted


def _sibling_root_tree(tmp_path: Path) -> tuple[Path, Path, Path]:
    allowed_root = tmp_path / "models"
    inside_entry = allowed_root / "inside"
    inside_entry.mkdir(parents=True)
    sibling_entry = tmp_path / "models-evil" / "x"
    sibling_entry.mkdir(parents=True)
    return allowed_root, inside_entry, sibling_entry


def test_TC_DL_26_ancestor_judgement_is_by_path_segment(tmp_path: Path) -> None:
    allowed_root, inside_entry, sibling_entry = _sibling_root_tree(tmp_path)

    assert_segment_level_ancestor_judgement(
        external_path_resolution,
        allowed_root=allowed_root,
        sibling_entry=sibling_entry,
        inside_entry=inside_entry,
    )
    # The helper the production judgement calls, on its own terms.
    assert path_is_within(PurePosixPath("/srv/models/x"), PurePosixPath("/srv/models"))
    assert path_is_within(PurePosixPath("/srv/models"), PurePosixPath("/srv/models"))
    assert not path_is_within(PurePosixPath("/srv/models-evil/x"), PurePosixPath("/srv/models"))


@pytest.mark.parametrize(
    "label,replacement",
    [
        ("startswith", "    return str(candidate).startswith(str(root))"),
        ("substring", "    return str(root) in str(candidate)"),
    ],
)
def test_TC_DL_26_positive_control_prefix_ancestor_test_fails_the_guard(
    label: str,
    replacement: str,
    tmp_path: Path,
) -> None:
    """Turn the segment test into a string test in production; the guard must fail."""

    allowed_root, inside_entry, sibling_entry = _sibling_root_tree(tmp_path)
    module = load_mutated_module(
        JUDGEMENT_SOURCE,
        [("    return candidate == root or root in candidate.parents", replacement)],
        module_name=f"_prefix_external_path_{label}",
        destination=tmp_path / "prefix_judgement.py",
    )

    with pytest.raises(AssertionError):
        assert_segment_level_ancestor_judgement(
            module,
            allowed_root=allowed_root,
            sibling_entry=sibling_entry,
            inside_entry=inside_entry,
        )


def test_TC_DL_26_the_allowed_root_itself_is_realpathed(tmp_path: Path) -> None:
    """A root reached through a symbolic link resolves to the real directory."""

    real_root = tmp_path / "real-models"
    (real_root / "synthetic-model").mkdir(parents=True)
    linked_root = tmp_path / "linked-models"
    linked_root.symlink_to(real_root, target_is_directory=True)

    policy = ExternalPathPolicy.for_configured_roots((linked_root,))
    assert policy.allowed_roots == (PurePosixPath(os.path.realpath(real_root)),)
    # A path spelled through the link and the same path spelled directly reach
    # the same conclusion, because both sides were resolved.
    assert judge_external_path(str(real_root / "synthetic-model"), policy).accepted
    assert judge_external_path(str(linked_root / "synthetic-model"), policy).accepted


def test_TC_DL_26_a_relative_or_empty_allowed_root_set_is_refused() -> None:
    with pytest.raises(ValueError):
        ExternalPathPolicy.for_configured_roots((Path("models"),))
    with pytest.raises(ValueError):
        # An empty Allowlist would refuse every path, which reads as "the
        # judgement works" while meaning "nothing was configured".
        ExternalPathPolicy(allowed_roots=())


def test_TC_DL_26_no_request_can_choose_an_allowed_root() -> None:
    """The Allowlist is configuration; a request has no field that reaches it."""

    assert set(ResolveRequest.model_fields) == {
        "source",
        "repository_id",
        "external_path",
        "revision",
    }


# ------------------------------------------------------------------ TC-RES-08
RELATIVE_AND_MALFORMED_PATHS: Final[tuple[str, ...]] = (
    "weights/../../synthetic-outside",
    "relative/model",
    "./model",
    "../model",
    "srv/models/model",
)


def test_TC_RES_08_relative_and_malformed_paths_are_refused_before_any_lookup(
    tmp_path: Path,
) -> None:
    """A path that cannot name a server directory never reaches the filesystem."""

    root = tmp_path / "models"
    root.mkdir()
    service = _service(_registry_for((root,)))
    hostile = (
        *RELATIVE_AND_MALFORMED_PATHS,
        _sample(PathAttackKind.PARENT_TRAVERSAL).untrusted_value,
        "/srv/models/\x00truncated",
        "/srv/models/forged\nline",
    )

    async def scenario() -> None:
        for untrusted in hostile:
            with pytest.raises(ResolveInputRejected) as failure:
                await service.resolve(
                    source=ModelOriginType.LOCAL_PATH,
                    repository_id=None,
                    external_path=untrusted,
                    revision=None,
                )
            assert untrusted not in str(failure.value)
            assert untrusted not in repr(failure.value)

    asyncio.run(scenario())


def test_TC_RES_08_every_refusal_collapses_onto_one_indistinguishable_conclusion(
    tmp_path: Path,
) -> None:
    """A caller cannot tell "absent" from "outside the roots" and so cannot map the server."""

    root = tmp_path / "models"
    root.mkdir()
    (tmp_path / "elsewhere").mkdir()
    policy = ExternalPathPolicy.for_configured_roots((root,))
    adapter = ExternalPathResolutionAdapter(policy=policy)
    probes = (
        str(root / "absent-model"),
        str(tmp_path / "elsewhere"),
        str(tmp_path / "elsewhere" / "also-absent"),
        str(root),
    )

    async def scenario() -> list[object]:
        return [
            await adapter.evaluate(
                ExternalPathRequest(source=ModelOriginType.LOCAL_PATH, untrusted_path=probe)
            )
            for probe in probes
        ]

    outcomes = asyncio.run(scenario())
    # The last probe is the root itself, which is a real readable directory.
    assert isinstance(outcomes[-1], ExternalPathAccepted)
    for outcome in outcomes[:-1]:
        assert_external_path_is_refused(outcome)
    assert len({repr(outcome) for outcome in outcomes[:-1]}) == 1


def test_TC_RES_08_a_refusal_reveals_neither_the_path_nor_the_allowed_roots(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    root = tmp_path / "tc-res-08-allowed-root-sentinel"
    root.mkdir()
    untrusted = str(tmp_path / "tc-res-08-outside-sentinel" / "tc-res-08-leaf-sentinel")
    sentinels = (
        "tc-res-08-allowed-root-sentinel",
        "tc-res-08-outside-sentinel",
        "tc-res-08-leaf-sentinel",
        os.path.realpath(untrusted),
        str(root),
    )
    policy = ExternalPathPolicy.for_configured_roots((root,))
    adapter = ExternalPathResolutionAdapter(policy=policy)
    request = ExternalPathRequest(
        source=ModelOriginType.LOCAL_PATH,
        untrusted_path=untrusted,
    )

    with caplog.at_level(logging.DEBUG):
        outcome = asyncio.run(adapter.evaluate(request))

    assert_external_path_is_refused(outcome)
    payload = render_preview(
        ResolutionPreview(outcome=outcome, local_availability=None)
    ).model_dump(mode="json")
    assert_nothing_discloses(
        sentinels,
        (
            json.dumps(payload),
            caplog.text,
            repr(outcome),
            repr(request),
            str(request),
            repr(judge_external_path(untrusted, policy)),
        ),
    )


ACCEPTED_LOG_LINE: Final[str] = '    _LOGGER.info("external path judgement: accepted")'


def test_TC_RES_08_an_accepted_path_reveals_neither_the_path_nor_the_allowed_roots(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The *accepted* branch is scanned too, with the refusal case's sink set.

    A refusal and an acceptance take different code paths and log different
    lines, so proving the scanner works on one says nothing about the other.
    M1-10A shipped exactly this gap in the opposite direction -- the sentinel
    scan ran only on the success path -- so both branches are scanned here.
    """

    root = tmp_path / "tc-res-08-accepted-root-sentinel"
    model = root / "tc-res-08-accepted-model-sentinel"
    model.mkdir(parents=True)
    accepted_path = str(model)
    sentinels = (
        "tc-res-08-accepted-root-sentinel",
        "tc-res-08-accepted-model-sentinel",
        os.path.realpath(accepted_path),
        str(root),
    )
    policy = ExternalPathPolicy.for_configured_roots((root,))
    adapter = ExternalPathResolutionAdapter(policy=policy)
    request = ExternalPathRequest(
        source=ModelOriginType.LOCAL_PATH,
        untrusted_path=accepted_path,
    )

    with caplog.at_level(logging.DEBUG):
        outcome = asyncio.run(adapter.evaluate(request))
        verdict = judge_external_path(accepted_path, policy)
        captured = caplog.text

    assert isinstance(outcome, ExternalPathAccepted), "the sentinel path must be accepted"
    assert verdict.accepted
    assert captured, "the accepted branch must actually have logged something to scan"
    payload = render_preview(
        ResolutionPreview(outcome=outcome, local_availability=None)
    ).model_dump(mode="json")
    assert_runtime_readability_is_explicitly_unverified(payload)
    assert_nothing_discloses(
        sentinels,
        (
            json.dumps(payload),
            captured,
            repr(outcome),
            repr(request),
            str(request),
            repr(verdict),
        ),
    )


@pytest.mark.parametrize(
    "label,leaked",
    [
        ("realpath", "resolved"),
        ("allowed_roots", "policy.allowed_roots"),
    ],
)
def test_TC_RES_08_positive_control_logging_the_accepted_path_is_caught(
    label: str,
    leaked: str,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Make production log the path on the *accepted* branch; the scanner must catch it."""

    root = tmp_path / "tc-res-08-accept-control-root-sentinel"
    model = root / "tc-res-08-accept-control-model-sentinel"
    model.mkdir(parents=True)
    module = load_mutated_module(
        JUDGEMENT_SOURCE,
        [
            (
                ACCEPTED_LOG_LINE,
                '    _LOGGER.info("external path judgement: accepted %s", ' + leaked + ")",
            )
        ],
        module_name=f"_leaky_accepted_external_path_{label}",
        destination=tmp_path / "leaky_accepted_judgement.py",
    )
    policy = module.ExternalPathPolicy.for_configured_roots((root,))

    with caplog.at_level(logging.DEBUG):
        verdict = module.judge_external_path(str(model), policy)
        captured = caplog.text

    assert verdict.accepted, "the control must run through the accepted branch"
    with pytest.raises(AssertionError):
        assert_nothing_discloses(
            (
                "tc-res-08-accept-control-root-sentinel",
                "tc-res-08-accept-control-model-sentinel",
            ),
            (captured,),
        )


SCANNED_BRANCH_CASES: Final[frozenset[str]] = frozenset(
    {
        "test_TC_RES_08_a_refusal_reveals_neither_the_path_nor_the_allowed_roots",
        "test_TC_RES_08_positive_control_logging_the_resolved_path_is_caught",
        "test_TC_RES_08_an_accepted_path_reveals_neither_the_path_nor_the_allowed_roots",
        "test_TC_RES_08_positive_control_logging_the_accepted_path_is_caught",
    }
)


def test_TC_RES_08_both_judgement_branches_stay_inside_the_leak_scan() -> None:
    """Refusal and acceptance must not drift apart.

    The two branches run different code and log different lines, so a scan
    proven on one says nothing about the other -- M1-10A shipped exactly that
    gap.  This case reads *this file* and requires ``assert_nothing_discloses``
    to be reached from both branches and from both of their controls, so
    deleting one branch's scan is a failure rather than a quiet narrowing.
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
                and node.func.id == "assert_nothing_discloses"
            ):
                scanning.add(function.name)

    assert scanning == SCANNED_BRANCH_CASES


def test_TC_RES_08_positive_control_logging_the_resolved_path_is_caught(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Make production log the ``realpath`` and the roots; the scanner must catch it."""

    root = tmp_path / "tc-res-08-control-root-sentinel"
    root.mkdir()
    untrusted = str(tmp_path / "tc-res-08-control-outside-sentinel")
    module = load_mutated_module(
        JUDGEMENT_SOURCE,
        [
            (
                "    if not any(path_is_within(resolved, root) for root in policy.allowed_roots):\n"
                "        return _refused(ExternalPathRejectionRule.OUTSIDE_ALLOWED_ROOTS)",
                "    if not any(path_is_within(resolved, root) for root in policy.allowed_roots):\n"
                '        _LOGGER.warning("outside %s (roots %s)", resolved, policy.allowed_roots)\n'
                "        return _refused(ExternalPathRejectionRule.OUTSIDE_ALLOWED_ROOTS)",
            )
        ],
        module_name="_leaky_external_path_resolution",
        destination=tmp_path / "leaky_judgement.py",
    )
    policy = module.ExternalPathPolicy.for_configured_roots((root,))

    with caplog.at_level(logging.DEBUG):
        verdict = module.judge_external_path(untrusted, policy)

    assert not verdict.accepted
    with pytest.raises(AssertionError):
        assert_nothing_discloses(
            ("tc-res-08-control-root-sentinel", "tc-res-08-control-outside-sentinel"),
            (caplog.text,),
        )


# ------------------------------------------------- TC-RES-02 / TC-INV-05 half
def test_TC_RES_02_TC_INV_05_the_judgement_writes_nothing_into_the_allowed_root(
    tmp_path: Path,
) -> None:
    root, probes = _populated_root(tmp_path)
    policy = ExternalPathPolicy.for_configured_roots((root,))
    before = directory_tree_snapshot(root)

    for probe in probes:
        judge_external_path(probe, policy)

    assert_no_external_root_writes(before, directory_tree_snapshot(root))


def _populated_root(tmp_path: Path) -> tuple[Path, tuple[str, ...]]:
    root = tmp_path / "models"
    model = root / "synthetic-model"
    model.mkdir(parents=True)
    (model / "config.json").write_text("{}", encoding="utf-8")
    (root / "weights").mkdir()
    probes = (
        str(model),
        str(root),
        str(root / "absent-model"),
        str(model / "config.json"),
        str(tmp_path / "outside"),
    )
    return root, probes


@pytest.mark.parametrize(
    "label,probe_statement",
    [
        ("mkdir", '    Path(resolved / "m2-07a-probe").mkdir()'),
        ("create", '    open(str(resolved / "m2-07a-probe"), "w", encoding="utf-8").close()'),
        ("touch", "    os.utime(resolved)"),
    ],
)
def test_TC_RES_02_positive_control_a_probe_written_during_the_judgement_is_caught(
    label: str,
    probe_statement: str,
    tmp_path: Path,
) -> None:
    """Let production write one entry on the judgement path; the snapshot must catch it."""

    root, _ = _populated_root(tmp_path)
    module = load_mutated_module(
        JUDGEMENT_SOURCE,
        [
            (
                "    if not os.access(resolved, os.R_OK | os.X_OK):",
                f"{probe_statement}\n    if not os.access(resolved, os.R_OK | os.X_OK):",
            )
        ],
        module_name=f"_writing_external_path_{label}",
        destination=tmp_path / "writing_judgement.py",
    )
    policy = module.ExternalPathPolicy.for_configured_roots((root,))
    before = directory_tree_snapshot(root)

    module.judge_external_path(str(root / "synthetic-model"), policy)

    with pytest.raises(AssertionError):
        assert_no_external_root_writes(before, directory_tree_snapshot(root))


# ------------------------------------------------------- unverified
def _accepted_payload(tmp_path: Path, renderer: Callable[..., Any]) -> dict[str, Any]:
    root = tmp_path / "models"
    model = root / "synthetic-model"
    model.mkdir(parents=True)
    adapter = ExternalPathResolutionAdapter(policy=ExternalPathPolicy.for_configured_roots((root,)))
    outcome = asyncio.run(
        adapter.evaluate(
            ExternalPathRequest(
                source=ModelOriginType.LOCAL_PATH,
                untrusted_path=str(model),
            )
        )
    )
    assert isinstance(outcome, ExternalPathAccepted)
    rendered = renderer(ResolutionPreview(outcome=outcome, local_availability=None))
    payload: dict[str, Any] = rendered.model_dump(mode="json")
    return payload


def test_TC_DL_26_runtime_readability_is_an_explicit_unverified_state(tmp_path: Path) -> None:
    payload = _accepted_payload(tmp_path, render_preview)

    assert_runtime_readability_is_explicitly_unverified(payload)
    # The state is present, not omitted: a client cannot mistake silence for a pass.
    assert "runtime_readability" in payload
    assert set(RuntimeReadability) == {RuntimeReadability.UNVERIFIED}


def test_TC_DL_26_positive_control_rendering_the_runtime_half_as_readable_fails(
    tmp_path: Path,
) -> None:
    """Render the unverified state as "readable" in production; the guard must fail."""

    module = load_mutated_module(
        ROUTES_SOURCE,
        [
            (
                "    runtime_readability: RuntimeReadability\n"
                "    runtime_readability_cause: RuntimeReadabilityUnverifiedCause",
                "    runtime_readability: str\n    runtime_readability_cause: str",
            ),
            (
                "        return ExternalPathAcceptedResponse(\n"
                "            runtime_readability=preview.outcome.runtime_readability,\n"
                "            runtime_readability_cause=preview.outcome.runtime_readability_cause,\n"
                "        )",
                "        return ExternalPathAcceptedResponse(\n"
                '            runtime_readability="readable",\n'
                '            runtime_readability_cause="runtime_uid_verified",\n'
                "        )",
            ),
        ],
        module_name="_readable_model_resolution_routes",
        destination=tmp_path / "readable_routes.py",
    )
    payload = _accepted_payload(tmp_path, module.render_preview)

    with pytest.raises(AssertionError):
        assert_runtime_readability_is_explicitly_unverified(payload)


def test_TC_DL_26_a_verdict_has_no_field_a_path_could_be_written_into() -> None:
    """The refusal shape is a rule, so "the log leaked the path" needs a code change."""

    verdict = ExternalPathVerdict(accepted=False, rule=ExternalPathRejectionRule.NOT_FOUND)
    assert set(vars(type(verdict))["__slots__"]) == {"accepted", "rule"}
    with pytest.raises(ValueError):
        ExternalPathVerdict(accepted=True, rule=ExternalPathRejectionRule.NOT_FOUND)
    with pytest.raises(ValueError):
        ExternalPathVerdict(accepted=False, rule=None)
