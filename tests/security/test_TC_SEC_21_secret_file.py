"""TC-SEC-21 Secret File Reader security boundary tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from holm_common.secrets import SecretFileError, read_secret_file

SENTINEL = b"TC_SEC_21_SECRET_BYTES_MUST_STAY_IN_MEMORY"


def _secret_directory(tmp_path: Path, mode: int = 0o750) -> Path:
    directory = tmp_path / "secrets"
    directory.mkdir(mode=mode)
    directory.chmod(mode)
    return directory


def _secret_file(directory: Path, mode: int = 0o400, content: bytes = SENTINEL) -> Path:
    path = directory / "value"
    path.write_bytes(content)
    path.chmod(mode)
    return path


@pytest.mark.parametrize("mode", [0o400, 0o440])
def test_TC_SEC_21_reads_only_allowed_modes_without_transforming_bytes(
    tmp_path: Path, mode: int
) -> None:
    secret_path = _secret_file(_secret_directory(tmp_path), mode)

    assert read_secret_file(secret_path) == SENTINEL


@pytest.mark.parametrize("mode", [0o000, 0o600, 0o640, 0o444])
def test_TC_SEC_21_rejects_secret_file_permissions(tmp_path: Path, mode: int) -> None:
    secret_path = _secret_file(_secret_directory(tmp_path), mode)

    with pytest.raises(SecretFileError) as captured:
        read_secret_file(secret_path)

    assert captured.value.code == "file_permissions"


# M7-01B：root 所有且组/其他人不可写的父目录（Docker /run/secrets）合法，测试检查以 root 执行时
# 0o755 会走该分支；改用在两条规则下都过宽的权限，判定「过宽即拒」不变。
@pytest.mark.parametrize("mode", [0o775, 0o757])
def test_TC_SEC_21_rejects_directory_permissions(tmp_path: Path, mode: int) -> None:
    secret_path = _secret_file(_secret_directory(tmp_path, mode))

    with pytest.raises(SecretFileError) as captured:
        read_secret_file(secret_path)

    assert captured.value.code == "directory_permissions"


@pytest.mark.parametrize(
    "identity",
    [
        {"expected_uid": os.geteuid() + 1},
        {"expected_gid": os.getegid() + 1},
    ],
)
def test_TC_SEC_21_rejects_owner_mismatch(tmp_path: Path, identity: dict[str, int]) -> None:
    secret_path = _secret_file(_secret_directory(tmp_path))

    with pytest.raises(SecretFileError) as captured:
        read_secret_file(secret_path, **identity)

    assert captured.value.code == "owner_mismatch"


def test_TC_SEC_21_rejects_relative_and_missing_paths(tmp_path: Path) -> None:
    with pytest.raises(SecretFileError) as relative:
        read_secret_file(Path("relative-secret"))
    with pytest.raises(SecretFileError) as missing:
        read_secret_file(tmp_path / "missing" / "value")

    assert relative.value.code == "invalid_path"
    assert missing.value.code == "unreadable"


def test_TC_SEC_21_rejects_nul_path_with_sanitized_error() -> None:
    sentinel = "/secret/TC_SEC_21_NUL_MUST_NOT_LEAK\x00/value"

    with pytest.raises(SecretFileError) as captured:
        read_secret_file(sentinel)

    assert captured.value.code == "invalid_path"
    assert sentinel not in str(captured.value)
    assert sentinel not in repr(captured.value)


def test_TC_SEC_21_rejects_file_and_directory_symlinks(tmp_path: Path) -> None:
    real_directory = _secret_directory(tmp_path)
    secret_path = _secret_file(real_directory)
    file_link = tmp_path / "file-link"
    file_link.symlink_to(secret_path)
    directory_link = tmp_path / "directory-link"
    directory_link.symlink_to(real_directory, target_is_directory=True)

    for candidate in (file_link, directory_link / "value"):
        with pytest.raises(SecretFileError) as captured:
            read_secret_file(candidate)
        assert captured.value.code == "symlink"


def test_TC_SEC_21_rejects_non_regular_files(tmp_path: Path) -> None:
    directory = _secret_directory(tmp_path)
    nested_directory = directory / "nested"
    nested_directory.mkdir(mode=0o400)
    fifo = directory / "fifo"
    os.mkfifo(fifo, mode=0o400)

    for candidate in (nested_directory, fifo):
        with pytest.raises(SecretFileError) as captured:
            read_secret_file(candidate)
        assert captured.value.code == "not_regular"


def test_TC_SEC_21_rejects_oversized_content(tmp_path: Path) -> None:
    secret_path = _secret_file(_secret_directory(tmp_path), content=b"12345")

    with pytest.raises(SecretFileError) as captured:
        read_secret_file(secret_path, max_bytes=4)

    assert captured.value.code == "too_large"


def test_TC_SEC_21_directory_swap_cannot_redirect_secret_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir(mode=0o750)
    secure = root / "secure"
    secure.mkdir(mode=0o750)
    original_secret = _secret_file(secure, content=b"pinned-secret")
    attacker = tmp_path / "attacker"
    attacker.mkdir(mode=0o750)
    _secret_file(attacker, content=b"redirected-secret")
    pinned = root / "secure-pinned"
    original_open = os.open
    swapped = False

    def swapping_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if path == original_secret.name and dir_fd is not None and not swapped:
            secure.rename(pinned)
            secure.symlink_to(attacker, target_is_directory=True)
            swapped = True
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", swapping_open)

    assert read_secret_file(original_secret) == b"pinned-secret"
    assert swapped


def test_TC_SEC_21_errors_and_logs_exclude_secret_path_and_value(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    secret_path = _secret_file(_secret_directory(tmp_path), mode=0o600)

    with pytest.raises(SecretFileError) as captured:
        read_secret_file(secret_path)

    rendered = f"{captured.value!s} {captured.value!r} {caplog.text}"
    assert SENTINEL.decode() not in rendered
    assert str(secret_path) not in rendered
