"""M2-14D (offline half): residue in staging makes publication fail loudly.

The 2026-09-14 probe read-out found ModelScope leaving ``<name>.incomplete`` at
the staging root on every interrupted phase, and writing a session file under
``.cache/modelscope/home/credentials/``.  Two things keep either out of a Ready
Artifact, and each gets its own check here:

* the exact file-set equality in ``ArtifactVerifier.verify`` -- any name at the
  root that the source listing does not name is ``ARTIFACT_FILE_LIST_INVALID``;
* the ``CACHEDIR.TAG`` pruning -- the only thing standing between the SDK's
  session file and the Artifact.  Removing the tag must make publication fail.

The online half (the real run reaching Ready with no residue) is in
``tests/certification``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from holm_domain import DownloadFailureReason
from holm_download_worker.artifact_publishing import (
    CACHEDIR_SIGNATURE,
    ArtifactValidationError,
    ArtifactVerifier,
)
from test_M2_10B_TC_DL_09_10_25_artifact_validation import _resolved, _write_valid


@pytest.mark.parametrize(
    "residue",
    [
        "model.safetensors.incomplete",
        "model.safetensors_0_1048575",
        "tmpk2j3h4x_",
    ],
)
def test_TC_DL_25_M2_14D_residue_at_the_staging_root_fails_publication(
    tmp_path: Path, residue: str
) -> None:
    root = tmp_path / "staging"
    _write_valid(root)
    resolved = _resolved(root)
    ArtifactVerifier().verify(root, resolved)  # clean staging publishes

    (root / residue).write_bytes(b"left behind")

    with pytest.raises(ArtifactValidationError) as captured:
        ArtifactVerifier().verify(root, resolved)
    assert captured.value.reason is DownloadFailureReason.ARTIFACT_FILE_LIST_INVALID


def test_TC_DL_25_M2_14D_the_sdk_session_file_is_kept_out_only_by_cachedir_tag(
    tmp_path: Path,
) -> None:
    root = tmp_path / "staging"
    _write_valid(root)
    resolved = _resolved(root)
    state = root / ".cache" / "modelscope"
    (state / "home" / "credentials").mkdir(parents=True)
    (state / "CACHEDIR.TAG").write_bytes(CACHEDIR_SIGNATURE + b"\n")
    (state / "home" / "credentials" / "session").write_bytes(b"x" * 32)

    ArtifactVerifier().verify(root, resolved)

    (state / "CACHEDIR.TAG").unlink()
    with pytest.raises(ArtifactValidationError) as captured:
        ArtifactVerifier().verify(root, resolved)
    assert captured.value.reason is DownloadFailureReason.ARTIFACT_FILE_LIST_INVALID
