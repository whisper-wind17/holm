import axe from "axe-core";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import {
  type AiService,
  serviceDisplayNameSchema,
} from "../api/ai-service-contracts";
import { ApiClient, ApiError } from "../api/client";
import {
  ADMIN_ME,
  USER_ME,
  FakeApiClient,
  renderApplication,
} from "../app/test-support";

// FX-M4-11B: metadata-only service; stopped backend exercises independent settings.
const service: AiService = {
  id: "0198f58a-2586-7e37-bbf5-000000000001",
  name: "研发助手",
  public_model_name: "public-code",
  description: "已有描述",
  enabled: false,
  primary_instance_id: "0198f58a-2586-7e37-bbf5-000000000002",
  primary_instance_status: "Stopped",
  route_version: 1,
  runtime_status: "Disabled",
};
async function setup() {
  const api = new FakeApiClient(ADMIN_ME);
  api.aiServiceDetailResults.push(service);
  const rename = vi
    .spyOn(api, "renameServiceDisplayName")
    .mockResolvedValue({ ...service, name: "文档助手", route_version: 2 });
  const view = renderApplication(api, `/services/${service.id}`);
  const user = userEvent.setup();
  await screen.findByRole("heading", { name: service.name });
  const trigger = screen.getByRole("button", { name: "修改服务名称" });
  await waitFor(() => expect(trigger).toBeEnabled());
  await user.click(trigger);
  const dialog = screen.getByRole("dialog", { name: "修改服务名称" });
  return { api, rename, user, trigger, dialog, ...view };
}
describe("M4-11B independent service display names", () => {
  it("M4_11B_TC_UI_01_03 saves without route confirmation and preserves public model name", async () => {
    const { user, rename, dialog, trigger, queryClient } = await setup();
    const field = within(dialog).getByLabelText(/^新服务名称/);
    expect(field).toHaveValue(service.name);
    expect(within(dialog).queryByLabelText(/再次/)).not.toBeInTheDocument();
    await user.clear(field);
    await user.type(field, "  文档助手  ");
    const scan = await axe.run(dialog);
    expect(
      scan.violations.filter(
        (v) => v.impact === "critical" || v.impact === "serious",
      ),
    ).toEqual([]);
    await user.click(within(dialog).getByRole("button", { name: "保存名称" }));
    await screen.findByRole("heading", { name: "文档助手" });
    expect(rename).toHaveBeenCalledExactlyOnceWith(service.id, {
      name: "文档助手",
      expected_route_version: 1,
    });
    expect(queryClient.getQueryData(["ai-services", service.id])).toMatchObject(
      { name: "文档助手", public_model_name: "public-code" },
    );
    expect(trigger).toHaveFocus();
    expect(screen.getAllByText("public-code").length).toBeGreaterThan(0);
  });
  it("M4_11B_TC_UI_13 keeps draft after conflict and refreshes only the observed version", async () => {
    const { api, rename, user, dialog } = await setup();
    rename.mockRejectedValueOnce(
      new ApiError({ code: "version_conflict", status: 409 }),
    );
    const field = within(dialog).getByLabelText(/^新服务名称/);
    await user.clear(field);
    await user.type(field, "文档助手");
    await user.click(within(dialog).getByRole("button", { name: "保存名称" }));
    expect(await within(dialog).findByText("配置已更新")).toBeVisible();
    expect(field).toHaveValue("文档助手");
    expect(
      within(dialog).getByRole("button", { name: "保存名称" }),
    ).toBeDisabled();
    api.aiServiceDetailResults.push({
      ...service,
      name: "其他管理员名称",
      route_version: 2,
    });
    await user.click(
      within(dialog).getByRole("button", { name: "刷新当前配置" }),
    );
    await waitFor(() =>
      expect(
        within(dialog).getByRole("button", { name: "保存名称" }),
      ).toBeEnabled(),
    );
    expect(field).toHaveValue("文档助手");
    await user.click(within(dialog).getByRole("button", { name: "保存名称" }));
    await waitFor(() => {
      expect(rename).toHaveBeenLastCalledWith(service.id, {
        name: "文档助手",
        expected_route_version: 2,
      });
    });
  });
  it("M4_11B_TC_UI_03 preserves failed input, cancels with focus return and resets on reopen", async () => {
    const { rename, user, dialog, trigger } = await setup();
    rename.mockRejectedValue(
      new ApiError({
        code: "service_display_name_conflict",
        status: 409,
        responseMessage: "服务名称已被使用",
      }),
    );
    const field = within(dialog).getByLabelText(/^新服务名称/);
    await user.clear(field);
    await user.type(field, "重复名称");
    await user.click(within(dialog).getByRole("button", { name: "保存名称" }));
    expect(await within(dialog).findByText("服务名称已被使用")).toBeVisible();
    expect(field).toHaveValue("重复名称");
    await user.keyboard("{Escape}");
    expect(trigger).toHaveFocus();
    await user.click(trigger);
    expect(screen.getByLabelText(/^新服务名称/)).toHaveValue(service.name);
  });
  it("M4_11B rejects blank, short, long and NUL but counts Unicode code points", () => {
    for (const value of ["", " ", "x", "😀".repeat(41), "bad\0name"])
      expect(serviceDisplayNameSchema.safeParse(value).success).toBe(false);
    expect(serviceDisplayNameSchema.parse("  😀😀  ")).toBe("😀😀");
    expect(serviceDisplayNameSchema.parse("😀".repeat(40))).toHaveLength(80);
  });
  it("M4_11B user card shows both names and keeps model examples public", async () => {
    const api = new FakeApiClient(USER_ME);
    api.myAiServiceResults.push({
      items: [
        {
          name: service.name,
          public_model_name: service.public_model_name,
          description: "适合开发",
          available: true,
          enabled: true,
          id: "45000000-0000-4000-8000-000000000001",
          last_used_at: null,
          last_succeeded_at: null,
        },
      ],
    });
    renderApplication(api, "/services");
    expect(
      await screen.findByRole("heading", { name: service.name }),
    ).toBeVisible();
    expect(
      screen.getAllByText(service.public_model_name).length,
    ).toBeGreaterThan(0);
    expect(
      screen.queryByRole("button", { name: "修改服务名称" }),
    ).not.toBeInTheDocument();
  });
  it("M4_11B client uses the authenticated display-name operation", async () => {
    const fetcher = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(
        new Response(
          JSON.stringify({ ...service, name: "文档助手", route_version: 2 }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      );
    const api = new ApiClient();
    vi.spyOn(document, "cookie", "get").mockReturnValue(
      "__Host-pws_csrf=FX-CSRF",
    );
    await api.renameServiceDisplayName(service.id, {
      name: "文档助手",
      expected_route_version: 1,
    });
    expect(fetcher.mock.calls[0]?.[0]).toContain(":rename-display-name");
    expect(
      (fetcher.mock.calls[0]?.[1]?.headers as Headers).get("X-CSRF-Token"),
    ).toBe("FX-CSRF");
  });
});
