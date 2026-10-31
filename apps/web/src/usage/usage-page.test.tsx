import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import axe from "axe-core";
import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { AiService } from "../api/ai-service-contracts";
import type { MeResponse } from "../api/contracts";
import type { UserOverview } from "../api/user-contracts";
import type {
  UsageFilters,
  UsageGroup,
  UsageReport,
  UsageTotals,
} from "../api/usage-contracts";
import {
  ADMIN_ME,
  FakeApiClient,
  USER_ME,
  TestProviders,
  renderApplication,
} from "../app/test-support";

import { useSession } from "../auth/session";
import { UsagePanel } from "./usage-page";

it("UI-44 TC-UI-17 keeps filter and date fields top-aligned with a stable clear button and narrow layout", () => {
  const css = readFileSync(
    resolve(dirname(fileURLToPath(import.meta.url)), "usage.css"),
    "utf8",
  );
  expect(css).toMatch(
    /\.pw-usage-filters,\s*\.pw-usage-filters__dates\s*\{[^}]*align-items: start;/,
  );
  expect(css).toMatch(
    /\.pw-usage-filters__clear\s*\{\s*margin-top: calc\(var\(--pw-lh-body\) \+ var\(--pw-space-8\)\);/,
  );
  const narrow = css.slice(css.indexOf("@media (max-width: 767px)"));
  expect(narrow).toMatch(
    /\.pw-usage-filters,\s*\.pw-usage-details header\s*\{\s*align-items: stretch;\s*flex-direction: column;/,
  );
  expect(narrow).toMatch(/\.pw-usage-filters__clear\s*\{\s*margin-top: 0;/);
});

const SERVICE_A_ID = "0198f58a-2586-7e37-bbf5-fefe165e8101";
const SERVICE_B_ID = "0198f58a-2586-7e37-bbf5-fefe165e8102";
const INSTANCE_ID = "0198f58a-2586-7e37-bbf5-fefe165e8201";
const USER_A_ID = "0198f58a-2586-7e37-bbf5-fefe165e8301";
const USER_B_ID = "0198f58a-2586-7e37-bbf5-fefe165e8302";
const KEY_A_ID = "0198f58a-2586-7e37-bbf5-fefe165e8401";
const KEY_B_ID = "0198f58a-2586-7e37-bbf5-fefe165e8402";
const KEY_REVOKED_ID = "0198f58a-2586-7e37-bbf5-fefe165e8403";

const KEY_A = usageGroup(KEY_A_ID, "密钥 A", "Active");
const KEY_B = usageGroup(KEY_B_ID, "密钥 B", "Active");
const KEY_REVOKED = usageGroup(
  KEY_REVOKED_ID,
  "已删除密钥末四位 9f2a",
  "Revoked",
);

describe("PROTO_13 Usage page", () => {
  it("keeps ordinary users on OWN controls with no user filter or grouping", async () => {
    const apiClient = new FakeApiClient(USER_ME);
    renderApplication(apiClient, "/usage");

    expect(await screen.findByRole("heading", { name: "用量" })).toBeVisible();
    expect(screen.queryByLabelText("用户")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "按用户" }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "AI 服务" })).toBeVisible();
    expect(screen.getByRole("combobox", { name: "API 密钥" })).toBeVisible();
    expect(
      await screen.findByRole("heading", { name: "所选范围内没有调用" }),
    ).toBeVisible();
    expect(
      apiClient.usageCalls.every(({ userId }) => userId === undefined),
    ).toBe(true);

    const result = await axe.run(document, {
      rules: { "color-contrast": { enabled: false } },
    });
    expect(result.passes.flatMap((rule) => rule.nodes)).not.toHaveLength(0);
    expect(
      result.violations.filter(
        (violation) =>
          violation.impact === "critical" || violation.impact === "serious",
      ),
    ).toEqual([]);
  });
});

describe("M5_14 TC_UI_17 administrator usage view", () => {
  it("renders the frozen metrics, trends, no update time, key status, and no billing fields", async () => {
    const apiClient = usageClient();
    const { container } = renderApplication(apiClient, "/usage");

    const today = await screen.findByRole("heading", { name: "今日" });
    const thisMonth = screen.getByRole("heading", { name: "本月" });
    for (const heading of [today, thisMonth]) {
      const period = heading.closest("article");
      expect(period).not.toBeNull();
      expect(
        within(period as HTMLElement).getByText("Input Tokens"),
      ).toBeVisible();
      expect(
        within(period as HTMLElement).getByText("Output Tokens"),
      ).toBeVisible();
      expect(
        within(period as HTMLElement).getByText("Total Tokens"),
      ).toBeVisible();
    }

    const rangeSummary = await screen.findByLabelText("所选范围汇总");
    expect(within(rangeSummary).getByText("Error Requests")).toBeVisible();
    expect(within(rangeSummary).getByText("Error Rate")).toBeVisible();
    expect(screen.getByRole("img", { name: "Requests 趋势" })).toBeVisible();
    expect(screen.getByRole("img", { name: "Tokens 趋势" })).toBeVisible();
    expect(screen.queryByText(/数据更新时间：/)).not.toBeInTheDocument();
    expect(
      screen.getByRole("option", {
        name: "已删除密钥末四位 9f2a · — · 已撤销",
      }),
    ).toBeVisible();
    expect(screen.getByRole("button", { name: "按用户" })).toBeVisible();
    expect(container).not.toHaveTextContent(/金额|单价|币种|人民币|美元/);

    const result = await axe.run(container, {
      rules: { "color-contrast": { enabled: false } },
    });
    expect(
      result.violations.filter(
        (violation) =>
          violation.impact === "critical" || violation.impact === "serious",
      ),
    ).toEqual([]);
  });

  it("keeps service-excluded keys disabled and explains why", async () => {
    const apiClient = usageClient();
    renderApplication(apiClient, "/usage");
    const user = userEvent.setup();

    await screen.findByRole("option", { name: "研发助手 · qwen-a" });
    await user.selectOptions(
      await screen.findByRole("combobox", { name: "AI 服务" }),
      SERVICE_A_ID,
    );

    const keySelect = screen.getByRole("combobox", { name: "API 密钥" });
    await waitFor(() => {
      expect(
        within(keySelect).getByRole("option", { name: "密钥 B · — · 生效中" }),
      ).toBeDisabled();
    });
    expect(
      screen.getByText(/密钥 B · — · 生效中：不在所选 AI 服务范围内/),
    ).toBeVisible();
    expect(keySelect).toHaveAccessibleDescription(
      expect.stringContaining("不在所选 AI 服务范围内"),
    );
  });

  it("resets a selected key and keeps user-excluded keys disabled with a reason", async () => {
    const apiClient = usageClient();
    renderApplication(apiClient, "/usage");
    const user = userEvent.setup();
    const keySelect = await screen.findByRole("combobox", {
      name: "API 密钥",
    });

    await within(keySelect).findByRole("option", {
      name: "密钥 A · — · 生效中",
    });
    await user.selectOptions(keySelect, KEY_A_ID);
    expect(keySelect).toHaveValue(KEY_A_ID);
    await user.selectOptions(
      screen.getByRole("combobox", { name: "用户" }),
      USER_B_ID,
    );

    await waitFor(() => {
      expect(keySelect).toHaveValue("");
      expect(
        within(keySelect).getByRole("option", { name: "密钥 A · — · 生效中" }),
      ).toBeDisabled();
    });
    expect(
      screen.getByText(/密钥 A · — · 生效中：不属于所选用户/),
    ).toBeVisible();
    expect(
      screen.getByText(
        "已清除 API 密钥筛选：「密钥 A」在当前服务、用户和时间范围内没有调用",
      ),
    ).toBeVisible();
  });

  it("keeps restricted keys usable when the complete key query fails", async () => {
    const apiClient = usageClient({ completeKeyFailure: true });
    renderApplication(apiClient, "/usage");
    const user = userEvent.setup();

    expect(await screen.findByText(/无法确认完整 API 密钥列表/)).toBeVisible();
    await user.selectOptions(
      screen.getByRole("combobox", { name: "AI 服务" }),
      SERVICE_A_ID,
    );

    const keySelect = screen.getByRole("combobox", { name: "API 密钥" });
    const available = await within(keySelect).findByRole("option", {
      name: "密钥 A · — · 生效中",
    });
    expect(available).toBeEnabled();
    expect(screen.queryByText("0")).not.toBeInTheDocument();
    expect(screen.queryByText("无法获取用量数据")).not.toBeInTheDocument();
  });
});

describe("M5_14 TC_UI_17 usage page states", () => {
  it("shows invalid-date, loading, and retryable report states without fabricating zeroes", async () => {
    const user = userEvent.setup();
    const invalidClient = usageClient();
    const invalidView = renderApplication(invalidClient, "/usage");
    await user.click(await screen.findByRole("button", { name: "自定义" }));
    fireEvent.change(screen.getByLabelText("结束日期"), {
      target: { value: "2026-08-01" },
    });
    expect(await screen.findByText("日期范围无效")).toBeVisible();
    invalidView.unmount();

    const loadingClient = usageClient({ reportPending: true });
    const loadingView = renderApplication(loadingClient, "/usage");
    expect(await screen.findByLabelText("用量正在加载")).toBeVisible();
    expect(screen.queryByText("0")).not.toBeInTheDocument();
    loadingView.unmount();

    const errorClient = usageClient({ reportFailure: true });
    renderApplication(errorClient, "/usage");
    expect(await screen.findByText("无法获取用量数据")).toBeVisible();
    expect(screen.getByRole("button", { name: "重试" })).toBeVisible();
    expect(screen.queryByText("0")).not.toBeInTheDocument();
  });

  it("keeps the filters usable at a narrow viewport and renders Permission honestly", async () => {
    const originalWidth = window.innerWidth;
    Object.defineProperty(window, "innerWidth", {
      configurable: true,
      value: 375,
    });
    window.dispatchEvent(new Event("resize"));
    const narrow = renderApplication(usageClient(), "/usage");
    expect(
      await screen.findByRole("combobox", { name: "API 密钥" }),
    ).toBeVisible();
    expect(screen.getByRole("combobox", { name: "用户" })).toBeVisible();
    narrow.unmount();
    Object.defineProperty(window, "innerWidth", {
      configurable: true,
      value: originalWidth,
    });

    const noUsageNavigation: MeResponse = {
      ...USER_ME,
      navigation: USER_ME.navigation.filter((item) => item.id !== "usage"),
    };
    renderApplication(new FakeApiClient(noUsageNavigation), "/usage");
    expect(
      await screen.findByRole("heading", { name: "无权限" }),
    ).toBeVisible();
    expect(
      screen.queryByRole("heading", { name: "用量" }),
    ).not.toBeInTheDocument();
  });
});

describe("M5_14 TC_SEC_13 ordinary-user information boundary", () => {
  it("requests only OWN usage and does not render infrastructure or other-user details", async () => {
    const apiClient = usageClient({ me: USER_ME });
    const { container } = renderApplication(apiClient, "/usage");

    expect(await screen.findByRole("heading", { name: "用量" })).toBeVisible();
    await waitFor(() => {
      expect(apiClient.usageCalls.length).toBeGreaterThan(0);
    });
    expect(
      apiClient.usageCalls.every(({ userId }) => userId === undefined),
    ).toBe(true);
    expect(screen.queryByLabelText("用户")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "按用户" }),
    ).not.toBeInTheDocument();
    expect(container).not.toHaveTextContent(
      /GPU|模型实例|Runtime|\bTP\b|容器|内部 Endpoint|Runtime Waiting|KV Cache|其他用户|平台阶段时延/,
    );
  });
});

function usageClient({
  completeKeyFailure = false,
  me = ADMIN_ME,
  reportFailure = false,
  reportPending = false,
}: {
  completeKeyFailure?: boolean;
  me?: MeResponse;
  reportFailure?: boolean;
  reportPending?: boolean;
} = {}) {
  const apiClient = new FakeApiClient(me);
  apiClient.aiServiceResults.push({
    items: [service(SERVICE_A_ID, "qwen-a"), service(SERVICE_B_ID, "qwen-b")],
  });
  apiClient.userResults.push({
    items: [usageUser(USER_A_ID, "alice"), usageUser(USER_B_ID, "bob")],
  });
  apiClient.usageSummaryResults.push({
    data_updated_at: "2026-09-22T08:00:00Z",
    this_month: usageTotals({ requests: 28 }),
    today: usageTotals({ requests: 12 }),
    today_active_users: 2,
  });

  vi.spyOn(apiClient, "getUsage").mockImplementation((filters) => {
    apiClient.usageCalls.push(filters);
    if (filters.groupBy === "api_key") {
      if (
        completeKeyFailure &&
        filters.serviceId === undefined &&
        filters.userId === undefined
      ) {
        return Promise.reject(new Error("complete key list unavailable"));
      }
      const details =
        filters.serviceId === SERVICE_A_ID
          ? [KEY_A]
          : filters.userId === USER_B_ID
            ? [KEY_B]
            : [KEY_A, KEY_B, KEY_REVOKED];
      return Promise.resolve(usageReport(filters, details));
    }
    if (reportFailure) return Promise.reject(new Error("usage unavailable"));
    if (reportPending) return new Promise<UsageReport>(() => undefined);
    return Promise.resolve(
      usageReport(filters, [
        usageGroup(
          filters.groupBy === "user" ? USER_A_ID : SERVICE_A_ID,
          filters.groupBy === "user" ? "alice" : "qwen-a",
          null,
        ),
      ]),
    );
  });
  return apiClient;
}

function usageReport(
  filters: UsageFilters,
  details: UsageGroup[],
): UsageReport {
  return {
    bucket_width: "day",
    details,
    from: filters.from,
    summary: usageTotals({ requests: 12 }),
    to: filters.to,
    trend: [
      {
        bucket_start: "2026-09-21T00:00:00Z",
        input_tokens: 180,
        output_tokens: 40,
        requests: 5,
        total_tokens: 220,
      },
      {
        bucket_start: "2026-09-22T00:00:00Z",
        input_tokens: 240,
        output_tokens: 60,
        requests: 7,
        total_tokens: 300,
      },
    ],
    tz: filters.timeZone,
  };
}

function usageTotals(overrides: Partial<UsageTotals> = {}): UsageTotals {
  return {
    error_rate: 0.083,
    error_requests: 1,
    input_tokens: 420,
    output_tokens: 100,
    partial_token_requests: 0,
    requests: 12,
    total_tokens: 520,
    ...overrides,
  };
}

function usageGroup(
  groupId: string,
  displayName: string,
  status: UsageGroup["status"],
): UsageGroup {
  return {
    ...usageTotals(),
    display_name: displayName,
    group_id: groupId,
    is_deleted: false,
    status,
  };
}

function service(id: string, publicModelName: string): AiService {
  return {
    description: "fixture",
    enabled: true,
    id,
    primary_instance_id: INSTANCE_ID,
    primary_instance_status: "Running",
    name: "研发助手",
    public_model_name: publicModelName,
    route_version: 1,
    runtime_status: "Available",
  };
}

function usageUser(id: string, username: string): UserOverview {
  return {
    api_key_count: 1,
    authorized_service_count: 1,
    display_name: username,
    id,
    last_login_at: null,
    must_change_password: false,
    role: "user",
    status: "active",
    username,
  };
}

it("M4-03A TC-UI-17 keeps removed keys selectable with their original state and retained usage", async () => {
  const apiClient = usageClient();
  const removed = { ...KEY_REVOKED, is_deleted: true };
  vi.spyOn(apiClient, "getUsage").mockImplementation((filters) => {
    apiClient.usageCalls.push(filters);
    return Promise.resolve(usageReport(filters, [removed]));
  });
  renderApplication(apiClient, "/usage");
  const user = userEvent.setup();
  const label = `${removed.display_name} · — · 已撤销 · 已从密钥列表移除`;
  const option = await screen.findByRole("option", { name: label });
  expect(option).toBeEnabled();
  await user.selectOptions(
    screen.getByRole("combobox", { name: "API 密钥" }),
    removed.group_id as string,
  );
  await waitFor(() => {
    expect(
      apiClient.usageCalls.some((call) => call.apiKeyId === removed.group_id),
    ).toBe(true);
  });
  await user.click(screen.getByRole("button", { name: "按密钥" }));
  expect(screen.getByText("已从密钥列表移除")).toBeVisible();
  expect(screen.getByText("已撤销")).toBeVisible();
  expect(screen.getByLabelText("所选范围汇总")).toHaveTextContent("Requests");
});

// FX-UI-43: synthetic usage groups based on UI-42 fields and PDD 9.8;
// no request bodies, credentials or real account data.
describe("UI-43 TC-UI-17 usage revision", () => {
  it("keeps a selected key when service, user and range still include it", async () => {
    const api = usageClient();
    renderApplication(api, "/usage");
    const user = userEvent.setup();
    const keys = await screen.findByRole("combobox", { name: "API 密钥" });
    await within(keys).findByRole("option", { name: "密钥 A · — · 生效中" });
    await user.selectOptions(keys, KEY_A_ID);
    await user.selectOptions(
      screen.getByRole("combobox", { name: "AI 服务" }),
      SERVICE_A_ID,
    );
    await user.selectOptions(
      screen.getByRole("combobox", { name: "用户" }),
      USER_A_ID,
    );
    await user.click(screen.getByRole("button", { name: "近 30 天" }));
    await waitFor(() => {
      expect(
        api.usageCalls
          .filter(
            (call) => call.groupBy === "service" && call.apiKeyId === KEY_A_ID,
          )
          .at(-1),
      ).toMatchObject({ serviceId: SERVICE_A_ID, userId: USER_A_ID });
    });
    expect(keys).toHaveValue(KEY_A_ID);
    expect(screen.queryByText(/已清除 API 密钥筛选/)).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "清除筛选" }));
    expect(keys).toHaveValue("");
    expect(screen.getByRole("combobox", { name: "AI 服务" })).toHaveValue("");
    expect(screen.getByRole("combobox", { name: "用户" })).toHaveValue("");
    expect(
      screen.queryByRole("button", { name: "清除筛选" }),
    ).not.toBeInTheDocument();
  });

  it("waits for the new scope, clears only on success and dismisses the notice on key selection", async () => {
    const api = usageClient();
    let resolveScope!: (value: UsageReport) => void;
    let pendingFilters!: UsageFilters;
    vi.spyOn(api, "getUsage").mockImplementation((filters) => {
      api.usageCalls.push(filters);
      if (filters.groupBy === "api_key" && filters.serviceId === SERVICE_B_ID) {
        pendingFilters = filters;
        return new Promise((resolve) => {
          resolveScope = resolve;
        });
      }
      return Promise.resolve(
        usageReport(
          filters,
          filters.groupBy === "api_key"
            ? [KEY_A, KEY_B]
            : [usageGroup(SERVICE_A_ID, "qwen-a", null)],
        ),
      );
    });
    renderApplication(api, "/usage");
    const user = userEvent.setup();
    const keys = await screen.findByRole("combobox", { name: "API 密钥" });
    await within(keys).findByRole("option", { name: "密钥 A · — · 生效中" });
    await user.selectOptions(keys, KEY_A_ID);
    await user.selectOptions(
      screen.getByRole("combobox", { name: "AI 服务" }),
      SERVICE_B_ID,
    );
    expect(screen.queryByText(/已清除 API 密钥筛选/)).not.toBeInTheDocument();
    resolveScope(usageReport(pendingFilters, [KEY_B]));
    expect(
      await screen.findByText(
        "已清除 API 密钥筛选：「密钥 A」在当前服务、用户和时间范围内没有调用",
      ),
    ).toBeVisible();
    expect(keys).toHaveValue("");
    await user.selectOptions(keys, KEY_B_ID);
    expect(screen.queryByText(/已清除 API 密钥筛选/)).not.toBeInTheDocument();
  });

  it("clears a key when the time range excludes it", async () => {
    const api = usageClient();
    vi.spyOn(api, "getUsage").mockImplementation((filters) =>
      Promise.resolve(
        usageReport(
          filters,
          filters.groupBy === "api_key" &&
            Date.parse(filters.to) - Date.parse(filters.from) < 86400000
            ? []
            : [KEY_A],
        ),
      ),
    );
    renderApplication(api, "/usage");
    const user = userEvent.setup();
    await screen.findByRole("option", { name: "密钥 A · — · 生效中" });
    await user.selectOptions(
      await screen.findByRole("combobox", { name: "API 密钥" }),
      KEY_A_ID,
    );
    await user.click(screen.getByRole("button", { name: "今日" }));
    expect(
      await screen.findByText(/已清除 API 密钥筛选：「密钥 A」/),
    ).toBeVisible();
  });

  it("initializes admin URL filters and ignores user_id for ordinary users", async () => {
    const api = usageClient();
    const view = renderApplication(
      api,
      `/usage?service_id=${SERVICE_A_ID}&user_id=${USER_B_ID}`,
    );
    await screen.findByRole("option", { name: "bob" });
    expect(screen.getByRole("combobox", { name: "用户" })).toHaveValue(
      USER_B_ID,
    );
    expect(screen.getByRole("combobox", { name: "AI 服务" })).toHaveValue(
      SERVICE_A_ID,
    );
    await waitFor(() => {
      expect(
        api.usageCalls.some(
          (call) =>
            call.serviceId === SERVICE_A_ID && call.userId === USER_B_ID,
        ),
      ).toBe(true);
    });
    view.unmount();
    const personal = usageClient({ me: USER_ME });
    renderApplication(
      personal,
      `/usage?service_id=${SERVICE_A_ID}&user_id=${USER_B_ID}`,
    );
    await screen.findByRole("option", { name: "qwen-a" });
    expect(screen.getByRole("combobox", { name: "AI 服务" })).toHaveValue(
      SERVICE_A_ID,
    );
    expect(
      screen.queryByRole("combobox", { name: "用户" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "按用户" }),
    ).not.toBeInTheDocument();
    expect(personal.usageCalls.every((call) => call.userId === undefined)).toBe(
      true,
    );
  });

  it("shows deleted metadata, full sorted details, one-decimal shares, compact summaries and partial counts", async () => {
    const api = usageClient();
    api.userResults.splice(0, 1, {
      items: [{ ...usageUser(USER_A_ID, "alice"), status: "deleted" }],
    });
    const deleted = {
      ...usageGroup(SERVICE_B_ID, "历史服务", null),
      is_deleted: true,
      total_tokens: 1500000,
    };
    const namedKey = {
      ...KEY_A,
      api_key_name: "生产调用",
      owner_username: "alice",
      owner_is_deleted: true,
    };
    api.aiServiceResults.splice(0, 1, {
      items: [service(SERVICE_A_ID, "qwen-a")],
    });
    vi.spyOn(api, "getUsage").mockImplementation((filters) =>
      Promise.resolve({
        ...usageReport(
          filters,
          filters.groupBy === "api_key"
            ? [namedKey]
            : [usageGroup(SERVICE_A_ID, "qwen-a", null), deleted],
        ),
        summary: usageTotals({
          total_tokens: 2000000,
          partial_token_requests: 2,
        }),
      }),
    );
    renderApplication(api, "/usage");
    expect(
      await screen.findByRole("option", { name: "历史服务（已删除）" }),
    ).toBeEnabled();
    expect(
      screen.getByRole("option", { name: "alice（已删除）" }),
    ).toBeVisible();
    expect(
      screen.getByRole("option", {
        name: "生产调用 · alice（已删除） · 生效中",
      }),
    ).toBeVisible();
    expect(
      screen.getByText(
        "其中 2 次调用异常中断，Token 按 Gateway 已确认的实际数量计入",
      ),
    ).toBeVisible();
    const summary = screen.getByLabelText("所选范围汇总");
    expect(within(summary).getByText("2M")).toHaveAttribute(
      "title",
      "2,000,000",
    );
    expect(within(summary).getByText("2M")).toHaveAccessibleName("2,000,000");
    const table = screen.getByRole("table", { name: "用量明细" });
    const rows = within(table).getAllByRole("row");
    expect(rows[1]).toHaveTextContent("历史服务");
    expect(rows[1]).toHaveTextContent("1,500,000");
    expect(rows[1]).toHaveTextContent("75.0%");
    expect(rows[1]).toHaveTextContent("已删除");
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "按密钥" }));
    expect(
      await screen.findByRole("columnheader", { name: "所属用户" }),
    ).toBeVisible();
    expect(within(table).getByText("alice（已删除）")).toBeVisible();
    expect(
      within(table).getByRole("button", { name: "生产调用" }),
    ).toBeVisible();
  });

  it.each([true, false])(
    "drills through dimensions using keyboard-capable names (admin=%s)",
    async (admin) => {
      const api = usageClient({ me: admin ? ADMIN_ME : USER_ME });
      renderApplication(api, "/usage");
      const user = userEvent.setup();
      const name = await screen.findByRole("button", { name: "qwen-a" });
      name.focus();
      await user.keyboard("{Enter}");
      expect(screen.getByRole("combobox", { name: "AI 服务" })).toHaveValue(
        SERVICE_A_ID,
      );
      if (admin) {
        expect(screen.getByRole("button", { name: "按用户" })).toHaveAttribute(
          "aria-pressed",
          "true",
        );
        await user.click(await screen.findByRole("button", { name: "alice" }));
        expect(screen.getByRole("combobox", { name: "用户" })).toHaveValue(
          USER_A_ID,
        );
      }
      expect(screen.getByRole("button", { name: "按密钥" })).toHaveAttribute(
        "aria-pressed",
        "true",
      );
      await user.click(await screen.findByRole("button", { name: "密钥 A" }));
      expect(screen.getByRole("combobox", { name: "API 密钥" })).toHaveValue(
        KEY_A_ID,
      );
      expect(screen.getByRole("button", { name: "按服务" })).toHaveAttribute(
        "aria-pressed",
        "true",
      );
    },
  );

  it("shows empty period dashes and no stale timestamp or heading description", async () => {
    renderApplication(new FakeApiClient(USER_ME), "/usage");
    const heading = await screen.findByRole("heading", { name: "今日" });
    await screen.findByRole("heading", { name: "所选范围内没有调用" });
    const card = within(heading.closest("article") as HTMLElement);
    expect(card.getByText("暂无调用")).toBeVisible();
    expect(card.getAllByText("—")).toHaveLength(4);
    expect(screen.getAllByText("暂无调用")).toHaveLength(2);
    expect(screen.queryByText(/数据更新时间/)).not.toBeInTheDocument();
    expect(screen.queryByText(/查看请求量与 Token/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "近 7 天" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.getByText(/· 按天/)).toBeVisible();
  });

  it("explains all custom-date errors and accepts inclusive same-day ranges", async () => {
    renderApplication(usageClient(), "/usage");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "自定义" }));
    const from = screen.getByLabelText("开始日期");
    const to = screen.getByLabelText("结束日期");
    const change = (element: HTMLElement, value: string) =>
      fireEvent.change(element, { target: { value } });
    change(from, "2026-01-10");
    change(to, "2026-01-09");
    expect(
      screen.getByText("结束日期早于开始日期，请重新选择。"),
    ).toBeVisible();
    expect(screen.queryByLabelText("所选范围汇总")).not.toBeInTheDocument();
    change(from, "2026-01-01");
    change(to, "2026-02-01");
    expect(screen.getByText("日期范围超过 31 天，请缩短范围。")).toBeVisible();
    change(from, "2099-01-01");
    change(to, "2099-01-02");
    expect(screen.getByText("日期不能晚于今天，请重新选择。")).toBeVisible();
    change(from, "");
    expect(screen.getByText("请选择开始日期和结束日期。")).toBeVisible();
    change(from, "2026-01-01");
    change(to, "2026-01-01");
    expect(await screen.findByLabelText("所选范围汇总")).toBeVisible();
    expect(screen.getByText("01-01 至 01-01 · 按小时")).toBeVisible();
    expect(screen.queryByText("日期范围无效")).not.toBeInTheDocument();
  });
});

function LockedPanel({ api }: { api: FakeApiClient }) {
  const session = useSession();
  return session.me ? (
    <UsagePanel
      apiClient={api}
      lockedService={{ id: SERVICE_A_ID, label: "qwen-a" }}
    />
  ) : null;
}

it("UI-43 TC-UI-17 keeps the locked service through drills and clearing filters", async () => {
  const api = usageClient();
  render(
    <TestProviders apiClient={api}>
      <LockedPanel api={api} />
    </TestProviders>,
  );
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "alice" }));
  expect(
    screen.queryByRole("combobox", { name: "AI 服务" }),
  ).not.toBeInTheDocument();
  expect(
    screen.queryByRole("heading", { name: "今日" }),
  ).not.toBeInTheDocument();
  expect(
    screen.queryByRole("button", { name: "按服务" }),
  ).not.toBeInTheDocument();
  await user.click(await screen.findByRole("button", { name: "密钥 A" }));
  expect(screen.getByRole("button", { name: "按用户" })).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  await user.click(screen.getByRole("button", { name: "清除筛选" }));
  await waitFor(() => {
    expect(api.usageCalls.at(-1)).toMatchObject({ serviceId: SERVICE_A_ID });
  });
  expect(api.usageSummaryCalls).toHaveLength(0);
  expect(screen.getByText("qwen-a")).toBeVisible();
});
