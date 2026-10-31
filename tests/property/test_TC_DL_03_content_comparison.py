"""TC-DL-03: FX-MS-M2-08B v1 complete file manifests, generated offline."""

import random
from dataclasses import replace

import pytest
from holm_domain import Identified, RemoteFile, Unrecognized
from holm_domain.download_merge import (
    ArtifactFileManifestUnavailable,
    ComparisonUnavailable,
    ComparisonUnavailableReason,
    ContentsDiffer,
    ContentsMatch,
    compare_contents,
)


@pytest.mark.parametrize("seed", range(32))
def test_TC_DL_03_comparison_is_order_independent_and_hash_sensitive(seed: int) -> None:
    generator = random.Random(seed)
    entries = {
        f"nested/{index}-{generator.randrange(10000)}.bin": (
            generator.randrange(2**40),
            generator.randbytes(32),
        )
        for index in range(seed % 21)
    }
    files = tuple(
        RemoteFile(path, size, Identified(digest.hex())) for path, (size, digest) in entries.items()
    )
    assert isinstance(compare_contents(Identified(files), Identified(files[::-1])), ContentsMatch)
    unavailable = compare_contents(Identified(files), ArtifactFileManifestUnavailable())
    assert unavailable == ComparisonUnavailable(
        ComparisonUnavailableReason.ARTIFACT_FILE_MANIFEST_UNAVAILABLE
    )
    if files:
        first = files[0]
        assert isinstance(first.content_sha256, Identified)
        changed_hash = (
            "0" if first.content_sha256.value[0] != "0" else "1"
        ) + first.content_sha256.value[1:]
        for modified in (
            files[1:],
            (replace(first, size_bytes=first.size_bytes + 1), *files[1:]),
            (replace(first, content_sha256=Identified(changed_hash)), *files[1:]),
        ):
            assert isinstance(
                compare_contents(Identified(files), Identified(modified)), ContentsDiffer
            )


@pytest.mark.parametrize(
    "files",
    [
        Unrecognized(),
        Identified((RemoteFile("config.json", 1),)),
        Identified((RemoteFile("config.json", 1, Identified("a" * 64)),) * 2),
    ],
)
def test_TC_DL_03_incomplete_or_ambiguous_snapshots_are_rejected(files: object) -> None:
    with pytest.raises(ValueError):
        compare_contents(files, Identified(()))  # type: ignore[arg-type]
    assert isinstance(
        compare_contents(files, ArtifactFileManifestUnavailable()), ComparisonUnavailable
    )  # type: ignore[arg-type]
