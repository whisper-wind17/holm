import axe from "axe-core";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { UserDetail } from "../api/user-contracts";
import { ADMIN_ME, FakeApiClient, renderApplication } from "./test-support";

// FX-M4-03D: existing administrator session plus explicit service grants.
const service = {
  max_concurrent_requests: null,
  max_requests_per_minute: null,
  description: "研发服务",
  enabled: true,
  id: "0198f58a-2586-7e37-bbf5-000000000042",
  name: "研发助手",
  public_model_name: "public-alpha",
};
function detail(granted: boolean): UserDetail {
  return {
    ...ADMIN_ME.user,
    api_key_count: 0,
    authorized_service_count: granted ? 1 : 0,
    created_at: "2026-10-01T00:00:00Z",
    authorized_services: granted ? [service] : [],
    api_keys: [],
    last_login_at: null,
    must_change_password: false,
    role: "admin",
    status: "active",
  };
}

describe("M4-03D administrator personal grants", () => {
  it("M4-03D hides the menu entry without grants and shows it after granting", async () => {
    const client = new FakeApiClient(ADMIN_ME);
    const getUser = vi
      .spyOn(client, "getUser")
      .mockResolvedValue(detail(false));
    renderApplication(client, "/users");
    const user = userEvent.setup();
    await screen.findByRole("heading", { name: "用户" });
    await user.click(screen.getByLabelText("打开用户菜单"));
    await waitFor(() => {
      expect(getUser).toHaveBeenCalledWith(ADMIN_ME.user.id);
    });
    expect(
      screen.queryByRole("link", { name: "个人 API 密钥" }),
    ).not.toBeInTheDocument();
    await user.click(screen.getByLabelText("打开用户菜单"));
    getUser.mockResolvedValue(detail(true));
    await user.click(screen.getByLabelText("打开用户菜单"));
    const link = await screen.findByRole("link", { name: "个人 API 密钥" });
    expect(
      within(screen.getByRole("navigation", { name: "主导航" })).queryByRole(
        "link",
        { name: /API 密钥/ },
      ),
    ).not.toBeInTheDocument();
    expect((await axe.run(document.body)).violations).toEqual([]);
    await user.click(link);
    await screen.findByRole("heading", { name: "API 密钥" });
    const create = screen.getAllByRole("button", { name: "创建密钥" })[0];
    if (create === undefined) throw new Error("missing create key action");
    await user.click(create);
    const dialog = await screen.findByRole("dialog");
    expect(
      within(dialog).getByLabelText("研发助手 · public-alpha"),
    ).toBeVisible();
  });

  it("M4-03D shows real administrator service counts and edits administrator grants", async () => {
    const client = new FakeApiClient(ADMIN_ME);
    const getUser = vi
      .spyOn(client, "getUser")
      .mockResolvedValue(detail(false));
    vi.spyOn(client, "getAiServices").mockResolvedValue({
      items: [
        {
          ...service,
          primary_instance_id: service.id,
          primary_instance_status: "Running",
          runtime_status: "Available",
          route_version: 1,
        },
      ],
    });
    renderApplication(client, `/users/${ADMIN_ME.user.id}`);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("tab", { name: /^服务权限/ }));
    expect(
      await screen.findByLabelText("研发助手 · public-alpha"),
    ).not.toBeChecked();
    expect(screen.queryByText("管理员无需单独授权")).not.toBeInTheDocument();
    await user.click(screen.getByLabelText("研发助手 · public-alpha"));
    getUser.mockResolvedValue(detail(true));
    await user.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() => {
      expect(client.userPermissionCalls).toEqual([
        { id: ADMIN_ME.user.id, serviceIds: [service.id] },
      ]);
    });
    expect((await axe.run(document.body)).violations).toEqual([]);
  });

  it("M4-03D includes administrators in service grant targets", async () => {
    const client = new FakeApiClient(ADMIN_ME);
    client.userResults.push({ items: [detail(false)] });
    vi.spyOn(client, "getAiService").mockResolvedValue({
      ...service,
      primary_instance_id: service.id,
      primary_instance_status: "Running",
      runtime_status: "Available",
      route_version: 1,
    });
    renderApplication(client, `/services/${service.id}`);
    await userEvent
      .setup()
      .click(await screen.findByRole("tab", { name: "访问权限" }));
    const user = userEvent.setup();
    const label = `${ADMIN_ME.user.display_name} (@${ADMIN_ME.user.username})`;
    await user.click(await screen.findByLabelText(label));
    await user.click(screen.getByRole("button", { name: /^授权选中/ }));
    await user.click(screen.getByRole("button", { name: "保存访问权限" }));
    await waitFor(() => {
      expect(client.servicePermissionCalls).toEqual([
        { id: service.id, userIds: [ADMIN_ME.user.id] },
      ]);
    });
    expect(client.userListCalls[0]).toEqual({});
  });
});
