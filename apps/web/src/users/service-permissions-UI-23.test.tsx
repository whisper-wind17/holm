import axe from "axe-core";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { AiService } from "../api/ai-service-contracts";
import type { UserOverview } from "../api/user-contracts";
import {
  ADMIN_ME,
  FakeApiClient,
  renderApplication,
} from "../app/test-support";

// FX-UI-23: metadata-only users (one active granted, one active ungranted, one
// disabled granted, one disabled ungranted, one more active ungranted).
const SERVICE_ID = "0198f58a-2586-7e37-bbf5-000000000031";
const BOB = "0198f58a-2586-7e37-bbf5-0000000000b1";
const CAROL = "0198f58a-2586-7e37-bbf5-0000000000c1";
const DAVE = "0198f58a-2586-7e37-bbf5-0000000000d1";
const ERIN = "0198f58a-2586-7e37-bbf5-0000000000e1";
const FRANK = "0198f58a-2586-7e37-bbf5-0000000000f1";

const service: AiService = {
  description: "",
  enabled: true,
  id: SERVICE_ID,
  name: "研发助手",
  primary_instance_id: "0198f58a-2586-7e37-bbf5-000000000032",
  primary_instance_status: "Running",
  public_model_name: "qwen-mini",
  route_version: 1,
  runtime_status: "Available",
};

function person(
  id: string,
  name: string,
  status: UserOverview["status"] = "active",
): UserOverview {
  return {
    api_key_count: 0,
    authorized_service_count: 0,
    display_name: name,
    id,
    last_login_at: null,
    must_change_password: false,
    role: "user",
    status,
    username: name.toLowerCase(),
  };
}

const USERS = {
  items: [
    person(BOB, "Bob"),
    person(CAROL, "Carol"),
    person(DAVE, "Dave", "disabled"),
    person(ERIN, "Erin", "disabled"),
    person(FRANK, "Frank"),
  ],
};

async function setup() {
  const api = new FakeApiClient(ADMIN_ME);
  api.aiServiceDetailResults.push(service);
  api.modelInstanceResults.push({ items: [] });
  // A successful save refetches the user list; queue the same list again.
  api.userResults.push(USERS, USERS);
  api.servicePermissionReadResults.push({
    user_ids: [BOB, DAVE],
    limits: Object.fromEntries(
      [BOB, DAVE].map((id) => [
        id,
        { max_concurrent_requests: null, max_requests_per_minute: null },
      ]),
    ),
  });
  const view = renderApplication(api, `/services/${SERVICE_ID}`);
  const user = userEvent.setup();
  await user.click(await screen.findByRole("tab", { name: "访问权限" }));
  await screen.findByLabelText("Bob (@bob)");
  return { api, user, view };
}

const authorizedZone = () => screen.getByRole("region", { name: /^已授权/ });
const unauthorizedZone = () => screen.getByRole("region", { name: /^未授权/ });

describe("UI-23 access permissions", () => {
  it("UI-23 splits authorized and unauthorized users, marks disabled users and lists only active users to grant", async () => {
    await setup();
    const authorized = within(authorizedZone());
    const unauthorized = within(unauthorizedZone());
    expect(authorized.getByLabelText("Bob (@bob)")).toBeVisible();
    expect(authorized.getByLabelText("Dave (@dave)")).toBeVisible();
    expect(authorized.getByText("用户已禁用")).toBeVisible();
    expect(authorized.queryByLabelText("Carol (@carol)")).toBeNull();
    expect(unauthorized.getByLabelText("Carol (@carol)")).toBeVisible();
    expect(unauthorized.getByLabelText("Frank (@frank)")).toBeVisible();
    // Erin is disabled and not granted: not offered.
    expect(screen.queryByLabelText("Erin (@erin)")).toBeNull();
    expect(screen.getByRole("heading", { name: "已授权 2 人" })).toBeVisible();
    expect(screen.getByRole("heading", { name: "未授权 2 人" })).toBeVisible();
    expect(
      screen.queryByRole("region", { name: "未保存的更改" }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "授权选中（0）" }),
    ).toBeDisabled();
    expect(
      screen.getByRole("button", { name: "移除选中（0）" }),
    ).toBeDisabled();
  });

  it("UI-23 search narrows both zones by username or display name", async () => {
    const { user } = await setup();
    await user.type(screen.getByLabelText("搜索用户名或显示名"), "car");
    expect(screen.getByLabelText("Carol (@carol)")).toBeVisible();
    expect(screen.queryByLabelText("Frank (@frank)")).toBeNull();
    expect(screen.queryByLabelText("Bob (@bob)")).toBeNull();
    expect(screen.getByText("没有匹配的已授权用户。")).toBeVisible();
  });

  it("UI-23 collects grants and removals in a bar and saves them with one PUT", async () => {
    const { api, user } = await setup();
    await user.click(screen.getByLabelText("Carol (@carol)"));
    await user.click(screen.getByRole("button", { name: "授权选中（1）" }));
    expect(
      within(authorizedZone()).getByLabelText("Carol (@carol)"),
    ).toBeVisible();
    expect(within(authorizedZone()).getByText("待授权")).toBeVisible();
    await user.click(within(authorizedZone()).getByLabelText("Bob (@bob)"));
    await user.click(screen.getByRole("button", { name: "移除选中（1）" }));
    expect(
      within(unauthorizedZone()).getByLabelText("Bob (@bob)"),
    ).toBeVisible();
    expect(within(unauthorizedZone()).getByText("待移除")).toBeVisible();
    const bar = screen.getByRole("region", { name: "未保存的更改" });
    expect(bar).toHaveTextContent("2 项未保存的更改");
    expect(api.servicePermissionCalls).toEqual([]);

    api.servicePermissionReadResults.push({
      user_ids: [DAVE, CAROL],
      limits: Object.fromEntries(
        [DAVE, CAROL].map((id) => [
          id,
          { max_concurrent_requests: null, max_requests_per_minute: null },
        ]),
      ),
    });
    await user.click(within(bar).getByRole("button", { name: "保存访问权限" }));
    await waitFor(() => {
      expect(api.servicePermissionCalls).toEqual([
        { id: SERVICE_ID, userIds: [DAVE, CAROL] },
      ]);
    });
    expect(await screen.findByText("访问权限已生效")).toBeVisible();
    expect(
      screen.queryByRole("region", { name: "未保存的更改" }),
    ).not.toBeInTheDocument();
    expect(
      within(authorizedZone()).getByLabelText("Carol (@carol)"),
    ).toBeVisible();
  });

  it("UI-23 discards the draft and treats a grant followed by its removal as no change", async () => {
    const { api, user } = await setup();
    await user.click(screen.getByLabelText("Carol (@carol)"));
    await user.click(screen.getByRole("button", { name: "授权选中（1）" }));
    await user.click(within(authorizedZone()).getByLabelText("Carol (@carol)"));
    await user.click(screen.getByRole("button", { name: "移除选中（1）" }));
    expect(
      screen.queryByRole("region", { name: "未保存的更改" }),
    ).not.toBeInTheDocument();

    await user.click(screen.getByLabelText("Frank (@frank)"));
    await user.click(screen.getByRole("button", { name: "授权选中（1）" }));
    await user.click(screen.getByRole("button", { name: "放弃更改" }));
    expect(
      within(unauthorizedZone()).getByLabelText("Frank (@frank)"),
    ).toBeVisible();
    expect(
      screen.queryByRole("region", { name: "未保存的更改" }),
    ).not.toBeInTheDocument();
    expect(api.servicePermissionCalls).toEqual([]);
  });

  it("UI-23 keeps the draft and the original grants when saving fails, and a retry saves", async () => {
    const { api, user } = await setup();
    api.servicePermissionResults.push(new Error("conflict"));
    await user.click(screen.getByLabelText("Carol (@carol)"));
    await user.click(screen.getByRole("button", { name: "授权选中（1）" }));
    await user.click(screen.getByRole("button", { name: "保存访问权限" }));
    expect(await screen.findByText("访问权限未保存")).toBeVisible();
    expect(screen.getByRole("region", { name: "未保存的更改" })).toBeVisible();
    expect(
      within(authorizedZone()).getByLabelText("Carol (@carol)"),
    ).toBeVisible();

    await user.click(screen.getByRole("button", { name: "保存访问权限" }));
    await waitFor(() => {
      expect(api.servicePermissionCalls).toEqual([
        { id: SERVICE_ID, userIds: [BOB, DAVE, CAROL] },
        { id: SERVICE_ID, userIds: [BOB, DAVE, CAROL] },
      ]);
    });
    expect(await screen.findByText("访问权限已生效")).toBeVisible();
  });

  it("UI-23 asks before leaving the tab, keeps the draft on 继续编辑 and drops it on 放弃更改", async () => {
    const { user } = await setup();
    await user.click(screen.getByLabelText("Carol (@carol)"));
    await user.click(screen.getByRole("button", { name: "授权选中（1）" }));

    await user.click(screen.getByRole("tab", { name: "设置" }));
    const dialog = await screen.findByRole("dialog", {
      name: "放弃未保存的访问权限更改？",
    });
    expect(dialog).toHaveTextContent(
      "「访问权限」有 1 项更改尚未保存。离开后这些更改会丢失，原授权不变。",
    );
    await user.click(within(dialog).getByRole("button", { name: "继续编辑" }));
    expect(screen.getByRole("tab", { name: "访问权限" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.getByRole("region", { name: "未保存的更改" })).toBeVisible();

    await user.click(screen.getByRole("tab", { name: "设置" }));
    await user.click(
      within(await screen.findByRole("dialog")).getByRole("button", {
        name: "放弃更改",
      }),
    );
    expect(screen.getByRole("tab", { name: "设置" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    await user.click(screen.getByRole("tab", { name: "访问权限" }));
    expect(
      within(unauthorizedZone()).getByLabelText("Carol (@carol)"),
    ).toBeVisible();
    expect(
      screen.queryByRole("region", { name: "未保存的更改" }),
    ).not.toBeInTheDocument();
  });

  it("UI-23 follows the breadcrumb straight away when nothing is unsaved", async () => {
    const { user } = await setup();
    // Clean: the breadcrumb navigates straight away.
    await user.click(
      within(screen.getByRole("navigation", { name: "面包屑" })).getByRole(
        "link",
        { name: "AI 服务" },
      ),
    );
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(
      await screen.findByRole("heading", { level: 1, name: "AI 服务" }),
    ).toBeVisible();
  });

  it.each([
    ["面包屑", "AI 服务"],
    ["主导航", "用户"],
  ])(
    "UI-23 prompts on the %s link %s while grants are unsaved",
    async (area, label) => {
      const { api, user } = await setup();
      await user.click(screen.getByLabelText("Carol (@carol)"));
      await user.click(screen.getByRole("button", { name: "授权选中（1）" }));
      const link = within(
        screen.getByRole("navigation", { name: area }),
      ).getByRole("link", { name: label });

      await user.click(link);
      const dialog = await screen.findByRole("dialog", {
        name: "放弃未保存的访问权限更改？",
      });
      await user.click(
        within(dialog).getByRole("button", { name: "继续编辑" }),
      );
      // Still on the detail page with the draft intact.
      expect(
        screen.getByRole("region", { name: "未保存的更改" }),
      ).toBeVisible();

      await user.click(link);
      await user.click(
        within(await screen.findByRole("dialog")).getByRole("button", {
          name: "放弃更改",
        }),
      );
      await waitFor(() => {
        expect(
          screen.queryByRole("region", { name: "未保存的更改" }),
        ).not.toBeInTheDocument();
      });
      expect(screen.queryByRole("tab", { name: "访问权限" })).toBeNull();
      expect(api.servicePermissionCalls).toEqual([]);
    },
  );

  it("UI-23 registers a beforeunload prompt only while changes are unsaved", async () => {
    const { user } = await setup();
    const fire = () => {
      const event = new Event("beforeunload", { cancelable: true });
      window.dispatchEvent(event);
      return event.defaultPrevented;
    };
    expect(fire()).toBe(false);
    await user.click(screen.getByLabelText("Carol (@carol)"));
    await user.click(screen.getByRole("button", { name: "授权选中（1）" }));
    expect(fire()).toBe(true);
    await user.click(screen.getByRole("button", { name: "放弃更改" }));
    expect(fire()).toBe(false);
  });

  it("UI-23 passes axe with a pending change", async () => {
    const { user, view } = await setup();
    await user.click(screen.getByLabelText("Carol (@carol)"));
    await user.click(screen.getByRole("button", { name: "授权选中（1）" }));
    const result = await axe.run(view.container, {
      rules: { "color-contrast": { enabled: false } },
    });
    expect(
      result.violations.filter(
        ({ impact }) => impact === "critical" || impact === "serious",
      ),
    ).toEqual([]);
  });
});

it("M4_18 edits service-side limits, counts once, discards and saves only the changed user", async () => {
  const { api, user, view } = await setup();
  const save = vi.spyOn(api, "replaceServicePermissions").mockResolvedValue({
    user_ids: [BOB, DAVE],
    limits: {
      [BOB]: { max_concurrent_requests: 1, max_requests_per_minute: 10 },
      [DAVE]: { max_concurrent_requests: null, max_requests_per_minute: null },
    },
  });
  const concurrent = () =>
    screen.getByRole("combobox", { name: "bob 的同时请求上限" });
  const rpm = () =>
    screen.getByRole("combobox", { name: "bob 的每分钟请求上限" });
  expect(concurrent()).toHaveValue("");
  await user.selectOptions(concurrent(), "1");
  expect(screen.getByText("待改上限")).toBeVisible();
  await user.click(screen.getByRole("button", { name: "放弃更改" }));
  expect(concurrent()).toHaveValue("");
  expect(screen.queryByText("待改上限")).not.toBeInTheDocument();
  await user.selectOptions(concurrent(), "1");
  await user.selectOptions(rpm(), "10");
  expect(
    screen.getByRole("region", { name: "未保存的更改" }),
  ).toHaveTextContent("1 项未保存的更改");
  expect(
    (
      await axe.run(view.container, {
        rules: { "color-contrast": { enabled: false } },
      })
    ).violations,
  ).toEqual([]);
  api.servicePermissionReadResults.push({
    user_ids: [BOB, DAVE],
    limits: {
      [BOB]: { max_concurrent_requests: 1, max_requests_per_minute: 10 },
      [DAVE]: { max_concurrent_requests: null, max_requests_per_minute: null },
    },
  });
  await user.click(screen.getByRole("button", { name: "保存访问权限" }));
  await waitFor(() => {
    expect(save).toHaveBeenCalledWith(SERVICE_ID, [BOB, DAVE], {
      [BOB]: { max_concurrent_requests: 1, max_requests_per_minute: 10 },
    });
  });
  expect(await screen.findByText("访问权限已生效")).toBeVisible();
  expect(concurrent()).toHaveValue("1");
});
