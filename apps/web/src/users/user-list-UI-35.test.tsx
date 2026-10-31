import axe from "axe-core";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import type { AiService } from "../api/ai-service-contracts";
import { ApiError } from "../api/client";
import type { MeResponse } from "../api/contracts";
import type { UserOverview } from "../api/user-contracts";
import type { UsageGroup, UsageReport } from "../api/usage-contracts";
import {
  ADMIN_ME,
  FakeApiClient,
  renderApplication,
} from "../app/test-support";

// FX-UI-35: synthetic users and usage groups derived from the UI-35 status matrix.
const ME: MeResponse = {
  ...ADMIN_ME,
  capabilities: [
    ...ADMIN_ME.capabilities,
    { capability: "view_usage", scope: "all" },
  ],
};
const SERVICE_ID = id(90);

function id(seed: number) {
  return `0198f58a-2586-7e37-bbf5-${seed.toString().padStart(12, "0")}`;
}

function person(
  seed: number,
  username: string,
  patch: Partial<UserOverview> = {},
): UserOverview {
  return {
    api_key_count: 0,
    authorized_service_count: 1,
    display_name: username.toUpperCase(),
    id: id(seed),
    last_login_at: null,
    must_change_password: false,
    role: "user",
    status: "active",
    username,
    ...patch,
  };
}

const self = person(0, "admin", {
  api_key_count: 1,
  display_name: "平台管理员",
  id: ADMIN_ME.user.id,
  role: "admin",
});
const zhangwei = person(1, "zhangwei", { api_key_count: 2 });
const lina = person(2, "lina", { api_key_count: 2, status: "disabled" });
const wangqiang = person(3, "wangqiang", { status: "deleted" });
const live = [lina, zhangwei, self];

function group(userId: string, total: number | null): UsageGroup {
  return {
    display_name: "u",
    error_rate: null,
    error_requests: 0,
    group_id: userId,
    input_tokens: null,
    is_deleted: false,
    output_tokens: null,
    partial_token_requests: 0,
    requests: 1,
    status: null,
    total_tokens: total,
  };
}

function report(details: UsageGroup[]): UsageReport {
  const totals = {
    error_rate: null,
    error_requests: 0,
    input_tokens: null,
    output_tokens: null,
    partial_token_requests: 0,
    requests: 0,
    total_tokens: null,
  };
  return {
    bucket_width: "day",
    details,
    from: "2026-10-14T00:00:00Z",
    summary: totals,
    to: "2026-10-14T01:00:00Z",
    trend: [],
    tz: "UTC",
  };
}

function service(): AiService {
  return {
    description: "",
    enabled: true,
    id: SERVICE_ID,
    name: "研发助手",
    primary_instance_id: id(91),
    primary_instance_status: "Running",
    public_model_name: "qwen-mini",
    route_version: 1,
    runtime_status: "Available",
  };
}

function rowOf(username: string) {
  const row = screen.getByText(new RegExp(`^@${username}`)).closest("tr");
  if (row === null) throw new Error(`row ${username} is missing`);
  return within(row);
}

async function renderList(
  users: UserOverview[][],
  usage: UsageReport = report([]),
) {
  const apiClient = new FakeApiClient(ME);
  for (const items of users) apiClient.userResults.push({ items });
  apiClient.usageResults.push(usage);
  apiClient.aiServiceResults.push({ items: [service()] });
  renderApplication(apiClient, "/users");
  await screen.findByText("@zhangwei");
  return apiClient;
}

describe("UI-35 user list", () => {
  it("UI-35 shows 8 columns, status counts, sorting and the deleted toggle", async () => {
    const user = userEvent.setup();
    await renderList([live, [...live, wangqiang]]);

    expect(
      screen.getAllByRole("columnheader").map((header) => header.textContent),
    ).toEqual([
      "用户",
      "角色",
      "服务数",
      "API Keys 数",
      "今日 Tokens",
      "最近登录",
      "状态",
      "操作",
    ]);
    // 正常 → 已禁用, then user name.
    expect(
      screen
        .getAllByRole("link", { name: /^(ZHANGWEI|LINA|平台管理员)$/ })
        .map((link) => link.textContent),
    ).toEqual(["平台管理员", "ZHANGWEI", "LINA"]);
    expect(screen.getByRole("button", { name: "全部 3" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.getByRole("button", { name: "正常 2" })).toBeVisible();
    expect(screen.getByRole("button", { name: "已禁用 1" })).toBeVisible();
    expect(
      screen.queryByRole("button", { name: /^已删除/ }),
    ).not.toBeInTheDocument();
    expect(screen.getByText("@admin · 当前登录")).toBeVisible();
    expect(screen.getByRole("link", { name: "ZHANGWEI" })).toHaveAttribute(
      "href",
      `/users/${zhangwei.id}`,
    );

    await user.click(screen.getByLabelText("包含已删除"));
    await user.click(await screen.findByRole("button", { name: "已删除 1" }));
    expect(screen.getByText("@wangqiang")).toBeVisible();
    expect(screen.queryByText("@zhangwei")).not.toBeInTheDocument();
    expect(rowOf("wangqiang").getByText("已删除，不可恢复")).toBeVisible();
    expect(rowOf("wangqiang").queryByRole("button")).not.toBeInTheDocument();
    expect(rowOf("wangqiang").getAllByRole("cell")[3]).toHaveAttribute(
      "title",
      "删除时已全部撤销",
    );
  });

  it("UI-35 shows API Keys and today Tokens per the rules", async () => {
    const apiClient = await renderList(
      [live],
      report([group(zhangwei.id, 1_284_530), group(self.id, null)]),
    );

    const keys = (name: string) => rowOf(name).getAllByRole("cell")[3];
    const tokens = (name: string) => rowOf(name).getAllByRole("cell")[4];
    expect(keys("zhangwei")).toHaveTextContent("2");
    expect(keys("zhangwei")).toHaveAttribute("title", "已过期、已撤销的不计入");
    expect(keys("lina")).toHaveTextContent("0");
    expect(keys("lina")).toHaveAttribute(
      "title",
      "用户已禁用：2 个 API Key 暂停使用，不计入；启用后恢复",
    );
    expect(tokens("zhangwei")).toHaveTextContent("1.3M");
    expect(tokens("zhangwei")).toHaveAttribute("title", "1,284,530 Tokens");
    expect(tokens("lina")).toHaveTextContent("0");
    expect(tokens("admin")).toHaveTextContent("—");
    expect(apiClient.usageCalls[0]?.groupBy).toBe("user");
  });

  it("UI-35 shows a dash for today Tokens when the usage query fails", async () => {
    const apiClient = new FakeApiClient(ME);
    apiClient.userResults.push({ items: live });
    apiClient.usageResults.push(new ApiError({ code: "boom", status: 500 }));
    renderApplication(apiClient, "/users");
    await screen.findByText("@zhangwei");
    await waitFor(() => {
      expect(apiClient.usageCalls).toHaveLength(1);
    });
    expect(rowOf("zhangwei").getAllByRole("cell")[4]).toHaveTextContent("—");
  });

  it("UI-35 filters by AI service and clears the filters", async () => {
    const user = userEvent.setup();
    const apiClient = await renderList([live]);
    apiClient.servicePermissionReadResults.push({
      user_ids: [zhangwei.id],
      limits: Object.fromEntries(
        [zhangwei.id].map((id) => [
          id,
          { max_concurrent_requests: null, max_requests_per_minute: null },
        ]),
      ),
    });

    await screen.findByRole("option", { name: "研发助手" });
    await user.selectOptions(
      screen.getByRole("combobox", { name: "AI 服务" }),
      SERVICE_ID,
    );
    await waitFor(() => {
      expect(screen.queryByText("@lina")).not.toBeInTheDocument();
    });
    expect(apiClient.servicePermissionReadCalls).toEqual([SERVICE_ID]);
    expect(screen.getByText("@zhangwei")).toBeVisible();
    expect(screen.getByRole("button", { name: "全部 1" })).toBeVisible();
    expect(screen.getByRole("button", { name: "已禁用 0" })).toBeVisible();

    await user.click(screen.getByRole("button", { name: "已禁用 0" }));
    expect(screen.getByText("没有符合筛选条件的用户。")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "清除筛选" }));
    expect(await screen.findByText("@zhangwei")).toBeVisible();
    expect(screen.getByRole("button", { name: "全部 3" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.getByRole("combobox", { name: "AI 服务" })).toHaveValue("");
    expect(await screen.findByText("@lina")).toBeVisible();
  });

  it("UI-35 offers row actions by status and locks them for the signed-in user", async () => {
    await renderList([[...live, wangqiang]]);
    const names = (username: string) =>
      rowOf(username)
        .getAllByRole("button")
        .map((button) => button.textContent);

    expect(names("zhangwei")).toEqual(["禁用", "重置密码", "删除"]);
    expect(names("lina")).toEqual(["启用", "重置密码", "删除"]);
    expect(names("admin")).toEqual(["禁用", "删除"]);
    for (const button of rowOf("admin").getAllByRole("button")) {
      expect(button).toBeDisabled();
      expect(button).toHaveAttribute(
        "title",
        "不能禁用或删除当前登录的自己；需要时请另一位管理员操作",
      );
    }
    expect(
      screen.queryByRole("button", { name: "查看用户" }),
    ).not.toBeInTheDocument();
  });

  it("UI-35 disables a user from the row and reports the result", async () => {
    const user = userEvent.setup();
    const apiClient = await renderList([live, live]);
    apiClient.userCommandResults.push({ ...zhangwei, status: "disabled" });

    await user.click(within(rowOfElement("zhangwei")).getByText("禁用"));
    const dialog = await screen.findByRole("dialog", {
      name: "禁用用户「zhangwei」？",
    });
    expect(
      within(dialog).getByText(
        "该用户的 2 个生效中 API 密钥暂停使用（不改变密钥本身的状态，也不删除）。",
      ),
    ).toBeVisible();
    expect(await axe.run(dialog)).toHaveProperty("violations", []);
    await user.click(within(dialog).getByRole("button", { name: "禁用用户" }));

    expect(await screen.findByRole("status")).toHaveTextContent(
      "已禁用 zhangwei",
    );
    expect(apiClient.userCommandCalls).toEqual([
      { command: "disable", id: zhangwei.id },
    ]);
  });

  it("UI-35 deletes without typing the username and points to the deleted filter", async () => {
    const user = userEvent.setup();
    const apiClient = await renderList([live, [self, lina]]);
    apiClient.userCommandResults.push({ ...zhangwei, status: "deleted" });

    await user.click(within(rowOfElement("zhangwei")).getByText("删除"));
    const dialog = await screen.findByRole("dialog", {
      name: "删除用户「zhangwei」？",
    });
    expect(
      within(dialog).getByText(
        "立即永久撤销尚未撤销的 API 密钥，移除全部服务授权。",
      ),
    ).toBeVisible();
    expect(within(dialog).queryByRole("textbox")).not.toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "删除用户" }));

    expect(
      await screen.findByText("已删除 zhangwei；勾选「包含已删除」可查看"),
    ).toBeVisible();
    await waitFor(() => {
      expect(screen.queryByText("@zhangwei")).not.toBeInTheDocument();
    });
  });

  it("UI-35 puts a failed action under its own row only", async () => {
    const user = userEvent.setup();
    const apiClient = await renderList([live]);
    apiClient.userCommandResults.push(
      new ApiError({
        code: "unavailable",
        responseMessage: "控制面暂时不可用。",
        status: 503,
      }),
    );

    await user.click(within(rowOfElement("zhangwei")).getByText("禁用"));
    await user.click(
      within(await screen.findByRole("dialog")).getByRole("button", {
        name: "禁用用户",
      }),
    );

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("操作未完成");
    expect(alert).toHaveTextContent("控制面暂时不可用。");
    expect(screen.getAllByRole("alert")).toHaveLength(1);
    expect(alert.closest("tr")?.previousElementSibling).toBe(
      rowOfElement("zhangwei"),
    );
    expect(apiClient.userCommandCalls).toHaveLength(1);
  });

  it("UI-35 resets the password of a disabled user and shows the secret once", async () => {
    const user = userEvent.setup();
    const apiClient = new FakeApiClient(ME);
    apiClient.userResults.push({ items: live }, { items: live });
    const { queryClient } = renderApplication(apiClient, "/users");
    await screen.findByText("@zhangwei");
    apiClient.userResetResults.push({
      must_change_password: true,
      temporary_password: "Temp-Secret-77",
    });

    await user.click(within(rowOfElement("lina")).getByText("重置密码"));
    const dialog = await screen.findByRole("dialog", {
      name: "重置「lina」的密码？",
    });
    expect(
      within(dialog).getByText("该用户目前已禁用：启用之前仍不能登录。"),
    ).toBeVisible();
    await user.click(within(dialog).getByRole("button", { name: "重置密码" }));

    const secret = await screen.findByRole("dialog", { name: "保存临时密码" });
    expect(within(secret).getByDisplayValue("Temp-Secret-77")).toBeVisible();
    expect(await axe.run(secret)).toHaveProperty("violations", []);
    expect(
      JSON.stringify(
        queryClient
          .getQueryCache()
          .getAll()
          .map(({ state }) => state.data),
      ),
    ).not.toContain("Temp-Secret-77");
    expect(queryClient.getMutationCache().getAll()).toHaveLength(0);
    await user.click(
      within(secret).getByRole("button", { name: "我已保存临时密码" }),
    );
    await waitFor(() => {
      expect(screen.queryByDisplayValue("Temp-Secret-77")).toBeNull();
    });
    expect(await screen.findByRole("status")).toHaveTextContent(
      "已重置 lina 的密码",
    );
    expect(document.body).not.toHaveTextContent("Temp-Secret-77");
  });

  it.each([
    [
      true,
      "用户名「Lina」属于一个已删除的用户，不能复用（不区分大小写）。勾选列表上方「包含已删除」可以查看该用户。",
    ],
    [false, "用户名「Lina」已被使用（不区分大小写）。"],
  ])(
    "UI-35 explains a username conflict (deleted owner: %s)",
    async (deleted, message) => {
      const user = userEvent.setup();
      const apiClient = await renderList([live]);
      apiClient.userCreateResults.push(
        new ApiError({ code: "username_conflict", status: 409 }),
      );
      apiClient.userResults.push({
        items: [deleted ? { ...lina, status: "deleted" } : lina],
      });

      await user.click(
        screen
          .getAllByRole("button", { name: "创建用户" })
          .at(0) as HTMLElement,
      );
      const dialog = await screen.findByRole("dialog", { name: "创建用户" });
      await user.type(within(dialog).getByLabelText(/^用户名/), "Lina");
      await user.click(
        within(dialog).getByRole("button", { name: "创建用户" }),
      );

      expect(await within(dialog).findByText(message)).toBeVisible();
      expect(apiClient.userListCalls.at(-1)).toEqual({
        includeDeleted: true,
        search: "Lina",
      });
    },
  );

  it("UI-35 combines search, role and service filters before counting statuses", async () => {
    const user = userEvent.setup();
    const apiClient = await renderList([live]);
    apiClient.userResults.push({ items: [zhangwei] });
    await user.type(screen.getByRole("searchbox", { name: "搜索" }), "z");
    await screen.findByRole("button", { name: "全部 1" });
    apiClient.userResults.push({ items: [zhangwei] });
    await user.selectOptions(
      screen.getByRole("combobox", { name: "角色" }),
      "user",
    );
    await waitFor(() => {
      expect(apiClient.userListCalls.at(-1)).toEqual({
        includeDeleted: false,
        role: "user",
        search: "z",
      });
    });
    apiClient.servicePermissionReadResults.push({
      user_ids: [zhangwei.id],
      limits: Object.fromEntries(
        [zhangwei.id].map((id) => [
          id,
          { max_concurrent_requests: null, max_requests_per_minute: null },
        ]),
      ),
    });
    await user.selectOptions(
      screen.getByRole("combobox", { name: "AI 服务" }),
      SERVICE_ID,
    );
    expect(await screen.findByText("@zhangwei")).toBeVisible();
    expect(screen.getByRole("button", { name: "正常 1" })).toBeVisible();
    expect(screen.getByRole("button", { name: "已禁用 0" })).toBeVisible();
  });

  it("UI-35 enables a disabled user and refreshes the row", async () => {
    const user = userEvent.setup();
    const apiClient = await renderList([
      live,
      [self, zhangwei, { ...lina, status: "active" }],
    ]);
    apiClient.userCommandResults.push({ ...lina, status: "active" });
    await user.click(rowOf("lina").getByRole("button", { name: "启用" }));
    const dialog = await screen.findByRole("dialog", {
      name: "启用用户「lina」？",
    });
    expect(
      within(dialog).getByText(
        "恢复仍未过期、未撤销的 API 密钥（2 个）和原有服务授权。",
      ),
    ).toBeVisible();
    expect(await axe.run(dialog)).toHaveProperty("violations", []);
    await user.click(within(dialog).getByRole("button", { name: "启用" }));
    expect(await screen.findByText("已启用 lina")).toBeVisible();
    expect(
      await rowOf("lina").findByRole("button", { name: "禁用" }),
    ).toBeVisible();
    expect(apiClient.userCommandCalls).toEqual([
      { command: "enable", id: lina.id },
    ]);
  });

  it("UI-35 formats billions, thousands and explicit zero; failed refresh hides old totals", async () => {
    const apiClient = new FakeApiClient(ME);
    apiClient.userResults.push({ items: live });
    apiClient.usageResults.push(
      report([
        group(zhangwei.id, 1_284_530_000),
        group(lina.id, 12345),
        group(self.id, 0),
      ]),
    );
    const { queryClient } = renderApplication(apiClient, "/users");
    await screen.findByText("1.3B");
    expect(rowOf("zhangwei").getAllByRole("cell")[4]).toHaveAttribute(
      "title",
      "1,284,530,000 Tokens",
    );
    expect(rowOf("lina").getAllByRole("cell")[4]).toHaveTextContent("12,345");
    expect(rowOf("admin").getAllByRole("cell")[4]).toHaveClass("pw-user-muted");
    apiClient.usageResults.push(
      new ApiError({ code: "unavailable", status: 503 }),
    );
    await act(async () => {
      await queryClient.invalidateQueries({
        queryKey: ["user-list-today-usage"],
      });
    });
    await waitFor(() =>
      expect(rowOf("zhangwei").getAllByRole("cell")[4]).toHaveTextContent("—"),
    );
  });

  it("UI-35 does not fetch usage without view_usage capability", async () => {
    const apiClient = new FakeApiClient({
      ...ME,
      capabilities: ME.capabilities.filter(
        ({ capability }) => capability !== "view_usage",
      ),
    });
    apiClient.userResults.push({ items: live });
    renderApplication(apiClient, "/users");
    await screen.findByText("@zhangwei");
    expect(apiClient.usageCalls).toEqual([]);
    expect(rowOf("zhangwei").getAllByRole("cell")[4]).toHaveTextContent("—");
  });

  it("UI-35 has no blocking axe violations on the list", async () => {
    await renderList([live]);
    const result = await axe.run(document, {
      rules: { "color-contrast": { enabled: false } },
    });
    expect(
      result.violations.filter(
        ({ impact }) => impact === "critical" || impact === "serious",
      ),
    ).toEqual([]);
  });

  it("UI-35 hides 最近登录 below 1180 px and 服务数 below 1024 px in CSS", () => {
    const css = readFileSync(
      resolve(dirname(fileURLToPath(import.meta.url)), "../styles/app.css"),
      "utf8",
    );
    const block = (query: string) => {
      const start = css.indexOf(`@media (max-width: ${query})`);
      return css.slice(start, css.indexOf("\n}\n", start));
    };
    expect(block("1179px")).toContain(
      '.pw-users-page [data-column="last-login"]',
    );
    expect(block("1023px")).toContain(
      '.pw-users-page [data-column="services"]',
    );
  });
});

function rowOfElement(username: string) {
  const row = screen.getByText(new RegExp(`^@${username}`)).closest("tr");
  if (row === null) throw new Error(`row ${username} is missing`);
  return row;
}
