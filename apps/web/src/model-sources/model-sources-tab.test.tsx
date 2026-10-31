import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import axe from "axe-core";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiClient, ApiError } from "../api/client";
import type {
  ModelSourceCredentialRequest,
  ModelSourceResponse,
} from "../api/model-source-contracts";
import { ApplicationRoutes } from "../app/application";
import { AppErrorBoundary } from "../app/error-boundary";
import {
  ADMIN_ME,
  DEFAULT_MODEL_SOURCES,
  FakeApiClient,
  renderApplication,
} from "../app/test-support";
import { SessionProvider } from "../auth/session";
import { SystemPage } from "../system/system-page";

const APP_CSS_PATH = resolve(
  dirname(fileURLToPath(import.meta.url)),
  "../styles/app.css",
);
const TOKEN_SENTINEL = "SRCTOK_M2_13A";
const PASSWORD_SENTINEL = "PWD_M2_13A";

afterEach(() => {
  localStorage.clear();
  sessionStorage.clear();
  document.cookie = "__Host-pws_csrf=; Max-Age=0; Path=/";
  vi.restoreAllMocks();
});

describe("M2_13A system deep link", () => {
  it("opens model sources directly and does not fetch them before entry", async () => {
    const direct = new FakeApiClient(ADMIN_ME);
    renderApplication(direct, "/system?tab=model-sources");
    expect(
      await screen.findByRole("tab", { name: "模型源", selected: true }),
    ).toBeVisible();
    await waitFor(() => {
      expect(direct.modelSourceCalls).toBe(1);
    });

    const defaultClient = new FakeApiClient(ADMIN_ME);
    renderApplication(defaultClient, "/system");
    await screen.findByRole("tab", { name: "服务器", selected: true });
    expect(defaultClient.modelSourceCalls).toBe(0);
  });

  it("removes an unknown tab value and returns to the default tab", async () => {
    const { apiClient } = renderBareSystem("/system?tab=future-tab");
    expect(
      await screen.findByRole("tab", { name: "服务器", selected: true }),
    ).toBeVisible();
    await waitFor(() => {
      expect(screen.getByTestId("location").textContent).toBe("/system");
    });
    expect(apiClient.modelSourceCalls).toBe(0);
  });

  it("positive control rejects a hard-coded server selection", () => {
    const { container } = render(
      <div role="tablist">
        <button aria-selected="true" role="tab">
          服务器
        </button>
        <button aria-selected="false" role="tab">
          模型源
        </button>
      </div>,
    );
    expect(() => {
      validateDeepLinkSelection(container);
    }).toThrow(/model-source deep link was not selected/);
  });
});

describe("M2_13A TC-SRC-01 response-to-DOM configuration states", () => {
  it.each([
    ["unconfigured", "未配置"],
    ["configured", "已配置"],
    ["key_unavailable", "凭证不可用"],
  ] as const)("keeps %s distinct after zod parsing", async (state, label) => {
    renderParsedSources(sourcesWith({ configuration_state: state }));
    const card = await sourceCard("Hugging Face");
    validateConfigurationState(card, state);
    expect(within(card).getByText(label)).toBeVisible();
  });

  it("orders the key-unavailable impact and recovery copy", async () => {
    renderParsedSources(
      sourcesWith({ configuration_state: "key_unavailable" }),
    );
    const card = await sourceCard("Hugging Face");
    expect(card).toHaveTextContent("平台无法解开已保存的凭证");
    expect(card).toHaveTextContent("私有仓库的读取与下载会失败");
    expect(card).toHaveTextContent("联系负责平台部署的管理员检查加密密钥");
    expect(card).not.toHaveTextContent("未配置");
    expect(card).not.toHaveTextContent("已配置");
  });

  it.each(["未配置", "已配置"])(
    "positive control rejects key_unavailable rendered as %s",
    async (wrongLabel) => {
      renderParsedSources(
        sourcesWith({ configuration_state: "key_unavailable" }),
      );
      const card = await sourceCard("Hugging Face");
      within(card).getByText("凭证不可用").textContent = wrongLabel;
      expect(() => {
        validateConfigurationState(card, "key_unavailable");
      }).toThrow(/configuration state collapsed/);
    },
  );

  it("keeps loading separate from unconfigured", async () => {
    const apiClient = new FakeApiClient(ADMIN_ME);
    vi.spyOn(apiClient, "getModelSources").mockReturnValue(
      new Promise(() => undefined),
    );
    renderApplication(apiClient, "/system?tab=model-sources");
    const loading = await screen.findByLabelText("模型源正在加载");
    expect(loading).toHaveAttribute("aria-busy", "true");
    expect(loading).not.toHaveTextContent("未配置");
  });

  it("keeps a read failure separate from every configuration state and retries", async () => {
    const apiClient = new FakeApiClient(ADMIN_ME);
    apiClient.modelSourceResults.push(
      new Error("unavailable"),
      structuredClone(DEFAULT_MODEL_SOURCES),
    );
    renderApplication(apiClient, "/system?tab=model-sources");
    const alert = await screen.findByRole("alert");
    validateReadFailure(alert);
    await userEvent.click(
      within(alert).getByRole("button", { name: "重新加载" }),
    );
    expect(await screen.findByText("可访问")).toBeVisible();
  });

  it("positive control rejects a read failure rendered as unconfigured", async () => {
    const apiClient = new FakeApiClient(ADMIN_ME);
    apiClient.modelSourceResults.push(new Error("unavailable"));
    renderApplication(apiClient, "/system?tab=model-sources");
    const alert = await screen.findByRole("alert");
    const mutation = document.createElement("span");
    mutation.textContent = "未配置";
    alert.append(mutation);
    expect(() => {
      validateReadFailure(alert);
    }).toThrow(/read failure collapsed/);
  });

  it("renders 403 as Permission without source cards", async () => {
    const apiClient = new FakeApiClient(ADMIN_ME);
    apiClient.modelSourceResults.push(
      new ApiError({ code: "service_not_authorized", status: 403 }),
    );
    renderApplication(apiClient, "/system?tab=model-sources");
    expect(await screen.findByText("无权限查看模型源")).toBeVisible();
    expect(screen.queryByRole("article")).not.toBeInTheDocument();
  });
});

describe("M2_13A TC-SRC-02 tail disclosure", () => {
  it("shows exactly the four server characters with no invented prefix", async () => {
    renderParsedSources(sourcesWith({ credential_last4: "8f2a" }));
    const card = await sourceCard("Hugging Face");
    expect(card).toHaveTextContent("尾部 8f2a");
    expect(card).not.toHaveTextContent(/hf_|··|••/);
  });

  it("explains a null tail instead of rendering blank or an em dash", async () => {
    renderParsedSources(sourcesWith({ credential_last4: null }));
    const card = await sourceCard("Hugging Face");
    validateNullTail(card);
  });

  it("positive control rejects a null tail rendered as an em dash", () => {
    const { container } = render(
      <dl>
        <div>
          <dt>凭证</dt>
          <dd>—</dd>
        </div>
      </dl>,
    );
    expect(() => {
      validateNullTail(container);
    }).toThrow(/null tail was hidden/);
  });

  it("positive control rejects a three-character response before DOM", async () => {
    mockModelSourceFetch(
      jsonResponse(sourcesWith({ credential_last4: "abc" })),
    );
    renderAppWithQueryClient(
      new ParsingModelSourceClient(),
      "/system?tab=model-sources",
    );
    expect(
      await screen.findByText("model_source_contract_error"),
    ).toBeVisible();
    expect(screen.queryByRole("article")).not.toBeInTheDocument();
  });
});

describe("M2_13A TC-SRC-06 validation conclusions", () => {
  it.each([
    ["accessible", "可访问", "i-ok"],
    ["invalid_credential", "凭证错误或已过期", "i-fail"],
    ["permission_denied", "没有访问权限", "i-lock"],
    ["not_found", "来源中未找到", "i-search"],
    ["rate_limited", "来源暂时限流", "i-clock"],
  ] as const)(
    "renders %s with its own label and icon",
    async (status, label, icon) => {
      renderParsedSources(sourcesWith({ verification_status: status }));
      const card = await sourceCard("Hugging Face");
      const badge = within(card).getByText(label).closest(".pw-status");
      expect(badge).not.toBeNull();
      expect((badge as HTMLElement).querySelector(".pwi")).toHaveClass(icon);
    },
  );

  it("keeps null status, null time, reason and action independent", async () => {
    renderParsedSources(
      sourcesWith({
        next_action: "install_validator",
        verification_reason_code: "validator_unavailable",
        verification_status: null,
        verified_at: null,
      }),
    );
    const card = await sourceCard("Hugging Face");
    expect(card).toHaveTextContent("未验证");
    expect(card).toHaveTextContent("验证服务尚未安装");
    expect(card).toHaveTextContent("联系平台部署管理员");
    expect(within(card).queryByText("验证时间")).not.toBeInTheDocument();
    expect(card).not.toHaveTextContent("刚刚");
  });

  it("unknown status fails at the parsed response boundary", async () => {
    mockModelSourceFetch(
      jsonResponse(sourcesWith({ verification_status: "future_status" })),
    );
    renderAppWithQueryClient(
      new ParsingModelSourceClient(),
      "/system?tab=model-sources",
    );
    expect(
      await screen.findByText("model_source_contract_error"),
    ).toBeVisible();
  });
});

describe("M2_13A TC-UI-03 credential writes", () => {
  it("keeps invalid_credentials in the modal, clears only the password, and keeps the session", async () => {
    vi.spyOn(document, "cookie", "get").mockReturnValue(
      "__Host-pws_csrf=model-source-csrf",
    );
    mockModelSourceFetch(
      jsonResponse(rawSources()),
      errorResponse(401, "invalid_credentials", "用户名或密码不正确"),
    );
    renderApplication(
      new ParsingModelSourceClient(),
      "/system?tab=model-sources",
    );
    await openConfigureAndFill(TOKEN_SENTINEL, PASSWORD_SENTINEL);
    await userEvent.click(screen.getByRole("button", { name: "验证并保存" }));
    expect(await screen.findByText("管理员密码不正确")).toBeVisible();
    expect(screen.getByRole("dialog")).toBeVisible();
    expect(screen.getByLabelText(/^你的管理员密码/)).toHaveValue("");
    expect(screen.getByLabelText(/^新的 Access Token/)).toHaveValue(
      TOKEN_SENTINEL,
    );
    expect(
      screen.getByRole("heading", { hidden: true, name: "系统" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { name: "登录" }),
    ).not.toBeInTheDocument();
  });

  it("sends authentication_required back through the real login route", async () => {
    vi.spyOn(document, "cookie", "get").mockReturnValue(
      "__Host-pws_csrf=model-source-csrf",
    );
    mockModelSourceFetch(
      jsonResponse(rawSources()),
      errorResponse(401, "authentication_required", "登录会话无效"),
    );
    renderApplication(
      new ParsingModelSourceClient(),
      "/system?tab=model-sources",
    );
    await openConfigureAndFill("short-token", "password");
    await userEvent.click(screen.getByRole("button", { name: "验证并保存" }));
    expect(await screen.findByRole("heading", { name: "登录" })).toBeVisible();
  });

  it.each([
    [429, "login_locked", "登录锁定", 173, "请在 173 秒后重试"],
    [403, "csrf_validation_failed", "安全校验失败", undefined, "安全校验失败"],
    [
      409,
      "credential_update_conflict",
      "凭证已被其他操作更新",
      undefined,
      "凭证已被其他操作更新",
    ],
    [400, "invalid_request", "请求字段无效", undefined, "请求字段无效"],
  ] as const)(
    "renders HTTP %s %s inline with server detail and Request ID",
    async (status, code, message, retryAfter, visibleText) => {
      const apiClient = new FakeApiClient(ADMIN_ME);
      apiClient.modelSourceUpdateResults.push(
        new ApiError({
          code,
          requestId: "request-m2-13a",
          responseMessage: message,
          retryAfterSeconds: retryAfter,
          status,
        }),
      );
      renderApplication(apiClient, "/system?tab=model-sources");
      await openConfigureAndFill("short-token", "password");
      await userEvent.click(screen.getByRole("button", { name: "验证并保存" }));
      expect(await screen.findByText(new RegExp(visibleText))).toBeVisible();
      expect(screen.getByText("Request ID：request-m2-13a")).toBeVisible();
      if (status === 409) {
        await waitFor(() => {
          expect(apiClient.modelSourceCalls).toBeGreaterThan(1);
        });
      }
    },
  );

  it("closes on success, updates from the response, and only announces the conclusion", async () => {
    const apiClient = new FakeApiClient(ADMIN_ME);
    apiClient.modelSourceUpdateResults.push(
      configuredSource("ModelScope", "rate_limited"),
    );
    renderApplication(apiClient, "/system?tab=model-sources");
    await openConfigureAndFill("short-token", "password");
    await userEvent.click(screen.getByRole("button", { name: "验证并保存" }));
    expect(await screen.findByText("来源暂时限流")).toBeVisible();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(
      screen.getByText(/ModelScope配置已更新，来源暂时限流/),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText("通知")).not.toBeInTheDocument();
    expect(document.body).not.toHaveTextContent(/保存成功|验证成功/);
  });

  it("opens all three modal actions with focus trapping and a Danger removal", async () => {
    const user = userEvent.setup();
    renderApplication(new FakeApiClient(ADMIN_ME), "/system?tab=model-sources");
    const replace = await screen.findByRole("button", { name: "更换凭证" });
    await user.click(replace);
    expect(
      screen.getByRole("heading", { name: "更换Hugging Face凭证" }),
    ).toHaveFocus();
    await user.keyboard("{Escape}");
    expect(replace).toHaveFocus();

    const remove = screen.getByRole("button", { name: "移除凭证" });
    await user.click(remove);
    expect(
      within(screen.getByRole("dialog")).getByRole("button", {
        name: "移除凭证",
      }),
    ).toHaveClass("pw-button--danger");
    await user.keyboard("{Escape}");

    const configure = screen.getByRole("button", { name: "配置凭证" });
    await user.click(configure);
    expect(
      screen.getByRole("heading", { name: "配置ModelScope凭证" }),
    ).toHaveFocus();
    expect(screen.getByLabelText(/^你的管理员密码/)).toHaveAttribute(
      "type",
      "password",
    );
    expect(screen.getByLabelText(/^新的 Access Token/)).toHaveAttribute(
      "type",
      "password",
    );
  });

  it("validates on blur and submit without disabling the Primary action", async () => {
    renderApplication(new FakeApiClient(ADMIN_ME), "/system?tab=model-sources");
    await userEvent.click(
      await screen.findByRole("button", { name: "配置凭证" }),
    );
    expect(screen.queryByText("请输入管理员密码")).not.toBeInTheDocument();
    await userEvent.click(screen.getByLabelText(/^你的管理员密码/));
    await userEvent.click(screen.getByLabelText(/^新的 Access Token/));
    expect(await screen.findByText("请输入管理员密码")).toBeVisible();
    expect(screen.getByRole("button", { name: "验证并保存" })).toBeEnabled();
  });
});

describe("M2_13A secret zero-residue", () => {
  it("removes token and password after success and after a 401 is cancelled", async () => {
    localStorage.setItem("unrelated", "safe");
    sessionStorage.setItem("unrelated", "safe");
    vi.spyOn(document, "cookie", "get").mockReturnValue(
      "__Host-pws_csrf=safe-csrf",
    );
    const consoleCalls: unknown[][] = [];
    for (const method of ["error", "log", "warn"] as const) {
      vi.spyOn(console, method).mockImplementation((...values: unknown[]) => {
        consoleCalls.push(values);
      });
    }
    mockModelSourceFetch(
      jsonResponse(rawSources()),
      jsonResponse(configuredSource("ModelScope", "accessible")),
      errorResponse(401, "invalid_credentials", "用户名或密码不正确"),
    );
    const apiClient = new ParsingModelSourceClient();
    const { queryClient } = renderAppWithQueryClient(
      apiClient,
      "/system?tab=model-sources",
    );

    await openConfigureAndFill(TOKEN_SENTINEL, PASSWORD_SENTINEL);
    await userEvent.click(screen.getByRole("button", { name: "验证并保存" }));
    await waitFor(() =>
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
    );

    const modelScope = await sourceCard("ModelScope");
    await userEvent.click(
      within(modelScope).getByRole("button", { name: "更换凭证" }),
    );
    await fillOpenForm(TOKEN_SENTINEL, PASSWORD_SENTINEL);
    await userEvent.click(screen.getByRole("button", { name: "验证并保存" }));
    expect(await screen.findByText("管理员密码不正确")).toBeVisible();
    await userEvent.click(screen.getByRole("button", { name: "取消" }));

    expect(
      await collectSecretResidue(
        queryClient,
        [TOKEN_SENTINEL, PASSWORD_SENTINEL],
        consoleCalls,
        apiClient.observedErrors,
      ),
    ).toEqual([]);
  });

  it("positive control detects a sentinel retained as mutation variables", async () => {
    const queryClient = new QueryClient();
    const mutation = queryClient.getMutationCache().build(queryClient, {
      mutationFn: (value: string) => Promise.resolve(value),
      mutationKey: ["positive-control"],
    });
    await mutation.execute(TOKEN_SENTINEL);
    expect(
      await collectSecretResidue(queryClient, [TOKEN_SENTINEL], [], []),
    ).toContain("mutation-cache");
  });
});

describe("M2_13A accessibility and narrow layout", () => {
  it.each(["normal", "error", "modal"] as const)(
    "has non-empty axe passes and no violations: %s",
    async (state) => {
      const apiClient = new FakeApiClient(ADMIN_ME);
      if (state === "error")
        apiClient.modelSourceResults.push(new Error("offline"));
      renderApplication(apiClient, "/system?tab=model-sources");
      if (state === "error") await screen.findByText("无法获取模型源");
      else await screen.findByText("可访问");
      if (state === "modal") {
        await userEvent.click(screen.getByRole("button", { name: "配置凭证" }));
      }
      const result = await axe.run(document.body, {
        rules: { "color-contrast": { enabled: false } },
      });
      expect(result.passes.flatMap((rule) => rule.nodes)).not.toHaveLength(0);
      expect(result.violations).toEqual([]);
    },
  );

  it("keeps cards and actions usable in the existing narrow breakpoint", () => {
    const css = readFileSync(APP_CSS_PATH, "utf8");
    const mobile = mediaBlock(css, "(max-width: 767px)");
    expect(mobile).toContain(".pw-model-source-card__head");
    expect(mobile).toContain("flex-direction: column");
    expect(mobile).toContain(".pw-model-source-card__actions");
    expect(mobile).toContain("width: 100%");
  });
});

class ParsingModelSourceClient extends FakeApiClient {
  readonly observedErrors: ApiError[] = [];

  constructor() {
    super(ADMIN_ME);
  }

  override getModelSources() {
    return ApiClient.prototype.getModelSources.call(this);
  }

  override async updateModelSource(body: ModelSourceCredentialRequest) {
    try {
      return await ApiClient.prototype.updateModelSource.call(this, body);
    } catch (error) {
      if (error instanceof ApiError) this.observedErrors.push(error);
      throw error;
    }
  }
}

function rawSources(): unknown[] {
  return [
    {
      configuration_state: "configured",
      credential_last4: "8f2a",
      next_action: null,
      source_type: "Hugging Face",
      verification_reason_code: "credential_valid",
      verification_status: "accessible",
      verified_at: "2026-09-11T05:00:00Z",
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
}

function sourcesWith(overrides: Record<string, unknown>) {
  const sources = structuredClone(rawSources()) as Array<
    Record<string, unknown>
  >;
  sources[0] = { ...sources[0], ...overrides };
  return sources;
}

function configuredSource(
  sourceType: "Hugging Face" | "ModelScope",
  status: ModelSourceResponse["verification_status"],
): ModelSourceResponse {
  return {
    configuration_state: "configured",
    credential_last4: "13AA",
    next_action: status === "rate_limited" ? "retry_later" : null,
    source_type: sourceType,
    verification_reason_code:
      status === "rate_limited" ? "source_rate_limited" : "credential_valid",
    verification_status: status,
    verified_at: "2026-09-11T05:30:00Z",
  };
}

function renderParsedSources(raw: unknown[]) {
  mockModelSourceFetch(jsonResponse(raw));
  return renderApplication(
    new ParsingModelSourceClient(),
    "/system?tab=model-sources",
  );
}

function mockModelSourceFetch(...results: Array<Response | Error>) {
  vi.spyOn(globalThis, "fetch").mockImplementation(() => {
    const result = results.shift();
    if (result === undefined)
      throw new Error("unexpected model-source request");
    if (result instanceof Error) throw result;
    return Promise.resolve(result);
  });
}

function jsonResponse(body: unknown) {
  return new Response(JSON.stringify(body), {
    headers: { "Content-Type": "application/json" },
    status: 200,
  });
}

function errorResponse(status: number, code: string, message: string) {
  return new Response(
    JSON.stringify({
      error: {
        code,
        details: {},
        message,
        request_id: "request-m2-13a",
      },
    }),
    { headers: { "Content-Type": "application/json" }, status },
  );
}

async function sourceCard(name: string) {
  return await screen.findByRole("article", { name: `${name} 模型源` });
}

function validateConfigurationState(
  container: HTMLElement,
  state: "unconfigured" | "configured" | "key_unavailable",
) {
  const expected = {
    configured: "已配置",
    key_unavailable: "凭证不可用",
    unconfigured: "未配置",
  }[state];
  const labels = ["未配置", "已配置", "凭证不可用"].filter((label) =>
    container.textContent.includes(label),
  );
  if (labels.length !== 1 || labels[0] !== expected) {
    throw new Error("configuration state collapsed");
  }
}

function validateReadFailure(container: HTMLElement) {
  if (container.textContent.includes("未配置")) {
    throw new Error("read failure collapsed into unconfigured");
  }
  expect(container).toHaveTextContent("无法获取模型源");
}

function validateNullTail(container: HTMLElement) {
  if (!container.textContent.includes("凭证较短，不显示尾部标识")) {
    throw new Error("null tail was hidden");
  }
  expect(container).not.toHaveTextContent(/凭证\s*—/);
}

function validateDeepLinkSelection(container: HTMLElement) {
  const modelSource = within(container).getByRole("tab", { name: "模型源" });
  if (modelSource.getAttribute("aria-selected") !== "true") {
    throw new Error("model-source deep link was not selected");
  }
}

async function openConfigureAndFill(token: string, password: string) {
  await userEvent.click(
    await screen.findByRole("button", { name: "配置凭证" }),
  );
  await fillOpenForm(token, password);
}

async function fillOpenForm(token: string, password: string) {
  const user = userEvent.setup();
  await user.type(screen.getByLabelText(/^你的管理员密码/), password);
  await user.type(screen.getByLabelText(/^新的 Access Token/), token);
}

function LocationProbe() {
  const location = useLocation();
  return (
    <output data-testid="location">{`${location.pathname}${location.search}`}</output>
  );
}

function renderBareSystem(initialEntry: string) {
  const apiClient = new FakeApiClient(ADMIN_ME);
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[initialEntry]}>
        <SystemPage apiClient={apiClient} />
        <LocationProbe />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { apiClient, queryClient };
}

function renderAppWithQueryClient(
  apiClient: FakeApiClient,
  initialEntry: string,
) {
  const queryClient = new QueryClient({
    defaultOptions: {
      mutations: { retry: false },
      queries: { retry: false },
    },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <AppErrorBoundary>
        <MemoryRouter initialEntries={[initialEntry]}>
          <SessionProvider apiClient={apiClient}>
            <ApplicationRoutes apiClient={apiClient} />
          </SessionProvider>
        </MemoryRouter>
      </AppErrorBoundary>
    </QueryClientProvider>,
  );
  return { queryClient };
}

async function collectSecretResidue(
  queryClient: QueryClient,
  secrets: readonly string[],
  consoleCalls: readonly unknown[][],
  observedErrors: readonly ApiError[],
) {
  const findings: string[] = [];
  for (const [name, storage] of [
    ["local-storage", localStorage],
    ["session-storage", sessionStorage],
  ] as const) {
    for (let index = 0; index < storage.length; index += 1) {
      const key = storage.key(index) ?? "";
      if (containsSecret(`${key}${storage.getItem(key) ?? ""}`, secrets)) {
        findings.push(name);
      }
    }
  }
  if (
    containsSecret(
      `${location.href}${location.search}${location.hash}`,
      secrets,
    )
  ) {
    findings.push("location");
  }
  if (containsSecret(document.documentElement.outerHTML, secrets)) {
    findings.push("dom");
  }
  for (const input of document.querySelectorAll<HTMLInputElement>("input")) {
    if (containsSecret(input.value, secrets)) findings.push("input-value");
  }
  if (
    containsSecret(
      JSON.stringify(
        queryClient
          .getQueryCache()
          .getAll()
          .map(({ state }) => state),
      ),
      secrets,
    )
  ) {
    findings.push("query-cache");
  }
  if (
    containsSecret(
      JSON.stringify(
        queryClient
          .getMutationCache()
          .getAll()
          .map(({ state }) => state),
      ),
      secrets,
    )
  ) {
    findings.push("mutation-cache");
  }
  if (containsSecret(JSON.stringify(consoleCalls), secrets)) {
    findings.push("console");
  }
  if (containsSecret(JSON.stringify(observedErrors), secrets)) {
    findings.push("api-error");
  }
  const indexedDb = globalThis.indexedDB as IDBFactory | undefined;
  if (typeof indexedDb?.databases === "function") {
    const databases = await indexedDb.databases();
    if (containsSecret(JSON.stringify(databases), secrets))
      findings.push("indexed-db");
  }
  return findings;
}

function containsSecret(value: string, secrets: readonly string[]) {
  return secrets.some((secret) => value.includes(secret));
}

function mediaBlock(css: string, condition: string) {
  const start = css.indexOf(`@media ${condition}`);
  if (start < 0) throw new Error(`missing media block ${condition}`);
  let depth = 0;
  for (let index = css.indexOf("{", start); index < css.length; index += 1) {
    if (css[index] === "{") depth += 1;
    if (css[index] === "}") depth -= 1;
    if (depth === 0) return css.slice(start, index + 1);
  }
  throw new Error(`unterminated media block ${condition}`);
}

describe("M2_01C credential save confirmation", () => {
  it("keeps rejected input in the modal and leaves the cached source unchanged", async () => {
    vi.spyOn(document, "cookie", "get").mockReturnValue(
      "__Host-pws_csrf=safe-csrf",
    );
    const apiClient = new ParsingModelSourceClient();
    const message = "新凭证未生效，下载仍使用原凭证，请检查后重新输入";
    mockModelSourceFetch(
      jsonResponse(rawSources()),
      errorResponse(422, "model_source_credential_rejected", message),
    );
    const { queryClient } = renderAppWithQueryClient(
      apiClient,
      "/system?tab=model-sources",
    );
    await openConfigureAndFill(TOKEN_SENTINEL, PASSWORD_SENTINEL);
    await userEvent.click(screen.getByRole("button", { name: "验证并保存" }));
    expect(await screen.findByText(message)).toBeVisible();
    expect(screen.getByLabelText(/^新的 Access Token/)).toHaveValue(
      TOKEN_SENTINEL,
    );
    expect(screen.getByLabelText(/^你的管理员密码/)).toHaveValue(
      PASSWORD_SENTINEL,
    );
    expect(
      screen.queryByRole("button", { name: "仍然保存（未验证）" }),
    ).not.toBeInTheDocument();
    expect(queryClient.getQueryData(["system", "model-sources"])).toEqual(
      rawSources(),
    );
    await userEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(
      await collectSecretResidue(
        queryClient,
        [TOKEN_SENTINEL, PASSWORD_SENTINEL],
        [],
        apiClient.observedErrors,
      ),
    ).toEqual([]);
  });

  it("requires a fresh password in the same dialog and saves an explicitly unverified source", async () => {
    vi.spyOn(document, "cookie", "get").mockReturnValue(
      "__Host-pws_csrf=safe-csrf",
    );
    const apiClient = new ParsingModelSourceClient();
    mockModelSourceFetch(
      jsonResponse(rawSources()),
      errorResponse(
        409,
        "model_source_validation_unavailable",
        "暂时无法验证新凭证，尚未保存",
      ),
      errorResponse(401, "invalid_credentials", "用户名或密码不正确"),
      jsonResponse({
        ...configuredSource("ModelScope", null),
        verification_reason_code: "source_rate_limited",
        next_action: "retry_later",
      }),
    );
    const { queryClient } = renderAppWithQueryClient(
      apiClient,
      "/system?tab=model-sources",
    );
    await openConfigureAndFill(TOKEN_SENTINEL, PASSWORD_SENTINEL);
    const dialog = screen.getByRole("dialog");
    await userEvent.click(screen.getByRole("button", { name: "验证并保存" }));
    const confirm = await screen.findByRole("button", {
      name: "仍然保存（未验证）",
    });
    expect(screen.getByRole("dialog")).toBe(dialog);
    expect(screen.getByLabelText(/^你的管理员密码/)).toHaveValue("");
    expect(screen.getByLabelText(/^你的管理员密码/)).toHaveFocus();
    expect(screen.getByLabelText(/^新的 Access Token/)).toHaveValue(
      TOKEN_SENTINEL,
    );
    await userEvent.click(confirm);
    expect(screen.getByLabelText(/^你的管理员密码/)).toBeInvalid();
    expect(vi.mocked(fetch)).toHaveBeenCalledTimes(2);
    const result = await axe.run(document.body, {
      rules: { "color-contrast": { enabled: false } },
    });
    expect(result.passes.flatMap((rule) => rule.nodes)).not.toHaveLength(0);
    expect(result.violations).toEqual([]);
    await userEvent.type(
      screen.getByLabelText(/^你的管理员密码/),
      PASSWORD_SENTINEL,
    );
    await userEvent.click(confirm);
    expect(await screen.findByText("管理员密码不正确")).toBeVisible();
    await userEvent.type(
      screen.getByLabelText(/^你的管理员密码/),
      PASSWORD_SENTINEL,
    );
    await userEvent.click(confirm);
    await waitFor(() =>
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
    );
    expect(await sourceCard("ModelScope")).toHaveTextContent("未验证");
    expect(await sourceCard("ModelScope")).not.toHaveTextContent("可访问");
    const writes = vi
      .mocked(fetch)
      .mock.calls.slice(1)
      .map((call) => {
        const body = call[1]?.body;
        if (typeof body !== "string") throw new Error("Expected JSON body");
        return JSON.parse(body) as Record<string, unknown>;
      });
    expect(writes.map((body) => body.confirm_unverified)).toEqual([
      undefined,
      true,
      true,
    ]);
    expect(
      await collectSecretResidue(
        queryClient,
        [TOKEN_SENTINEL, PASSWORD_SENTINEL],
        [],
        apiClient.observedErrors,
      ),
    ).toEqual([]);
  });

  it("requires a new validation when the credential changes after an unavailable result", async () => {
    const apiClient = new FakeApiClient(ADMIN_ME);
    apiClient.modelSourceUpdateResults.push(
      new ApiError({
        status: 409,
        code: "model_source_validation_unavailable",
      }),
    );
    renderApplication(apiClient, "/system?tab=model-sources");
    await openConfigureAndFill(TOKEN_SENTINEL, PASSWORD_SENTINEL);
    await userEvent.click(screen.getByRole("button", { name: "验证并保存" }));
    expect(
      await screen.findByRole("button", { name: "仍然保存（未验证）" }),
    ).toBeVisible();
    await userEvent.type(
      screen.getByLabelText(/^新的 Access Token/),
      "changed",
    );
    expect(screen.getByRole("button", { name: "验证并保存" })).toBeVisible();
  });
});
