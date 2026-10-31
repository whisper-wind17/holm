"""A local ModelScope Hub that replays the FX-MS-09 recordings.

Why a server and not a stub: the thing under test is the Adapter's HTTP round
trip -- which endpoint it asks, what it sends, and how it reads a status line
together with a body ``Code``.  Patching the reading functions would test the
patch; the Adapter here runs unmodified and is simply pointed at this address
(an address, not a credential).

**Every source conclusion this Hub can produce is a recording.**  Repository
metadata, file listings, revision answers, credential decisions and refusals
are never synthesized.  An outcome nobody recorded raises
:class:`MissingRecording` and is served as an obviously wrong 599.

Transfer bodies have one deliberately narrower escape hatch, matching the
Hugging Face fixture: it is off by default, recorded bodies win, and only the
bytes of a file already present in a recorded listing may be generated.  Its
length is the listing's recorded ``Size`` and its ETag says ``synthetic``.
Recorded refusals are selected before that path.  LFS keeps the recorded 302
path and query while deriving only a loopback host; the loopback target applies
the same bounded body rule.  This can test transport mechanics, but can never
create a repository, file, size, revision, permission or credential outcome.

One attack shape is expressible, and it is off by default: ``listing_path_substitutes``
renames the recorded ``config.json`` listing entry to ``FX-MS-06``'s path samples
(M2-14A, ``TC-DL-26``).  Everything except the name stays recorded -- size,
hash and body -- so it can test where the bytes land, never whether a source
would have served them.

The two recorded gaps (a successful ``/users/info``; a private or gated
repository from either side) are exactly the ones ``recorded/PROVENANCE.md``
lists as needing real credentials, and they belong to M2-14.
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
from pathlib import PurePosixPath
from typing import Final
from urllib.parse import parse_qs, unquote, urlsplit, urlunsplit

from .recordings import RecordedResponse, load_recording

PUBLIC_REPOSITORY_ID: Final[str] = "Qwen/Qwen2.5-7B-Instruct"
SMALL_REPOSITORY_ID: Final[str] = "iic/nlp_structbert_word-segmentation_chinese-base"
MISSING_REPOSITORY_ID: Final[str] = "Qwen/Qwen2.5-7B-Instruct-no-such-repo-abc123"
"""The three identities the recording script used, spelled as it spelled them.

The missing one deliberately shares a prefix with the public one, which is how
the recording was made; routing is by exact identity, never by prefix."""

PUBLIC_DEFAULT_BRANCH: Final[str] = "master"
PUBLIC_TRUNCATED_SHA: Final[str] = "3fe289ea2828843e2d"
PUBLIC_UNKNOWN_SHA: Final[str] = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
PUBLIC_UNKNOWN_BRANCH: Final[str] = "nonexistent-branch-xyz"
PUBLIC_LARGE_FILE: Final[str] = "model-00001-of-00004.safetensors"
CONFIG_FILENAME: Final[str] = "config.json"

USERS_INFO_PATH: Final[str] = "/api/v1/users/info"
_MODELS_PREFIX: Final[str] = "/api/v1/models/"


class MissingRecording(RuntimeError):
    """The fixture was asked for an outcome nobody recorded from the real Hub."""


class CredentialPresence(StrEnum):
    """How the incoming request identified itself, without storing the token."""

    ABSENT = "absent"
    VALID = "valid"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class ObservedRequest:
    """One request the Adapter made.  The credential is recorded as a state only."""

    method: str
    path: str
    query: Mapping[str, tuple[str, ...]]
    credential: CredentialPresence
    range_header: str | None

    def revision(self) -> str | None:
        values = self.query.get("Revision")
        return values[0] if values else None


@dataclass(frozen=True, slots=True)
class RecordedRepository:
    """A repository the recordings cover, and which recording covers what."""

    repository_id: str
    metadata_recordings: Mapping[CredentialPresence, str]
    listing_recordings: Mapping[str, str] = field(default_factory=dict)
    listing_recording_for_any_revision: str | None = None
    revisions_recording: str | None = None
    file_recordings: Mapping[str, str] = field(default_factory=dict)

    def metadata(self, credential: CredentialPresence) -> RecordedResponse:
        recording = self.metadata_recordings.get(credential)
        if recording is None:
            raise MissingRecording(
                f"no metadata recording for {self.repository_id} with a {credential} credential"
            )
        return load_recording(recording)

    def listing(self, revision: str | None) -> RecordedResponse:
        if self.listing_recording_for_any_revision is not None:
            return load_recording(self.listing_recording_for_any_revision)
        if revision is None:
            raise MissingRecording(f"a file listing without a Revision for {self.repository_id}")
        recording = self.listing_recordings.get(revision)
        if recording is None:
            raise MissingRecording(f"no file listing recording for {self.repository_id}@{revision}")
        return load_recording(recording)

    def revisions(self) -> RecordedResponse:
        if self.revisions_recording is None:
            raise MissingRecording(f"no revisions recording for {self.repository_id}")
        return load_recording(self.revisions_recording)

    def file(self, filename: str | None) -> RecordedResponse:
        recording = self.file_recordings.get(filename or "")
        if recording is None:
            raise MissingRecording(f"no file recording for {self.repository_id}/{filename}")
        return load_recording(recording)

    def claimed_recordings(self) -> frozenset[str]:
        names = set(self.metadata_recordings.values())
        names.update(self.listing_recordings.values())
        names.update(self.file_recordings.values())
        for optional in (self.listing_recording_for_any_revision, self.revisions_recording):
            if optional is not None:
                names.add(optional)
        return frozenset(names)


PUBLIC_REPOSITORY: Final[RecordedRepository] = RecordedRepository(
    repository_id=PUBLIC_REPOSITORY_ID,
    metadata_recordings={
        CredentialPresence.ABSENT: "public-repo-metadata",
        CredentialPresence.INVALID: "invalid-credential-public-repo",
    },
    listing_recordings={
        PUBLIC_DEFAULT_BRANCH: "public-repo-files-master",
        "": "public-repo-files-empty-revision",
        PUBLIC_TRUNCATED_SHA: "public-repo-files-by-partial-sha",
        PUBLIC_UNKNOWN_SHA: "public-repo-files-unknown-sha",
        PUBLIC_UNKNOWN_BRANCH: "public-repo-files-unknown-branch",
    },
    revisions_recording="public-repo-revisions",
    file_recordings={
        CONFIG_FILENAME: "public-repo-config",
        PUBLIC_LARGE_FILE: "public-repo-lfs-redirect",
    },
)
SMALL_REPOSITORY: Final[RecordedRepository] = RecordedRepository(
    repository_id=SMALL_REPOSITORY_ID,
    metadata_recordings={},
    listing_recordings={PUBLIC_DEFAULT_BRANCH: "small-repo-files-master"},
)
MISSING_REPOSITORY: Final[RecordedRepository] = RecordedRepository(
    repository_id=MISSING_REPOSITORY_ID,
    metadata_recordings={
        CredentialPresence.ABSENT: "missing-repo-metadata-anonymous",
        CredentialPresence.INVALID: "invalid-credential-missing-repo",
    },
    listing_recording_for_any_revision="missing-repo-files-anonymous",
)
RECORDED_REPOSITORIES: Final[tuple[RecordedRepository, ...]] = (
    PUBLIC_REPOSITORY,
    SMALL_REPOSITORY,
    MISSING_REPOSITORY,
)

_USERS_INFO_RECORDINGS: Final[Mapping[CredentialPresence, str]] = {
    CredentialPresence.ABSENT: "anonymous-users-info",
    CredentialPresence.INVALID: "invalid-credential-users-info",
}
"""No entry for ``VALID``: the recording session had no ModelScope account, and
the missing 200 is the known gap M2-14 owns.  Adding a synthesized one here
would turn ``TC-DL-32``'s control permanently green."""


def claimed_recordings() -> frozenset[str]:
    """Every recording this fake Hub can actually put back on the wire.

    A recording that stops being claimed -- or a new one nobody wired up --
    fails ``FX-MS-09``'s coverage test instead of quietly going unused.
    """

    names: set[str] = set()
    for repository in RECORDED_REPOSITORIES:
        names |= repository.claimed_recordings()
    names.update(_USERS_INFO_RECORDINGS.values())
    return frozenset(names)


def _recorded_file(
    repository: RecordedRepository,
    filename: str,
) -> Mapping[str, object] | None:
    recording_name = repository.listing_recordings.get(PUBLIC_DEFAULT_BRANCH)
    if recording_name is None:
        return None
    document = load_recording(recording_name).json_body()
    if not isinstance(document, dict):
        raise AssertionError("a recorded listing must be a JSON object")
    data = document.get("Data")
    if not isinstance(data, dict):
        raise AssertionError("a recorded listing must carry Data")
    files = data.get("Files")
    if not isinstance(files, list):
        raise AssertionError("a recorded listing must carry Files")
    for entry in files:
        if isinstance(entry, dict) and entry.get("Path") == filename:
            return entry
    return None


def assert_recorded_lfs_redirect_agrees_with_listing() -> str:
    """Tie the replayed redirect path and query to the recorded LFS entry."""

    entry = _recorded_file(PUBLIC_REPOSITORY, PUBLIC_LARGE_FILE)
    assert entry is not None and entry.get("IsLFS") is True
    sha256 = entry.get("Sha256")
    size = entry.get("Size")
    assert isinstance(sha256, str) and len(sha256) == 64
    assert isinstance(size, int) and size > 0
    location = load_recording("public-repo-lfs-redirect").header("location")
    assert location is not None
    split = urlsplit(location)
    query = parse_qs(split.query, keep_blank_values=True)
    assert split.hostname == "cdn-lfs-cn-1.modelscope.cn"
    assert split.path.removeprefix("/prod/lfs-objects/").replace("/", "") == sha256
    assert query == {
        "filename": [PUBLIC_LARGE_FILE],
        "namespace": ["Qwen"],
        "repository": ["Qwen2.5-7B-Instruct"],
        "revision": [PUBLIC_DEFAULT_BRANCH],
        "tag": ["model"],
        "auth_key": ["<signed-value-redacted>"],
    }
    return location


def derive_lfs_redirect_location(endpoint: str) -> str:
    """Replace only the host of the recorded redirect with the fixture host."""

    recorded = urlsplit(assert_recorded_lfs_redirect_agrees_with_listing())
    target = urlsplit(endpoint)
    assert target.scheme in {"http", "https"} and target.netloc
    return urlunsplit(
        (target.scheme, target.netloc, recorded.path, recorded.query, recorded.fragment)
    )


def _synthetic_reply(filename: str, total_size: int, range_header: str | None) -> _Reply:
    offset = 0
    end = total_size - 1
    status = 200
    range_headers: tuple[tuple[str, str], ...] = ()
    if range_header is not None:
        match = re.fullmatch(r"bytes=(\d+)-(\d*)", range_header)
        if match is None:
            return _Reply(
                status=416,
                headers=(("content-range", f"bytes */{total_size}"),),
                body=b"",
            )
        offset = int(match.group(1))
        requested_end = int(match.group(2)) if match.group(2) else end
        if offset >= total_size or requested_end < offset:
            return _Reply(
                status=416,
                headers=(("content-range", f"bytes */{total_size}"),),
                body=b"",
            )
        end = min(requested_end, end)
        status = 206
        range_headers = (("content-range", f"bytes {offset}-{end}/{total_size}"),)
    size = end - offset + 1
    return _Reply(
        status=status,
        headers=(
            ("content-type", "application/octet-stream"),
            ("etag", f'"synthetic-{filename}"'),
            ("accept-ranges", "bytes"),
            ("content-length", str(size)),
            *range_headers,
        ),
        body=b"",
        synthetic_filename=filename,
        synthetic_offset=offset,
        synthetic_size=size,
    )


@dataclass
class FakeHubState:
    """Everything the handler needs, shared by the serving threads."""

    valid_token: str | None
    serve_synthetic_file_bodies: bool
    response_chunk_bytes: int
    response_chunk_delay_seconds: float
    requests: list[ObservedRequest] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)
    listing_path_substitutes: tuple[str, ...] = ()
    hostile_listing_after: int = 0
    listing_requests: int = 0
    hostile_listings_served: int = 0

    def next_listing_is_hostile(self) -> bool:
        """Count one listing read; every read after ``hostile_listing_after`` is substituted."""

        with self.lock:
            self.listing_requests += 1
            hostile = self.listing_requests > self.hostile_listing_after
            if hostile:
                self.hostile_listings_served += 1
            return hostile

    def credential(
        self,
        authorization: str | None,
        cookie: str | None,
    ) -> CredentialPresence:
        token: str | None = None
        if authorization is not None:
            token = authorization.removeprefix("Bearer ").strip()
        elif cookie is not None:
            for item in cookie.split(";"):
                key, separator, value = item.strip().partition("=")
                if separator and key == "m_session_id":
                    token = value
                    break
        if token is None:
            return CredentialPresence.ABSENT
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
    synthetic_filename: str | None = None
    synthetic_offset: int = 0
    synthetic_size: int = 0

    @classmethod
    def from_recording(cls, recording: RecordedResponse) -> _Reply:
        return cls(
            status=recording.status,
            headers=recording.replayable_headers(),
            body=recording.body,
        )


def _substituted_listing(paths: Sequence[str]) -> _Reply:
    """``FX-MS-06``'s listing-path attack: the recorded ``config.json`` entry under other names.

    Only ``Path`` and ``Name`` change.  ``Size``, ``Sha256`` and every other key
    stay the recorded ones, and each name is answered with the recorded
    ``config.json`` body, so the SDK's own size and hash checks pass and the one
    variable left is where the file lands.  No source conclusion is invented:
    the repository, its revision and its bytes are all recorded.
    """

    recording = load_recording("public-repo-files-master")
    document = recording.json_body()
    entry = _recorded_file(PUBLIC_REPOSITORY, CONFIG_FILENAME)
    if not isinstance(document, dict) or entry is None:
        raise AssertionError("the recorded listing must carry the config.json entry")
    data = document.get("Data")
    if not isinstance(data, dict):
        raise AssertionError("a recorded listing must carry Data")
    data["Files"] = [{**entry, "Path": path, "Name": PurePosixPath(path).name} for path in paths]
    headers = tuple(
        (key, value)
        for key, value in recording.replayable_headers()
        if key.lower() != "content-length"
    )
    return _Reply(
        status=recording.status,
        headers=headers,
        body=json.dumps(document).encode("utf-8"),
    )


class _FakeHubHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    state: FakeHubState

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        """Silence the stdlib access log; the test asserts on ``requests``."""

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        split = urlsplit(self.path)
        path = unquote(split.path)
        query = {
            key: tuple(values)
            for key, values in parse_qs(split.query, keep_blank_values=True).items()
        }
        credential = self.state.credential(
            self.headers.get("authorization"),
            self.headers.get("cookie"),
        )
        observed = ObservedRequest(
            method="GET",
            path=path,
            query=query,
            credential=credential,
            range_header=self.headers.get("range"),
        )
        self.state.observe(observed)
        try:
            reply = self._reply_for(observed)
        except MissingRecording as error:
            reply = _Reply(
                status=599,
                headers=(("content-type", "text/plain; charset=utf-8"),),
                body=f"fake hub has no recording: {error}".encode(),
            )
        self._send(reply)

    def _reply_for(self, request: ObservedRequest) -> _Reply:
        substituted = self._substituted_reply(request)
        if substituted is not None:
            return substituted
        if request.path.startswith("/prod/lfs-objects/"):
            return self._synthetic_lfs_reply(request)
        try:
            recording = self._recording_for(request)
        except MissingRecording:
            if not self.state.serve_synthetic_file_bodies or not request.path.endswith("/repo"):
                raise
            filename_values = request.query.get("FilePath")
            filename = filename_values[0] if filename_values else None
            remainder = request.path.removeprefix(_MODELS_PREFIX).removesuffix("/repo")
            repository = _repository_by_id(remainder)
            if repository is None or filename is None:
                raise
            entry = _recorded_file(repository, filename)
            if entry is None or entry.get("IsLFS") is True:
                raise
            size = entry.get("Size")
            if not isinstance(size, int):
                raise MissingRecording("a recorded file has no integer Size") from None
            return _synthetic_reply(filename, size, request.range_header)
        if not request.path.endswith("/repo"):
            return _Reply.from_recording(recording)
        filename_values = request.query.get("FilePath")
        filename = filename_values[0] if filename_values else None
        if recording.status == 302 and self.state.serve_synthetic_file_bodies:
            endpoint = f"http://{self.headers['host']}"
            headers = tuple(
                (
                    key,
                    derive_lfs_redirect_location(endpoint) if key.lower() == "location" else value,
                )
                for key, value in recording.replayable_headers()
            )
            return _Reply(status=recording.status, headers=headers, body=recording.body)
        return _Reply.from_recording(recording)

    def _substituted_reply(self, request: ObservedRequest) -> _Reply | None:
        substitutes = self.state.listing_path_substitutes
        if not substitutes or not request.path.startswith(_MODELS_PREFIX):
            return None
        remainder = request.path.removeprefix(_MODELS_PREFIX)
        if remainder == f"{PUBLIC_REPOSITORY_ID}/repo/files":
            if self.state.next_listing_is_hostile():
                return _substituted_listing(substitutes)
            return _substituted_listing((CONFIG_FILENAME,))
        filename_values = request.query.get("FilePath")
        if (
            remainder == f"{PUBLIC_REPOSITORY_ID}/repo"
            and filename_values
            and filename_values[0] in substitutes
        ):
            return _Reply.from_recording(PUBLIC_REPOSITORY.file(CONFIG_FILENAME))
        return None

    def _recording_for(self, request: ObservedRequest) -> RecordedResponse:
        if request.path == USERS_INFO_PATH:
            recording = _USERS_INFO_RECORDINGS.get(request.credential)
            if recording is None:
                raise MissingRecording("a successful users/info was never recorded")
            return load_recording(recording)
        if not request.path.startswith(_MODELS_PREFIX):
            raise MissingRecording(f"unrouted path {request.path}")
        remainder = request.path.removeprefix(_MODELS_PREFIX)
        for suffix in ("/repo/files", "/repo", "/revisions"):
            if remainder.endswith(suffix):
                repository = _repository_by_id(remainder.removesuffix(suffix))
                if repository is None:
                    raise MissingRecording(f"no recording covers {remainder}")
                if suffix == "/repo/files":
                    return repository.listing(request.revision())
                if suffix == "/revisions":
                    return repository.revisions()
                values = request.query.get("FilePath")
                return repository.file(values[0] if values else None)
        repository = _repository_by_id(remainder)
        if repository is None:
            raise MissingRecording(f"no recording covers the repository {remainder}")
        return repository.metadata(request.credential)

    def _send(self, reply: _Reply) -> None:
        self.send_response(reply.status)
        content_length_sent = False
        for key, value in reply.headers:
            if key.lower() == "content-length":
                content_length_sent = True
            self.send_header(key, value)
        if not content_length_sent:
            length = (
                reply.synthetic_size if reply.synthetic_filename is not None else len(reply.body)
            )
            self.send_header("content-length", str(length))
        self.end_headers()
        if reply.synthetic_filename is not None:
            seed = hashlib.sha256(reply.synthetic_filename.encode("utf-8")).digest()
            remaining = reply.synthetic_size
            offset = reply.synthetic_offset
            try:
                while remaining:
                    amount = min(remaining, self.state.response_chunk_bytes)
                    repeated = seed * (
                        (offset % len(seed) + amount + len(seed) - 1) // len(seed) + 1
                    )
                    start = offset % len(seed)
                    self.wfile.write(repeated[start : start + amount])
                    self.wfile.flush()
                    remaining -= amount
                    offset += amount
                    if self.state.response_chunk_delay_seconds:
                        time.sleep(self.state.response_chunk_delay_seconds)
            except (BrokenPipeError, ConnectionResetError):
                return
        elif reply.body:
            self.wfile.write(reply.body)

    def _synthetic_lfs_reply(self, request: ObservedRequest) -> _Reply:
        if not self.state.serve_synthetic_file_bodies:
            raise MissingRecording("the recorded LFS redirect target is not replayable")
        recorded = urlsplit(assert_recorded_lfs_redirect_agrees_with_listing())
        if request.path != recorded.path or request.query != {
            key: tuple(values)
            for key, values in parse_qs(recorded.query, keep_blank_values=True).items()
        }:
            raise MissingRecording("the LFS redirect path or query drifted from its recording")
        entry = _recorded_file(PUBLIC_REPOSITORY, PUBLIC_LARGE_FILE)
        assert entry is not None
        size = entry.get("Size")
        assert isinstance(size, int)
        return _synthetic_reply(PUBLIC_LARGE_FILE, size, request.range_header)


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
        """How many listing reads were answered with the substituted paths."""

        with self.state.lock:
            return self.state.hostile_listings_served

    def clear(self) -> None:
        with self.state.lock:
            self.state.requests.clear()


@contextmanager
def fake_modelscope_hub(
    *,
    valid_token: str | None = None,
    serve_synthetic_file_bodies: bool = False,
    response_chunk_bytes: int = 64 * 1024,
    response_chunk_delay_seconds: float = 0.0,
    listing_path_substitutes: Sequence[str] = (),
    hostile_listing_after: int = 0,
) -> Iterator[FakeHub]:
    """Serve the recordings on a loopback port for the duration of the block.

    ``listing_path_substitutes`` turns the public repository's file listing into
    ``FX-MS-06``'s listing-path attack (``_substituted_listing``).  The first
    ``hostile_listing_after`` listing reads get the recorded ``config.json``
    entry under its own name instead, which expresses a source that changes its
    answer between two reads.
    """

    if response_chunk_bytes < 1 or response_chunk_delay_seconds < 0:
        raise ValueError("synthetic transfer timing must be non-negative and bounded")
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
    handler = type("_BoundFakeModelScopeHandler", (_FakeHubHandler,), {"state": state})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, name="fake-modelscope-hub")
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
    "CONFIG_FILENAME",
    "MISSING_REPOSITORY",
    "MISSING_REPOSITORY_ID",
    "PUBLIC_DEFAULT_BRANCH",
    "PUBLIC_LARGE_FILE",
    "PUBLIC_REPOSITORY",
    "PUBLIC_REPOSITORY_ID",
    "PUBLIC_TRUNCATED_SHA",
    "PUBLIC_UNKNOWN_BRANCH",
    "PUBLIC_UNKNOWN_SHA",
    "RECORDED_REPOSITORIES",
    "SMALL_REPOSITORY",
    "SMALL_REPOSITORY_ID",
    "USERS_INFO_PATH",
    "CredentialPresence",
    "FakeHub",
    "MissingRecording",
    "ObservedRequest",
    "RecordedRepository",
    "claimed_recordings",
    "assert_recorded_lfs_redirect_agrees_with_listing",
    "derive_lfs_redirect_location",
    "fake_modelscope_hub",
]
