import axe from "axe-core";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import type { AiService } from "../api/ai-service-contracts";
import { ApiError } from "../api/client";
import type { ModelInstance } from "../api/model-instance-contracts";
import type { UserList } from "../api/user-contracts";
import {
  ADMIN_ME,
  FakeApiClient,
  renderApplication,
} from "../app/test-support";

// FX-UI-21: admin fixtures for the PDD 8.3 create-service dialog; ids are synthetic.
describe("UI-21 create service dialog (PDD 8.3)", () => {
  it("UI-21 lists only Running instances, omits a blank name and points an unauthorized result to access", async () => {
    const apiClient = clientWith([
      instance({ id: RUNNING_ID, name: "运行实例" }),
      instance({ id: STOPPED_ID, name: "停止实例", status: "Stopped" }),
    ]);
    const created = service({
      name: "qwen-chat",
      public_model_name: "qwen-chat",
    });
    apiClient.aiServiceCreateResults.push(created);
    apiClient.aiServiceDetailResults.push(created);
    renderApplication(apiClient, "/services");
    const user = userEvent.setup();

    const dialog = await openCreateDialog(user);
    const select = within(dialog).getByLabelText(/^后端实例/);
    expect(
      within(select)
        .getAllByRole("option")
        .map(({ textContent }) => textContent),
    ).toEqual(["请选择运行中的模型实例", "运行实例"]);
    expect(
      within(dialog).getByText("服务类型", { selector: "span" }).parentElement,
    ).toHaveTextContent("Chat Completions · 当前版本只提供这一种");
    expect(
      within(dialog).getByRole("radio", { name: /^暂不授权/ }),
    ).toBeChecked();
    expect(within(dialog).getByRole("radio", { name: /^启用/ })).toBeChecked();

    await user.selectOptions(select, RUNNING_ID);
    await user.type(within(dialog).getByLabelText(/^API 模型名/), "qwen-chat");
    const preview = within(dialog).getByRole("region", { name: "服务预览" });
    expect(
      within(preview).getByText("qwen-chat", { selector: "strong" }),
    ).toBeVisible();
    expect(preview).toHaveTextContent(`${window.location.origin}/v1`);
    expect(preview).toHaveTextContent('"model":"qwen-chat"');
    expect(preview.textContent).not.toMatch(/后端实例|GPU|Runtime|运行实例/);

    await user.click(within(dialog).getByRole("button", { name: "创建服务" }));

    const result = await screen.findByRole("dialog", {
      name: "AI 服务已创建",
    });
    expect(apiClient.aiServiceCreateCalls).toHaveLength(1);
    expect(apiClient.aiServiceCreateCalls[0]).not.toHaveProperty("name");
    expect(apiClient.aiServiceCreateCalls[0]).toEqual({
      description: "",
      enabled: true,
      primary_instance_id: RUNNING_ID,
      public_model_name: "qwen-chat",
    });
    expect(apiClient.servicePermissionCalls).toEqual([]);
    expect(result).toHaveTextContent("服务已可用。");
    expect(result).toHaveTextContent("服务已发布，但尚无用户可以访问");
    expect(result).toHaveTextContent(
      "用户需要自行创建 API 密钥，管理员不能代为创建",
    );
    expect(result).toHaveTextContent("运行实例");

    await user.click(within(result).getByRole("button", { name: "前往授权" }));
    expect(
      await screen.findByRole("heading", { level: 1, name: /qwen-chat/ }),
    ).toBeVisible();
  });

  it("UI-21 grants chosen active users, sends a disabled initial state and warns about shared compute", async () => {
    const apiClient = clientWith(
      [instance({ id: RUNNING_ID, name: "运行实例" })],
      {
        services: [service({ id: uuid(9), name: "已有服务" })],
      },
    );
    apiClient.userResults.push(users());
    const created = service({ enabled: false, runtime_status: "Disabled" });
    apiClient.aiServiceCreateResults.push(created);
    renderApplication(apiClient, "/services");
    const user = userEvent.setup();

    const dialog = await openCreateDialog(user);
    await user.selectOptions(
      within(dialog).getByLabelText(/^后端实例/),
      RUNNING_ID,
    );
    expect(dialog).toHaveTextContent("多个服务会共享它的算力");
    await user.type(within(dialog).getByLabelText(/^API 模型名/), "qwen-mini");
    await user.type(within(dialog).getByLabelText(/^服务名称/), "研发助手");
    await user.click(
      within(dialog).getByRole("radio", { name: "现在授权给指定用户" }),
    );
    await user.click(
      await within(dialog).findByRole("checkbox", { name: /zhang\.wei/ }),
    );
    await user.click(within(dialog).getByRole("radio", { name: /^停用/ }));
    const preview = within(dialog).getByRole("region", { name: "服务预览" });
    expect(preview).toHaveTextContent("已停用");
    expect(apiClient.userListCalls).toContainEqual({ status: "active" });

    await user.click(within(dialog).getByRole("button", { name: "创建服务" }));

    const result = await screen.findByRole("dialog", {
      name: "AI 服务已创建",
    });
    expect(apiClient.aiServiceCreateCalls).toEqual([
      {
        name: "研发助手",
        description: "",
        enabled: false,
        primary_instance_id: RUNNING_ID,
        public_model_name: "qwen-mini",
      },
    ]);
    expect(apiClient.servicePermissionCalls).toEqual([
      { id: SERVICE_ID, userIds: [USER_ID] },
    ]);
    expect(result).toHaveTextContent("服务已停用，启用后才可调用");
    expect(result).toHaveTextContent(
      "已授权 1 位用户，他们可以用 API 模型名「qwen-mini」调用",
    );
    expect(result).toHaveTextContent("管理员不能代为创建");
    expect(
      within(result).getByRole("button", { name: "查看服务" }),
    ).toBeVisible();
    expect(
      within(result).queryByRole("button", { name: "前往授权" }),
    ).not.toBeInTheDocument();
  });

  it("UI-21 keeps the created service when the grant fails and retries only the grant", async () => {
    const apiClient = clientWith([
      instance({ id: RUNNING_ID, name: "运行实例" }),
    ]);
    apiClient.userResults.push(users());
    apiClient.aiServiceCreateResults.push(service());
    apiClient.servicePermissionResults.push(
      new ApiError({ code: "internal_error", status: 500 }),
    );
    renderApplication(apiClient, "/services");
    const user = userEvent.setup();

    const dialog = await openCreateDialog(user);
    await user.selectOptions(
      within(dialog).getByLabelText(/^后端实例/),
      RUNNING_ID,
    );
    await user.type(within(dialog).getByLabelText(/^API 模型名/), "qwen-mini");
    await user.click(
      within(dialog).getByRole("radio", { name: "现在授权给指定用户" }),
    );
    await user.click(
      await within(dialog).findByRole("checkbox", { name: /zhang\.wei/ }),
    );
    await user.click(within(dialog).getByRole("button", { name: "创建服务" }));

    const result = await screen.findByRole("dialog", {
      name: "AI 服务已创建",
    });
    expect(result).toHaveTextContent("服务已创建，但用户授权未保存");
    expect(
      within(result).getByRole("button", { name: "前往授权" }),
    ).toBeVisible();
    await user.click(within(result).getByRole("button", { name: "重试授权" }));

    expect(
      await within(result).findByRole("button", { name: "查看服务" }),
    ).toBeVisible();
    expect(result).not.toHaveTextContent("用户授权未保存");
    expect(apiClient.servicePermissionCalls).toEqual([
      { id: SERVICE_ID, userIds: [USER_ID] },
      { id: SERVICE_ID, userIds: [USER_ID] },
    ]);
    expect(apiClient.aiServiceCreateCalls).toHaveLength(1);
    // The service row is already in the list; nothing was rolled back.
    expect(
      screen.getAllByText("研发助手", { selector: "a" }).length,
    ).toBeGreaterThan(0);
  });

  it("UI-21 shows API model name and service name errors under their fields", async () => {
    const apiClient = clientWith([
      instance({ id: RUNNING_ID, name: "运行实例" }),
    ]);
    apiClient.aiServiceCreateResults.push(
      new ApiError({ code: "service_name_conflict", status: 409 }),
      new ApiError({ code: "service_display_name_conflict", status: 409 }),
    );
    renderApplication(apiClient, "/services");
    const user = userEvent.setup();

    const dialog = await openCreateDialog(user);
    await user.selectOptions(
      within(dialog).getByLabelText(/^后端实例/),
      RUNNING_ID,
    );
    const modelName = within(dialog).getByLabelText(/^API 模型名/);
    await user.type(modelName, "Bad Name");
    await user.click(within(dialog).getByRole("button", { name: "创建服务" }));
    expect(modelName).toHaveAccessibleDescription(/API 模型名需为 2～64 位/);
    expect(apiClient.aiServiceCreateCalls).toEqual([]);

    await user.clear(modelName);
    await user.type(modelName, "qwen-mini");
    await user.click(within(dialog).getByRole("button", { name: "创建服务" }));
    await waitFor(() => {
      expect(modelName).toHaveAccessibleDescription(/API 模型名已被使用/);
    });
    expect(within(dialog).queryByText("AI 服务未创建")).not.toBeInTheDocument();

    const displayName = within(dialog).getByLabelText(/^服务名称/);
    await user.type(displayName, "研发助手");
    await user.click(within(dialog).getByRole("button", { name: "创建服务" }));
    await waitFor(() => {
      expect(displayName).toHaveAccessibleDescription(/服务名称已被使用/);
    });
    expect(apiClient.aiServiceCreateCalls).toHaveLength(2);
  });

  it("UI-21 opens the prerequisite explanation instead of the form without a Running instance", async () => {
    const apiClient = clientWith([
      instance({ id: STOPPED_ID, name: "停止实例", status: "Stopped" }),
    ]);
    renderApplication(apiClient, "/services");
    const user = userEvent.setup();

    // The list has finished loading instances once its empty state shows.
    await screen.findByText("还没有 AI 服务");
    // UI-21 / prototype: the prerequisite dialog replaces the old list banner.
    expect(screen.queryByText("还没有运行中的模型实例")).toBeNull();
    expect(screen.queryByText(/可以先填写服务信息/)).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "创建服务" }));
    const dialog = await screen.findByRole("dialog", {
      name: "还不能创建 AI 服务",
    });
    expect(dialog).toHaveTextContent("AI 服务需要绑定一个运行中的模型实例");
    expect(within(dialog).queryByRole("textbox")).not.toBeInTheDocument();
    expect(
      within(dialog).getByRole("button", { name: "前往模型实例" }),
    ).toBeVisible();
    expect(
      within(dialog).getByRole("button", { name: "部署模型" }),
    ).toBeVisible();
    const result = await axe.run(dialog, {
      rules: { "color-contrast": { enabled: false } },
    });
    expect(result.violations).toEqual([]);
  });

  it("UI-21 offers only deployment when there is no model instance at all", async () => {
    const apiClient = clientWith([]);
    renderApplication(apiClient, "/services");
    const user = userEvent.setup();

    await screen.findByText("还没有 AI 服务");
    await user.click(screen.getByRole("button", { name: "创建服务" }));
    const dialog = await screen.findByRole("dialog", {
      name: "还不能创建 AI 服务",
    });
    expect(dialog).toHaveTextContent("当前还没有模型实例");
    expect(
      within(dialog).queryByRole("button", { name: "前往模型实例" }),
    ).not.toBeInTheDocument();
    expect(
      within(dialog).getByRole("button", { name: "部署模型" }),
    ).toBeVisible();
  });

  it("UI-21 preselects the instance when publishing from the instance list", async () => {
    const running = instance({ id: RUNNING_ID, name: "运行实例" });
    const apiClient = clientWith([running]);
    // The services page may refetch instances after navigation.
    apiClient.modelInstanceResults.push({ items: [running] });
    renderApplication(apiClient, "/models");
    const user = userEvent.setup();

    const table = await screen.findByRole("table", { name: "模型实例" });
    await user.click(
      await within(table).findByRole("button", { name: "发布服务" }),
    );

    const dialog = await screen.findByRole("dialog", { name: "创建服务" });
    expect(within(dialog).getByLabelText(/^后端实例/)).toHaveValue(RUNNING_ID);
    expect(dialog).toHaveTextContent(
      "从模型实例「运行实例」进入，已预选这个实例。",
    );
    const result = await axe.run(dialog, {
      rules: { "color-contrast": { enabled: false } },
    });
    expect(result.violations).toEqual([]);
  });

  it("UI-21 treats a requested instance that is no longer Running as the prerequisite", async () => {
    const apiClient = clientWith([
      instance({ id: RUNNING_ID, name: "运行实例" }),
      instance({ id: STOPPED_ID, name: "停止实例", status: "Stopped" }),
    ]);
    renderApplication(apiClient, `/services?create=${STOPPED_ID}`);

    const dialog = await screen.findByRole("dialog", {
      name: "还不能创建 AI 服务",
    });
    expect(dialog).toHaveTextContent("模型实例「停止实例」当前没有运行");
    expect(
      within(dialog).getByRole("button", { name: "部署模型" }),
    ).toBeVisible();
  });

  it("UI-21 shows a plain dash named by the reason when today's usage is not permitted", async () => {
    const apiClient = clientWith(
      [instance({ id: RUNNING_ID, name: "运行实例" })],
      {
        services: [service()],
      },
    );
    renderApplication(apiClient, "/services");

    const link = await screen.findByRole("link", { name: "研发助手" });
    const cell = (link.closest("tr") as HTMLElement).querySelector(
      '[data-column="today-requests"]',
    ) as HTMLElement;
    const mark = within(cell).getByText("—");
    expect(mark).toHaveAttribute("aria-hidden", "true");
    expect(mark.parentElement).toHaveAttribute("title", "无权查看今日用量");
    expect(cell).toHaveTextContent("无权查看今日用量");
    expect(cell.querySelector(".pwi")).toBeNull();
    expect(apiClient.usageCalls).toEqual([]);
  });
});

const SERVICE_ID = uuid(1);
const RUNNING_ID = uuid(2);
const STOPPED_ID = uuid(3);
const ARTIFACT_ID = uuid(4);
const USER_ID = uuid(5);

function uuid(seed: number) {
  return `0198f58a-2586-7e37-bbf5-${seed.toString().padStart(12, "0")}`;
}

async function openCreateDialog(user: ReturnType<typeof userEvent.setup>) {
  await screen.findByRole("heading", { level: 1, name: "AI 服务" });
  await user.click(screen.getByRole("button", { name: "创建服务" }));
  const dialog = await screen.findByRole("dialog", { name: "创建服务" });
  await within(dialog).findByRole("option", { name: "运行实例" });
  return dialog;
}

function clientWith(
  items: ModelInstance[],
  { services = [] }: { services?: AiService[] } = {},
) {
  const apiClient = new FakeApiClient(ADMIN_ME);
  apiClient.modelInstanceResults.push({ items });
  apiClient.aiServiceResults.push({ items: services });
  return apiClient;
}

function instance(overrides: Partial<ModelInstance> = {}): ModelInstance {
  return {
    artifact_id: ARTIFACT_ID,
    config_version: 1,
    updated_at: "2026-10-13T09:00:00Z",
    context_length: 4096,
    error_code: null,
    gpu_memory_utilization: 0.9,
    restart_count: 0,
    gpu_uuids: ["GPU-fixture-0"],
    id: RUNNING_ID,
    internal_endpoint: null,
    name: "运行实例",
    internal_model_name: "mi_ui21fixture",
    runtime_type: "vllm",
    status: "Running",
    tensor_parallel_size: 1,
    ...overrides,
  };
}

function service(overrides: Partial<AiService> = {}): AiService {
  return {
    description: "",
    enabled: true,
    id: SERVICE_ID,
    primary_instance_id: RUNNING_ID,
    primary_instance_status: "Running",
    name: "研发助手",
    public_model_name: "qwen-mini",
    route_version: 1,
    runtime_status: "Available",
    ...overrides,
  };
}

function users(): UserList {
  return {
    items: [
      {
        api_key_count: 0,
        authorized_service_count: 0,
        display_name: "张伟",
        id: USER_ID,
        last_login_at: null,
        must_change_password: false,
        role: "user",
        status: "active",
        username: "zhang.wei",
      },
    ],
  };
}
