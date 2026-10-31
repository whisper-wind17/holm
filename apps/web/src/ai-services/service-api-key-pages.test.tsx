import { UI_46_PERSONAL_SERVICE } from "../app/test-support";
import axe from "axe-core";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { AiService } from "../api/ai-service-contracts";
import type { ApiKey, CreatedApiKey } from "../api/api-key-contracts";
import { ApiError } from "../api/client";
import type { ModelInstance } from "../api/model-instance-contracts";
import {
  ADMIN_ME,
  FakeApiClient,
  renderApplication,
  USER_ME,
} from "../app/test-support";
import {
  AI_SERVICE_REFRESH_INTERVAL_MS,
  aiServiceQueryOptions,
  aiServicesQueryOptions,
} from "./queries";

describe("PROTO_07 AI service and API key UI", () => {
  it("PROTO_07 keeps created plaintext only in the open modal and out of TanStack caches", async () => {
    const apiClient = new FakeApiClient(USER_ME);
    apiClient.myAiServiceResults.push({ items: [UI_46_PERSONAL_SERVICE] });
    const created = createdApiKey();
    apiClient.apiKeyResults.push(
      { items: [] },
      { items: [{ ...apiKey(), effective_services: [] }] },
    );
    apiClient.apiKeyCreateResults.push(created);
    const { queryClient } = renderApplication(apiClient, "/api-keys");
    const user = userEvent.setup();
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText: vi.fn().mockRejectedValue(new Error("denied")) },
    });

    await screen.findByRole("heading", { name: "API 密钥" });
    await user.click(screen.getByRole("button", { name: "创建密钥" }));
    const createDialog = await screen.findByRole("dialog", {
      name: "创建密钥",
    });
    await user.type(within(createDialog).getByLabelText(/^名称/), "开发环境");
    await user.click(
      within(createDialog).getByRole("button", { name: "创建密钥" }),
    );

    const secretDialog = await screen.findByRole("dialog", {
      name: "保存 API 密钥",
    });
    expect(
      within(secretDialog).getByDisplayValue(created.api_key),
    ).toBeVisible();
    expect(within(secretDialog).getByText("OpenAI Python SDK")).toBeVisible();
    expect(cacheContents(queryClient)).not.toContain(created.api_key);
    await user.click(
      within(secretDialog).getByRole("button", { name: "复制 API 密钥" }),
    );
    expect(
      await within(secretDialog).findByRole("button", { name: "全选密钥" }),
    ).toBeVisible();

    await user.click(
      within(secretDialog).getByRole("button", { name: "关闭" }),
    );
    expect(within(secretDialog).getByText("关闭后无法再次查看")).toBeVisible();
    await user.click(
      within(secretDialog).getByRole("button", { name: "仍然关闭" }),
    );
    await waitFor(() =>
      expect(screen.queryByText(created.api_key)).not.toBeInTheDocument(),
    );
    expect(screen.queryByDisplayValue(created.api_key)).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "测试调用" }),
    ).not.toBeInTheDocument();
    expect(cacheContents(queryClient)).not.toContain(created.api_key);
  });

  it("M4-13 shows four successful stages without exposing response text and passes axe", async () => {
    const apiClient = new FakeApiClient(USER_ME);
    apiClient.myAiServiceResults.push({ items: [UI_46_PERSONAL_SERVICE] });
    const created = createdApiKey();
    const responseSentinel = "M4-13-DOM-RESPONSE-SENTINEL";
    apiClient.apiKeyResults.push({ items: [] });
    apiClient.apiKeyCreateResults.push(created);
    const fetchCall = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce(
        jsonResponse({ data: [{ id: "qwen-mini" }] }, 200, "request-models"),
      )
      .mockResolvedValueOnce(
        jsonResponse(
          {
            choices: [
              { message: { content: responseSentinel, role: "assistant" } },
            ],
            usage: { completion_tokens: 1 },
          },
          200,
          "request-completion",
        ),
      );
    const storageWrite = vi.spyOn(Storage.prototype, "setItem");
    const { queryClient } = renderApplication(apiClient, "/api-keys");
    const user = userEvent.setup();

    await screen.findByRole("heading", { name: "API 密钥" });
    await user.click(screen.getByRole("button", { name: "创建密钥" }));
    const createDialog = await screen.findByRole("dialog", {
      name: "创建密钥",
    });
    await user.type(within(createDialog).getByLabelText(/^名称/), "首次调用");
    await user.click(
      within(createDialog).getByRole("button", { name: "创建密钥" }),
    );

    const secretDialog = await screen.findByRole("dialog", {
      name: "保存 API 密钥",
    });
    await user.click(
      within(secretDialog).getByRole("button", { name: "测试调用" }),
    );

    expect(await within(secretDialog).findByText("测试调用成功")).toBeVisible();
    for (const label of ["Endpoint", "鉴权", "服务路由", "模型响应"]) {
      expect(within(secretDialog).getByText(label)).toBeVisible();
    }
    expect(secretDialog).toHaveTextContent("已返回 Token");
    expect(secretDialog).toHaveTextContent("Request ID：request-completion");
    expect(secretDialog).not.toHaveTextContent(responseSentinel);
    expect(cacheContents(queryClient)).not.toContain(responseSentinel);
    expect(storageWrite).not.toHaveBeenCalled();

    const request = fetchCall.mock.calls[1];
    expect(request?.[0]).toBe("/v1/chat/completions");
    expect(parseRequestBody(request?.[1]?.body)).toMatchObject({
      max_tokens: 8,
    });

    const axeResult = await axe.run(secretDialog, {
      rules: { "color-contrast": { enabled: false } },
    });
    expect(
      axeResult.passes.flatMap((rule) => rule.nodes),
      "axe must inspect the M4-13 result surface",
    ).not.toHaveLength(0);
    expect(
      axeResult.violations.filter(
        (violation) =>
          violation.impact === "critical" || violation.impact === "serious",
      ),
    ).toEqual([]);
  });

  it("PROTO_07 renders only API key prefix and last four characters in the list", async () => {
    const apiClient = new FakeApiClient(USER_ME);
    apiClient.myAiServiceResults.push({ items: [UI_46_PERSONAL_SERVICE] });
    apiClient.apiKeyResults.push({
      items: [{ ...apiKey(), effective_services: [] }],
    });
    renderApplication(apiClient, "/api-keys");

    const identifier = await screen.findByText("holm_sk_…1234");
    const table = identifier.closest("table");
    expect(table).not.toBeNull();
    expect(identifier).toBeVisible();
    expect(table as HTMLElement).not.toHaveTextContent(
      "lookup_fixture_secret_1234",
    );
    expect(within(table as HTMLElement).getByText("有效")).toBeVisible();
  });

  it("PROTO_07 discards plaintext from a creation response that arrives after close", async () => {
    const apiClient = new FakeApiClient(USER_ME);
    apiClient.myAiServiceResults.push({ items: [UI_46_PERSONAL_SERVICE] });
    let resolveCreation: ((value: CreatedApiKey) => void) | undefined;
    vi.spyOn(apiClient, "createApiKey").mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveCreation = resolve;
        }),
    );
    const { queryClient } = renderApplication(apiClient, "/api-keys");
    const user = userEvent.setup();

    await screen.findByRole("heading", { name: "API 密钥" });
    await user.click(screen.getByRole("button", { name: "创建密钥" }));
    const dialog = await screen.findByRole("dialog", { name: "创建密钥" });
    await user.type(within(dialog).getByLabelText(/^名称/), "临时调用");
    await user.click(within(dialog).getByRole("button", { name: "创建密钥" }));
    await user.click(within(dialog).getByRole("button", { name: "关闭" }));

    const late = createdApiKey();
    resolveCreation?.(late);
    await waitFor(() =>
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
    );
    expect(screen.queryByDisplayValue(late.api_key)).not.toBeInTheDocument();
    expect(cacheContents(queryClient)).not.toContain(late.api_key);
  });

  it("PROTO_07 shows the dedicated duplicate public model name message", async () => {
    const apiClient = new FakeApiClient(ADMIN_ME);
    apiClient.aiServiceResults.push({ items: [] });
    apiClient.modelInstanceResults.push({ items: [modelInstance()] });
    apiClient.aiServiceCreateResults.push(
      new ApiError({ code: "service_name_conflict", status: 409 }),
    );
    renderApplication(apiClient, "/services");
    const user = userEvent.setup();

    await screen.findByRole("heading", { name: "AI 服务" });
    await user.click(screen.getByRole("button", { name: "创建服务" }));
    const dialog = await screen.findByRole("dialog", { name: "创建服务" });
    await user.type(within(dialog).getByLabelText(/^服务名称/), "研发助手");
    await user.type(within(dialog).getByLabelText(/^API 模型名/), "qwen-mini");
    await user.selectOptions(
      within(dialog).getByLabelText(/^后端实例/),
      INSTANCE_ID,
    );
    await user.click(within(dialog).getByRole("button", { name: "创建服务" }));

    expect(await within(dialog).findByText("API 模型名已被使用")).toBeVisible();
    expect(apiClient.aiServiceCreateCalls).toEqual([
      {
        name: "研发助手",
        description: "",
        enabled: true,
        primary_instance_id: INSTANCE_ID,
        public_model_name: "qwen-mini",
      },
    ]);
  });

  it("PROTO_07 shows bound instance readiness and toggles a service", async () => {
    const apiClient = new FakeApiClient(ADMIN_ME);
    const service = aiService();
    apiClient.aiServiceResults.push({ items: [service] });
    apiClient.modelInstanceResults.push({ items: [modelInstance()] });
    apiClient.aiServiceUpdateResults.push({ ...service, enabled: false });
    renderApplication(apiClient, "/services");
    const user = userEvent.setup();

    const row = (
      await screen.findByRole("link", { name: service.name })
    ).closest("tr");
    expect(row).not.toBeNull();
    expect(
      within(row as HTMLElement).getByRole("img", { name: "可用" }),
    ).toBeVisible();
    await user.click(
      within(row as HTMLElement).getByRole("button", { name: "停用" }),
    );
    await waitFor(() => {
      expect(apiClient.aiServiceUpdateCalls).toEqual([
        { body: { enabled: false }, id: SERVICE_ID },
      ]);
    });
  });

  it.each([
    ["Available", "可用"],
    ["Degraded", "启动中"],
    ["Unavailable", "不可用"],
    ["Disabled", "已停用"],
  ] as const)(
    "M4-11 renders server-derived %s with icon, text, and no serious axe violations",
    async (runtimeStatus, label) => {
      const apiClient = new FakeApiClient(ADMIN_ME);
      const service = aiService({
        enabled: runtimeStatus !== "Disabled",
        runtime_status: runtimeStatus,
      });
      apiClient.aiServiceResults.push({ items: [service] });
      apiClient.modelInstanceResults.push({ items: [modelInstance()] });
      renderApplication(apiClient, "/services");

      const row = (
        await screen.findByRole("link", { name: service.name })
      ).closest("tr");
      expect(row).not.toBeNull();
      const status = within(row as HTMLElement).getByRole("img", {
        name: label,
      });
      expect(status).toHaveClass("pw-service-signal");
      expect(status).toHaveAttribute("title", label);
      const axeResult = await axe.run(row as HTMLElement, {
        rules: { "color-contrast": { enabled: false } },
      });
      expect(
        axeResult.violations.filter(
          (violation) =>
            violation.impact === "critical" || violation.impact === "serious",
        ),
      ).toEqual([]);
    },
  );

  it("M4-11 uses API status instead of joining instance state and keeps the deploy prerequisite", async () => {
    const apiClient = new FakeApiClient(ADMIN_ME);
    apiClient.aiServiceResults.push({
      items: [
        aiService({
          primary_instance_status: "Stopped",
          runtime_status: "Unavailable",
        }),
      ],
    });
    apiClient.modelInstanceResults.push({ items: [] });
    renderApplication(apiClient, "/services");

    expect(await screen.findByRole("img", { name: "不可用" })).toBeVisible();
    expect(
      screen.getByRole("link", { name: "前往处理" }).parentElement,
    ).toHaveTextContent("已停止 · 前往处理");
    expect(screen.queryByText("还没有运行中的模型实例")).toBeNull();
  });

  it("M4-11 refreshes service list and detail queries every five seconds", () => {
    expect(AI_SERVICE_REFRESH_INTERVAL_MS).toBe(5_000);
    const apiClient = new FakeApiClient(ADMIN_ME);
    expect(aiServicesQueryOptions(apiClient).refetchInterval).toBe(5_000);
    expect(aiServiceQueryOptions(apiClient, SERVICE_ID).refetchInterval).toBe(
      5_000,
    );
  });

  it("M4-11 renders the four-state status on the service detail", async () => {
    const apiClient = new FakeApiClient(ADMIN_ME);
    apiClient.aiServiceDetailResults.push(
      aiService({
        primary_instance_status: "Starting",
        runtime_status: "Degraded",
      }),
    );
    apiClient.modelInstanceResults.push({ items: [modelInstance()] });
    renderApplication(apiClient, `/services/${SERVICE_ID}`);

    await screen.findByRole("heading", { name: "研发助手" });
    expect(screen.getAllByText("启动中").length).toBeGreaterThanOrEqual(2);
  });
});

const SERVICE_ID = uuid(1);
const INSTANCE_ID = uuid(2);
const ARTIFACT_ID = uuid(3);
const KEY_ID = uuid(4);
const CREATED_AT = "2026-09-15T03:00:00Z";

function uuid(seed: number) {
  return `0198f58a-2586-7e37-bbf5-${seed.toString().padStart(12, "0")}`;
}

function aiService(overrides: Partial<AiService> = {}): AiService {
  return {
    description: "适合日常对话",
    enabled: true,
    id: SERVICE_ID,
    primary_instance_id: INSTANCE_ID,
    primary_instance_status: "Running",
    name: "研发助手",
    public_model_name: "qwen-mini",
    route_version: 1,
    runtime_status: "Available",
    ...overrides,
  };
}

function modelInstance(): ModelInstance {
  return {
    artifact_id: ARTIFACT_ID,
    config_version: 1,
    updated_at: "2026-10-08T09:00:00Z",
    context_length: 4096,
    error_code: null,
    gpu_memory_utilization: 0.9,
    restart_count: 0,
    gpu_uuids: ["GPU-fixture-0"],
    id: INSTANCE_ID,
    internal_endpoint: null,
    name: "Qwen3-0.6B",
    internal_model_name: "Qwen3-0.6B",
    runtime_type: "vllm",
    status: "Running",
    tensor_parallel_size: 1,
  };
}

function apiKey(): ApiKey {
  return {
    created_at: CREATED_AT,
    display_prefix: "holm_sk_",
    expires_at: null,
    id: KEY_ID,
    last4: "1234",
    last_used_at: null,
    name: "开发环境",
    revoked_at: null,
    service_ids: [],
    status: "Active",
  };
}

function createdApiKey(): CreatedApiKey {
  return { ...apiKey(), api_key: "holm_sk_lookup_fixture_secret_1234" };
}

function cacheContents(queryClient: {
  getMutationCache: () => {
    getAll: () => Array<{ state: { data?: unknown } }>;
  };
  getQueryCache: () => {
    getAll: () => Array<{ state: { data?: unknown } }>;
  };
}) {
  return JSON.stringify({
    mutations: queryClient
      .getMutationCache()
      .getAll()
      .map(({ state }) => state.data),
    queries: queryClient
      .getQueryCache()
      .getAll()
      .map(({ state }) => state.data),
  });
}

function jsonResponse(payload: unknown, status: number, requestId: string) {
  return new Response(JSON.stringify(payload), {
    headers: {
      "Content-Type": "application/json",
      "x-request-id": requestId,
    },
    status,
  });
}

function parseRequestBody(body: BodyInit | null | undefined) {
  if (typeof body !== "string") throw new Error("expected a JSON request body");
  return JSON.parse(body) as unknown;
}
