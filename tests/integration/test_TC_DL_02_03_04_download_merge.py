"""FX-MS-01/M2-08B v1: deterministic resolution metadata, real PostgreSQL.

Source immutability/hash shapes mirror the certified HF/ModelScope contracts;
these are orchestration fixtures, not new live-source or GPU certification.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from conftest import EphemeralPostgres
from fixtures.asgi_client import SessionCookies
from fixtures.source_mutation import load_mutated_module
from holm_control_api import download_creation_service as creation_module
from holm_control_api import download_resolution_merge_service as merge_module
from holm_control_api.download_resolution_merge_service import (
    DownloadMergeContended,
    DownloadMergeRejected,
    DownloadResolutionMergeService,
)
from holm_control_api.model_resolution import ResolutionRequest, ResolutionSucceeded
from holm_domain import Identified, ModelOriginType, RemoteFile, ResolvedModel
from holm_domain.download_merge import (
    ComparisonUnavailable,
    ComparisonUnavailableReason,
    DownloadCompleted,
    DownloadMerged,
    DownloadStarted,
)
from holm_domain.model_resolution import RevisionImmutability
from holm_persistence import database_utc_now
from holm_persistence.download_task_repository import (
    DownloadIdentityConflict,
    DownloadTaskRepository,
)
from holm_persistence.model_domain import DOWNLOAD_TASKS, MODEL_ARTIFACTS
from test_TC_DL_01_download_creation import Environment, create, environment, rows
from test_TC_MDL_01_10_11_13_model_catalog_api import PASSWORD, _cookie, _truncate
from test_TC_MDL_05_07_model_registration_concurrency import _prepare, _RaceAdapter

ROOT = Path(__file__).resolve().parents[2]
REPOSITORY_SOURCE = ROOT / "packages/persistence/src/holm_persistence/download_task_repository.py"
MERGE_SOURCE = ROOT / "apps/control_api/src/holm_control_api/download_resolution_merge_service.py"
DOMAIN_SOURCE = ROOT / "packages/domain/src/holm_domain/download_merge.py"
SOURCES = [ModelOriginType.HUGGING_FACE, ModelOriginType.MODELSCOPE]


@pytest.fixture(autouse=True)
def empty_database(ephemeral_postgres: EphemeralPostgres) -> Iterator[None]:
    _prepare(ephemeral_postgres.engine)
    _truncate(ephemeral_postgres.engine)
    yield
    _truncate(ephemeral_postgres.engine)


async def resolving(
    env: Environment, source: ModelOriginType, revision: str = "branch-one"
) -> tuple[UUID, ResolvedModel]:
    created = await create(env, key=revision, source=source, revision=revision)
    outcome = await _RaceAdapter().resolve(
        ResolutionRequest(source, "Owner/Model", revision), credential=None
    )
    assert isinstance(outcome, ResolutionSucceeded)
    resolved = outcome.resolved
    if source is ModelOriginType.MODELSCOPE:
        resolved = replace(
            resolved,
            revision_immutability=RevisionImmutability.MUTABLE_REFERENCE,
            resolved_revision="master",
            files=Identified((RemoteFile("config.json", 2, Identified("a" * 64)),)),
        )
    async with env.factory() as session:
        await session.execute(
            sa.update(DOWNLOAD_TASKS)
            .where(DOWNLOAD_TASKS.c.id == created.task.id)
            .values(status="Resolving")
        )
        await session.commit()
    return created.task.id, resolved


async def artifact(env: Environment, task_id: UUID, resolved: ResolvedModel) -> UUID:
    artifact_id = uuid4()
    async with env.factory() as session:
        task = await DownloadTaskRepository(session).get(task_id)
        assert task is not None
        now = await database_utc_now(session)
        await session.execute(
            sa.insert(MODEL_ARTIFACTS).values(
                id=artifact_id,
                model_id=task.model_id,
                source_type=resolved.source.value,
                repository_id=resolved.repository_id,
                resolved_revision=resolved.resolved_revision,
                storage_type="Managed",
                status="Ready",
                path="/fixture/managed/model",
                manifest_hash="b" * 64,
                size_bytes=2,
                created_at=now,
                updated_at=now,
            )
        )
        await session.commit()
    return artifact_id


async def artifact_scenario(
    env: Environment,
    fixture: EphemeralPostgres,
    source: ModelOriginType,
    service: Any = None,
) -> None:
    task_id, resolved = await resolving(env, source)
    artifact_id = await artifact(env, task_id, resolved)
    service = service or DownloadResolutionMergeService(session_factory=env.factory)
    result = await service.merge(task_id, resolved)
    task = rows(fixture, "download_tasks")[0]
    if source is ModelOriginType.HUGGING_FACE:
        assert isinstance(result, DownloadCompleted)
        assert result.artifact_id == artifact_id
        assert task["status"] == "Completed" and task["artifact_id"] == artifact_id
        assert task["completed_at"] is not None
    else:
        assert isinstance(result, DownloadStarted)
        assert task["status"] == "Downloading" and task["artifact_id"] is None
        assert task["completed_at"] is None
    assert task["error_code"] is None
    assert len(rows(fixture, "download_subscriptions")) == 1
    assert rows(fixture, "download_subscriptions")[0]["task_id"] == task_id
    before = rows(fixture, "download_tasks")
    for _ in range(3):
        assert await service.merge(task_id, resolved) == result
    assert rows(fixture, "download_tasks") == before


@pytest.mark.parametrize("source", SOURCES)
def test_TC_DL_03_ready_artifact_and_replay(
    ephemeral_postgres: EphemeralPostgres,
    source: ModelOriginType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    comparisons: list[Any] = []
    original = vars(merge_module)["compare_contents"]

    def observed(*args: Any) -> Any:
        result = original(*args)
        comparisons.append(result)
        return result

    monkeypatch.setattr(merge_module, "compare_contents", observed)

    async def run() -> None:
        async with environment(ephemeral_postgres) as env:
            await artifact_scenario(env, ephemeral_postgres, source)

    asyncio.run(run())
    if source is ModelOriginType.MODELSCOPE:
        assert comparisons == [
            ComparisonUnavailable(ComparisonUnavailableReason.ARTIFACT_FILE_MANIFEST_UNAVAILABLE)
        ]
    else:
        assert comparisons == []  # HF has no per-file hash; revision is sufficient.


async def active_scenario(
    env: Environment,
    fixture: EphemeralPostgres,
    source: ModelOriginType,
    service: Any = None,
) -> None:
    first_id, first = await resolving(env, source)
    service = service or DownloadResolutionMergeService(session_factory=env.factory)
    assert isinstance(await service.merge(first_id, first), DownloadStarted)
    winner_subscription = rows(fixture, "download_subscriptions")[0]
    second_id, second = await resolving(env, source, "branch-two")
    async with env.factory() as session:
        await DownloadTaskRepository(session).subscribe(
            task_id=second_id, actor_id=env.second_actor_id, now=await database_utc_now(session)
        )
        await session.commit()
    result = await service.merge(second_id, second)
    assert isinstance(result, DownloadMerged)
    tasks = {row["id"]: row for row in rows(fixture, "download_tasks")}
    assert tasks[second_id]["status"] == "Cancelled"
    assert tasks[second_id]["error_code"] == "duplicate_resolved_revision"
    assert tasks[second_id]["artifact_id"] is None
    subscriptions = rows(fixture, "download_subscriptions")
    assert len(subscriptions) == 2 and {row["task_id"] for row in subscriptions} == {first_id}
    assert winner_subscription in subscriptions
    assert {row["subscriber_id"] for row in subscriptions} == {env.actor_id, env.second_actor_id}
    # Replay still works after the former winner leaves the index.
    async with env.factory() as session:
        await session.execute(
            sa.update(DOWNLOAD_TASKS).where(DOWNLOAD_TASKS.c.id == first_id).values(status="Failed")
        )
        await session.commit()
    before = rows(fixture, "download_tasks")
    for _ in range(3):
        assert await service.merge(second_id, second) == result
    assert rows(fixture, "download_tasks") == before
    assert rows(fixture, "download_subscriptions") == subscriptions


@pytest.mark.parametrize("source", SOURCES)
def test_TC_DL_02_active_merge_subscription_collision_and_replay(
    ephemeral_postgres: EphemeralPostgres,
    source: ModelOriginType,
) -> None:
    async def run() -> None:
        async with environment(ephemeral_postgres) as env:
            await active_scenario(env, ephemeral_postgres, source)

    asyncio.run(run())


@pytest.mark.parametrize("source", SOURCES)
def test_TC_DL_04_real_multiconnection_unique_conflict(
    ephemeral_postgres: EphemeralPostgres,
    source: ModelOriginType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        gate = asyncio.Barrier(2)
        pids: set[int] = set()
        conflicts: list[DownloadIdentityConflict] = []

        class RacingRepository(DownloadTaskRepository):
            async def acquire_resolved_identity(
                self, task_id: UUID, resolved_revision: str, now: datetime
            ) -> Any:
                pids.add(
                    (await self._session.execute(sa.text("SELECT pg_backend_pid()"))).scalar_one()
                )
                await asyncio.wait_for(gate.wait(), 5)
                result = await super().acquire_resolved_identity(task_id, resolved_revision, now)
                if isinstance(result, DownloadIdentityConflict):
                    conflicts.append(result)
                return result

        async with environment(ephemeral_postgres) as env:
            first = await resolving(env, source)
            second = await resolving(env, source, "branch-two")
            monkeypatch.setattr(merge_module, "DownloadTaskRepository", RacingRepository)
            service = DownloadResolutionMergeService(session_factory=env.factory)
            results = await asyncio.wait_for(
                asyncio.gather(service.merge(*first), service.merge(*second)), 10
            )
            assert len(pids) == 2 and len(conflicts) == 1
            assert {type(result) for result in results} == {DownloadStarted, DownloadMerged}
            tasks = rows(ephemeral_postgres, "download_tasks")
            assert sorted(row["status"] for row in tasks) == ["Cancelled", "Downloading"]
            winner = next(row["id"] for row in tasks if row["status"] == "Downloading")
            assert {
                row["task_id"] for row in rows(ephemeral_postgres, "download_subscriptions")
            } == {winner}
            print(f"TC-DL-04 {source.value}: 2 backend PIDs, 1 unique conflict, 1 active task")

    asyncio.run(run())


async def lock_scenario(env: Environment, monkeypatch: pytest.MonkeyPatch) -> None:
    winner, resolved = await resolving(env, SOURCES[0])
    service = DownloadResolutionMergeService(session_factory=env.factory)
    await service.merge(winner, resolved)
    loser, other = await resolving(env, SOURCES[0], "branch-two")
    candidate_pid = 0
    base = vars(merge_module)["DownloadTaskRepository"]

    class ObservedRepository(base):  # type: ignore[misc, valid-type]
        async def active_resolution_winner(self, task_id: UUID, value: ResolvedModel) -> Any:
            nonlocal candidate_pid
            candidate_pid = (
                await self._session.execute(sa.text("SELECT pg_backend_pid()"))
            ).scalar_one()
            return await super().active_resolution_winner(task_id, value)

    monkeypatch.setattr(merge_module, "DownloadTaskRepository", ObservedRepository)
    async with env.factory() as updater:
        updater_pid = (await updater.execute(sa.text("SELECT pg_backend_pid()"))).scalar_one()
        await updater.execute(
            sa.update(DOWNLOAD_TASKS)
            .where(DOWNLOAD_TASKS.c.id == winner)
            .values(status="Cancelled")
        )
        pending = asyncio.create_task(service.merge(loser, other))
        try:

            async def blocked() -> None:
                async with env.factory() as observer:
                    while True:
                        assert not pending.done(), "candidate read did not wait for the winner lock"
                        if candidate_pid:
                            blockers = (
                                await observer.execute(
                                    sa.text("SELECT pg_blocking_pids(:pid)"), {"pid": candidate_pid}
                                )
                            ).scalar_one()
                            if updater_pid in blockers:
                                return
                        await asyncio.sleep(0.01)

            await asyncio.wait_for(blocked(), 3)
            await updater.commit()
            assert isinstance(await asyncio.wait_for(pending, 3), DownloadStarted)
        finally:
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


def test_TC_DL_02_candidate_waits_then_rechecks_departed_winner(
    ephemeral_postgres: EphemeralPostgres,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        async with environment(ephemeral_postgres) as env:
            await lock_scenario(env, monkeypatch)

    asyncio.run(run())


async def creation_race_scenario(
    env: Environment,
    fixture: EphemeralPostgres,
    monkeypatch: pytest.MonkeyPatch,
    *,
    exhausted: bool = False,
) -> None:
    first = await create(env)
    base = vars(creation_module)["DownloadTaskRepository"]
    attempts = 0

    class DepartingRepository(base):  # type: ignore[misc, valid-type]
        async def _pre_resolution_winner(self, *args: Any) -> Any:
            nonlocal attempts
            attempts += 1
            if exhausted:
                return None
            async with env.factory() as updater:
                await updater.execute(
                    sa.update(DOWNLOAD_TASKS)
                    .where(DOWNLOAD_TASKS.c.id == first.task.id)
                    .values(status="Cancelled")
                )
                await updater.commit()
            return await super()._pre_resolution_winner(*args)

    monkeypatch.setattr(creation_module, "DownloadTaskRepository", DepartingRepository)
    login = await env.client.request(
        "POST",
        "/api/v1/auth/login",
        json_body={
            "username": "download-admin",
            "password": PASSWORD,
        },
    )
    response = await env.client.request(
        "POST",
        "/api/v1/downloads",
        json_body={"source": SOURCES[0].value, "repository_id": "Owner/Model"},
        cookies=SessionCookies(
            {"__Host-pws_session": _cookie(login, "__Host-pws_session")},
            csrf_token=str(login.json()["csrf_token"]),
        ),
        headers={"Idempotency-Key": "new-after-race"},
    )
    assert response.server_error is None
    if exhausted:
        assert attempts == 3 and response.status == 503
        assert response.json()["error"]["code"] == "download_creation_contended"
        assert "Owner/Model" not in response.text
        assert response.json()["error"]["request_id"]
        assert len(rows(fixture, "download_tasks")) == 1
    else:
        assert attempts == 1 and response.status == 200
        assert response.json()["disposition"] == "created"
        assert response.json()["id"] != str(first.task.id)
        assert sorted(row["status"] for row in rows(fixture, "download_tasks")) == [
            "Cancelled",
            "Queued",
        ]


@pytest.mark.parametrize("exhausted", [False, True])
def test_TC_DL_01_departed_creation_winner_and_bounded_retry_http(
    ephemeral_postgres: EphemeralPostgres,
    monkeypatch: pytest.MonkeyPatch,
    exhausted: bool,
) -> None:
    async def run() -> None:
        async with environment(ephemeral_postgres) as env:
            await creation_race_scenario(env, ephemeral_postgres, monkeypatch, exhausted=exhausted)

    asyncio.run(run())


@pytest.mark.parametrize(
    "failure", ["complete_resolution", "merge_subscriptions", "acquire_resolved_identity"]
)
def test_TC_DL_02_03_04_transaction_rollback(
    ephemeral_postgres: EphemeralPostgres,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    async def run() -> None:
        async with environment(ephemeral_postgres) as env:
            task_id, resolved = await resolving(env, SOURCES[0])
            service = DownloadResolutionMergeService(session_factory=env.factory)
            if failure == "complete_resolution":
                await artifact(env, task_id, resolved)
            elif failure == "merge_subscriptions":
                await service.merge(task_id, resolved)
                task_id, resolved = await resolving(env, SOURCES[0], "branch-two")
            before = {
                name: rows(ephemeral_postgres, name)
                for name in (
                    "download_tasks",
                    "download_subscriptions",
                    "audit_events",
                )
            }
            original = getattr(DownloadTaskRepository, failure)

            async def fail(self: DownloadTaskRepository, *args: Any) -> Any:
                await original(self, *args)
                raise RuntimeError("injected after write")

            monkeypatch.setattr(DownloadTaskRepository, failure, fail)
            with pytest.raises(RuntimeError, match="injected after write"):
                await service.merge(task_id, resolved)
            assert {name: rows(ephemeral_postgres, name) for name in before} == before

    asyncio.run(run())


def test_TC_DL_04_invalid_identity_state_and_bounded_contention(
    ephemeral_postgres: EphemeralPostgres,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        async with environment(ephemeral_postgres) as env:
            task_id, resolved = await resolving(env, SOURCES[0])
            service = DownloadResolutionMergeService(session_factory=env.factory)
            for invalid in (
                replace(resolved, repository_id="private-identity-sentinel"),
                replace(resolved, requested_revision_norm="other"),
                replace(resolved, source=SOURCES[1]),
            ):
                with pytest.raises(DownloadMergeRejected) as caught:
                    await service.merge(task_id, invalid)
                assert "private-identity-sentinel" not in str(caught.value)
            with pytest.raises(DownloadMergeRejected, match="unavailable"):
                await service.merge(uuid4(), resolved)
            count = 0

            async def conflict(
                self: DownloadTaskRepository, *args: Any
            ) -> DownloadIdentityConflict:
                nonlocal count
                count += 1
                return DownloadIdentityConflict()

            with monkeypatch.context() as patch:
                patch.setattr(DownloadTaskRepository, "acquire_resolved_identity", conflict)
                with pytest.raises(DownloadMergeContended):
                    await service.merge(task_id, resolved)
                assert count == 3
            async with env.factory() as session:
                await session.execute(sa.update(DOWNLOAD_TASKS).values(status="Queued"))
                await session.commit()
            with pytest.raises(DownloadMergeRejected, match="not resolving"):
                await service.merge(task_id, resolved)

    asyncio.run(run())


@pytest.mark.parametrize(
    "mutation",
    [
        "01_mutable_artifact",
        "02_unavailable_collapsed",
        "02_unmutated_copy",
        "03_mutable_active_skipped",
        "04_immutable_hash_required",
        "05_cancel_reason",
        "05_complete_reason",
        "06_subscription_collision",
        "07_candidate_lock",
        "08_creation_retry",
        "09_artifact_cancelled",
        "10_result_collapsed",
    ],
)
def test_TC_DL_02_03_04_positive_controls(
    ephemeral_postgres: EphemeralPostgres,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mutation: str,
) -> None:
    from holm_persistence import download_task_repository as repository_module
    from sqlalchemy.exc import IntegrityError

    source_path = MERGE_SOURCE
    replacements: list[tuple[str, str]]
    if mutation == "01_mutable_artifact":
        replacements = [
            (
                "if resolved.revision_immutability is RevisionImmutability.IMMUTABLE:",
                "if True:",
            )
        ]
    elif mutation in {"02_unavailable_collapsed", "02_unmutated_copy"}:
        source_path = DOMAIN_SOURCE
        replacements = [
            (
                "return ComparisonUnavailable("
                "ComparisonUnavailableReason.ARTIFACT_FILE_MANIFEST_UNAVAILABLE)",
                "return ContentsDiffer()",
            )
        ]
        if mutation == "02_unmutated_copy":
            replacements = [("Pure content comparison", "Equivalent pure content comparison")]
    elif mutation == "03_mutable_active_skipped":
        replacements = [
            (
                "winner_id = await repository.active_resolution_winner(task_id, resolved)",
                "winner_id = (await repository.active_resolution_winner(task_id, resolved) "
                "if resolved.revision_immutability is RevisionImmutability.IMMUTABLE else None)",
            )
        ]
    elif mutation == "04_immutable_hash_required":
        replacements = [
            (
                "if resolved.revision_immutability is RevisionImmutability.IMMUTABLE:",
                "if False:",
            )
        ]
    elif mutation in {"05_cancel_reason", "05_complete_reason"}:
        source_path = REPOSITORY_SOURCE
        if mutation == "05_cancel_reason":
            replacements = [
                (
                    "error_code=DownloadCancellationReason.DUPLICATE_RESOLVED_REVISION.value,",
                    "error_code=None,",
                )
            ]
        else:
            replacements = [
                (
                    "artifact_id=artifact_id,\n                error_code=None,",
                    "artifact_id=artifact_id,\n                "
                    "error_code=DownloadCancellationReason.DUPLICATE_RESOLVED_REVISION.value,",
                )
            ]
    elif mutation == "06_subscription_collision":
        source_path = REPOSITORY_SOURCE
        replacements = [
            (
                '.on_conflict_do_nothing(constraint="uq_download_subscription_identity")',
                "",
            )
        ]
    elif mutation == "07_candidate_lock":
        source_path = REPOSITORY_SOURCE
        replacements = [
            (
                "DOWNLOAD_TASKS.c.status.in_(RESOLVED_ACTIVE_STATUSES),\n"
                "                    )\n                    .with_for_update()",
                "DOWNLOAD_TASKS.c.status.in_(RESOLVED_ACTIVE_STATUSES),\n                    )",
            )
        ]
    elif mutation == "08_creation_retry":
        source_path = REPOSITORY_SOURCE
        replacements = [
            (
                "            if row is not None:\n"
                "                return DownloadTaskCreation(_task(row), created=False)",
                "            if row is None:\n"
                "                raise RuntimeError('download conflict winner unavailable')\n"
                "            return DownloadTaskCreation(_task(row), created=False)",
            )
        ]
    elif mutation == "09_artifact_cancelled":
        source_path = REPOSITORY_SOURCE
        replacements = [
            (
                "status=DownloadTaskStatus.COMPLETED.value,",
                "status=DownloadTaskStatus.CANCELLED.value,",
            ),
            (
                "artifact_id=artifact_id,\n                error_code=None,",
                "artifact_id=artifact_id,\n                "
                "error_code=DownloadCancellationReason.DUPLICATE_RESOLVED_REVISION.value,",
            ),
        ]
    else:
        replacements = [("return DownloadCompleted(task_id, artifact_id)", "return True")]
    mutated = load_mutated_module(
        source_path,
        replacements,
        module_name=f"_dl_merge_{mutation}",
        destination=tmp_path / f"{mutation}.py",
    )
    if source_path == REPOSITORY_SOURCE:
        # Preserve class identities so a control cannot fail just because the
        # imported copy defines a second nominal result class.
        for name in (
            "DownloadIdentityAcquired",
            "DownloadIdentityConflict",
            "DownloadCreationContended",
            "DownloadTaskRecord",
            "DownloadTaskCreation",
        ):
            monkeypatch.setattr(mutated, name, getattr(repository_module, name))
        monkeypatch.setattr(merge_module, "DownloadTaskRepository", mutated.DownloadTaskRepository)
        monkeypatch.setattr(
            creation_module, "DownloadTaskRepository", mutated.DownloadTaskRepository
        )
    elif source_path == DOMAIN_SOURCE:
        from holm_domain import download_merge as domain_module

        for name in (
            "ArtifactFileManifestUnavailable",
            "ContentsMatch",
            "ContentsDiffer",
            "ComparisonUnavailable",
            "ComparisonUnavailableReason",
        ):
            monkeypatch.setattr(mutated, name, getattr(domain_module, name))
        monkeypatch.setattr(merge_module, "compare_contents", mutated.compare_contents)

    async def run() -> None:
        async with environment(ephemeral_postgres) as env:
            service = (
                mutated.DownloadResolutionMergeService(session_factory=env.factory)
                if source_path == MERGE_SOURCE
                else None
            )
            if mutation in {"02_unavailable_collapsed", "02_unmutated_copy"}:
                seen: list[Any] = []
                comparator = vars(merge_module)["compare_contents"]

                def observed(*args: Any) -> Any:
                    result = comparator(*args)
                    seen.append(result)
                    return result

                monkeypatch.setattr(merge_module, "compare_contents", observed)
                # Behavior remains Downloading. Only the reason assertion kills
                # this mutant, proving that absence is not silently collapsed.
                await artifact_scenario(env, ephemeral_postgres, SOURCES[1])
                assert len(seen) == 1
                expected = [
                    ComparisonUnavailable(
                        ComparisonUnavailableReason.ARTIFACT_FILE_MANIFEST_UNAVAILABLE
                    )
                ]
                if mutation == "02_unmutated_copy":
                    assert seen == expected
                    print("TC-DL-03 unmutated comparator copy: original behavior accepted")
                    return
                with pytest.raises(AssertionError):
                    assert seen == expected
            elif mutation == "07_candidate_lock":
                with pytest.raises(AssertionError, match="did not wait"):
                    await lock_scenario(env, monkeypatch)
            elif mutation == "08_creation_retry":
                with pytest.raises(AssertionError):
                    await creation_race_scenario(env, ephemeral_postgres, monkeypatch)
            elif mutation == "03_mutable_active_skipped":
                with pytest.raises(mutated.DownloadMergeContended):
                    await active_scenario(env, ephemeral_postgres, SOURCES[1], service)
            elif mutation == "06_subscription_collision":
                with pytest.raises(IntegrityError) as caught:
                    await active_scenario(env, ephemeral_postgres, SOURCES[0])
                assert caught.value.orig.diag.constraint_name == "uq_download_subscription_identity"  # type: ignore[union-attr]
            elif mutation == "05_cancel_reason":
                with pytest.raises(AssertionError):
                    await active_scenario(env, ephemeral_postgres, SOURCES[0])
            else:
                source = SOURCES[1] if mutation == "01_mutable_artifact" else SOURCES[0]
                with pytest.raises(AssertionError):
                    await artifact_scenario(env, ephemeral_postgres, source, service)
            print(
                f"TC-DL-02/03/04 positive control {mutation}: broken production behavior detected"
            )

    asyncio.run(run())


def test_TC_DL_04_same_task_concurrent_replay_is_one_write(
    ephemeral_postgres: EphemeralPostgres,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        gate = asyncio.Barrier(2)
        pids: set[int] = set()
        writes = 0

        class RacingRepository(DownloadTaskRepository):
            async def lock_resolution_task(self, task_id: UUID) -> sa.RowMapping | None:
                pids.add(
                    (await self._session.execute(sa.text("SELECT pg_backend_pid()"))).scalar_one()
                )
                await asyncio.wait_for(gate.wait(), 3)
                return await super().lock_resolution_task(task_id)

            async def acquire_resolved_identity(
                self, task_id: UUID, resolved_revision: str, now: datetime
            ) -> Any:
                nonlocal writes
                writes += 1
                return await super().acquire_resolved_identity(task_id, resolved_revision, now)

        async with environment(ephemeral_postgres) as env:
            task_id, resolved = await resolving(env, SOURCES[0])
            subscriptions = rows(ephemeral_postgres, "download_subscriptions")
            monkeypatch.setattr(merge_module, "DownloadTaskRepository", RacingRepository)
            service = DownloadResolutionMergeService(session_factory=env.factory)
            results = await asyncio.wait_for(
                asyncio.gather(service.merge(task_id, resolved), service.merge(task_id, resolved)),
                5,
            )
            assert results[0] == results[1] == DownloadStarted(task_id)
            assert len(pids) == 2 and writes == 1
            assert rows(ephemeral_postgres, "download_subscriptions") == subscriptions

    asyncio.run(run())


@pytest.mark.parametrize("source", SOURCES)
def test_TC_DL_02_03_recorded_source_results_drive_real_merge(
    ephemeral_postgres: EphemeralPostgres,
    source: ModelOriginType,
    tmp_path: Path,
) -> None:
    """FX-MS-08/09 recordings retain their existing provenance and certification."""
    from contextlib import ExitStack

    from fixtures.hugging_face import PUBLIC_REPOSITORY_ID as HF_REPOSITORY
    from fixtures.hugging_face import fake_hugging_face_hub
    from fixtures.modelscope import PUBLIC_REPOSITORY_ID as MS_REPOSITORY
    from fixtures.modelscope import fake_modelscope_hub
    from holm_control_api.hugging_face_resolution import HuggingFaceResolutionAdapter
    from holm_control_api.modelscope_resolution import ModelScopeResolutionAdapter

    with ExitStack() as stack:
        adapter: HuggingFaceResolutionAdapter | ModelScopeResolutionAdapter
        if source is ModelOriginType.HUGGING_FACE:
            hf_hub = stack.enter_context(fake_hugging_face_hub())
            adapter = HuggingFaceResolutionAdapter(endpoint=hf_hub.endpoint, scratch_root=tmp_path)
            repository_id = HF_REPOSITORY
        else:
            ms_hub = stack.enter_context(fake_modelscope_hub())
            adapter = ModelScopeResolutionAdapter(endpoint=ms_hub.endpoint)
            repository_id = MS_REPOSITORY

        async def run() -> None:
            outcome = await adapter.resolve(
                ResolutionRequest(source, repository_id, "@default"), credential=None
            )
            assert isinstance(outcome, ResolutionSucceeded)
            resolved = outcome.resolved
            assert isinstance(resolved.files, Identified)
            assert resolved.files.value
            assert (resolved.revision_immutability is RevisionImmutability.IMMUTABLE) == (
                source is ModelOriginType.HUGGING_FACE
            )
            async with environment(ephemeral_postgres) as env:
                created = await create(env, source=source, repository=repository_id)
                async with env.factory() as session:
                    await session.execute(
                        sa.update(DOWNLOAD_TASKS)
                        .where(DOWNLOAD_TASKS.c.id == created.task.id)
                        .values(status="Resolving")
                    )
                    await session.commit()
                artifact_id = await artifact(env, created.task.id, resolved)
                service = DownloadResolutionMergeService(session_factory=env.factory)
                result = await service.merge(created.task.id, resolved)
                if source is ModelOriginType.HUGGING_FACE:
                    assert result == DownloadCompleted(created.task.id, artifact_id)
                else:
                    assert result == DownloadStarted(created.task.id)
                assert await service.merge(created.task.id, resolved) == result

        asyncio.run(run())
