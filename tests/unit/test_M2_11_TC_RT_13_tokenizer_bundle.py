"""TC-RT-13: minimal Tokenizer Bundle policy, staging, and atomic publish."""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Final, cast
from uuid import uuid4

import pytest
from fixtures.source_mutation import load_mutated_module
from holm_common import TokenizerStagingLayout
from holm_domain import ArtifactManifest, ArtifactManifestEntry, build_artifact_manifest
from holm_download_worker import (
    TOKENIZER_BUNDLE_ALLOWLIST,
    ArtifactPublicationError,
    ArtifactSystemCalls,
    TokenizerBundleBuilt,
    TokenizerBundleEmpty,
    TokenizerBundleRecoveryError,
    TokenizerBundleRejected,
    TokenizerBundleRejectionReason,
    build_managed_tokenizer_bundle,
    publish_verified_artifact,
    recover_tokenizer_bundle,
)

ROOT: Final[Path] = Path(__file__).resolve().parents[2]
BUNDLE_SOURCE: Final[Path] = (
    ROOT / "apps/workers/download/src/holm_download_worker/tokenizer_bundle.py"
)


def _entry(path: str, content: bytes) -> ArtifactManifestEntry:
    return ArtifactManifestEntry(path, len(content), hashlib.sha256(content).hexdigest())


def _source_manifest(root: Path, payloads: dict[str, bytes]) -> ArtifactManifest:
    for relative, content in payloads.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    return build_artifact_manifest(_entry(path, content) for path, content in payloads.items())


def _assert_bundle_is_minimal(bundle: TokenizerBundleBuilt) -> None:
    paths = {entry.path for entry in bundle.manifest.files}
    assert paths
    assert paths <= TOKENIZER_BUNDLE_ALLOWLIST
    assert not paths & {
        "evil.py",
        "libx.so",
        "model.safetensors",
        "run-tokenizer",
        "outside-link",
    }


def _build(
    tmp_path: Path,
    payloads: dict[str, bytes],
    *,
    tokenizer_root: Path | None = None,
) -> TokenizerBundleBuilt | TokenizerBundleEmpty | TokenizerBundleRejected:
    model_root = tmp_path / "models"
    source = model_root / ".staging" / str(uuid4())
    manifest = _source_manifest(source, payloads)
    root = tokenizer_root if tokenizer_root is not None else tmp_path / "tokenizers"
    return build_managed_tokenizer_bundle(
        source,
        manifest,
        layout=TokenizerStagingLayout(root.resolve()),
        model_root=model_root.resolve(),
        artifact_id=uuid4(),
    )


def test_TC_RT_13_allowlist_default_denies_weight_code_library_executable_and_link(
    tmp_path: Path,
) -> None:
    payloads = {
        "config.json": b'{"model_type":"fixture"}',
        "tokenizer.json": b'{"version":"1.0"}',
        "special_tokens_map.json": b'{"eos_token":"</s>"}',
        "evil.py": b"raise SystemExit",
        "libx.so": b"ELF fixture",
        "model.safetensors": b"weight fixture",
        "run-tokenizer": b"#!/bin/sh\nexit 1\n",
    }
    model_root = tmp_path / "models"
    source = model_root / ".staging" / str(uuid4())
    manifest = _source_manifest(source, payloads)
    executable = source / "run-tokenizer"
    executable.chmod(0o755)
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside")
    (source / "outside-link").symlink_to(outside)
    manifest = build_artifact_manifest((*manifest.files, _entry("outside-link", b"outside")))

    result = build_managed_tokenizer_bundle(
        source,
        manifest,
        layout=TokenizerStagingLayout((tmp_path / "tokenizers").resolve()),
        model_root=model_root.resolve(),
        artifact_id=uuid4(),
    )

    assert isinstance(result, TokenizerBundleBuilt)
    _assert_bundle_is_minimal(result)
    assert {entry.path for entry in result.manifest.files} == {
        "config.json",
        "special_tokens_map.json",
        "tokenizer.json",
    }
    assert all(path.is_file() and not path.is_symlink() for path in result.staging_path.iterdir())

    with pytest.raises(AssertionError):
        _assert_bundle_is_minimal(
            cast(
                TokenizerBundleBuilt,
                SimpleNamespace(
                    manifest=build_artifact_manifest(
                        (*result.manifest.files, _entry("evil.py", payloads["evil.py"]))
                    ),
                ),
            )
        )


def test_TC_RT_13_allowlisted_executable_is_rejected_instead_of_copied(tmp_path: Path) -> None:
    model_root = tmp_path / "models"
    source = model_root / ".staging" / str(uuid4())
    payloads = {"tokenizer.json": b"{}"}
    manifest = _source_manifest(source, payloads)
    (source / "tokenizer.json").chmod(0o755)
    result = build_managed_tokenizer_bundle(
        source,
        manifest,
        layout=TokenizerStagingLayout((tmp_path / "tokenizers").resolve()),
        model_root=model_root.resolve(),
        artifact_id=uuid4(),
    )
    assert result == TokenizerBundleRejected(TokenizerBundleRejectionReason.UNSAFE_FILE)


def test_TC_RT_13_allowlisted_escaping_symlink_reaches_nofollow_guard(tmp_path: Path) -> None:
    model_root = tmp_path / "models"
    source = model_root / ".staging" / str(uuid4())
    source.mkdir(parents=True)
    outside = tmp_path / "outside-tokenizer"
    outside.write_bytes(b"outside")
    (source / "tokenizer.json").symlink_to(outside)
    manifest = build_artifact_manifest((_entry("tokenizer.json", b"outside"),))
    arguments = {
        "layout": TokenizerStagingLayout((tmp_path / "tokenizers").resolve()),
        "model_root": model_root.resolve(),
        "artifact_id": uuid4(),
    }

    result = build_managed_tokenizer_bundle(source, manifest, **arguments)
    assert result == TokenizerBundleRejected(TokenizerBundleRejectionReason.UNSAFE_FILE)

    anchor = "source_descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)"
    mutated = load_mutated_module(
        BUNDLE_SOURCE,
        [(anchor, "source_descriptor = os.open(source, os.O_RDONLY)")],
        module_name="_m2_11_nofollow_mutation",
        destination=tmp_path / "mutated_nofollow.py",
    )
    mutated_result = mutated.build_managed_tokenizer_bundle(source, manifest, **arguments)
    with pytest.raises(AssertionError):
        assert isinstance(mutated_result, mutated.TokenizerBundleRejected)


def test_TC_RT_13_tokenizer_root_symlink_is_rejected_before_any_target_write(
    tmp_path: Path,
) -> None:
    # Every component but the last is already resolved, so the only symlink the
    # guard can trip over is ``tokenizer_root`` itself.  Resolving the configured
    # root here instead would hand the layout an ordinary directory and erase the
    # very property under test.
    root = tmp_path.resolve()
    model_root = root / "models"
    source = model_root / ".staging" / str(uuid4())
    manifest = _source_manifest(source, {"tokenizer.json": b"{}"})
    outside = root / "outside"
    outside.mkdir()
    tokenizer_root = root / "tokenizers"
    tokenizer_root.symlink_to(outside, target_is_directory=True)

    result = build_managed_tokenizer_bundle(
        source,
        manifest,
        layout=TokenizerStagingLayout(tokenizer_root),
        model_root=model_root,
        artifact_id=uuid4(),
    )

    assert result == TokenizerBundleRejected(TokenizerBundleRejectionReason.UNSAFE_PATH)
    assert tuple(outside.iterdir()) == ()


def test_TC_RT_13_empty_and_rejected_are_explicit_non_success_states(tmp_path: Path) -> None:
    empty = _build(tmp_path / "empty", {"model.safetensors": b"weights"})
    assert isinstance(empty, TokenizerBundleEmpty)

    other_root = Path("/dev/shm") / f"holm-M2-11-{uuid4()}"
    try:
        assert os.stat(tmp_path).st_dev != os.stat("/dev/shm").st_dev
        rejected = _build(
            tmp_path / "cross-device",
            {"tokenizer.json": b"{}"},
            tokenizer_root=other_root,
        )
        assert rejected == TokenizerBundleRejected(
            TokenizerBundleRejectionReason.FILESYSTEM_MISMATCH
        )
        assert not any(path.is_file() for path in other_root.rglob("*"))
        assert not isinstance(rejected, TokenizerBundleBuilt)
    finally:
        shutil.rmtree(other_root, ignore_errors=True)

    assert not isinstance(empty, TokenizerBundleBuilt)


class _FailBeforeReplace(ArtifactSystemCalls):
    def replace(self, source: Path, destination: Path) -> None:
        raise OSError("injected before replace")


class _FailAfterReplace(ArtifactSystemCalls):
    def fsync_directory(self, path: Path) -> None:
        if path.name == "tokenizers":
            raise OSError("injected after replace")
        super().fsync_directory(path)


@pytest.mark.parametrize("after_replace", [False, True], ids=["before-replace", "after-replace"])
def test_TC_RT_13_bundle_publication_failure_has_atomic_visibility(
    tmp_path: Path,
    after_replace: bool,
) -> None:
    result = _build(tmp_path, {"tokenizer.json": b"{}", "vocab.json": b'{"x":0}'})
    assert isinstance(result, TokenizerBundleBuilt)
    calls = _FailAfterReplace() if after_replace else _FailBeforeReplace()

    with pytest.raises(ArtifactPublicationError) as captured:
        publish_verified_artifact(
            result.staging_path,
            result.destination_path,
            result,
            system_calls=calls,
        )

    assert captured.value.renamed is after_replace
    assert result.destination_path.exists() is after_replace
    if after_replace:
        assert {path.name for path in result.destination_path.iterdir()} == {
            "manifest.json",
            "tokenizer.json",
            "vocab.json",
        }


@pytest.mark.parametrize("tamper", ["extra-code", "extra-symlink", "executable"])
def test_TC_RT_13_retry_rejects_non_exact_or_non_inert_published_bundle(
    tmp_path: Path,
    tamper: str,
) -> None:
    result = _build(tmp_path, {"tokenizer.json": b"{}"})
    assert isinstance(result, TokenizerBundleBuilt)
    publish_verified_artifact(result.staging_path, result.destination_path, result)
    if tamper == "extra-code":
        (result.destination_path / "evil.py").write_bytes(b"pass")
    elif tamper == "extra-symlink":
        outside = tmp_path / "outside-recovery"
        outside.write_bytes(b"outside")
        (result.destination_path / "extra-link").symlink_to(outside)
    else:
        (result.destination_path / "tokenizer.json").chmod(0o755)

    with pytest.raises(TokenizerBundleRecoveryError):
        recover_tokenizer_bundle(result.destination_path)


class _LeaseLostProbe(RuntimeError):
    pass


def test_TC_RT_13_lease_loss_stops_copy_without_deleting_new_owner_staging(
    tmp_path: Path,
) -> None:
    model_root = tmp_path / "models"
    source = model_root / ".staging" / str(uuid4())
    manifest = _source_manifest(source, {"tokenizer.json": b"x" * (1024 * 1024 + 1)})
    artifact_id = uuid4()
    layout = TokenizerStagingLayout((tmp_path / "tokenizers").resolve())
    calls = 0

    def ensure_owned() -> None:
        nonlocal calls
        calls += 1
        if calls == 6:
            staging = layout.staging_path(artifact_id)
            shutil.rmtree(staging)
            staging.mkdir()
            (staging / "new-owner-marker").write_bytes(b"owned by replacement")
            raise _LeaseLostProbe

    with pytest.raises(_LeaseLostProbe):
        build_managed_tokenizer_bundle(
            source,
            manifest,
            layout=layout,
            model_root=model_root.resolve(),
            artifact_id=artifact_id,
            ensure_owned=ensure_owned,
        )

    assert calls == 6
    assert (layout.staging_path(artifact_id) / "new-owner-marker").read_bytes() == (
        b"owned by replacement"
    )


def test_TC_RT_13_positive_control_mutates_real_allowlist_and_fails_policy(
    tmp_path: Path,
) -> None:
    anchor = '        "vocab.txt",\n'
    mutated = load_mutated_module(
        BUNDLE_SOURCE,
        [(anchor, anchor + '        "model.safetensors",\n')],
        module_name="_m2_11_allowlist_mutation",
        destination=tmp_path / "mutated_tokenizer_bundle.py",
    )
    model_root = tmp_path / "models"
    source = model_root / ".staging" / str(uuid4())
    payloads = {"tokenizer.json": b"{}", "model.safetensors": b"weights"}
    manifest = _source_manifest(source, payloads)
    result = mutated.build_managed_tokenizer_bundle(
        source,
        manifest,
        layout=TokenizerStagingLayout((tmp_path / "tokenizers").resolve()),
        model_root=model_root.resolve(),
        artifact_id=uuid4(),
    )
    assert isinstance(result, mutated.TokenizerBundleBuilt)
    with pytest.raises(AssertionError):
        _assert_bundle_is_minimal(result)


def test_TC_RT_13_positive_control_mutates_allowlist_logic_and_fails_policy(
    tmp_path: Path,
) -> None:
    mutated = load_mutated_module(
        BUNDLE_SOURCE,
        [
            (
                "entry.path in TOKENIZER_BUNDLE_ALLOWLIST",
                "entry.path not in TOKENIZER_BUNDLE_ALLOWLIST",
            ),
            (
                "if not paths <= TOKENIZER_BUNDLE_ALLOWLIST:",
                "if False:",
            ),
        ],
        module_name="_m2_11_allowlist_logic_mutation",
        destination=tmp_path / "mutated_tokenizer_logic.py",
    )
    model_root = tmp_path / "models"
    source = model_root / ".staging" / str(uuid4())
    payloads = {"tokenizer.json": b"{}", "evil.py": b"pass"}
    manifest = _source_manifest(source, payloads)
    result = mutated.build_managed_tokenizer_bundle(
        source,
        manifest,
        layout=TokenizerStagingLayout((tmp_path / "tokenizers").resolve()),
        model_root=model_root.resolve(),
        artifact_id=uuid4(),
    )
    assert isinstance(result, mutated.TokenizerBundleBuilt)
    with pytest.raises(AssertionError):
        _assert_bundle_is_minimal(result)
