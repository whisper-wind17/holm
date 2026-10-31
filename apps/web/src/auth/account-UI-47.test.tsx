import {
  act,
  fireEvent,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import axe from "axe-core";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client";
import type { PersonalProfile } from "../api/user-contracts";
import {
  ADMIN_ME,
  USER_ME,
  FakeApiClient,
  renderApplication,
} from "../app/test-support";

// FX-UI-47: UI-45 profile contract with fixed, non-sensitive account timestamps.
const profile: PersonalProfile = {
  username: USER_ME.user.username,
  display_name: USER_ME.user.display_name,
  role: "user",
  current_login_at: new Date(2026, 9, 16, 9, 12).toISOString(),
  previous_login_at: new Date(2026, 9, 14, 18, 40).toISOString(),
  created_at: "2026-01-01T00:00:00Z",
  authorized_service_count: 2,
};
afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("UI_47 TC-UI-27 account pages", () => {
  it("shows read-only profile fields, edits inline, cancels, rejects 81 characters, and refreshes the menu", async () => {
    const api = new FakeApiClient(USER_ME);
    api.myProfileResults = [
      profile,
      { ...profile, display_name: "新名称" },
      { ...profile, display_name: profile.username },
    ];
    const refresh = vi.spyOn(api, "getMe");
    const { container } = renderApplication(api, "/profile");
    const user = userEvent.setup();
    await screen.findByText("2 个服务");
    expect(screen.getByRole("link", { name: "2 个服务" })).toHaveAttribute(
      "href",
      "/services",
    );
    expect(screen.getByText(/（上次 10-14 18:40）/)).toBeVisible();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect((await axe.run(container)).violations).toEqual([]);
    await user.click(screen.getByRole("button", { name: "修改" }));
    await user.clear(screen.getByLabelText("显示名称"));
    await user.type(screen.getByLabelText("显示名称"), "x".repeat(81));
    await user.click(screen.getByRole("button", { name: "保存" }));
    expect(await screen.findByText("最多 80 个字符。")).toBeVisible();
    expect(api.updateMyProfileCalls).toEqual([]);
    await user.click(screen.getByRole("button", { name: "取消" }));
    await user.click(screen.getByRole("button", { name: "修改" }));
    expect(screen.getByLabelText("显示名称")).toHaveValue(profile.display_name);
    await user.clear(screen.getByLabelText("显示名称"));
    await user.type(screen.getByLabelText("显示名称"), "新名称");
    refresh.mockResolvedValue({
      ...USER_ME,
      user: { ...USER_ME.user, display_name: "新名称" },
    });
    await user.click(screen.getByRole("button", { name: "保存" }));
    await screen.findByText("已保存");
    expect(
      within(screen.getByLabelText("打开用户菜单")).getByText("新名称"),
    ).toBeVisible();
    await user.click(screen.getByRole("button", { name: "修改" }));
    await user.clear(screen.getByLabelText("显示名称"));
    refresh.mockResolvedValue({
      ...USER_ME,
      user: { ...USER_ME.user, display_name: profile.username },
    });
    await user.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() =>
      expect(screen.queryByRole("textbox")).not.toBeInTheDocument(),
    );
    expect(api.updateMyProfileCalls).toEqual([
      { display_name: "新名称" },
      { display_name: "" },
    ]);
    expect(
      within(screen.getByLabelText("打开用户菜单")).getByText(profile.username),
    ).toBeVisible();
  });

  it("shows one login timestamp without a previous login and links the administrator to their own detail", async () => {
    const api = new FakeApiClient(ADMIN_ME);
    api.myProfileResults = [
      { ...profile, role: "admin", previous_login_at: null },
    ];
    renderApplication(api, "/profile");
    await screen.findByText("2 个服务");
    expect(screen.queryByText(/上次/)).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "2 个服务" })).toHaveAttribute(
      "href",
      `/users/${ADMIN_ME.user.id}`,
    );
  });

  it("UI_47 opens the administrator's own detail on the service permissions tab", async () => {
    const api = new FakeApiClient(ADMIN_ME);
    api.myProfileResults = [{ ...profile, role: "admin" }];
    api.userDetailResults.push({
      api_key_count: 0,
      api_keys: [],
      authorized_service_count: 0,
      authorized_services: [],
      created_at: "2026-10-01T00:00:00Z",
      display_name: "Admin",
      id: ADMIN_ME.user.id,
      last_login_at: null,
      must_change_password: false,
      role: "admin",
      status: "active",
      username: "admin",
    });
    renderApplication(api, "/profile");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("link", { name: "2 个服务" }));
    expect(
      await screen.findByRole("tab", { name: /服务权限/, selected: true }),
    ).toBeVisible();
  });

  it("recovers a profile read failure and keeps a failed save editable", async () => {
    const api = new FakeApiClient(USER_ME);
    api.myProfileResults = [
      new ApiError({ code: "unavailable", status: 503 }),
      profile,
      new ApiError({ code: "unavailable", status: 503 }),
    ];
    renderApplication(api, "/profile");
    const user = userEvent.setup();
    await screen.findByText("无法获取个人资料");
    await user.click(screen.getByRole("button", { name: "重试" }));
    await user.click(await screen.findByRole("button", { name: "修改" }));
    await user.click(screen.getByRole("button", { name: "保存" }));
    expect(await screen.findByText("显示名称未保存")).toBeVisible();
    expect(screen.getByLabelText("显示名称")).toBeVisible();
  });

  it("keeps password success on the page with the current session and no password fields", async () => {
    const api = new FakeApiClient(USER_ME);
    const { container } = renderApplication(api, "/change-password");
    const user = userEvent.setup();
    await user.type(
      await screen.findByLabelText(/^当前密码/),
      "old secure passphrase",
    );
    await user.type(
      screen.getByLabelText(/^新密码/),
      "new secure passphrase 2026",
    );
    await user.type(
      screen.getByLabelText(/^确认新密码/),
      "new secure passphrase 2026",
    );
    await user.click(screen.getByRole("button", { name: "修改密码" }));
    expect(await screen.findByText("密码已修改。")).toBeVisible();
    expect(
      screen.getByText(
        "其他设备上的登录已退出，当前设备保持登录；API 密钥不受影响，程序调用照常进行。",
      ),
    ).toBeVisible();
    expect(screen.getByRole("heading", { name: "修改密码" })).toBeVisible();
    expect(screen.queryByLabelText(/^新密码/)).not.toBeInTheDocument();
    expect(api.writes).toEqual(["password"]);
    expect((await axe.run(container)).violations).toEqual([]);
  });

  it("counts server locks down from 60 then 120 seconds and prevents submission until zero", async () => {
    const api = new FakeApiClient();
    api.loginError = new ApiError({
      code: "login_locked",
      status: 429,
      retryAfterSeconds: 60,
    });
    renderApplication(api, "/login");
    const username = await screen.findByLabelText(/^用户名/);
    fireEvent.change(username, { target: { value: "user" } });
    fireEvent.change(screen.getByLabelText(/^密码/), {
      target: { value: "wrong" },
    });
    vi.useFakeTimers();
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "登录" }));
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(screen.getByText("请在 1 分 00 秒后重试。")).toBeVisible();
    expect(screen.getByRole("button", { name: "请稍后再试" })).toBeDisabled();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    expect(screen.getByText("请在 59 秒后重试。")).toBeVisible();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(59000);
    });
    expect(screen.getByRole("button", { name: "登录" })).toBeEnabled();
    api.loginError = new ApiError({
      code: "login_locked",
      status: 429,
      retryAfterSeconds: 120,
    });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "登录" }));
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(screen.getByText("请在 2 分 00 秒后重试。")).toBeVisible();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    expect(screen.getByText("请在 1 分 59 秒后重试。")).toBeVisible();
  });
});
