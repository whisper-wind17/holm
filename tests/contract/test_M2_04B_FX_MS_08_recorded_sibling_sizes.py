"""FX-MS-08: an unmeasured file keeps its place in the listing and states no size.

Two of the recorded metadata responses carry ``siblings`` entries with nothing
but an ``rfilename``: ``gated-repo-metadata`` (17 files) and
``invalid-credential-public-repo`` (14 files).  The Hub fills ``size``/``blobId``
in only for a metadata read that asked for ``?blobs=true``, and those two
recordings did not, so for them the recorded evidence about file sizes is
literally absent.

That leaves the fake Hub exactly two honest choices when it re-serves such a
listing, and both of the tempting ones are forbidden: filling the gap with ``0``
invents a measurement the source never made (the privacy policy Invariant,
"禁止 ``or 0`` 填充"; PDD line 710), and leaving the entry out shrinks the file
listing by 17 entries while every positive case stays green.  So the size is
carried as explicitly missing and the served tree says ``null``.

Both branches are exercised against the real ``huggingface_hub`` SDK talking to
the real fake Hub -- the fully measured public repository and the wholly
unmeasured gated one -- and each of the two forbidden shapes is planted back
into the fixture source to prove the judgement below fails when it should.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

import pytest
from fixtures.hugging_face import (
    GATED_REPOSITORY_ID,
    PUBLIC_REPOSITORY_ID,
    fake_hugging_face_hub,
    load_recording,
)
from fixtures.source_mutation import load_mutated_module
from huggingface_hub import HfApi

FAKE_HUB_SOURCE: Final[Path] = (
    Path(__file__).resolve().parents[1] / "fixtures/hugging_face/fake_hub.py"
)

GATED_FILE_COUNT: Final[int] = 17
PUBLIC_FILE_COUNT: Final[int] = 14


def recorded_listing(recording_name: str) -> dict[str, int | None]:
    """The recorded ``siblings`` array, read straight off the recording.

    This deliberately does not go through the fake Hub: an expectation derived
    from the code under test would move with it, and "both sides changed
    together" is not a judgement (``LESSONS.md``, M2-15B R1-2).
    """

    body = load_recording(recording_name).json_body()
    siblings = body["siblings"]
    assert isinstance(siblings, list)
    return {str(entry["rfilename"]): entry.get("size") for entry in siblings}


def served_listing(endpoint: str, repository_id: str) -> dict[str, int | None]:
    """What the SDK reads back from the fake Hub's tree listing.

    ``token=False`` is "send no Authorization header"; ``None`` would let the
    SDK pick an ambient token up from the environment.
    """

    api = HfApi(endpoint=endpoint, token=False)
    return {
        entry.path: entry.size
        for entry in api.list_repo_tree(repository_id, recursive=True, revision="main")
    }


def assert_listing_states_every_recorded_size(
    served: dict[str, int | None],
    recorded: dict[str, int | None],
) -> None:
    """Every recorded file is listed once, with exactly what the recording says.

    One equality carries both halves: a dropped entry changes the key set and a
    filled-in size changes the value, so neither forbidden shape can pass.
    """

    if served != recorded:
        disagreeing = sorted(set(served) | set(recorded))
        detail = [
            (path, served.get(path, "<not listed>"), recorded.get(path, "<not recorded>"))
            for path in disagreeing
            if served.get(path, "<not listed>") != recorded.get(path, "<not recorded>")
        ]
        raise AssertionError(
            f"the served listing does not match the recorded siblings: {detail[:8]}"
        )


def test_FX_MS_08_the_two_recorded_listings_disagree_about_measurement() -> None:
    """The premise of this case, taken from the recordings themselves.

    Without this the two branches below could both be the measured one and the
    unmeasured path would never run.
    """

    public = recorded_listing("public-repo-main")
    gated = recorded_listing("gated-repo-metadata")

    assert len(public) == PUBLIC_FILE_COUNT
    assert all(isinstance(size, int) and size > 0 for size in public.values())

    assert len(gated) == GATED_FILE_COUNT
    assert all(size is None for size in gated.values())

    # Not a permission effect: the same shape appears on a *public* repository
    # read with an invalid token, which PROVENANCE.md records as returning full
    # metadata.  What the two have in common is that neither read asked for
    # ``?blobs=true``.
    unmeasured_public = recorded_listing("invalid-credential-public-repo")
    assert len(unmeasured_public) == PUBLIC_FILE_COUNT
    assert all(size is None for size in unmeasured_public.values())


def test_FX_MS_08_the_served_tree_reports_the_recorded_size_or_an_explicit_null() -> None:
    """Both branches, through the real SDK against the real fake Hub."""

    with fake_hugging_face_hub() as hub:
        measured = served_listing(hub.endpoint, PUBLIC_REPOSITORY_ID)
        unmeasured = served_listing(hub.endpoint, GATED_REPOSITORY_ID)

    assert_listing_states_every_recorded_size(measured, recorded_listing("public-repo-main"))
    assert len(measured) == PUBLIC_FILE_COUNT
    assert all(isinstance(size, int) for size in measured.values())

    assert_listing_states_every_recorded_size(unmeasured, recorded_listing("gated-repo-metadata"))
    assert len(unmeasured) == GATED_FILE_COUNT
    # ``None`` and not ``0``: the file is listed, and nothing claims to know how
    # large it is.
    assert all(size is None for size in unmeasured.values())
    assert 0 not in set(unmeasured.values())


def _serve_mutated_gated_listing(
    replacements: list[tuple[str, str]],
    *,
    module_name: str,
    destination: Path,
) -> dict[str, int | None]:
    """Run the mutated fixture for real and read its gated tree listing back."""

    module = load_mutated_module(
        FAKE_HUB_SOURCE,
        replacements,
        module_name=module_name,
        destination=destination,
    )
    with module.fake_hugging_face_hub() as hub:
        return served_listing(hub.endpoint, GATED_REPOSITORY_ID)


def test_FX_MS_08_positive_control_filling_a_missing_size_with_zero_is_detected(
    tmp_path: Path,
) -> None:
    """Planting ``or 0`` back into the fixture must fail the judgement loudly."""

    served = _serve_mutated_gated_listing(
        [
            (
                '                size_bytes=_recorded_number(entry.get("size")),\n',
                '                size_bytes=int(entry.get("size", 0)),\n',
            )
        ],
        module_name="fixtures.hugging_face._zero_filled_size_mutation",
        destination=tmp_path / "zero_filled_size_mutation.py",
    )

    # The mutation really took effect: the listing is complete but measured.
    assert len(served) == GATED_FILE_COUNT
    assert set(served.values()) == {0}
    with pytest.raises(AssertionError):
        assert_listing_states_every_recorded_size(served, recorded_listing("gated-repo-metadata"))


def test_FX_MS_08_positive_control_dropping_a_sizeless_sibling_is_detected(
    tmp_path: Path,
) -> None:
    """Skipping the unmeasured files must fail the judgement loudly.

    This is the shape that would otherwise pass unnoticed: the gated listing
    would simply be empty and every measured repository would stay green.
    """

    served = _serve_mutated_gated_listing(
        [
            (
                "            for entry in entries\n        )\n",
                "            for entry in entries\n"
                '            if entry.get("size") is not None\n'
                "        )\n",
            )
        ],
        module_name="fixtures.hugging_face._dropped_sibling_mutation",
        destination=tmp_path / "dropped_sibling_mutation.py",
    )

    # The mutation really took effect: 17 recorded files became none at all.
    assert served == {}
    with pytest.raises(AssertionError):
        assert_listing_states_every_recorded_size(served, recorded_listing("gated-repo-metadata"))
