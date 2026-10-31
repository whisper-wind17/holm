import axe from "axe-core";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ComponentProps } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ModelInstance } from "../api/model-instance-contracts";
import type { RequestRecord } from "../api/request-record-contracts";
import type { RequestRecordStatus } from "../api/request-record-contracts";
import type { UserOverview } from "../api/user-contracts";
import type { UsageReport } from "../api/usage-contracts";
import {
  ADMIN_ME,
  FakeApiClient,
  TestProviders,
  USER_ME,
  renderApplication,
} from "../app/test-support";
import { RequestRecordsPanel } from "./request-records-panel";

// UI-23: the default window is "today" (local midnight) and presets are anchored
// when picked, so the clock is pinned to assert exact `from` values.
const NOW = new Date(2026, 9, 13, 15, 30, 0);
const TODAY_FROM = new Date(2026, 9, 13).toISOString();
const DAY_MS = 86_400_000;

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(NOW);
});

afterEach(() => {
  vi.useRealTimers();
});

function renderPanel(
  apiClient: FakeApiClient,
  props: Partial<ComponentProps<typeof RequestRecordsPanel>> = {},
) {
  return render(
    <TestProviders apiClient={apiClient}>
      <RequestRecordsPanel apiClient={apiClient} {...props} />
    </TestProviders>,
  );
}

describe("PROTO_12 Request Records UI", () => {
  it("PROTO_12 mounts the admin tab, filters records, and renders missing Token values", async () => {
    const apiClient = new FakeApiClient(ADMIN_ME);
    apiClient.aiServiceResults.push({
      items: [
        {
          description: "fixture",
          enabled: true,
          id: SERVICE_ID,
          primary_instance_id: INSTANCE_ID,
          primary_instance_status: "Running",
          name: "研发助手",
          public_model_name: "qwen-mini",
          route_version: 1,
          runtime_status: "Available",
        },
      ],
    });
    vi.spyOn(apiClient, "getModelInstances").mockResolvedValue({
      items: [instance()],
    });
    apiClient.requestRecordResults.push(
      { items: [requestRecord()], next_cursor: NEXT_CURSOR },
      { items: [requestRecord()], next_cursor: null },
    );
    renderApplication(apiClient, "/services");
    const user = userEvent.setup();

    await user.click(await screen.findByRole("tab", { name: "调用记录" }));
    const table = await screen.findByRole("table", { name: "调用记录" });
    expect(within(table).getByText("已完成")).toBeVisible();
    expect(within(table).getAllByText("—")).toHaveLength(2);
    expect(
      within(table).getByRole("button", { name: "复制 Request ID" }),
    ).toBeVisible();
    expect(apiClient.requestRecordCalls[0]).toEqual({ from: TODAY_FROM });

    await user.selectOptions(screen.getByLabelText("状态"), "completed");
    await waitFor(() => {
      expect(apiClient.requestRecordCalls).toContainEqual({
        from: TODAY_FROM,
        status: "completed",
      });
    });
  });

  it("PROTO_12 shows a retryable error and hides the admin-only tab from users", async () => {
    const adminClient = new FakeApiClient(ADMIN_ME);
    adminClient.requestRecordResults.push(new Error("storage unavailable"));
    adminClient.requestRecordResults.push({
      items: [requestRecord()],
      next_cursor: null,
    });
    const adminView = renderApplication(adminClient, "/services");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("tab", { name: "调用记录" }));
    expect(await screen.findByText("无法获取调用记录")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "重试" }));
    expect(
      await screen.findByRole("table", { name: "调用记录" }),
    ).toBeVisible();
    expect(screen.queryByText("无法获取调用记录")).not.toBeInTheDocument();
    expect(adminClient.requestRecordCalls).toHaveLength(2);
    adminView.unmount();

    const userClient = new FakeApiClient(USER_ME);
    renderApplication(userClient, "/services");
    await screen.findAllByRole("heading", { name: "AI 服务" });
    expect(
      screen.queryByRole("tab", { name: "调用记录" }),
    ).not.toBeInTheDocument();
  });

  it("M5_11 renders and filters all seven ledger states without inventing Unknown values", async () => {
    const apiClient = new FakeApiClient(ADMIN_ME);
    const statuses: RequestRecordStatus[] = [
      "received",
      "in_progress",
      "completed",
      "failed",
      "aborted",
      "rejected",
      "unknown",
    ];
    apiClient.aiServiceResults.push({ items: [] });
    vi.spyOn(apiClient, "getModelInstances").mockResolvedValue({
      items: [instance()],
    });
    apiClient.requestRecordResults.push({
      items: statuses.map((status, index) =>
        requestRecord({
          ...(status === "failed" ? { error_code: "backend_unavailable" } : {}),
          ...(status === "received" ||
          status === "in_progress" ||
          status === "unknown"
            ? {
                completed_at: null,
                duration_ms: null,
                http_status: null,
                input_tokens: null,
                output_tokens: null,
                ttft_ms: null,
              }
            : {}),
          request_id: `0198f58a-2586-7e37-bbf5-${String(index + 32).padStart(12, "0")}`,
          status,
        }),
      ),
      next_cursor: null,
    });
    const view = renderApplication(apiClient, "/services");
    const user = userEvent.setup();

    await user.click(await screen.findByRole("tab", { name: "调用记录" }));
    const table = await screen.findByRole("table", { name: "调用记录" });
    const statusSelect = screen.getByLabelText("状态");
    expect(within(statusSelect).getAllByRole("option")).toHaveLength(8);
    for (const label of [
      "已接收",
      "进行中",
      "已完成",
      "失败",
      "已中止",
      "已拒绝",
      "状态未知",
    ]) {
      expect(within(table).getByText(label)).toBeVisible();
    }
    expect(within(table).getByText("backend_unavailable")).toBeVisible();
    const unknownRow = within(table).getByText("状态未知").closest("tr");
    expect(unknownRow).not.toBeNull();
    // Token, TTFT and duration stay unknown (—), never 0.
    expect(within(unknownRow as HTMLElement).getAllByText("—")).toHaveLength(4);

    await user.selectOptions(statusSelect, "unknown");
    await waitFor(() => {
      expect(apiClient.requestRecordCalls).toContainEqual({
        from: TODAY_FROM,
        status: "unknown",
      });
    });

    const axeResult = await axe.run(view.container, {
      rules: { "color-contrast": { enabled: false } },
    });
    expect(
      axeResult.passes.flatMap((rule) => rule.nodes),
      "axe must scan at least one node and report a passing rule",
    ).not.toHaveLength(0);
    expect(
      axeResult.violations.filter(
        (violation) =>
          violation.impact === "critical" || violation.impact === "serious",
      ),
    ).toEqual([]);
  });

  it("M5_11 shows an empty state instead of a zero-valued record", async () => {
    const apiClient = new FakeApiClient(ADMIN_ME);
    apiClient.aiServiceResults.push({ items: [] });
    apiClient.requestRecordResults.push({ items: [], next_cursor: null });
    renderApplication(apiClient, "/services");
    const user = userEvent.setup();

    await user.click(await screen.findByRole("tab", { name: "调用记录" }));
    const table = await screen.findByRole("table", { name: "调用记录" });
    expect(within(table).getByText("还没有调用记录")).toBeVisible();
    expect(within(table).queryByText(/^0$/)).not.toBeInTheDocument();
  });
});

describe("UI-23 request records filters, columns and detail", () => {
  it("UI-23 defaults to today and each of the six filters applies, alone and combined", async () => {
    const apiClient = new FakeApiClient(ADMIN_ME);
    apiClient.aiServiceResults.push({ items: [] });
    apiClient.userResults.push({ items: [userOverview()] });
    apiClient.usageResults.push(usageReport());
    renderPanel(apiClient);
    const user = userEvent.setup();
    await screen.findByRole("table", { name: "调用记录" });
    await screen.findByRole("option", { name: "laptop-dev" });
    await screen.findByRole("option", { name: "bob" });

    expect(apiClient.requestRecordCalls[0]).toEqual({ from: TODAY_FROM });
    // The API key options come from the usage report grouped by key, scoped to
    // the 30-day detail retention.
    expect(apiClient.usageCalls).toEqual([
      {
        from: new Date(NOW.getTime() - 30 * DAY_MS).toISOString(),
        groupBy: "api_key",
        timeZone: Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC",
        to: NOW.toISOString(),
      },
    ]);
    expect(
      within(screen.getByLabelText("时间"))
        .getAllByRole("option")
        .map((o) => o.textContent),
    ).toEqual(["近 1 小时", "今日", "近 7 天", "近 30 天"]);
    expect(screen.getByLabelText("时间")).toHaveValue("today");

    const last = () => apiClient.requestRecordCalls.at(-1);
    await user.selectOptions(screen.getByLabelText("时间"), "1h");
    await waitFor(() => {
      expect(last()).toEqual({
        from: new Date(NOW.getTime() - 3_600_000).toISOString(),
      });
    });
    await user.selectOptions(screen.getByLabelText("时间"), "30d");
    await waitFor(() => {
      expect(last()).toEqual({
        from: new Date(NOW.getTime() - 30 * DAY_MS).toISOString(),
      });
    });
    await user.selectOptions(screen.getByLabelText("时间"), "7d");
    const sevenDays = new Date(NOW.getTime() - 7 * DAY_MS).toISOString();
    await waitFor(() => {
      expect(last()).toEqual({ from: sevenDays });
    });

    await user.selectOptions(screen.getByLabelText("状态"), "failed");
    await waitFor(() => {
      expect(last()).toEqual({ from: sevenDays, status: "failed" });
    });
    expect(
      within(screen.getByLabelText("错误码")).getAllByRole("option"),
    ).toHaveLength(13);
    await user.selectOptions(
      screen.getByLabelText("错误码"),
      "backend_unavailable",
    );
    await waitFor(() => {
      expect(last()).toEqual({
        errorCode: "backend_unavailable",
        from: sevenDays,
        status: "failed",
      });
    });
    await user.selectOptions(screen.getByLabelText("用户"), USER_ID);
    await user.selectOptions(screen.getByLabelText("API 密钥"), API_KEY_ID);
    await user.type(screen.getByLabelText("Request ID"), REQUEST_ID);
    await waitFor(() => {
      expect(last()).toEqual({
        apiKeyId: API_KEY_ID,
        errorCode: "backend_unavailable",
        from: sevenDays,
        requestId: REQUEST_ID,
        status: "failed",
        userId: USER_ID,
      });
    });
    // A partial Request ID is not sent.
    expect(
      apiClient.requestRecordCalls.filter(
        (call) => call.requestId !== undefined && call.requestId !== REQUEST_ID,
      ),
    ).toEqual([]);

    await user.selectOptions(screen.getByLabelText("状态"), "");
    await waitFor(() => {
      expect(last()).not.toHaveProperty("status");
    });
  });

  it("UI-23 shows key name, prefix and last four, or a dash, and maps the instance name", async () => {
    const apiClient = new FakeApiClient(ADMIN_ME);
    apiClient.aiServiceResults.push({ items: [] });
    apiClient.modelInstanceResults.push({ items: [instance()] });
    apiClient.requestRecordResults.push({
      items: [
        requestRecord({ api_key_name: "laptop-dev" }),
        requestRecord({
          request_id: uuid(40),
          status: "failed",
          error_code: "service_not_authorized",
        }),
        requestRecord({
          api_key_display_prefix: null,
          api_key_id: null,
          api_key_last4: null,
          instance_id: null,
          request_id: uuid(41),
        }),
      ],
      next_cursor: null,
    });
    renderPanel(apiClient);

    const table = await screen.findByRole("table", { name: "调用记录" });
    await within(table).findByText("laptop-dev");
    const rows = within(table).getAllByRole("row").slice(1);
    expect(
      within(rows[0] as HTMLElement).getByText("laptop-dev"),
    ).toBeVisible();
    expect(within(rows[0] as HTMLElement).getByText("研发实例")).toBeVisible();
    expect(
      within(rows[1] as HTMLElement).getByText("holm_sk_…1234"),
    ).toBeVisible();
    // Failed rows carry the error code next to the status.
    const failedStatus = within(rows[1] as HTMLElement).getByText("失败");
    expect(failedStatus.closest("td")).toHaveTextContent(
      "service_not_authorized",
    );
    expect(
      within(rows[2] as HTMLElement).getAllByText("—").length,
    ).toBeGreaterThanOrEqual(2);
    const headers = within(table)
      .getAllByRole("columnheader")
      .map((header) => header.textContent);
    expect(headers).toEqual([
      "请求时间",
      "用户",
      "服务",
      "API 密钥",
      "状态",
      "Input Tokens",
      "Output Tokens",
      "TTFT",
      "端到端耗时",
      "后端实例",
      "Request ID",
    ]);
    expect(headers.join()).not.toContain("Queue Time");
    expect(
      screen.getByText(
        /调用明细保留 30\s*天（管理员可调长），更早的调用只保留在用量统计里/,
      ),
    ).toBeVisible();
  });

  it("UI-23 locked service hides the service filter and column and scopes the key options", async () => {
    const apiClient = new FakeApiClient(ADMIN_ME);
    renderPanel(apiClient, {
      lockedService: { id: SERVICE_ID, label: "研发助手" },
    });
    const table = await screen.findByRole("table", { name: "调用记录" });
    expect(screen.queryByLabelText("服务")).not.toBeInTheDocument();
    expect(
      within(table).queryByRole("columnheader", { name: "服务" }),
    ).not.toBeInTheDocument();
    await waitFor(() => {
      expect(apiClient.requestRecordCalls[0]).toEqual({
        from: TODAY_FROM,
        serviceId: SERVICE_ID,
      });
    });
    expect(apiClient.usageCalls[0]).toMatchObject({ serviceId: SERVICE_ID });
  });

  it("UI-23 an initial error code and window apply, show as 近 7 天, and give way to the time filter", async () => {
    const apiClient = new FakeApiClient(ADMIN_ME);
    const from = new Date(NOW.getTime() - 7 * DAY_MS).toISOString();
    const to = NOW.toISOString();
    renderPanel(apiClient, {
      initialFilters: { errorCode: "backend_unavailable", from, to },
      lockedService: { id: SERVICE_ID, label: "研发助手" },
    });
    await screen.findByRole("table", { name: "调用记录" });
    expect(apiClient.requestRecordCalls[0]).toEqual({
      errorCode: "backend_unavailable",
      from,
      serviceId: SERVICE_ID,
      to,
    });
    expect(screen.getByLabelText("时间")).toHaveValue("7d");
    expect(screen.getByLabelText("错误码")).toHaveValue("backend_unavailable");

    await userEvent
      .setup()
      .selectOptions(screen.getByLabelText("时间"), "today");
    await waitFor(() => {
      expect(apiClient.requestRecordCalls.at(-1)).toEqual({
        errorCode: "backend_unavailable",
        from: TODAY_FROM,
        serviceId: SERVICE_ID,
      });
    });
  });

  it("UI-23 an initial unknown status applies", async () => {
    const apiClient = new FakeApiClient(ADMIN_ME);
    renderPanel(apiClient, {
      initialFilters: { status: "unknown" },
    });
    await screen.findByRole("table", { name: "调用记录" });
    expect(apiClient.requestRecordCalls[0]).toEqual({
      from: TODAY_FROM,
      status: "unknown",
    });
    expect(screen.getByLabelText("状态")).toHaveValue("unknown");
  });

  it("UI-23 the list page adds a service column and filter", async () => {
    const apiClient = new FakeApiClient(ADMIN_ME);
    vi.spyOn(apiClient, "getAiServices").mockResolvedValue({
      items: [
        {
          description: "",
          enabled: true,
          id: SERVICE_ID,
          primary_instance_id: INSTANCE_ID,
          primary_instance_status: "Running",
          name: "研发助手",
          public_model_name: "qwen-mini",
          route_version: 1,
          runtime_status: "Available",
        },
      ],
    });
    apiClient.requestRecordResults.push({
      items: [requestRecord()],
      next_cursor: null,
    });
    renderApplication(apiClient, "/services");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("tab", { name: "调用记录" }));
    const table = await screen.findByRole("table", { name: "调用记录" });
    expect(
      within(table).getByRole("columnheader", { name: "服务" }),
    ).toBeVisible();
    expect(within(table).getByText("qwen-mini")).toBeVisible();
    await screen.findByRole("option", { name: /研发助手/ });
    await user.selectOptions(screen.getByLabelText("服务"), SERVICE_ID);
    await waitFor(() => {
      expect(apiClient.requestRecordCalls.at(-1)).toEqual({
        from: TODAY_FROM,
        serviceId: SERVICE_ID,
      });
    });
  });

  it("UI-23 opens a detail with every field, Queue Time unsupported, and restores focus", async () => {
    const apiClient = new FakeApiClient(ADMIN_ME);
    apiClient.aiServiceResults.push({ items: [] });
    apiClient.modelInstanceResults.push({ items: [instance()] });
    apiClient.requestRecordResults.push({
      items: [
        requestRecord({
          error_code: "inference_timeout",
          finish_reason: "length",
          http_status: 504,
          input_tokens: 12,
          output_tokens: 34,
          status: "failed",
          stream: true,
        }),
      ],
      next_cursor: null,
    });
    const view = renderPanel(apiClient);
    const user = userEvent.setup();
    const open = await screen.findByRole("button", { name: /查看详情/ });

    open.focus();
    await user.keyboard("{Enter}");
    const dialog = await screen.findByRole("dialog", { name: "调用详情" });
    for (const [term, value] of [
      ["Request ID", REQUEST_ID],
      ["HTTP 状态", "504"],
      ["错误码", "inference_timeout"],
      ["用户", "admin"],
      ["后端实例", "研发实例"],
      ["流式", "是"],
      ["Finish Reason", "length"],
      ["Tokens", "Input 12 · Output 34"],
      ["TTFT", "80 ms"],
      ["Queue Time", "当前 Runtime 不支持"],
      ["端到端耗时", "100 ms"],
    ] as const) {
      const row = within(dialog).getByText(term, { selector: "dt" });
      expect(row.nextElementSibling).toHaveTextContent(value);
    }
    expect(within(dialog).getByText("接收时间")).toBeVisible();
    expect(within(dialog).getByText("完成时间")).toBeVisible();
    expect(
      (
        await axe.run(view.baseElement, {
          rules: { "color-contrast": { enabled: false } },
        })
      ).violations.filter(
        ({ impact }) => impact === "critical" || impact === "serious",
      ),
    ).toEqual([]);

    await user.keyboard("{Escape}");
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(open).toHaveFocus();
  });

  it("UI-23 clicking a row opens the detail and an unknown record explains the missing result", async () => {
    const apiClient = new FakeApiClient(ADMIN_ME);
    apiClient.aiServiceResults.push({ items: [] });
    apiClient.requestRecordResults.push({
      items: [
        requestRecord({
          completed_at: null,
          duration_ms: null,
          http_status: null,
          input_tokens: null,
          output_tokens: null,
          status: "unknown",
          ttft_ms: null,
        }),
      ],
      next_cursor: null,
    });
    renderPanel(apiClient);
    const user = userEvent.setup();
    const table = await screen.findByRole("table", { name: "调用记录" });

    // Clicking the copy button must not open the detail.
    await user.click(
      await within(table).findByRole("button", { name: "复制 Request ID" }),
    );
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();

    await user.click(within(table).getByText("admin"));
    const dialog = await screen.findByRole("dialog", { name: "调用详情" });
    expect(
      within(dialog).getByText(
        "平台重启前未收到最终结果。这次调用的用量可能不完整，不计为成功或失败。",
      ),
    ).toBeVisible();
    expect(
      within(dialog).getByText("账本完整性").nextElementSibling,
    ).toHaveTextContent("未知：平台重启前未收到最终结果");
    expect(
      within(dialog).getByText("完成时间").nextElementSibling,
    ).toHaveTextContent("—");
    expect(dialog).not.toHaveTextContent(/Prompt|messages/i);
  });
});

const SERVICE_ID = "0198f58a-2586-7e37-bbf5-000000000012";
const INSTANCE_ID = "0198f58a-2586-7e37-bbf5-000000000013";
const USER_ID = "0198f58a-2586-7e37-bbf5-000000000014";
const API_KEY_ID = "0198f58a-2586-7e37-bbf5-000000000015";
const REQUEST_ID = "0198f58a-2586-7e37-bbf5-000000000016";
const NEXT_CURSOR = "opaque-next-page";

function uuid(n: number) {
  return `0198f58a-2586-7e37-bbf5-${String(n).padStart(12, "0")}`;
}

function requestRecord(overrides: Partial<RequestRecord> = {}): RequestRecord {
  return {
    api_key_display_prefix: "holm_sk_",
    api_key_id: API_KEY_ID,
    api_key_name: null,
    api_key_last4: "1234",
    completed_at: "2026-09-15T12:00:00.100Z",
    duration_ms: 100,
    error_code: null,
    finish_reason: "stop",
    http_status: 200,
    input_tokens: null,
    instance_id: INSTANCE_ID,
    output_tokens: null,
    public_model_name: "qwen-mini",
    received_at: "2026-09-15T12:00:00Z",
    request_id: REQUEST_ID,
    service_id: SERVICE_ID,
    status: "completed",
    stream: false,
    ttft_ms: 80,
    user_id: USER_ID,
    username: "admin",
    ...overrides,
  };
}

function instance(): ModelInstance {
  return {
    artifact_id: uuid(50),
    config_version: 1,
    context_length: 4096,
    error_code: null,
    gpu_memory_utilization: 0.9,
    restart_count: 0,
    gpu_uuids: ["FX-UI-23-GPU"],
    id: INSTANCE_ID,
    internal_endpoint: null,
    internal_model_name: "fx-ui-23",
    name: "研发实例",
    runtime_type: "vllm",
    status: "Running",
    tensor_parallel_size: 1,
    updated_at: "2026-10-13T00:00:00Z",
  };
}

function userOverview(): UserOverview {
  return {
    api_key_count: 0,
    authorized_service_count: 0,
    display_name: "Bob",
    id: USER_ID,
    last_login_at: null,
    must_change_password: false,
    role: "user",
    status: "active",
    username: "bob",
  };
}

function usageReport(): UsageReport {
  const totals = {
    error_rate: null,
    error_requests: 0,
    input_tokens: 0,
    output_tokens: 0,
    partial_token_requests: 0,
    requests: 0,
    total_tokens: 0,
  };
  return {
    bucket_width: "day",
    details: [
      {
        ...totals,
        display_name: "laptop-dev",
        group_id: API_KEY_ID,
        is_deleted: false,
        status: "Active",
      },
    ],
    from: new Date(NOW.getTime() - 30 * DAY_MS).toISOString(),
    summary: totals,
    to: NOW.toISOString(),
    trend: [],
    tz: "UTC",
  };
}
