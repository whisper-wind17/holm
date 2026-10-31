"""TC-DL-26/27: what the Hugging Face Adapter refuses, and what it never leaks.

Both cases run the real ``huggingface_hub`` SDK against the fake Hub that
replays ``FX-MS-08``; nothing inside the SDK is patched.  The positive controls
break the **production** module and reload it, so each guard is shown failing on
real code rather than on a hand-built structure.
"""

from __future__ import annotations

import ast
import asyncio
import json
import logging
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Final

import pytest
from fixtures.fake_model_source import PathAttackKind, path_attack_samples
from fixtures.hugging_face import (
    MISSING_REPOSITORY_ID,
    PUBLIC_REPOSITORY_ID,
    CredentialPresence,
    FakeHub,
    fake_hugging_face_hub,
)
from fixtures.source_mutation import load_mutated_module, mutate_source
from holm_control_api.hugging_face_resolution import (
    HUGGING_FACE_OFFICIAL_ENDPOINT,
    HuggingFaceResolutionAdapter,
)
from holm_control_api.model_resolution import (
    ModelResolutionRegistry,
    ResolutionFailed,
    ResolutionRequest,
    ResolutionSucceeded,
)
from holm_control_api.model_resolution_routes import ResolveRequest
from holm_control_api.model_resolution_service import (
    ModelResolutionService,
    ResolveInputRejected,
)
from holm_domain import (
    ModelOriginType,
    ModelSourceValidationAction,
    ModelSourceValidationReason,
)
from holm_persistence import DEFAULT_REQUESTED_REVISION
from holm_security import MasterKey, MasterKeyring, MasterKeyState
from huggingface_hub import constants

REPOSITORY_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
ADAPTER_SOURCE: Final[Path] = (
    REPOSITORY_ROOT / "apps/control_api/src/holm_control_api/hugging_face_resolution.py"
)

KEYRING: Final = MasterKeyring((MasterKey("tc-dl-26", MasterKeyState.WRITABLE, b"K" * 32),))

# FX-MS-06 has two kinds of sample.  Three of them describe a *Repository ID*
# and must be refused before any source is contacted.  The other five describe
# an *entry inside a repository* -- a device node, a FIFO, a socket, a setuid
# file, an escaping symlink -- which only exists once files are being verified
# and published.  That half belongs to M2-05/M2-09 and to M2-07's External Path
# checks, and is registered in the development backlog.  The partition is
# asserted to be exhaustive so a new FX-MS-06 kind cannot slip through unclaimed.
REJECTED_AS_REPOSITORY_ID: Final[frozenset[PathAttackKind]] = frozenset(
    {
        PathAttackKind.INVALID_REPOSITORY_ID,
        PathAttackKind.PARENT_TRAVERSAL,
        PathAttackKind.ABSOLUTE_PATH,
    }
)
DEFERRED_TO_DOWNLOAD_VERIFICATION: Final[frozenset[PathAttackKind]] = frozenset(
    {
        PathAttackKind.ESCAPING_SYMBOLIC_LINK,
        PathAttackKind.DEVICE_FILE,
        PathAttackKind.FIFO,
        PathAttackKind.SOCKET,
        PathAttackKind.SETUID_FILE,
    }
)

ADDRESS_SHAPED_REPOSITORY_IDS: Final[tuple[str, ...]] = (
    "https://huggingface.co/Qwen/Qwen2.5-7B-Instruct",
    "http://127.0.0.1:8080/owner/model",
    "//evil.example/owner/model",
    "owner/model?endpoint=https://evil.example",
    "file:///etc/passwd",
    "owner/model#https://evil.example",
    "evil.example:443/owner/model",
)

SENTINEL_SINK_NAMES: Final[tuple[str, ...]] = (
    "HF_TOKEN",
    "HUGGING_FACE_HUB_TOKEN",
    "HF_HUB_TOKEN",
)


@pytest.fixture
def hub() -> Iterator[FakeHub]:
    with fake_hugging_face_hub() as running:
        yield running


def _exploding_session_factory() -> object:
    def factory(*arguments: object, **keywords: object) -> object:
        raise AssertionError("a rejected input must never reach the database")

    return factory


def _service(registry: ModelResolutionRegistry) -> ModelResolutionService:
    return ModelResolutionService(
        session_factory=_exploding_session_factory(),  # type: ignore[arg-type]
        keyring=KEYRING,
        registry=registry,
    )


def _endpoint_keywords(source: str) -> list[ast.expr]:
    tree = ast.parse(source)
    return [
        keyword.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for keyword in node.keywords
        if keyword.arg == "endpoint"
    ]


def assert_the_endpoint_is_never_taken_from_a_request(source: str) -> None:
    """Every SDK call must address ``self._endpoint`` and nothing else."""

    keywords = _endpoint_keywords(source)
    assert keywords, "the Adapter must pass an explicit endpoint to the SDK"
    for value in keywords:
        assert isinstance(value, ast.Attribute), "endpoint must be the Adapter's own attribute"
        assert value.attr == "_endpoint"
        assert isinstance(value.value, ast.Name) and value.value.id == "self"


# ------------------------------------------------------------------ TC-DL-26
def test_TC_DL_26_fx_ms_06_partition_is_exhaustive() -> None:
    kinds = {sample.kind for sample in path_attack_samples()}
    assert kinds == set(PathAttackKind)
    assert kinds == REJECTED_AS_REPOSITORY_ID | DEFERRED_TO_DOWNLOAD_VERIFICATION
    assert not (REJECTED_AS_REPOSITORY_ID & DEFERRED_TO_DOWNLOAD_VERIFICATION)


def test_TC_DL_26_hostile_repository_ids_are_refused_before_the_source(hub: FakeHub) -> None:
    adapter = HuggingFaceResolutionAdapter(endpoint=hub.endpoint)
    service = _service(ModelResolutionRegistry({ModelOriginType.HUGGING_FACE: adapter}))
    hostile = (
        tuple(
            sample.untrusted_value
            for sample in path_attack_samples()
            if sample.kind in REJECTED_AS_REPOSITORY_ID
        )
        + ADDRESS_SHAPED_REPOSITORY_IDS
    )

    async def scenario() -> None:
        for untrusted in hostile:
            with pytest.raises(ResolveInputRejected) as failure:
                await service.resolve(
                    source=ModelOriginType.HUGGING_FACE,
                    repository_id=untrusted,
                    external_path=None,
                    revision=None,
                )
            # The refusal never quotes the attacker's input back.
            assert untrusted not in str(failure.value)

    asyncio.run(scenario())
    # No source was contacted at all: the rejection is upstream of the network.
    assert hub.requests == ()


def test_TC_DL_26_no_request_can_choose_an_address(hub: FakeHub) -> None:
    assert "endpoint" not in ResolveRequest.model_fields
    assert "external_path" in ResolveRequest.model_fields  # the only path-shaped field
    assert HUGGING_FACE_OFFICIAL_ENDPOINT == "https://huggingface.co"
    assert HuggingFaceResolutionAdapter().endpoint == HUGGING_FACE_OFFICIAL_ENDPOINT
    # The Adapter's address comes from its construction, not from the caller.
    assert HuggingFaceResolutionAdapter(endpoint=hub.endpoint).endpoint == hub.endpoint
    assert_the_endpoint_is_never_taken_from_a_request(ADAPTER_SOURCE.read_text(encoding="utf-8"))


def test_TC_DL_26_positive_control_a_request_supplied_endpoint_fails_the_guard() -> None:
    mutated = mutate_source(
        ADAPTER_SOURCE,
        [
            (
                "                revision=commit,\n                endpoint=self._endpoint,",
                "                revision=commit,\n                endpoint=request.repository_id,",
            )
        ],
    )
    with pytest.raises(AssertionError):
        assert_the_endpoint_is_never_taken_from_a_request(mutated)


def test_TC_DL_26_external_path_is_refused_rather_than_previewed(hub: FakeHub) -> None:
    """``realpath``/Allowlist/Runtime-UID checks are M2-07's; refusing is not."""

    service = _service(
        ModelResolutionRegistry(
            {ModelOriginType.HUGGING_FACE: HuggingFaceResolutionAdapter(endpoint=hub.endpoint)}
        )
    )
    preview = asyncio.run(
        service.resolve(
            source=ModelOriginType.LOCAL_PATH,
            repository_id=None,
            external_path="/srv/models/../../etc",
            revision=None,
        )
    )
    assert isinstance(preview.outcome, ResolutionFailed)
    assert preview.local_availability is None
    assert preview.outcome.result.status is None
    assert preview.outcome.result.reason_code is ModelSourceValidationReason.VALIDATOR_UNAVAILABLE
    assert preview.outcome.result.next_action is ModelSourceValidationAction.INSTALL_VALIDATOR
    assert hub.requests == ()


# ------------------------------------------------------------------ TC-DL-27
def _sentinel() -> str:
    return "hf_PWSSNTLSRCTOKTCDL27" + os.urandom(8).hex().upper()


def _hub_cache_surfaces() -> tuple[str, ...]:
    """Every SDK-owned on-disk surface a token could be written to."""

    surfaces: list[str] = []
    for candidate in (
        Path(constants.HF_TOKEN_PATH),
        Path(constants.HF_HUB_CACHE),
        Path(constants.HF_HOME),
        Path.home() / ".cache" / "huggingface",
        Path.home() / ".huggingface",
    ):
        if candidate.is_file():
            surfaces.append(candidate.read_text(encoding="utf-8", errors="replace"))
        elif candidate.is_dir():
            for path in candidate.rglob("*"):
                surfaces.append(str(path))
                if path.is_file() and path.stat().st_size < 1 << 20:
                    surfaces.append(path.read_text(encoding="utf-8", errors="replace"))
    return tuple(surfaces)


def _process_surfaces(caplog: pytest.LogCaptureFixture) -> tuple[str, ...]:
    rendered_logs = "\n".join(
        f"{record.getMessage()} {record.__dict__!r}" for record in caplog.records
    )
    return (
        rendered_logs,
        Path("/proc/self/cmdline").read_bytes().decode("utf-8", errors="replace"),
        json.dumps(dict(os.environ)),
        *_hub_cache_surfaces(),
    )


def test_TC_DL_27_a_sentinel_credential_reaches_the_source_and_no_sink(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    sentinel = _sentinel()
    caplog.set_level(logging.DEBUG)
    scratch_root = tmp_path / "scratch"
    scratch_root.mkdir()
    conclusions: list[str] = []

    with fake_hugging_face_hub(valid_token=sentinel) as hub:
        adapter = HuggingFaceResolutionAdapter(endpoint=hub.endpoint, scratch_root=scratch_root)

        async def scenario() -> None:
            succeeded = await adapter.resolve(
                ResolutionRequest(
                    source=ModelOriginType.HUGGING_FACE,
                    repository_id=PUBLIC_REPOSITORY_ID,
                    requested_revision_norm=DEFAULT_REQUESTED_REVISION,
                ),
                credential=sentinel.encode("utf-8"),
            )
            assert isinstance(succeeded, ResolutionSucceeded)
            conclusions.append(repr(succeeded))

            # The failing half: the same credential against a repository the
            # Hub reports as missing.
            failed = await adapter.resolve(
                ResolutionRequest(
                    source=ModelOriginType.HUGGING_FACE,
                    repository_id=MISSING_REPOSITORY_ID,
                    requested_revision_norm=DEFAULT_REQUESTED_REVISION,
                ),
                credential=sentinel.encode("utf-8"),
            )
            assert isinstance(failed, ResolutionFailed)
            conclusions.append(repr(failed))
            conclusions.append(str(failed))

        asyncio.run(scenario())

        # Not vacuous: the credential really did travel to the source boundary,
        # and the source really did answer both halves.
        presences = {request.credential for request in hub.requests}
        assert presences == {CredentialPresence.VALID}

    for surface in (*_process_surfaces(caplog), *conclusions):
        assert sentinel not in surface
    for name in SENTINEL_SINK_NAMES:
        assert name not in os.environ
    assert list(scratch_root.iterdir()) == []


def test_TC_DL_27_positive_control_a_leak_into_a_log_or_the_environment_is_caught(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    """Break production two ways; the same scan must catch both."""

    controls = (
        (
            "log",
            (
                "            api = HfApi(endpoint=self._endpoint, token=token)",
                '            __import__("logging").getLogger("hf").error("token=%s", token)\n'
                "            api = HfApi(endpoint=self._endpoint, token=token)",
            ),
        ),
        (
            "environment",
            (
                "            api = HfApi(endpoint=self._endpoint, token=token)",
                '            __import__("os").environ["HF_TOKEN"] = str(token)\n'
                "            api = HfApi(endpoint=self._endpoint, token=token)",
            ),
        ),
    )
    for label, replacement in controls:
        sentinel = _sentinel()
        caplog.clear()
        caplog.set_level(logging.DEBUG)
        module = load_mutated_module(
            ADAPTER_SOURCE,
            [replacement],
            module_name=f"_leaking_hugging_face_resolution_{label}",
            destination=tmp_path / f"leaking_{label}.py",
        )
        scratch_root = tmp_path / f"scratch-{label}"
        scratch_root.mkdir()
        try:
            with fake_hugging_face_hub(valid_token=sentinel) as hub:
                adapter = module.HuggingFaceResolutionAdapter(
                    endpoint=hub.endpoint,
                    scratch_root=scratch_root,
                )
                asyncio.run(
                    adapter.resolve(
                        ResolutionRequest(
                            source=ModelOriginType.HUGGING_FACE,
                            repository_id=PUBLIC_REPOSITORY_ID,
                            requested_revision_norm=DEFAULT_REQUESTED_REVISION,
                        ),
                        credential=sentinel.encode("utf-8"),
                    )
                )
            leaked = any(sentinel in surface for surface in _process_surfaces(caplog))
            assert leaked, f"the {label} sink scan did not catch a real leak"
        finally:
            os.environ.pop("HF_TOKEN", None)


def test_TC_DL_27_production_never_writes_a_token_bearing_environment_or_file() -> None:
    source = ADAPTER_SOURCE.read_text(encoding="utf-8")
    for forbidden in ("os.environ", "HF_TOKEN", "HUGGING_FACE_HUB_TOKEN", "login("):
        assert f"{forbidden}[" not in source
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute):
            assert node.value.attr != "environ", (
                "the Adapter must not write an environment variable"
            )
    # ``token=None`` would let the SDK pick an ambient credential up from the
    # environment or from the user's Hub login file; ``False`` means "no auth".
    assert "False if credential is None else credential.decode" in source
