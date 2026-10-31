"""M2-14A TC-DL-26: a source's file listing can never make a download write outside staging.

``FX-MS-06`` holds two samples a source can deliver over HTTP: a parent traversal
and an absolute path.  Both fake Hubs can serve them as listing paths
(``listing_path_substitutes``): only the name changes, while size, hash and body
stay the recorded ``config.json``.  The production download child then runs the
real official SDK against them; nothing inside either SDK is patched.

What each parameter proves differs, and the difference is the point:

* Hugging Face's SDK refuses both names itself
  (``huggingface_hub/_local_folder.py:199``).  That parameter passes with or
  without a platform guard, so it shows the judgement *can* pass and nothing more.
* ModelScope's SDK joins the listing path onto ``local_dir`` unchecked
  (``modelscope_hub/_download.py:573``).  Those parameters are the ones that pin
  the platform's own guard, and the positive control below runs that SDK with
  nothing in front of it to show the escape really happens and really is
  detected -- so a green ModelScope case cannot come from a blind detector.

A listing that differs between two reads is expressed by ``hostile_listing_after``:
a guard that inspects one listing and then lets the SDK fetch its own is bypassed
by exactly that source.

The benign control serves the same reduced listing under the recorded name, so a
failure in the hostile cases is attributable to the path and not to the shape of
the substituted listing.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from contextlib import AbstractContextManager
from pathlib import Path, PurePosixPath
from typing import Final
from uuid import uuid4

import pytest
from conftest import EphemeralPostgres
from fixtures.fake_model_source import PathAttackKind, path_attack_samples
from fixtures.hugging_face import PUBLIC_REPOSITORY_COMMIT, fake_hugging_face_hub
from fixtures.hugging_face import PUBLIC_REPOSITORY_ID as HF_PUBLIC_REPOSITORY_ID
from fixtures.hugging_face import FakeHub as HuggingFaceFakeHub
from fixtures.modelscope import CONFIG_FILENAME, PUBLIC_DEFAULT_BRANCH, fake_modelscope_hub
from fixtures.modelscope import PUBLIC_REPOSITORY_ID as MS_PUBLIC_REPOSITORY_ID
from fixtures.modelscope import FakeHub as ModelScopeFakeHub
from holm_domain import DownloadFailureReason, JobExecutionKey, ModelOriginType
from holm_download_worker import (
    DownloadSubprocessError,
    DownloadSubprocessRequest,
    ProductionDownloadProcess,
    SubprocessModelDownloader,
)
from migration_test_support import upgrade_head
from sqlalchemy.engine import URL

LISTING_DELIVERABLE_KINDS: Final[tuple[PathAttackKind, ...]] = (
    PathAttackKind.PARENT_TRAVERSAL,
    PathAttackKind.ABSOLUTE_PATH,
)
"""The FX-MS-06 kinds a source can put in a listing.  The other five describe an
entry on disk (symlink, device, FIFO, socket, setuid) and are M2-10B's
post-download verification (``test_M2_10B_TC_DL_09_10_25_artifact_validation.py``)."""

SOURCES: Final[tuple[ModelOriginType, ...]] = (
    ModelOriginType.HUGGING_FACE,
    ModelOriginType.MODELSCOPE,
)
FAILURE_CODES: Final[frozenset[str]] = frozenset(reason.value for reason in DownloadFailureReason)
"""Any persisted Download Task failure.  Which one is the implementation's choice;
it must be a stable member of the one vocabulary, never free text."""

SUBPROCESS_TIMEOUT_SECONDS: Final[float] = 120.0


@pytest.fixture(autouse=True)
def _migrated_empty_database(ephemeral_postgres: EphemeralPostgres) -> None:
    upgrade_head(ephemeral_postgres.engine)


def _write_secret(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o400)


def _process(fixture: EphemeralPostgres, tmp_path: Path) -> ProductionDownloadProcess:
    database_url_file = tmp_path / "database-url"
    _write_secret(
        database_url_file,
        URL.create(
            "postgresql+psycopg",
            username="ubuntu",
            database="postgres",
            query={"host": str(fixture.socket_directory), "port": str(fixture.port)},
        ).render_as_string(hide_password=False),
    )
    # No source row carries a credential, so the keyring file is never opened.
    return ProductionDownloadProcess(
        database_url_file=database_url_file,
        master_keyring_file=tmp_path / "unused-keyring",
        max_workers=1,
    )


def _listing_path(kind: PathAttackKind, tmp_path: Path) -> str:
    (sample,) = (item for item in path_attack_samples() if item.kind is kind)
    untrusted = PurePosixPath(sample.untrusted_value)
    if kind is PathAttackKind.ABSOLUTE_PATH:
        assert untrusted.is_absolute()
        # Relocated below tmp_path.  Until the guard exists a red run really
        # writes, and the container runs the suite as root: FX-MS-06's literal
        # absolute sample would land on the container's own root filesystem.
        return str(tmp_path / "absolute-outside" / untrusted.name)
    assert kind is PathAttackKind.PARENT_TRAVERSAL
    assert ".." in untrusted.parts
    return sample.untrusted_value


def _staging(tmp_path: Path) -> Path:
    staging = tmp_path / "work" / "staging"
    staging.mkdir(parents=True)
    return staging


def _escape_target(staging: Path, listing_path: str) -> Path:
    """Where a naive ``local_dir / path`` join puts the file."""

    return Path(os.path.normpath(staging / listing_path))


def _entries_outside(root: Path, staging: Path) -> frozenset[str]:
    """Every entry under ``root`` except ``staging`` and what it contains."""

    return frozenset(
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path != staging and staging not in path.parents
    )


def _hub(
    source: ModelOriginType,
    substitutes: tuple[str, ...],
    *,
    hostile_listing_after: int = 0,
) -> AbstractContextManager[HuggingFaceFakeHub | ModelScopeFakeHub]:
    if source is ModelOriginType.HUGGING_FACE:
        return fake_hugging_face_hub(
            listing_path_substitutes=substitutes,
            hostile_listing_after=hostile_listing_after,
        )
    return fake_modelscope_hub(
        listing_path_substitutes=substitutes,
        hostile_listing_after=hostile_listing_after,
    )


def _request(source: ModelOriginType, staging: Path, endpoint: str) -> DownloadSubprocessRequest:
    if source is ModelOriginType.HUGGING_FACE:
        repository_id, revision = HF_PUBLIC_REPOSITORY_ID, PUBLIC_REPOSITORY_COMMIT
    else:
        repository_id, revision = MS_PUBLIC_REPOSITORY_ID, PUBLIC_DEFAULT_BRANCH
    return DownloadSubprocessRequest(
        source=source,
        repository_id=repository_id,
        resolved_revision=revision,
        staging_path=staging,
        endpoint=endpoint,
        execution_key=JobExecutionKey(uuid4(), 1),
    )


def _download(process: ProductionDownloadProcess, request: DownloadSubprocessRequest) -> object:
    async def no_progress(increment: int) -> None:
        del increment

    try:
        return asyncio.run(SubprocessModelDownloader(process).download(request, no_progress))
    except DownloadSubprocessError as error:
        return error


@pytest.mark.parametrize("kind", LISTING_DELIVERABLE_KINDS, ids=str)
@pytest.mark.parametrize("source", SOURCES, ids=str)
def test_TC_DL_26_M2_14A_an_escaping_listing_path_never_writes_outside_staging(
    source: ModelOriginType,
    kind: PathAttackKind,
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    process = _process(ephemeral_postgres, tmp_path)
    staging = _staging(tmp_path)
    listing_path = _listing_path(kind, tmp_path)
    before = _entries_outside(tmp_path, staging)

    with _hub(source, (listing_path,)) as hub:
        outcome = _download(process, _request(source, staging, hub.endpoint))
        delivered = hub.hostile_listings_served

    # The attack reached the SDK; a guard that never saw the listing proves nothing.
    assert delivered >= 1
    assert not _escape_target(staging, listing_path).exists(), "a file was written outside staging"
    assert _entries_outside(tmp_path, staging) == before
    assert isinstance(outcome, DownloadSubprocessError), "an escaping listing path completed"
    assert outcome.code in FAILURE_CODES
    for rendered in (str(outcome), repr(outcome), caplog.text):
        assert listing_path not in rendered


@pytest.mark.parametrize("kind", LISTING_DELIVERABLE_KINDS, ids=str)
def test_TC_DL_26_M2_14A_a_listing_that_turns_hostile_on_a_later_read_never_escapes(
    kind: PathAttackKind,
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
) -> None:
    """ModelScope only: its SDK is the one with no guard of its own to fall back on."""

    process = _process(ephemeral_postgres, tmp_path)
    staging = _staging(tmp_path)
    listing_path = _listing_path(kind, tmp_path)
    before = _entries_outside(tmp_path, staging)

    with fake_modelscope_hub(
        listing_path_substitutes=(listing_path,),
        hostile_listing_after=1,
    ) as hub:
        outcome = _download(process, _request(ModelOriginType.MODELSCOPE, staging, hub.endpoint))
        delivered = hub.hostile_listings_served

    assert not _escape_target(staging, listing_path).exists(), "a file was written outside staging"
    assert _entries_outside(tmp_path, staging) == before
    if isinstance(outcome, DownloadSubprocessError):
        assert outcome.code in FAILURE_CODES
        assert listing_path not in f"{outcome}{outcome!r}"
    else:
        # Completing is acceptable only when the one listing ever read was the
        # benign first answer: then nothing hostile was delivered to anyone.
        assert delivered == 0
        assert (staging / CONFIG_FILENAME).is_file()


@pytest.mark.parametrize("source", SOURCES, ids=str)
def test_TC_DL_26_M2_14A_control_the_substituted_listing_under_its_recorded_name_downloads(
    source: ModelOriginType,
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
) -> None:
    process = _process(ephemeral_postgres, tmp_path)
    staging = _staging(tmp_path)
    before = _entries_outside(tmp_path, staging)

    with _hub(source, (CONFIG_FILENAME,)) as hub:
        outcome = _download(process, _request(source, staging, hub.endpoint))
        delivered = hub.hostile_listings_served

    assert delivered >= 1
    assert not isinstance(outcome, DownloadSubprocessError), outcome.code
    assert (staging / CONFIG_FILENAME).is_file()
    assert _entries_outside(tmp_path, staging) == before


@pytest.mark.parametrize("kind", LISTING_DELIVERABLE_KINDS, ids=str)
def test_TC_DL_26_M2_14A_positive_control_unguarded_modelscope_sdk_escape_is_detected(
    kind: PathAttackKind,
    tmp_path: Path,
) -> None:
    """The official ModelScope SDK with nothing in front of it really escapes.

    It runs in its own interpreter with its own ``HOME``, so the SDK's cache and
    log files land in ``tmp_path`` instead of the test runner's home.  If a
    future SDK starts refusing these names this control fails -- the signal
    that the ModelScope parameters above no longer isolate the platform guard.
    """

    staging = _staging(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    listing_path = _listing_path(kind, tmp_path)
    before = _entries_outside(tmp_path, staging)
    script = (
        "import sys\n"
        "from modelscope import snapshot_download\n"
        "snapshot_download(sys.argv[1], revision=sys.argv[2], local_dir=sys.argv[3],"
        " endpoint=sys.argv[4], max_workers=1)\n"
    )

    with fake_modelscope_hub(listing_path_substitutes=(listing_path,)) as hub:
        completed = subprocess.run(  # noqa: S603 - fixed interpreter and argument vector
            [
                sys.executable,
                "-c",
                script,
                MS_PUBLIC_REPOSITORY_ID,
                PUBLIC_DEFAULT_BRANCH,
                str(staging),
                hub.endpoint,
            ],
            env={"HOME": str(home), "PATH": os.environ.get("PATH", ""), "LANG": "C.UTF-8"},
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
            check=False,
        )
        delivered = hub.hostile_listings_served

    escaped = _escape_target(staging, listing_path)
    assert delivered == 1
    assert escaped.is_file(), completed.stderr[-2000:]
    assert escaped.relative_to(tmp_path).as_posix() in _entries_outside(tmp_path, staging) - before
