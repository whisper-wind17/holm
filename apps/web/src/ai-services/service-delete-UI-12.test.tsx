import axe from "axe-core";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { AiService } from "../api/ai-service-contracts";
import { ApiClient, ApiError } from "../api/client";
import {
  ADMIN_ME,
  FakeApiClient,
  renderApplication,
} from "../app/test-support";

// FX-UI-12: metadata-only service, derived from the UI-05 service contract fixture.
const service: AiService = {
  id: "0198f58a-2586-7e37-bbf5-000000000051",
  name: "研发助手",
  public_model_name: "code-assist",
  description: "",
  enabled: true,
  primary_instance_id: "0198f58a-2586-7e37-bbf5-000000000046",
  primary_instance_status: "Stopped",
  runtime_status: "Unavailable",
  route_version: 1,
};
function setup(item = service) {
  const api = new FakeApiClient(ADMIN_ME);
  const listing = vi
    .spyOn(api, "getAiServices")
    .mockResolvedValue({ items: [item] });
  const instances = vi
    .spyOn(api, "getModelInstances")
    .mockResolvedValue({ items: [] });
  vi.spyOn(api, "getServicePermissions").mockResolvedValue({
    user_ids: ["user-1", "user-2"],
    limits: Object.fromEntries(
      ["user-1", "user-2"].map((id) => [
        id,
        { max_concurrent_requests: null, max_requests_per_minute: null },
      ]),
    ),
  });
  const view = renderApplication(api, "/services");
  return { api, listing, instances, user: userEvent.setup(), ...view };
}
describe("UI-12 service deletion", () => {
  it.each([
    [true, "Running", false],
    [true, "Starting", false],
    [true, "Stopped", true],
    [true, "Failed", true],
    [false, "Running", true],
  ] as const)(
    "UI-12 enabled=%s backend=%s exposes delete=%s",
    async (enabled, status, canDelete) => {
      setup({ ...service, enabled, primary_instance_status: status });
      const row = (
        await screen.findByRole("link", { name: service.name })
      ).closest("tr") as HTMLElement;
      const button = within(row).queryByRole("button", {
        name: `删除 ${service.name}`,
      });
      if (canDelete) expect(button).toBeVisible();
      else expect(button).not.toBeInTheDocument();
    },
  );
  it("UI-12 reveals deletion after disabling the service", async () => {
    const enabled = { ...service, primary_instance_status: "Running" as const };
    const { api, user, listing } = setup(enabled);
    const row = (
      await screen.findByRole("link", { name: service.name })
    ).closest("tr") as HTMLElement;
    expect(
      within(row).queryByRole("button", { name: `删除 ${service.name}` }),
    ).not.toBeInTheDocument();
    api.aiServiceUpdateResults.push({ ...enabled, enabled: false });
    listing.mockResolvedValue({ items: [{ ...enabled, enabled: false }] });
    await user.click(within(row).getByRole("button", { name: "停用" }));
    expect(
      await screen.findByRole("button", { name: `删除 ${service.name}` }),
    ).toBeVisible();
    expect(api.aiServiceUpdateCalls).toEqual([
      { id: service.id, body: { enabled: false } },
    ]);
  });
  it("UI-12 confirms consequences, passes axe, deletes and refreshes both lists", async () => {
    const { api, user, listing, instances } = setup();
    await user.click(
      await screen.findByRole("button", { name: `删除 ${service.name}` }),
    );
    const dialog = screen.getByRole("dialog", {
      name: `删除 AI 服务「${service.name}」？`,
    });
    expect(within(dialog).getByText("2 人")).toBeVisible();
    expect(within(dialog).getByText("模型实例（已停止）")).toBeVisible();
    expect(within(dialog).getByText(service.public_model_name)).toBeVisible();
    expect(within(dialog).getByText("model_not_found")).toBeVisible();
    expect(within(dialog).getByText(/密钥本身不会被撤销/)).toBeVisible();
    expect(
      within(dialog).getByText(/历史用量、调用记录与审计保留/),
    ).toBeVisible();
    expect(within(dialog).getByText("删除后无法恢复。")).toBeVisible();
    expect(within(dialog).queryByRole("textbox")).not.toBeInTheDocument();
    expect((await axe.run(dialog)).violations).toEqual([]);
    listing.mockResolvedValue({ items: [] });
    const instanceReads = instances.mock.calls.length;
    await user.click(within(dialog).getByRole("button", { name: "删除服务" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
    expect(api.aiServiceDeleteCalls).toEqual([service.id]);
    expect(await screen.findByText("还没有 AI 服务")).toBeVisible();
    expect(screen.getByRole("link", { name: "前往模型实例" })).toHaveAttribute(
      "href",
      "/models",
    );
    expect(screen.getByText(/已删除 AI 服务/)).toBeVisible();
    await waitFor(() => {
      expect(instances.mock.calls.length).toBeGreaterThan(instanceReads);
    });
  });
  it("UI-12 keeps a 409 failure inside the open dialog", async () => {
    const { api, user } = setup();
    api.aiServiceDeleteResults.push(
      new ApiError({ status: 409, code: "service_delete_conflict" }),
    );
    await user.click(
      await screen.findByRole("button", { name: `删除 ${service.name}` }),
    );
    const dialog = screen.getByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "删除服务" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "服务没有被删除",
    );
    expect(dialog).toBeVisible();
    expect((await axe.run(dialog)).violations).toEqual([]);
    await user.click(within(dialog).getByRole("button", { name: "取消" }));
    expect(screen.getByRole("link", { name: service.name })).toBeVisible();
  });
  it("UI-12 sends DELETE with CSRF protection", async () => {
    const fetcher = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(new Response(null, { status: 204 }));
    const api = new ApiClient();
    vi.spyOn(document, "cookie", "get").mockReturnValue(
      "__Host-pws_csrf=FX-UI-12-csrf",
    );
    await api.deleteAiService(service.id);
    expect(fetcher).toHaveBeenCalledWith(
      `/api/v1/services/${service.id}`,
      expect.objectContaining({ method: "DELETE" }),
    );
    const headers = fetcher.mock.calls[0]?.[1]?.headers as Headers;
    expect(headers.get("X-CSRF-Token")).toBe("FX-UI-12-csrf");
  });
});
