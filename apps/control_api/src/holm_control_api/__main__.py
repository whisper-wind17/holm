"""Control API process entry point."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import sqlalchemy as sa
import uvicorn
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import FastAPI
from holm_common import (
    PlatformConfig,
    SecretFileError,
    load_config,
    read_secret_file,
    startup_failure_code,
)
from holm_contracts import ReleaseManifest, version_payload_from_manifest
from holm_domain.production_events import PRODUCTION_EVENT_REGISTRY
from holm_persistence import (
    AsyncSessionFactory,
    SystemStatusRepository,
    SystemStatusSnapshot,
    create_database_engine,
    create_session_factory,
)
from holm_runtimes import load_runtime_compatibility
from holm_security import (
    ApiKeyPepperKeyringError,
    KeyringError,
    PasswordHasher,
    load_api_key_pepper_keyring,
    load_master_keyring,
)
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.ext.asyncio import AsyncEngine

from holm_control_api.agent_client import GrpcHostAgentClient
from holm_control_api.ai_service_service import AiServiceManagementService
from holm_control_api.api_key_pepper_service import ApiKeyPepperService
from holm_control_api.api_key_service import ApiKeyManagementService
from holm_control_api.application import create_app
from holm_control_api.attention_service import AttentionService
from holm_control_api.authentication import AuthenticationService
from holm_control_api.bootstrap_service import BootstrapService
from holm_control_api.component_probe import ComponentProbeClient
from holm_control_api.credential_rotation import DatabaseCredentialStartupCheck
from holm_control_api.deployment_options import DeploymentOptionsService
from holm_control_api.diagnostics_service import (
    DiagnosticBundleService,
    ModelInstanceSanitizedLogSource,
    QueryServiceRequestRecordSource,
    RepositoryDiagnosticEventSource,
    SystemHealthDiagnosticSource,
    TelemetryOverviewDiagnosticSource,
)
from holm_control_api.download_command_service import DownloadCommandService
from holm_control_api.download_creation_service import DownloadCreationService
from holm_control_api.event_stream import EventStreamService
from holm_control_api.model_acquisition_read_service import (
    ModelAcquisitionReadService,
    ModelRootAvailability,
)
from holm_control_api.model_catalog_service import ModelCatalogService
from holm_control_api.model_instance_service import ModelInstanceService
from holm_control_api.model_registration_service import ModelRegistrationService
from holm_control_api.model_resolution_registry import create_default_resolution_registry
from holm_control_api.model_resolution_service import ModelResolutionService
from holm_control_api.model_source_service import ModelSourceCredentialService
from holm_control_api.readiness import database_schema_check
from holm_control_api.request_record_service import RequestRecordQueryService
from holm_control_api.system_status import SystemStatusService
from holm_control_api.telemetry_service import (
    TelemetryService,
    runtime_metric_mapping_resolver,
)
from holm_control_api.usage_service import UsageQueryService
from holm_control_api.user_management_service import UserManagementService

DEFAULT_ALEMBIC_CONFIG = Path("/opt/holm/migrations/alembic.ini")
DEFAULT_RUNTIME_COMPATIBILITY = Path("/opt/holm/runtime-compat.yaml")


class ControlApiConfigurationError(RuntimeError):
    """One startup input is unavailable or invalid, without retaining its value."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _DatabaseStatusReader:
    def __init__(self, session_factory: AsyncSessionFactory) -> None:
        self._session_factory = session_factory

    async def read(self) -> SystemStatusSnapshot:
        async with self._session_factory() as session:
            return await SystemStatusRepository(session).read()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--database-url-file", required=True, type=Path)
    parser.add_argument("--master-keyring-file", required=True, type=Path)
    parser.add_argument("--api-key-pepper-keyring-file", required=True, type=Path)
    parser.add_argument("--idempotency-hmac-secret-file", required=True, type=Path)
    parser.add_argument("--agent-socket", required=True, type=Path)
    parser.add_argument("--release-manifest", type=Path)
    parser.add_argument("--gateway-health-url")
    parser.add_argument("--edge-health-url")
    parser.add_argument(
        "--runtime-compatibility",
        type=Path,
        default=DEFAULT_RUNTIME_COMPATIBILITY,
    )
    parser.add_argument("--alembic-config", type=Path, default=DEFAULT_ALEMBIC_CONFIG)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    return parser.parse_args(argv)


def _load_json_object(path: Path, label: str) -> Mapping[str, object]:
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ControlApiConfigurationError(f"{label}_unavailable") from None
    if not isinstance(raw, dict):
        raise ControlApiConfigurationError(f"{label}_invalid")
    return cast(Mapping[str, object], raw)


def _load_platform_config(path: Path) -> PlatformConfig:
    document = _load_json_object(path, "platform_config")
    nested = document.get("platform")
    payload = nested if isinstance(nested, dict) else document
    return load_config(cast(Mapping[str, object], payload))


def _load_release_manifest(path: Path) -> ReleaseManifest:
    try:
        document = _load_json_object(path, "release_manifest")
        nested = document.get("release_manifest")
        payload = nested if isinstance(nested, dict) else document
        return ReleaseManifest.model_validate(payload)
    except ValueError:
        raise ControlApiConfigurationError("release_manifest_invalid") from None


def _load_database_url(path: Path) -> URL:
    try:
        raw = read_secret_file(path)
    except SecretFileError as error:
        raise ControlApiConfigurationError(f"database_secret_unavailable:{error.code}") from None
    try:
        value = raw.decode("utf-8").strip()
        url = make_url(value)
    except (ArgumentError, UnicodeDecodeError, ValueError, TypeError):
        raise ControlApiConfigurationError("database_url_invalid") from None
    if not value or url.drivername != "postgresql+psycopg" or not url.username:
        raise ControlApiConfigurationError("database_url_invalid")
    return url


def _load_idempotency_secret(path: Path) -> bytes:
    try:
        value = read_secret_file(path)
    except SecretFileError as error:
        raise ControlApiConfigurationError(f"idempotency_secret_unavailable:{error.code}") from None
    if len(value) < 32:
        raise ControlApiConfigurationError("idempotency_secret_invalid")
    return value


def _alembic_head(path: Path) -> str:
    try:
        head = ScriptDirectory.from_config(Config(path)).get_current_head()
    except Exception:
        raise ControlApiConfigurationError("migration_metadata_unavailable") from None
    if head is None:
        raise ControlApiConfigurationError("migration_metadata_invalid")
    return head


def _build_application(
    *,
    args: argparse.Namespace,
    configuration: PlatformConfig,
    manifest: ReleaseManifest,
    database_url: URL,
    master_keyring_file: Path,
    api_key_pepper_keyring_file: Path,
    idempotency_secret: bytes,
) -> tuple[FastAPI, AsyncEngine]:
    master_keyring = load_master_keyring(
        master_keyring_file,
        expected_gid=os.getegid(),
    )
    api_key_pepper_keyring = load_api_key_pepper_keyring(api_key_pepper_keyring_file)
    runtime_catalog = load_runtime_compatibility(args.runtime_compatibility)
    head = _alembic_head(args.alembic_config)
    version = version_payload_from_manifest(manifest)

    engine = create_database_engine(database_url, connect_timeout_seconds=5)
    factory = create_session_factory(engine)
    agent = GrpcHostAgentClient(args.agent_socket)
    authentication = AuthenticationService(
        session_factory=factory,
        password_hasher=PasswordHasher(),
    )
    system_status = SystemStatusService(
        host_requirements=manifest.host_requirements,
        runtime_catalog=runtime_catalog,
        agent=agent,
        database=_DatabaseStatusReader(factory),
        probes=ComponentProbeClient(
            gateway_url=getattr(args, "gateway_health_url", None),
            edge_url=getattr(args, "edge_health_url", None),
        ),
        version=version,
    )
    bootstrap = BootstrapService(
        session_factory=factory,
        agent=agent,
        system_status=system_status,
        authentication=authentication,
    )
    resolution = ModelResolutionService(
        session_factory=factory,
        keyring=master_keyring,
        registry=create_default_resolution_registry(
            external_model_roots=configuration.storage.external_model_roots,
        ),
    )
    registration = ModelRegistrationService(
        session_factory=factory,
        resolution=resolution,
        idempotency_hmac_secret=idempotency_secret,
    )
    model_instances = ModelInstanceService(
        session_factory=factory,
        agent=agent,
        log_agent=agent,
        runtime_catalog=runtime_catalog,
    )
    ai_services = AiServiceManagementService(session_factory=factory)
    api_key_peppers = ApiKeyPepperService(
        session_factory=factory,
        keyring=api_key_pepper_keyring,
    )
    telemetry = TelemetryService(
        agent=agent,
        instances=model_instances,
        ai_services=ai_services,
        session_factory=factory,
        build_version=version.build_version,
        metric_mapping_resolver=runtime_metric_mapping_resolver(runtime_catalog),
        interval_seconds=configuration.telemetry.scrape_interval_seconds,
    )

    async def read_revision() -> str | None:
        async with factory() as session:
            value = await session.scalar(sa.text("SELECT version_num FROM alembic_version"))
        return None if value is None else str(value)

    request_records = RequestRecordQueryService(session_factory=factory)
    diagnostics = DiagnosticBundleService(
        version=version,
        config=configuration.model_dump(mode="json"),
        health=SystemHealthDiagnosticSource(system_status),
        events=RepositoryDiagnosticEventSource(factory),
        metrics=TelemetryOverviewDiagnosticSource(telemetry),
        request_records=QueryServiceRequestRecordSource(request_records),
        logs=ModelInstanceSanitizedLogSource(model_instances),
    )
    app = create_app(
        diagnostics=diagnostics,
        version_payload=version,
        readiness_checks=[database_schema_check(read_revision, compatible_revisions=(head,))],
        credential_startup_check=DatabaseCredentialStartupCheck(factory, master_keyring),
        authentication=authentication,
        event_stream=EventStreamService(factory, PRODUCTION_EVENT_REGISTRY, listener=None),
        system_status=system_status,
        bootstrap=bootstrap,
        model_sources=ModelSourceCredentialService(
            session_factory=factory,
            authentication=authentication,
            keyring=master_keyring,
        ),
        model_resolution=resolution,
        model_registration=registration,
        download_creation=DownloadCreationService(
            session_factory=factory,
            registration=registration,
            idempotency_hmac_secret=idempotency_secret,
        ),
        download_commands=DownloadCommandService(session_factory=factory),
        model_catalog=ModelCatalogService(session_factory=factory),
        model_instances=model_instances,
        deployment_options=DeploymentOptionsService(
            session_factory=factory,
            catalog=runtime_catalog,
            agent=agent,
        ),
        model_acquisition_read=ModelAcquisitionReadService(
            session_factory=factory,
            storage_availability=ModelRootAvailability(configuration.storage.model_root),
        ),
        ai_services=ai_services,
        api_keys=ApiKeyManagementService(
            session_factory=factory,
            keyring=api_key_pepper_keyring,
        ),
        api_key_peppers=api_key_peppers,
        users=UserManagementService(session_factory=factory),
        request_records=request_records,
        usage=UsageQueryService(session_factory=factory),
        attention=AttentionService(
            session_factory=factory,
            system_server=system_status.read_server,
            telemetry_overview=telemetry.overview,
        ),
        telemetry=telemetry,
    )
    return app, engine


async def _run(args: argparse.Namespace) -> None:
    configuration = _load_platform_config(args.config)
    manifest = _load_release_manifest(
        args.config if args.release_manifest is None else args.release_manifest
    )

    # Secrets are all consumed from explicit files before any database engine is built.
    database_url = _load_database_url(args.database_url_file)
    idempotency_secret = _load_idempotency_secret(args.idempotency_hmac_secret_file)

    app, engine = _build_application(
        args=args,
        configuration=configuration,
        manifest=manifest,
        database_url=database_url,
        master_keyring_file=args.master_keyring_file,
        api_key_pepper_keyring_file=args.api_key_pepper_keyring_file,
        idempotency_secret=idempotency_secret,
    )
    try:
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host=args.host,
                port=args.port,
                access_log=False,
                proxy_headers=False,
                server_header=False,
            )
        )
        await server.serve()
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> None:
    try:
        asyncio.run(_run(_parse_args(argv)))
    except KeyboardInterrupt:
        return
    except Exception as error:
        code = startup_failure_code(
            error,
            (ControlApiConfigurationError, SecretFileError, KeyringError, ApiKeyPepperKeyringError),
        )
        print(f"control API startup failed: {code}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
