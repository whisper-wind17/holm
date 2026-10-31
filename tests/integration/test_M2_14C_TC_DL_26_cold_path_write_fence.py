"""M2-14C TC-DL-26: the write fence must let a benign download through.

M2-14A installed a process-wide audit hook that refuses every filesystem write
whose target is not a safe relative path under staging.  That is strictly
correct against the attack it was built for, and strictly too wide for anything
else: the interpreter's own ``.pyc`` files, ``tempfile``'s scratch files and any
SDK cache that has not been redirected all land outside staging and are refused
too, which kills a benign download.

Measured on 2026-09-12 against the live ``https://www.modelscope.cn`` through the
production child: with warm bytecode the download succeeds (663 bytes delivered,
``config.json`` in staging); with ``PYTHONPYCACHEPREFIX`` pointed at an empty
directory it fails with ``download_process_failed`` and delivers nothing.  CI is
green today only because the container's site-packages bytecode is pre-compiled.

Narrowing the fence has one obvious hole, and the parameters below exist to
close it.  ``local_dir`` is staging and the SDK joins the listing path onto it
unchecked, so ``staging/../../<D>/x`` *is* ``<D>/x``: permitting any directory
outside staging permits a listing path that resolves into it.  Whatever the
implementation allows through, a listing path aimed at the same place must still
be refused.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Final

import pytest
from conftest import EphemeralPostgres
from fixtures.modelscope import CONFIG_FILENAME, PUBLIC_DEFAULT_BRANCH, fake_modelscope_hub
from fixtures.modelscope import PUBLIC_REPOSITORY_ID as MS_PUBLIC_REPOSITORY_ID
from holm_domain import ModelOriginType
from holm_download_worker import DownloadSubprocessError
from migration_test_support import upgrade_head
from test_M2_14A_TC_DL_26_download_listing_paths import (
    SOURCES,
    SUBPROCESS_TIMEOUT_SECONDS,
    _download,
    _entries_outside,
    _escape_target,
    _hub,
    _process,
    _request,
    _staging,
)

BYTECODE_PREFIX_VARIABLE: Final[str] = "PYTHONPYCACHEPREFIX"

OUTSIDE_TARGETS: Final[tuple[str, ...]] = ("bytecode_prefix", "child_temporary_directory")
"""The two places a narrowing is most likely to permit.

``bytecode_prefix`` is where the interpreter wants to write ``.pyc``;
``child_temporary_directory`` is what ``tempfile`` resolves to inside the child
(``install_child_environment`` clears the environment and ``CHILD_ENVIRONMENT``
carries no ``TMPDIR``, so it is the system default).  Both are outside staging,
so both are reachable by a listing path.
"""


@pytest.fixture(autouse=True)
def _migrated_empty_database(ephemeral_postgres: EphemeralPostgres) -> None:
    upgrade_head(ephemeral_postgres.engine)


@pytest.fixture
def cold_bytecode_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point the child interpreter's bytecode cache at an empty outside directory.

    ``PYTHONPYCACHEPREFIX`` is read at interpreter start-up, so the spawned child
    honours it even though ``install_child_environment`` clears the environment
    once the child is already running.
    """

    root = tmp_path / "cold-bytecode"
    root.mkdir()
    monkeypatch.setenv(BYTECODE_PREFIX_VARIABLE, str(root))
    yield root


def _outside_directory(name: str, cold_bytecode_root: Path) -> Path:
    if name == "bytecode_prefix":
        return cold_bytecode_root
    assert name == "child_temporary_directory"
    # The child's tempfile default, not this process's tmp_path.
    return Path("/tmp")


@pytest.mark.parametrize("cold", [False, True], ids=["warm_bytecode", "cold_bytecode"])
@pytest.mark.parametrize("source", SOURCES, ids=str)
def test_TC_DL_26_M2_14C_benign_download_succeeds_on_a_cold_path(
    source: ModelOriginType,
    cold: bool,
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A download with nothing hostile about it must not be refused.

    The listing is reduced to the one file both fake Hubs hold a recorded body
    for, exactly as M2-14A's benign control does
    (``test_M2_14A_TC_DL_26_download_listing_paths.py``): an unreduced listing
    makes the SDK ask for bodies that were never recorded and the Hub answers
    599, which would fail this case for a reason that has nothing to do with the
    fence.

    ``warm_bytecode`` is green today.  That is what makes ``cold_bytecode``'s
    failure attributable: same listing, same Hub, same production child -- the
    only thing that changed is where the interpreter wants to put its ``.pyc``.

    The assertion deliberately does not require any ``.pyc`` to appear: an
    implementation that stops those writes happening at all (redirecting them
    under staging, or not writing bytecode in the child) is a good answer and
    must pass here too.
    """

    bytecode_root = tmp_path / "cold-bytecode"
    bytecode_root.mkdir()
    if cold:
        monkeypatch.setenv(BYTECODE_PREFIX_VARIABLE, str(bytecode_root))
    else:
        monkeypatch.delenv(BYTECODE_PREFIX_VARIABLE, raising=False)

    process = _process(ephemeral_postgres, tmp_path)
    staging = _staging(tmp_path)

    with _hub(source, (CONFIG_FILENAME,)) as hub:
        outcome = _download(process, _request(source, staging, hub.endpoint))
        served = hub.hostile_listings_served

    assert served >= 1
    assert not isinstance(outcome, DownloadSubprocessError), outcome.code
    assert (staging / CONFIG_FILENAME).is_file()


@pytest.mark.parametrize("cold", [False, True], ids=["warm_bytecode", "cold_bytecode"])
@pytest.mark.parametrize("outside", OUTSIDE_TARGETS)
@pytest.mark.parametrize("source", SOURCES, ids=str)
def test_TC_DL_26_M2_14C_a_listing_path_into_a_permitted_directory_is_still_refused(
    source: ModelOriginType,
    outside: str,
    cold: bool,
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Permitting a directory must not permit a listing path aimed at it.

    The pair matters.  ``warm_bytecode`` is green today, which is what proves
    this test can tell a refusal from an escape at all.  ``cold_bytecode`` is red
    today for an unrelated reason -- the child dies on its own ``.pyc`` before the
    listing is ever fetched, so ``served`` is 0 -- and becomes the real question
    only once the cold path works: at that point the download proceeds and the
    hostile path is genuinely delivered into whatever the narrowing permits.
    Without the warm half, a green cold half could mean nothing was ever served.
    """

    bytecode_root = tmp_path / "cold-bytecode"
    bytecode_root.mkdir()
    if cold:
        monkeypatch.setenv(BYTECODE_PREFIX_VARIABLE, str(bytecode_root))
    else:
        monkeypatch.delenv(BYTECODE_PREFIX_VARIABLE, raising=False)

    process = _process(ephemeral_postgres, tmp_path)
    staging = _staging(tmp_path)
    target = _outside_directory(outside, bytecode_root) / f"m2-14c-{os.getpid()}-{cold}-pwned.txt"
    listing_path = os.path.relpath(target, staging)
    assert not target.exists()
    before = _entries_outside(tmp_path, staging)

    try:
        with _hub(source, (listing_path,)) as hub:
            outcome = _download(process, _request(source, staging, hub.endpoint))
            served = hub.hostile_listings_served

        # The attack has to have been delivered, or "nothing escaped" is empty.
        assert served >= 1
        assert not target.exists(), f"{target} was created"
        assert _escape_target(staging, listing_path) == target
        assert _entries_outside(tmp_path, staging) == before
        assert isinstance(outcome, DownloadSubprocessError)
    finally:
        target.unlink(missing_ok=True)


@pytest.mark.parametrize("outside", OUTSIDE_TARGETS)
def test_TC_DL_26_M2_14C_positive_control_the_unguarded_sdk_reaches_those_directories(
    outside: str,
    tmp_path: Path,
    cold_bytecode_root: Path,
) -> None:
    """Without the fence the same listing path really does land the file there.

    Without this, a green refusal above could just as well mean the SDK never
    tried, or that the fake Hub never served the hostile listing.
    """

    staging = _staging(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    target = _outside_directory(outside, cold_bytecode_root) / f"m2-14c-control-{os.getpid()}.txt"
    listing_path = os.path.relpath(target, staging)
    assert not target.exists()
    script = (
        "import sys\n"
        "from modelscope import snapshot_download\n"
        "snapshot_download(sys.argv[1], revision=sys.argv[2], local_dir=sys.argv[3],"
        " endpoint=sys.argv[4], max_workers=1)\n"
    )

    try:
        with fake_modelscope_hub(listing_path_substitutes=(listing_path,)) as hub:
            subprocess.run(  # noqa: S603 - fixed interpreter and argument vector
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
            served = hub.hostile_listings_served

        assert served == 1
        assert target.is_file(), f"the unguarded SDK did not reach {target}"
    finally:
        target.unlink(missing_ok=True)


def test_TC_DL_26_M2_14C_control_the_cold_bytecode_lever_really_writes_outside_staging(
    tmp_path: Path,
    cold_bytecode_root: Path,
) -> None:
    """The cold-path lever has to be real, or the first test proves nothing.

    A plain interpreter with the same prefix must actually write ``.pyc`` there.
    If a future image pre-compiles into this prefix, or the variable stops being
    honoured, this control fails and says so instead of letting the cold-path
    test pass for the wrong reason.
    """

    assert not any(cold_bytecode_root.rglob("*.pyc"))
    completed = subprocess.run(  # noqa: S603 - fixed interpreter and argument vector
        [sys.executable, "-c", "import modelscope, huggingface_hub  # noqa: F401"],
        env={
            "HOME": str(tmp_path / "home-control"),
            "PATH": os.environ.get("PATH", ""),
            "LANG": "C.UTF-8",
            BYTECODE_PREFIX_VARIABLE: str(cold_bytecode_root),
        },
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    assert any(cold_bytecode_root.rglob("*.pyc")), "PYTHONPYCACHEPREFIX wrote nothing"
