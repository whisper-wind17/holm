"""TC-HEALTH: the three internal endpoints frozen by TDD 17.1 (task M1-01A).

TST V0.3 has no case ID for the operability endpoints yet -- section 17.1 of the
TDD is the only baseline that names them -- so these cases use the ``TC-HEALTH``
prefix (M1-01A). Their mapping to TST section 3 and the FX-SQ numbering
remain incomplete.

Every request here goes through the real ASGI application; nothing patches a
route or a handler.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from typing import Final
from uuid import UUID

import pytest
from fastapi import FastAPI
from fixtures.asgi_client import (
    NO_CONFIGURED_CREDENTIAL_ROWS,
    AsgiResponse,
    AsgiTestClient,
)
from holm_contracts import VersionPayload
from holm_control_api import (
    INTERNAL_ERROR_CODE,
    LIVE_PATH,
    READY_PATH,
    VERSION_PATH,
    DependencyCheck,
    ReadinessReason,
    create_app,
    public_route,
)
from holm_control_api.readiness import database_schema_check

HEAD_REVISION: Final[str] = "20260828_0001"
OTHER_REVISION: Final[str] = "20270101_0009"
SECRET_LOOKING_DETAIL: Final[str] = "postgresql://control:hunter2@10.0.0.7:5432/holm"


def _version_payload() -> VersionPayload:
    return VersionPayload(
        build_version="1.4.0+gm1010a1111",
        commit="a" * 40,
        schema_version=3,
        runtime_compat_hash="b" * 64,
    )


class SchemaRevisionReaderSpy:
    """A schema revision reader that records whether it was called at all."""

    def __init__(
        self,
        *,
        revision: str | None = None,
        failure: BaseException | None = None,
    ) -> None:
        self._revision = revision
        self._failure = failure
        self.calls = 0

    async def __call__(self) -> str | None:
        self.calls += 1
        if self._failure is not None:
            raise self._failure
        return self._revision


async def _never_answers() -> str | None:
    await asyncio.sleep(30)
    return HEAD_REVISION


def _database_check(reader: SchemaRevisionReaderSpy) -> DependencyCheck:
    return database_schema_check(reader, compatible_revisions=(HEAD_REVISION,))


def _build_app(
    checks: Sequence[DependencyCheck],
    *,
    readiness_timeout_seconds: float = 5.0,
) -> FastAPI:
    return create_app(
        version_payload=_version_payload(),
        readiness_checks=checks,
        credential_startup_check=NO_CONFIGURED_CREDENTIAL_ROWS,
        readiness_timeout_seconds=readiness_timeout_seconds,
    )


def _get_all(app: FastAPI, paths: Sequence[str]) -> tuple[AsgiResponse, ...]:
    """Run the application's lifespan, then issue each request in order."""

    async def scenario() -> tuple[AsgiResponse, ...]:
        client = AsgiTestClient(app)
        responses: list[AsgiResponse] = []
        async with client.lifespan():
            for path in paths:
                responses.append(await client.get(path))
        return tuple(responses)

    return asyncio.run(scenario())


def _get(app: FastAPI, path: str) -> AsgiResponse:
    return _get_all(app, (path,))[0]


def _get_without_lifespan(app: FastAPI, path: str) -> AsgiResponse:
    async def scenario() -> AsgiResponse:
        return await AsgiTestClient(app).get(path)

    return asyncio.run(scenario())


def test_TC_HEALTH_01_liveness_reports_the_process_without_touching_dependencies() -> None:
    reader = SchemaRevisionReaderSpy(failure=OSError(SECRET_LOOKING_DETAIL))
    app = _build_app([_database_check(reader)])

    response = _get(app, LIVE_PATH)

    assert response.status == 200
    assert response.json() == {"status": "live"}
    # TDD 17.1: liveness checks no external dependency. The probe that would
    # have failed loudly was never invoked.
    assert reader.calls == 0


def test_TC_HEALTH_02_liveness_stays_200_while_readiness_fails_on_an_unreachable_database() -> None:
    reader = SchemaRevisionReaderSpy(failure=OSError(SECRET_LOOKING_DETAIL))
    app = _build_app([_database_check(reader)])

    ready, live = _get_all(app, (READY_PATH, LIVE_PATH))

    assert ready.status == 503
    assert ready.json() == {
        "status": "not_ready",
        "dependencies": [
            {
                "name": "database",
                "status": "fail",
                "reason": ReadinessReason.DEPENDENCY_UNREACHABLE.value,
            }
        ],
    }
    assert SECRET_LOOKING_DETAIL not in ready.text
    # The database really is unreachable in this scenario -- readiness proved it
    # by trying -- and liveness still answers 200, so a dependency outage cannot
    # turn into a restart storm.
    assert reader.calls == 1
    assert live.status == 200
    assert live.json() == {"status": "live"}


def test_TC_HEALTH_03_readiness_is_200_when_the_database_and_migration_match() -> None:
    reader = SchemaRevisionReaderSpy(revision=HEAD_REVISION)
    app = _build_app([_database_check(reader)])

    response = _get(app, READY_PATH)

    assert response.status == 200
    assert response.json() == {
        "status": "ready",
        "dependencies": [{"name": "database", "status": "pass", "reason": "ok"}],
    }
    assert reader.calls == 1


@pytest.mark.parametrize(
    ("revision", "expected_reason"),
    [
        (OTHER_REVISION, ReadinessReason.SCHEMA_REVISION_INCOMPATIBLE),
        (None, ReadinessReason.SCHEMA_REVISION_MISSING),
    ],
)
def test_TC_HEALTH_04_readiness_rejects_an_incompatible_migration_version(
    revision: str | None,
    expected_reason: ReadinessReason,
) -> None:
    reader = SchemaRevisionReaderSpy(revision=revision)
    app = _build_app([_database_check(reader)])

    response = _get(app, READY_PATH)

    assert response.status == 503
    assert response.json()["dependencies"] == [
        {"name": "database", "status": "fail", "reason": expected_reason.value}
    ]


def test_TC_HEALTH_05_readiness_is_not_ready_before_startup_and_after_shutdown() -> None:
    reader = SchemaRevisionReaderSpy(revision=HEAD_REVISION)
    app = _build_app([_database_check(reader)])

    before_startup = _get_without_lifespan(app, READY_PATH)

    assert before_startup.status == 503
    assert before_startup.json()["dependencies"] == [
        {
            "name": "database",
            "status": "fail",
            "reason": ReadinessReason.STARTUP_INCOMPLETE.value,
        }
    ]
    assert reader.calls == 0

    during_startup = _get(app, READY_PATH)
    assert during_startup.status == 200

    after_shutdown = _get_without_lifespan(app, READY_PATH)
    assert after_shutdown.status == 503
    assert after_shutdown.json()["status"] == "not_ready"


def test_TC_HEALTH_06_readiness_bounds_a_hanging_dependency_with_an_explicit_timeout() -> None:
    app = _build_app(
        [DependencyCheck(name="database", probe=_never_answers)],
        readiness_timeout_seconds=0.05,
    )

    started = time.monotonic()
    response = _get(app, READY_PATH)
    elapsed = time.monotonic() - started

    assert response.status == 503
    assert response.json()["dependencies"] == [
        {
            "name": "database",
            "status": "fail",
            "reason": ReadinessReason.DEPENDENCY_TIMEOUT.value,
        }
    ]
    assert elapsed < 5.0


def test_TC_HEALTH_07_version_serves_the_frozen_M0_08_payload() -> None:
    payload = _version_payload()
    reader = SchemaRevisionReaderSpy(revision=HEAD_REVISION)
    app = _build_app([_database_check(reader)])

    response = _get(app, VERSION_PATH)

    assert response.status == 200
    # Field-for-field identical to the contract frozen by M0-08; the endpoint
    # defines no second set of version fields of its own.
    assert response.json() == payload.model_dump(mode="json")
    assert set(response.json()) == set(VersionPayload.model_fields)


def test_TC_HEALTH_08_an_uncaught_exception_never_returns_a_stack_or_internal_path() -> None:
    reader = SchemaRevisionReaderSpy(revision=HEAD_REVISION)
    app = _build_app([_database_check(reader)])

    @public_route
    async def explode() -> None:
        raise RuntimeError(SECRET_LOOKING_DETAIL)

    app.add_api_route("/internal/m1-01a-failing-probe", explode, methods=["GET"])

    response = _get(app, "/internal/m1-01a-failing-probe")

    assert response.status == 500
    body = response.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "request_id"}
    assert body["error"]["code"] == INTERNAL_ERROR_CODE
    request_id = UUID(body["error"]["request_id"])
    assert [value for name, value in response.headers if name.casefold() == "x-request-id"] == [
        str(request_id)
    ]
    for leak in (SECRET_LOOKING_DETAIL, "Traceback", "RuntimeError", "holm_control_api", "/"):
        assert leak not in response.text
    # The process still sees the failure; only the caller's copy is sanitized.
    assert isinstance(response.server_error, RuntimeError)


def test_TC_HEALTH_09_an_application_cannot_be_built_without_a_real_dependency_check() -> None:
    with pytest.raises(ValueError, match="at least one dependency check"):
        _build_app([])

    with pytest.raises(ValueError, match="compatible Migration revision"):
        database_schema_check(
            SchemaRevisionReaderSpy(revision=HEAD_REVISION),
            compatible_revisions=(),
        )


def test_TC_HEALTH_10_internal_endpoints_need_no_credentials_and_publish_no_schema() -> None:
    reader = SchemaRevisionReaderSpy(revision=HEAD_REVISION)
    app = _build_app([_database_check(reader)])

    live, ready, version, openapi, docs = _get_all(
        app,
        (LIVE_PATH, READY_PATH, VERSION_PATH, "/openapi.json", "/docs"),
    )

    # No Authorization header was sent: /internal/ endpoints must stay callable
    # by the process supervisor, which has no credentials (M1-01B/C own auth).
    assert (live.status, ready.status, version.status) == (200, 200, 200)
    # The route table and interactive docs belong to a later card; publishing
    # them here would hand an unauthenticated caller the internal surface.
    assert (openapi.status, docs.status) == (404, 404)
