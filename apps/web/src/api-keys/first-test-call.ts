export const DEFAULT_API_KEY_PROPAGATION_TIMEOUT_MS = 2_000;

export const FIRST_TEST_CALL_STAGE_LABELS = {
  authentication: "鉴权",
  endpoint: "Endpoint",
  modelResponse: "模型响应",
  serviceRouting: "服务路由",
} as const;

export type FirstTestCallStageId =
  "endpoint" | "authentication" | "serviceRouting" | "modelResponse";

export type FirstTestCallStageStatus =
  "completed" | "current" | "failed" | "not-run" | "waiting";

export interface FirstTestCallStage {
  detail: string;
  id: FirstTestCallStageId;
  label: string;
  status: FirstTestCallStageStatus;
}

export interface FirstTestCallFailure {
  code?: GatewayErrorCode;
  conclusion: string;
  requestId: string;
  stage: FirstTestCallStageId;
}

export interface FirstTestCallProgress {
  failure?: FirstTestCallFailure;
  percent: number;
  requestId?: string;
  stages: readonly FirstTestCallStage[];
  status: "running" | "succeeded" | "failed" | "cancelled";
}

export const GATEWAY_ERROR_CODES = [
  "invalid_request",
  "invalid_api_key",
  "api_key_expired",
  "service_not_authorized",
  "model_not_found",
  "rate_limit_exceeded",
  "service_disabled",
  "backend_unavailable",
  "gateway_overloaded",
  "usage_storage_unavailable",
  "control_plane_unavailable",
  "inference_timeout",
] as const;

export type GatewayErrorCode = (typeof GATEWAY_ERROR_CODES)[number];

type Fetcher = (
  input: RequestInfo | URL,
  init?: RequestInit,
) => Promise<Response>;

interface FirstTestCallOptions {
  apiKey: string;
  targetModel: string;
  createRequestId?: () => string;
  fetcher?: Fetcher;
  now?: () => number;
  onProgress?: (progress: FirstTestCallProgress) => void;
  propagationTimeoutMs?: number;
  signal?: AbortSignal;
  sleep?: (milliseconds: number, signal?: AbortSignal) => Promise<void>;
}

interface GatewayFailure {
  code?: GatewayErrorCode;
  requestId: string;
  status: number;
}

const STAGE_ORDER: readonly FirstTestCallStageId[] = [
  "endpoint",
  "authentication",
  "serviceRouting",
  "modelResponse",
];

const STAGE_LABELS: Readonly<Record<FirstTestCallStageId, string>> = {
  authentication: FIRST_TEST_CALL_STAGE_LABELS.authentication,
  endpoint: FIRST_TEST_CALL_STAGE_LABELS.endpoint,
  modelResponse: FIRST_TEST_CALL_STAGE_LABELS.modelResponse,
  serviceRouting: FIRST_TEST_CALL_STAGE_LABELS.serviceRouting,
};

const SUCCESS_DETAILS: Readonly<Record<FirstTestCallStageId, string>> = {
  authentication: "API 密钥有效",
  endpoint: "已连接平台 Endpoint",
  modelResponse: "已返回 Token",
  serviceRouting: "已找到可调用的 AI 服务",
};

const ERROR_PRESENTATIONS: Readonly<
  Record<GatewayErrorCode, Omit<FirstTestCallFailure, "requestId">>
> = {
  api_key_expired: {
    code: "api_key_expired",
    conclusion:
      "API 密钥已过期，本次测试未通过。请关闭弹层后重新创建 API 密钥。",
    stage: "authentication",
  },
  backend_unavailable: {
    code: "backend_unavailable",
    conclusion: "AI 服务暂不可用，无法完成服务路由。请稍后重试。",
    stage: "serviceRouting",
  },
  control_plane_unavailable: {
    code: "control_plane_unavailable",
    conclusion: "平台授权数据暂不可用，无法完成服务路由。请稍后重试。",
    stage: "serviceRouting",
  },
  gateway_overloaded: {
    code: "gateway_overloaded",
    conclusion: "平台并发已满，无法完成服务路由。请稍后重试。",
    stage: "serviceRouting",
  },
  inference_timeout: {
    code: "inference_timeout",
    conclusion: "等待模型响应超时，本次测试未完成。请稍后重试。",
    stage: "modelResponse",
  },
  invalid_api_key: {
    code: "invalid_api_key",
    conclusion: "API 密钥无效，本次测试未通过。请关闭弹层后重新创建 API 密钥。",
    stage: "authentication",
  },
  invalid_request: {
    code: "invalid_request",
    conclusion: "测试请求不受支持，模型响应未完成。请联系管理员检查 AI 服务。",
    stage: "modelResponse",
  },
  model_not_found: {
    code: "model_not_found",
    conclusion:
      "API 模型名不存在，无法完成服务路由。请联系管理员检查 AI 服务。",
    stage: "serviceRouting",
  },
  rate_limit_exceeded: {
    code: "rate_limit_exceeded",
    conclusion: "请求过于频繁，无法完成服务路由。请稍后重试。",
    stage: "serviceRouting",
  },
  service_disabled: {
    code: "service_disabled",
    conclusion: "AI 服务已停用，无法完成服务路由。请联系管理员启用后重试。",
    stage: "serviceRouting",
  },
  service_not_authorized: {
    code: "service_not_authorized",
    conclusion:
      "当前账号或 API 密钥未获此 AI 服务权限。请联系管理员授权后重试。",
    stage: "serviceRouting",
  },
  usage_storage_unavailable: {
    code: "usage_storage_unavailable",
    conclusion: "调用记录暂时无法可靠保存，本次测试未继续。请稍后重试。",
    stage: "serviceRouting",
  },
};

class PropagationDeadlineError extends Error {}

export function gatewayErrorPresentation(
  code: GatewayErrorCode,
  requestId: string,
): FirstTestCallFailure {
  return { ...ERROR_PRESENTATIONS[code], requestId };
}

export async function runFirstTestCall(
  options: FirstTestCallOptions,
): Promise<FirstTestCallProgress> {
  const fetcher = options.fetcher ?? window.fetch.bind(window);
  const now = options.now ?? (() => performance.now());
  const sleep = options.sleep ?? abortableSleep;
  const propagationTimeoutMs =
    options.propagationTimeoutMs ?? DEFAULT_API_KEY_PROPAGATION_TIMEOUT_MS;
  const fallbackRequestId =
    options.createRequestId?.() ?? globalThis.crypto.randomUUID();
  const deadline = now() + Math.max(0, propagationTimeoutMs);
  const bearerHeaders = {
    Accept: "application/json",
    Authorization: `Bearer ${options.apiKey}`,
  };
  let lastPropagationRequestId = fallbackRequestId;

  emit(options, runningAt("endpoint"));

  try {
    let modelsResponse: Response;
    for (;;) {
      try {
        modelsResponse = await fetchBeforeDeadline(
          fetcher,
          "/v1/models",
          { credentials: "same-origin", headers: bearerHeaders },
          deadline,
          now,
          options.signal,
        );
      } catch (error) {
        if (options.signal?.aborted === true) return cancelledProgress();
        if (error instanceof PropagationDeadlineError) {
          return finishWithFailure(options, {
            conclusion:
              "新 API 密钥尚未完成服务路由同步，本次测试未继续。请稍后重试。",
            requestId: lastPropagationRequestId,
            stage: "serviceRouting",
          });
        }
        return finishWithFailure(options, {
          conclusion:
            "未能连接平台 Endpoint，本次测试未发出。请检查网络后重试。",
          requestId: fallbackRequestId,
          stage: "endpoint",
        });
      }

      const modelsPayload = await readJson(modelsResponse);
      lastPropagationRequestId = requestIdFrom(
        modelsResponse,
        modelsPayload,
        fallbackRequestId,
      );
      if (modelsResponse.status !== 401) {
        if (!modelsResponse.ok) {
          return finishWithFailure(
            options,
            failureFromResponse(
              modelsResponse,
              modelsPayload,
              fallbackRequestId,
            ),
          );
        }

        const model = targetPublicModelName(modelsPayload, options.targetModel);
        if (model === undefined) {
          return finishWithFailure(options, {
            conclusion:
              "当前 API 密钥没有可测试的 AI 服务，服务路由未完成。请联系管理员授权后重试。",
            requestId: lastPropagationRequestId,
            stage: "serviceRouting",
          });
        }

        emit(options, runningAt("modelResponse"));
        return await requestCompletion(
          options,
          fetcher,
          bearerHeaders,
          model,
          fallbackRequestId,
        );
      }

      if (now() >= deadline) {
        return finishWithFailure(options, {
          conclusion:
            "新 API 密钥尚未完成服务路由同步，本次测试未继续。请稍后重试。",
          requestId: lastPropagationRequestId,
          stage: "serviceRouting",
        });
      }
      await sleep(Math.min(100, Math.max(0, deadline - now())), options.signal);
      if (options.signal?.aborted === true) return cancelledProgress();
    }
  } catch {
    if (options.signal?.aborted === true) return cancelledProgress();
    return finishWithFailure(options, {
      conclusion: "未能连接平台 Endpoint，本次测试未发出。请检查网络后重试。",
      requestId: fallbackRequestId,
      stage: "endpoint",
    });
  }
}

async function requestCompletion(
  options: FirstTestCallOptions,
  fetcher: Fetcher,
  bearerHeaders: Readonly<Record<string, string>>,
  model: string,
  fallbackRequestId: string,
) {
  let response: Response;
  try {
    response = await fetcher("/v1/chat/completions", {
      body: JSON.stringify({
        max_tokens: 8,
        messages: [{ content: "请简短确认服务可用。", role: "user" }],
        model,
      }),
      credentials: "same-origin",
      headers: { ...bearerHeaders, "Content-Type": "application/json" },
      method: "POST",
      signal: options.signal,
    });
  } catch {
    if (options.signal?.aborted === true) return cancelledProgress();
    return finishWithFailure(options, {
      conclusion: "未能连接平台 Endpoint，本次测试未发出。请检查网络后重试。",
      requestId: fallbackRequestId,
      stage: "endpoint",
    });
  }

  const payload = await readJson(response);
  const requestId = requestIdFrom(response, payload, fallbackRequestId);
  if (!response.ok) {
    return finishWithFailure(
      options,
      failureFromResponse(response, payload, fallbackRequestId),
    );
  }
  if (!hasReturnedToken(payload)) {
    return finishWithFailure(options, {
      conclusion:
        "请求已完成，但未确认返回 Token。请稍后重试；响应正文未显示。",
      requestId,
      stage: "modelResponse",
    });
  }

  const progress: FirstTestCallProgress = {
    percent: 100,
    requestId,
    stages: STAGE_ORDER.map((id) => ({
      detail: SUCCESS_DETAILS[id],
      id,
      label: STAGE_LABELS[id],
      status: "completed",
    })),
    status: "succeeded",
  };
  emit(options, progress);
  return progress;
}

function failureFromResponse(
  response: Response,
  payload: unknown,
  fallbackRequestId: string,
): FirstTestCallFailure {
  const requestId = requestIdFrom(response, payload, fallbackRequestId);
  const gatewayFailure: GatewayFailure = {
    code: gatewayCodeFrom(payload),
    requestId,
    status: response.status,
  };
  if (gatewayFailure.code !== undefined) {
    return gatewayErrorPresentation(gatewayFailure.code, requestId);
  }

  const stage = stageForStatus(gatewayFailure.status);
  return {
    conclusion: "平台未返回可识别的测试结果，本次测试已停止。请稍后重试。",
    requestId,
    stage,
  };
}

function finishWithFailure(
  options: Pick<FirstTestCallOptions, "onProgress">,
  failure: FirstTestCallFailure,
) {
  const failedIndex = STAGE_ORDER.indexOf(failure.stage);
  const progress: FirstTestCallProgress = {
    failure,
    percent: failedIndex * 25,
    requestId: failure.requestId,
    stages: STAGE_ORDER.map((id, index) => ({
      detail:
        index < failedIndex
          ? SUCCESS_DETAILS[id]
          : index === failedIndex
            ? failure.conclusion
            : "前一阶段未通过，本阶段未执行",
      id,
      label: STAGE_LABELS[id],
      status:
        index < failedIndex
          ? "completed"
          : index === failedIndex
            ? "failed"
            : "not-run",
    })),
    status: "failed",
  };
  emit(options, progress);
  return progress;
}

function runningAt(current: FirstTestCallStageId): FirstTestCallProgress {
  const currentIndex = STAGE_ORDER.indexOf(current);
  return {
    percent: currentIndex * 25,
    stages: STAGE_ORDER.map((id, index) => ({
      detail:
        index < currentIndex
          ? SUCCESS_DETAILS[id]
          : index === currentIndex
            ? runningDetail(id)
            : "等待前一阶段完成",
      id,
      label: STAGE_LABELS[id],
      status:
        index < currentIndex
          ? "completed"
          : index === currentIndex
            ? "current"
            : "waiting",
    })),
    status: "running",
  };
}

function runningDetail(id: FirstTestCallStageId) {
  if (id === "endpoint") return "正在连接平台 Endpoint";
  if (id === "modelResponse") return "正在等待模型响应";
  return "正在检查";
}

function cancelledProgress(): FirstTestCallProgress {
  return {
    percent: 0,
    stages: STAGE_ORDER.map((id) => ({
      detail: "弹层已关闭",
      id,
      label: STAGE_LABELS[id],
      status: "not-run",
    })),
    status: "cancelled",
  };
}

function emit(
  options: Pick<FirstTestCallOptions, "onProgress">,
  progress: FirstTestCallProgress,
) {
  options.onProgress?.(progress);
}

function targetPublicModelName(payload: unknown, targetModel: string) {
  if (!isRecord(payload) || !Array.isArray(payload.data)) return undefined;
  for (const item of payload.data) {
    if (
      isRecord(item) &&
      typeof item.id === "string" &&
      item.id === targetModel &&
      item.id.length > 0
    ) {
      return item.id;
    }
  }
  return undefined;
}

function hasReturnedToken(payload: unknown) {
  if (!isRecord(payload)) return false;
  const usage = payload.usage;
  if (
    isRecord(usage) &&
    typeof usage.completion_tokens === "number" &&
    usage.completion_tokens > 0
  ) {
    return true;
  }
  return Array.isArray(payload.choices) && payload.choices.length > 0;
}

function gatewayCodeFrom(payload: unknown): GatewayErrorCode | undefined {
  if (!isRecord(payload) || !isRecord(payload.error)) return undefined;
  const code = payload.error.code;
  return typeof code === "string" && isGatewayErrorCode(code)
    ? code
    : undefined;
}

function requestIdFrom(response: Response, payload: unknown, fallback: string) {
  const header = response.headers.get("x-request-id");
  if (header !== null && header.length > 0) return header;
  if (
    isRecord(payload) &&
    isRecord(payload.error) &&
    typeof payload.error.request_id === "string" &&
    payload.error.request_id.length > 0
  ) {
    return payload.error.request_id;
  }
  return fallback;
}

function stageForStatus(status: number): FirstTestCallStageId {
  if (status === 401) return "authentication";
  if (status === 403 || status === 404 || status === 429 || status === 503) {
    return "serviceRouting";
  }
  if (status === 400 || status === 504) return "modelResponse";
  return "endpoint";
}

function isGatewayErrorCode(value: string): value is GatewayErrorCode {
  return (GATEWAY_ERROR_CODES as readonly string[]).includes(value);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

async function readJson(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    return undefined;
  }
}

async function fetchBeforeDeadline(
  fetcher: Fetcher,
  input: RequestInfo | URL,
  init: RequestInit,
  deadline: number,
  now: () => number,
  externalSignal?: AbortSignal,
) {
  const controller = new AbortController();
  const remaining = Math.max(1, deadline - now());
  let timeout = 0;
  const deadlinePromise = new Promise<Response>((_resolve, reject) => {
    timeout = window.setTimeout(() => {
      controller.abort();
      reject(new PropagationDeadlineError());
    }, remaining);
  });
  const abortFromExternal = () => {
    controller.abort();
  };
  externalSignal?.addEventListener("abort", abortFromExternal, { once: true });
  try {
    return await Promise.race([
      fetcher(input, { ...init, signal: controller.signal }),
      deadlinePromise,
    ]);
  } finally {
    window.clearTimeout(timeout);
    externalSignal?.removeEventListener("abort", abortFromExternal);
  }
}

function abortableSleep(milliseconds: number, signal?: AbortSignal) {
  return new Promise<void>((resolve) => {
    if (signal?.aborted === true) {
      resolve();
      return;
    }
    const timeout = window.setTimeout(resolve, milliseconds);
    signal?.addEventListener(
      "abort",
      () => {
        window.clearTimeout(timeout);
        resolve();
      },
      { once: true },
    );
  });
}
