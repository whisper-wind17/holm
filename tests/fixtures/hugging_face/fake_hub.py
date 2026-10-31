"""A local Hugging Face Hub that replays the FX-MS-08 recordings.

Why a server and not a stub: the development container cannot reach
``huggingface.co`` (``recorded/PROVENANCE.md``), but the thing under test is the
real ``huggingface_hub`` SDK talking to a Hub.  Patching SDK internals would
test the patch instead of the Adapter, so the SDK runs unmodified and
``HF_ENDPOINT`` (an address, not a credential) points it here.

What is replayed byte for byte, and what is derived
---------------------------------------------------
Replayed verbatim (status line, headers and body as recorded):

* ``GET /api/models/<repo>/revision/<ref>`` -- the repository metadata,
* ``HEAD /<repo>/resolve/<sha>/model-00001-of-00004.safetensors``,
* ``GET  /<repo>/resolve/<sha>/config.json``,
* every recorded refusal (gated file, missing repository, ``whoami-v2``).

Derived, because the human recording session did not capture them and this
fixture is forbidden from inventing source evidence:

* ``GET /api/models/<repo>/tree/<sha>`` -- built from the ``siblings`` array of
  that repository's own recorded metadata (``path``/``size``/``oid`` are the
  recorded ``rfilename``/``size``/``blobId``, re-serialized into the shape
  ``HfApi.list_repo_tree`` parses),
* ``HEAD /<repo>/resolve/<sha>/<other file>`` -- ``content-length`` and ``etag``
  from the same recorded ``siblings`` entry.

The derivation is checked against the one recording that covers both halves:
the recorded ``HEAD`` reports ``x-linked-size: 3945441440`` and the recorded
``siblings`` entry for the same file reports ``size: 3945441440``
(``assert_recorded_head_agrees_with_siblings``).  A test asserts that equality
so a drifting derivation fails loudly instead of quietly serving made-up sizes.

Unrecorded source outcomes are refused, never invented: asking this Hub for a
successful ``whoami-v2`` or for a gated file with a valid credential raises
``MissingRecording`` rather than answering 200.  Transfer bytes are the sole
exception: an off-by-default switch may generate only the body of a file whose
existence, size and commit already come from recorded ``siblings``.  Recorded
bodies and refusals win before that path, and the synthetic ETag labels it.

One attack shape is expressible, and it is off by default: ``listing_path_substitutes``
renames the recorded ``config.json`` sibling in the derived tree to ``FX-MS-06``'s
path samples (M2-14A, ``TC-DL-26``).  Size, ``oid`` and body stay the recorded
``config.json``'s, so it can test where the bytes land, never whether a source
would have served them.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Final
from urllib.parse import parse_qs, unquote, urlsplit

from .recordings import RecordedResponse, load_recording

PUBLIC_REPOSITORY_ID: Final[str] = "Qwen/Qwen2.5-7B-Instruct"
PUBLIC_REPOSITORY_COMMIT: Final[str] = "a09a35458c702b33eeacc393d103063234e8bc28"
TINY_REPOSITORY_ID: Final[str] = "hf-internal-testing/tiny-random-gpt2"
TINY_REPOSITORY_COMMIT: Final[str] = "71034c5d8bde858ff824298bdedc65515b97d2b9"
GATED_REPOSITORY_ID: Final[str] = "meta-llama/Llama-3.1-8B-Instruct"

# The recordings for "a repository that does not exist" do not carry the name
# that was asked for, so the name is this fixture's own choice.  It is not
# source evidence and nothing is derived from it.
MISSING_REPOSITORY_ID: Final[str] = "holm-fixture/repository-that-does-not-exist"

PUBLIC_REPOSITORY_LARGE_FILE: Final[str] = "model-00001-of-00004.safetensors"


class MissingRecording(RuntimeError):
    """The fixture was asked for an outcome nobody recorded from the real Hub."""


class CredentialPresence(StrEnum):
    """How the incoming request identified itself, without storing the token."""

    ABSENT = "absent"
    VALID = "valid"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class ObservedRequest:
    """One request the SDK made.  The credential is recorded as a state only."""

    method: str
    path: str
    query: Mapping[str, tuple[str, ...]]
    credential: CredentialPresence
    range_header: str | None


@dataclass(frozen=True, slots=True)
class RecordedFile:
    """One entry of a recorded ``siblings`` array, with its recorded numbers.

    ``size_bytes`` and ``blob_id`` are ``None`` when the recording carries no
    such number, which is a real shape of the recorded responses rather than a
    defect: ``gated-repo-metadata`` lists 17 files and
    ``invalid-credential-public-repo`` lists 14, and neither carries a single
    ``size`` or ``blobId`` (the Hub only fills those in for a metadata read that
    asked for ``?blobs=true``).

    ``None`` means "this recording does not say".  It is not ``0`` -- a file of
    zero bytes is a different statement -- and it is not a reason to leave the
    entry out: dropping the unmeasured files would silently shrink the listing
    while every positive case stayed green.
    """

    path: str
    size_bytes: int | None
    blob_id: str | None


def _recorded_number(value: object) -> int | None:
    """One recorded ``siblings`` number, or ``None`` when the recording has none."""

    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise MissingRecording(f"a recorded siblings number is not an integer: {value!r}")
    return value


def _recorded_text(value: object) -> str | None:
    """One recorded ``siblings`` string, or ``None`` when the recording has none."""

    if value is None:
        return None
    if not isinstance(value, str):
        raise MissingRecording(f"a recorded siblings identifier is not a string: {value!r}")
    return value


@dataclass(frozen=True, slots=True)
class RecordedRepository:
    """A repository the recordings cover, and which recording covers what."""

    repository_id: str
    default_branch_recording: str
    commit_recording: str | None = None
    invalid_credential_metadata_recording: str | None = None
    file_head_recordings: Mapping[str, str] = field(default_factory=dict)
    file_body_recordings: Mapping[str, str] = field(default_factory=dict)
    denied_file_recordings: Mapping[CredentialPresence, str] = field(default_factory=dict)

    def metadata(
        self,
        credential: CredentialPresence,
        reference: str | None = None,
    ) -> RecordedResponse:
        """The recording that answers this metadata read.

        The public repository was recorded twice -- once by branch name and once
        by the resolved Commit -- and that pair is what proves Branch -> Commit
        pinning is idempotent, so the two are kept apart here instead of one
        standing in for the other.
        """

        if (
            credential is CredentialPresence.INVALID
            and self.invalid_credential_metadata_recording is not None
        ):
            return load_recording(self.invalid_credential_metadata_recording)
        if (
            self.commit_recording is not None
            and reference is not None
            and reference == self.commit()
        ):
            return load_recording(self.commit_recording)
        return load_recording(self.default_branch_recording)

    def commit(self) -> str:
        sha = load_recording(self.default_branch_recording).json_body()["sha"]
        assert isinstance(sha, str)
        return sha

    def files(self) -> tuple[RecordedFile, ...]:
        """The recorded file listing, as the only source of derived sizes.

        Every recorded sibling becomes one entry, including the ones the
        recording gives no ``size``/``blobId`` for.  Those keep ``None`` (see
        ``RecordedFile``) instead of being filled in or filtered out.
        """

        entries = load_recording(self.default_branch_recording).json_body()["siblings"]
        return tuple(
            RecordedFile(
                path=str(entry["rfilename"]),
                size_bytes=_recorded_number(entry.get("size")),
                blob_id=_recorded_text(entry.get("blobId")),
            )
            for entry in entries
        )

    def claimed_recordings(self) -> frozenset[str]:
        """Every recording this repository entry is responsible for replaying."""

        names = {self.default_branch_recording}
        for optional in (self.commit_recording, self.invalid_credential_metadata_recording):
            if optional is not None:
                names.add(optional)
        names.update(self.file_head_recordings.values())
        names.update(self.file_body_recordings.values())
        names.update(self.denied_file_recordings.values())
        return frozenset(names)


PUBLIC_REPOSITORY: Final[RecordedRepository] = RecordedRepository(
    repository_id=PUBLIC_REPOSITORY_ID,
    default_branch_recording="public-repo-main",
    commit_recording="public-repo-by-sha",
    invalid_credential_metadata_recording="invalid-credential-public-repo",
    file_head_recordings={PUBLIC_REPOSITORY_LARGE_FILE: "public-repo-file-head"},
    file_body_recordings={"config.json": "public-repo-config"},
)
TINY_REPOSITORY: Final[RecordedRepository] = RecordedRepository(
    repository_id=TINY_REPOSITORY_ID,
    default_branch_recording="tiny-repo-main",
)
GATED_REPOSITORY: Final[RecordedRepository] = RecordedRepository(
    repository_id=GATED_REPOSITORY_ID,
    default_branch_recording="gated-repo-metadata",
    denied_file_recordings={
        CredentialPresence.ABSENT: "gated-repo-file-denied",
        CredentialPresence.INVALID: "invalid-credential-gated-file",
    },
)
RECORDED_REPOSITORIES: Final[tuple[RecordedRepository, ...]] = (
    PUBLIC_REPOSITORY,
    TINY_REPOSITORY,
    GATED_REPOSITORY,
)

_MISSING_REPOSITORY_RECORDINGS: Final[Mapping[CredentialPresence, str]] = {
    CredentialPresence.ABSENT: "missing-repo-anonymous",
    CredentialPresence.INVALID: "invalid-credential-missing-repo",
    CredentialPresence.VALID: "missing-repo-authenticated",
}
_WHOAMI_RECORDINGS: Final[Mapping[CredentialPresence, str]] = {
    CredentialPresence.ABSENT: "anonymous-whoami",
    CredentialPresence.INVALID: "invalid-credential-whoami",
}


def claimed_recordings() -> frozenset[str]:
    """Every recording the fake Hub can actually put back on the wire.

    A recording that stops being claimed -- or a new one nobody wired up --
    fails ``FX-MS-08``'s coverage test instead of quietly going unused.
    """

    names: set[str] = set()
    for repository in RECORDED_REPOSITORIES:
        names |= repository.claimed_recordings()
    names.update(_MISSING_REPOSITORY_RECORDINGS.values())
    names.update(_WHOAMI_RECORDINGS.values())
    return frozenset(names)


def assert_recorded_head_agrees_with_siblings() -> int:
    """Prove the derived per-file sizes come from the same numbers the Hub sent.

    Returns the agreed size so a caller can assert on it directly.
    """

    head = load_recording(PUBLIC_REPOSITORY.file_head_recordings[PUBLIC_REPOSITORY_LARGE_FILE])
    linked_size = head.header("x-linked-size")
    if linked_size is None:
        raise MissingRecording("the recorded file HEAD carries no x-linked-size")
    sibling_sizes = {entry.path: entry.size_bytes for entry in PUBLIC_REPOSITORY.files()}
    recorded = sibling_sizes[PUBLIC_REPOSITORY_LARGE_FILE]
    if recorded is None:
        raise MissingRecording("the recorded siblings entry carries no size")
    if recorded != int(linked_size):
        raise AssertionError(
            "the recorded HEAD and the recorded siblings entry disagree about the file size"
        )
    return recorded


@dataclass
class FakeHubState:
    """Everything the handler needs, shared by the serving threads."""

    valid_token: str | None
    serve_synthetic_file_bodies: bool
    response_chunk_bytes: int | None
    response_chunk_delay_seconds: float
    requests: list[ObservedRequest] = field(default_factory=list)
    active_file_gets: int = 0
    max_concurrent_file_gets: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)
    idle_condition: threading.Condition = field(init=False)
    listing_path_substitutes: tuple[str, ...] = ()
    hostile_listing_after: int = 0
    listing_requests: int = 0
    hostile_listings_served: int = 0

    def __post_init__(self) -> None:
        self.idle_condition = threading.Condition(self.lock)

    def next_listing_is_hostile(self) -> bool:
        """Count one listing read; every read after ``hostile_listing_after`` is substituted."""

        with self.lock:
            self.listing_requests += 1
            hostile = self.listing_requests > self.hostile_listing_after
            if hostile:
                self.hostile_listings_served += 1
            return hostile

    def credential(self, authorization: str | None) -> CredentialPresence:
        if authorization is None:
            return CredentialPresence.ABSENT
        token = authorization.removeprefix("Bearer ").strip()
        if self.valid_token is not None and token == self.valid_token:
            return CredentialPresence.VALID
        return CredentialPresence.INVALID

    def observe(self, request: ObservedRequest) -> None:
        with self.lock:
            self.requests.append(request)


def _repository_by_id(repository_id: str) -> RecordedRepository | None:
    for repository in RECORDED_REPOSITORIES:
        if repository.repository_id == repository_id:
            return repository
    return None


@dataclass(frozen=True, slots=True)
class _Reply:
    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes
    track_transfer: bool = False

    @classmethod
    def from_recording(cls, recording: RecordedResponse) -> _Reply:
        return cls(
            status=recording.status,
            headers=recording.replayable_headers(),
            body=recording.body,
        )


_JSON_HEADERS: Final[tuple[tuple[str, str], ...]] = (
    ("content-type", "application/json; charset=utf-8"),
)


def _derived_tree(repository: RecordedRepository) -> _Reply:
    """Re-serialize the recorded ``siblings`` array into a tree listing.

    A file the recording gives no ``size``/``blobId`` for keeps the key with an
    explicit ``null``: the listing still names every recorded file, and it says
    "unmeasured" rather than claiming zero bytes.  ``huggingface_hub`` reads
    both keys straight off the JSON (``hf_api.RepoFile.__init__``), so the null
    travels into ``RepoFile.size`` unchanged instead of being invented here.
    """

    entries = [
        {
            "type": "file",
            "path": entry.path,
            "size": entry.size_bytes,
            "oid": entry.blob_id,
        }
        for entry in repository.files()
    ]
    return _Reply(
        status=200,
        headers=_JSON_HEADERS,
        body=json.dumps(entries).encode("utf-8"),
    )


def _substituted_tree(paths: Sequence[str]) -> _Reply:
    """``FX-MS-06``'s listing-path attack: the recorded ``config.json`` sibling under other names.

    Only ``path`` changes.  ``size`` and ``oid`` are the recorded sibling's, and
    every such name is answered with the recorded ``config.json`` HEAD and body.
    """

    (entry,) = (item for item in PUBLIC_REPOSITORY.files() if item.path == "config.json")
    entries = [
        {"type": "file", "path": path, "size": entry.size_bytes, "oid": entry.blob_id}
        for path in paths
    ]
    return _Reply(status=200, headers=_JSON_HEADERS, body=json.dumps(entries).encode("utf-8"))


def _derived_file_head(repository: RecordedRepository, filename: str) -> _Reply | None:
    for entry in repository.files():
        if entry.path != filename:
            continue
        if entry.size_bytes is None or entry.blob_id is None:
            # A HEAD has nowhere to say "size unknown": every number it carries
            # is a claim about the file.  Refusing keeps the fixture from
            # answering with a made-up length the SDK would then treat as fact.
            raise MissingRecording(
                f"the recording gives no size for {repository.repository_id}/{filename}"
            )
        return _Reply(
            status=200,
            headers=(
                ("content-type", "application/octet-stream"),
                ("etag", f'"{entry.blob_id}"'),
                ("x-repo-commit", repository.commit()),
                ("accept-ranges", "bytes"),
                ("content-length", str(entry.size_bytes)),
            ),
            body=b"",
        )
    return None


def _sibling_size(repository: RecordedRepository, filename: str) -> int | None:
    """The recorded size of one file, or ``None`` when the repository has no such file.

    A file that *is* listed but carries no recorded size is refused rather than
    reported as absent, so "nobody measured it" cannot be read as "not here".
    """

    for entry in repository.files():
        if entry.path == filename:
            if entry.size_bytes is None:
                raise MissingRecording(
                    f"the recording gives no size for {repository.repository_id}/{filename}"
                )
            return entry.size_bytes
    return None


class _FakeHubHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    state: FakeHubState

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        """Silence the stdlib access log; the test asserts on ``requests``."""

    # -------------------------------------------------------------- routing
    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        self._dispatch("GET")

    def do_HEAD(self) -> None:  # noqa: N802 - stdlib naming
        self._dispatch("HEAD")

    def _dispatch(self, method: str) -> None:
        split = urlsplit(self.path)
        path = unquote(split.path)
        query = {key: tuple(values) for key, values in parse_qs(split.query).items()}
        credential = self.state.credential(self.headers.get("authorization"))
        self.state.observe(
            ObservedRequest(
                method=method,
                path=path,
                query=query,
                credential=credential,
                range_header=self.headers.get("range"),
            )
        )
        try:
            reply = self._reply_for(method, path, credential)
        except MissingRecording as error:
            reply = _Reply(
                status=599,
                headers=(("content-type", "text/plain; charset=utf-8"),),
                body=f"fake hub has no recording: {error}".encode(),
            )
        self._send(method, reply)

    def _reply_for(self, method: str, path: str, credential: CredentialPresence) -> _Reply:
        if path == "/api/whoami-v2":
            recording = _WHOAMI_RECORDINGS.get(credential)
            if recording is None:
                raise MissingRecording("a successful whoami-v2 was never recorded")
            return _Reply.from_recording(load_recording(recording))
        if path.startswith("/api/models/"):
            return self._model_api_reply(path, credential)
        return self._resolve_reply(method, path, credential)

    def _model_api_reply(self, path: str, credential: CredentialPresence) -> _Reply:
        remainder = path.removeprefix("/api/models/")
        for keyword in ("/revision/", "/tree/"):
            head, separator, _ = remainder.partition(keyword)
            if separator:
                repository_id = head
                break
        else:
            repository_id = remainder
            keyword = "/revision/"
        repository = _repository_by_id(repository_id)
        if repository is None:
            return self._missing_repository_reply(credential)
        if keyword == "/tree/":
            substitutes = self.state.listing_path_substitutes
            if substitutes and repository is PUBLIC_REPOSITORY:
                if self.state.next_listing_is_hostile():
                    return _substituted_tree(substitutes)
                return _substituted_tree(("config.json",))
            return _derived_tree(repository)
        _, _, reference = remainder.partition(keyword)
        return _Reply.from_recording(repository.metadata(credential, reference or None))

    def _resolve_reply(self, method: str, path: str, credential: CredentialPresence) -> _Reply:
        head, separator, remainder = path.lstrip("/").partition("/resolve/")
        if not separator:
            return _Reply(status=404, headers=_JSON_HEADERS, body=b'{"error":"unrouted"}')
        repository_id = head
        _, _, filename = remainder.partition("/")
        repository = _repository_by_id(repository_id)
        if repository is None:
            return self._missing_repository_reply(credential)
        denied = repository.denied_file_recordings.get(credential)
        if denied is not None:
            return _Reply.from_recording(load_recording(denied))
        if repository.denied_file_recordings:
            raise MissingRecording(
                f"an authorized file read of {repository.repository_id} was never recorded"
            )
        if repository is PUBLIC_REPOSITORY and filename in self.state.listing_path_substitutes:
            # A substituted name stands for the recorded config.json, HEAD and body alike.
            filename = "config.json"
        recorded_body = repository.file_body_recordings.get(filename)
        if method == "GET" and recorded_body is not None:
            return _Reply.from_recording(load_recording(recorded_body))
        if method == "GET":
            size = _sibling_size(repository, filename)
            if size is None:
                return _Reply(
                    status=404, headers=_JSON_HEADERS, body=b'{"error":"Entry not found"}'
                )
            if not self.state.serve_synthetic_file_bodies:
                raise MissingRecording(f"no recorded body for {repository_id}/{filename}")
            seed = hashlib.sha256(filename.encode("utf-8")).digest()
            body = (seed * ((size + len(seed) - 1) // len(seed)))[:size]
            range_header = self.headers.get("range")
            status = 200
            range_headers: tuple[tuple[str, str], ...] = ()
            if range_header is not None:
                matched = re.fullmatch(r"bytes=(\d+)-", range_header)
                if matched is None or int(matched.group(1)) >= size:
                    return _Reply(
                        status=416,
                        headers=(("content-range", f"bytes */{size}"),),
                        body=b"",
                    )
                start = int(matched.group(1))
                body = body[start:]
                status = 206
                range_headers = (("content-range", f"bytes {start}-{size - 1}/{size}"),)
            return _Reply(
                status=status,
                headers=(
                    ("content-type", "application/octet-stream"),
                    ("etag", f'"synthetic-{filename}"'),
                    ("x-repo-commit", repository.commit()),
                    ("accept-ranges", "bytes"),
                    ("content-length", str(len(body))),
                    *range_headers,
                ),
                body=body,
                track_transfer=True,
            )
        recorded_head = repository.file_head_recordings.get(filename)
        if recorded_head is not None:
            return _Reply.from_recording(load_recording(recorded_head))
        derived = _derived_file_head(repository, filename)
        if derived is None:
            return _Reply(status=404, headers=_JSON_HEADERS, body=b'{"error":"Entry not found"}')
        return derived

    def _missing_repository_reply(self, credential: CredentialPresence) -> _Reply:
        return _Reply.from_recording(load_recording(_MISSING_REPOSITORY_RECORDINGS[credential]))

    # --------------------------------------------------------------- output
    def _send(self, method: str, reply: _Reply) -> None:
        body = b"" if method == "HEAD" else reply.body
        self.send_response(reply.status)
        content_length_sent = False
        for key, value in reply.headers:
            if key.lower() == "content-length":
                content_length_sent = True
                self.send_header(key, value)
                continue
            self.send_header(key, value)
        if not content_length_sent:
            self.send_header("content-length", str(len(reply.body)))
        self.end_headers()
        if body:
            chunk_size = self.state.response_chunk_bytes or len(body)
            if reply.track_transfer:
                with self.state.lock:
                    self.state.active_file_gets += 1
                    self.state.max_concurrent_file_gets = max(
                        self.state.max_concurrent_file_gets,
                        self.state.active_file_gets,
                    )
            try:
                for offset in range(0, len(body), chunk_size):
                    self.wfile.write(body[offset : offset + chunk_size])
                    self.wfile.flush()
                    if self.state.response_chunk_delay_seconds > 0:
                        time.sleep(self.state.response_chunk_delay_seconds)
            except (BrokenPipeError, ConnectionResetError):
                return
            finally:
                if reply.track_transfer:
                    with self.state.idle_condition:
                        self.state.active_file_gets -= 1
                        if self.state.active_file_gets == 0:
                            self.state.idle_condition.notify_all()


@dataclass(frozen=True, slots=True)
class FakeHub:
    """A running fake Hub: its endpoint URL and the requests it has seen."""

    endpoint: str
    state: FakeHubState

    @property
    def requests(self) -> tuple[ObservedRequest, ...]:
        with self.state.lock:
            return tuple(self.state.requests)

    @property
    def hostile_listings_served(self) -> int:
        """How many tree listings were answered with the substituted paths."""

        with self.state.lock:
            return self.state.hostile_listings_served

    @property
    def max_concurrent_file_gets(self) -> int:
        with self.state.lock:
            return self.state.max_concurrent_file_gets

    def clear(self) -> None:
        with self.state.lock:
            if self.state.active_file_gets != 0:
                raise RuntimeError("cannot clear fake Hub observations during a file transfer")
            self.state.requests.clear()
            self.state.max_concurrent_file_gets = 0

    def wait_until_idle(self, timeout: float) -> None:
        """Wait for serving threads to finish file GETs before clearing observations."""

        with self.state.idle_condition:
            if not self.state.idle_condition.wait_for(
                lambda: self.state.active_file_gets == 0, timeout=timeout
            ):
                raise TimeoutError(
                    f"fake Hub did not become idle: active_file_gets={self.state.active_file_gets}"
                )


@contextmanager
def fake_hugging_face_hub(
    *,
    valid_token: str | None = None,
    serve_synthetic_file_bodies: bool = False,
    response_chunk_bytes: int | None = None,
    response_chunk_delay_seconds: float = 0.0,
    listing_path_substitutes: Sequence[str] = (),
    hostile_listing_after: int = 0,
) -> Iterator[FakeHub]:
    """Serve the recordings on a loopback port for the duration of the block.

    ``listing_path_substitutes`` turns the public repository's tree listing into
    ``FX-MS-06``'s listing-path attack (``_substituted_tree``).  The first
    ``hostile_listing_after`` tree reads get the recorded ``config.json`` sibling
    under its own name instead, which expresses a source that changes its answer
    between two reads.
    """

    if response_chunk_bytes is not None and response_chunk_bytes <= 0:
        raise ValueError("response chunk size must be positive")
    if response_chunk_delay_seconds < 0:
        raise ValueError("response chunk delay must not be negative")
    if hostile_listing_after < 0 or any(not path for path in listing_path_substitutes):
        raise ValueError("listing substitutes must be non-empty and the delay non-negative")
    state = FakeHubState(
        valid_token=valid_token,
        serve_synthetic_file_bodies=serve_synthetic_file_bodies,
        response_chunk_bytes=response_chunk_bytes,
        response_chunk_delay_seconds=response_chunk_delay_seconds,
        listing_path_substitutes=tuple(listing_path_substitutes),
        hostile_listing_after=hostile_listing_after,
    )
    handler = type("_BoundFakeHubHandler", (_FakeHubHandler,), {"state": state})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, name="fake-hugging-face-hub")
    thread.start()
    try:
        yield FakeHub(endpoint=f"http://127.0.0.1:{server.server_port}", state=state)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)
        if thread.is_alive():  # pragma: no cover - would leak a thread
            raise RuntimeError("the fake Hub thread did not stop")


__all__ = [
    "GATED_REPOSITORY",
    "GATED_REPOSITORY_ID",
    "MISSING_REPOSITORY_ID",
    "PUBLIC_REPOSITORY",
    "PUBLIC_REPOSITORY_COMMIT",
    "PUBLIC_REPOSITORY_ID",
    "PUBLIC_REPOSITORY_LARGE_FILE",
    "RECORDED_REPOSITORIES",
    "TINY_REPOSITORY",
    "TINY_REPOSITORY_COMMIT",
    "TINY_REPOSITORY_ID",
    "CredentialPresence",
    "FakeHub",
    "MissingRecording",
    "ObservedRequest",
    "RecordedFile",
    "RecordedRepository",
    "assert_recorded_head_agrees_with_siblings",
    "claimed_recordings",
    "fake_hugging_face_hub",
]
