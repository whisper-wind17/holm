"""The Control API ASGI application factory.

The application exposes internal endpoints and
the optional, explicitly injected authentication service and its three routes;
later business routers remain outside this factory's current scope.

Everything the application needs is injected, which is what lets a test build a
process-shaped application with a deliberately broken database and no
configuration subsystem (that subsystem does not exist yet).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Final

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from holm_common.usernames import UsernameError
from holm_contracts import VersionPayload
from holm_domain.instance_names import InvalidInstanceName
from holm_persistence import AsyncSessionFactory, DuplicateUsernameError
from holm_persistence.model_instance_repository import ModelInstanceNameConflict

from holm_control_api.agent_client import HostAgentClient
from holm_control_api.ai_service_routes import (
    create_ai_service_router,
    handle_ai_service_error,
)
from holm_control_api.ai_service_service import (
    AiServiceDisplayNameConflict,
    AiServiceManagementService,
    AiServiceNameConflict,
    AiServiceNotFound,
    AiServicePrimaryInstanceNotFound,
    AiServiceVersionConflict,
)
from holm_control_api.api_key_pepper_routes import create_api_key_pepper_router
from holm_control_api.api_key_pepper_service import ApiKeyPepperService
from holm_control_api.api_key_routes import create_api_key_router, handle_api_key_error
from holm_control_api.api_key_service import (
    ApiKeyActiveDeletion,
    ApiKeyExpirationInvalid,
    ApiKeyManagementService,
    ApiKeyNotFound,
    ApiKeyScopeNotAuthorized,
    ApiKeyScopeServiceNotFound,
)
from holm_control_api.attention_routes import create_attention_router
from holm_control_api.attention_service import AttentionService
from holm_control_api.auth_routes import (
    create_auth_router,
    handle_authentication_failure,
    handle_request_validation_error,
)
from holm_control_api.authentication import AuthenticationFailure, AuthenticationService
from holm_control_api.authorization import RouteAuthorizationMiddleware
from holm_control_api.bootstrap_routes import create_bootstrap_router
from holm_control_api.bootstrap_service import BootstrapService
from holm_control_api.credential_rotation import CredentialStartupCheck
from holm_control_api.deployment_options import (
    DeploymentModelNotFound,
    DeploymentOptionsService,
    create_deployment_options_router,
    handle_deployment_model_not_found,
)
from holm_control_api.diagnostics_routes import create_diagnostics_router
from holm_control_api.diagnostics_service import DiagnosticBundleService
from holm_control_api.download_command_routes import (
    create_download_command_router,
    handle_download_command_error,
)
from holm_control_api.download_command_service import (
    DownloadAlreadyInProgress,
    DownloadCommandNotAllowed,
    DownloadCommandNotFound,
    DownloadCommandService,
)
from holm_control_api.download_creation_routes import (
    create_download_creation_router,
    handle_download_source_rejected,
)
from holm_control_api.download_creation_service import (
    DownloadCreationService,
    DownloadSourceRejected,
)
from holm_control_api.errors import handle_unexpected_error
from holm_control_api.event_stream import EventStreamService
from holm_control_api.events_routes import create_events_router
from holm_control_api.internal_routes import create_internal_router
from holm_control_api.master_key_rotation_routes import (
    create_master_key_rotation_router,
    handle_master_key_rotation_unavailable,
)
from holm_control_api.master_key_rotation_service import (
    MasterKeyRotationOperationService,
    MasterKeyRotationUnavailable,
)
from holm_control_api.me_routes import create_me_router
from holm_control_api.model_acquisition_read_routes import (
    ModelAcquisitionQueryInvalid,
    create_artifact_listing_router,
    create_download_listing_router,
    handle_model_acquisition_cursor_invalid,
    handle_model_acquisition_query_invalid,
)
from holm_control_api.model_acquisition_read_service import (
    ModelAcquisitionCursorInvalid,
    ModelAcquisitionReadService,
)
from holm_control_api.model_catalog_routes import (
    create_model_catalog_router,
    handle_model_catalog_cursor_invalid,
    handle_model_catalog_not_found,
    handle_model_catalog_referenced,
)
from holm_control_api.model_catalog_service import (
    ModelCatalogCursorInvalid,
    ModelCatalogNotFound,
    ModelCatalogReferenced,
    ModelCatalogService,
)
from holm_control_api.model_instance_routes import (
    create_model_instance_router,
    handle_model_instance_error,
)
from holm_control_api.model_instance_service import (
    ModelInstanceArtifactNotReady,
    ModelInstanceGpuInventoryUnavailable,
    ModelInstanceInvalidConfiguration,
    ModelInstanceInvalidState,
    ModelInstanceLogCursorInvalid,
    ModelInstanceLogsUnavailable,
    ModelInstanceNotFound,
    ModelInstanceReferenced,
    ModelInstanceService,
)
from holm_control_api.model_registration_routes import (
    create_model_registration_router,
    handle_idempotency_conflict,
)
from holm_control_api.model_registration_service import (
    IdempotencyConflict,
    ModelRegistrationService,
)
from holm_control_api.model_resolution_routes import (
    create_model_resolution_router,
    handle_resolve_input_rejected,
)
from holm_control_api.model_resolution_service import (
    ModelResolutionService,
    ResolveInputRejected,
)
from holm_control_api.model_source_routes import (
    create_model_source_router,
    handle_model_source_conflict,
)
from holm_control_api.model_source_service import (
    ModelSourceCredentialConflict,
    ModelSourceCredentialService,
)
from holm_control_api.readiness import (
    DEFAULT_READINESS_TIMEOUT_SECONDS,
    DependencyCheck,
    ReadinessEvaluator,
)
from holm_control_api.request_record_routes import (
    create_request_record_router,
    handle_request_record_query_invalid,
)
from holm_control_api.request_record_service import (
    RequestRecordQueryInvalid,
    RequestRecordQueryService,
)
from holm_control_api.state import STATE_ATTRIBUTE, ControlApiState
from holm_control_api.system_routes import create_system_router
from holm_control_api.system_status import HEARTBEAT_STALE_AFTER_SECONDS, SystemStatusService
from holm_control_api.telemetry_routes import create_telemetry_router
from holm_control_api.telemetry_service import TelemetryService
from holm_control_api.usage_routes import create_usage_router, handle_usage_error
from holm_control_api.usage_service import (
    UsageQueryForbidden,
    UsageQueryInvalid,
    UsageQueryService,
)
from holm_control_api.user_management_routes import (
    create_user_management_router,
    handle_user_management_error,
)
from holm_control_api.user_management_service import (
    LastActiveAdministrator,
    PermissionTargetInvalid,
    ServicePermissionNotFound,
    UserInputInvalid,
    UserManagementService,
    UserNotFound,
    UserSelfProtection,
)

APPLICATION_TITLE: Final[str] = "Holm Control API"


def create_app(
    *,
    version_payload: VersionPayload,
    readiness_checks: Sequence[DependencyCheck],
    credential_startup_check: CredentialStartupCheck,
    readiness_timeout_seconds: float = DEFAULT_READINESS_TIMEOUT_SECONDS,
    authentication: AuthenticationService | None = None,
    event_stream: EventStreamService | None = None,
    system_status: SystemStatusService | None = None,
    bootstrap: BootstrapService | None = None,
    model_sources: ModelSourceCredentialService | None = None,
    model_resolution: ModelResolutionService | None = None,
    model_registration: ModelRegistrationService | None = None,
    download_creation: DownloadCreationService | None = None,
    download_commands: DownloadCommandService | None = None,
    model_catalog: ModelCatalogService | None = None,
    model_instances: ModelInstanceService | None = None,
    deployment_options: DeploymentOptionsService | None = None,
    ai_services: AiServiceManagementService | None = None,
    api_keys: ApiKeyManagementService | None = None,
    api_key_peppers: ApiKeyPepperService | None = None,
    users: UserManagementService | None = None,
    model_acquisition_read: ModelAcquisitionReadService | None = None,
    master_key_rotation: MasterKeyRotationOperationService | None = None,
    request_records: RequestRecordQueryService | None = None,
    usage: UsageQueryService | None = None,
    telemetry: TelemetryService | None = None,
    telemetry_agent: HostAgentClient | None = None,
    telemetry_session_factory: AsyncSessionFactory | None = None,
    attention: AttentionService | None = None,
    diagnostics: DiagnosticBundleService | None = None,
) -> FastAPI:
    """Build the Control API application.

    ``readiness_checks`` is required and must not be empty: readiness has to
    report on something real (readiness includes Migration compatibility and the
    critical dependencies), and an application assembled without checks would
    answer 200 forever. ``ReadinessEvaluator`` rejects the empty case.
    """
    telemetry_assembly_requested = (
        telemetry_agent is not None or telemetry_session_factory is not None
    )
    if telemetry is not None and telemetry_assembly_requested:
        raise ValueError("telemetry must be injected or assembled, not both")
    if telemetry is None and telemetry_assembly_requested:
        if (
            telemetry_agent is None
            or telemetry_session_factory is None
            or model_instances is None
            or ai_services is None
        ):
            raise ValueError("telemetry assembly requires Agent, database, instances, and services")
        telemetry = TelemetryService(
            agent=telemetry_agent,
            instances=model_instances,
            ai_services=ai_services,
            session_factory=telemetry_session_factory,
            build_version=version_payload.build_version,
            heartbeat_stale_after_seconds=HEARTBEAT_STALE_AFTER_SECONDS,
        )
    if users is None and api_keys is not None:
        users = UserManagementService(session_factory=api_keys.session_factory)

    state = ControlApiState(
        version_payload=version_payload,
        readiness=ReadinessEvaluator(
            readiness_checks,
            timeout_seconds=readiness_timeout_seconds,
        ),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await credential_startup_check.check()
        if api_key_peppers is not None:
            await api_key_peppers.check_startup()
        if event_stream is not None:
            await event_stream.start()
        if telemetry is not None:
            await telemetry.start()
        state.mark_startup_complete()
        try:
            yield
        finally:
            if telemetry is not None:
                await telemetry.stop()
            if event_stream is not None:
                await event_stream.stop()
            state.mark_stopped()

    application = FastAPI(
        title=APPLICATION_TITLE,
        lifespan=lifespan,
        # The public API and its published OpenAPI document belong to later
        # cards. Until then the schema and the interactive docs would only
        # publish this process's route table to unauthenticated callers.
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
    )
    setattr(application.state, STATE_ATTRIBUTE, state)
    application.include_router(create_internal_router())
    if authentication is not None:
        application.include_router(create_auth_router(authentication))
        application.include_router(create_me_router())
        application.add_exception_handler(
            AuthenticationFailure,
            handle_authentication_failure,
        )
    if bootstrap is not None:
        application.include_router(create_bootstrap_router(bootstrap))
    if (
        authentication is not None
        or bootstrap is not None
        or model_resolution is not None
        or model_registration is not None
        or download_creation is not None
        or download_commands is not None
        or model_catalog is not None
        or model_instances is not None
        or deployment_options is not None
        or ai_services is not None
        or api_keys is not None
        or api_key_peppers is not None
        or users is not None
        or model_acquisition_read is not None
        or request_records is not None
        or usage is not None
        or telemetry is not None
        or diagnostics is not None
    ):
        # These routers close their request models, so a malformed body must
        # render the sanitized envelope rather than FastAPI's default 422, which
        # echoes the rejected input back to the caller.
        application.add_exception_handler(
            RequestValidationError,
            handle_request_validation_error,
        )
    if event_stream is not None:
        application.include_router(create_events_router(event_stream))
    if system_status is not None:
        application.include_router(create_system_router(system_status))
    if model_sources is not None:
        application.include_router(create_model_source_router(model_sources))
        application.add_exception_handler(
            ModelSourceCredentialConflict,
            handle_model_source_conflict,
        )
    if model_resolution is not None:
        application.include_router(create_model_resolution_router(model_resolution))
        application.add_exception_handler(
            ResolveInputRejected,
            handle_resolve_input_rejected,
        )
    if model_registration is not None:
        application.include_router(create_model_registration_router(model_registration))
        application.add_exception_handler(
            IdempotencyConflict,
            handle_idempotency_conflict,
        )
        application.add_exception_handler(
            ResolveInputRejected,
            handle_resolve_input_rejected,
        )
    if download_creation is not None:
        application.include_router(create_download_creation_router(download_creation))
        application.add_exception_handler(DownloadSourceRejected, handle_download_source_rejected)
        application.add_exception_handler(IdempotencyConflict, handle_idempotency_conflict)
        application.add_exception_handler(ResolveInputRejected, handle_resolve_input_rejected)
    if download_commands is not None:
        application.include_router(create_download_command_router(download_commands))
        application.add_exception_handler(DownloadCommandNotFound, handle_download_command_error)
        application.add_exception_handler(
            DownloadCommandNotAllowed,
            handle_download_command_error,
        )
        application.add_exception_handler(
            DownloadAlreadyInProgress,
            handle_download_command_error,
        )
    if model_catalog is not None:
        application.include_router(create_model_catalog_router(model_catalog))
        application.add_exception_handler(
            ModelCatalogCursorInvalid,
            handle_model_catalog_cursor_invalid,
        )
        application.add_exception_handler(
            ModelCatalogNotFound,
            handle_model_catalog_not_found,
        )
        application.add_exception_handler(
            ModelCatalogReferenced,
            handle_model_catalog_referenced,
        )
    if model_instances is not None:
        application.include_router(create_model_instance_router(model_instances))
        for model_instance_exception_type in (
            ModelInstanceNotFound,
            InvalidInstanceName,
            ModelInstanceNameConflict,
            ModelInstanceArtifactNotReady,
            ModelInstanceInvalidConfiguration,
            ModelInstanceInvalidState,
            ModelInstanceLogCursorInvalid,
            ModelInstanceLogsUnavailable,
            ModelInstanceGpuInventoryUnavailable,
            ModelInstanceReferenced,
        ):
            application.add_exception_handler(
                model_instance_exception_type, handle_model_instance_error
            )
    if deployment_options is not None:
        application.include_router(create_deployment_options_router(deployment_options))
        application.add_exception_handler(
            DeploymentModelNotFound,
            handle_deployment_model_not_found,
        )
    if ai_services is not None:
        application.include_router(create_ai_service_router(ai_services))
        for ai_service_exception_type in (
            AiServiceNotFound,
            AiServicePrimaryInstanceNotFound,
            AiServiceVersionConflict,
            ModelInstanceInvalidState,
            AiServiceNameConflict,
            AiServiceDisplayNameConflict,
        ):
            application.add_exception_handler(ai_service_exception_type, handle_ai_service_error)
    if api_keys is not None:
        application.include_router(create_api_key_router(api_keys))
        for api_key_exception_type in (
            ApiKeyNotFound,
            ApiKeyScopeServiceNotFound,
            ApiKeyScopeNotAuthorized,
            ApiKeyActiveDeletion,
            ApiKeyExpirationInvalid,
        ):
            application.add_exception_handler(api_key_exception_type, handle_api_key_error)
    if api_key_peppers is not None:
        application.include_router(create_api_key_pepper_router(api_key_peppers))
    if users is not None:
        application.include_router(create_user_management_router(users))
        for user_exception_type in (
            UserNotFound,
            DuplicateUsernameError,
            UserSelfProtection,
            LastActiveAdministrator,
            PermissionTargetInvalid,
            ServicePermissionNotFound,
            UsernameError,
            UserInputInvalid,
        ):
            application.add_exception_handler(
                user_exception_type,
                handle_user_management_error,
            )
    if model_acquisition_read is not None:
        if model_instances is not None:
            model_acquisition_read.enable_database_reference_count()
        application.include_router(create_download_listing_router(model_acquisition_read))
        application.include_router(create_artifact_listing_router(model_acquisition_read))
        application.add_exception_handler(
            ModelAcquisitionCursorInvalid,
            handle_model_acquisition_cursor_invalid,
        )
        application.add_exception_handler(
            ModelAcquisitionQueryInvalid,
            handle_model_acquisition_query_invalid,
        )
    if request_records is not None:
        application.include_router(create_request_record_router(request_records))
        application.add_exception_handler(
            RequestRecordQueryInvalid,
            handle_request_record_query_invalid,
        )
    if usage is not None:
        application.include_router(create_usage_router(usage))
        application.add_exception_handler(UsageQueryInvalid, handle_usage_error)
        application.add_exception_handler(UsageQueryForbidden, handle_usage_error)
    if master_key_rotation is not None:
        application.include_router(create_master_key_rotation_router(master_key_rotation))
        application.add_exception_handler(
            MasterKeyRotationUnavailable,
            handle_master_key_rotation_unavailable,
        )
    if telemetry is not None:
        application.include_router(create_telemetry_router(telemetry))
    if attention is not None:
        application.include_router(create_attention_router(attention))
    if diagnostics is not None:
        application.include_router(create_diagnostics_router(diagnostics))
    application.add_middleware(
        RouteAuthorizationMiddleware,
        routes=application.router.routes,
        authentication=authentication,
    )
    # Starlette's default handler answers an uncaught exception with a bare 500;
    # this one answers with a stable code and a Request ID instead, and never
    # with a stack, a module path or the exception message.
    application.add_exception_handler(Exception, handle_unexpected_error)
    return application
