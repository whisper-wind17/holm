"""One bounded ModelScope Hub read, reduced to what may be judged on.

Both ModelScope faces -- the Resolve-preview Adapter and the credential
validator -- talk to the same Hub over the same two shapes, so the wire lives
here once and the two judgements stay in their own modules.  Nothing in this
module reaches a conclusion; it only turns a response into the facts a
conclusion may be drawn from.

Three properties are structural rather than remembered:

* **The source's prose has nowhere to go.**  A ModelScope error body carries a
  Chinese ``Message`` ("获取模型信息失败，信息：record not found").
  :class:`ModelScopeReply` has no field for it, so it cannot reach a response
  body, a log, an audit detail or an exception text by being forgotten about
  (the credential privacy boundary).  What is kept is the numeric ``Code``
  and the HTTP status.
* **``Code`` and HTTP status are not the same layer.**  ``Code: 200`` is
  observed inside a 200 response whose ``Files`` is ``null`` -- a Revision that
  does not exist.  Both are therefore carried separately and a
  judgement that reads only one of them cannot be written by accident.
* **An absent file listing is not an empty one.**  :func:`parse_file_listing`
  answers with :class:`FileListingAbsent` for ``Files: null`` and with a tuple
  for a listing that is present, so the caller has to decide.  ``Files or []``
  -- the one line that turns a mistyped Revision into "resolved, no files" while
  every positive case stays green -- is not expressible through this function.

The HTTP client is the one this repository already has: ``huggingface_hub``
ships and configures a shared ``httpx`` client, and ``model_source_validation``
already reads a source over it.  Reusing it adds no dependency and no second
HTTP stack.  It carries no Hub-specific default headers; the
Authorization header below is built per request and passed by value.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import IntEnum
from typing import Any, Final
from urllib.parse import quote, urlencode

from holm_domain import (
    ArtifactManifestError,
    Identified,
    RemoteFile,
    canonical_artifact_path,
)
from huggingface_hub import get_session

MODELSCOPE_OFFICIAL_ENDPOINT: Final[str] = "https://modelscope.cn"
"""The one address this source is read at.

A Resolve request cannot influence it: the request Schema has no Endpoint, Base
URL, mirror or proxy field, and this constant is the default of the objects
below rather than something a caller supplies per call."""

MODELSCOPE_API_PREFIX: Final[str] = "/api/v1"
MODELSCOPE_USERS_INFO_PATH: Final[str] = f"{MODELSCOPE_API_PREFIX}/users/info"
CONFIG_FILENAME: Final[str] = "config.json"
"""Preview may read a small set of configuration files; this is the only one."""

MAX_CONFIG_BYTES: Final[int] = 1 << 20

_BLOB_TYPE: Final[str] = "blob"
_TREE_TYPE: Final[str] = "tree"


class ModelScopeCode(IntEnum):
    """The response-body ``Code`` values observed in recorded source responses.

    This enum is deliberately not "every code ModelScope has".  A member here
    is a value some recording actually carries, because a mapping entry for a
    value nobody has seen would be an invented semantic with no case able to
    drive it.  ``PROVENANCE.md`` additionally names ``10010102001`` (参数错误)
    as seen during the recording session, but no recording retains it, so it is
    absent here on purpose and lands in the callers' explicit "unmapped"
    conclusion.
    """

    SUCCESS = 200
    RECORD_NOT_FOUND = 10010205001
    USER_NOT_LOGGED_IN = 10010101003


@dataclass(frozen=True, slots=True, repr=False)
class ModelScopeReply:
    """One source answer: the HTTP status, the body ``Code`` and the payload.

    ``repr`` is suppressed because ``data`` is source-controlled content (a
    model card, a description) and a dataclass ``repr`` would render it into
    anything that formats this value.  The Chinese ``Message`` is not carried
    at all -- there is no field for it.
    """

    http_status: int
    code: int | None
    data: Mapping[str, Any] | None

    def __repr__(self) -> str:
        return f"ModelScopeReply(http_status={self.http_status}, code={self.code}, data=<omitted>)"


@dataclass(frozen=True, slots=True)
class FileListingAbsent:
    """``Files`` is ``null``: the source has no tree for this Revision.

    The recorded source response shows this as ``200`` / ``Success: true`` /
    ``Message: "success"`` / ``Files: null`` for both a mistyped branch name and
    a well-formed but non-existent sha.  It is the source's only signal that the
    Revision does not exist, and it is a failure, not an empty listing.
    """


@dataclass(frozen=True, slots=True)
class FileListingUnreadable:
    """The listing is present but not in the recorded shape.

    Kept apart from :class:`FileListingAbsent` so that "this Revision does not
    exist" is never reported for a listing the platform simply could not read.
    """


type FileListing = tuple[RemoteFile, ...] | FileListingAbsent | FileListingUnreadable


def parse_file_listing(data: Mapping[str, Any]) -> FileListing:
    """Read ``Data.Files`` without ever substituting a value for it.

    A ``tree`` entry is a directory: it is not a file, and its ``Size: 0`` /
    ``Sha256: ""`` are not measurements of anything.  Directories are therefore
    left out of the file listing, while an entry of any *other* type makes the
    whole listing unreadable rather than being dropped -- silently skipping
    entries is how a file plan loses members with every positive case still
    green (a transport failure must not become a successful validation).
    """

    entries = data.get("Files")
    if entries is None:
        return FileListingAbsent()
    if not isinstance(entries, list):
        return FileListingUnreadable()
    files: list[RemoteFile] = []
    for entry in entries:
        if not isinstance(entry, dict):
            return FileListingUnreadable()
        entry_type = entry.get("Type")
        if entry_type == _TREE_TYPE:
            continue
        if entry_type != _BLOB_TYPE:
            return FileListingUnreadable()
        path = entry.get("Path")
        size = entry.get("Size")
        digest = entry.get("Sha256")
        if not isinstance(path, str) or not path:
            return FileListingUnreadable()
        try:
            canonical_artifact_path(path)
        except ArtifactManifestError:
            return FileListingUnreadable()
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            return FileListingUnreadable()
        if not isinstance(digest, str):
            return FileListingUnreadable()
        try:
            files.append(RemoteFile(path=path, size_bytes=size, content_sha256=Identified(digest)))
        except ValueError:
            # A digest the source states but that is not a SHA-256 digest is a
            # listing this platform cannot read, not a file without a hash.
            return FileListingUnreadable()
    return tuple(sorted(files, key=lambda item: item.path))


def model_path(repository_id: str) -> str:
    """The metadata path for one repository, with the identity percent-encoded.

    ``/`` stays a separator because a ModelScope Repository ID is
    ``<owner>/<name>`` and the Hub route is spelled that way.  Every other
    character that would change the request is encoded, and the identity
    reaching here has already been narrowed to two alphanumeric-leading
    segments by ``ModelResolutionService``.
    """

    return f"{MODELSCOPE_API_PREFIX}/models/{quote(repository_id, safe='/')}"


def repository_files_path(repository_id: str) -> str:
    return f"{model_path(repository_id)}/repo/files"


def repository_file_path(repository_id: str) -> str:
    return f"{model_path(repository_id)}/repo"


def credential_headers(credential: bytes | None) -> dict[str, str]:
    """Build the one request header a credential may travel in, by value.

    No credential means no ``Authorization`` header at all rather than an empty
    one: "not configured" and "configured but rejected" must reach the source as
    different requests, which is what lets the validation tests observe that the source
    answers them identically.
    """

    if credential is None:
        return {}
    return {"Authorization": f"Bearer {credential.decode('utf-8')}"}


def fetch_reply(
    *,
    endpoint: str,
    path: str,
    params: Mapping[str, str] | None,
    credential: bytes | None,
    timeout_seconds: float,
) -> ModelScopeReply:
    """Perform one GET and reduce the answer to status, ``Code`` and ``Data``.

    Transport failures propagate: they are not a source answer, and turning
    them into one would make "the network is down" indistinguishable from "the
    repository does not exist".
    """

    url = _url(endpoint, path, params)
    response = get_session().get(
        url,
        headers=credential_headers(credential),
        timeout=timeout_seconds,
    )
    document = _json_object(response.content)
    if document is None:
        return ModelScopeReply(http_status=response.status_code, code=None, data=None)
    code = document.get("Code")
    data = document.get("Data")
    return ModelScopeReply(
        http_status=response.status_code,
        code=code if isinstance(code, int) and not isinstance(code, bool) else None,
        data=data if isinstance(data, dict) else None,
    )


def fetch_repository_document(
    *,
    endpoint: str,
    repository_id: str,
    revision: str,
    filename: str,
    credential: bytes | None,
    timeout_seconds: float,
) -> Mapping[str, Any] | None:
    """Read one small configuration file into memory, or answer ``None``.

    ``None`` means "this preview has no configuration to read from", and every
    caller turns it into an explicit unrecognized metadata state rather than
    into a default value.
    """

    response = get_session().get(
        _url(
            endpoint,
            repository_file_path(repository_id),
            {"Revision": revision, "FilePath": filename},
        ),
        headers=credential_headers(credential),
        timeout=timeout_seconds,
    )
    if response.status_code != 200 or len(response.content) > MAX_CONFIG_BYTES:
        return None
    return _json_object(response.content)


def _url(endpoint: str, path: str, params: Mapping[str, str] | None) -> str:
    base = f"{endpoint.rstrip('/')}{path}"
    if not params:
        return base
    return f"{base}?{urlencode(params)}"


def _json_object(payload: bytes) -> Mapping[str, Any] | None:
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return document if isinstance(document, dict) else None


__all__ = [
    "CONFIG_FILENAME",
    "MAX_CONFIG_BYTES",
    "MODELSCOPE_API_PREFIX",
    "MODELSCOPE_OFFICIAL_ENDPOINT",
    "MODELSCOPE_USERS_INFO_PATH",
    "FileListing",
    "FileListingAbsent",
    "FileListingUnreadable",
    "ModelScopeCode",
    "ModelScopeReply",
    "credential_headers",
    "fetch_reply",
    "fetch_repository_document",
    "model_path",
    "parse_file_listing",
    "repository_file_path",
    "repository_files_path",
]
