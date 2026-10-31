"""M2-01C / FX-M2-01C: sanitized outcomes injected at the validator boundary.

Reuse TC-SRC's real PostgreSQL, authentication, and encrypted credential fixture.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass

import pytest
import sqlalchemy as sa
from conftest import EphemeralPostgres
from credential_scan import database_bytes
from fixtures.asgi_client import AsgiResponse
from holm_control_api.model_source_validation import ModelSourceValidationResult
from holm_domain import (
    ModelSourceType,
    ModelSourceValidationReason,
    ModelSourceValidationStatus,
)
from holm_persistence import ModelSourceRepository
from migration_test_support import upgrade_head
from test_TC_SEC_19_credential_rotation import (
    _assert_sentinels_absent as assert_sentinels_absent,
)
from test_TC_SRC_01_09_model_source_api import (
    KEYRING,
    PASSWORD,
    _environment,
    _login,
    _put,
    _reset_and_create_admin,
)


@dataclass
class OutcomeValidator:
    result: ModelSourceValidationResult

    async def validate(self, *, credential: bytes | None) -> ModelSourceValidationResult:
        return self.result


@pytest.mark.parametrize(
    ("status", "reason", "rejected"),
    [
        ("invalid_credential", "credential_invalid", True),
        ("permission_denied", "source_permission_denied", True),
        ("not_found", "source_not_found", True),
        ("rate_limited", "source_rate_limited", False),
        (None, "validation_timeout", False),
        (None, "validation_failed", False),
        (None, "validator_unavailable", False),
    ],
)
@pytest.mark.parametrize("existing", [False, True])
def test_M2_01C_TC_SRC_03_05_06_07_09_save_semantics(
    ephemeral_postgres: EphemeralPostgres,
    caplog: pytest.LogCaptureFixture,
    status: str | None,
    reason: str,
    rejected: bool,
    existing: bool,
) -> None:
    upgrade_head(ephemeral_postgres.engine)
    sentinel = f"PWSSNTL-SRCTOK-M2-01C-{os.urandom(8).hex()}"
    password_sentinel = f"PWSSNTL-PASSWD-M2-01C-{os.urandom(8).hex()}"
    validator = OutcomeValidator(
        ModelSourceValidationResult(
            ModelSourceValidationStatus.ACCESSIBLE,
            ModelSourceValidationReason.CREDENTIAL_VALID,
            None,
        )
    )

    def rows() -> list[sa.RowMapping]:
        with ephemeral_postgres.engine.connect() as connection:
            return list(connection.execute(sa.text("SELECT * FROM core.model_sources")).mappings())

    async def scenario() -> None:
        async with _environment(ephemeral_postgres, validator=validator) as environment:
            await _reset_and_create_admin(environment)
            cookies, headers = await _login(environment)

            async def put(
                *, confirm: bool = False, password: str | None = PASSWORD
            ) -> AsgiResponse:
                response = await _put(
                    environment,
                    cookies=cookies,
                    headers=headers,
                    source_type=ModelSourceType.HUGGING_FACE,
                    credential=sentinel,
                    password=password,
                    confirm_unverified=confirm,
                )
                assert sentinel not in response.text
                assert password_sentinel not in response.text
                assert response.server_error is None
                return response

            if existing:
                response = await _put(
                    environment,
                    cookies=cookies,
                    headers=headers,
                    source_type=ModelSourceType.HUGGING_FACE,
                    credential="FX-M2-01C-original-valid",
                )
                assert response.status == 200
            before = rows()
            validator.result = ModelSourceValidationResult(
                None if status is None else ModelSourceValidationStatus(status),
                ModelSourceValidationReason(reason),
                None,
            )
            response = await put()
            assert response.status == (422 if rejected else 409)
            assert response.json()["error"]["code"] == (
                "model_source_credential_rejected"
                if rejected
                else "model_source_validation_unavailable"
            )
            assert response.json()["error"]["details"] == {"reason_code": reason}
            assert response.json()["error"]["request_id"]
            assert rows() == before
            for password in (None, password_sentinel):
                denied = await put(confirm=True, password=password)
                assert denied.status == 401
                assert rows() == before
            confirmed = await put(confirm=True)
            if rejected:
                assert confirmed.status == 422
                assert rows() == before
            else:
                assert confirmed.status == 200
                assert confirmed.json()["verification_status"] is None
                assert confirmed.json()["verification_reason_code"] == reason
                async with environment.session_factory() as session:
                    record = await ModelSourceRepository(session).get_by_type(
                        ModelSourceType.HUGGING_FACE
                    )
                    assert record is not None and record.credential is not None
                    assert (
                        KEYRING.decrypt(
                            record.credential, source_id=record.id, credential_type="access_token"
                        )
                        == sentinel.encode()
                    )
            with ephemeral_postgres.engine.connect() as connection:
                audits = (
                    connection.execute(
                        sa.text(
                            "SELECT result, details FROM core.audit_events "
                            "WHERE action = 'model_source.credential_change' ORDER BY created_at"
                        )
                    )
                    .mappings()
                    .all()
                )
            assert audits[-4]["result"] == "failed"
            assert audits[-4]["details"]["reason_code"] == reason
            assert len(audits) == (5 if existing else 4)

    asyncio.run(scenario())
    assert_sentinels_absent(
        database_bytes(ephemeral_postgres.engine),
        (sentinel.encode(), password_sentinel.encode()),
    )
    assert sentinel not in caplog.text
    assert password_sentinel not in caplog.text
