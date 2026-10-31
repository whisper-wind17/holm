import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import type { ReactNode } from "react";

import { ApiClient, ApiError } from "../api/client";
import type {
  BootstrapAdminRequest,
  BootstrapAdminResponse,
  BootstrapCheckReport,
  BootstrapStatusResponse,
} from "../api/bootstrap-contracts";
import type {
  LoginRequest,
  MeResponse,
  PasswordChangeRequest,
} from "../api/contracts";
import type {
  AgentObservation,
  GpuDevice,
  SystemComponentsResponse,
  SystemServerResponse,
} from "../api/system-contracts";
import { SYSTEM_COMPONENT_IDS } from "../api/system-contracts";
import type {
  DownloadCommand,
  DownloadCommandResponse,
  DownloadTaskPage,
  ModelArtifactPage,
} from "../api/model-acquisition-contracts";
import type {
  ModelSourceCredentialRequest,
  ModelSourceResponse,
} from "../api/model-source-contracts";
import type {
  DownloadCreationResponse,
  ModelCatalogPage,
  RegistrationResponse,
  ResolveRequest,
  ResolveResponse,
} from "../api/model-selection-contracts";
import type {
  CreateModelInstanceRequest,
  ModelInstance,
  ModelInstanceList,
  ModelInstanceLogPage,
} from "../api/model-instance-contracts";
import type {
  AiService,
  AiServiceList,
  CreateAiServiceRequest,
  UpdateAiServiceRequest,
} from "../api/ai-service-contracts";
import type {
  ApiKey,
  ApiKeyList,
  CreateApiKeyRequest,
  CreatedApiKey,
} from "../api/api-key-contracts";
import type {
  RequestRecordFilters,
  RequestRecordPage,
} from "../api/request-record-contracts";
import type {
  GpuTelemetryList,
  InstanceTelemetry,
  OverviewAttention,
  OverviewTelemetry,
  ServiceTelemetry,
} from "../api/telemetry-contracts";
import type {
  CreatedUser,
  CreateUserRequest,
  PersonalAiServiceList,
  PersonalProfile,
  UpdateMyProfileRequest,
  ResetPassword,
  ServiceIds,
  UserDetail,
  UserIds,
  UserList,
  UserOverview,
  UserRole,
  UserStatus,
} from "../api/user-contracts";
import type {
  UsageFilters,
  UsageReport,
  UsageSummary,
} from "../api/usage-contracts";
import type { ModelDeploymentOptions } from "../api/deployment-options-contracts";
import { SessionProvider } from "../auth/session";
import { ApplicationRoutes } from "./application";
import { toThrowableError } from "./errors";

export const ADMIN_ME: MeResponse = {
  capabilities: [
    { capability: "view_system", scope: "allowed" },
    { capability: "manage_api_keys", scope: "own_keys" },
    { capability: "view_request_records", scope: "call_metadata" },
  ],
  must_change_password: false,
  navigation: [
    {
      id: "overview",
      label: "总览",
      required_capability: "view_infrastructure",
    },
    {
      id: "ai_services",
      label: "AI 服务",
      required_capability: "view_ai_services",
    },
    { id: "models", label: "模型", required_capability: "manage_local_models" },
    { id: "users", label: "用户", required_capability: "manage_users" },
    { id: "usage", label: "用量", required_capability: "view_usage" },
    { id: "system", label: "系统", required_capability: "view_system" },
  ],
  role: "admin",
  user: {
    display_name: "平台管理员",
    id: "0198f58a-2586-7e37-bbf5-fefe165e8080",
    username: "admin",
  },
};

export const USER_ME: MeResponse = {
  capabilities: [
    { capability: "manage_api_keys", scope: "own_keys" },
    { capability: "view_usage", scope: "own" },
  ],
  must_change_password: false,
  navigation: [
    {
      id: "ai_services",
      label: "AI 服务",
      required_capability: "view_ai_services",
    },
    {
      id: "api_keys",
      label: "API 密钥",
      required_capability: "manage_api_keys",
    },
    { id: "usage", label: "用量", required_capability: "view_usage" },
  ],
  role: "user",
  user: {
    display_name: "普通用户",
    id: "0198f58a-2586-7e37-bbf5-fefe165e8081",
    username: "user",
  },
};

export const SYSTEM_OBSERVED_AT = "2026-09-01T08:42:08Z";

export const DEFAULT_SYSTEM_SERVER: SystemServerResponse = {
  agent: {
    gpus: {
      devices: [],
      discovery_state: "no_devices",
      failure_reason: "no_devices",
      snapshot_id: null,
      topology_links: [],
    },
    host: {
      available_disk_bytes: { state: "known", value: 536_870_912_000 },
      cuda_driver_version: { state: "known", value: "13.2" },
      driver_version: { state: "known", value: "595.45.04" },
      logical_cpu_count: { state: "known", value: 128 },
      total_disk_bytes: { state: "known", value: 1_099_511_627_776 },
      total_memory_bytes: { state: "known", value: 549_755_813_888 },
    },
    status: "available",
  },
  observed_at: SYSTEM_OBSERVED_AT,
};

export function systemServerWithUnavailableGpu(): SystemServerResponse {
  const server = structuredClone(DEFAULT_SYSTEM_SERVER);
  if (server.agent.status !== "available") {
    throw new Error("FX-NV-01 fixture requires an available Agent");
  }
  server.agent.gpus = {
    devices: [],
    discovery_state: "unavailable",
    failure_reason: "permission_denied",
    snapshot_id: null,
    topology_links: [],
  };
  return server;
}

export const DEFAULT_SYSTEM_COMPONENTS: SystemComponentsResponse = {
  host_environment: {
    cuda_driver_version: { state: "known", value: "13.0" },
    items: (
      [
        "nvidia_driver",
        "docker",
        "compose",
        "nvidia_container_toolkit",
      ] as const
    ).map((id) => ({
      id,
      current: { state: "unknown", reason: "not_collected" },
      minimum: null,
      status: "unknown",
      reason: "current_unavailable",
    })),
  },
  inference_engines: [],
  components: SYSTEM_COMPONENT_IDS.map((id) => ({
    details:
      id === "Request Ledger Writer"
        ? {
            kind: "request_ledger_writer" as const,
            oldest_event_at: {
              reason: "not_collected" as const,
              state: "unknown" as const,
            },
            pending_event_count: {
              reason: "not_collected" as const,
              state: "unknown" as const,
            },
          }
        : { kind: "none" as const },
    id,
    reason:
      id === "Control Plane" || id === "Database"
        ? ("self_ready" as const)
        : ("no_signal_source" as const),
    signal_sources: [],
    status:
      id === "Control Plane" || id === "Database"
        ? ("ready" as const)
        : ("not_deployed" as const),
    version:
      id === "Control Plane" || id === "Database"
        ? ({ state: "known" as const, value: "0.1.0" } as const)
        : ({
            reason: "not_deployed" as const,
            state: "unknown" as const,
          } as const),
  })),
  observed_at: SYSTEM_OBSERVED_AT,
  platform: { build_version: "0.1.0" },
  summary: {
    abnormal_count: 0,
    not_deployed_count: 8,
    ready_count: 2,
    status: "ready",
  },
};

export const DEFAULT_MODEL_SOURCES: ModelSourceResponse[] = [
  {
    configuration_state: "configured",
    credential_last4: "8f2a",
    next_action: null,
    source_type: "Hugging Face",
    verification_reason_code: "credential_valid",
    verification_status: "accessible",
    verified_at: SYSTEM_OBSERVED_AT,
  },
  {
    configuration_state: "unconfigured",
    credential_last4: null,
    next_action: null,
    source_type: "ModelScope",
    verification_reason_code: null,
    verification_status: null,
    verified_at: null,
  },
];

export const BOOTSTRAP_OBSERVED_AT = "2026-09-02T03:20:00Z";
export const BOOTSTRAP_TOKEN_EXPIRES_AT = "2026-09-02T03:50:00Z";

/** a device list of any length, so no test assumes 1/2/4 cards. */
export function bootstrapGpuDevices(count: number): GpuDevice[] {
  return Array.from({ length: count }, (_, index) => ({
    display_index: index,
    health: "healthy" as const,
    health_reasons: [],
    memory_bytes: 51_539_607_552,
    model: "NVIDIA L20",
    uuid: `GPU-fixture-${String(index)}`,
  }));
}

type AvailableAgent = Extract<AgentObservation, { status: "available" }>;

export function bootstrapAgent(deviceCount = 8): AvailableAgent {
  return {
    gpus: {
      devices: bootstrapGpuDevices(deviceCount),
      discovery_state: "ready",
      failure_reason: "none",
      snapshot_id: "snapshot-fixture",
      topology_links: [],
    },
    host: {
      available_disk_bytes: { state: "known", value: 3_298_534_883_328 },
      cuda_driver_version: { state: "known", value: "13.2" },
      driver_version: { state: "known", value: "595.45.04" },
      logical_cpu_count: { state: "known", value: 128 },
      total_disk_bytes: { state: "known", value: 7_696_581_394_432 },
      total_memory_bytes: { state: "known", value: 549_755_813_888 },
    },
    status: "available",
  };
}

/** Every detection card passing, which is the only state that lets step three run. */
export const PASSING_BOOTSTRAP_REPORT: BootstrapCheckReport = {
  agent: bootstrapAgent(),
  host_environment: {
    cuda_driver_version: { state: "known", value: "13.2" },
    items: [
      {
        id: "nvidia_driver",
        current: { state: "known", value: "595.45.04" },
        minimum: "580.65.06",
        status: "satisfied",
        reason: "meets_minimum",
      },
      {
        id: "docker",
        current: { state: "known", value: "28.3.2" },
        minimum: "27.0",
        status: "satisfied",
        reason: "meets_minimum",
      },
      {
        id: "compose",
        current: { state: "known", value: "2.38.2" },
        minimum: "2.20",
        status: "satisfied",
        reason: "meets_minimum",
      },
      {
        id: "nvidia_container_toolkit",
        current: { state: "known", value: "1.17.8" },
        minimum: "1.16",
        status: "satisfied",
        reason: "meets_minimum",
      },
    ],
  },
  blocking: [],
  can_complete: true,
  checks: [
    { id: "gpu", outcome: "passed", reasons: ["gpu_devices_present"] },
    {
      id: "nvidia_driver",
      outcome: "passed",
      reasons: ["driver_meets_minimum"],
    },
    {
      id: "docker_compose",
      outcome: "passed",
      reasons: ["docker_compose_meets_minimum"],
    },
    {
      id: "nvidia_container_toolkit",
      outcome: "passed",
      reasons: ["toolkit_meets_minimum"],
    },
    { id: "disk", outcome: "passed", reasons: ["disk_sufficient"] },
    { id: "memory", outcome: "passed", reasons: ["memory_sufficient"] },
    {
      id: "holm_components",
      outcome: "passed",
      reasons: ["components_ready"],
    },
  ],
  observed_at: BOOTSTRAP_OBSERVED_AT,
};

// authorized personal service metadata for creation and target selection.
export const UI_46_PERSONAL_SERVICE = {
  id: "45000000-0000-4000-8000-000000000001",
  name: "研发助手",
  public_model_name: "qwen-mini",
  description: "适合日常对话",
  available: true,
  enabled: true,
  last_used_at: null,
  last_succeeded_at: null,
};

export class FakeApiClient extends ApiClient {
  changePasswordErrors: unknown[] = [];
  loginError: unknown;
  loginResult: MeResponse | undefined;
  me: MeResponse | undefined;
  readonly writes: string[] = [];
  systemComponentsResults: Array<SystemComponentsResponse | Error> = [];
  systemServerResults: Array<SystemServerResponse | Error> = [];
  systemComponentsCalls = 0;
  systemServerCalls = 0;
  downloadTaskResults: Array<DownloadTaskPage | Error> = [];
  modelArtifactResults: Array<ModelArtifactPage | Error> = [];
  modelSourceResults: Array<ModelSourceResponse[] | Error> = [];
  modelSourceUpdateResults: Array<ModelSourceResponse | Error> = [];
  modelSourceCalls = 0;
  downloadCommandErrors: unknown[] = [];
  readonly downloadTaskCursors: Array<string | undefined> = [];
  readonly modelArtifactCalls: Array<{
    cursor?: string;
    search?: string;
    source?: string;
    status?: string;
    storageType?: string;
  }> = [];
  readonly downloadCommandCalls: Array<{
    command: DownloadCommand;
    id: string;
  }> = [];
  modelCatalogResults: Array<ModelCatalogPage | Error> = [];
  resolveModelResults: Array<ResolveResponse | Error> = [];
  registrationResults: Array<RegistrationResponse | Error> = [];
  downloadCreationResults: Array<DownloadCreationResponse | Error> = [];
  readonly modelCatalogCursors: Array<string | undefined> = [];
  readonly modelWriteCalls: Array<{
    body: ResolveRequest;
    idempotencyKey?: string;
    path: "/api/v1/downloads" | "/api/v1/models" | "/api/v1/models/resolve";
  }> = [];
  modelInstanceResults: Array<ModelInstanceList | Error> = [];
  modelInstanceDetailResults: Array<ModelInstance | Error> = [];
  modelInstanceLogResults: Array<ModelInstanceLogPage | Error> = [];
  modelInstanceCreateResults: Array<ModelInstance | Error> = [];
  modelInstanceCommandResults: Array<ModelInstance | Error> = [];
  modelDeploymentOptionsResults: Array<ModelDeploymentOptions | Error> = [];
  readonly modelDeploymentOptionsCalls: string[] = [];
  readonly modelInstanceCreateCalls: CreateModelInstanceRequest[] = [];
  readonly modelInstanceCommandCalls: Array<{
    command: "delete" | "start" | "stop";
    deleteBoundServices?: boolean;
    id: string;
  }> = [];
  readonly modelInstanceLogCalls: Array<{
    cursor?: string;
    id: string;
    pageSize: number;
  }> = [];
  aiServiceResults: Array<AiServiceList | Error> = [];
  aiServiceDetailResults: Array<AiService | Error> = [];
  aiServiceCreateResults: Array<AiService | Error> = [];
  aiServiceUpdateResults: Array<AiService | Error> = [];
  aiServiceCalls = 0;
  modelInstanceCalls = 0;
  readonly aiServiceCreateCalls: CreateAiServiceRequest[] = [];
  readonly aiServiceUpdateCalls: Array<{
    body: UpdateAiServiceRequest;
    id: string;
  }> = [];
  apiKeyResults: Array<ApiKeyList | Error> = [];
  apiKeyCreateResults: Array<CreatedApiKey | Error> = [];
  apiKeyRevokeResults: Array<ApiKey | Error> = [];
  readonly apiKeyCreateCalls: CreateApiKeyRequest[] = [];
  readonly apiKeyRevokeCalls: string[] = [];
  requestRecordResults: Array<RequestRecordPage | Error> = [];
  readonly requestRecordCalls: RequestRecordFilters[] = [];
  usageSummaryResults: Array<UsageSummary | Error> = [];
  usageResults: Array<UsageReport | Error> = [];
  readonly usageSummaryCalls: string[] = [];
  readonly usageCalls: UsageFilters[] = [];
  instanceTelemetryResults: Array<InstanceTelemetry | Error> = [];
  gpuTelemetryResults: Array<GpuTelemetryList | Error> = [];
  overviewTelemetryResults: Array<OverviewTelemetry | Error> = [];
  serviceTelemetryResults: Array<ServiceTelemetry | Error> = [];
  overviewAttentionResults: Array<OverviewAttention | Error> = [];
  userResults: Array<UserList | Error> = [];
  userDetailResults: Array<UserDetail | Error> = [];
  userCreateResults: Array<CreatedUser | Error> = [];
  userCommandResults: Array<UserOverview | Error> = [];
  userResetResults: Array<ResetPassword | Error> = [];
  userPermissionResults: Array<ServiceIds | Error> = [];
  servicePermissionReadResults: Array<UserIds | Error> = [];
  servicePermissionResults: Array<UserIds | Error> = [];
  myProfileResults: Array<PersonalProfile | Error> = [];
  updateMyProfileCalls: UpdateMyProfileRequest[] = [];
  getMyProfileCalls = 0;
  myAiServiceResults: Array<PersonalAiServiceList | Error> = [];
  readonly userListCalls: Array<{
    includeDeleted?: boolean;
    role?: UserRole;
    search?: string;
    status?: UserStatus;
  }> = [];
  readonly userCreateCalls: CreateUserRequest[] = [];
  readonly userCommandCalls: Array<{
    command: "disable" | "enable" | "delete";
    id: string;
  }> = [];
  readonly userPermissionCalls: Array<{
    id: string;
    serviceIds: readonly string[];
  }> = [];
  readonly servicePermissionCalls: Array<{
    id: string;
    userIds: readonly string[];
  }> = [];
  readonly servicePermissionReadCalls: string[] = [];
  getMyAiServicesCalls = 0;
  /** Default: a machine that was set up long ago, so every Bootstrap path 404s. */
  bootstrapInitialized = true;
  bootstrapCheckDefault: BootstrapCheckReport | Error =
    PASSING_BOOTSTRAP_REPORT;
  bootstrapCheckResults: (BootstrapCheckReport | Error)[] = [];
  bootstrapAdminErrors: unknown[] = [];
  bootstrapAdminResult: MeResponse | undefined;
  readonly bootstrapStatusCalls: string[] = [];
  readonly bootstrapCheckCalls: string[] = [];
  readonly bootstrapAdminCalls: {
    body: BootstrapAdminRequest;
    token: string;
  }[] = [];

  constructor(me?: MeResponse) {
    super();
    this.me = me;
  }

  override getMe() {
    if (this.me === undefined) {
      return Promise.reject(
        new ApiError({ code: "authentication_required", status: 401 }),
      );
    }
    return Promise.resolve(this.me);
  }

  override login(body: LoginRequest) {
    void body;
    if (this.loginError !== undefined) {
      return Promise.reject(toThrowableError(this.loginError));
    }
    this.me = this.loginResult ?? ADMIN_ME;
    this.writes.push("login");
    return Promise.resolve();
  }

  override logout() {
    this.me = undefined;
    this.writes.push("logout");
    return Promise.resolve();
  }

  override changePassword(body: PasswordChangeRequest) {
    void body;
    const failure = this.changePasswordErrors.shift();
    if (failure !== undefined) {
      return Promise.reject(toThrowableError(failure));
    }
    if (this.me === undefined) {
      return Promise.reject(
        new ApiError({ code: "authentication_required", status: 401 }),
      );
    }
    this.me = { ...this.me, must_change_password: false };
    this.writes.push("password");
    return Promise.resolve();
  }

  override getBootstrapStatus(token: string) {
    this.bootstrapStatusCalls.push(token);
    if (this.bootstrapInitialized) {
      return Promise.reject(new ApiError({ code: "not_found", status: 404 }));
    }
    if (token.length === 0) {
      return Promise.reject(
        new ApiError({ code: "bootstrap_token_invalid", status: 401 }),
      );
    }
    const response: BootstrapStatusResponse = {
      initialized: false,
      token_expires_at: BOOTSTRAP_TOKEN_EXPIRES_AT,
    };
    return Promise.resolve(response);
  }

  override runBootstrapChecks(token: string) {
    this.bootstrapCheckCalls.push(token);
    const result =
      this.bootstrapCheckResults.shift() ?? this.bootstrapCheckDefault;
    return result instanceof Error
      ? Promise.reject(result)
      : Promise.resolve(result);
  }

  override createBootstrapAdministrator(
    token: string,
    body: BootstrapAdminRequest,
  ) {
    this.bootstrapAdminCalls.push({ body, token });
    const failure = this.bootstrapAdminErrors.shift();
    if (failure !== undefined) {
      return Promise.reject(toThrowableError(failure));
    }
    this.writes.push("bootstrap-admin");
    // The real endpoint hands back a logged-in session, so the fake starts
    // answering /me the way a Set-Cookie would have made the browser do.
    this.me = this.bootstrapAdminResult ?? ADMIN_ME;
    this.bootstrapInitialized = true;
    const response: BootstrapAdminResponse = {
      csrf_token: "csrf-from-bootstrap",
      must_change_password: false,
      system_initialized_at: BOOTSTRAP_OBSERVED_AT,
    };
    return Promise.resolve(response);
  }

  override getSystemServer() {
    this.systemServerCalls += 1;
    const result = this.systemServerResults.shift() ?? DEFAULT_SYSTEM_SERVER;
    return result instanceof Error
      ? Promise.reject(result)
      : Promise.resolve(result);
  }

  override getSystemComponents() {
    this.systemComponentsCalls += 1;
    const result =
      this.systemComponentsResults.shift() ?? DEFAULT_SYSTEM_COMPONENTS;
    return result instanceof Error
      ? Promise.reject(result)
      : Promise.resolve(result);
  }

  override getModelSources() {
    this.modelSourceCalls += 1;
    const result =
      this.modelSourceResults.shift() ?? structuredClone(DEFAULT_MODEL_SOURCES);
    return result instanceof Error
      ? Promise.reject(result)
      : Promise.resolve(result);
  }

  override updateModelSource(body: ModelSourceCredentialRequest) {
    void body;
    const result = this.modelSourceUpdateResults.shift();
    if (result instanceof Error) return Promise.reject(result);
    if (result !== undefined) return Promise.resolve(result);
    const existing = DEFAULT_MODEL_SOURCES.find(
      ({ source_type }) => source_type === body.source_type,
    );
    if (existing === undefined) {
      return Promise.reject(new Error("unknown model source fixture"));
    }
    return Promise.resolve(structuredClone(existing));
  }

  override getDownloadTasks(cursor?: string) {
    this.downloadTaskCursors.push(cursor);
    const result = this.downloadTaskResults.shift() ?? {
      items: [],
      next_cursor: null,
    };
    return result instanceof Error
      ? Promise.reject(result)
      : Promise.resolve(result);
  }

  override getModelCatalog(cursor?: string) {
    this.modelCatalogCursors.push(cursor);
    const result = this.modelCatalogResults.shift() ?? {
      items: [],
      next_cursor: null,
    };
    return result instanceof Error
      ? Promise.reject(result)
      : Promise.resolve(result);
  }

  override resolveModel(body: ResolveRequest) {
    this.modelWriteCalls.push({ body, path: "/api/v1/models/resolve" });
    const result = this.resolveModelResults.shift();
    if (result === undefined)
      return Promise.reject(new Error("resolve fixture is missing"));
    return result instanceof Error
      ? Promise.reject(result)
      : Promise.resolve(result);
  }

  override registerModel(body: ResolveRequest, idempotencyKey: string) {
    this.modelWriteCalls.push({
      body,
      idempotencyKey,
      path: "/api/v1/models",
    });
    const result = this.registrationResults.shift();
    if (result === undefined)
      return Promise.reject(new Error("registration fixture is missing"));
    return result instanceof Error
      ? Promise.reject(result)
      : Promise.resolve(result);
  }

  override createDownload(body: ResolveRequest, idempotencyKey: string) {
    this.modelWriteCalls.push({
      body,
      idempotencyKey,
      path: "/api/v1/downloads",
    });
    const result = this.downloadCreationResults.shift();
    if (result === undefined)
      return Promise.reject(new Error("download creation fixture is missing"));
    return result instanceof Error
      ? Promise.reject(result)
      : Promise.resolve(result);
  }

  override getModelArtifacts(
    options: {
      cursor?: string;
      search?: string;
      source?: string;
      status?: string;
      storageType?: string;
    } = {},
  ) {
    this.modelArtifactCalls.push(options);
    const result = this.modelArtifactResults.shift() ?? {
      items: [],
      next_cursor: null,
      summary: {
        active_download_tasks: 0,
        managed_bytes: 0,
        storage_available_bytes: 0,
      },
    };
    return result instanceof Error
      ? Promise.reject(result)
      : Promise.resolve(result);
  }

  override executeDownloadCommand(id: string, command: DownloadCommand) {
    this.downloadCommandCalls.push({ command, id });
    const failure = this.downloadCommandErrors.shift();
    if (failure !== undefined) return Promise.reject(toThrowableError(failure));
    const response: DownloadCommandResponse = {
      attempt: 1,
      command,
      id,
      staging_cleanup_requested: command === "cancel" || command === "retry",
      status:
        command === "pause"
          ? "Paused"
          : command === "cancel"
            ? "Cancelled"
            : "Resolving",
    };
    return Promise.resolve(response);
  }

  override getModelInstances() {
    this.modelInstanceCalls += 1;
    const result = this.modelInstanceResults.shift() ?? { items: [] };
    return result instanceof Error
      ? Promise.reject(result)
      : Promise.resolve(result);
  }

  override getModelDeploymentOptions(modelId: string) {
    this.modelDeploymentOptionsCalls.push(modelId);
    const result = this.modelDeploymentOptionsResults.shift() ?? {
      model_context_limit: { state: "unrecognized" as const },
      runtimes: [
        {
          certified_gpu_counts: [1, 2, 3, 4, 5, 6, 7, 8],
          context_lengths: [2048, 4096, 8192, 16384, 32768],
          default_context_length: 4096,
          max_gpus_per_instance: 8,
          name: "vllm" as const,
          version: "0.29.0",
        },
      ],
    };
    return result instanceof Error
      ? Promise.reject(result)
      : Promise.resolve(result);
  }

  override getModelInstance(id: string) {
    void id;
    const result = this.modelInstanceDetailResults.shift();
    if (result === undefined)
      return Promise.reject(new Error("model instance fixture is missing"));
    return result instanceof Error
      ? Promise.reject(result)
      : Promise.resolve(result);
  }

  override getModelInstanceLogs(id: string, cursor?: string, pageSize = 100) {
    this.modelInstanceLogCalls.push({ cursor, id, pageSize });
    const result = this.modelInstanceLogResults.shift() ?? {
      entries: [],
      next_cursor: null,
    };
    return result instanceof Error
      ? Promise.reject(result)
      : Promise.resolve(result);
  }

  override getInstanceTelemetry(id: string) {
    const result = this.instanceTelemetryResults.shift() ?? {
      window_1h: {
        requests: null,
        error_rate: null,
        ttft_p95_ms: null,
        started_at: null,
      },
      current: {
        kv_cache_usage_ratio: null,
        output_tps: null,
        queue_time_p50_seconds: null,
        queue_time_p95_seconds: null,
        running: null,
        ttft_p50_seconds: null,
        ttft_p95_seconds: null,
        ttft_p99_seconds: null,
        unsupported: [],
        waiting: null,
      },
      instance_id: id,
      sampled_at: null,
      series: [],
      quality: "partial" as const,
      telemetry_status: "unavailable" as const,
    };
    return result instanceof Error
      ? Promise.reject(result)
      : Promise.resolve(result);
  }

  override getServiceTelemetry(id: string) {
    const result = this.serviceTelemetryResults.shift() ?? {
      service_id: id,
      window_1h: {
        requests: null,
        error_rate: null,
        ttft_p95_ms: null,
        started_at: null,
      },
      sampled_at: null,
      telemetry_status: "unavailable" as const,
      quality: "partial" as const,
      current: {
        ttft_p50_ms: null,
        ttft_p95_ms: null,
        ttft_p99_ms: null,
        tpot_p50_ms: null,
        tpot_p95_ms: null,
        e2e_latency_p50_ms: null,
        e2e_latency_p95_ms: null,
        e2e_latency_p99_ms: null,
        output_tps_avg: null,
        output_tps_max: null,
        inflight_avg: null,
        inflight_max: null,
        inflight_last: null,
        request_rate_avg: null,
        accepted_requests: null,
        rejected_requests: null,
        client_disconnects: null,
        error_requests: null,
        error_rate: null,
      },
      series: [],
    };
    return result instanceof Error
      ? Promise.reject(result)
      : Promise.resolve(result);
  }

  override getGpuTelemetry() {
    const result = this.gpuTelemetryResults.shift() ?? {
      items: [],
      sampled_at: null,
      quality: "partial" as const,
      telemetry_status: "unavailable" as const,
    };
    return result instanceof Error
      ? Promise.reject(result)
      : Promise.resolve(result);
  }

  override getOverviewAttention() {
    const result = this.overviewAttentionResults.shift() ?? { items: [] };
    return result instanceof Error
      ? Promise.reject(result)
      : Promise.resolve(result);
  }

  override getTelemetryOverview() {
    const result = this.overviewTelemetryResults.shift() ?? {
      current: {
        error_rate: null,
        requests_1h: 0,
        error_rate_1h: null,
        window_started_at: SYSTEM_OBSERVED_AT,
        gpu_allocated: 0,
        gpu_available: 0,
        gpu_total: 0,
        requests_running: 0,
        requests_waiting: 0,
        ttft_p95_ms: null,
        services_available: 0,
        services_starting: 0,
        services_total: 0,
        services_unavailable: 0,
      },
      sampled_at: SYSTEM_OBSERVED_AT,
      telemetry_status: "available" as const,
    };
    return result instanceof Error
      ? Promise.reject(result)
      : Promise.resolve(result);
  }

  override createModelInstance(body: CreateModelInstanceRequest) {
    this.modelInstanceCreateCalls.push(body);
    const result = this.modelInstanceCreateResults.shift();
    if (result === undefined)
      return Promise.reject(
        new Error("model instance create fixture is missing"),
      );
    return result instanceof Error
      ? Promise.reject(result)
      : Promise.resolve(result);
  }

  override startModelInstance(id: string) {
    return this.runModelInstanceCommand(id, "start");
  }

  override stopModelInstance(id: string) {
    return this.runModelInstanceCommand(id, "stop");
  }

  override deleteModelInstance(id: string, deleteBoundServices = false) {
    this.modelInstanceCommandCalls.push({
      command: "delete",
      id,
      ...(deleteBoundServices ? { deleteBoundServices } : {}),
    });
    return Promise.resolve();
  }

  override getAiServices() {
    this.aiServiceCalls += 1;
    const result = this.aiServiceResults.shift() ?? { items: [] };
    return result instanceof Error
      ? Promise.reject(result)
      : Promise.resolve(result);
  }

  override getAiService(id: string) {
    void id;
    const result = this.aiServiceDetailResults.shift();
    if (result === undefined)
      return Promise.reject(new Error("AI service fixture is missing"));
    return result instanceof Error
      ? Promise.reject(result)
      : Promise.resolve(result);
  }

  override createAiService(body: CreateAiServiceRequest) {
    this.aiServiceCreateCalls.push(body);
    const result = this.aiServiceCreateResults.shift();
    if (result === undefined)
      return Promise.reject(new Error("AI service create fixture is missing"));
    return result instanceof Error
      ? Promise.reject(result)
      : Promise.resolve(result);
  }

  aiServiceDeleteCalls: string[] = [];
  aiServiceDeleteResults: (undefined | Error)[] = [];

  override deleteAiService(id: string) {
    this.aiServiceDeleteCalls.push(id);
    const result = this.aiServiceDeleteResults.shift();
    return result instanceof Error ? Promise.reject(result) : Promise.resolve();
  }

  override updateAiService(id: string, body: UpdateAiServiceRequest) {
    this.aiServiceUpdateCalls.push({ body, id });
    const result = this.aiServiceUpdateResults.shift();
    if (result === undefined)
      return Promise.reject(new Error("AI service update fixture is missing"));
    return result instanceof Error
      ? Promise.reject(result)
      : Promise.resolve(result);
  }

  override getApiKeys() {
    const result = this.apiKeyResults.shift() ?? { items: [] };
    return result instanceof Error
      ? Promise.reject(result)
      : Promise.resolve(result);
  }

  override createApiKey(body: CreateApiKeyRequest) {
    this.apiKeyCreateCalls.push(body);
    const result = this.apiKeyCreateResults.shift();
    if (result === undefined)
      return Promise.reject(new Error("API key create fixture is missing"));
    return result instanceof Error
      ? Promise.reject(result)
      : Promise.resolve(result);
  }

  override revokeApiKey(id: string) {
    this.apiKeyRevokeCalls.push(id);
    const result = this.apiKeyRevokeResults.shift();
    if (result === undefined)
      return Promise.reject(new Error("API key revoke fixture is missing"));
    return result instanceof Error
      ? Promise.reject(result)
      : Promise.resolve(result);
  }

  override getRequestRecords(options: RequestRecordFilters = {}) {
    this.requestRecordCalls.push(options);
    const result = this.requestRecordResults.shift() ?? {
      items: [],
      next_cursor: null,
    };
    return result instanceof Error
      ? Promise.reject(result)
      : Promise.resolve(result);
  }

  override getUsageSummary(timeZone = "UTC") {
    this.usageSummaryCalls.push(timeZone);
    const result = this.usageSummaryResults.shift() ?? {
      data_updated_at: null,
      this_month: emptyUsageTotals(),
      today: emptyUsageTotals(),
      today_active_users: 0,
    };
    return result instanceof Error
      ? Promise.reject(result)
      : Promise.resolve(result);
  }

  override getUsage(options: UsageFilters) {
    this.usageCalls.push(options);
    const result = this.usageResults.shift() ?? {
      bucket_width: "day" as const,
      details: [],
      from: options.from,
      summary: emptyUsageTotals(),
      to: options.to,
      trend: [],
      tz: options.timeZone,
    };
    return result instanceof Error
      ? Promise.reject(result)
      : Promise.resolve(result);
  }

  override getUsers(
    options: {
      includeDeleted?: boolean;
      role?: UserRole;
      search?: string;
      status?: UserStatus;
    } = {},
  ) {
    this.userListCalls.push(options);
    const result = this.userResults.shift() ?? { items: [] };
    return result instanceof Error
      ? Promise.reject(result)
      : Promise.resolve(result);
  }

  override getUser(id: string) {
    void id;
    const result = this.userDetailResults.shift();
    if (result === undefined)
      return Promise.reject(new Error("user detail fixture is missing"));
    return result instanceof Error
      ? Promise.reject(result)
      : Promise.resolve(result);
  }

  override createUser(body: CreateUserRequest) {
    this.userCreateCalls.push(body);
    const result = this.userCreateResults.shift();
    if (result === undefined)
      return Promise.reject(new Error("user creation fixture is missing"));
    return result instanceof Error
      ? Promise.reject(result)
      : Promise.resolve(result);
  }

  override commandUser(id: string, command: "disable" | "enable" | "delete") {
    this.userCommandCalls.push({ command, id });
    const result = this.userCommandResults.shift();
    if (result === undefined)
      return Promise.reject(new Error("user command fixture is missing"));
    return result instanceof Error
      ? Promise.reject(result)
      : Promise.resolve(result);
  }

  override resetUserPassword(id: string) {
    void id;
    const result = this.userResetResults.shift();
    if (result === undefined)
      return Promise.reject(new Error("password reset fixture is missing"));
    return result instanceof Error
      ? Promise.reject(result)
      : Promise.resolve(result);
  }

  override replaceUserPermissions(id: string, serviceIds: readonly string[]) {
    this.userPermissionCalls.push({ id, serviceIds });
    const result = this.userPermissionResults.shift() ?? {
      service_ids: [...serviceIds],
    };
    return result instanceof Error
      ? Promise.reject(result)
      : Promise.resolve(result);
  }

  override replaceServicePermissions(id: string, userIds: readonly string[]) {
    this.servicePermissionCalls.push({ id, userIds });
    const result = this.servicePermissionResults.shift() ?? {
      user_ids: [...userIds],
      limits: Object.fromEntries(
        [...userIds].map((id) => [
          id,
          { max_concurrent_requests: null, max_requests_per_minute: null },
        ]),
      ),
    };
    return result instanceof Error
      ? Promise.reject(result)
      : Promise.resolve(result);
  }

  override getServicePermissions(id: string) {
    this.servicePermissionReadCalls.push(id);
    const result = this.servicePermissionReadResults.shift() ?? {
      user_ids: [],
      limits: Object.fromEntries(
        [].map((id) => [
          id,
          { max_concurrent_requests: null, max_requests_per_minute: null },
        ]),
      ),
    };
    return result instanceof Error
      ? Promise.reject(result)
      : Promise.resolve(result);
  }

  override getMyProfile() {
    this.getMyProfileCalls += 1;
    return this.profileResult();
  }

  override updateMyProfile(body: UpdateMyProfileRequest) {
    this.updateMyProfileCalls.push(body);
    return this.profileResult();
  }

  private profileResult(): Promise<PersonalProfile> {
    const result = this.myProfileResults.shift();
    if (result === undefined)
      return Promise.reject(new Error("profile fixture is missing"));
    return result instanceof Error
      ? Promise.reject(result)
      : Promise.resolve(result);
  }

  override getMyAiServices() {
    this.getMyAiServicesCalls += 1;
    const result = this.myAiServiceResults.shift() ?? { items: [] };
    return result instanceof Error
      ? Promise.reject(result)
      : Promise.resolve(result);
  }

  private runModelInstanceCommand(id: string, command: "start" | "stop") {
    this.modelInstanceCommandCalls.push({ command, id });
    const result = this.modelInstanceCommandResults.shift();
    if (result === undefined)
      return Promise.reject(
        new Error("model instance command fixture is missing"),
      );
    return result instanceof Error
      ? Promise.reject(result)
      : Promise.resolve(result);
  }
}

function emptyUsageTotals() {
  return {
    error_rate: null,
    error_requests: 0,
    input_tokens: null,
    output_tokens: null,
    partial_token_requests: 0,
    requests: 0,
    total_tokens: null,
  };
}

export function renderApplication(
  apiClient: FakeApiClient,
  initialEntry = "/",
) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const rendered = render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[initialEntry]}>
        <SessionProvider apiClient={apiClient}>
          <ApplicationRoutes apiClient={apiClient} />
        </SessionProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { ...rendered, queryClient };
}

export function TestProviders({
  apiClient,
  children,
  initialEntry = "/",
}: {
  apiClient: FakeApiClient;
  children: ReactNode;
  initialEntry?: string;
}) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return (
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[initialEntry]}>
        <SessionProvider apiClient={apiClient}>{children}</SessionProvider>
      </MemoryRouter>
    </QueryClientProvider>
  );
}
