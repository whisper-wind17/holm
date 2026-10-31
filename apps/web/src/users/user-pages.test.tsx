import { UI_46_PERSONAL_SERVICE } from "../app/test-support";
import axe from "axe-core";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { AiService } from "../api/ai-service-contracts";
import { ApiError } from "../api/client";
import type {
  CreatedUser,
  UserDetail,
  UserOverview,
} from "../api/user-contracts";
import {
  ADMIN_ME,
  FakeApiClient,
  renderApplication,
  USER_ME,
} from "../app/test-support";

describe("PROTO_15 user management and personal services", () => {
  it("PROTO_15 keeps a temporary password only in the open modal and out of Query caches", async () => {
    const apiClient = new FakeApiClient(ADMIN_ME);
    apiClient.userResults.push({ items: [] }, { items: [] });
    vi.spyOn(apiClient, "getAiServices").mockResolvedValue({
      items: [service()],
    });
    const created = createdUser();
    apiClient.userCreateResults.push(created);
    const { queryClient } = renderApplication(apiClient, "/users");
    const user = userEvent.setup();
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText: vi.fn().mockRejectedValue(new Error("denied")) },
    });

    await screen.findByRole("heading", { name: "用户" });
    const createButtons = screen.getAllByRole("button", { name: "创建用户" });
    const createButton = createButtons.at(0);
    if (createButton === undefined)
      throw new Error("create user action is missing");
    await user.click(createButton);
    const dialog = await screen.findByRole("dialog", { name: "创建用户" });
    await user.type(within(dialog).getByLabelText(/^用户名/), "bob");
    await user.type(within(dialog).getByLabelText(/^显示名称/), "Bob");
    await user.click(
      await within(dialog).findByLabelText("研发助手 · qwen-mini"),
    );
    await user.click(within(dialog).getByRole("button", { name: "创建用户" }));

    const secretDialog = await screen.findByRole("dialog", {
      name: "保存临时密码",
    });
    expect(
      within(secretDialog).getByDisplayValue(created.temporary_password),
    ).toBeVisible();
    expect(cacheContents(queryClient)).not.toContain(
      created.temporary_password,
    );
    expect(apiClient.userPermissionCalls).toEqual([
      { id: USER_ID, serviceIds: [SERVICE_ID] },
    ]);
    await user.click(
      within(secretDialog).getByRole("button", { name: "复制临时密码" }),
    );
    expect(
      await within(secretDialog).findByRole("button", { name: "全选密码" }),
    ).toBeVisible();
    await user.click(
      within(secretDialog).getByRole("button", { name: "关闭" }),
    );

    await waitFor(() =>
      expect(
        screen.queryByDisplayValue(created.temporary_password),
      ).not.toBeInTheDocument(),
    );
    expect(document.body).not.toHaveTextContent(created.temporary_password);
    expect(cacheContents(queryClient)).not.toContain(
      created.temporary_password,
    );
  });

  it("PROTO_15 UI-35 deletes a user after one confirmation without typing the username", async () => {
    const apiClient = new FakeApiClient(ADMIN_ME);
    apiClient.userDetailResults.push(userDetail());
    apiClient.aiServiceResults.push({ items: [] });
    apiClient.userCommandResults.push({ ...userOverview(), status: "deleted" });
    renderApplication(apiClient, `/users/${USER_ID}`);
    const user = userEvent.setup();

    await screen.findByRole("heading", { name: "Bob" });
    await user.click(screen.getByRole("button", { name: "删除" }));
    const dialog = await screen.findByRole("dialog", {
      name: "删除用户「bob」？",
    });
    expect(within(dialog).queryByRole("textbox")).not.toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "删除用户" }));

    await waitFor(() => {
      expect(apiClient.userCommandCalls).toEqual([
        { command: "delete", id: USER_ID },
      ]);
    });
  });

  it("PROTO_15 PROTO_18 submits matching full replacements from both permission entry points", async () => {
    const user = userEvent.setup();
    const fromUser = new FakeApiClient(ADMIN_ME);
    fromUser.userDetailResults.push(userDetail());
    fromUser.aiServiceResults.push({ items: [service()] });
    const first = renderApplication(fromUser, `/users/${USER_ID}`);
    await screen.findByRole("heading", { name: "Bob" });
    await user.click(screen.getByRole("tab", { name: /^服务权限/ }));
    await user.click(screen.getByLabelText("研发助手 · qwen-mini"));
    await user.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() => {
      expect(fromUser.userPermissionCalls).toEqual([
        { id: USER_ID, serviceIds: [SERVICE_ID] },
      ]);
    });
    first.unmount();

    const fromService = new FakeApiClient(ADMIN_ME);
    fromService.aiServiceDetailResults.push(service());
    fromService.modelInstanceResults.push({ items: [] });
    fromService.userResults.push({ items: [userOverview()] });
    renderApplication(fromService, `/services/${SERVICE_ID}`);
    await userEvent
      .setup()
      .click(await screen.findByRole("tab", { name: "访问权限" }));
    await screen.findByRole("heading", { name: "访问权限" });
    await user.click(await screen.findByLabelText("Bob (@bob)"));
    await user.click(screen.getByRole("button", { name: /^授权选中/ }));
    await user.click(screen.getByRole("button", { name: "保存访问权限" }));
    await waitFor(() => {
      expect(fromService.servicePermissionCalls).toEqual([
        { id: SERVICE_ID, userIds: [USER_ID] },
      ]);
      expect(fromService.servicePermissionReadCalls).toEqual([
        SERVICE_ID,
        SERVICE_ID,
      ]);
    });
  });

  it("PROTO_18 reads service permissions once instead of fetching every user detail", async () => {
    const apiClient = new FakeApiClient(ADMIN_ME);
    const getUser = vi.spyOn(apiClient, "getUser");
    apiClient.aiServiceDetailResults.push(service());
    apiClient.modelInstanceResults.push({ items: [] });
    apiClient.userResults.push({
      items: [
        userOverview(),
        {
          ...userOverview(),
          display_name: "Eve",
          id: uuid(9),
          username: "eve",
        },
      ],
    });
    apiClient.servicePermissionReadResults.push({
      user_ids: [USER_ID],
      limits: Object.fromEntries(
        [USER_ID].map((id) => [
          id,
          { max_concurrent_requests: null, max_requests_per_minute: null },
        ]),
      ),
    });

    renderApplication(apiClient, `/services/${SERVICE_ID}`);
    await userEvent
      .setup()
      .click(await screen.findByRole("tab", { name: "访问权限" }));

    expect(
      within(
        await screen.findByRole("region", { name: /^已授权/ }),
      ).getByLabelText("Bob (@bob)"),
    ).toBeVisible();
    expect(
      within(screen.getByRole("region", { name: /^未授权/ })).getByLabelText(
        "Eve (@eve)",
      ),
    ).toBeVisible();
    expect(apiClient.servicePermissionReadCalls).toEqual([SERVICE_ID]);
    expect(getUser).not.toHaveBeenCalled();
  });

  it("UI_46 PROTO_15 renders personal services without administrator service or instance calls", async () => {
    const apiClient = new FakeApiClient(USER_ME);
    apiClient.myAiServiceResults.push({
      items: [
        {
          available: true,
          enabled: true,
          id: "45000000-0000-4000-8000-000000000001",
          last_used_at: null,
          last_succeeded_at: null,
          description: "适合日常对话",
          name: "研发助手",
          public_model_name: "qwen-mini",
        },
      ],
    });
    renderApplication(apiClient, "/services");

    await screen.findByRole("heading", { name: "研发助手" });
    expect(screen.getByText("可用")).toBeVisible();
    expect(screen.getByRole("link", { name: "查看接入方式" })).toHaveAttribute(
      "href",
      `/services/${UI_46_PERSONAL_SERVICE.id}`,
    );
    expect(
      screen.queryByText(`${window.location.origin}/v1`),
    ).not.toBeInTheDocument();
    expect(apiClient.getMyAiServicesCalls).toBe(1);
    expect(apiClient.aiServiceCalls).toBe(0);
    expect(apiClient.modelInstanceCalls).toBe(0);
    expect(document.body).not.toHaveTextContent("绑定实例");
    expect(document.body).not.toHaveTextContent("GPU");
    expect(document.body).not.toHaveTextContent("Runtime");
  });

  it("UI_45 TC-UI-17 shows an accessible available status without a busy hint", async () => {
    const apiClient = new FakeApiClient(USER_ME);
    apiClient.myAiServiceResults.push({
      items: [
        {
          available: true,
          enabled: true,
          id: "45000000-0000-4000-8000-000000000001",
          last_used_at: null,
          last_succeeded_at: null,
          description: "适合日常对话",
          name: "研发助手",
          public_model_name: "qwen-mini",
        },
      ],
    });
    renderApplication(apiClient, "/services");

    expect(await screen.findByText("可用")).toBeVisible();
    expect(screen.queryByText("暂不可用")).not.toBeInTheDocument();
    const hint = screen.getByText("可用").closest(".pw-status");
    expect(hint?.querySelector(".pwi")).not.toBeNull();
    const axeResult = await axe.run(hint as HTMLElement, {
      rules: { "color-contrast": { enabled: false } },
    });
    expect(axeResult.violations).toEqual([]);
  });

  it("UI_46 PROTO_15 defaults to all authorized services and explains a 403", async () => {
    const apiClient = new FakeApiClient(USER_ME);
    apiClient.myAiServiceResults.push({ items: [UI_46_PERSONAL_SERVICE] });
    apiClient.apiKeyResults.push({ items: [] });
    apiClient.apiKeyCreateResults.push(
      new ApiError({ code: "service_not_authorized", status: 403 }),
    );
    renderApplication(apiClient, "/api-keys");
    const user = userEvent.setup();

    await screen.findByRole("heading", { name: "API 密钥" });
    await user.click(screen.getByRole("button", { name: "创建密钥" }));
    const dialog = await screen.findByRole("dialog", { name: "创建密钥" });
    expect(
      within(dialog).getByRole("radio", {
        name: "全部已授权服务（含以后新授权的）",
      }),
    ).toBeChecked();
    await user.type(within(dialog).getByLabelText(/^名称/), "个人调用");
    await user.click(within(dialog).getByRole("button", { name: "创建密钥" }));

    expect(
      await within(dialog).findByText(/当前账号没有相应服务权限/),
    ).toBeVisible();
    expect(apiClient.apiKeyCreateCalls[0]?.service_ids).toEqual([]);
    expect(apiClient.aiServiceCalls).toBe(0);
  });

  it.each([
    [
      "self_protection",
      "不能禁用或删除当前账号，本次操作未执行，请使用其他管理员账号。",
    ],
    [
      "last_active_administrator",
      "系统必须保留一个正常管理员，本次操作未执行，请先创建或启用其他管理员。",
    ],
  ])("PROTO_15 shows the %s protection message", async (code, message) => {
    const apiClient = new FakeApiClient(ADMIN_ME);
    apiClient.userDetailResults.push({ ...userDetail(), role: "admin" });
    apiClient.aiServiceResults.push({ items: [] });
    apiClient.userCommandResults.push(new ApiError({ code, status: 409 }));
    renderApplication(apiClient, `/users/${USER_ID}`);
    const user = userEvent.setup();

    await screen.findByRole("heading", { name: "Bob" });
    await user.click(screen.getByRole("button", { name: "禁用" }));
    await user.click(
      within(await screen.findByRole("dialog")).getByRole("button", {
        name: "禁用用户",
      }),
    );
    expect(await screen.findByText(message)).toBeVisible();
  });

  it("PROTO_15 has no blocking axe violations on the users page", async () => {
    const apiClient = new FakeApiClient(ADMIN_ME);
    apiClient.userResults.push({ items: [userOverview()] });
    apiClient.aiServiceResults.push({ items: [] });
    renderApplication(apiClient, "/users");
    await screen.findByText("@bob");
    const result = await axe.run(document, {
      rules: { "color-contrast": { enabled: false } },
    });
    expect(result.passes.flatMap((rule) => rule.nodes)).not.toHaveLength(0);
    expect(
      result.violations.filter(
        ({ impact }) => impact === "critical" || impact === "serious",
      ),
    ).toEqual([]);
  });
});

const USER_ID = uuid(1);
const SERVICE_ID = uuid(2);
const INSTANCE_ID = uuid(3);

function uuid(seed: number) {
  return `0198f58a-2586-7e37-bbf5-${seed.toString().padStart(12, "0")}`;
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

function userDetail(): UserDetail {
  return {
    ...userOverview(),
    created_at: "2026-10-01T00:00:00Z",
    api_keys: [],
    authorized_services: [],
  };
}

function createdUser(): CreatedUser {
  return {
    ...userOverview(),
    must_change_password: true,
    temporary_password: "Temporary-Secret-42",
  };
}

function service(): AiService {
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
  };
}

function cacheContents(queryClient: {
  getMutationCache: () => {
    getAll: () => Array<{ state: { data?: unknown } }>;
  };
  getQueryCache: () => { getAll: () => Array<{ state: { data?: unknown } }> };
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
