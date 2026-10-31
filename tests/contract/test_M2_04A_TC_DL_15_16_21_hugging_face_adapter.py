"""TC-DL-15/16/21: the Hugging Face Adapter's dry run, filter rules and Revision.

Every case below drives the **real** ``huggingface_hub`` SDK.  Nothing inside
the SDK is patched: a local fake Hub replays the ``FX-MS-08`` recordings and
``HF_ENDPOINT`` -- an address, not a credential -- points the SDK at it, because
the development container cannot reach ``huggingface.co`` at all
(``tests/fixtures/hugging_face/recorded/PROVENANCE.md``).  Patching
``HfApi``/``snapshot_download`` would test the patch instead of the Adapter, and
the integration-test policy forbids a Mock as milestone evidence.

The round trip against the *real* Hub is the "certified version Contract Test"
TST 8.2 lists as **suspended**, not passing; M2-14 owns it.
"""

from __future__ import annotations

import ast
import asyncio
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Final

import pytest
from fixtures.hugging_face import (
    PUBLIC_REPOSITORY,
    PUBLIC_REPOSITORY_COMMIT,
    PUBLIC_REPOSITORY_ID,
    PUBLIC_REPOSITORY_LARGE_FILE,
    TINY_REPOSITORY_COMMIT,
    TINY_REPOSITORY_ID,
    CredentialPresence,
    FakeHub,
    assert_recorded_head_agrees_with_siblings,
    claimed_recordings,
    fake_hugging_face_hub,
    recording_names,
)
from fixtures.source_mutation import mutate_source
from holm_control_api.hugging_face_resolution import (
    BLOCKING_WORKER_LIMIT,
    CONFIG_FILENAME,
    HUGGING_FACE_ALLOW_PATTERNS,
    HUGGING_FACE_IGNORE_PATTERNS,
    SNAPSHOT_MAX_WORKERS,
    DryRunWroteToDisk,
    HuggingFaceResolutionAdapter,
    assert_no_disk_residue,
    sdk_patterns,
)
from holm_control_api.model_resolution import (
    DEFAULT_RESOLUTION_TIMEOUT_SECONDS,
    ResolutionRequest,
    ResolutionSucceeded,
)
from holm_control_api.model_resolution_routes import ResolveRequest
from holm_domain import Identified, ModelOriginType, ResolvedModel, Unrecognized
from holm_persistence import DEFAULT_REQUESTED_REVISION
from huggingface_hub import constants, snapshot_download
from pydantic import ValidationError

ADAPTER_SOURCE: Final[Path] = Path(
    "apps/control_api/src/holm_control_api/hugging_face_resolution.py"
).resolve()
if not ADAPTER_SOURCE.is_file():  # pragma: no cover - path is repository-relative
    ADAPTER_SOURCE = (
        Path(__file__).resolve().parents[2]
        / "apps/control_api/src/holm_control_api/hugging_face_resolution.py"
    )

RECORDED_PUBLIC_TOTAL_BYTES: Final[int] = 15_242_807_270
RECORDED_PUBLIC_FILE_COUNT: Final[int] = 14
RECORDED_LARGE_FILE_BYTES: Final[int] = 3_945_441_440

# The four functions that turn one recorded source field into a knowledge state.
METADATA_MAPPERS: Final[tuple[str, ...]] = (
    "_architecture",
    "_precision",
    "_license",
    "_context_length",
)

# Field names that would let a caller steer the download filter or the address.
FORBIDDEN_REQUEST_FIELDS: Final[tuple[str, ...]] = (
    "allow_patterns",
    "ignore_patterns",
    "glob",
    "patterns",
    "endpoint",
    "base_url",
    "mirror",
    "proxy",
)


@pytest.fixture
def hub() -> Iterator[FakeHub]:
    with fake_hugging_face_hub() as running:
        yield running


def _adapter(hub: FakeHub, scratch_root: Path) -> HuggingFaceResolutionAdapter:
    return HuggingFaceResolutionAdapter(endpoint=hub.endpoint, scratch_root=scratch_root)


def _resolve(
    adapter: HuggingFaceResolutionAdapter,
    repository_id: str,
    requested_revision_norm: str = DEFAULT_REQUESTED_REVISION,
) -> ResolvedModel:
    outcome = asyncio.run(
        adapter.resolve(
            ResolutionRequest(
                source=ModelOriginType.HUGGING_FACE,
                repository_id=repository_id,
                requested_revision_norm=requested_revision_norm,
            ),
            credential=None,
        )
    )
    assert isinstance(outcome, ResolutionSucceeded)
    return outcome.resolved


def _directory_snapshot(directory: Path) -> tuple[tuple[str, bytes], ...]:
    """Every file under ``directory`` with its bytes; absent reads as empty."""

    if not directory.exists():
        return ()
    return tuple(
        (str(path.relative_to(directory)), path.read_bytes())
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    )


# --------------------------------------------------------------------- helpers
def _snapshot_download_call(source: str) -> ast.Call:
    tree = ast.parse(source)
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "snapshot_download"
    ]
    assert len(calls) == 1, "the Adapter must have exactly one snapshot_download call site"
    return calls[0]


def _keyword(call: ast.Call, name: str) -> ast.expr:
    for keyword in call.keywords:
        if keyword.arg == name:
            return keyword.value
    raise AssertionError(f"snapshot_download is called without an explicit {name}")


def assert_dry_run_is_requested(source: str) -> None:
    """The call site must ask for a dry run with a literal, not a variable."""

    value = _keyword(_snapshot_download_call(source), "dry_run")
    assert isinstance(value, ast.Constant) and value.value is True, (
        "snapshot_download must be called with dry_run=True"
    )


def assert_the_residue_guard_is_called_after_the_dry_run(source: str) -> None:
    """``_file_plan`` must *call* ``assert_no_disk_residue``, inside the scratch block.

    The guard function has its own positive control -- a real download makes it
    raise -- but that says nothing about whether production ever reaches it.
    Replacing the call with ``pass`` left every case in this suite green, because
    the dry run legitimately writes nothing either way, so the deletion changes
    no observable result.  Searching the module text for the name would not
    catch it either: the name also appears in the module docstring and in
    ``__all__``, and either would make such a check pass on its own.  So this
    walks the function definition.

    The call must also sit *inside* the ``TemporaryDirectory`` block and be
    handed what that block bound.  Once the block exits the directory is gone,
    and ``rglob`` over a directory that no longer exists reports no residue --
    the same silent pass reached by another route.
    """

    definitions = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.FunctionDef) and node.name == "_file_plan"
    ]
    assert len(definitions) == 1, "the Adapter must define _file_plan exactly once"
    scratch_blocks = [
        node
        for node in ast.walk(definitions[0])
        if isinstance(node, ast.With)
        and any(
            isinstance(item.context_expr, ast.Call)
            and isinstance(item.context_expr.func, ast.Name)
            and item.context_expr.func.id == "TemporaryDirectory"
            for item in node.items
        )
    ]
    assert len(scratch_blocks) == 1, "_file_plan must open exactly one scratch directory"
    block = scratch_blocks[0]
    bound = {
        item.optional_vars.id for item in block.items if isinstance(item.optional_vars, ast.Name)
    }
    calls = [
        node
        for statement in block.body
        for node in ast.walk(statement)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "assert_no_disk_residue"
    ]
    assert len(calls) == 1, (
        "_file_plan must call assert_no_disk_residue exactly once, inside the scratch block"
    )
    referenced = {node.id for node in ast.walk(calls[0]) if isinstance(node, ast.Name)}
    assert bound and bound <= referenced, (
        "assert_no_disk_residue must be given the scratch directory the dry run used"
    )


def assert_unknown_is_never_filled_with_a_default(source: str) -> None:
    """No metadata field may be reported as a literal stand-in value.

    the project invariants and PDD line 710 forbid ``or "unknown"``,
    ``or 0`` and ``or ""``: an unidentified field is an explicit state.  The
    recordings can only reach part of that.  ``_license`` has two unidentified
    branches -- "no ``cardData`` at all" and "a ``cardData`` carrying no
    ``license``" -- and ``FX-MS-08`` holds no repository of the second kind, so
    an ``Identified("unknown")`` planted *there* is invisible to every
    end-to-end assertion.  Recording such a repository is out of this suite's
    reach (the container cannot leave the network) and inventing one is
    forbidden, so that branch is judged where it is written instead.

    Two mechanical clauses:

    * ``Identified`` is never handed a literal anywhere in the Adapter -- an
      identified value can only be something the source actually said;
    * every metadata mapper returns nothing but ``Identified``/``Unrecognized``,
      ends on the ``Unrecognized`` fallback, and never reaches a value through
      an ``or``.
    """

    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "Identified"
        ):
            continue
        for argument in [*node.args, *(keyword.value for keyword in node.keywords)]:
            assert not isinstance(argument, ast.Constant), (
                "Identified() must carry what the source confirmed, never a literal default"
            )

    definitions = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name in METADATA_MAPPERS
    }
    assert set(definitions) == set(METADATA_MAPPERS), (
        f"the Adapter must define every metadata mapper: {METADATA_MAPPERS}"
    )
    for name, definition in definitions.items():
        for statement in ast.walk(definition):
            if not isinstance(statement, ast.Return):
                continue
            value = statement.value
            assert (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Name)
                and value.func.id in {"Identified", "Unrecognized"}
            ), f"{name} must return one of the two knowledge states and nothing else"
            assert not any(isinstance(node, ast.BoolOp) for node in ast.walk(statement)), (
                f'{name} must not reach a reported value through an "or" fallback'
            )
        last = definition.body[-1]
        assert (
            isinstance(last, ast.Return)
            and isinstance(last.value, ast.Call)
            and isinstance(last.value.func, ast.Name)
            and last.value.func.id == "Unrecognized"
        ), f"{name} must fall back to Unrecognized(), not to a default value"


def assert_filter_rules_are_the_empty_constants(source: str) -> None:
    """Judge the call site and the constants, not the resulting file plan.

    A result comparison cannot tell "no rule was applied" from "a rule was
    applied and happened to match everything", and the M2-03 acceptance run
    showed a downstream comparison staying green through a real behavioural
    change.  So this reads the AST: each filter argument must be
    ``sdk_patterns(<the module constant>)``, and each constant must be an empty
    tuple literal.
    """

    tree = ast.parse(source)
    call = _snapshot_download_call(source)
    expected = {
        "allow_patterns": "HUGGING_FACE_ALLOW_PATTERNS",
        "ignore_patterns": "HUGGING_FACE_IGNORE_PATTERNS",
    }
    for argument, constant_name in expected.items():
        value = _keyword(call, argument)
        assert isinstance(value, ast.Call), f"{argument} must go through sdk_patterns()"
        assert isinstance(value.func, ast.Name) and value.func.id == "sdk_patterns", (
            f"{argument} must go through sdk_patterns()"
        )
        assert len(value.args) == 1 and not value.keywords
        argument_node = value.args[0]
        assert isinstance(argument_node, ast.Name) and argument_node.id == constant_name, (
            f"{argument} must come from {constant_name} and from nothing else"
        )

    assigned: dict[str, ast.expr] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.value is not None:
                assigned[node.target.id] = node.value
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in expected.values():
                    assigned[target.id] = node.value
    for constant_name in expected.values():
        value = assigned[constant_name]
        assert isinstance(value, ast.Tuple) and not value.elts, (
            f"{constant_name} stays empty until Runtime filters are integrated"
        )


# ------------------------------------------------------------------ FX-MS-08
def test_FX_MS_08_every_recording_is_claimed_by_the_fake_hub() -> None:
    """No recording may go unused, and none may be invented.

    ``PROVENANCE.md`` says these responses *are* the real source: the container
    cannot fetch them again.  If the fake Hub stops replaying one, or if someone
    drops a new file in without wiring it up, this fails instead of the evidence
    quietly rotting.
    """

    on_disk = frozenset(recording_names())
    assert len(on_disk) == 14
    assert claimed_recordings() == on_disk


def test_FX_MS_08_the_two_public_metadata_recordings_are_kept_apart(hub: FakeHub) -> None:
    """Branch and Commit reads must come from their own recordings."""

    by_branch = PUBLIC_REPOSITORY.metadata(CredentialPresence.ABSENT, "main")
    by_commit = PUBLIC_REPOSITORY.metadata(CredentialPresence.ABSENT, PUBLIC_REPOSITORY_COMMIT)
    assert by_branch.name == "public-repo-main"
    assert by_commit.name == "public-repo-by-sha"
    assert by_branch.json_body()["sha"] == by_commit.json_body()["sha"]
    del hub


# ------------------------------------------------------------------ TC-DL-15
def test_TC_DL_15_dry_run_returns_a_file_plan_and_writes_nothing(
    hub: FakeHub,
    tmp_path: Path,
) -> None:
    scratch_root = tmp_path / "scratch"
    scratch_root.mkdir()
    official_cache = Path(constants.HF_HUB_CACHE)
    cache_before = _directory_snapshot(official_cache)

    resolved = _resolve(_adapter(hub, scratch_root), PUBLIC_REPOSITORY_ID)

    assert isinstance(resolved.files, Identified)
    plan = resolved.files.value
    assert len(plan) == RECORDED_PUBLIC_FILE_COUNT
    recorded_sizes = {entry.path: entry.size_bytes for entry in PUBLIC_REPOSITORY.files()}
    assert {item.path: item.size_bytes for item in plan} == recorded_sizes
    assert resolved.total_size_bytes == Identified(RECORDED_PUBLIC_TOTAL_BYTES)
    assert sum(item.size_bytes for item in plan) == RECORDED_PUBLIC_TOTAL_BYTES

    # The derived per-file sizes are the Hub's own numbers: the one file that is
    # covered by both a recorded HEAD and a recorded siblings entry agrees.
    assert assert_recorded_head_agrees_with_siblings() == RECORDED_LARGE_FILE_BYTES
    assert recorded_sizes[PUBLIC_REPOSITORY_LARGE_FILE] == RECORDED_LARGE_FILE_BYTES

    # Neither the directory the Adapter pointed the SDK at nor the official Hub
    # cache changed: no new file, no ``.incomplete`` partial, no lock.
    assert _directory_snapshot(scratch_root) == ()
    assert list(scratch_root.iterdir()) == []
    assert _directory_snapshot(official_cache) == cache_before


def test_TC_DL_15_positive_control_a_real_download_is_detected_as_residue(
    tmp_path: Path,
) -> None:
    """The production judgement must fail when a byte really is written."""

    target = tmp_path / "written"
    target.mkdir()
    assert_no_disk_residue(target)  # empty: the judgement passes for the right reason

    with fake_hugging_face_hub(serve_synthetic_file_bodies=True) as writing_hub:
        # A real download of the small fixture repository.  The bytes are filler
        # supplied by the fixture purely so that something gets written; nothing
        # is asserted about their content.
        snapshot_download(
            TINY_REPOSITORY_ID,
            revision=TINY_REPOSITORY_COMMIT,
            endpoint=writing_hub.endpoint,
            cache_dir=str(target),
            dry_run=False,
            token=False,
            allow_patterns=sdk_patterns(HUGGING_FACE_ALLOW_PATTERNS),
            ignore_patterns=sdk_patterns(HUGGING_FACE_IGNORE_PATTERNS),
            max_workers=2,
            etag_timeout=10,
        )
    assert _directory_snapshot(target) != ()
    with pytest.raises(DryRunWroteToDisk):
        assert_no_disk_residue(target)


def test_TC_DL_15_positive_control_dropping_the_dry_run_flag_fails_the_guard() -> None:
    assert_dry_run_is_requested(ADAPTER_SOURCE.read_text(encoding="utf-8"))
    mutated = mutate_source(ADAPTER_SOURCE, [("dry_run=True,", "dry_run=False,")])
    with pytest.raises(AssertionError):
        assert_dry_run_is_requested(mutated)


def test_TC_DL_15_the_production_path_really_calls_the_residue_guard() -> None:
    """Judge the call site, because no result comparison can see this one.

    Deleting the call changes nothing observable under these fixtures -- the dry
    run writes no byte either way -- so every case in this suite stayed green
    with the guard gone.  M2-03 already demonstrated the shape: an identity
    ``.strip()`` added to production passed every end-to-end assertion and was
    caught only by an AST call-site scan.
    """

    residue_call = "            assert_no_disk_residue(Path(scratch))\n"
    source = ADAPTER_SOURCE.read_text(encoding="utf-8")
    assert_the_residue_guard_is_called_after_the_dry_run(source)

    # Positive control: the exact mutation the acceptance run found silent.
    # The mutation is applied outside ``pytest.raises`` so that an anchor that
    # stopped matching fails the test instead of satisfying it.
    deleted = mutate_source(ADAPTER_SOURCE, [(residue_call, "            pass\n")])
    with pytest.raises(AssertionError):
        assert_the_residue_guard_is_called_after_the_dry_run(deleted)

    # Positive control: the call survives, but moves past the block that owns
    # the directory -- where a removed directory reports no residue at all.
    moved = mutate_source(
        ADAPTER_SOURCE,
        [
            (
                residue_call + "        return tuple(",
                "            pass\n"
                "        assert_no_disk_residue(Path(scratch))\n"
                "        return tuple(",
            )
        ],
    )
    with pytest.raises(AssertionError):
        assert_the_residue_guard_is_called_after_the_dry_run(moved)


def test_TC_DL_15_identified_and_unrecognized_both_come_from_recorded_repositories(
    hub: FakeHub,
    tmp_path: Path,
) -> None:
    """Both halves of the metadata mapping, from two recorded repositories.

    ``Qwen/Qwen2.5-7B-Instruct`` happens to describe every mapped field, so a
    suite that resolves only it never reaches the "the source did not say"
    branch: turning ``_architecture``'s or ``_license``'s fallback into
    ``Identified("unknown")`` -- precisely what the project invariants and
    PDD line 710 forbid -- left the whole card green.

    ``hf-internal-testing/tiny-random-gpt2`` is the counter-example and it is
    already recorded (``FX-MS-08``): its ``config`` carries no ``architectures``
    and it has no ``cardData`` at all, while its ``safetensors.parameters``
    names exactly one dtype.  Asserting both repositories field by field means
    neither "identify everything" nor "report everything as unrecognized" can
    pass.
    """

    scratch_root = tmp_path / "scratch"
    scratch_root.mkdir()
    adapter = _adapter(hub, scratch_root)

    described = _resolve(adapter, PUBLIC_REPOSITORY_ID)
    assert described.architecture == Identified("Qwen2ForCausalLM")
    assert described.license == Identified("apache-2.0")
    assert described.precision == Identified("BF16")
    assert described.context_length == Identified(32768)

    partly_described = _resolve(adapter, TINY_REPOSITORY_ID)
    # No ``architectures`` in the recorded ``config``, and no ``cardData`` key.
    assert partly_described.architecture == Unrecognized()
    assert partly_described.license == Unrecognized()
    # One dtype in ``safetensors.parameters``: the identified half of the very
    # same round trip, so "answer Unrecognized to everything" fails here too.
    assert partly_described.precision == Identified("F32")

    # This repository's ``config.json`` body was never recorded, so the fake Hub
    # refuses it rather than inventing one and the Adapter gets no confirmation.
    # The refused request is asserted, so "unrecognized because nobody even
    # asked" stays distinguishable from "unrecognized because the source did
    # not say".
    config_suffix = f"/{TINY_REPOSITORY_COMMIT}/{CONFIG_FILENAME}"
    assert [
        request
        for request in hub.requests
        if request.method == "GET" and request.path.endswith(config_suffix)
    ]
    assert partly_described.context_length == Unrecognized()

    # The fields with no source at all today -- the Runtime compatibility
    # configuration is empty, and no Hub field states a modality or a
    # quantization -- are unrecognized on both, never ``()``, ``0`` or ``""``.
    for preview in (described, partly_described):
        assert preview.modalities == Unrecognized()
        assert preview.quantization == Unrecognized()
        assert preview.candidate_runtimes == Unrecognized()
        # ``Unrecognized`` carries no ``value`` attribute at all: there is
        # nowhere for a default to hide, even by accident.
        assert not hasattr(preview.modalities, "value")


def test_TC_DL_15_no_metadata_field_is_ever_filled_with_a_default_value() -> None:
    """The mapper branches no recording reaches are judged where they are written.

    The two-repository case above covers every branch ``FX-MS-08`` can reach.
    It cannot reach ``_license``'s "``cardData`` without a ``license``" branch,
    because no recorded repository is shaped that way, and an
    ``Identified("unknown")`` planted there passed every case in this suite.
    Each control below is that kind of filling, written a different way.
    """

    assert_unknown_is_never_filled_with_a_default(ADAPTER_SOURCE.read_text(encoding="utf-8"))

    license_tail = (
        '    value = card_data.get("license")\n'
        "    if isinstance(value, str) and value:\n"
        "        return Identified(value)\n"
    )
    # The branch no recording reaches: silent until this guard existed.
    filled = mutate_source(
        ADAPTER_SOURCE,
        [
            (
                license_tail + "    return Unrecognized()\n",
                license_tail + '    return Identified("unknown")\n',
            )
        ],
    )
    with pytest.raises(AssertionError):
        assert_unknown_is_never_filled_with_a_default(filled)

    # The same filling written as the ``or`` the Invariants name literally.
    filled_with_or = mutate_source(
        ADAPTER_SOURCE,
        [(license_tail, license_tail.replace("Identified(value)", 'Identified(value or "")'))],
    )
    with pytest.raises(AssertionError):
        assert_unknown_is_never_filled_with_a_default(filled_with_or)

    # And a mapper that stops reporting the unidentified state at all.
    without_fallback = mutate_source(
        ADAPTER_SOURCE,
        [
            (
                "        return Identified(dtypes[0])\n    return Unrecognized()\n",
                '        return Identified(dtypes[0])\n    return Identified("unknown")\n',
            )
        ],
    )
    with pytest.raises(AssertionError):
        assert_unknown_is_never_filled_with_a_default(without_fallback)


def test_TC_DL_15_the_blocking_sdk_runs_off_the_event_loop_in_a_bounded_pool(
    hub: FakeHub,
    tmp_path: Path,
) -> None:
    """``huggingface_hub`` is synchronous; the ASGI loop must stay responsive."""

    scratch_root = tmp_path / "scratch"
    scratch_root.mkdir()
    adapter = _adapter(hub, scratch_root)
    ticks = 0

    async def heartbeat() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.001)
            ticks += 1

    async def scenario() -> None:
        beat = asyncio.create_task(heartbeat())
        try:
            outcome = await adapter.resolve(
                ResolutionRequest(
                    source=ModelOriginType.HUGGING_FACE,
                    repository_id=PUBLIC_REPOSITORY_ID,
                    requested_revision_norm=DEFAULT_REQUESTED_REVISION,
                ),
                credential=None,
            )
            assert isinstance(outcome, ResolutionSucceeded)
        finally:
            beat.cancel()

    asyncio.run(scenario())
    # A blocking call on the loop would have starved the heartbeat completely.
    assert ticks > 0

    # Bounded, and the one timeout M2-03 already froze -- there is no second knob.
    executor = adapter._executor  # noqa: SLF001 - the bound is the property under test
    assert isinstance(executor, ThreadPoolExecutor)
    assert 0 < executor._max_workers <= BLOCKING_WORKER_LIMIT  # noqa: SLF001
    assert 0 < SNAPSHOT_MAX_WORKERS <= BLOCKING_WORKER_LIMIT
    assert adapter._timeout_seconds == DEFAULT_RESOLUTION_TIMEOUT_SECONDS  # noqa: SLF001
    source = ADAPTER_SOURCE.read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg not in {"timeout", "etag_timeout"}:
                continue
            assert isinstance(keyword.value, ast.Attribute)
            assert keyword.value.attr == "_timeout_seconds"


# ------------------------------------------------------------------ TC-DL-16
def test_TC_DL_16_request_schema_has_no_filter_or_address_field() -> None:
    """The absence is a property of the Schema, not a runtime filter."""

    assert "allow_patterns" not in ResolveRequest.model_fields
    assert "ignore_patterns" not in ResolveRequest.model_fields
    for field_name in FORBIDDEN_REQUEST_FIELDS:
        assert field_name not in ResolveRequest.model_fields
        with pytest.raises(ValidationError) as failure:
            ResolveRequest.model_validate(
                {
                    "source": ModelOriginType.HUGGING_FACE.value,
                    "repository_id": PUBLIC_REPOSITORY_ID,
                    field_name: "*.safetensors",
                }
            )
        assert failure.value.errors()[0]["type"] == "extra_forbidden"
    # The Adapter is not given the request object either: the only identity it
    # receives has three fields and none of them is a pattern or an address.
    assert set(ResolutionRequest.__dataclass_fields__) == {
        "source",
        "repository_id",
        "requested_revision_norm",
    }


def test_TC_DL_16_filter_rules_are_one_empty_constant() -> None:
    assert HUGGING_FACE_ALLOW_PATTERNS == ()
    assert HUGGING_FACE_IGNORE_PATTERNS == ()
    # An empty rule set means "no rule"; the SDK reads an empty list as "allow
    # nothing", so the translation is part of the contract.
    assert sdk_patterns(()) is None
    assert sdk_patterns(("*.json",)) == ["*.json"]
    assert_filter_rules_are_the_empty_constants(ADAPTER_SOURCE.read_text(encoding="utf-8"))


def test_TC_DL_16_positive_control_user_input_on_the_filter_fails_the_guard() -> None:
    mutated = mutate_source(
        ADAPTER_SOURCE,
        [
            (
                "allow_patterns=sdk_patterns(HUGGING_FACE_ALLOW_PATTERNS),",
                "allow_patterns=[repository_id],",
            )
        ],
    )
    with pytest.raises(AssertionError):
        assert_filter_rules_are_the_empty_constants(mutated)


def test_TC_DL_16_positive_control_a_hard_coded_rule_fails_the_guard() -> None:
    mutated = mutate_source(
        ADAPTER_SOURCE,
        [
            (
                "HUGGING_FACE_IGNORE_PATTERNS: Final[tuple[str, ...]] = ()",
                'HUGGING_FACE_IGNORE_PATTERNS: Final[tuple[str, ...]] = ("*.ckpt",)',
            )
        ],
    )
    with pytest.raises(AssertionError):
        assert_filter_rules_are_the_empty_constants(mutated)


def test_TC_DL_16_a_non_empty_rule_set_would_really_change_the_plan(
    hub: FakeHub,
    tmp_path: Path,
) -> None:
    """The emptiness is load-bearing, not decorative."""

    everything = snapshot_download(
        PUBLIC_REPOSITORY_ID,
        revision=PUBLIC_REPOSITORY_COMMIT,
        endpoint=hub.endpoint,
        cache_dir=str(tmp_path / "plan-all"),
        dry_run=True,
        token=False,
        allow_patterns=sdk_patterns(HUGGING_FACE_ALLOW_PATTERNS),
        ignore_patterns=sdk_patterns(HUGGING_FACE_IGNORE_PATTERNS),
        max_workers=2,
        etag_timeout=10,
    )
    filtered = snapshot_download(
        PUBLIC_REPOSITORY_ID,
        revision=PUBLIC_REPOSITORY_COMMIT,
        endpoint=hub.endpoint,
        cache_dir=str(tmp_path / "plan-json"),
        dry_run=True,
        token=False,
        allow_patterns=sdk_patterns(("*.json",)),
        ignore_patterns=sdk_patterns(HUGGING_FACE_IGNORE_PATTERNS),
        max_workers=2,
        etag_timeout=10,
    )
    assert len(everything) == RECORDED_PUBLIC_FILE_COUNT
    assert 0 < len(filtered) < len(everything)


# ------------------------------------------------------------------ TC-DL-21
def test_TC_DL_21_branch_resolves_to_an_immutable_commit_and_is_idempotent(
    hub: FakeHub,
    tmp_path: Path,
) -> None:
    scratch_root = tmp_path / "scratch"
    scratch_root.mkdir()
    adapter = _adapter(hub, scratch_root)

    by_branch = _resolve(adapter, PUBLIC_REPOSITORY_ID)
    assert by_branch.requested_revision_norm == DEFAULT_REQUESTED_REVISION
    assert by_branch.resolved_revision == PUBLIC_REPOSITORY_COMMIT
    assert len(by_branch.resolved_revision) == 40
    assert all(character in "0123456789abcdef" for character in by_branch.resolved_revision)

    by_commit = _resolve(adapter, PUBLIC_REPOSITORY_ID, PUBLIC_REPOSITORY_COMMIT)
    assert by_commit.resolved_revision == by_branch.resolved_revision
    assert by_commit.requested_revision_norm == PUBLIC_REPOSITORY_COMMIT
    assert by_commit.files == by_branch.files
    assert by_commit.total_size_bytes == by_branch.total_size_bytes

    metadata_paths = [
        request.path for request in hub.requests if request.path.startswith("/api/models/")
    ]
    # The first read really did ask for the default branch, and the second read
    # asked for the commit: the pinning is a source round trip, not a rename.
    assert f"/api/models/{PUBLIC_REPOSITORY_ID}/revision/main" in metadata_paths
    assert (
        f"/api/models/{PUBLIC_REPOSITORY_ID}/revision/{PUBLIC_REPOSITORY_COMMIT}" in metadata_paths
    )
    # Every file plan is fetched at the commit, never at the branch name.
    for request in hub.requests:
        if "/resolve/" in request.path or "/tree/" in request.path:
            assert PUBLIC_REPOSITORY_COMMIT in request.path


def test_TC_DL_21_a_resolved_revision_can_never_be_the_default_sentinel(
    hub: FakeHub,
    tmp_path: Path,
) -> None:
    scratch_root = tmp_path / "scratch"
    scratch_root.mkdir()
    tiny = _resolve(_adapter(hub, scratch_root), TINY_REPOSITORY_ID)
    assert tiny.resolved_revision == TINY_REPOSITORY_COMMIT
    assert tiny.resolved_revision != DEFAULT_REQUESTED_REVISION
    with pytest.raises(ValueError):
        ResolvedModel(
            source=ModelOriginType.HUGGING_FACE,
            repository_id=PUBLIC_REPOSITORY_ID,
            requested_revision_norm=DEFAULT_REQUESTED_REVISION,
            resolved_revision=DEFAULT_REQUESTED_REVISION,
            revision_immutability=tiny.revision_immutability,
            files=tiny.files,
            total_size_bytes=tiny.total_size_bytes,
            architecture=tiny.architecture,
            modalities=tiny.modalities,
            precision=tiny.precision,
            quantization=tiny.quantization,
            context_length=tiny.context_length,
            candidate_runtimes=tiny.candidate_runtimes,
            license=tiny.license,
        )
