"""Read-only, inert loading and counting for a published Tokenizer Bundle.

Level 3 metering counts Tokens inside the Gateway from the minimal Tokenizer
Bundle that the download Worker published beside the Artifact.  Three
constraints shape this module:

* **Nothing in a Bundle is executed.**  Only JSON is parsed; ``trust_remote_code``
  has no analogue here because no Python, dynamic library, or template engine is
  ever loaded.  A Bundle describing a tokenizer this module cannot reproduce
  exactly is rejected, never approximated.
* **The Bundle is verified before use.**  The Manifest Hash recorded on the
  Artifact must match the Bundle's own ``manifest.json``, the published file set
  must equal the Manifest exactly, and every file's SHA-256 is recomputed.
* **No request body ever reaches this module's outputs.**  It returns integers.

The tokenizer itself is a byte-level BPE reproduced in pure Python, because the
platform adds no production dependency for metering.  Reproducing a tokenizer
approximately would silently corrupt Usage, so the loader accepts only the
certified configuration shape (NFC normalizer, the Split+ByteLevel
pre-tokenizer with a known pattern, and a plain BPE model) and rejects anything
else as ``UNSUPPORTED``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from enum import StrEnum
from functools import lru_cache
from heapq import heappop, heappush
from pathlib import Path
from typing import Final

from holm_domain import (
    ARTIFACT_MANIFEST_FILENAME,
    ArtifactManifest,
    ArtifactManifestError,
    artifact_manifest_hash,
    deserialize_artifact_manifest,
)

_READ_CHUNK_BYTES: Final[int] = 1024 * 1024
_MAX_BUNDLE_FILE_BYTES: Final[int] = 64 * 1024 * 1024
_MAX_MANIFEST_BYTES: Final[int] = 1024 * 1024
_TOKENIZER_FILENAME: Final[str] = "tokenizer.json"

# The platform-level Tokenizer Bundle Allowlist.  The download Worker owns the
# publishing copy ( `holm_download_worker.tokenizer_bundle`); this
# reader cannot import it because `apps` never depend on `apps`.  A unit test
# (`test_M5_07_tokenizer_bundle`) fails loudly if the two ever diverge, so this
# stays a mirror rather than a second policy.
TOKENIZER_BUNDLE_ALLOWLIST: Final[frozenset[str]] = frozenset(
    {
        "added_tokens.json",
        "config.json",
        "merges.txt",
        "sentencepiece.bpe.model",
        "special_tokens_map.json",
        "spiece.model",
        "tokenizer.json",
        "tokenizer.model",
        "tokenizer.vocab",
        "tokenizer_config.json",
        "vocab.json",
        "vocab.txt",
    }
)

# Accepted pre-tokenizer split patterns.  The value is the maximum length of a
# digit run that the pattern keeps together, which is the only difference
# between the two certified spellings.
_ACCEPTED_SPLIT_PATTERNS: Final[Mapping[str, int]] = {
    (
        r"(?i:'s|'t|'re|'ve|'m|'ll|'d)|[^\r\n\p{L}\p{N}]?\p{L}+|\p{N}|"
        r" ?[^\s\p{L}\p{N}]+[\r\n]*|\s*[\r\n]+|\s+(?!\S)|\s+"
    ): 1,
    (
        r"(?i:'s|'t|'re|'ve|'m|'ll|'d)|[^\r\n\p{L}\p{N}]?\p{L}+|\p{N}{1,3}|"
        r" ?[^\s\p{L}\p{N}]+[\r\n]*|\s*[\r\n]+|\s+(?!\S)|\s+"
    ): 3,
}

# Per-entry memory estimates for the LRU budget.  CPython gives no cheap exact
# footprint for a large dict of small strings, so the cache accounts a loaded
# Bundle with these measured-order constants rather than pretending the on-disk
# size is the in-memory size.  They are deliberately generous: under-counting
# would let the cache exceed its configured ceiling.
_VOCAB_ENTRY_BYTES: Final[int] = 120
_MERGE_ENTRY_BYTES: Final[int] = 160
_SPECIAL_ENTRY_BYTES: Final[int] = 200


class TokenizerBundleReason(StrEnum):
    """Why a Bundle cannot be used, as a stable code that carries no path."""

    NOT_PUBLISHED = "not_published"
    EXTERNAL_ARTIFACT = "external_artifact"
    PATH_UNSAFE = "path_unsafe"
    FILE_SET_MISMATCH = "file_set_mismatch"
    HASH_MISMATCH = "hash_mismatch"
    UNREADABLE = "unreadable"
    UNSUPPORTED_TOKENIZER = "unsupported_tokenizer"
    #: The Bundle itself is fine; this Gateway had no bounded worker capacity
    #: for it in time, or the pool was already shutting down.
    WORKER_SATURATED = "worker_saturated"


class TokenizerBundleUnusable(RuntimeError):
    """A Bundle is absent, unverifiable, or not reproducible without guessing."""

    def __init__(self, reason: TokenizerBundleReason) -> None:
        self.reason = reason
        super().__init__("Tokenizer Bundle unusable")


def _is_letter(character: str) -> bool:
    return unicodedata.category(character)[0] == "L"


def _is_number(character: str) -> bool:
    return unicodedata.category(character)[0] == "N"


# ``str.isspace`` additionally accepts the C0 information separators, which the
# regex engines behind the reference tokenizer do not treat as ``\s``.
_NON_REGEX_SPACE: Final[frozenset[str]] = frozenset("\x1c\x1d\x1e\x1f")


def _is_space(character: str) -> bool:
    return character.isspace() and character not in _NON_REGEX_SPACE


_CONTRACTIONS: Final[tuple[str, ...]] = ("re", "ve", "ll", "s", "t", "m", "d")


def _pre_tokenize(text: str, *, max_digit_run: int) -> list[str]:
    """Reproduce the certified Split pattern with explicit Unicode predicates.

    Python's ``re`` has no ``\\p{L}``/``\\p{N}``, and the platform adds no regex
    dependency for metering, so the alternation is evaluated directly.  Order
    and greediness follow leftmost-first (Perl-style) semantics, matching the
    engine used by the reference implementation.
    """

    pieces: list[str] = []
    index = 0
    length = len(text)
    gap_start = 0
    while index < length:
        end = _match_alternation(text, index, length, max_digit_run=max_digit_run)
        if end is None:
            index += 1
            continue
        if gap_start < index:
            pieces.append(text[gap_start:index])
        pieces.append(text[index:end])
        index = end
        gap_start = index
    if gap_start < length:
        pieces.append(text[gap_start:length])
    return pieces


def _match_alternation(text: str, start: int, length: int, *, max_digit_run: int) -> int | None:
    character = text[start]

    # 1. (?i:'s|'t|'re|'ve|'m|'ll|'d)
    if character == "'":
        for suffix in _CONTRACTIONS:
            end = start + 1 + len(suffix)
            if end <= length and text[start + 1 : end].lower() == suffix:
                return end

    # 2. [^\r\n\p{L}\p{N}]?\p{L}+
    cursor = start
    if (
        character not in "\r\n"
        and not _is_letter(character)
        and not _is_number(character)
        and start + 1 < length
        and _is_letter(text[start + 1])
    ):
        cursor = start + 1
    if cursor < length and _is_letter(text[cursor]):
        while cursor < length and _is_letter(text[cursor]):
            cursor += 1
        return cursor

    # 3. \p{N} (optionally a bounded run)
    if _is_number(character):
        cursor = start
        while cursor < length and cursor - start < max_digit_run and _is_number(text[cursor]):
            cursor += 1
        return cursor

    # 4. ' ?[^\s\p{L}\p{N}]+[\r\n]*'
    cursor = start
    if character == " " and start + 1 < length:
        candidate = text[start + 1]
        if not _is_space(candidate) and not _is_letter(candidate) and not _is_number(candidate):
            cursor = start + 1
    if (
        cursor < length
        and not _is_space(text[cursor])
        and not _is_letter(text[cursor])
        and not _is_number(text[cursor])
    ):
        while (
            cursor < length
            and not _is_space(text[cursor])
            and not _is_letter(text[cursor])
            and not _is_number(text[cursor])
        ):
            cursor += 1
        while cursor < length and text[cursor] in "\r\n":
            cursor += 1
        return cursor

    if not _is_space(character):
        return None

    run_end = start
    while run_end < length and _is_space(text[run_end]):
        run_end += 1

    # 5. '\s*[\r\n]+' - greedy \s* backtracks to the last line break in the run.
    last_break = -1
    for position in range(run_end - 1, start - 1, -1):
        if text[position] in "\r\n":
            last_break = position
            break
    if last_break >= 0:
        return last_break + 1

    # 6. '\s+(?!\S)' - the whole run when it ends the text, otherwise one short.
    if run_end == length:
        return run_end
    if run_end - start >= 2:
        return run_end - 1

    # 7. '\s+'
    return run_end


@lru_cache(maxsize=1)
def _byte_encoder() -> Mapping[int, str]:
    """The GPT-2 byte-to-unicode table used by every ByteLevel tokenizer."""

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
    return dict(zip(printable, (chr(item) for item in mapped), strict=True))


class BundleTokenizer:
    """Count Tokens for in-memory text with a verified, inert Bundle."""

    __slots__ = (
        "_added_pattern",
        "_added_tokens",
        "_estimated_bytes",
        "_max_digit_run",
        "_merge_ranks",
        "_normalize_nfc",
        "_vocab",
    )

    def __init__(
        self,
        *,
        vocab: Mapping[str, int],
        merge_ranks: Mapping[tuple[str, str], int],
        added_tokens: Sequence[str],
        normalize_nfc: bool,
        max_digit_run: int,
    ) -> None:
        self._vocab = vocab
        self._merge_ranks = merge_ranks
        self._added_tokens = tuple(added_tokens)
        self._normalize_nfc = normalize_nfc
        self._max_digit_run = max_digit_run
        self._added_pattern = (
            re.compile("|".join(re.escape(token) for token in sorted(added_tokens, key=len)[::-1]))
            if added_tokens
            else None
        )
        self._estimated_bytes = (
            len(vocab) * _VOCAB_ENTRY_BYTES
            + len(merge_ranks) * _MERGE_ENTRY_BYTES
            + len(self._added_tokens) * _SPECIAL_ENTRY_BYTES
        )

    @property
    def estimated_bytes(self) -> int:
        return self._estimated_bytes

    def count(self, text: str) -> int:
        """Return the Token count of ``text``; the text itself is never retained."""

        return self.count_all((text,))

    def count_all(self, texts: Iterable[str]) -> int:
        """Count one request's texts together, sharing a call-local merge cache.

        The cache is deliberately call-local.  A process-wide cache keyed by
        encoded fragments would retain pieces of request bodies after the
        request that produced them had finished, which chapter 5 does not allow
        even in memory.
        """

        cache: dict[str, int] = {}
        return sum(self._count_text(text, cache) for text in texts)

    def _count_text(self, text: str, cache: dict[str, int]) -> int:
        if not text:
            return 0
        total = 0
        for segment, is_added in self._split_added(text):
            if is_added:
                total += 1
                continue
            prepared = unicodedata.normalize("NFC", segment) if self._normalize_nfc else segment
            for piece in _pre_tokenize(prepared, max_digit_run=self._max_digit_run):
                total += self._count_piece(piece, cache)
        return total

    def _split_added(self, text: str) -> list[tuple[str, bool]]:
        pattern = self._added_pattern
        if pattern is None:
            return [(text, False)]
        segments: list[tuple[str, bool]] = []
        position = 0
        for match in pattern.finditer(text):
            if match.start() > position:
                segments.append((text[position : match.start()], False))
            segments.append((match.group(), True))
            position = match.end()
        if position < len(text):
            segments.append((text[position:], False))
        return segments

    def _count_piece(self, piece: str, cache: dict[str, int]) -> int:
        encoder = _byte_encoder()
        encoded = "".join(encoder[byte] for byte in piece.encode("utf-8"))
        cached = cache.get(encoded)
        if cached is not None:
            return cached
        count = self._merge(encoded)
        cache[encoded] = count
        return count

    def _merge(self, encoded: str) -> int:
        """Return how many Tokens ``encoded`` reduces to under the merge table.

        Merges are applied lowest rank first, leftmost on a tie, which is what
        the reference implementation's ``min`` over adjacent pairs does.  The
        pending pairs live in a heap rather than being rescanned each round: a
        linear rescan is quadratic in the piece length, and a single very long
        piece (a separator line, a blob) would then pin a metering worker for
        minutes.  Stale heap entries are discarded on pop.
        """

        symbols = list(encoded)
        length = len(symbols)
        if length < 2:
            return length
        following = list(range(1, length + 1))
        preceding = list(range(-1, length - 1))
        alive = [True] * length
        pending: list[tuple[int, int, int]] = []
        for index in range(length - 1):
            rank = self._merge_ranks.get((symbols[index], symbols[index + 1]))
            if rank is not None:
                heappush(pending, (rank, index, index + 1))
        total = length
        while pending:
            rank, left, right = heappop(pending)
            if not alive[left] or not alive[right] or following[left] != right:
                continue
            if self._merge_ranks.get((symbols[left], symbols[right])) != rank:
                continue
            symbols[left] = symbols[left] + symbols[right]
            alive[right] = False
            total -= 1
            after = following[right]
            following[left] = after
            if after < length:
                preceding[after] = left
                merged = self._merge_ranks.get((symbols[left], symbols[after]))
                if merged is not None:
                    heappush(pending, (merged, left, after))
            before = preceding[left]
            if before >= 0:
                merged = self._merge_ranks.get((symbols[before], symbols[left]))
                if merged is not None:
                    heappush(pending, (merged, before, left))
        return total


def _open_inert(path: Path) -> int:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as error:
        raise TokenizerBundleUnusable(TokenizerBundleReason.UNREADABLE) from error
    try:
        metadata = os.fstat(descriptor)
    except OSError as error:
        os.close(descriptor)
        raise TokenizerBundleUnusable(TokenizerBundleReason.UNREADABLE) from error
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o111:
        os.close(descriptor)
        raise TokenizerBundleUnusable(TokenizerBundleReason.PATH_UNSAFE)
    return descriptor


def _read_verified(path: Path, *, expected_size: int, expected_sha256: str) -> bytes:
    """Read one Bundle file while proving it is the exact Manifest entry."""

    if expected_size > _MAX_BUNDLE_FILE_BYTES:
        raise TokenizerBundleUnusable(TokenizerBundleReason.FILE_SET_MISMATCH)
    descriptor = _open_inert(path)
    digest = hashlib.sha256()
    chunks: list[bytes] = []
    total = 0
    try:
        while chunk := os.read(descriptor, _READ_CHUNK_BYTES):
            total += len(chunk)
            if total > expected_size:
                raise TokenizerBundleUnusable(TokenizerBundleReason.HASH_MISMATCH)
            digest.update(chunk)
            chunks.append(chunk)
    except OSError as error:
        raise TokenizerBundleUnusable(TokenizerBundleReason.UNREADABLE) from error
    finally:
        os.close(descriptor)
    if total != expected_size or digest.hexdigest() != expected_sha256:
        raise TokenizerBundleUnusable(TokenizerBundleReason.HASH_MISMATCH)
    return b"".join(chunks)


def _hash_only(path: Path, *, expected_size: int, expected_sha256: str) -> None:
    descriptor = _open_inert(path)
    digest = hashlib.sha256()
    total = 0
    try:
        while chunk := os.read(descriptor, _READ_CHUNK_BYTES):
            total += len(chunk)
            digest.update(chunk)
    except OSError as error:
        raise TokenizerBundleUnusable(TokenizerBundleReason.UNREADABLE) from error
    finally:
        os.close(descriptor)
    if total != expected_size or digest.hexdigest() != expected_sha256:
        raise TokenizerBundleUnusable(TokenizerBundleReason.HASH_MISMATCH)


def _verified_manifest(path: Path, expected_manifest_hash: str) -> ArtifactManifest:
    manifest_path = path / ARTIFACT_MANIFEST_FILENAME
    descriptor = _open_inert(manifest_path)
    chunks: list[bytes] = []
    total = 0
    try:
        while chunk := os.read(descriptor, _READ_CHUNK_BYTES):
            total += len(chunk)
            if total > _MAX_MANIFEST_BYTES:
                raise TokenizerBundleUnusable(TokenizerBundleReason.FILE_SET_MISMATCH)
            chunks.append(chunk)
    except OSError as error:
        raise TokenizerBundleUnusable(TokenizerBundleReason.UNREADABLE) from error
    finally:
        os.close(descriptor)
    raw = b"".join(chunks)
    if artifact_manifest_hash(raw) != expected_manifest_hash:
        raise TokenizerBundleUnusable(TokenizerBundleReason.HASH_MISMATCH)
    try:
        manifest = deserialize_artifact_manifest(raw)
    except ArtifactManifestError as error:
        raise TokenizerBundleUnusable(TokenizerBundleReason.FILE_SET_MISMATCH) from error
    names = {entry.path for entry in manifest.files}
    if not names or not names <= TOKENIZER_BUNDLE_ALLOWLIST:
        raise TokenizerBundleUnusable(TokenizerBundleReason.FILE_SET_MISMATCH)
    try:
        published = {entry.name for entry in os.scandir(path)}
    except OSError as error:
        raise TokenizerBundleUnusable(TokenizerBundleReason.UNREADABLE) from error
    if published != names | {ARTIFACT_MANIFEST_FILENAME}:
        raise TokenizerBundleUnusable(TokenizerBundleReason.FILE_SET_MISMATCH)
    return manifest


def load_bundle_tokenizer(path: Path, expected_manifest_hash: str) -> BundleTokenizer:
    """Load one published Bundle after proving it is exactly what was published.

    This is blocking, CPU-bound work; callers must run it on a bounded worker
    thread so no inference Event Loop ever waits for it.
    """

    if not path.is_absolute():
        raise TokenizerBundleUnusable(TokenizerBundleReason.PATH_UNSAFE)
    try:
        root = path.lstat()
    except OSError as error:
        raise TokenizerBundleUnusable(TokenizerBundleReason.UNREADABLE) from error
    if not stat.S_ISDIR(root.st_mode):
        raise TokenizerBundleUnusable(TokenizerBundleReason.PATH_UNSAFE)

    manifest = _verified_manifest(path, expected_manifest_hash)
    entries = {entry.path: entry for entry in manifest.files}
    tokenizer_entry = entries.get(_TOKENIZER_FILENAME)
    if tokenizer_entry is None:
        raise TokenizerBundleUnusable(TokenizerBundleReason.UNSUPPORTED_TOKENIZER)
    raw = _read_verified(
        path / _TOKENIZER_FILENAME,
        expected_size=tokenizer_entry.size_bytes,
        expected_sha256=tokenizer_entry.sha256,
    )
    for name, entry in entries.items():
        if name == _TOKENIZER_FILENAME:
            continue
        _hash_only(path / name, expected_size=entry.size_bytes, expected_sha256=entry.sha256)
    return _build_tokenizer(raw)


def _build_tokenizer(raw: bytes) -> BundleTokenizer:
    try:
        document = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise TokenizerBundleUnusable(TokenizerBundleReason.UNSUPPORTED_TOKENIZER) from error
    if not isinstance(document, dict):
        raise TokenizerBundleUnusable(TokenizerBundleReason.UNSUPPORTED_TOKENIZER)
    if document.get("truncation") is not None or document.get("padding") is not None:
        raise TokenizerBundleUnusable(TokenizerBundleReason.UNSUPPORTED_TOKENIZER)

    normalizer = document.get("normalizer")
    if normalizer is None:
        normalize_nfc = False
    elif isinstance(normalizer, dict) and normalizer.get("type") == "NFC":
        normalize_nfc = True
    else:
        raise TokenizerBundleUnusable(TokenizerBundleReason.UNSUPPORTED_TOKENIZER)

    max_digit_run = _split_behaviour(document.get("pre_tokenizer"))
    model = document.get("model")
    if not isinstance(model, dict) or model.get("type") != "BPE":
        raise TokenizerBundleUnusable(TokenizerBundleReason.UNSUPPORTED_TOKENIZER)
    if (
        model.get("dropout") is not None
        or model.get("unk_token") is not None
        or model.get("continuing_subword_prefix") not in (None, "")
        or model.get("end_of_word_suffix") not in (None, "")
        or model.get("byte_fallback") is True
        or model.get("fuse_unk") is True
    ):
        raise TokenizerBundleUnusable(TokenizerBundleReason.UNSUPPORTED_TOKENIZER)

    vocab = model.get("vocab")
    if not isinstance(vocab, dict) or not vocab:
        raise TokenizerBundleUnusable(TokenizerBundleReason.UNSUPPORTED_TOKENIZER)
    merges = model.get("merges")
    if not isinstance(merges, list) or not merges:
        raise TokenizerBundleUnusable(TokenizerBundleReason.UNSUPPORTED_TOKENIZER)

    ranks: dict[tuple[str, str], int] = {}
    for rank, item in enumerate(merges):
        pair = _merge_pair(item)
        ranks.setdefault(pair, rank)
    added = _added_tokens(document.get("added_tokens"))
    encoder = _byte_encoder()
    if any(encoder[byte] not in vocab for byte in range(256)):
        # Without every byte symbol in the vocabulary a merge can terminate on
        # an unknown symbol, and the count would silently become an estimate.
        raise TokenizerBundleUnusable(TokenizerBundleReason.UNSUPPORTED_TOKENIZER)
    return BundleTokenizer(
        vocab={key: value for key, value in vocab.items() if isinstance(key, str)},
        merge_ranks=ranks,
        added_tokens=added,
        normalize_nfc=normalize_nfc,
        max_digit_run=max_digit_run,
    )


def _merge_pair(item: object) -> tuple[str, str]:
    if isinstance(item, str):
        left, separator, right = item.partition(" ")
        if not separator or not left or not right:
            raise TokenizerBundleUnusable(TokenizerBundleReason.UNSUPPORTED_TOKENIZER)
        return left, right
    if (
        isinstance(item, list)
        and len(item) == 2
        and all(isinstance(part, str) and part for part in item)
    ):
        return item[0], item[1]
    raise TokenizerBundleUnusable(TokenizerBundleReason.UNSUPPORTED_TOKENIZER)


def _added_tokens(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise TokenizerBundleUnusable(TokenizerBundleReason.UNSUPPORTED_TOKENIZER)
    contents: list[str] = []
    for item in value:
        if not isinstance(item, dict) or not isinstance(item.get("content"), str):
            raise TokenizerBundleUnusable(TokenizerBundleReason.UNSUPPORTED_TOKENIZER)
        if item.get("lstrip") or item.get("rstrip") or item.get("normalized"):
            # Stripping and normalized added Tokens change surrounding text; the
            # platform refuses to guess at that rather than mis-count.
            raise TokenizerBundleUnusable(TokenizerBundleReason.UNSUPPORTED_TOKENIZER)
        contents.append(item["content"])
    return tuple(contents)


def _split_behaviour(value: object) -> int:
    if not isinstance(value, dict) or value.get("type") != "Sequence":
        raise TokenizerBundleUnusable(TokenizerBundleReason.UNSUPPORTED_TOKENIZER)
    stages = value.get("pretokenizers")
    if not isinstance(stages, list) or len(stages) != 2:
        raise TokenizerBundleUnusable(TokenizerBundleReason.UNSUPPORTED_TOKENIZER)
    split, byte_level = stages
    if (
        not isinstance(split, dict)
        or split.get("type") != "Split"
        or split.get("behavior") != "Isolated"
        or split.get("invert") is True
    ):
        raise TokenizerBundleUnusable(TokenizerBundleReason.UNSUPPORTED_TOKENIZER)
    pattern = split.get("pattern")
    if not isinstance(pattern, dict) or not isinstance(pattern.get("Regex"), str):
        raise TokenizerBundleUnusable(TokenizerBundleReason.UNSUPPORTED_TOKENIZER)
    max_digit_run = _ACCEPTED_SPLIT_PATTERNS.get(pattern["Regex"])
    if max_digit_run is None:
        raise TokenizerBundleUnusable(TokenizerBundleReason.UNSUPPORTED_TOKENIZER)
    if (
        not isinstance(byte_level, dict)
        or byte_level.get("type") != "ByteLevel"
        or byte_level.get("add_prefix_space") is True
        or byte_level.get("use_regex") is True
    ):
        raise TokenizerBundleUnusable(TokenizerBundleReason.UNSUPPORTED_TOKENIZER)
    return max_digit_run


__all__ = [
    "BundleTokenizer",
    "TokenizerBundleReason",
    "TokenizerBundleUnusable",
    "load_bundle_tokenizer",
]
