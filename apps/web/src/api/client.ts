import {
  diagnosticManifestSchema,
  diagnosticRequestSchema,
  type DiagnosticRequest,
  type DiagnosticPreview,
} from "./diagnostic-contracts";
import {
  meResponseSchema,
  safeErrorEnvelope,
  type LoginRequest,
  type MeResponse,
  type PasswordChangeRequest,
} from "./contracts";
import {
  BOOTSTRAP_ADMIN_PATH,
  BOOTSTRAP_CHECK_PATH,
  BOOTSTRAP_STATUS_PATH,
  BOOTSTRAP_TOKEN_HEADER,
  parseBootstrapAdminResponse,
  parseBootstrapCheckReport,
  parseBootstrapStatusResponse,
  type BootstrapAdminRequest,
  type BootstrapAdminResponse,
  type BootstrapCheckReport,
  type BootstrapStatusResponse,
} from "./bootstrap-contracts";
import {
  parseSystemComponentsResponse,
  parseSystemServerResponse,
  type SystemComponentsResponse,
  type SystemServerResponse,
} from "./system-contracts";
import {
  parseModelSourceCredentialRequest,
  parseModelSourceListResponse,
  parseModelSourceResponse,
  type ModelSourceCredentialRequest,
  type ModelSourceResponse,
} from "./model-source-contracts";
import {
  parseDownloadStatusCounts,
  type DownloadStatusCounts,
  type ModelArtifactDetail,
  parseModelArtifactDetail,
  type DownloadTaskStatus,
  parseArtifactDeleteResponse,
  parseArtifactVerificationStatus,
  parseArtifactVerifyResponse,
  parseArtifactDeletionStatus,
  parseDownloadCommandResponse,
  parseDownloadTaskPage,
  parseModelArtifactPage,
  type ArtifactDeleteResponse,
  type ArtifactDeletionStatus,
  type ArtifactVerificationStatus,
  type ArtifactVerifyResponse,
  type DownloadCommand,
  type DownloadCommandResponse,
  type DownloadTaskPage,
  type ModelArtifactPage,
} from "./model-acquisition-contracts";
import {
  parseDownloadCreationResponse,
  parseModelCatalogPage,
  parseRegistrationResponse,
  parseResolveRequest,
  parseResolveResponse,
  type DownloadCreationResponse,
  type ModelCatalogPage,
  type RegistrationResponse,
  type ResolveRequest,
  type ResolveResponse,
} from "./model-selection-contracts";
import {
  parseCreateModelInstanceRequest,
  parseModelInstanceEventPage,
  parseModelInstance,
  parseModelInstanceList,
  parseModelInstanceLogPage,
  type CreateModelInstanceRequest,
  type ModelInstance,
  type ModelInstanceEventPage,
  type ModelInstanceList,
  type ModelInstanceLogPage,
} from "./model-instance-contracts";
import {
  parseAiService,
  parseAiServiceList,
  parseCreateAiServiceRequest,
  parseUpdateAiServiceRequest,
  parseRenameAiServiceRequest,
  renameServiceDisplayNameSchema,
  type RenameServiceDisplayNameRequest,
  type RenameAiServiceRequest,
  type AiService,
  type AiServiceList,
  type CreateAiServiceRequest,
  type UpdateAiServiceRequest,
} from "./ai-service-contracts";
import {
  parseApiKey,
  parseApiKeyList,
  parseCreateApiKeyRequest,
  parseRenameApiKeyRequest,
  parseCreatedApiKey,
  type ApiKey,
  type ApiKeyList,
  type CreateApiKeyRequest,
  type RenameApiKeyRequest,
  type CreatedApiKey,
} from "./api-key-contracts";
import {
  parseRequestRecordPage,
  parseRequestRecordErrorGroups,
  type RequestRecordErrorGroupFilters,
  type RequestRecordErrorGroups,
  type RequestRecordFilters,
  type RequestRecordPage,
} from "./request-record-contracts";
import {
  parseGpuTelemetryList,
  parseInstanceTelemetry,
  parseOverviewAttention,
  parseOverviewTelemetry,
  parseServiceTelemetry,
  type GpuTelemetryList,
  type InstanceTelemetry,
  type OverviewAttention,
  type OverviewTelemetry,
  type ServiceTelemetry,
} from "./telemetry-contracts";
import {
  parseCreatedUser,
  parseCreateUserRequest,
  parsePersonalAiServiceList,
  parsePersonalProfile,
  type PersonalProfile,
  type UpdateMyProfileRequest,
  parseResetPassword,
  parseSafeApiKey,
  parseServiceIds,
  parseUserDetail,
  parseUserActivityPage,
  parseUserIds,
  parseUserList,
  parseUserOverview,
  type CreatedUser,
  type CreateUserRequest,
  type PersonalAiServiceList,
  type ResetPassword,
  type SafeApiKey,
  type ServiceIds,
  type UserDetail,
  type UserActivityAction,
  type UserActivityPage,
  type UserIds,
  type PermissionLimitsMap,
  type UserList,
  type UserOverview,
  type UserRole,
  type UserStatus,
} from "./user-contracts";
import {
  parseUsageReport,
  parseUsageSummary,
  type UsageFilters,
  type UsageReport,
  type UsageSummary,
} from "./usage-contracts";
import {
  parseModelDeploymentOptions,
  type ModelDeploymentOptions,
} from "./deployment-options-contracts";

const CSRF_COOKIE_NAME = "__Host-pws_csrf";
const CSRF_HEADER_NAME = "X-CSRF-Token";

type AuthenticationFailureHandler = () => void;

export class ApiError extends Error {
  readonly code: string;
  readonly details: Record<string, unknown> | undefined;
  readonly requestId: string | undefined;
  readonly retryAfterSeconds: number | undefined;
  readonly responseMessage: string | undefined;
  readonly status: number;

  constructor(options: {
    code: string;
    details?: Record<string, unknown>;
    requestId?: string;
    responseMessage?: string;
    retryAfterSeconds?: number;
    status: number;
  }) {
    super(options.responseMessage ?? options.code);
    this.name = "ApiError";
    this.code = options.code;
    this.details = options.details;
    this.requestId = options.requestId;
    this.retryAfterSeconds = options.retryAfterSeconds;
    this.responseMessage = options.responseMessage;
    this.status = options.status;
  }
}

export function readCsrfCookie(cookieHeader = document.cookie) {
  for (const part of cookieHeader.split(";")) {
    const separator = part.indexOf("=");
    if (separator < 0) {
      continue;
    }
    const name = part.slice(0, separator).trim();
    if (name === CSRF_COOKIE_NAME) {
      return decodeURIComponent(part.slice(separator + 1));
    }
  }
  return undefined;
}

interface RequestOptions {
  body?: unknown;
  extraHeaders?: Readonly<Record<string, string>>;
  handleAuthenticationFailure?: boolean;
  method?: "DELETE" | "GET" | "PATCH" | "POST" | "PUT";
  parse?: (value: unknown) => unknown;
  parseResponse?: (response: Response) => Promise<unknown>;
  requiresCsrf?: boolean;
}

export class ApiClient {
  private authenticationFailureHandler:
    AuthenticationFailureHandler | undefined;

  setAuthenticationFailureHandler(
    handler: AuthenticationFailureHandler | undefined,
  ) {
    this.authenticationFailureHandler = handler;
  }

  async getMe() {
    return this.request<MeResponse>("/api/v1/me", {
      parse: (value) => meResponseSchema.parse(value),
    });
  }

  async getSystemServer() {
    return this.request<SystemServerResponse>("/api/v1/system/server", {
      parse: parseSystemServerResponse,
    });
  }

  async getSystemComponents() {
    return this.request<SystemComponentsResponse>("/api/v1/system/components", {
      parse: parseSystemComponentsResponse,
    });
  }

  async getModelSources() {
    return this.request<ModelSourceResponse[]>("/api/v1/system/model-sources", {
      parse: parseModelSourceListResponse,
    });
  }

  async getModelCatalog(cursor?: string) {
    const query = new URLSearchParams();
    if (cursor !== undefined) query.set("cursor", cursor);
    const suffix = query.size === 0 ? "" : `?${query.toString()}`;
    return this.request<ModelCatalogPage>(`/api/v1/models${suffix}`, {
      parse: parseModelCatalogPage,
    });
  }

  async resolveModel(body: ResolveRequest) {
    return this.withAuthenticationRequiredRecovery(
      this.request<ResolveResponse>("/api/v1/models/resolve", {
        body: parseResolveRequest(body),
        handleAuthenticationFailure: false,
        method: "POST",
        parse: parseResolveResponse,
        requiresCsrf: true,
      }),
    );
  }

  async registerModel(body: ResolveRequest, idempotencyKey: string) {
    return this.modelWrite<RegistrationResponse>(
      "/api/v1/models",
      body,
      idempotencyKey,
      parseRegistrationResponse,
    );
  }

  async createDownload(body: ResolveRequest, idempotencyKey: string) {
    return this.modelWrite<DownloadCreationResponse>(
      "/api/v1/downloads",
      body,
      idempotencyKey,
      parseDownloadCreationResponse,
    );
  }

  private async modelWrite<T>(
    path: string,
    body: ResolveRequest,
    idempotencyKey: string,
    parse: (value: unknown) => T,
  ) {
    return this.withAuthenticationRequiredRecovery(
      this.request<T>(path, {
        body: parseResolveRequest(body),
        extraHeaders: { "Idempotency-Key": idempotencyKey },
        handleAuthenticationFailure: false,
        method: "POST",
        parse,
        requiresCsrf: true,
      }),
    );
  }

  async updateModelSource(body: ModelSourceCredentialRequest) {
    return this.withAuthenticationRequiredRecovery(
      this.request<ModelSourceResponse>("/api/v1/system/model-sources", {
        body: parseModelSourceCredentialRequest(body),
        handleAuthenticationFailure: false,
        method: "PUT",
        parse: parseModelSourceResponse,
        requiresCsrf: true,
      }),
    );
  }

  private async withAuthenticationRequiredRecovery<T>(request: Promise<T>) {
    try {
      return await request;
    } catch (error) {
      if (
        error instanceof ApiError &&
        error.status === 401 &&
        error.code === "authentication_required"
      ) {
        this.authenticationFailureHandler?.();
      }
      throw error;
    }
  }

  async getDownloadTasks(
    cursor?: string,
    status: readonly DownloadTaskStatus[] = [],
  ) {
    const query = new URLSearchParams();
    if (cursor !== undefined) query.set("cursor", cursor);
    for (const value of status) query.append("status", value);
    const suffix = query.size === 0 ? "" : `?${query.toString()}`;
    return this.request<DownloadTaskPage>(`/api/v1/downloads${suffix}`, {
      parse: parseDownloadTaskPage,
    });
  }

  async getModelArtifacts(
    options: {
      cursor?: string;
      search?: string;
      source?: string;
      status?: string;
      storageType?: string;
    } = {},
  ) {
    const query = new URLSearchParams();
    if (options.cursor !== undefined) query.set("cursor", options.cursor);
    if (options.search !== undefined) query.set("search", options.search);
    if (options.source !== undefined) query.set("source", options.source);
    if (options.status !== undefined) query.set("status", options.status);
    if (options.storageType !== undefined)
      query.set("storage_type", options.storageType);
    const suffix = query.size === 0 ? "" : `?${query.toString()}`;
    return this.request<ModelArtifactPage>(`/api/v1/artifacts${suffix}`, {
      parse: parseModelArtifactPage,
    });
  }

  async downloadStatusCounts() {
    return this.request<DownloadStatusCounts>(
      "/api/v1/downloads/status-counts",
      {
        parse: parseDownloadStatusCounts,
      },
    );
  }

  async artifactDetail(id: string) {
    return this.request<ModelArtifactDetail>(
      `/api/v1/artifacts/${encodeURIComponent(id)}`,
      {
        parse: parseModelArtifactDetail,
      },
    );
  }

  async deleteDownloadTask(id: string, cleanupStaging = false) {
    return this.request(
      `/api/v1/downloads/${encodeURIComponent(id)}?cleanup_staging=${String(cleanupStaging)}`,
      {
        method: "DELETE",
        requiresCsrf: true,
      },
    );
  }

  async executeDownloadCommand(
    id: string,
    command: DownloadCommand,
    cleanupStaging?: boolean,
  ) {
    const query =
      command === "cancel" && cleanupStaging !== undefined
        ? `?cleanup_staging=${String(cleanupStaging)}`
        : "";
    return this.request<DownloadCommandResponse>(
      `/api/v1/downloads/${encodeURIComponent(id)}/${command}${query}`,
      {
        body: {},
        method: "POST",
        parse: parseDownloadCommandResponse,
        requiresCsrf: true,
      },
    );
  }

  async deleteModelArtifact(id: string): Promise<ArtifactDeleteResponse> {
    return this.request<ArtifactDeleteResponse>(
      `/api/v1/artifacts/${encodeURIComponent(id)}`,
      {
        method: "DELETE",
        parse: parseArtifactDeleteResponse,
        requiresCsrf: true,
      },
    );
  }

  async verifyModelArtifact(id: string): Promise<ArtifactVerifyResponse> {
    return this.request<ArtifactVerifyResponse>(
      `/api/v1/artifacts/${encodeURIComponent(id)}:verify`,
      {
        method: "POST",
        body: {},
        requiresCsrf: true,
        parse: parseArtifactVerifyResponse,
      },
    );
  }

  async getArtifactVerificationStatus(
    id: string,
  ): Promise<ArtifactVerificationStatus> {
    return this.request<ArtifactVerificationStatus>(
      `/api/v1/artifacts/${encodeURIComponent(id)}/verification`,
      {
        parse: parseArtifactVerificationStatus,
      },
    );
  }

  async getArtifactDeletionStatus(id: string): Promise<ArtifactDeletionStatus> {
    return this.request<ArtifactDeletionStatus>(
      `/api/v1/artifacts/${encodeURIComponent(id)}/deletion`,
      {
        parse: parseArtifactDeletionStatus,
      },
    );
  }

  async getModelInstances() {
    return this.request<ModelInstanceList>("/api/v1/instances", {
      parse: parseModelInstanceList,
    });
  }

  async getModelDeploymentOptions(modelId: string) {
    const query = new URLSearchParams({ model_id: modelId });
    return this.request<ModelDeploymentOptions>(
      `/api/v1/runtimes?${query.toString()}`,
      { parse: parseModelDeploymentOptions },
    );
  }

  async getModelInstance(id: string) {
    return this.request<ModelInstance>(
      `/api/v1/instances/${encodeURIComponent(id)}`,
      { parse: parseModelInstance },
    );
  }

  async getModelInstanceLogs(id: string, cursor?: string, pageSize = 100) {
    const query = new URLSearchParams({ page_size: String(pageSize) });
    if (cursor !== undefined) query.set("cursor", cursor);
    return this.request<ModelInstanceLogPage>(
      `/api/v1/instances/${encodeURIComponent(id)}/logs?${query.toString()}`,
      { parse: parseModelInstanceLogPage },
    );
  }

  async getModelInstanceEvents(id: string, cursor?: string, pageSize = 20) {
    const query = new URLSearchParams({ page_size: String(pageSize) });
    if (cursor !== undefined) query.set("cursor", cursor);
    return this.request<ModelInstanceEventPage>(
      `/api/v1/instances/${encodeURIComponent(id)}/events?${query.toString()}`,
      { parse: parseModelInstanceEventPage },
    );
  }

  async getInstanceTelemetry(id: string) {
    return this.request<InstanceTelemetry>(
      `/api/v1/telemetry/instances/${encodeURIComponent(id)}`,
      { parse: parseInstanceTelemetry },
    );
  }

  async getServiceTelemetry(
    id: string,
    range: "1h" | "6h" | "24h" | "7d" = "1h",
  ) {
    return this.request<ServiceTelemetry>(
      `/api/v1/telemetry/services/${encodeURIComponent(id)}?range=${range}`,
      { parse: parseServiceTelemetry },
    );
  }

  async getGpuTelemetry() {
    return this.request<GpuTelemetryList>("/api/v1/telemetry/gpus", {
      parse: parseGpuTelemetryList,
    });
  }

  async getTelemetryOverview() {
    return this.request<OverviewTelemetry>("/api/v1/telemetry/overview", {
      parse: parseOverviewTelemetry,
    });
  }

  async getOverviewAttention() {
    return this.request<OverviewAttention>("/api/v1/overview/attention", {
      parse: parseOverviewAttention,
    });
  }

  async createModelInstance(body: CreateModelInstanceRequest) {
    return this.request<ModelInstance>("/api/v1/instances", {
      body: parseCreateModelInstanceRequest(body),
      method: "POST",
      parse: parseModelInstance,
      requiresCsrf: true,
    });
  }

  async startModelInstance(id: string) {
    return this.instanceCommand(id, "start");
  }

  async stopModelInstance(id: string) {
    return this.instanceCommand(id, "stop");
  }

  async restartModelInstance(id: string) {
    return this.instanceCommand(id, "restart");
  }

  async retryModelInstance(id: string) {
    return this.instanceCommand(id, "retry");
  }

  async selectModelInstanceGpus(id: string, gpuUuids: readonly string[]) {
    return this.request<ModelInstance>(
      `/api/v1/instances/${encodeURIComponent(id)}:select-gpus`,
      {
        body: { gpu_uuids: gpuUuids },
        method: "POST",
        parse: parseModelInstance,
        requiresCsrf: true,
      },
    );
  }

  async cancelModelInstanceDeployment(id: string) {
    await this.request(`/api/v1/instances/${encodeURIComponent(id)}:cancel`, {
      body: {},
      method: "POST",
      requiresCsrf: true,
    });
  }

  private async instanceCommand(
    id: string,
    command: "restart" | "retry" | "start" | "stop",
  ) {
    return this.request<ModelInstance>(
      `/api/v1/instances/${encodeURIComponent(id)}:${command}`,
      {
        body: {},
        method: "POST",
        parse: parseModelInstance,
        requiresCsrf: true,
      },
    );
  }

  async deleteModelInstance(id: string, deleteBoundServices = false) {
    const query = deleteBoundServices ? "?delete_bound_services=true" : "";
    await this.request(`/api/v1/instances/${encodeURIComponent(id)}${query}`, {
      method: "DELETE",
      requiresCsrf: true,
    });
  }

  async getAiServices() {
    return this.request<AiServiceList>("/api/v1/services", {
      parse: parseAiServiceList,
    });
  }

  async getAiService(id: string) {
    return this.request<AiService>(
      `/api/v1/services/${encodeURIComponent(id)}`,
      { parse: parseAiService },
    );
  }

  async getRequestRecords(options: RequestRecordFilters = {}) {
    const query = new URLSearchParams();
    if (options.apiKeyId !== undefined)
      query.set("api_key_id", options.apiKeyId);
    if (options.errorCode !== undefined)
      query.set("error_code", options.errorCode);
    if (options.from !== undefined) query.set("from", options.from);
    if (options.to !== undefined) query.set("to", options.to);
    if (options.cursor !== undefined) query.set("cursor", options.cursor);
    if (options.requestId !== undefined)
      query.set("request_id", options.requestId);
    if (options.serviceId !== undefined)
      query.set("service_id", options.serviceId);
    if (options.status !== undefined) query.set("status", options.status);
    if (options.userId !== undefined) query.set("user_id", options.userId);
    const suffix = query.size === 0 ? "" : `?${query.toString()}`;
    return this.request<RequestRecordPage>(`/api/v1/requests${suffix}`, {
      parse: parseRequestRecordPage,
    });
  }

  async getRequestRecordErrorGroups(options: RequestRecordErrorGroupFilters) {
    const query = new URLSearchParams({ from: options.from });
    if (options.to !== undefined) query.set("to", options.to);
    if (options.serviceId !== undefined)
      query.set("service_id", options.serviceId);
    return this.request<RequestRecordErrorGroups>(
      `/api/v1/requests/error-groups?${query.toString()}`,
      { parse: parseRequestRecordErrorGroups },
    );
  }

  async getUsageSummary(timeZone = "UTC") {
    const query = new URLSearchParams({ tz: timeZone });
    return this.request<UsageSummary>(
      `/api/v1/usage/summary?${query.toString()}`,
      { parse: parseUsageSummary },
    );
  }

  async getUsage(options: UsageFilters) {
    const query = new URLSearchParams({
      from: options.from,
      group_by: options.groupBy,
      to: options.to,
      tz: options.timeZone,
    });
    if (options.apiKeyId !== undefined)
      query.set("api_key_id", options.apiKeyId);
    if (options.serviceId !== undefined)
      query.set("service_id", options.serviceId);
    if (options.userId !== undefined) query.set("user_id", options.userId);
    return this.request<UsageReport>(`/api/v1/usage?${query.toString()}`, {
      parse: parseUsageReport,
    });
  }

  async createAiService(body: CreateAiServiceRequest) {
    return this.request<AiService>("/api/v1/services", {
      body: parseCreateAiServiceRequest(body),
      method: "POST",
      parse: parseAiService,
      requiresCsrf: true,
    });
  }

  async renameServiceDisplayName(
    id: string,
    body: RenameServiceDisplayNameRequest,
  ) {
    return this.request<AiService>(
      `/api/v1/services/${encodeURIComponent(id)}:rename-display-name`,
      {
        method: "POST",
        body: renameServiceDisplayNameSchema.parse(body),
        parse: parseAiService,
        requiresCsrf: true,
      },
    );
  }

  async renameAiService(id: string, body: RenameAiServiceRequest) {
    return this.request<AiService>(
      `/api/v1/services/${encodeURIComponent(id)}:rename`,
      {
        method: "POST",
        body: parseRenameAiServiceRequest(body),
        parse: parseAiService,
        requiresCsrf: true,
      },
    );
  }

  async deleteAiService(id: string) {
    await this.request(`/api/v1/services/${encodeURIComponent(id)}`, {
      method: "DELETE",
      requiresCsrf: true,
    });
  }

  async updateAiService(id: string, body: UpdateAiServiceRequest) {
    return this.request<AiService>(
      `/api/v1/services/${encodeURIComponent(id)}`,
      {
        body: parseUpdateAiServiceRequest(body),
        method: "PATCH",
        parse: parseAiService,
        requiresCsrf: true,
      },
    );
  }

  async getUsers(
    options: {
      includeDeleted?: boolean;
      role?: UserRole;
      search?: string;
      status?: UserStatus;
    } = {},
  ) {
    const query = new URLSearchParams();
    if (options.search !== undefined && options.search.length > 0)
      query.set("search", options.search);
    if (options.role !== undefined) query.set("role", options.role);
    if (options.status !== undefined) query.set("status", options.status);
    if (options.includeDeleted === true) query.set("include_deleted", "true");
    const suffix = query.size === 0 ? "" : `?${query.toString()}`;
    return this.request<UserList>(`/api/v1/users${suffix}`, {
      parse: parseUserList,
    });
  }

  async getUser(id: string) {
    return this.request<UserDetail>(`/api/v1/users/${encodeURIComponent(id)}`, {
      parse: parseUserDetail,
    });
  }

  async getUserActivity(
    userId: string,
    options: {
      actions?: UserActivityAction[];
      cursor?: string;
      limit?: number;
    } = {},
  ) {
    const query = new URLSearchParams();
    for (const action of options.actions ?? []) query.append("action", action);
    if (options.cursor !== undefined) query.set("cursor", options.cursor);
    if (options.limit !== undefined) query.set("limit", String(options.limit));
    const suffix = query.size ? `?${query.toString()}` : "";
    return this.request<UserActivityPage>(
      `/api/v1/users/${encodeURIComponent(userId)}/activity${suffix}`,
      { parse: parseUserActivityPage },
    );
  }

  async revokeUserApiKey(userId: string, keyId: string) {
    return this.request<SafeApiKey>(
      `/api/v1/users/${encodeURIComponent(userId)}/api-keys/${encodeURIComponent(keyId)}:revoke`,
      {
        body: {},
        method: "POST",
        parse: parseSafeApiKey,
        requiresCsrf: true,
      },
    );
  }

  async createUser(body: CreateUserRequest) {
    return this.request<CreatedUser>("/api/v1/users", {
      body: parseCreateUserRequest(body),
      method: "POST",
      parse: parseCreatedUser,
      requiresCsrf: true,
    });
  }

  async commandUser(id: string, command: "disable" | "enable" | "delete") {
    return this.request<UserOverview>(
      `/api/v1/users/${encodeURIComponent(id)}:${command}`,
      {
        body: {},
        method: "POST",
        parse: parseUserOverview,
        requiresCsrf: true,
      },
    );
  }

  async resetUserPassword(id: string) {
    return this.request<ResetPassword>(
      `/api/v1/users/${encodeURIComponent(id)}:reset-password`,
      {
        body: {},
        method: "POST",
        parse: parseResetPassword,
        requiresCsrf: true,
      },
    );
  }

  async replaceUserPermissions(
    id: string,
    serviceIds: readonly string[],
    limits?: PermissionLimitsMap,
  ) {
    return this.request<ServiceIds>(
      `/api/v1/users/${encodeURIComponent(id)}/permissions`,
      {
        body: {
          service_ids: serviceIds,
          ...(limits && Object.keys(limits).length ? { limits } : {}),
        },
        method: "PUT",
        parse: parseServiceIds,
        requiresCsrf: true,
      },
    );
  }

  async replaceServicePermissions(
    id: string,
    userIds: readonly string[],
    limits?: PermissionLimitsMap,
  ) {
    return this.request<UserIds>(
      `/api/v1/services/${encodeURIComponent(id)}/permissions`,
      {
        body: {
          user_ids: userIds,
          ...(limits && Object.keys(limits).length ? { limits } : {}),
        },
        method: "PUT",
        parse: parseUserIds,
        requiresCsrf: true,
      },
    );
  }

  async getServicePermissions(id: string) {
    return this.request<UserIds>(
      `/api/v1/services/${encodeURIComponent(id)}/permissions`,
      { parse: parseUserIds },
    );
  }

  async getMyProfile() {
    return this.request<PersonalProfile>("/api/v1/me/profile", {
      parse: parsePersonalProfile,
    });
  }

  async updateMyProfile(body: UpdateMyProfileRequest) {
    return this.request<PersonalProfile>("/api/v1/me/profile", {
      body,
      method: "PATCH",
      parse: parsePersonalProfile,
      requiresCsrf: true,
    });
  }

  async getMyAiServices() {
    return this.request<PersonalAiServiceList>("/api/v1/me/services", {
      parse: parsePersonalAiServiceList,
    });
  }

  async getApiKeys() {
    return this.request<ApiKeyList>("/api/v1/me/api-keys", {
      parse: parseApiKeyList,
    });
  }

  async createApiKey(body: CreateApiKeyRequest) {
    return this.request<CreatedApiKey>("/api/v1/me/api-keys", {
      body: parseCreateApiKeyRequest(body),
      method: "POST",
      parse: parseCreatedApiKey,
      requiresCsrf: true,
    });
  }

  async renameApiKey(id: string, body: RenameApiKeyRequest) {
    return this.request<ApiKey>(
      `/api/v1/me/api-keys/${encodeURIComponent(id)}`,
      {
        body: parseRenameApiKeyRequest(body),
        method: "PATCH",
        parse: parseApiKey,
        requiresCsrf: true,
      },
    );
  }

  async deleteApiKey(id: string) {
    return this.request<ApiKey>(
      `/api/v1/me/api-keys/${encodeURIComponent(id)}`,
      {
        method: "DELETE",
        parse: parseApiKey,
        requiresCsrf: true,
      },
    );
  }

  async revokeApiKey(id: string) {
    return this.request<ApiKey>(
      `/api/v1/me/api-keys/${encodeURIComponent(id)}:revoke`,
      {
        body: {},
        method: "POST",
        parse: parseApiKey,
        requiresCsrf: true,
      },
    );
  }

  /**
   * The three Bootstrap calls.
   *
   * All of them are anonymous and carry the one-time Token in a request header,
   * never in the URL. `handleAuthenticationFailure: false`
   * keeps their 401 — "this Token is not usable" — from being mistaken for an
   * expired *session* and clearing one the caller may still hold.
   */
  async getBootstrapStatus(token: string) {
    return this.request<BootstrapStatusResponse>(BOOTSTRAP_STATUS_PATH, {
      extraHeaders: { [BOOTSTRAP_TOKEN_HEADER]: token },
      handleAuthenticationFailure: false,
      parse: parseBootstrapStatusResponse,
    });
  }

  async runBootstrapChecks(token: string) {
    return this.request<BootstrapCheckReport>(BOOTSTRAP_CHECK_PATH, {
      body: {},
      extraHeaders: { [BOOTSTRAP_TOKEN_HEADER]: token },
      handleAuthenticationFailure: false,
      method: "POST",
      parse: parseBootstrapCheckReport,
    });
  }

  async createBootstrapAdministrator(
    token: string,
    body: BootstrapAdminRequest,
  ) {
    return this.request<BootstrapAdminResponse>(BOOTSTRAP_ADMIN_PATH, {
      body,
      extraHeaders: { [BOOTSTRAP_TOKEN_HEADER]: token },
      handleAuthenticationFailure: false,
      method: "POST",
      parse: parseBootstrapAdminResponse,
    });
  }

  async previewDiagnostics(
    value: DiagnosticRequest,
  ): Promise<DiagnosticPreview> {
    const input = diagnosticRequestSchema.parse(value);
    const query = new URLSearchParams({
      window_minutes: String(input.window_minutes),
    });
    if (input.end_at) query.set("end_at", input.end_at);
    if (input.instance_id) query.set("instance_id", input.instance_id);
    input.request_ids.forEach((id) => {
      query.append("request_id", id);
    });
    return this.request<DiagnosticPreview>(
      `/api/v1/diagnostics?${query.toString()}`,
      {
        parseResponse: async (response) => {
          const receipt = response.headers.get("X-Diagnostic-Preview");
          const ttl = Number(response.headers.get("X-Diagnostic-Preview-TTL"));
          if (
            !receipt ||
            !/^[A-Za-z0-9_-]{43}$/.test(receipt) ||
            !Number.isFinite(ttl) ||
            ttl <= 0 ||
            ttl > 300
          ) {
            throw new ApiError({
              code: "invalid_diagnostic_response",
              status: response.status,
              requestId: response.headers.get("x-request-id") ?? undefined,
            });
          }
          const parsed = diagnosticManifestSchema.safeParse(
            await readJson(response),
          );
          if (!parsed.success)
            throw new ApiError({
              code: "invalid_diagnostic_response",
              status: response.status,
              requestId: response.headers.get("x-request-id") ?? undefined,
            });
          return {
            manifest: parsed.data,
            receipt,
            expiresAt: Date.now() + ttl * 1000,
          };
        },
      },
    );
  }

  async downloadDiagnostics(
    value: DiagnosticRequest,
    receipt: string,
  ): Promise<Blob> {
    return this.request<Blob>("/api/v1/diagnostics", {
      body: {
        ...diagnosticRequestSchema.parse(value),
        preview_receipt: receipt,
      },
      method: "POST",
      requiresCsrf: true,
      extraHeaders: { Accept: "application/zip" },
      parseResponse: readDiagnosticZip,
    });
  }

  async login(body: LoginRequest) {
    await this.request("/api/v1/auth/login", {
      body,
      handleAuthenticationFailure: false,
      method: "POST",
    });
  }

  async logout() {
    await this.request("/api/v1/auth/logout", {
      method: "POST",
      requiresCsrf: true,
    });
  }

  async changePassword(body: PasswordChangeRequest) {
    await this.withAuthenticationRequiredRecovery(
      this.request("/api/v1/auth/password", {
        body,
        handleAuthenticationFailure: false,
        method: "POST",
        requiresCsrf: true,
      }),
    );
  }

  async request<T = void>(path: string, options: RequestOptions = {}) {
    const headers = new Headers({ Accept: "application/json" });
    if (options.body !== undefined) {
      headers.set("Content-Type", "application/json");
    }
    for (const [name, value] of Object.entries(options.extraHeaders ?? {})) {
      headers.set(name, value);
    }
    if (options.requiresCsrf === true) {
      const csrfToken = readCsrfCookie();
      if (csrfToken === undefined || csrfToken.length === 0) {
        throw new ApiError({ code: "csrf_token_unavailable", status: 0 });
      }
      headers.set(CSRF_HEADER_NAME, csrfToken);
    }

    const response = await fetch(path, {
      body:
        options.body === undefined ? undefined : JSON.stringify(options.body),
      credentials: "same-origin",
      headers,
      method: options.method ?? "GET",
    });

    if (!response.ok) {
      const parsed = safeErrorEnvelope(await readJson(response));
      const headerRequestId = response.headers.get("x-request-id") ?? undefined;
      const error = new ApiError({
        code: parsed.success ? parsed.data.error.code : "request_failed",
        details: parsed.success ? parsed.data.error.details : undefined,
        requestId: parsed.success
          ? (parsed.data.error.request_id ?? headerRequestId)
          : headerRequestId,
        responseMessage: parsed.success ? parsed.data.error.message : undefined,
        retryAfterSeconds: parsed.success
          ? parsed.data.error.details?.retry_after_seconds
          : undefined,
        status: response.status,
      });
      if (
        response.status === 401 &&
        options.handleAuthenticationFailure !== false
      ) {
        this.authenticationFailureHandler?.();
      }
      throw error;
    }

    if (options.parseResponse !== undefined) {
      return (await options.parseResponse(response)) as T;
    }
    if (options.parse === undefined) {
      return undefined as T;
    }
    return options.parse(await readJson(response)) as T;
  }
}

async function readJson(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    return undefined;
  }
}

export const browserApiClient = new ApiClient();

async function readDiagnosticZip(response: Response): Promise<Blob> {
  const maxBytes = 3 * 1024 * 1024;
  const invalid = () =>
    new ApiError({
      code: "invalid_diagnostic_response",
      status: response.status,
      requestId: response.headers.get("x-request-id") ?? undefined,
    });
  if (
    response.headers.get("content-type")?.split(";")[0]?.trim() !==
      "application/zip" ||
    Number(response.headers.get("content-length")) > maxBytes ||
    response.body === null
  ) {
    await response.body?.cancel();
    throw invalid();
  }
  const reader = response.body.getReader();
  const parts: ArrayBuffer[] = [];
  let size = 0;
  try {
    let part = await reader.read();
    while (!part.done) {
      size += part.value.byteLength;
      if (size > maxBytes) {
        await reader.cancel();
        throw invalid();
      }
      parts.push(new Uint8Array(part.value).buffer);
      part = await reader.read();
    }
  } finally {
    reader.releaseLock();
  }
  const blob = new Blob(parts, { type: "application/zip" });
  const signature = new Uint8Array(await blob.slice(0, 4).arrayBuffer());
  if (signature.join(",") !== "80,75,3,4") throw invalid();
  return blob;
}
