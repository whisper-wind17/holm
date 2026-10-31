"""TC-DL-28/29/30: the ModelScope Adapter's Revision, file listing and default branch.

Every case drives the production Adapter against a local Hub that replays the
``FX-MS-09`` recordings and nothing else (``fixtures.modelscope``).  The
development container's reachability of ``modelscope.cn`` is unverified, and
the certified round trip against the real Hub is TST 8.2's **suspended** item,
which M2-14 owns.

The three branches this Adapter can take -- a preview, a ``Files: null``
failure and a repository that does not exist -- are each driven once here, and
each case asserts the sentinel only *it* can produce, so "the control proves the
detector works" is not mistaken for "the branch was executed"
(``LESSONS.md``: M2-15A/M2-15B/M2-04B).
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Final

import pytest
from fixtures.hugging_face import PUBLIC_REPOSITORY_COMMIT, fake_hugging_face_hub
from fixtures.hugging_face import PUBLIC_REPOSITORY_ID as HF_PUBLIC_REPOSITORY_ID
from fixtures.modelscope import (
    MISSING_REPOSITORY_ID,
    PUBLIC_DEFAULT_BRANCH,
    PUBLIC_REPOSITORY_ID,
    PUBLIC_UNKNOWN_BRANCH,
    PUBLIC_UNKNOWN_SHA,
    FakeHub,
    body_without_request_id,
    claimed_recordings,
    fake_modelscope_hub,
    load_recording,
    recording_names,
)
from fixtures.source_mutation import load_mutated_module
from holm_control_api.hugging_face_resolution import HuggingFaceResolutionAdapter
from holm_control_api.model_resolution import (
    ResolutionFailed,
    ResolutionOutcome,
    ResolutionRequest,
    ResolutionSucceeded,
)
from holm_control_api.model_resolution_routes import render_preview
from holm_control_api.model_resolution_service import ResolutionPreview
from holm_control_api.modelscope_resolution import ModelScopeResolutionAdapter
from holm_control_api.modelscope_source import (
    FileListingAbsent,
    FileListingUnreadable,
    parse_file_listing,
)
from holm_domain import (
    Identified,
    LocalAvailability,
    ModelOriginType,
    ModelSourceValidationAction,
    ModelSourceValidationReason,
    ModelSourceValidationStatus,
    RevisionImmutability,
    Unrecognized,
)
from holm_persistence import DEFAULT_REQUESTED_REVISION

REPOSITORY_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
ADAPTER_SOURCE: Final[Path] = (
    REPOSITORY_ROOT / "apps/control_api/src/holm_control_api/modelscope_resolution.py"
)
SOURCE_READ_SOURCE: Final[Path] = (
    REPOSITORY_ROOT / "apps/control_api/src/holm_control_api/modelscope_source.py"
)

# One sentinel per branch of this Adapter.  Nothing else in the suite asks the
# Hub for these three things, so seeing one is proof that this branch ran.
RESOLVED_SENTINEL: Final[str] = "resolved-preview"
FILES_NULL_SENTINEL: Final[str] = "files-null-failure"
NOT_FOUND_SENTINEL: Final[str] = "repository-not-found-failure"


def recorded_metadata() -> dict[str, Any]:
    data = load_recording("public-repo-metadata").json_body()["Data"]
    assert isinstance(data, dict)
    return data


def recorded_listing(name: str) -> list[dict[str, Any]]:
    """The recorded ``Files`` array, read straight off the recording.

    Deliberately not routed through the Adapter: an expectation derived from the
    code under test moves with it, and "both sides changed together" is not a
    judgement (``LESSONS.md``, M2-15B R1-2).
    """

    entries = load_recording(name).json_body()["Data"]["Files"]
    assert isinstance(entries, list)
    return entries


def observed_sentinels(hub: FakeHub) -> set[str]:
    """Which of this Adapter's three branches actually reached the source."""

    seen: set[str] = set()
    for request in hub.requests:
        if MISSING_REPOSITORY_ID in request.path:
            seen.add(NOT_FOUND_SENTINEL)
            continue
        revision = request.revision()
        if revision == PUBLIC_DEFAULT_BRANCH:
            seen.add(RESOLVED_SENTINEL)
        if revision == PUBLIC_UNKNOWN_BRANCH:
            seen.add(FILES_NULL_SENTINEL)
    return seen


def resolve(
    adapter: Any,
    repository_id: str,
    revision: str = DEFAULT_REQUESTED_REVISION,
    credential: bytes | None = None,
) -> ResolutionOutcome:
    """One preview attempt against whichever Adapter object was handed in.

    ``adapter`` is typed loosely on purpose: the positive controls hand in the
    class from a *mutated copy* of the production module, which is a different
    class object with the same shape.
    """

    return asyncio.run(
        adapter.resolve(
            ResolutionRequest(
                source=ModelOriginType.MODELSCOPE,
                repository_id=repository_id,
                requested_revision_norm=revision,
            ),
            credential=credential,
        )
    )


def assert_the_recorded_branch_reference(outcome: ResolutionOutcome) -> None:
    """The one judgement shared by the real case and its positive controls.

    The expected Revision is the branch name the *recording* states, and the
    marker must say the reference is mutable.  Both halves matter: a Commit-like
    string here would mean the platform minted an identifier it cannot hand back
    to ModelScope, and a missing marker would push the decision onto whoever
    reads ``resolved_revision`` later.
    """

    assert isinstance(outcome, ResolutionSucceeded)
    resolved = outcome.resolved
    branch = recorded_metadata()["Revision"]
    assert resolved.resolved_revision == branch == PUBLIC_DEFAULT_BRANCH
    assert resolved.revision_immutability is RevisionImmutability.MUTABLE_REFERENCE
    # Not one of the per-file last-commit shas, and not any 40-hex string: the
    # per-file shas are the tempting raw material for a synthetic repository
    # level identifier (7 distinct values across 15 files).
    listing = recorded_listing("public-repo-files-master")
    file_level_revisions = {str(entry["Revision"]) for entry in listing}
    assert len(file_level_revisions) == 7
    assert resolved.resolved_revision not in file_level_revisions
    assert not all(character in "0123456789abcdef" for character in resolved.resolved_revision)


def assert_the_revision_does_not_exist(outcome: ResolutionOutcome) -> None:
    """``Files: null`` is a failure -- never a preview with an empty listing."""

    assert isinstance(outcome, ResolutionFailed)
    assert outcome.result.status is ModelSourceValidationStatus.NOT_FOUND
    assert outcome.result.reason_code is ModelSourceValidationReason.SOURCE_NOT_FOUND
    assert outcome.result.next_action is ModelSourceValidationAction.CHECK_SOURCE_REFERENCE


@pytest.fixture
def hub() -> Iterator[FakeHub]:
    with fake_modelscope_hub() as running:
        yield running


@pytest.fixture
def adapter(hub: FakeHub) -> ModelScopeResolutionAdapter:
    return ModelScopeResolutionAdapter(endpoint=hub.endpoint)


def test_FX_MS_09_every_recording_is_replayed_and_none_is_invented() -> None:
    assert claimed_recordings() == set(recording_names())
    assert len(recording_names()) == 16


def test_TC_DL_28_resolved_revision_is_a_branch_name_marked_as_a_mutable_reference(
    hub: FakeHub,
    adapter: ModelScopeResolutionAdapter,
) -> None:
    outcome = resolve(adapter, PUBLIC_REPOSITORY_ID)

    assert_the_recorded_branch_reference(outcome)
    assert observed_sentinels(hub) == {RESOLVED_SENTINEL}

    # The source states no repository-level Commit anywhere: /revisions carries
    # branches and tags only, so there is nothing to pin to even if one wanted.
    revisions = load_recording("public-repo-revisions").json_body()["Data"]["RevisionMap"]
    assert [branch["Revision"] for branch in revisions["Branches"]][0] == PUBLIC_DEFAULT_BRANCH
    assert revisions["Tags"] == []
    assert (
        re.search(
            r"[0-9a-f]{40}",
            load_recording("public-repo-revisions").body.decode("utf-8"),
        )
        is None
    )


def test_TC_DL_28_the_file_listing_carries_every_recorded_sha256(
    adapter: ModelScopeResolutionAdapter,
) -> None:
    outcome = resolve(adapter, PUBLIC_REPOSITORY_ID)
    assert isinstance(outcome, ResolutionSucceeded)
    files = outcome.resolved.files
    assert isinstance(files, Identified)

    recorded = {
        str(entry["Path"]): (int(entry["Size"]), str(entry["Sha256"]))
        for entry in recorded_listing("public-repo-files-master")
    }
    assert {item.path for item in files.value} == set(recorded)
    for item in files.value:
        size, digest = recorded[item.path]
        assert item.size_bytes == size
        # Reproducibility of a mutable reference rests on these, so an
        # unrecognized hash here would be a hole rather than an honest gap.
        assert item.content_sha256 == Identified(digest)
    assert outcome.resolved.total_size_bytes == Identified(
        sum(size for size, _ in recorded.values())
    )


def test_TC_DL_28_a_directory_entry_is_not_a_file_and_nothing_else_is_dropped() -> None:
    """The small repository's listing contains a ``tree`` entry with no measurements.

    Driven through the parsing function rather than the whole Adapter because
    that is how far the recordings reach: the recording session captured this
    repository's file tree and not its metadata, and inventing the missing
    metadata response to complete the round trip is exactly what the fixture
    refuses to do.  The recorded listing itself is replayed unchanged.
    """

    entries = recorded_listing("small-repo-files-master")
    trees = [entry for entry in entries if entry["Type"] == "tree"]
    blobs = [entry for entry in entries if entry["Type"] == "blob"]
    assert len(trees) == 1
    assert trees[0]["Sha256"] == "" and trees[0]["Size"] == 0
    assert trees[0]["Path"] in {str(entry["Path"]).split("/")[0] for entry in blobs}

    files = parse_file_listing(load_recording("small-repo-files-master").json_body()["Data"])
    assert isinstance(files, tuple)
    # The directory is gone and every file under it is still there, with its own
    # measurements: a listing that shrank by more than the one tree entry would
    # be a file plan quietly losing members.
    assert {item.path for item in files} == {str(entry["Path"]) for entry in blobs}
    assert len(files) == len(entries) - 1
    assert all(isinstance(item.content_sha256, Identified) for item in files)


def test_TC_DL_28_positive_control_omitting_the_immutability_marker_cannot_resolve(
    hub: FakeHub,
    tmp_path: Path,
) -> None:
    """Drop the marker in production; the preview must not be constructible."""

    module = load_mutated_module(
        ADAPTER_SOURCE,
        [("            revision_immutability=RevisionImmutability.MUTABLE_REFERENCE,\n", "")],
        module_name="_modelscope_without_immutability_marker",
        destination=tmp_path / "modelscope_without_immutability_marker.py",
    )
    with pytest.raises(TypeError):
        resolve(module.ModelScopeResolutionAdapter(endpoint=hub.endpoint), PUBLIC_REPOSITORY_ID)
    assert observed_sentinels(hub) == {RESOLVED_SENTINEL}


def test_TC_DL_28_positive_control_a_synthesized_repository_revision_fails(
    hub: FakeHub,
    tmp_path: Path,
) -> None:
    """Mint a repository-level identifier out of file-level hashes; the guard must fail."""

    module = load_mutated_module(
        ADAPTER_SOURCE,
        [
            (
                "            resolved_revision=reading.branch,\n",
                "            resolved_revision=max(\n"
                "                item.content_sha256.value\n"
                "                for item in files\n"
                "                if isinstance(item.content_sha256, Identified)\n"
                "            ),\n",
            )
        ],
        module_name="_modelscope_with_synthesized_revision",
        destination=tmp_path / "modelscope_with_synthesized_revision.py",
    )
    synthesized = resolve(
        module.ModelScopeResolutionAdapter(endpoint=hub.endpoint),
        PUBLIC_REPOSITORY_ID,
    )
    assert isinstance(synthesized, ResolutionSucceeded)
    assert len(synthesized.resolved.resolved_revision) == 64
    with pytest.raises(AssertionError):
        assert_the_recorded_branch_reference(synthesized)


def test_TC_DL_29_a_null_file_listing_is_an_explicit_failure(
    hub: FakeHub,
    adapter: ModelScopeResolutionAdapter,
) -> None:
    outcome = resolve(adapter, PUBLIC_REPOSITORY_ID, PUBLIC_UNKNOWN_BRANCH)

    assert_the_revision_does_not_exist(outcome)
    assert observed_sentinels(hub) == {FILES_NULL_SENTINEL}

    # The envelope this conclusion had to be drawn from looks like a success in
    # every field but the listing itself.
    body = load_recording("public-repo-files-unknown-branch").json_body()
    assert body["Code"] == 200
    assert body["Success"] is True
    assert body["Message"] == "success"
    assert body["Data"]["Files"] is None
    assert load_recording("public-repo-files-unknown-branch").status == 200


def test_TC_DL_29_a_well_formed_but_unknown_sha_answers_the_same_way(
    adapter: ModelScopeResolutionAdapter,
) -> None:
    assert_the_revision_does_not_exist(resolve(adapter, PUBLIC_REPOSITORY_ID, PUBLIC_UNKNOWN_SHA))
    # The two recordings are one answer: only the RequestId differs.
    assert body_without_request_id("public-repo-files-unknown-sha") == body_without_request_id(
        "public-repo-files-unknown-branch"
    )


def test_TC_DL_29_an_absent_listing_and_an_empty_one_are_different_answers() -> None:
    """The distinction lives in the parser, where the ``or []`` would go.

    ``Files: []`` has no recording -- an empty ModelScope repository was never
    captured -- so it is exercised here as a direct call on the parsing function
    and never as a fabricated source response (``PROVENANCE.md`` forbids
    synthesizing source evidence, and a fake 200 is exactly that).
    """

    assert parse_file_listing({"Files": None}) == FileListingAbsent()
    assert parse_file_listing({}) == FileListingAbsent()
    assert parse_file_listing({"Files": []}) == ()
    assert parse_file_listing({"Files": [{"Type": "socket"}]}) == FileListingUnreadable()
    assert parse_file_listing({"Files": "master"}) == FileListingUnreadable()


def test_TC_DL_29_positive_control_files_or_empty_list_resolves_a_mistyped_revision(
    hub: FakeHub,
    tmp_path: Path,
) -> None:
    """Plant ``Files or []`` in production and require the judgement to fail.

    This is the one line the whole case exists for: with it, a Revision nobody
    ever pushed becomes "resolved, and the model happens to have no files",
    while every positive case in this file stays green.
    """

    mutated_reads = load_mutated_module(
        SOURCE_READ_SOURCE,
        [('    entries = data.get("Files")\n', '    entries = data.get("Files") or []\n')],
        module_name="_modelscope_source_files_or_empty",
        destination=tmp_path / "modelscope_source_files_or_empty.py",
    )
    assert mutated_reads.parse_file_listing({"Files": None}) == ()
    module = load_mutated_module(
        ADAPTER_SOURCE,
        [
            (
                "from holm_control_api.modelscope_source import (",
                "from _modelscope_source_files_or_empty import (",
            )
        ],
        module_name="_modelscope_adapter_files_or_empty",
        destination=tmp_path / "modelscope_adapter_files_or_empty.py",
    )
    lenient = resolve(
        module.ModelScopeResolutionAdapter(endpoint=hub.endpoint),
        PUBLIC_REPOSITORY_ID,
        PUBLIC_UNKNOWN_BRANCH,
    )
    assert isinstance(lenient, ResolutionSucceeded)
    assert lenient.resolved.files == Identified(())
    assert observed_sentinels(hub) == {FILES_NULL_SENTINEL}
    with pytest.raises(AssertionError):
        assert_the_revision_does_not_exist(lenient)


def test_TC_DL_30_an_unspecified_revision_resolves_to_the_named_default_branch(
    hub: FakeHub,
    adapter: ModelScopeResolutionAdapter,
) -> None:
    outcome = resolve(adapter, PUBLIC_REPOSITORY_ID, DEFAULT_REQUESTED_REVISION)

    assert_the_recorded_branch_reference(outcome)
    assert isinstance(outcome, ResolutionSucceeded)
    assert outcome.resolved.requested_revision_norm == DEFAULT_REQUESTED_REVISION
    assert outcome.resolved.resolved_revision != DEFAULT_REQUESTED_REVISION

    # The sentinel is resolved before anything is asked of the source: no
    # request carries "@default", and the listing was fetched by branch name.
    assert all(request.revision() != DEFAULT_REQUESTED_REVISION for request in hub.requests)
    assert observed_sentinels(hub) == {RESOLVED_SENTINEL}

    # Which matters because the source would have accepted the omission
    # silently: an empty Revision returns the default branch's listing, so
    # "it worked" would not have told anyone which branch was read.
    assert body_without_request_id("public-repo-files-empty-revision") == body_without_request_id(
        "public-repo-files-master"
    )


def test_TC_DL_30_positive_control_leaking_the_default_sentinel_fails(
    hub: FakeHub,
    tmp_path: Path,
) -> None:
    module = load_mutated_module(
        ADAPTER_SOURCE,
        [
            (
                "    if requested_revision_norm != DEFAULT_REQUESTED_REVISION:\n"
                "        return requested_revision_norm\n",
                "    return requested_revision_norm\n",
            )
        ],
        module_name="_modelscope_leaking_default_sentinel",
        destination=tmp_path / "modelscope_leaking_default_sentinel.py",
    )
    leaked = resolve(
        module.ModelScopeResolutionAdapter(endpoint=hub.endpoint),
        PUBLIC_REPOSITORY_ID,
        DEFAULT_REQUESTED_REVISION,
    )
    # The mutation really did put the sentinel on the wire; the source has no
    # such Revision, so the fake Hub refuses instead of inventing an answer.
    assert any(request.revision() == DEFAULT_REQUESTED_REVISION for request in hub.requests)
    with pytest.raises(AssertionError):
        assert_the_recorded_branch_reference(leaked)


def test_TC_DL_28_the_hugging_face_side_still_states_an_immutable_commit(
    tmp_path: Path,
) -> None:
    """The marker is per source, and the other source's value did not move.

    M2-06A added the field; this is the case that keeps it from being read as
    "ModelScope's flag".  Hugging Face resolves a Branch to a repository-level
    Commit, so its preview must still say ``IMMUTABLE`` and still carry that
    Commit -- and its per-file hash stays explicitly unrecognized, because the
    dry-run plan M2-04A reads states none and inventing one here would change
    that Adapter's behaviour.
    """

    scratch_root = tmp_path / "scratch"
    scratch_root.mkdir()
    with fake_hugging_face_hub() as hub:
        outcome = asyncio.run(
            HuggingFaceResolutionAdapter(
                endpoint=hub.endpoint,
                scratch_root=scratch_root,
            ).resolve(
                ResolutionRequest(
                    source=ModelOriginType.HUGGING_FACE,
                    repository_id=HF_PUBLIC_REPOSITORY_ID,
                    requested_revision_norm=DEFAULT_REQUESTED_REVISION,
                ),
                credential=None,
            )
        )
    assert isinstance(outcome, ResolutionSucceeded)
    assert outcome.resolved.resolved_revision == PUBLIC_REPOSITORY_COMMIT
    assert outcome.resolved.revision_immutability is RevisionImmutability.IMMUTABLE
    files = outcome.resolved.files
    assert isinstance(files, Identified)
    assert {item.content_sha256 for item in files.value} == {Unrecognized()}
    assert list(scratch_root.iterdir()) == []


def test_TC_DL_28_the_response_carries_the_marker_and_every_per_file_hash(
    adapter: ModelScopeResolutionAdapter,
) -> None:
    """The two TDD 10.1 additions reach the endpoint payload, not just the domain."""

    outcome = resolve(adapter, PUBLIC_REPOSITORY_ID)
    assert isinstance(outcome, ResolutionSucceeded)
    payload = render_preview(
        ResolutionPreview(outcome=outcome, local_availability=LocalAvailability.ABSENT)
    ).model_dump(mode="json")

    assert payload["resolved"]["revision_immutability"] == "mutable_reference"
    assert payload["resolved"]["resolved_revision"] == PUBLIC_DEFAULT_BRANCH
    recorded = {
        str(entry["Path"]): str(entry["Sha256"])
        for entry in recorded_listing("public-repo-files-master")
    }
    rendered = payload["resolved"]["files"]["value"]
    assert {item["path"] for item in rendered} == set(recorded)
    for item in rendered:
        assert item["content_sha256"] == {
            "state": "identified",
            "value": recorded[item["path"]],
        }


def test_TC_DL_28_the_listing_is_requested_recursively(
    hub: FakeHub,
    adapter: ModelScopeResolutionAdapter,
) -> None:
    """A non-recursive listing would omit every file inside a directory."""

    resolve(adapter, PUBLIC_REPOSITORY_ID)
    listings = [request for request in hub.requests if request.path.endswith("/repo/files")]
    assert len(listings) == 1
    assert listings[0].query["Recursive"] == ("True",)
