import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import axe from "axe-core";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client";
import type { UserActivityItem, UserDetail } from "../api/user-contracts";
import {
  ADMIN_ME,
  FakeApiClient,
  renderApplication,
} from "../app/test-support";
import { activityText } from "./user-detail-activity";
import { usageWindow } from "./user-detail-usage";

// FX-UI-36: synthetic user, safe key metadata and audit events; no secrets.
const id = "0198f58a-2586-7e37-bbf5-000000000036";
const sid = "0198f58a-2586-7e37-bbf5-000000000037";
const key = {
  id: sid,
  name: "程序调用",
  display_prefix: "pw",
  last4: "1234",
  status: "Active" as const,
  expires_at: null,
  last_used_at: null,
};
const detail: UserDetail = {
  id,
  username: "bob",
  display_name: "Bob",
  status: "active",
  role: "user",
  created_at: "2026-10-01T00:00:00Z",
  last_login_at: null,
  must_change_password: true,
  api_key_count: 1,
  authorized_service_count: 0,
  authorized_services: [],
  api_keys: [key, { ...key, id, name: "旧密钥", status: "Revoked" }],
};
const event: UserActivityItem = {
  id,
  created_at: "2026-10-01T00:00:00Z",
  action: "auth.login",
  operation: null,
  result: "succeeded",
  reason_code: null,
  actor: { type: "user", id, username: "bob", display_name: "Bob" },
  api_key: null,
  added_service_ids: [],
  removed_service_ids: [],
  limit_changed_service_ids: [],
};
function setup(overrides: Partial<UserDetail> = {}) {
  const client = new FakeApiClient({
    ...ADMIN_ME,
    capabilities: [
      ...ADMIN_ME.capabilities,
      { capability: "view_usage", scope: "all" },
    ],
  });
  const get = vi
    .spyOn(client, "getUser")
    .mockResolvedValue({ ...detail, ...overrides });
  vi.spyOn(client, "getAiServices").mockResolvedValue({
    items: [
      {
        id: sid,
        name: "研发助手",
        public_model_name: "alpha",
        description: "",
        enabled: false,
        primary_instance_id: id,
        primary_instance_status: "Running",
        runtime_status: "Available",
        route_version: 1,
      },
    ],
  });
  const activity = vi
    .spyOn(client, "getUserActivity")
    .mockResolvedValue({ items: [event], next_cursor: null });
  const revoke = vi
    .spyOn(client, "revokeUserApiKey")
    .mockResolvedValue({ ...key, status: "Revoked" });
  const view = renderApplication(client, `/users/${overrides.id ?? id}`);
  return { client, get, activity, revoke, ...view, user: userEvent.setup() };
}
async function tab(
  user: ReturnType<typeof userEvent.setup>,
  name: RegExp | string,
) {
  await user.click(await screen.findByRole("tab", { name }));
}

describe("UI-36 user detail", () => {
  it("UI-36 renders overview, five accessible tabs and keyboard navigation", async () => {
    const { user, container } = setup();
    await screen.findByRole("heading", { name: "Bob" });
    expect(
      screen.getByRole("navigation", { name: "面包屑" }),
    ).toHaveTextContent("用户 › Bob");
    expect(screen.getByText("创建时间")).toBeVisible();
    expect(screen.getByText("尚未完成首次登录")).toBeVisible();
    for (const name of ["概览", /^服务权限/, /^API 密钥/, "用量", "活动"]) {
      await tab(user, name);
      await waitFor(() =>
        expect(screen.queryByText("正在加载活动…")).not.toBeInTheDocument(),
      );
      expect(
        (
          await axe.run(container, {
            rules: { "color-contrast": { enabled: false } },
          })
        ).violations,
      ).toEqual([]);
    }
    await tab(user, "概览");
    screen.getByRole("tab", { name: "概览" }).focus();
    await user.keyboard("{ArrowRight}");
    expect(screen.getByRole("tab", { name: /^服务权限/ })).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });
  it.each(["disabled", "deleted"] as const)(
    "UI-36 shows %s banner and preserves read-only deleted account",
    async (status) => {
      const { user } = setup({ status });
      await screen.findByRole("heading", { name: "Bob" });
      expect(
        screen.getByText(
          status === "deleted" ? /账号不能恢复/ : /1 个 API 密钥暂停使用/,
        ),
      ).toBeVisible();
      await tab(user, /^API 密钥/);
      if (status === "disabled")
        expect(screen.getByText("暂停使用")).toHaveAttribute(
          "title",
          expect.stringContaining("启用用户后恢复"),
        );
      else {
        expect(
          screen.queryByRole("button", { name: "撤销" }),
        ).not.toBeInTheDocument();
        await tab(user, /^服务权限/);
        expect(
          screen.getByText("用户已删除，全部服务授权已在删除时移除。"),
        ).toBeVisible();
        expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
      }
    },
  );
  it("UI-36 shows self password link and empty display fallback", async () => {
    setup({ id: ADMIN_ME.user.id, display_name: "" });
    await screen.findByRole("heading", { name: "bob" });
    expect(screen.getByRole("link", { name: "修改我的密码" })).toHaveAttribute(
      "href",
      "/change-password",
    );
    expect(screen.getByText("（未填写，显示用户名）")).toBeVisible();
    expect(screen.getByRole("button", { name: "禁用" })).toBeDisabled();
  });
  it.each([404, 503])("UI-36 distinguishes load error %s", async (status) => {
    const client = new FakeApiClient({
      ...ADMIN_ME,
      capabilities: [
        ...ADMIN_ME.capabilities,
        { capability: "view_usage", scope: "all" },
      ],
    });
    vi.spyOn(client, "getUser").mockRejectedValue(
      new ApiError({
        status,
        code: status === 404 ? "user_not_found" : "unavailable",
      }),
    );
    renderApplication(client, `/users/${id}`);
    expect(
      await screen.findByText(
        status === 404 ? "找不到这个用户" : "无法获取用户详情",
      ),
    ).toBeVisible();
    expect(screen.getByRole("link", { name: "返回用户列表" })).toHaveAttribute(
      "href",
      "/users",
    );
    expect(screen.queryByRole("button", { name: "重试" }) !== null).toBe(
      status !== 404,
    );
  });
  it.each(["继续编辑", "关闭", "Escape", "放弃更改并离开"])(
    "UI-37 restores the initiating tab after %s without reopening confirmation",
    async (close) => {
      const { user } = setup();
      await tab(user, /^服务权限/);
      await user.click(
        screen.getByRole("checkbox", { name: "研发助手 · alpha" }),
      );
      const trigger = screen.getByRole("tab", { name: "概览" });
      await user.click(trigger);
      await screen.findByRole("dialog");
      if (close === "Escape") await user.keyboard("{Escape}");
      else await user.click(screen.getByRole("button", { name: close }));
      await waitFor(() => expect(trigger).toHaveFocus());
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
      expect(trigger).toHaveAttribute(
        "aria-selected",
        close === "放弃更改并离开" ? "true" : "false",
      );
    },
  );
  it.each(["取消", "关闭", "Escape"])(
    "UI-37 restores the revoke button after %s",
    async (close) => {
      const { user, revoke } = setup();
      await tab(user, /^API 密钥/);
      const trigger = screen.getByRole("button", { name: "撤销" });
      await user.click(trigger);
      await screen.findByRole("dialog");
      if (close === "Escape") await user.keyboard("{Escape}");
      else await user.click(screen.getByRole("button", { name: close }));
      await waitFor(() => expect(trigger).toHaveFocus());
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
      expect(revoke).not.toHaveBeenCalled();
    },
  );
  it("UI-36 guards tab/page departure, discards and retains draft after save failure", async () => {
    const { client, user, get } = setup();
    const save = vi
      .spyOn(client, "replaceUserPermissions")
      .mockRejectedValueOnce(new Error("offline"))
      .mockResolvedValue({ service_ids: [sid] });
    await tab(user, /^服务权限/);
    await user.click(
      screen.getByRole("checkbox", { name: "研发助手 · alpha" }),
    );
    expect(
      screen.getByText("未保存：新增授权 1 个。保存后立即生效。"),
    ).toBeVisible();
    await tab(user, "概览");
    expect(
      await screen.findByRole("dialog", { name: "有未保存的服务权限更改" }),
    ).toBeVisible();
    await user.click(screen.getByRole("button", { name: "继续编辑" }));
    await user.click(screen.getByRole("button", { name: "保存" }));
    expect(await screen.findByText("服务权限未保存")).toBeVisible();
    expect(screen.getByRole("checkbox")).toBeChecked();
    get.mockResolvedValue({
      ...detail,
      authorized_services: [
        {
          max_concurrent_requests: 8,
          max_requests_per_minute: null,
          id: sid,
          name: "研发助手",
          public_model_name: "alpha",
          description: "",
          enabled: false,
        },
      ],
    });
    await user.click(screen.getByRole("button", { name: "保存" }));
    expect(await screen.findByText("服务权限已生效")).toBeVisible();
    expect(save).toHaveBeenLastCalledWith(id, [sid], {});
    await user.click(screen.getByRole("checkbox"));
    expect(screen.getByText("将撤销")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "放弃更改" }));
    expect(screen.getByRole("checkbox")).toBeChecked();
    await user.click(screen.getByRole("checkbox"));
    await tab(user, "概览");
    await user.click(screen.getByRole("button", { name: "放弃更改并离开" }));
    expect(screen.getByRole("tab", { name: "概览" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });
  it("UI-36 hides historical keys, revokes with exact impact and reports failure", async () => {
    const { user, revoke, get, client } = setup();
    await tab(user, /^API 密钥/);
    expect(screen.queryByText("旧密钥")).not.toBeInTheDocument();
    expect(screen.getByText(/已隐藏 1 个/)).toBeVisible();
    await user.click(
      screen.getByRole("checkbox", { name: "显示已过期和已撤销（1）" }),
    );
    expect(screen.getByText("旧密钥")).toBeVisible();
    expect(screen.getAllByRole("button", { name: "撤销" })).toHaveLength(1);
    revoke.mockRejectedValueOnce(
      new ApiError({
        status: 503,
        code: "unavailable",
        responseMessage: "暂时无法撤销",
      }),
    );
    await user.click(screen.getByRole("button", { name: "撤销" }));
    const dialog = await screen.findByRole("dialog");
    expect(
      within(dialog).getByText(
        "立即生效且不可恢复：用这个密钥的调用会被拒绝（api_key_revoked）。",
      ),
    ).toBeVisible();
    await user.click(within(dialog).getByRole("button", { name: "撤销密钥" }));
    expect(await screen.findByText("操作未完成")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "撤销" }));
    get.mockResolvedValue({
      ...detail,
      api_key_count: 0,
      api_keys: [{ ...key, status: "Revoked" }],
    });
    await user.click(
      within(await screen.findByRole("dialog")).getByRole("button", {
        name: "撤销密钥",
      }),
    );
    expect(await screen.findByText("已撤销 程序调用")).toBeVisible();
    expect(revoke).toHaveBeenCalledWith(id, sid);
    expect(
      client.usageCalls.some(
        (call) =>
          call.groupBy === "api_key" &&
          call.userId === id &&
          Date.parse(call.to) - Date.parse(call.from) === 30 * 86400000,
      ),
    ).toBe(true);
  });
  it.each([{ keys: [] }, { keys: [{ ...key, status: "Expired" as const }] }])(
    "UI-36 keys empty state %#",
    async ({ keys }) => {
      const { user } = setup({ api_keys: keys, api_key_count: 0 });
      await tab(user, /^API 密钥/);
      expect(
        screen.getByText(
          keys.length
            ? "没有生效中的 API 密钥。勾选右上角可查看已过期和已撤销的密钥。"
            : "该用户还没有创建 API 密钥。用户登录后可以在自己的「API 密钥」页创建。",
        ),
      ).toBeVisible();
      expect(
        screen.queryByRole("button", { name: "撤销" }),
      ).not.toBeInTheDocument();
    },
  );
  it("UI-36 queries usage ranges and renders empty summary", async () => {
    const { user, client } = setup();
    await tab(user, "用量");
    for (const label of ["今日", "近 7 天", "本月"])
      await user.click(screen.getByRole("button", { name: label }));
    expect(
      await screen.findByText("所选时间范围内没有调用记录。"),
    ).toBeVisible();
    expect(screen.getByText("Input Tokens")).toBeVisible();
    expect(
      screen.getByRole("link", { name: "在「用量」页查看明细" }),
    ).toHaveAttribute("href", `/usage?user_id=${id}`);
    expect(client.usageCalls.every((call) => call.userId === id)).toBe(true);
    const now = new Date(2026, 9, 14, 12);
    expect(usageWindow("today", now).from).toBe(
      new Date(2026, 9, 14).toISOString(),
    );
    expect(usageWindow("month", now).from).toBe(
      new Date(2026, 9, 1).toISOString(),
    );
    expect(Date.parse(usageWindow("week", now).from)).toBe(
      now.getTime() - 7 * 86400000,
    );
  });
  it("UI-36 activity filters and cursor append", async () => {
    const { user, activity } = setup();
    activity
      .mockResolvedValueOnce({ items: [event], next_cursor: "next" })
      .mockResolvedValueOnce({
        items: [
          { ...event, id: sid, action: "account.change", operation: "create" },
        ],
        next_cursor: null,
      });
    await tab(user, "活动");
    await user.click(await screen.findByRole("button", { name: "加载更多" }));
    expect(await screen.findByText("创建账号")).toBeVisible();
    expect(screen.getByText("登录成功")).toBeVisible();
    expect(activity).toHaveBeenCalledWith(id, {
      actions: [],
      cursor: "next",
      limit: 50,
    });
    for (const [label, actions] of [
      ["登录", ["auth.login"]],
      ["账号与密码", ["account.change", "account.password_change"]],
      ["API 密钥", ["api_key.change"]],
      ["服务权限", ["permission.change"]],
    ] as const) {
      await user.click(
        within(screen.getByRole("group", { name: "活动类型" })).getByRole(
          "button",
          { name: label },
        ),
      );
      await waitFor(() => {
        expect(activity).toHaveBeenLastCalledWith(id, {
          actions: [...actions],
          cursor: undefined,
          limit: 50,
        });
      });
    }
  });
  it("UI-36 guards breadcrumb departure", async () => {
    const { user } = setup();
    await tab(user, /^服务权限/);
    await user.click(screen.getByRole("checkbox"));
    await user.click(
      within(screen.getByRole("navigation", { name: "面包屑" })).getByRole(
        "link",
        { name: "用户" },
      ),
    );
    expect(await screen.findByRole("dialog")).toHaveTextContent(
      "有未保存的服务权限更改",
    );
    expect(screen.getByRole("checkbox", { hidden: true })).toBeChecked();
  });
  it("UI-36 displays both usage dimensions and null key totals without inventing zero", async () => {
    const { client, user } = setup();
    vi.spyOn(client, "getUsage").mockImplementation((options) =>
      Promise.resolve({
        ...options,
        tz: options.timeZone,
        bucket_width: "day",
        trend: [],
        summary: {
          requests: 3,
          input_tokens: 1000000,
          output_tokens: 2000000,
          total_tokens: 3000000,
          error_requests: 0,
          error_rate: 0,
          partial_token_requests: 0,
        },
        details: [
          {
            group_id: options.groupBy === "service" ? id : sid,
            display_name:
              options.groupBy === "service" ? "历史服务" : "历史密钥",
            is_deleted: true,
            status: options.groupBy === "api_key" ? "Expired" : null,
            requests: 3,
            input_tokens: null,
            output_tokens: null,
            total_tokens: null,
            error_requests: 0,
            error_rate: 0,
            partial_token_requests: 3,
          },
        ],
      }),
    );
    await tab(user, "用量");
    expect(await screen.findByText("历史服务")).toBeVisible();
    expect(screen.getByText("历史密钥（已过期）")).toBeVisible();
    expect(screen.getByText("3.0M")).toHaveAttribute("title", "3,000,000");
    expect(
      screen.getByRole("table", { name: "按 API 密钥" }),
    ).toHaveTextContent("—");
    await tab(user, /^API 密钥/);
    const row = screen.getByRole("row", { name: /程序调用/ });
    await waitFor(() =>
      expect(within(row).getAllByRole("cell")[4]).toHaveTextContent("—"),
    );
  });
  it("UI-36 keeps breakpoint rules in existing blocks", () => {
    const css = readFileSync(
      resolve(dirname(fileURLToPath(import.meta.url)), "../styles/app.css"),
      "utf8",
    );
    expect(css.match(/@media \(max-width: 767px\)/g)).toHaveLength(1);
    for (const [size, rule] of [
      ["1023px", ".pw-user-detail__grid"],
      ["767px", ".pw-user-detail__metrics"],
    ] as const) {
      const start = css.indexOf(`@media (max-width: ${size})`);
      expect(css.slice(start, css.indexOf("\n}\n", start))).toContain(rule);
    }
  });
  it("UI-36 maps event operations and legacy audit descriptions", () => {
    for (const [action, operation, text] of [
      ["account.change", "create", "创建账号"],
      ["account.change", "bootstrap", "初始化时创建管理员账号"],
      ["account.change", "disable", "禁用账号"],
      ["account.change", "enable", "启用账号"],
      [
        "account.change",
        "delete",
        "删除账号；撤销全部 API 密钥、移除全部服务授权",
      ],
      ["account.password_change", "change", "修改密码"],
      ["account.password_change", "reset", "重置密码，撤销全部登录会话"],
      ["api_key.change", "create", "创建 API 密钥"],
      ["api_key.change", "rename", "改名 API 密钥"],
      ["api_key.change", "revoke", "撤销 API 密钥"],
      ["api_key.change", "delete", "删除记录 API 密钥"],
      ["account.change", null, "修改账号"],
      ["account.password_change", null, "修改密码"],
      ["permission.change", null, "修改服务权限"],
      ["api_key.change", null, "变更 API 密钥"],
    ] as const)
      expect(activityText({ ...event, action, operation }, new Map())).toBe(
        text,
      );
    expect(
      activityText(
        {
          ...event,
          action: "permission.change",
          operation: "replace_by_service",
          added_service_ids: [id],
          removed_service_ids: [sid],
        },
        new Map([[id, "A"]]),
      ),
    ).toBe(
      "授权 AI 服务「A」；撤销 AI 服务「已删除的 AI 服务」的授权（在 AI 服务页操作）",
    );
    expect(
      activityText(
        { ...event, action: "permission.change", operation: "replace" },
        new Map(),
      ),
    ).toBe("服务权限未变化");
    expect(
      activityText(
        {
          ...event,
          action: "api_key.change",
          operation: "revoke",
          api_key: key,
        },
        new Map(),
      ),
    ).toBe("撤销 API 密钥「程序调用」…1234");
  });
});

// FX-M4-18: reuse UI-36 service metadata with explicit call limits.
describe("M4_18 user call limits", () => {
  const grant = {
    id: sid,
    name: "研发助手",
    public_model_name: "alpha",
    description: "",
    enabled: false,
    max_concurrent_requests: 3,
    max_requests_per_minute: null,
  };
  it("M4_18 preserves off-preset values, discards, sends one changed grant and refreshes", async () => {
    const { user, client, get, container } = setup({
      authorized_services: [grant],
    });
    const save = vi
      .spyOn(client, "replaceUserPermissions")
      .mockImplementation(() => {
        get.mockResolvedValue({
          ...detail,
          authorized_services: [
            {
              ...grant,
              max_concurrent_requests: null,
              max_requests_per_minute: 10,
            },
          ],
        });
        return Promise.resolve({ service_ids: [sid] });
      });
    await tab(user, /^服务权限/);
    const concurrent = () =>
      screen.getByRole("combobox", { name: "研发助手 的同时请求上限" });
    const rpm = () =>
      screen.getByRole("combobox", { name: "研发助手 的每分钟请求上限" });
    expect(concurrent()).toHaveValue("3");
    expect(
      within(concurrent()).getByRole("option", { name: "3 个" }),
    ).toBeVisible();
    await user.selectOptions(concurrent(), "1");
    await user.click(screen.getByRole("button", { name: "放弃更改" }));
    expect(concurrent()).toHaveValue("3");
    await user.selectOptions(concurrent(), "");
    await user.selectOptions(rpm(), "10");
    expect(
      screen.getByRole("region", { name: "未保存的更改" }),
    ).toHaveTextContent("未保存：修改上限 1 个。保存后立即生效。");
    expect(
      (
        await axe.run(container, {
          rules: { "color-contrast": { enabled: false } },
        })
      ).violations,
    ).toEqual([]);
    await user.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() => {
      expect(save).toHaveBeenCalledWith(id, [sid], {
        [sid]: { max_concurrent_requests: null, max_requests_per_minute: 10 },
      });
    });
    await screen.findByText("服务权限已生效");
    expect(concurrent()).toHaveValue("");
    expect(rpm()).toHaveValue("10");
    expect(get.mock.calls.length).toBeGreaterThan(1);
  });
  it.each([false, true])(
    "M4_18 new grant sends overrides only when changed: %s",
    async (change) => {
      const { user, client } = setup();
      const save = vi
        .spyOn(client, "replaceUserPermissions")
        .mockResolvedValue({ service_ids: [sid] });
      await tab(user, /^服务权限/);
      const checkbox = screen.getByRole("checkbox", {
        name: "研发助手 · alpha",
      });
      await user.click(checkbox);
      const concurrent = screen.getByRole("combobox", {
        name: "研发助手 的同时请求上限",
      });
      expect(concurrent).toHaveValue("8");
      expect(
        screen.getByRole("combobox", { name: "研发助手 的每分钟请求上限" }),
      ).toHaveValue("");
      if (change) await user.selectOptions(concurrent, "2");
      await user.click(checkbox);
      expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
      await user.click(checkbox);
      await user.click(screen.getByRole("button", { name: "保存" }));
      await waitFor(() => {
        expect(save).toHaveBeenCalledWith(
          id,
          [sid],
          change
            ? {
                [sid]: {
                  max_concurrent_requests: 2,
                  max_requests_per_minute: null,
                },
              }
            : {},
        );
      });
    },
  );
  it("M4_18 displays limit changes alongside authorization and revocation", async () => {
    const { user, activity } = setup();
    activity.mockResolvedValue({
      items: [
        {
          ...event,
          action: "permission.change",
          operation: "replace",
          limit_changed_service_ids: [sid],
          added_service_ids: [sid],
          removed_service_ids: [id],
        },
      ],
      next_cursor: null,
    });
    await tab(user, "活动");
    expect(
      await screen.findByText("调整 1 个 AI 服务的调用上限", { exact: true }),
    ).toBeVisible();
    expect(
      screen.getByText("授权 1 个 AI 服务", { exact: true }),
    ).toBeVisible();
    expect(
      screen.getByText("撤销 1 个 AI 服务的授权", { exact: true }),
    ).toBeVisible();
  });
});
