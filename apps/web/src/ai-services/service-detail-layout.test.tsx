import axe from "axe-core";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { AiService } from "../api/ai-service-contracts";
import { ApiError } from "../api/client";
import type { ModelInstance } from "../api/model-instance-contracts";
import {
  ADMIN_ME,
  USER_ME,
  FakeApiClient,
  renderApplication,
} from "../app/test-support";

// FX-UI-04: metadata-only fixtures based on the PROTO-07 service/instance contracts.
const service: AiService = {
  id: "0198f58a-2586-7e37-bbf5-000000000001",
  primary_instance_id: "0198f58a-2586-7e37-bbf5-000000000002",
  primary_instance_status: "Running",
  name: "研发助手",
  public_model_name: "public-code",
  description: "已有描述",
  enabled: true,
  runtime_status: "Available",
  route_version: 1,
};
const instance: ModelInstance = {
  id: service.primary_instance_id,
  artifact_id: "0198f58a-2586-7e37-bbf5-000000000003",
  config_version: 1,
  context_length: 8192,
  error_code: null,
  gpu_memory_utilization: 0.9,
  restart_count: 0,
  gpu_uuids: ["FX-UI-04-GPU"],
  internal_endpoint: null,
  internal_model_name: "fx-ui-04",
  name: "研发实例",
  runtime_type: "vllm",
  status: "Running",
  tensor_parallel_size: 1,
  updated_at: "2026-10-08T00:00:00Z",
};

async function setup() {
  const api = new FakeApiClient({
    ...ADMIN_ME,
    capabilities: [
      ...ADMIN_ME.capabilities,
      { capability: "view_usage", scope: "all" },
    ],
  });
  const getService = vi.spyOn(api, "getAiService").mockResolvedValue(service);
  api.modelInstanceResults.push({
    items: [
      instance,
      {
        ...instance,
        id: "0198f58a-2586-7e37-bbf5-000000000004",
        name: "备用实例",
      },
    ],
  });
  const view = renderApplication(api, `/services/${service.id}`);
  const user = userEvent.setup();
  await screen.findByRole("heading", { name: service.name });
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "修改服务名称" })).toBeEnabled(),
  );
  return { api, user, getService, ...view };
}

describe("UI-04 service detail layout", () => {
  it("UI_04_TC_UI_01_03 groups compact entries with their values and restores keyboard focus", async () => {
    const { user, container } = await setup();
    const title = screen.getByRole("heading", { name: service.name });
    if (title.parentElement === null) throw new Error("Missing title group");
    const nameEdit = within(title.parentElement).getByRole("button", {
      name: "修改服务名称",
    });
    expect(nameEdit).toHaveClass("pw-button--s", "pw-button--ghost");
    expect(nameEdit.textContent).toBe("修改");
    expect(screen.getAllByRole("tab").map((tab) => tab.textContent)).toEqual([
      "概览",
      "监控",
      "调用记录",
      "访问权限",
      "用量",
      "设置",
    ]);
    expect(
      screen.queryByRole("button", { name: "保存设置" }),
    ).not.toBeInTheDocument();
    const access = screen.getByRole("region", { name: "接入信息" });
    const rename = within(access).getByRole("button", {
      name: "修改 API 模型名",
    });
    expect(rename).toHaveTextContent(/^修改$/);
    expect(rename).toHaveClass("pw-button--s", "pw-button--ghost");
    const clipboard = vi.spyOn(navigator.clipboard, "writeText");
    await user.click(
      within(access).getByRole("button", {
        name: "复制 OpenAI 兼容 Endpoint",
      }),
    );
    expect(clipboard).toHaveBeenLastCalledWith(`${window.location.origin}/v1`);
    await user.click(
      within(access).getByRole("button", {
        name: "复制 Anthropic 兼容 Endpoint",
      }),
    );
    expect(clipboard).toHaveBeenLastCalledWith(window.location.origin);
    await user.click(
      within(access).getByRole("button", { name: "复制 API 模型名" }),
    );
    expect(clipboard).toHaveBeenLastCalledWith(service.public_model_name);
    nameEdit.focus();
    await user.keyboard("{Enter}");
    expect(screen.getByRole("dialog", { name: "修改服务名称" })).toBeVisible();
    await user.keyboard("{Escape}");
    expect(nameEdit).toHaveFocus();
    rename.focus();
    await user.keyboard("{Enter}");
    expect(
      screen.getByText(
        "修改只更新路由，不重启模型；仍使用旧名称的客户端会收到 404，需要同步修改配置。",
      ),
    ).toBeVisible();
    await user.keyboard("{Escape}");
    expect(rename).toHaveFocus();
    expect(
      (await axe.run(container)).violations.filter(
        ({ impact }) => impact === "critical" || impact === "serious",
      ),
    ).toEqual([]);
  });

  it("UI_04_TC_UI_01 saves binding and description only from settings and preserves failed drafts", async () => {
    const { api, user, getService, container } = await setup();
    await user.click(screen.getByRole("tab", { name: "设置" }));
    const panel = screen.getByRole("tabpanel", { name: "设置" });
    expect(
      within(panel).queryByRole("button", { name: "修改服务名称" }),
    ).not.toBeInTheDocument();
    expect(
      within(panel).queryByRole("button", { name: "修改 API 模型名" }),
    ).not.toBeInTheDocument();
    expect(within(panel).getAllByRole("button")).toHaveLength(1);
    await user.selectOptions(
      within(panel).getByLabelText("后端实例"),
      "0198f58a-2586-7e37-bbf5-000000000004",
    );
    await user.clear(within(panel).getByLabelText(/^描述/));
    await user.type(within(panel).getByLabelText(/^描述/), "更新描述");
    api.aiServiceUpdateResults.push(
      new ApiError({
        status: 409,
        code: "backend_unavailable",
        responseMessage: "绑定实例尚未就绪",
      }),
    );
    await user.click(within(panel).getByRole("button", { name: "保存设置" }));
    await user.click(screen.getByRole("button", { name: "切换并保存" }));
    expect(await screen.findByText("绑定实例尚未就绪")).toBeVisible();
    expect(within(panel).getByLabelText(/^描述/)).toHaveValue("更新描述");
    expect(within(panel).getByLabelText("后端实例")).toHaveValue(
      "0198f58a-2586-7e37-bbf5-000000000004",
    );
    await user.click(screen.getByRole("button", { name: "取消" }));
    getService.mockResolvedValue({
      ...service,
      description: "更新描述",
      primary_instance_id: "0198f58a-2586-7e37-bbf5-000000000004",
    });
    api.aiServiceUpdateResults.push({
      ...service,
      description: "更新描述",
      primary_instance_id: "0198f58a-2586-7e37-bbf5-000000000004",
    });
    await user.click(within(panel).getByRole("button", { name: "保存设置" }));
    await user.click(screen.getByRole("button", { name: "切换并保存" }));
    expect(await screen.findByText("换绑已生效")).toBeVisible();
    expect(api.aiServiceUpdateCalls).toEqual(
      Array.from({ length: 2 }, () => ({
        id: service.id,
        body: {
          description: "更新描述",
          primary_instance_id: "0198f58a-2586-7e37-bbf5-000000000004",
        },
      })),
    );
    expect(
      (await axe.run(container)).violations.filter(
        ({ impact }) => impact === "critical" || impact === "serious",
      ),
    ).toEqual([]);
    await user.click(screen.getByRole("tab", { name: "概览" }));
    expect(screen.getByText("更新描述")).toBeVisible();
    expect(
      screen.queryByRole("button", { name: "保存设置" }),
    ).not.toBeInTheDocument();
  });

  it("UI_04_TC_SEC_11 hides both edit entries from ordinary users", async () => {
    const api = new FakeApiClient(USER_ME);
    renderApplication(api, `/services/${service.id}`);
    await screen.findByText("无权限");
    expect(
      screen.queryByRole("button", { name: "修改服务名称" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "修改 API 模型名" }),
    ).not.toBeInTheDocument();
  });
});
