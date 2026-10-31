"""M5-07: read-only Tokenizer Bundle loading, verification, and counting."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Final

import pytest
from holm_domain import (
    ARTIFACT_MANIFEST_FILENAME,
    ArtifactManifestEntry,
    artifact_manifest_hash,
    build_artifact_manifest,
    serialize_artifact_manifest,
)
from holm_download_worker import TOKENIZER_BUNDLE_ALLOWLIST as PUBLISHER_ALLOWLIST
from holm_gateway.tokenizer_bundle import (
    TOKENIZER_BUNDLE_ALLOWLIST,
    TokenizerBundleReason,
    TokenizerBundleUnusable,
    load_bundle_tokenizer,
)

_QWEN_SPLIT_PATTERN: Final[str] = (
    r"(?i:'s|'t|'re|'ve|'m|'ll|'d)|[^\r\n\p{L}\p{N}]?\p{L}+|\p{N}|"
    r" ?[^\s\p{L}\p{N}]+[\r\n]*|\s*[\r\n]+|\s+(?!\S)|\s+"
)


def _byte_symbols() -> list[str]:
    printable = (
        list(range(ord("!"), ord("~") + 1))
        + list(range(ord("\xa1"), ord("\xac") + 1))
        + list(range(ord("\xae"), ord("\xff") + 1))
    )
    mapped = list(printable)
    offset = 0
    for value in range(256):
        if value in printable:
            continue
        printable.append(value)
        mapped.append(256 + offset)
        offset += 1
    return [chr(item) for _, item in sorted(zip(printable, mapped, strict=True))]


def _tokenizer_document(
    *,
    merges: tuple[str, ...],
    added: tuple[str, ...] = (),
    model_type: str = "BPE",
    pattern: str = _QWEN_SPLIT_PATTERN,
) -> dict[str, Any]:
    vocab: dict[str, int] = {symbol: index for index, symbol in enumerate(_byte_symbols())}
    for merge in merges:
        left, right = merge.split(" ")
        vocab.setdefault(left + right, len(vocab))
    for index, content in enumerate(added):
        vocab.setdefault(content, 100_000 + index)
    return {
        "version": "1.0",
        "truncation": None,
        "padding": None,
        "added_tokens": [
            {
                "id": 100_000 + index,
                "content": content,
                "single_word": False,
                "lstrip": False,
                "rstrip": False,
                "normalized": False,
                "special": True,
            }
            for index, content in enumerate(added)
        ],
        "normalizer": {"type": "NFC"},
        "pre_tokenizer": {
            "type": "Sequence",
            "pretokenizers": [
                {
                    "type": "Split",
                    "pattern": {"Regex": pattern},
                    "behavior": "Isolated",
                    "invert": False,
                },
                {
                    "type": "ByteLevel",
                    "add_prefix_space": False,
                    "trim_offsets": False,
                    "use_regex": False,
                },
            ],
        },
        "post_processor": {"type": "ByteLevel"},
        "decoder": {"type": "ByteLevel"},
        "model": {
            "type": model_type,
            "dropout": None,
            "unk_token": None,
            "continuing_subword_prefix": "",
            "end_of_word_suffix": "",
            "fuse_unk": False,
            "byte_fallback": False,
            "vocab": vocab,
            "merges": list(merges),
        },
    }


def _publish(root: Path, files: dict[str, bytes]) -> str:
    root.mkdir(parents=True, exist_ok=True)
    entries = []
    for name, payload in files.items():
        (root / name).write_bytes(payload)
        entries.append(
            ArtifactManifestEntry(name, len(payload), hashlib.sha256(payload).hexdigest())
        )
    manifest_bytes = serialize_artifact_manifest(build_artifact_manifest(entries))
    (root / ARTIFACT_MANIFEST_FILENAME).write_bytes(manifest_bytes)
    return artifact_manifest_hash(manifest_bytes)


def _bundle(
    tmp_path: Path,
    *,
    merges: tuple[str, ...] = ("h e", "he l", "hel l", "hell o"),
    added: tuple[str, ...] = (),
    extra: dict[str, bytes] | None = None,
    model_type: str = "BPE",
    pattern: str = _QWEN_SPLIT_PATTERN,
) -> tuple[Path, str]:
    root = tmp_path / "bundle"
    document = _tokenizer_document(
        merges=merges, added=added, model_type=model_type, pattern=pattern
    )
    files = {"tokenizer.json": json.dumps(document).encode("utf-8")}
    files.update(extra or {})
    return root, _publish(root, files)


def test_M5_07_gateway_allowlist_mirrors_the_publisher_allowlist() -> None:
    # `apps` never import `apps`, so the Gateway keeps a mirror of the download
    # Worker's Allowlist.  This assertion is the only thing keeping the two from
    # drifting into two different policies.
    assert TOKENIZER_BUNDLE_ALLOWLIST == PUBLISHER_ALLOWLIST


def test_M5_07_counts_follow_the_published_merge_table(tmp_path: Path) -> None:
    root, manifest_hash = _bundle(tmp_path)
    tokenizer = load_bundle_tokenizer(root, manifest_hash)

    # "hello" merges down to one Token; " world" has no merges, so it stays one
    # Token per byte symbol including the leading-space marker.
    assert tokenizer.count("hello") == 1
    assert tokenizer.count(" world") == 6
    assert tokenizer.count("hello world") == 7
    assert tokenizer.count("") == 0
    assert tokenizer.count_all(("hello", "hello")) == 2


def test_M5_07_added_tokens_count_as_one_token(tmp_path: Path) -> None:
    root, manifest_hash = _bundle(tmp_path, added=("<|im_end|>",))
    tokenizer = load_bundle_tokenizer(root, manifest_hash)

    assert tokenizer.count("<|im_end|>") == 1
    assert tokenizer.count("hello<|im_end|>hello") == 3


def test_M5_07_manifest_hash_mismatch_is_rejected(tmp_path: Path) -> None:
    root, _ = _bundle(tmp_path)
    with pytest.raises(TokenizerBundleUnusable) as raised:
        load_bundle_tokenizer(root, "0" * 64)
    assert raised.value.reason is TokenizerBundleReason.HASH_MISMATCH


def test_M5_07_tampered_bundle_content_is_rejected(tmp_path: Path) -> None:
    root, manifest_hash = _bundle(tmp_path)
    (root / "tokenizer.json").write_bytes(b'{"model": {"type": "BPE"}}')
    with pytest.raises(TokenizerBundleUnusable) as raised:
        load_bundle_tokenizer(root, manifest_hash)
    assert raised.value.reason is TokenizerBundleReason.HASH_MISMATCH


def test_M5_07_unexpected_published_file_is_rejected(tmp_path: Path) -> None:
    root, manifest_hash = _bundle(tmp_path)
    (root / "vocab.json").write_bytes(b"{}")
    with pytest.raises(TokenizerBundleUnusable) as raised:
        load_bundle_tokenizer(root, manifest_hash)
    assert raised.value.reason is TokenizerBundleReason.FILE_SET_MISMATCH


def test_M5_07_non_allowlisted_manifest_entry_is_rejected(tmp_path: Path) -> None:
    root, manifest_hash = _bundle(tmp_path, extra={"model.safetensors": b"weights"})
    with pytest.raises(TokenizerBundleUnusable) as raised:
        load_bundle_tokenizer(root, manifest_hash)
    assert raised.value.reason is TokenizerBundleReason.FILE_SET_MISMATCH


def test_M5_07_absent_bundle_directory_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(TokenizerBundleUnusable) as raised:
        load_bundle_tokenizer(tmp_path / "absent", "0" * 64)
    assert raised.value.reason is TokenizerBundleReason.UNREADABLE


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"model_type": "WordPiece"}, TokenizerBundleReason.UNSUPPORTED_TOKENIZER),
        ({"pattern": r"\w+"}, TokenizerBundleReason.UNSUPPORTED_TOKENIZER),
    ],
)
def test_M5_07_unreproducible_tokenizer_shape_is_rejected_not_approximated(
    tmp_path: Path, kwargs: dict[str, Any], reason: TokenizerBundleReason
) -> None:
    root, manifest_hash = _bundle(tmp_path, **kwargs)
    with pytest.raises(TokenizerBundleUnusable) as raised:
        load_bundle_tokenizer(root, manifest_hash)
    assert raised.value.reason is reason


def test_M5_07_executable_bundle_file_is_rejected(tmp_path: Path) -> None:
    root, manifest_hash = _bundle(tmp_path)
    (root / "tokenizer.json").chmod(0o750)
    with pytest.raises(TokenizerBundleUnusable) as raised:
        load_bundle_tokenizer(root, manifest_hash)
    assert raised.value.reason is TokenizerBundleReason.PATH_UNSAFE


def test_M5_07_pre_tokenizer_reproduces_the_certified_splits(tmp_path: Path) -> None:
    root, manifest_hash = _bundle(tmp_path, merges=("~ ~",))
    tokenizer = load_bundle_tokenizer(root, manifest_hash)

    # The only merge pair ("~~") appears in none of these strings, so every byte
    # is one Token, so the count is the UTF-8 length
    # of each pre-tokenized piece.  These strings exercise the contraction,
    # letter, digit, symbol, line-break, and trailing-space alternatives.
    for text in ("don't", "a1b2", " !!!\n\n", "  ", "你好", "a\r\nb", "x  "):
        assert tokenizer.count(text) == len(text.encode("utf-8")), text
