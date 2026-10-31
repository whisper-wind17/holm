import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import axe from "axe-core";
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiClient, ApiError } from "../api/client";
import {
  parseDownloadCreationResponse,
  parseModelCatalogPage,
  parseRegistrationResponse,
  parseResolveResponse,
  ModelSelectionContractError,
} from "../api/model-selection-contracts";
import { AppErrorBoundary } from "../app/error-boundary";
import { toThrowableError } from "../app/errors";
import {
  ADMIN_ME,
  FakeApiClient,
  USER_ME,
  renderApplication,
} from "../app/test-support";
import { ModelDownloadPage } from "./download-page";

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("M2_13B action boundaries", () => {
  it("only resolves until the administrator explicitly loads metadata", async () => {
    const client = new FakeApiClient(ADMIN_ME);
    client.resolveModelResults.push(resolved());
    client.registrationResults.push(registered());
    renderDownload(client);
    await chooseRemote("Hugging Face", "Qwen/Qwen3");
    await userEvent.click(screen.getByRole("button", { name: "读取模型信息" }));
    await screen.findByRole("region", { name: "读取信息预览" });
    expect(client.modelWriteCalls.map(({ path }) => path)).toEqual([
      "/api/v1/models/resolve",
    ]);
    await userEvent.click(
      screen.getByRole("button", { name: "只加载到模型目录" }),
    );
    await screen.findByText("已加载到模型目录。");
    expect(client.modelWriteCalls.map(({ path }) => path)).toEqual([
      "/api/v1/models/resolve",
      "/api/v1/models",
    ]);
  });

  it("FIX_27 sends the trimmed remote identity and invalidates a stale preview", async () => {
    const client = new FakeApiClient(ADMIN_ME);
    client.resolveModelResults.push(resolved());
    renderDownload(client);
    const original = "  Qwen/Qwen3-32B  ";
    await chooseRemote("Hugging Face", original);
    await userEvent.click(screen.getByRole("button", { name: "读取模型信息" }));
    await screen.findByRole("region", { name: "读取信息预览" });
    expect(client.modelWriteCalls[0]?.body.repository_id).toBe(
      "Qwen/Qwen3-32B",
    );
    expect(screen.getByRole("button", { name: "确认下载" })).toBeVisible();
    await userEvent.type(screen.getByLabelText(/^Repository ID/), "changed");
    expect(
      screen.queryByRole("button", { name: "确认下载" }),
    ).not.toBeInTheDocument();
  });

  it("sends no request when a non-empty input loses focus", async () => {
    const client = new FakeApiClient(ADMIN_ME);
    client.resolveModelResults.push(resolved());
    renderDownload(client);
    await userEvent.click(screen.getByRole("button", { name: "ModelScope" }));
    const input = screen.getByLabelText(/^Model ID/);
    await userEvent.type(input, "Qwen/Qwen3");
    await userEvent.tab();
    expect(client.modelWriteCalls).toEqual([]);
  });

  it.each([
    ["mutable_reference", true],
    ["immutable", false],
  ] as const)(
    "renders mutable-reference guidance for %s only",
    async (revisionImmutability, expected) => {
      const client = new FakeApiClient(ADMIN_ME);
      client.resolveModelResults.push(
        resolved({ revision_immutability: revisionImmutability }),
      );
      renderDownload(client);
      await chooseRemote("Hugging Face", "Qwen/Qwen3");
      await userEvent.click(
        screen.getByRole("button", { name: "读取模型信息" }),
      );
      await screen.findByRole("region", { name: "读取信息预览" });
      const guidance = screen.queryByText(
        "ModelScope 没有不可变 Revision，下载时以文件清单与逐文件哈希固定版本",
      );
      if (expected) expect(guidance).toBeVisible();
      else expect(guidance).not.toBeInTheDocument();
    },
  );

  it("surfaces parsed external_path_accepted as a contract error", async () => {
    const client = new FakeApiClient(ADMIN_ME);
    vi.spyOn(client, "resolveModel").mockImplementation((body) =>
      ApiClient.prototype.resolveModel.call(client, body),
    );
    mockJsonFetch({
      control_plane_readable: true,
      outcome: "external_path_accepted",
      runtime_readability: "unverified",
      runtime_readability_cause: "runtime_identity_check_unavailable",
    });
    renderDownloadWithBoundary(client);
    await chooseRemote("Hugging Face", "Qwen/Qwen3");
    await userEvent.click(screen.getByRole("button", { name: "读取模型信息" }));
    expect(
      await screen.findByText("model_selection_contract_error"),
    ).toBeVisible();
  });

  it("surfaces an invalid registration response as a contract error", async () => {
    const client = new FakeApiClient(ADMIN_ME);
    client.resolveModelResults.push(resolved());
    vi.spyOn(client, "registerModel").mockImplementation((body, key) =>
      ApiClient.prototype.registerModel.call(client, body, key),
    );
    mockJsonFetch({ outcome: "future_registration_outcome" });
    renderDownloadWithBoundary(client);
    await chooseRemote("Hugging Face", "Qwen/Qwen3");
    await userEvent.click(screen.getByRole("button", { name: "读取模型信息" }));
    await screen.findByRole("region", { name: "读取信息预览" });
    await userEvent.click(
      screen.getByRole("button", { name: "只加载到模型目录" }),
    );
    expect(
      await screen.findByText("model_selection_contract_error"),
    ).toBeVisible();
  });

  it("surfaces an invalid catalog response as a contract error", async () => {
    const client = new FakeApiClient(ADMIN_ME);
    vi.spyOn(client, "getModelCatalog").mockImplementation((cursor) =>
      ApiClient.prototype.getModelCatalog.call(client, cursor),
    );
    mockJsonFetch({ items: [], next_cursor: 42 });
    renderDownloadWithBoundary(client);
    await userEvent.click(screen.getByRole("button", { name: "模型目录" }));
    expect(
      await screen.findByText("model_selection_contract_error"),
    ).toBeVisible();
    expect(screen.queryByText("无法读取模型目录")).not.toBeInTheDocument();
  });

  it("preserves model-selection contract errors for throwable consumers", () => {
    const error = new ModelSelectionContractError();
    expect(toThrowableError(error)).toBe(error);
  });

  it("renders credential guidance for update_credential but not rate limiting", async () => {
    const client = new FakeApiClient(ADMIN_ME);
    client.resolveModelResults.push(
      unresolved("rate_limited", "source_rate_limited", "retry_later"),
      unresolved(
        "invalid_credential",
        "credential_invalid",
        "update_credential",
      ),
    );
    renderDownload(client);
    await chooseRemote("Hugging Face", "Qwen/Qwen3");
    await userEvent.click(screen.getByRole("button", { name: "读取模型信息" }));
    await screen.findByText("读取信息失败：");
    expect(screen.getByRole("alert")).toHaveTextContent(
      "模型源暂时限制了请求频率。",
    );
    expect(
      screen.queryByRole("link", { name: "前往「系统 › 模型源」" }),
    ).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "读取模型信息" }));
    const link = await screen.findByRole("link", {
      name: "前往「系统 › 模型源」",
    });
    expect(link).toHaveAttribute("href", "/system?tab=model-sources");
  });

  it("shows server 400 below the field and exposes the over-ten-second phase", async () => {
    const client = new FakeApiClient(ADMIN_ME);
    vi.spyOn(client, "resolveModel").mockReturnValue(
      new Promise(() => undefined),
    );
    renderDownload(client);
    await chooseRemote("Hugging Face", "not/a/repo/value");
    vi.useFakeTimers();
    fireEvent.click(screen.getByRole("button", { name: "读取模型信息" }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    expect(screen.getByText("正在等待模型源返回仓库元数据…")).toBeVisible();
  });

  it("renders server 400 as an actionable field error after sending the input", async () => {
    const client = new FakeApiClient(ADMIN_ME);
    client.resolveModelResults.push(
      new ApiError({
        code: "invalid_request",
        requestId: "resolve-400",
        responseMessage: "模型标识不正确，本次操作未执行，请检查后重试",
        status: 400,
      }),
    );
    renderDownload(client);
    await chooseRemote("Hugging Face", "not/a/repo/value");
    await userEvent.click(screen.getByRole("button", { name: "读取模型信息" }));
    expect(await screen.findByText(/Repository ID 需为/)).toBeVisible();
    expect(client.modelWriteCalls).toHaveLength(1);
  });

  it.each([
    ["unresolved", undefined],
    ["409", new ApiError({ code: "idempotency_conflict", status: 409 })],
    ["503", new ApiError({ code: "registration_contended", status: 503 })],
  ] as const)("covers load-model %s result", async (kind, failure) => {
    const client = new FakeApiClient(ADMIN_ME);
    client.resolveModelResults.push(resolved());
    client.registrationResults.push(
      failure ??
        unresolved(
          "invalid_credential",
          "credential_invalid",
          "update_credential",
        ),
    );
    renderDownload(client);
    await chooseRemote("Hugging Face", "Qwen/Qwen3");
    await userEvent.click(screen.getByRole("button", { name: "读取模型信息" }));
    await screen.findByRole("region", { name: "读取信息预览" });
    await userEvent.click(
      screen.getByRole("button", { name: "只加载到模型目录" }),
    );
    if (kind === "unresolved")
      expect(
        await screen.findByRole("link", { name: "前往「系统 › 模型源」" }),
      ).toBeVisible();
    else expect(await screen.findByText("加载模型失败")).toBeVisible();
  });
});

describe("M2_13B catalog states and identity", () => {
  it("distinguishes loading, exact empty, error and permission states", async () => {
    const loadingClient = new FakeApiClient(ADMIN_ME);
    vi.spyOn(loadingClient, "getModelCatalog").mockReturnValue(
      new Promise(() => undefined),
    );
    const loading = renderDownload(loadingClient);
    await userEvent.click(screen.getByRole("button", { name: "模型目录" }));
    expect(await screen.findByLabelText("模型目录，正在加载")).toBeVisible();
    loading.unmount();

    const empty = renderDownload(catalogClient([]));
    await userEvent.click(screen.getByRole("button", { name: "模型目录" }));
    expect(
      await screen.findByText(
        "还没有加载任何模型。从 Hugging Face 或 ModelScope 读取并下载第一个模型。",
      ),
    ).toBeVisible();
    empty.unmount();

    const failedClient = new FakeApiClient(ADMIN_ME);
    failedClient.modelCatalogResults.push(new Error("catalog failed"));
    const failed = renderDownload(failedClient);
    await userEvent.click(screen.getByRole("button", { name: "模型目录" }));
    expect(await screen.findByText("无法读取模型目录")).toBeVisible();
    expect(screen.queryByText("模型目录为空")).not.toBeInTheDocument();
    failed.unmount();

    const deniedClient = new FakeApiClient(ADMIN_ME);
    deniedClient.modelCatalogResults.push(
      new ApiError({ code: "forbidden", status: 403 }),
    );
    renderDownload(deniedClient);
    await userEvent.click(screen.getByRole("button", { name: "模型目录" }));
    expect(await screen.findByText("无权限查看模型目录")).toBeVisible();
  });

  it("renders all availability states, explicit unrecognized values, and no Local Path selection", async () => {
    const client = catalogClient([
      catalogItem({
        display_name: "ready",
        local_availability: "artifact_ready",
      }),
      catalogItem({
        display_name: "active",
        id: UUID_2,
        local_availability: "download_active",
      }),
      catalogItem({
        architecture: { state: "unrecognized" },
        display_name: "unknown",
        estimated_size_bytes: { state: "unrecognized" },
        id: UUID_3,
        modalities: { state: "unrecognized" },
      }),
      catalogItem({
        display_name: "local",
        id: UUID_4,
        repository_id: null,
        source: "Local Path",
      }),
    ]);
    renderDownload(client);
    await userEvent.click(screen.getByRole("button", { name: "模型目录" }));
    await screen.findByText("ready");
    expect(screen.getByText("本地可用")).toBeVisible();
    expect(screen.getByText("下载中")).toBeVisible();
    const unknownRow = screen.getByText("unknown").closest("tr");
    expect(unknownRow).not.toBeNull();
    expect(unknownRow?.textContent.match(/未识别/g) ?? []).toHaveLength(2);
    expect(unknownRow).not.toHaveTextContent("0 B");
    const localRow = screen.getByText("local").closest("tr");
    expect(
      within(localRow as HTMLElement).getByText("本地目录模型不需要下载"),
    ).toBeVisible();
    expect(
      within(localRow as HTMLElement).queryByRole("button", { name: "选择" }),
    ).not.toBeInTheDocument();
  });

  it("uses repository_id rather than display_name and confirms through downloads only", async () => {
    const client = catalogClient([
      catalogItem({
        display_name: "Friendly model",
        repository_id: "owner/real-repository",
      }),
    ]);
    client.downloadCreationResults.push(downloadCreated());
    renderDownload(client);
    await userEvent.click(screen.getByRole("button", { name: "模型目录" }));
    const row = (await screen.findByText("Friendly model")).closest("tr");
    await userEvent.click(
      within(row as HTMLElement).getByRole("button", { name: "选择" }),
    );
    await screen.findByRole("region", { name: "读取信息预览" });
    expect(client.modelWriteCalls.map(({ path }) => path)).toEqual([
      "/api/v1/models/resolve",
    ]);
    await userEvent.click(screen.getByRole("button", { name: "确认下载" }));
    await screen.findAllByText("已开始下载。");
    expect(client.modelWriteCalls.map(({ path }) => path)).toEqual([
      "/api/v1/models/resolve",
      "/api/v1/downloads",
    ]);
    expect(client.modelWriteCalls[0]?.body.repository_id).toBe(
      "owner/real-repository",
    );
  });

  it("UI_32 shows freshly read catalog facts, unrecognized values and revision", async () => {
    const revision = "catalog-revision";
    const client = catalogClient([
      catalogItem({ display_name: "Friendly catalog model", revision }),
    ]);
    client.resolveModelResults.unshift(
      resolved({
        architecture: { state: "unrecognized" },
        total_size_bytes: { state: "unrecognized" },
        resolved_revision: revision,
      }),
    );
    renderDownload(client);
    await userEvent.click(screen.getByRole("button", { name: "模型目录" }));
    const row = (await screen.findByText("Friendly catalog model")).closest(
      "tr",
    );
    await userEvent.click(
      within(row as HTMLElement).getByRole("button", { name: "选择" }),
    );
    const confirmation = await screen.findByRole("region", {
      name: "读取信息预览",
    });
    expect(
      within(confirmation).getByRole("heading", { name: "Qwen/Qwen3" }),
    ).toBeVisible();
    expect(
      within(confirmation).getByText("Hugging Face · 读取于刚刚"),
    ).toBeVisible();
    expect(within(confirmation).getByText("架构")).toBeVisible();
    expect(within(confirmation).getByText("模态")).toBeVisible();
    expect(within(confirmation).getByText("text")).toBeVisible();
    expect(within(confirmation).getByText("预计大小")).toBeVisible();
    expect(within(confirmation).getByText("未识别")).toBeVisible();
    expect(
      within(confirmation).getByText("未识别（共 1 个文件）"),
    ).toBeVisible();
    expect(within(confirmation).getByText("本地文件")).toBeVisible();
    expect(within(confirmation).getByText("本地还没有这个版本")).toBeVisible();
    expect(confirmation).toHaveTextContent(revision);
    expect(client.modelWriteCalls[0]?.body).toEqual({
      repository_id: "Qwen/Qwen3",
      source: "Hugging Face",
      revision,
    });
  });

  it("returns the opaque catalog cursor unchanged", async () => {
    const client = new FakeApiClient(ADMIN_ME);
    client.modelCatalogResults.push(
      parseModelCatalogPage({
        items: [catalogItem()],
        next_cursor: "opaque+/cursor==",
      }),
      parseModelCatalogPage({ items: [], next_cursor: null }),
    );
    renderDownload(client);
    await userEvent.click(screen.getByRole("button", { name: "模型目录" }));
    await screen.findByText("Catalog display name");
    await userEvent.click(screen.getByRole("button", { name: "加载更多" }));
    await waitFor(() => {
      expect(client.modelCatalogCursors).toEqual([
        undefined,
        "opaque+/cursor==",
      ]);
    });
  });
});

describe("M2_13B write outcomes", () => {
  it("surfaces an invalid download response as a contract error", async () => {
    const client = catalogClient([catalogItem()]);
    vi.spyOn(client, "createDownload").mockImplementation((body, key) =>
      ApiClient.prototype.createDownload.call(client, body, key),
    );
    mockJsonFetch({ disposition: "future_download_disposition" });
    renderDownloadWithBoundary(client);
    await selectFirstCatalogRow();
    await userEvent.click(screen.getByRole("button", { name: "确认下载" }));
    expect(
      await screen.findByText("model_selection_contract_error"),
    ).toBeVisible();
  });

  it("reuses a key after network failure and rotates it after a definite success", async () => {
    const client = catalogClient([catalogItem()]);
    client.downloadCreationResults.push(
      new Error("network"),
      downloadCreated(),
      downloadCreated({ id: UUID_4 }),
    );
    renderDownload(client);
    await selectFirstCatalogRow();
    await userEvent.click(screen.getByRole("button", { name: "确认下载" }));
    await screen.findAllByText("确认下载失败");
    await userEvent.click(screen.getByRole("button", { name: "确认下载" }));
    await screen.findAllByText("已开始下载。");
    client.resolveModelResults.push(resolved());
    await userEvent.click(screen.getByRole("button", { name: "读取模型信息" }));
    await screen.findByRole("region", { name: "读取信息预览" });
    await userEvent.click(screen.getByRole("button", { name: "确认下载" }));
    await waitFor(() => {
      expect(
        client.modelWriteCalls.filter(
          ({ path }) => path === "/api/v1/downloads",
        ),
      ).toHaveLength(3);
    });
    const keys = client.modelWriteCalls
      .filter(({ path }) => path === "/api/v1/downloads")
      .map(({ idempotencyKey }) => idempotencyKey);
    expect(keys[1]).toBe(keys[0]);
    expect(keys[2]).not.toBe(keys[1]);
  });

  it.each([
    ["created", false, "已开始下载。"],
    ["joined_existing", true, "已加入进行中的下载。"],
  ] as const)(
    "renders disposition %s and replay truthfully",
    async (disposition, replayed, title) => {
      const client = catalogClient([catalogItem()]);
      client.downloadCreationResults.push(
        downloadCreated({ disposition, replayed }),
      );
      renderDownload(client);
      await selectFirstCatalogRow();
      await userEvent.click(screen.getByRole("button", { name: "确认下载" }));
      expect(
        await screen.findByText(title, { selector: ".pw-alert strong" }),
      ).toBeVisible();
      if (replayed)
        expect(screen.getByText(/这是之前已提交的同一请求/)).toBeVisible();
      expect(screen.getByText(/任务在后台进行，可以离开本页/)).toBeVisible();
    },
  );

  it.each([
    [409, "idempotency_conflict"],
    [503, "download_creation_contended"],
  ])("shows HTTP %s message and Request ID", async (status, code) => {
    const client = catalogClient([catalogItem()]);
    client.downloadCreationResults.push(
      new ApiError({
        code,
        requestId: "request-positive-control",
        responseMessage: "服务端判定本次未创建下载任务",
        status,
      }),
    );
    renderDownload(client);
    await selectFirstCatalogRow();
    await userEvent.click(screen.getByRole("button", { name: "确认下载" }));
    expect(
      await screen.findByText("服务端判定本次未创建下载任务"),
    ).toBeVisible();
    expect(
      screen.getByText("Request ID：request-positive-control"),
    ).toBeVisible();
  });

  it("never cancels a successfully created download on unmount", async () => {
    const client = catalogClient([catalogItem()]);
    client.downloadCreationResults.push(downloadCreated());
    const beforeUnload = vi.spyOn(window, "addEventListener");
    const view = renderDownload(client);
    await selectFirstCatalogRow();
    await userEvent.click(screen.getByRole("button", { name: "确认下载" }));
    await screen.findByText("已开始下载。", {
      selector: ".pw-alert strong",
    });
    view.unmount();
    expect(client.downloadCommandCalls).toEqual([]);
    expect(
      beforeUnload.mock.calls.some(([type]) => type === "beforeunload"),
    ).toBe(false);
  });

  it("never cancels a successfully created download when navigation leaves the page", async () => {
    const client = catalogClient([catalogItem()]);
    client.downloadCreationResults.push(downloadCreated());
    const beforeUnload = vi.spyOn(window, "addEventListener");
    renderDownload(client);
    await selectFirstCatalogRow();
    await userEvent.click(screen.getByRole("button", { name: "确认下载" }));
    await userEvent.click(
      await screen.findByRole("link", { name: "查看本地模型" }),
    );
    await waitFor(() => {
      expect(
        screen.queryByRole("heading", { name: "下载模型" }),
      ).not.toBeInTheDocument();
    });
    expect(client.downloadCommandCalls).toEqual([]);
    expect(
      beforeUnload.mock.calls.some(([type]) => type === "beforeunload"),
    ).toBe(false);
  });
});

describe("M2_13B routing, responsive and accessibility", () => {
  it("focuses the H1 when the models navigation grants the route", async () => {
    const client = catalogClient([]);
    renderApplication(client, "/models/download");
    const heading = await screen.findByRole("heading", {
      name: "下载模型",
      level: 1,
    });
    await waitFor(() => expect(heading).toHaveFocus());
  });

  it("keeps the unknown-path permission behavior without a models navigation item", async () => {
    renderApplication(new FakeApiClient(USER_ME), "/models/download");
    expect(
      await screen.findByRole("heading", { name: "无权限" }),
    ).toBeVisible();
  });

  it("defines a narrow single-column layout without literal colors", () => {
    const css = readFileSync(
      resolve(dirname(fileURLToPath(import.meta.url)), "../styles/app.css"),
      "utf8",
    );
    expect(css).toContain("@media (max-width: 767px)");
    expect(css).toMatch(
      /\.pw-model-preview dl\s*\{[\s\S]*?grid-template-columns: 1fr/,
    );
  });

  it("has non-empty axe passes and no violations for catalog, preview, guidance and result", async () => {
    const catalog = renderDownload(catalogClient([]));
    await userEvent.click(screen.getByRole("button", { name: "模型目录" }));
    await screen.findByText("模型目录为空");
    await expectAxe(catalog.container);
    catalog.unmount();

    const previewClient = new FakeApiClient(ADMIN_ME);
    previewClient.resolveModelResults.push(resolved());
    const preview = renderDownload(previewClient);
    await chooseRemote("Hugging Face", "Qwen/Qwen3");
    await userEvent.click(screen.getByRole("button", { name: "读取模型信息" }));
    await screen.findByRole("region", { name: "读取信息预览" });
    await expectAxe(preview.container);
    preview.unmount();

    const guidanceClient = new FakeApiClient(ADMIN_ME);
    guidanceClient.resolveModelResults.push(
      unresolved(
        "invalid_credential",
        "credential_invalid",
        "update_credential",
      ),
    );
    const guidance = renderDownload(guidanceClient);
    await chooseRemote("Hugging Face", "Qwen/Qwen3");
    await userEvent.click(screen.getByRole("button", { name: "读取模型信息" }));
    await screen.findByRole("link", { name: "前往「系统 › 模型源」" });
    await expectAxe(guidance.container);
    guidance.unmount();

    const resultClient = catalogClient([catalogItem()]);
    resultClient.downloadCreationResults.push(downloadCreated());
    const result = renderDownload(resultClient);
    await selectFirstCatalogRow();
    await userEvent.click(screen.getByRole("button", { name: "确认下载" }));
    await screen.findAllByText("已开始下载。");
    await expectAxe(result.container);
  });
});

function renderDownload(client: FakeApiClient) {
  storageFixture(client);
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/models/download"]}>
        <Routes>
          <Route
            element={<ModelDownloadPage apiClient={client} />}
            path="/models/download"
          />
          <Route element={<p>本地模型页</p>} path="/models" />
          <Route element={<DeploymentLocation />} path="/models/deploy" />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function renderDownloadWithBoundary(client: FakeApiClient) {
  storageFixture(client);
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <AppErrorBoundary>
        <MemoryRouter initialEntries={["/models/download"]}>
          <ModelDownloadPage apiClient={client} />
        </MemoryRouter>
      </AppErrorBoundary>
    </QueryClientProvider>,
  );
}

function catalogClient(items: unknown[]) {
  const client = new FakeApiClient(ADMIN_ME);
  client.resolveModelResults.push(resolved());
  client.modelCatalogResults.push(
    parseModelCatalogPage({ items, next_cursor: null }),
  );
  return client;
}

function mockJsonFetch(body: unknown) {
  vi.spyOn(document, "cookie", "get").mockReturnValue(
    "__Host-pws_csrf=model-selection-csrf",
  );
  vi.spyOn(globalThis, "fetch").mockResolvedValue(
    new Response(JSON.stringify(body), {
      headers: { "Content-Type": "application/json" },
      status: 200,
    }),
  );
}

async function chooseRemote(
  source: "Hugging Face" | "ModelScope",
  value: string,
) {
  await userEvent.click(screen.getByRole("button", { name: source }));
  await userEvent.type(
    screen.getByLabelText(
      source === "Hugging Face" ? /^Repository ID/ : /^Model ID/,
    ),
    value,
  );
}

async function selectFirstCatalogRow() {
  await userEvent.click(screen.getByRole("button", { name: "模型目录" }));
  const row = (await screen.findByText("Catalog display name")).closest("tr");
  await userEvent.click(
    within(row as HTMLElement).getByRole("button", { name: "选择" }),
  );
  await screen.findByRole("region", { name: "读取信息预览" });
}

async function expectAxe(container: HTMLElement) {
  const result = await axe.run(container);
  expect(result.violations).toEqual([]);
  expect(result.passes.length).toBeGreaterThan(0);
}

function unresolved(
  status: string | null,
  reason_code: string,
  next_action: string,
) {
  const value = parseResolveResponse({
    outcome: "unresolved",
    status,
    reason_code,
    next_action,
  });
  if (value.outcome !== "unresolved")
    throw new Error("unresolved fixture mismatch");
  return value;
}

function resolved(overrides: Record<string, unknown> = {}) {
  const identified = <T,>(value: T) => ({
    state: "identified" as const,
    value,
  });
  return parseResolveResponse({
    local_availability: "absent",
    outcome: "resolved",
    deployable_layout: "supported",
    resolved: {
      architecture: identified("Qwen3ForCausalLM"),
      candidate_runtimes: identified(["vLLM"]),
      context_length: identified(32_768),
      files: identified([
        {
          content_sha256: { state: "unrecognized" },
          path: "weights.safetensors",
          size_bytes: 1024,
        },
      ]),
      license: { state: "unrecognized" },
      modalities: identified(["text"]),
      precision: identified("BF16"),
      quantization: { state: "unrecognized" },
      repository_id: "Qwen/Qwen3",
      requested_revision_norm: "@default",
      resolved_revision: "main",
      revision_immutability: "mutable_reference",
      source: "Hugging Face",
      total_size_bytes: identified(1024),
      ...overrides,
    },
  });
}

function registered() {
  const identified = <T,>(value: T) => ({
    state: "identified" as const,
    value,
  });
  return parseRegistrationResponse({
    architecture: identified("Qwen3ForCausalLM"),
    candidate_runtimes: identified(["vLLM"]),
    context_length: identified(32_768),
    display_name: "Qwen/Qwen3",
    estimated_size_bytes: identified(1024),
    id: UUID_1,
    license: { state: "unrecognized" },
    metadata_status: "CONFIRMED",
    modalities: identified(["text"]),
    precision: identified("BF16"),
    quantization: { state: "unrecognized" },
    replayed: false,
    source: "Hugging Face",
  });
}

function downloadCreated(overrides: Record<string, unknown> = {}) {
  return parseDownloadCreationResponse({
    disposition: "created",
    id: UUID_2,
    model_id: UUID_1,
    replayed: false,
    status: "Queued",
    ...overrides,
  });
}

function catalogItem(overrides: Record<string, unknown> = {}) {
  return {
    architecture: { state: "identified", value: "Qwen3ForCausalLM" },
    display_name: "Catalog display name",
    estimated_size_bytes: { state: "identified", value: 1024 },
    id: UUID_1,
    local_availability: "absent",
    modalities: { state: "identified", value: ["text"] },
    references: { artifacts: 0, download_tasks: 0, model_instances: null },
    repository_id: "Qwen/Qwen3",
    revision: "—",
    source: "Hugging Face",
    ...overrides,
  };
}

const UUID_1 = "0198f58a-2586-7e37-bbf5-fefe165e8081";
const UUID_2 = "0198f58a-2586-7e37-bbf5-fefe165e8082";
const UUID_3 = "0198f58a-2586-7e37-bbf5-fefe165e8083";
const UUID_4 = "0198f58a-2586-7e37-bbf5-fefe165e8084";

// FX-UI-32-storage: sufficient test storage for the pre-existing download flows.
function storageFixture(client: FakeApiClient) {
  vi.spyOn(client, "getModelArtifacts").mockResolvedValue({
    items: [],
    next_cursor: null,
    summary: {
      active_download_tasks: 0,
      managed_bytes: 0,
      storage_available_bytes: 4096,
    },
  });
}

function DeploymentLocation() {
  const location = useLocation();
  return (
    <p>
      部署目标：{location.pathname}
      {location.search}
    </p>
  );
}

// FX-UI-32: metadata fixtures extend M2_13B's parsed remote response above.
describe("UI_32 revision and result actions", () => {
  it.each(["", "release-v1"])(
    "carries revision %s through all three requests",
    async (revision) => {
      const client = new FakeApiClient(ADMIN_ME);
      client.resolveModelResults.push(resolved(), resolved());
      client.registrationResults.push(registered());
      client.downloadCreationResults.push(downloadCreated());
      renderDownload(client);
      await chooseRemote("Hugging Face", "Qwen/Qwen3");
      if (revision)
        await userEvent.type(
          screen.getByLabelText("Revision（可选）"),
          revision,
        );
      await userEvent.click(
        screen.getByRole("button", { name: "读取模型信息" }),
      );
      await screen.findByRole("region", { name: "读取信息预览" });
      await userEvent.click(
        screen.getByRole("button", { name: "只加载到模型目录" }),
      );
      await screen.findByText("已加载到模型目录。");
      expect(
        screen.queryByRole("region", { name: "读取信息预览" }),
      ).not.toBeInTheDocument();
      expect(
        screen.getByRole("link", { name: "返回本地模型" }),
      ).toHaveAttribute("href", "/models?tab=local-models");
      await userEvent.click(
        screen.getByRole("button", { name: "读取模型信息" }),
      );
      await screen.findByRole("region", { name: "读取信息预览" });
      await userEvent.click(screen.getByRole("button", { name: "确认下载" }));
      await screen.findByText("已开始下载。");
      expect(
        screen.queryByRole("region", { name: "读取信息预览" }),
      ).not.toBeInTheDocument();
      const body = {
        repository_id: "Qwen/Qwen3",
        source: "Hugging Face",
        ...(revision ? { revision } : {}),
      };
      expect(
        client.modelWriteCalls.map((call) => [call.path, call.body]),
      ).toEqual([
        ["/api/v1/models/resolve", body],
        ["/api/v1/models", body],
        ["/api/v1/models/resolve", body],
        ["/api/v1/downloads", body],
      ]);
    },
  );

  it("defaults to HF, enables short input and clears input, revision and completion on switching source", async () => {
    const client = new FakeApiClient(ADMIN_ME);
    client.resolveModelResults.push(resolved());
    client.registrationResults.push(registered());
    renderDownload(client);
    expect(screen.getByRole("group", { name: "模型来源" })).toBeVisible();
    expect(
      screen.getByRole("button", { name: "Hugging Face" }),
    ).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "读取模型信息" })).toBeEnabled();
    await userEvent.type(screen.getByLabelText(/^Repository ID/), "  ab  ");
    expect(screen.getByRole("button", { name: "读取模型信息" })).toBeEnabled();
    await userEvent.clear(screen.getByLabelText(/^Repository ID/));
    await userEvent.type(screen.getByLabelText(/^Repository ID/), "Qwen/Qwen3");
    await userEvent.type(screen.getByLabelText("Revision（可选）"), "v1");
    await userEvent.click(screen.getByRole("button", { name: "读取模型信息" }));
    await screen.findByRole("region", { name: "读取信息预览" });
    await userEvent.click(
      screen.getByRole("button", { name: "只加载到模型目录" }),
    );
    await screen.findByText("已加载到模型目录。");
    await userEvent.click(screen.getByRole("button", { name: "ModelScope" }));
    expect(screen.getByLabelText(/^Model ID/)).toHaveValue("");
    expect(screen.getByLabelText("Revision（可选）")).toHaveValue("");
    expect(screen.getByLabelText("Revision（可选）")).toHaveAttribute(
      "placeholder",
      "例如 master",
    );
    expect(screen.queryByText("已加载到模型目录。")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Hugging Face" }));
    expect(screen.getByLabelText(/^Repository ID/)).toHaveValue("");
  });

  it.each([
    [512, false, true, "磁盘空间不足：需要 1 KB，模型存储目录剩余 512 B。"],
    [4096, false, false, "下载需要 1 KB，模型存储目录剩余 4 KB。"],
    [
      null,
      false,
      false,
      "无法预先确认磁盘空间；空间不足时下载任务会暂停并提示清理。",
    ],
    [
      4096,
      true,
      false,
      "无法预先确认磁盘空间；空间不足时下载任务会暂停并提示清理。",
    ],
    [
      "failed",
      false,
      false,
      "无法预先确认磁盘空间；空间不足时下载任务会暂停并提示清理。",
    ],
  ] as const)(
    "storage %s unknown size %s",
    async (available, unknown, disabled, note) => {
      const client = new FakeApiClient(ADMIN_ME);
      client.resolveModelResults.push(
        resolved(
          unknown ? { total_size_bytes: { state: "unrecognized" } } : {},
        ),
      );
      renderDownload(client);
      if (available === "failed")
        vi.spyOn(client, "getModelArtifacts").mockRejectedValue(
          new Error("unreachable"),
        );
      else
        vi.spyOn(client, "getModelArtifacts").mockResolvedValue({
          items: [],
          next_cursor: null,
          summary: {
            active_download_tasks: 0,
            managed_bytes: 0,
            storage_available_bytes: available,
          },
        });
      await chooseRemote("Hugging Face", "Qwen/Qwen3");
      await userEvent.click(
        screen.getByRole("button", { name: "读取模型信息" }),
      );
      await screen.findByRole("region", { name: "读取信息预览" });
      expect(screen.getByText(note, { exact: !disabled })).toBeVisible();
      expect(
        screen
          .getByRole("button", { name: "确认下载" })
          .hasAttribute("disabled"),
      ).toBe(disabled);
      expect(
        screen.getByRole("button", { name: "只加载到模型目录" }),
      ).toBeEnabled();
      expect(
        vi.spyOn(client, "getModelArtifacts"),
      ).toHaveBeenCalledExactlyOnceWith();
      if (disabled) {
        await userEvent.click(screen.getByRole("button", { name: "确认下载" }));
        expect(client.modelWriteCalls.map((call) => call.path)).toEqual([
          "/api/v1/models/resolve",
        ]);
      }
    },
  );

  it.each([true, false])(
    "deploys an existing version with matching artifact %s",
    async (found) => {
      const client = new FakeApiClient(ADMIN_ME);
      const value = resolved();
      if (value.outcome !== "resolved") throw new Error("fixture");
      client.resolveModelResults.push({
        ...value,
        local_availability: "artifact_ready",
      });
      renderDownload(client);
      await chooseRemote("Hugging Face", "Qwen/Qwen3");
      await userEvent.click(
        screen.getByRole("button", { name: "读取模型信息" }),
      );
      await screen.findByRole("region", { name: "读取信息预览" });
      expect(screen.getByText("已有可用的本地模型")).toBeVisible();
      expect(
        screen.getByText(
          "同一来源、同一版本只保存一份，可以直接部署，不需要再次下载。",
        ),
      ).toBeVisible();
      expect(
        screen.queryByRole("button", { name: "确认下载" }),
      ).not.toBeInTheDocument();
      vi.spyOn(client, "getModelArtifacts").mockResolvedValue({
        items: [
          {
            created_at: "2026-10-14T00:00:00Z",
            updated_at: "2026-10-14T00:00:00Z",
            display_name: "Qwen",
            id: UUID_1,
            model_id: UUID_2,
            last_verified_at: null,
            path: "/models/fixture",
            referencing_instances: 0,
            repository_id: "qwen/QWEN3",
            resolved_revision: found ? "main" : "other",
            size_bytes: 1024,
            source: "Hugging Face",
            status: "Ready",
            storage_type: "Managed",
          },
        ],
        next_cursor: null,
        summary: {
          active_download_tasks: 0,
          managed_bytes: 1024,
          storage_available_bytes: 4096,
        },
      });
      await userEvent.click(
        screen.getByRole("button", { name: "部署这个模型" }),
      );
      expect(
        await screen.findByText(
          `部署目标：/models/deploy${found ? `?artifact_id=${UUID_1}` : ""}`,
        ),
      ).toBeVisible();
      expect(vi.spyOn(client, "getModelArtifacts")).toHaveBeenLastCalledWith({
        search: "Qwen/Qwen3",
        source: "Hugging Face",
        status: "Ready",
      });
    },
  );

  it("UI_32 reports deployment lookup failure and allows another attempt", async () => {
    const client = new FakeApiClient(ADMIN_ME);
    const value = resolved();
    if (value.outcome !== "resolved") throw new Error("fixture");
    client.resolveModelResults.push({
      ...value,
      local_availability: "artifact_ready",
    });
    renderDownload(client);
    await chooseRemote("Hugging Face", "Qwen/Qwen3");
    await userEvent.click(screen.getByRole("button", { name: "读取模型信息" }));
    await screen.findByRole("region", { name: "读取信息预览" });
    vi.spyOn(client, "getModelArtifacts").mockRejectedValueOnce(
      new ApiError({
        code: "unavailable",
        status: 503,
        requestId: "UI-32-deploy",
      }),
    );
    await userEvent.click(screen.getByRole("button", { name: "部署这个模型" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "无法读取本地模型",
    );
    expect(screen.getByText("Request ID：UI-32-deploy")).toBeVisible();
    await userEvent.click(screen.getByRole("button", { name: "部署这个模型" }));
    expect(await screen.findByText("部署目标：/models/deploy")).toBeVisible();
  });

  it("links to active downloads without invented progress", async () => {
    const client = new FakeApiClient(ADMIN_ME);
    const value = resolved();
    if (value.outcome !== "resolved") throw new Error("fixture");
    client.resolveModelResults.push({
      ...value,
      local_availability: "download_active",
    });
    renderDownload(client);
    await chooseRemote("Hugging Face", "Qwen/Qwen3");
    await userEvent.click(screen.getByRole("button", { name: "读取模型信息" }));
    const preview = await screen.findByRole("region", { name: "读取信息预览" });
    expect(preview).toHaveTextContent("正在下载");
    expect(preview).not.toHaveTextContent("%");
    expect(
      screen.getByText(
        "同一版本已有进行中的下载任务，重复下载会并入那个任务。",
      ),
    ).toBeVisible();
    expect(screen.getByRole("link", { name: "查看下载进度" })).toHaveAttribute(
      "href",
      "/models?tab=local-models",
    );
    expect(
      screen.queryByRole("button", { name: "确认下载" }),
    ).not.toBeInTheDocument();
  });

  it("retries a transport error with the same revision and Request ID", async () => {
    const client = new FakeApiClient(ADMIN_ME);
    client.resolveModelResults.push(
      new ApiError({
        code: "unavailable",
        status: 503,
        requestId: "UI-32-read",
      }),
      resolved({
        revision_immutability: "immutable",
        requested_revision_norm: "v1",
        resolved_revision: "abc123",
        candidate_runtimes: { state: "identified", value: ["vLLM", "SGLang"] },
      }),
    );
    renderDownload(client);
    await chooseRemote("ModelScope", "Qwen/Qwen3");
    await userEvent.type(screen.getByLabelText("Revision（可选）"), "v1");
    await userEvent.click(screen.getByRole("button", { name: "读取模型信息" }));
    expect(await screen.findByText("Request ID：UI-32-read")).toBeVisible();
    await userEvent.click(screen.getByRole("button", { name: "重试" }));
    await screen.findByRole("region", { name: "读取信息预览" });
    expect(screen.getByText("由 v1 解析的不可变 Commit")).toBeVisible();
    expect(screen.getByText("vLLM、SGLang")).toBeVisible();
    expect(screen.getByText("32,768")).toBeVisible();
    expect(client.modelWriteCalls.map((call) => call.body)).toEqual(
      Array.from({ length: 2 }, () => ({
        repository_id: "Qwen/Qwen3",
        source: "ModelScope",
        revision: "v1",
      })),
    );
  });

  it.each([
    ["not_found", "source_not_found", "check_source_reference", false],
    [null, "validation_timeout", "retry_later", true],
  ])(
    "shows unresolved %s with the appropriate retry action",
    async (status, reason, action, retry) => {
      const client = new FakeApiClient(ADMIN_ME);
      client.resolveModelResults.push(
        unresolved(status, reason, action),
        resolved(),
      );
      renderDownload(client);
      await chooseRemote("Hugging Face", "Qwen/Qwen3");
      await userEvent.click(
        screen.getByRole("button", { name: "读取模型信息" }),
      );
      expect(await screen.findByRole("alert")).toHaveTextContent(
        "读取信息失败：",
      );
      if (retry) {
        await userEvent.click(screen.getByRole("button", { name: "重试" }));
        await screen.findByRole("region", { name: "读取信息预览" });
        expect(client.modelWriteCalls[1]?.body).toEqual(
          client.modelWriteCalls[0]?.body,
        );
      } else
        expect(
          screen.queryByRole("button", { name: "重试" }),
        ).not.toBeInTheDocument();
    },
  );
});

// FX-UI-38: existing resolved/unresolved fixtures plus manually ordered promises.
describe("UI_38 current model read", () => {
  it.each(["source", "repository", "revision"])(
    "abandons on %s edits and ignores the old completion during a new read",
    async (edit) => {
      const client = new FakeApiClient(ADMIN_ME);
      type Result = Awaited<ReturnType<ApiClient["resolveModel"]>>;
      let finishOld!: (value: Result) => void;
      let finishNew!: (value: Result) => void;
      const read = vi
        .spyOn(client, "resolveModel")
        .mockReturnValueOnce(
          new Promise<Result>((resolve) => {
            finishOld = resolve;
          }),
        )
        .mockReturnValueOnce(
          new Promise<Result>((resolve) => {
            finishNew = resolve;
          }),
        );
      renderDownload(client);
      await chooseRemote("Hugging Face", "Qwen/Qwen3");
      vi.useFakeTimers();
      fireEvent.click(screen.getByRole("button", { name: "读取模型信息" }));
      await act(async () => {
        await vi.advanceTimersByTimeAsync(10_000);
      });
      expect(screen.getByText("正在等待模型源返回仓库元数据…")).toBeVisible();
      expect(screen.getByRole("button", { name: "ModelScope" })).toBeEnabled();
      expect(screen.getByLabelText(/^Repository ID/)).toBeEnabled();
      expect(screen.getByLabelText("Revision（可选）")).toBeEnabled();
      if (edit === "source") {
        fireEvent.click(screen.getByRole("button", { name: "ModelScope" }));
        expect(
          screen.getByRole("button", { name: "读取模型信息" }),
        ).toBeEnabled();
        fireEvent.change(screen.getByLabelText(/^Model ID/), {
          target: { value: "Qwen/New" },
        });
      } else {
        fireEvent.change(
          screen.getByLabelText(
            edit === "repository" ? /^Repository ID/ : "Revision（可选）",
          ),
          { target: { value: edit === "repository" ? "Qwen/New" : "v2" } },
        );
      }
      expect(
        screen.queryByText("正在等待模型源返回仓库元数据…"),
      ).not.toBeInTheDocument();
      expect(
        screen.getByRole("button", { name: "读取模型信息" }),
      ).toBeEnabled();
      fireEvent.click(screen.getByRole("button", { name: "读取模型信息" }));
      await act(async () => {
        await vi.advanceTimersByTimeAsync(0);
      });
      expect(read).toHaveBeenCalledTimes(2);
      expect(read.mock.calls[1]?.[0]).toEqual({
        source: edit === "source" ? "ModelScope" : "Hugging Face",
        repository_id: edit === "revision" ? "Qwen/Qwen3" : "Qwen/New",
        ...(edit === "revision" ? { revision: "v2" } : {}),
      });
      await act(async () => {
        finishOld(unresolved(null, "validation_timeout", "retry_later"));
        await Promise.resolve();
      });
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
      expect(
        screen.getByRole("button", { name: "正在读取模型信息…" }),
      ).toBeDisabled();
      await act(async () => {
        await vi.advanceTimersByTimeAsync(10_000);
      });
      expect(screen.getByText("正在等待模型源返回仓库元数据…")).toBeVisible();
      await act(async () => {
        finishNew(resolved());
        await Promise.resolve();
      });
      vi.useRealTimers();
      expect(
        await screen.findByRole("region", { name: "读取信息预览" }),
      ).toBeVisible();
      expect(
        screen.queryByText("正在等待模型源返回仓库元数据…"),
      ).not.toBeInTheDocument();
    },
  );

  it.each(["Hugging Face", "ModelScope"] as const)(
    "shows safe unreachable text and retries for %s",
    async (source) => {
      const client = new FakeApiClient(ADMIN_ME);
      client.resolveModelResults.push(
        unresolved(null, "validation_timeout", "retry_later"),
        unresolved(null, "validation_failed", "retry_later"),
        resolved(),
      );
      const { container } = renderDownload(client);
      await chooseRemote(source, "Qwen/Qwen3");
      await userEvent.click(
        screen.getByRole("button", { name: "读取模型信息" }),
      );
      for (const reason of ["超时", "失败"]) {
        expect(await screen.findByRole("alert")).toHaveTextContent(
          `连接 ${source} ${reason}，服务器可能无法访问该模型源。当前不能读取该模型。请稍后重试，或改用其他模型源。`,
        );
        await expectAxe(container);
        await userEvent.click(screen.getByRole("button", { name: "重试" }));
      }
      await screen.findByRole("region", { name: "读取信息预览" });
      expect(client.modelWriteCalls).toHaveLength(3);
    },
  );

  it("labels empty inputs as examples", async () => {
    renderDownload(new FakeApiClient(ADMIN_ME));
    expect(
      screen.getByPlaceholderText("例如 Qwen/Qwen2.5-7B-Instruct"),
    ).toHaveValue("");
    expect(screen.getByPlaceholderText("例如 main")).toHaveValue("");
    await userEvent.click(screen.getByRole("button", { name: "ModelScope" }));
    expect(screen.getByPlaceholderText("例如 master")).toHaveValue("");
  });
});

// FX-UI-40: reuse the parsed M2_13B resolve/write fixtures and ADMIN_ME.
describe("UI_40 read validation and local-model links", () => {
  it.each([
    ["Hugging Face", "Repository ID"],
    ["ModelScope", "Model ID"],
  ] as const)(
    "validates empty and short %s input on click and Enter",
    async (source, label) => {
      const client = new FakeApiClient(ADMIN_ME);
      const read = vi.spyOn(client, "resolveModel");
      const view = renderDownload(client);
      await userEvent.click(screen.getByRole("button", { name: source }));
      const input = screen.getByLabelText(new RegExp(`^${label}`));
      const button = screen.getByRole("button", { name: "读取模型信息" });
      const message = `请填写 ${label}，格式为「组织/仓库名」，例如 Qwen/Qwen2.5-7B-Instruct。`;
      for (const value of ["", "  ab  "]) {
        for (const submit of ["click", "enter"]) {
          await userEvent.clear(input);
          if (value) await userEvent.type(input, value);
          expect(button).toBeEnabled();
          if (submit === "click") await userEvent.click(button);
          else {
            await userEvent.click(input);
            await userEvent.keyboard("{Enter}");
          }
          expect(screen.getByText(message)).toBeVisible();
          expect(input).toHaveAccessibleDescription(message);
          expect(input).toHaveFocus();
          expect(read).not.toHaveBeenCalled();
          await userEvent.type(input, "x");
          expect(screen.queryByText(message)).not.toBeInTheDocument();
        }
      }
      await userEvent.clear(input);
      await userEvent.click(button);
      await expectAxe(view.container);
    },
  );

  it.each(["register", "download"] as const)(
    "disables reading during %s writes",
    async (action) => {
      const client = new FakeApiClient(ADMIN_ME);
      client.resolveModelResults.push(resolved());
      const read = vi.spyOn(client, "resolveModel");
      const write = vi.spyOn(
        client,
        action === "register" ? "registerModel" : "createDownload",
      );
      write.mockImplementation(() => new Promise<never>(() => {}));
      renderDownload(client);
      await chooseRemote("Hugging Face", "Qwen/Qwen3");
      await userEvent.click(
        screen.getByRole("button", { name: "读取模型信息" }),
      );
      await screen.findByRole("region", { name: "读取信息预览" });
      await userEvent.click(
        screen.getByRole("button", {
          name: action === "register" ? "只加载到模型目录" : "确认下载",
        }),
      );
      expect(write).toHaveBeenCalledTimes(1);
      const button = screen.getByRole("button", { name: "读取模型信息" });
      expect(button).toBeDisabled();
      await userEvent.click(button);
      expect(read).toHaveBeenCalledTimes(1);
    },
  );

  it.each(["返回本地模型", "查看本地模型", "查看下载进度"])(
    "%s opens the real local-models tab",
    async (label) => {
      const client = new FakeApiClient(ADMIN_ME);
      const value = resolved();
      if (value.outcome !== "resolved") throw new Error("fixture");
      client.resolveModelResults.push({
        ...value,
        local_availability:
          label === "查看下载进度" ? "download_active" : "absent",
      });
      client.registrationResults.push(registered());
      client.downloadCreationResults.push(downloadCreated());
      storageFixture(client);
      renderApplication(client, "/models/download");
      await screen.findByRole("heading", { name: "下载模型", level: 1 });
      await chooseRemote("Hugging Face", "Qwen/Qwen3");
      await userEvent.click(
        screen.getByRole("button", { name: "读取模型信息" }),
      );
      await screen.findByRole("region", { name: "读取信息预览" });
      if (label !== "查看下载进度") {
        await userEvent.click(
          screen.getByRole("button", {
            name: label === "返回本地模型" ? "只加载到模型目录" : "确认下载",
          }),
        );
      }
      const link = await screen.findByRole("link", { name: label });
      expect(link).toHaveAttribute("href", "/models?tab=local-models");
      await userEvent.click(link);
      expect(
        await screen.findByRole("tab", { name: "本地模型" }),
      ).toHaveAttribute("aria-selected", "true");
    },
  );
});

describe("FIX_23 TC_RES_11 deployment layout preview", () => {
  it("shows the unsupported warning without registration or download and passes axe", async () => {
    const client = new FakeApiClient(ADMIN_ME);
    client.resolveModelResults.push(
      parseResolveResponse({
        ...resolved(),
        deployable_layout: "unsupported",
      }),
    );
    const { container } = renderDownload(client);
    await chooseRemote("ModelScope", "Qwen/Qwen-Image-2.1");
    await userEvent.click(screen.getByRole("button", { name: "读取模型信息" }));
    await screen.findByText("该模型类型暂不支持");
    expect(
      screen.getByText(
        "平台目前只部署对话类大语言模型，这个模型的文件结构不符合，不能下载或加载。",
      ),
    ).toBeVisible();
    expect(
      screen.queryByRole("button", { name: "确认下载" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "只加载到模型目录" }),
    ).not.toBeInTheDocument();
    expect(client.modelWriteCalls.map(({ path }) => path)).toEqual([
      "/api/v1/models/resolve",
    ]);
    await expectAxe(container);
  });
  it.each(["supported", "unknown"] as const)(
    "keeps actions for %s without recomputing the incomplete file fixture",
    async (layout) => {
      const client = new FakeApiClient(ADMIN_ME);
      client.resolveModelResults.push(
        parseResolveResponse({
          ...resolved(),
          deployable_layout: layout,
        }),
      );
      renderDownload(client);
      await chooseRemote("ModelScope", "Qwen/Qwen2.5-0.5B-Instruct");
      await userEvent.click(
        screen.getByRole("button", { name: "读取模型信息" }),
      );
      expect(
        await screen.findByRole("button", { name: "确认下载" }),
      ).toBeEnabled();
      expect(
        screen.getByRole("button", { name: "只加载到模型目录" }),
      ).toBeEnabled();
    },
  );
  it.each([
    ["artifact_ready", "部署这个模型"],
    ["download_active", "查看下载进度"],
  ] as const)("preserves the %s branch", async (availability, action) => {
    const client = new FakeApiClient(ADMIN_ME);
    client.resolveModelResults.push(
      parseResolveResponse({
        ...resolved(),
        deployable_layout: "unsupported",
        local_availability: availability,
      }),
    );
    renderDownload(client);
    await chooseRemote("ModelScope", "Qwen/Qwen-Image-2.1");
    await userEvent.click(screen.getByRole("button", { name: "读取模型信息" }));
    expect(await screen.findByText(action)).toBeVisible();
    expect(screen.queryByText("该模型类型暂不支持")).not.toBeInTheDocument();
  });
  it("rejects an unknown layout enum loudly", () => {
    expect(() =>
      parseResolveResponse({ ...resolved(), deployable_layout: "future" }),
    ).toThrow(ModelSelectionContractError);
  });
});

// FX-M2-07D: Local Path response shapes from M2-07A / M2-07C contracts.
describe("M2_07D external directory registration", () => {
  const accepted = () =>
    parseResolveResponse({
      outcome: "external_path_accepted",
      control_plane_readable: true,
      runtime_readability: "unverified",
      runtime_readability_cause: "runtime_identity_check_unavailable",
    });
  const rejected = () =>
    unresolved(null, "source_not_found", "check_source_reference");
  async function localInput(client: FakeApiClient) {
    const view = renderDownload(client);
    await userEvent.click(
      screen.getByRole("button", { name: "服务器本地目录" }),
    );
    return { ...view, input: screen.getByLabelText("服务器绝对路径") };
  }
  it("M2_07D rejects relative paths without requests and invalidates acceptance on edit", async () => {
    const client = new FakeApiClient(ADMIN_ME);
    client.resolveModelResults.push(accepted());
    const { input, container } = await localInput(client);
    fireEvent.change(input, { target: { value: "relative" } });
    fireEvent.blur(input);
    expect(await screen.findByText("必须是以 / 开头的绝对路径")).toBeVisible();
    expect(client.modelWriteCalls).toEqual([]);
    fireEvent.change(input, { target: { value: "/srv/models/one" } });
    fireEvent.blur(input);
    expect(await screen.findByText("目录可以注册")).toBeVisible();
    expect(screen.getByRole("button", { name: "注册本地目录" })).toBeEnabled();
    expect(container).not.toHaveTextContent("可读");
    await expectAxe(container);
    fireEvent.change(input, { target: { value: "/srv/models/two" } });
    expect(screen.queryByText("目录可以注册")).toBeNull();
    expect(screen.getByRole("button", { name: "注册本地目录" })).toBeDisabled();
  });
  it.each(["created", "joined_existing"] as const)(
    "M2_07D registers with idempotency and shows %s",
    async (disposition) => {
      const client = new FakeApiClient(ADMIN_ME);
      client.resolveModelResults.push(accepted());
      client.downloadCreationResults.push(
        downloadCreated({
          disposition,
          status: disposition === "created" ? "Verifying" : "Completed",
        }),
      );
      const { input, container } = await localInput(client);
      fireEvent.change(input, { target: { value: "/srv/models/one" } });
      fireEvent.blur(input);
      await screen.findByText("目录可以注册");
      await userEvent.click(
        screen.getByRole("button", { name: "注册本地目录" }),
      );
      expect(
        await screen.findByText(
          disposition === "created"
            ? "已注册为外部目录。"
            : "这个目录已经注册过。",
        ),
      ).toBeVisible();
      expect(
        screen.getByText(
          disposition === "created"
            ? "平台正在校验目录中的文件，校验通过后即可部署。"
            : "没有重复创建，可在「本地模型」里查看它。",
        ),
      ).toBeVisible();
      expect(client.modelWriteCalls).toHaveLength(2);
      expect(client.modelWriteCalls[1]).toMatchObject({
        path: "/api/v1/downloads",
        body: { source: "Local Path", external_path: "/srv/models/one" },
      });
      expect(client.modelWriteCalls[1]?.idempotencyKey).toEqual(
        expect.any(String),
      );
      expect(container).not.toHaveTextContent("可读");
      expect(
        screen.getByRole("link", { name: "查看本地模型" }),
      ).toHaveAttribute("href", "/models?tab=local-models");
    },
  );
  it.each([false, true])(
    "M2_07D merges rejected path hints (at registration: %s)",
    async (atWrite) => {
      const client = new FakeApiClient(ADMIN_ME);
      client.resolveModelResults.push(atWrite ? accepted() : rejected());
      if (atWrite) client.downloadCreationResults.push(rejected());
      const { input } = await localInput(client);
      fireEvent.change(input, { target: { value: "/etc" } });
      fireEvent.blur(input);
      if (atWrite) {
        await screen.findByText("目录可以注册");
        await userEvent.click(
          screen.getByRole("button", { name: "注册本地目录" }),
        );
      }
      expect(
        await screen.findByText("找不到这个目录，或它不在允许注册的范围内"),
      ).toBeVisible();
      expect(
        screen.getByRole("button", { name: "注册本地目录" }),
      ).toBeDisabled();
    },
  );
  it("M2_07D discards a precheck response after the path changes", async () => {
    const client = new FakeApiClient(ADMIN_ME);
    let finish!: (value: ReturnType<typeof accepted>) => void;
    vi.spyOn(client, "resolveModel").mockReturnValue(
      new Promise((resolve) => {
        finish = resolve;
      }),
    );
    const { input } = await localInput(client);
    fireEvent.change(input, { target: { value: "/srv/models/one" } });
    fireEvent.blur(input);
    fireEvent.change(input, { target: { value: "/srv/models/two" } });
    await act(async () => {
      finish(accepted());
      await Promise.resolve();
    });
    expect(screen.queryByText("目录可以注册")).toBeNull();
    expect(screen.getByRole("button", { name: "注册本地目录" })).toBeDisabled();
  });
});

describe("FIX_27 download input trimming", () => {
  // FX-FIX-27: pasted identities based on the task-card examples.
  it.each(["blur", "enter"])(
    "FIX_27 trims local paths on %s before precheck and registration",
    async (trigger) => {
      const client = new FakeApiClient(ADMIN_ME);
      client.resolveModelResults.push(
        parseResolveResponse({
          outcome: "external_path_accepted",
          control_plane_readable: true,
          runtime_readability: "unverified",
          runtime_readability_cause: "runtime_identity_check_unavailable",
        }),
      );
      client.downloadCreationResults.push(downloadCreated());
      renderDownload(client);
      await userEvent.click(
        screen.getByRole("button", { name: "服务器本地目录" }),
      );
      const input = screen.getByLabelText("服务器绝对路径");
      fireEvent.change(input, { target: { value: "  /srv/models/x \t\n" } });
      if (trigger === "blur") fireEvent.blur(input);
      else fireEvent.keyDown(input, { key: "Enter" });
      await screen.findByText("目录可以注册");
      expect(input).toHaveValue("/srv/models/x");
      expect(screen.queryByText("必须是以 / 开头的绝对路径")).toBeNull();
      await userEvent.click(
        screen.getByRole("button", { name: "注册本地目录" }),
      );
      await screen.findByText("已注册为外部目录。");
      expect(client.modelWriteCalls).toHaveLength(2);
      for (const call of client.modelWriteCalls) {
        expect(call.body).toEqual({
          source: "Local Path",
          external_path: "/srv/models/x",
        });
      }
    },
  );

  it.each(["Hugging Face", "ModelScope"] as const)(
    "FIX_27 trims %s on blur without reading and on submit before reading",
    async (source) => {
      const client = new FakeApiClient(ADMIN_ME);
      client.resolveModelResults.push(resolved());
      renderDownload(client);
      await userEvent.click(screen.getByRole("button", { name: source }));
      const input = screen.getByLabelText(
        source === "Hugging Face" ? /^Repository ID/ : /^Model ID/,
      );
      fireEvent.change(input, {
        target: { value: " \tQwen/Qwen2.5-7B-Instruct \n" },
      });
      fireEvent.blur(input);
      expect(input).toHaveValue("Qwen/Qwen2.5-7B-Instruct");
      expect(client.modelWriteCalls).toEqual([]);
      fireEvent.change(input, {
        target: { value: " \tQwen/Qwen2.5-7B-Instruct \n" },
      });
      fireEvent.submit(input.closest("form") as HTMLFormElement);
      await screen.findByRole("region", { name: "读取信息预览" });
      expect(input).toHaveValue("Qwen/Qwen2.5-7B-Instruct");
      expect(client.modelWriteCalls[0]?.body.repository_id).toBe(
        "Qwen/Qwen2.5-7B-Instruct",
      );
    },
  );

  it("FIX_27 preserves internal characters and keeps whitespace-only input invalid", async () => {
    const client = new FakeApiClient(ADMIN_ME);
    client.resolveModelResults.push(resolved());
    renderDownload(client);
    const input = screen.getByLabelText(/^Repository ID/);
    fireEvent.change(input, { target: { value: " \t \n" } });
    fireEvent.submit(input.closest("form") as HTMLFormElement);
    expect(input).toHaveValue("");
    expect(client.modelWriteCalls).toEqual([]);
    fireEvent.change(input, { target: { value: "  Org/model name\tpart  " } });
    fireEvent.submit(input.closest("form") as HTMLFormElement);
    await screen.findByRole("region", { name: "读取信息预览" });
    expect(input).toHaveValue("Org/model name\tpart");
    expect(client.modelWriteCalls[0]?.body.repository_id).toBe(
      "Org/model name\tpart",
    );
  });
});
