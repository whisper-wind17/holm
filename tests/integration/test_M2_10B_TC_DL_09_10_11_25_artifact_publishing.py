"""TC-DL-09/10/11/25 and TC-RT-13: PostgreSQL-backed publication pipeline."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from conftest import EphemeralPostgres
from holm_common.config import StorageConfig
from holm_domain import (
    DOWNLOAD_RESOLUTION_JOB_TYPE,
    DownloadFailureReason,
    Identified,
    MetadataStatus,
    ModelArtifactStatus,
    ModelOriginType,
    RemoteFile,
    ResolvedModel,
    RevisionImmutability,
    Unrecognized,
)
from holm_domain.download_merge import (
    ArtifactFileManifestUnavailable,
    ContentsDiffer,
    ContentsMatch,
    compare_contents,
)
from holm_download_worker import (
    ArtifactHealthJudgement,
    ArtifactPublicationError,
    ArtifactRevalidator,
    ArtifactSystemCalls,
    ArtifactVerifier,
    DownloadArtifactPublisher,
    DownloadResolutionJobHandler,
    DownloadStagingLifecycle,
    DownloadSubprocessRequest,
    DownloadSubprocessResult,
    DownloadWorker,
    ResolutionSubprocessRequest,
    ResolutionSubprocessResult,
    RuntimeCapability,
)
from holm_persistence import (
    AsyncSessionFactory,
    JobDraft,
    JobRepository,
    create_database_engine,
    create_session_factory,
)
from holm_persistence.download_task_repository import DownloadTaskRepository
from holm_persistence.model_domain import DOWNLOAD_TASKS, MODEL_ARTIFACTS, MODELS
from holm_persistence.model_sources import MODEL_SOURCES
from migration_test_support import upgrade_head
from sqlalchemy.engine import URL, Engine
from sqlalchemy.ext.asyncio import AsyncEngine

REPOSITORY_ID: Final[str] = "fixture/publishing"
RESOLVED_REVISION: Final[str] = "a" * 40
REPOSITORY_HASH: Final[str] = hashlib.sha256(REPOSITORY_ID.encode("utf-8")).hexdigest()


def _async_url(fixture: EphemeralPostgres) -> URL:
    return URL.create(
        "postgresql+psycopg",
        username="ubuntu",
        host=str(fixture.socket_directory),
        port=fixture.port,
        database="postgres",
    )


def _safetensors_bytes() -> bytes:
    header = json.dumps(
        {
            "fixture.weight": {
                "data_offsets": [0, 4],
                "dtype": "F32",
                "shape": [1],
            }
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return len(header).to_bytes(8, "little") + header + b"data"


def _valid_payloads() -> dict[str, bytes]:
    return {
        "config.json": b'{"architectures":["FixtureForCausalLM"]}',
        "tokenizer.json": b'{"version":"1.0"}',
        "model.safetensors": _safetensors_bytes(),
        ".gitattributes": b"*.safetensors filter=lfs",
    }


def _remote_files(payloads: dict[str, bytes]) -> tuple[RemoteFile, ...]:
    return tuple(
        RemoteFile(path, len(content), Identified(hashlib.sha256(content).hexdigest()))
        for path, content in sorted(payloads.items())
    )


def _resolved(payloads: dict[str, bytes]) -> ResolvedModel:
    files = _remote_files(payloads)
    return ResolvedModel(
        source=ModelOriginType.HUGGING_FACE,
        repository_id=REPOSITORY_ID,
        requested_revision_norm="main",
        resolved_revision=RESOLVED_REVISION,
        revision_immutability=RevisionImmutability.IMMUTABLE,
        files=Identified(files),
        total_size_bytes=Identified(sum(value.size_bytes for value in files)),
        architecture=Identified("FixtureForCausalLM"),
        modalities=Unrecognized(),
        precision=Unrecognized(),
        quantization=Unrecognized(),
        context_length=Unrecognized(),
        candidate_runtimes=Unrecognized(),
        license=Unrecognized(),
    )


def _reset(engine: Engine) -> None:
    upgrade_head(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "TRUNCATE core.domain_events, core.component_heartbeats, core.jobs, "
            "core.download_subscriptions, core.download_tasks, core.model_artifacts, "
            "core.models, core.model_sources CASCADE"
        )


def _seed_downloading(engine: Engine) -> UUID:
    source_id = uuid4()
    model_id = uuid4()
    task_id = uuid4()
    with engine.begin() as connection:
        now = connection.exec_driver_sql("SELECT clock_timestamp()").scalar_one()
        connection.execute(
            sa.insert(MODEL_SOURCES).values(
                id=source_id,
                type=ModelOriginType.HUGGING_FACE.value,
                credential_key_id=None,
                credential_nonce=None,
                credential_ciphertext=None,
                credential_tag=None,
                verification_status=None,
                verified_at=None,
                verification_reason_code=None,
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            sa.insert(MODELS).values(
                id=model_id,
                source_id=source_id,
                source_type=ModelOriginType.HUGGING_FACE.value,
                source_key=REPOSITORY_ID,
                display_name="Publishing Fixture",
                repository_id=REPOSITORY_ID,
                external_path=None,
                architecture="FixtureForCausalLM",
                modalities=None,
                precision=None,
                quantization=None,
                context_length=None,
                supported_runtimes=None,
                estimated_size_bytes=None,
                license=None,
                metadata_status=MetadataStatus.CONFIRMED.value,
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            sa.insert(DOWNLOAD_TASKS).values(
                id=task_id,
                model_id=model_id,
                artifact_id=None,
                source_type=ModelOriginType.HUGGING_FACE.value,
                repository_id=REPOSITORY_ID,
                requested_revision_norm="main",
                resolved_revision=RESOLVED_REVISION,
                status="Downloading",
                attempt=1,
                downloaded_bytes=0,
                total_bytes=None,
                lease_owner=None,
                lease_until=None,
                staging_path=None,
                error_code=None,
                created_at=now,
                updated_at=now,
                completed_at=None,
            )
        )
    return task_id


class _StaticResolver:
    def __init__(self, resolved: ResolvedModel) -> None:
        self.resolved = resolved
        self.calls = 0

    async def resolve(self, request: ResolutionSubprocessRequest) -> ResolutionSubprocessResult:
        self.calls += 1
        assert request.source is self.resolved.source
        return ResolutionSubprocessResult(os.getpid(), self.resolved)


class _WritingDownloader:
    def __init__(self, payloads: dict[str, bytes], *, fifo: bool = False) -> None:
        self.payloads = payloads
        self.fifo = fifo
        self.calls = 0

    async def download(
        self, request: DownloadSubprocessRequest, progress: object
    ) -> DownloadSubprocessResult:
        self.calls += 1
        for relative, content in self.payloads.items():
            target = request.staging_path.joinpath(*relative.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        if self.fifo:
            os.mkfifo(request.staging_path / "sdk.fifo")
        callback = progress
        assert callable(callback)
        await callback(sum(map(len, self.payloads.values())))
        return DownloadSubprocessResult(os.getpid())


@dataclass
class _FailFirstCommit:
    calls: int = 0

    def __call__(self) -> None:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("injected database commit failure")


@dataclass
class _FailBundleReplaceOnce(ArtifactSystemCalls):
    replace_calls: int = 0

    def replace(self, source: Path, destination: Path) -> None:
        self.replace_calls += 1
        if self.replace_calls == 2:
            raise OSError("injected Bundle replace failure")
        super().replace(source, destination)


async def _enqueue(factory: AsyncSessionFactory, task_id: UUID) -> None:
    async with factory() as session, session.begin():
        await JobRepository(session).enqueue(
            JobDraft(
                job_type=DOWNLOAD_RESOLUTION_JOB_TYPE,
                payload={},
                target_object_id=task_id,
                target_object_version=1,
            )
        )


def _components(
    fixture: EphemeralPostgres,
    model_root: Path,
    resolved: ResolvedModel,
    downloader: _WritingDownloader,
    *,
    verifier: ArtifactVerifier | None = None,
    before_commit: _FailFirstCommit | None = None,
    system_calls: ArtifactSystemCalls | None = None,
) -> tuple[AsyncEngine, DownloadWorker, _StaticResolver]:
    async_engine = create_database_engine(_async_url(fixture))
    factory = create_session_factory(async_engine)
    storage = StorageConfig(
        model_root=model_root,
        tokenizer_root=model_root.parent / "tokenizers",
    )
    resolver = _StaticResolver(resolved)
    handler = DownloadResolutionJobHandler(session_factory=factory, resolver=resolver)
    handler.attach_staging_lifecycle(
        DownloadStagingLifecycle(session_factory=factory, storage=storage)
    )
    handler.attach_download_executor(
        downloader,
        hugging_face_endpoint="https://fixture.invalid",
        modelscope_endpoint="https://fixture.invalid",
    )
    handler.attach_artifact_publisher(
        DownloadArtifactPublisher(
            session_factory=factory,
            storage=storage,
            verifier=verifier,
            before_commit=before_commit,
            system_calls=system_calls,
        )
    )
    worker = DownloadWorker(
        session_factory=factory,
        handler=handler,
        owner="m2-10b-worker",
        build_version="m2-10b-test",
        lease_seconds=5,
        heartbeat_seconds=1,
    )
    return async_engine, worker, resolver


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("file_list", DownloadFailureReason.ARTIFACT_FILE_LIST_INVALID),
        ("hash", DownloadFailureReason.ARTIFACT_MANIFEST_HASH_FAILED),
        ("required", DownloadFailureReason.ARTIFACT_REQUIRED_FILES_INVALID),
        ("safetensors", DownloadFailureReason.ARTIFACT_SAFETENSORS_INVALID),
        ("runtime", DownloadFailureReason.ARTIFACT_RUNTIME_INCOMPATIBLE),
        ("unsafe", DownloadFailureReason.ARTIFACT_UNSAFE_FILE),
        ("remote_code", DownloadFailureReason.REMOTE_CODE_NOT_ALLOWED),
    ],
)
def test_TC_DL_09_each_validation_branch_fails_task_without_artifact(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
    mutation: str,
    reason: DownloadFailureReason,
) -> None:
    _reset(ephemeral_postgres.engine)
    task_id = _seed_downloading(ephemeral_postgres.engine)
    payloads = _valid_payloads()
    source_payloads = dict(payloads)
    fifo = mutation == "unsafe"
    verifier: ArtifactVerifier | None = None
    if mutation == "file_list":
        source_payloads["missing.bin"] = b"missing"
    elif mutation == "hash":
        resolved = _resolved(source_payloads)
        assert isinstance(resolved.files, Identified)
        first = resolved.files.value[0]
        wrong = replace(first, content_sha256=Identified("0" * 64))
        resolved = replace(resolved, files=Identified((wrong, *resolved.files.value[1:])))
    elif mutation == "required":
        payloads.pop("tokenizer.json")
        source_payloads = dict(payloads)
    elif mutation == "safetensors":
        payloads["model.safetensors"] = b"corrupt"
        source_payloads = dict(payloads)
    elif mutation == "runtime":
        verifier = ArtifactVerifier(
            (RuntimeCapability("fixture", frozenset({"AnotherArchitecture"})),)
        )
    elif mutation == "remote_code":
        payloads["config.json"] = (
            b'{"architectures":["FixtureForCausalLM"],"auto_map":{"AutoModel":"model.py"}}'
        )
        source_payloads = dict(payloads)
    if mutation != "hash":
        resolved = _resolved(source_payloads)
    downloader = _WritingDownloader(payloads, fifo=fifo)
    async_engine, worker, _ = _components(
        ephemeral_postgres,
        tmp_path / mutation,
        resolved,
        downloader,
        verifier=verifier,
    )

    async def run() -> None:
        factory = create_session_factory(async_engine)
        await _enqueue(factory, task_id)
        result = await worker.run_once()
        assert result is not None and result.completed
        await async_engine.dispose()

    asyncio.run(run())
    with ephemeral_postgres.engine.connect() as connection:
        task = (
            connection.execute(
                sa.select(DOWNLOAD_TASKS.c.status, DOWNLOAD_TASKS.c.error_code).where(
                    DOWNLOAD_TASKS.c.id == task_id
                )
            )
            .mappings()
            .one()
        )
        artifact_count = connection.execute(
            sa.select(sa.func.count()).select_from(MODEL_ARTIFACTS)
        ).scalar_one()
    assert task == {"status": "Failed", "error_code": reason.value}
    assert artifact_count == 0
    assert not (
        tmp_path / mutation / "managed" / "hugging-face" / REPOSITORY_HASH / RESOLVED_REVISION
    ).exists()


def test_TC_RT_13_bundle_failure_before_database_commit_never_creates_ready_artifact(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
) -> None:
    _reset(ephemeral_postgres.engine)
    task_id = _seed_downloading(ephemeral_postgres.engine)
    payloads = _valid_payloads()
    downloader = _WritingDownloader(payloads)
    calls = _FailBundleReplaceOnce()
    model_root = tmp_path / "models"
    async_engine, worker, resolver = _components(
        ephemeral_postgres,
        model_root,
        _resolved(payloads),
        downloader,
        system_calls=calls,
    )
    destination = model_root / "managed" / "hugging-face" / REPOSITORY_HASH / RESOLVED_REVISION
    tokenizer_root = model_root.parent / "tokenizers"

    async def run() -> None:
        factory = create_session_factory(async_engine)
        await _enqueue(factory, task_id)
        with pytest.raises(ArtifactPublicationError) as captured:
            await worker.run_once()
        assert captured.value.renamed is True
        with ephemeral_postgres.engine.connect() as connection:
            assert (
                connection.execute(
                    sa.select(sa.func.count()).select_from(MODEL_ARTIFACTS)
                ).scalar_one()
                == 0
            )
            task = (
                connection.execute(
                    sa.select(DOWNLOAD_TASKS.c.status, DOWNLOAD_TASKS.c.artifact_id).where(
                        DOWNLOAD_TASKS.c.id == task_id
                    )
                )
                .mappings()
                .one()
            )
        assert task == {"status": "Publishing", "artifact_id": None}
        assert destination.is_dir()
        assert not tuple(path for path in tokenizer_root.iterdir() if path.name != ".staging")

        with ephemeral_postgres.engine.begin() as connection:
            connection.exec_driver_sql(
                "UPDATE core.jobs SET run_after = clock_timestamp() WHERE target_object_id = %s",
                (task_id,),
            )
        result = await worker.run_once()
        assert result is not None and result.completed
        await async_engine.dispose()

    asyncio.run(run())
    assert calls.replace_calls == 3
    assert downloader.calls == 1
    assert resolver.calls == 1
    with ephemeral_postgres.engine.connect() as connection:
        artifact = connection.execute(sa.select(MODEL_ARTIFACTS)).mappings().one()
    bundle = Path(str(artifact["tokenizer_bundle_path"]))
    assert bundle == tokenizer_root / str(artifact["id"])
    assert bundle.is_dir()


def test_TC_DL_10_11_25_TC_RT_13_publish_recovery_and_external_revalidation_is_read_only(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
) -> None:
    _reset(ephemeral_postgres.engine)
    task_id = _seed_downloading(ephemeral_postgres.engine)
    payloads = _valid_payloads()
    resolved = _resolved(payloads)
    downloader = _WritingDownloader(payloads)
    fail_first = _FailFirstCommit()
    model_root = tmp_path / "models"
    async_engine, worker, resolver = _components(
        ephemeral_postgres,
        model_root,
        resolved,
        downloader,
        before_commit=fail_first,
    )
    destination = model_root / "managed" / "hugging-face" / REPOSITORY_HASH / RESOLVED_REVISION
    tokenizer_root = model_root.parent / "tokenizers"

    async def run() -> tuple[UUID, ArtifactHealthJudgement]:
        factory = create_session_factory(async_engine)
        await _enqueue(factory, task_id)
        with pytest.raises(RuntimeError, match="injected database commit failure"):
            await worker.run_once()

        with ephemeral_postgres.engine.connect() as connection:
            first_task = (
                connection.execute(
                    sa.select(DOWNLOAD_TASKS.c.status, DOWNLOAD_TASKS.c.artifact_id).where(
                        DOWNLOAD_TASKS.c.id == task_id
                    )
                )
                .mappings()
                .one()
            )
            first_artifacts = connection.execute(
                sa.select(sa.func.count()).select_from(MODEL_ARTIFACTS)
            ).scalar_one()
        assert first_task == {"status": "Publishing", "artifact_id": None}
        assert first_artifacts == 0
        assert destination.is_dir()
        assert not (model_root / ".staging" / str(task_id)).exists()
        tokenizer_publications = tuple(
            path for path in tokenizer_root.iterdir() if path.name != ".staging"
        )
        assert len(tokenizer_publications) == 1
        tokenizer_publication = tokenizer_publications[0]
        tokenizer_snapshot = tuple(
            sorted(
                (path.relative_to(tokenizer_publication).as_posix(), path.read_bytes())
                for path in tokenizer_publication.rglob("*")
                if path.is_file()
            )
        )

        with ephemeral_postgres.engine.begin() as connection:
            connection.exec_driver_sql(
                "UPDATE core.jobs SET run_after = clock_timestamp() WHERE target_object_id = %s",
                (task_id,),
            )
        result = await worker.run_once()
        assert result is not None and result.completed
        assert (
            tuple(
                sorted(
                    (path.relative_to(tokenizer_publication).as_posix(), path.read_bytes())
                    for path in tokenizer_publication.rglob("*")
                    if path.is_file()
                )
            )
            == tokenizer_snapshot
        )

        async with factory() as session:
            task = await DownloadTaskRepository(session).lock_resolution_work(task_id)
            assert task is not None and task.status.value == "Completed"
            assert task.staging_path is None
            row = (
                (
                    await session.execute(
                        sa.select(MODEL_ARTIFACTS.c.id).where(
                            MODEL_ARTIFACTS.c.id == task.id  # deliberately false identity probe
                        )
                    )
                )
                .scalars()
                .one_or_none()
            )
            assert row is None, "Artifact and Download Task identities were incorrectly conflated"
            artifact_id = (
                await session.execute(
                    sa.select(MODEL_ARTIFACTS.c.id).where(
                        MODEL_ARTIFACTS.c.path == str(destination)
                    )
                )
            ).scalar_one()
            bundle_pair = (
                await session.execute(
                    sa.select(
                        MODEL_ARTIFACTS.c.tokenizer_bundle_hash,
                        MODEL_ARTIFACTS.c.tokenizer_bundle_path,
                    ).where(MODEL_ARTIFACTS.c.id == artifact_id)
                )
            ).one()
            tokenizer_publication = tokenizer_root / str(artifact_id)
            bundle_manifest = (tokenizer_publication / "manifest.json").read_bytes()
            assert bundle_pair == (
                hashlib.sha256(bundle_manifest).hexdigest(),
                str(tokenizer_publication),
            )
            artifact_manifest = await DownloadTaskRepository(session).artifact_file_manifest(
                artifact_id
            )
            assert isinstance(artifact_manifest, Identified)
            assert isinstance(compare_contents(resolved.files, artifact_manifest), ContentsMatch)
            assert isinstance(resolved.files, Identified)
            changed = replace(
                resolved,
                files=Identified(
                    (
                        replace(
                            resolved.files.value[0],
                            content_sha256=Identified("f" * 64),
                        ),
                        *resolved.files.value[1:],
                    )
                ),
            )
            assert isinstance(compare_contents(changed.files, artifact_manifest), ContentsDiffer)

        manifest_path = destination / "manifest.json"
        saved_manifest = manifest_path.read_bytes()
        manifest_path.unlink()
        async with factory() as session:
            missing = await DownloadTaskRepository(session).artifact_file_manifest(artifact_id)
        assert isinstance(missing, ArtifactFileManifestUnavailable)
        manifest_path.write_bytes(saved_manifest)

        with ephemeral_postgres.engine.begin() as connection:
            connection.execute(
                sa.update(MODEL_ARTIFACTS)
                .where(MODEL_ARTIFACTS.c.id == artifact_id)
                .values(
                    storage_type="External",
                    tokenizer_bundle_hash=None,
                    tokenizer_bundle_path=None,
                )
            )
        damaged_path = destination / "model.safetensors"
        damaged_content = b"external owner damaged this file"
        damaged_path.write_bytes(damaged_content)
        before_entries = tuple(
            sorted(path.relative_to(destination).as_posix() for path in destination.rglob("*"))
        )
        before_mtime = damaged_path.stat().st_mtime_ns
        judgement = await ArtifactRevalidator(session_factory=factory).revalidate(
            artifact_id, full=True
        )
        assert damaged_path.read_bytes() == damaged_content
        assert damaged_path.stat().st_mtime_ns == before_mtime
        assert (
            tuple(
                sorted(path.relative_to(destination).as_posix() for path in destination.rglob("*"))
            )
            == before_entries
        )
        await async_engine.dispose()
        return artifact_id, judgement

    artifact_id, judgement = asyncio.run(run())
    tokenizer_publication = tokenizer_root / str(artifact_id)
    assert downloader.calls == 1
    assert resolver.calls == 1
    assert fail_first.calls == 2
    assert destination.is_dir()
    assert tuple((model_root / "managed").iterdir()) == (model_root / "managed" / "hugging-face",)
    assert judgement.status is ModelArtifactStatus.CORRUPTED
    with ephemeral_postgres.engine.connect() as connection:
        artifacts = connection.execute(sa.select(MODEL_ARTIFACTS)).mappings().all()
        task = (
            connection.execute(
                sa.select(DOWNLOAD_TASKS.c.status, DOWNLOAD_TASKS.c.artifact_id).where(
                    DOWNLOAD_TASKS.c.id == task_id
                )
            )
            .mappings()
            .one()
        )
    assert len(artifacts) == 1 and artifacts[0]["id"] == artifact_id
    assert artifact_id != task_id
    assert {
        path.relative_to(tokenizer_publication).as_posix()
        for path in tokenizer_publication.rglob("*")
        if path.is_file()
    } == {"config.json", "manifest.json", "tokenizer.json"}
    assert artifacts[0]["status"] == "Corrupted"
    assert artifacts[0]["storage_type"] == "External"
    assert artifacts[0]["tokenizer_bundle_hash"] is None
    assert artifacts[0]["tokenizer_bundle_path"] is None
    assert task == {"status": "Completed", "artifact_id": artifact_id}


def test_TC_RT_13_external_artifact_keeps_null_bundle_and_never_copies_source(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
) -> None:
    _reset(ephemeral_postgres.engine)
    task_id = _seed_downloading(ephemeral_postgres.engine)
    external_root = tmp_path / "external"
    external_root.mkdir()
    payloads = _valid_payloads()
    for relative, content in payloads.items():
        (external_root / relative).write_bytes(content)
    verified = ArtifactVerifier().verify(external_root, _resolved(payloads))
    (external_root / "manifest.json").write_bytes(verified.manifest_bytes)
    artifact_id = uuid4()
    with ephemeral_postgres.engine.begin() as connection:
        model_id = connection.execute(
            sa.select(DOWNLOAD_TASKS.c.model_id).where(DOWNLOAD_TASKS.c.id == task_id)
        ).scalar_one()
        now = connection.exec_driver_sql("SELECT clock_timestamp()").scalar_one()
        connection.execute(
            sa.insert(MODEL_ARTIFACTS).values(
                id=artifact_id,
                model_id=model_id,
                source_type=ModelOriginType.HUGGING_FACE.value,
                repository_id=f"{REPOSITORY_ID}/external",
                resolved_revision="b" * 40,
                storage_type="External",
                status="Ready",
                path=str(external_root),
                manifest_hash=verified.manifest_hash,
                tokenizer_bundle_hash=None,
                tokenizer_bundle_path=None,
                size_bytes=verified.size_bytes,
                created_at=now,
                updated_at=now,
            )
        )
    before = tuple(
        sorted(
            (path.relative_to(external_root).as_posix(), path.read_bytes(), path.stat().st_mtime_ns)
            for path in external_root.rglob("*")
            if path.is_file()
        )
    )
    tokenizer_root = tmp_path / "tokenizers"

    async def run() -> ArtifactHealthJudgement:
        async_engine = create_database_engine(_async_url(ephemeral_postgres))
        try:
            return await ArtifactRevalidator(
                session_factory=create_session_factory(async_engine)
            ).revalidate(artifact_id, full=True)
        finally:
            await async_engine.dispose()

    judgement = asyncio.run(run())
    after = tuple(
        sorted(
            (path.relative_to(external_root).as_posix(), path.read_bytes(), path.stat().st_mtime_ns)
            for path in external_root.rglob("*")
            if path.is_file()
        )
    )
    assert judgement.status is ModelArtifactStatus.READY
    assert after == before
    assert not tokenizer_root.exists()
    with ephemeral_postgres.engine.connect() as connection:
        bundle_pair = connection.execute(
            sa.select(
                MODEL_ARTIFACTS.c.tokenizer_bundle_hash,
                MODEL_ARTIFACTS.c.tokenizer_bundle_path,
            ).where(MODEL_ARTIFACTS.c.id == artifact_id)
        ).one()
    assert bundle_pair == (None, None)

    changed = list(after)
    changed[0] = (changed[0][0], b"mutated", changed[0][2])
    with pytest.raises(AssertionError):
        assert tuple(changed) == before


@pytest.mark.parametrize(
    ("partition_fields", "expected"),
    [
        ({"num_attention_heads": 16, "num_key_value_heads": 4}, (16, 4)),
        ({}, (None, None)),
        ({"num_attention_heads": "16", "num_key_value_heads": 4.0}, (None, None)),
        ({"num_attention_heads": 0, "num_key_value_heads": 0}, (None, None)),
        ({"num_attention_heads": -16, "num_key_value_heads": -4}, (None, None)),
    ],
)
def test_M3_03A_artifact_publication_persists_only_positive_integer_head_counts(
    ephemeral_postgres: EphemeralPostgres,
    tmp_path: Path,
    partition_fields: dict[str, object],
    expected: tuple[int | None, int | None],
) -> None:
    _reset(ephemeral_postgres.engine)
    task_id = _seed_downloading(ephemeral_postgres.engine)
    payloads = _valid_payloads()
    configuration: dict[str, object] = {"architectures": ["FixtureForCausalLM"]}
    configuration.update(partition_fields)
    payloads["config.json"] = json.dumps(configuration).encode("utf-8")
    async_engine, worker, _ = _components(
        ephemeral_postgres,
        tmp_path / "models",
        _resolved(payloads),
        _WritingDownloader(payloads),
    )

    async def run() -> None:
        await _enqueue(create_session_factory(async_engine), task_id)
        result = await worker.run_once()
        assert result is not None and result.completed
        await async_engine.dispose()

    asyncio.run(run())
    with ephemeral_postgres.engine.connect() as connection:
        persisted = connection.execute(
            sa.select(MODEL_ARTIFACTS.c.attention_heads, MODEL_ARTIFACTS.c.kv_heads)
        ).one()
    assert persisted == expected
