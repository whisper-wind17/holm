import { screen, fireEvent, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import axe from "axe-core";
import { describe, it, expect, vi } from "vitest";
import { ApiError } from "../api/client";
import type { ModelInstance } from "../api/model-instance-contracts";
import {
  instanceNameSchema,
  parseModelInstance,
} from "../api/model-instance-contracts";
import type { ModelArtifactItem } from "../api/model-acquisition-contracts";
import type { ModelCatalogItem } from "../api/model-selection-contracts";
import {
  ADMIN_ME,
  DEFAULT_SYSTEM_SERVER,
  FakeApiClient,
  renderApplication,
} from "../app/test-support";

// FX-M3-10B: existing PROTO_04 models and fake inventory; display name differs from routing name.
describe("M3_10B TC-UI-01/02 instance names", () => {
  it("keeps editable defaults and custom names through refresh, confirmation, failure and keyboard navigation", async () => {
    const client = wizardClient(1);
    client.modelInstanceCreateResults.push(
      new ApiError({
        code: "instance_name_conflict",
        status: 409,
        responseMessage: "实例名称已被使用，模型实例未创建，请修改名称后重试",
      }),
    );
    const { container, queryClient } = renderApplication(
      client,
      `/models/deploy?artifact_id=${ARTIFACT_ID}`,
    );
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "下一步" }));
    await user.click(screen.getByRole("checkbox"));
    await user.click(screen.getByRole("button", { name: "下一步" }));
    const input = screen.getByRole("textbox", { name: "实例名称" });
    expect((input as HTMLInputElement).value).toBe("Qwen3-0.6B");
    input.focus();
    await user.keyboard("{Control>}a{/Control}研发实例 / 主用");
    await user.tab();
    expect(input).not.toHaveFocus();
    expect(input).toHaveValue("研发实例 / 主用");
    client.modelCatalogResults.push(
      catalogPage([{ ...catalogItem(), display_name: "刷新后的模型名" }]),
    );
    await queryClient.invalidateQueries({
      queryKey: ["model-deploy", "catalog"],
    });
    await user.click(screen.getByRole("button", { name: "刷新资源" }));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "下一步" })).toBeEnabled(),
    );
    expect(input).toHaveValue("研发实例 / 主用");
    expect((await axe.run(container)).violations).toEqual([]);
    await user.click(screen.getByRole("button", { name: "下一步" }));
    expect(screen.getByText("研发实例 / 主用")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "开始部署" }));
    expect(await screen.findByText(/实例名称已被使用/)).toBeVisible();
    expect(client.modelInstanceCreateCalls[0]).toMatchObject({
      name: "研发实例 / 主用",
      gpu_uuids: ["GPU-fixture-0"],
      context_length: 4096,
    });
    await user.click(screen.getByRole("button", { name: "上一步" }));
    expect(screen.getByRole("textbox", { name: "实例名称" })).toHaveValue(
      "研发实例 / 主用",
    );
    expect(screen.getByRole("combobox", { name: /^上下文长度/ })).toHaveValue(
      "4096",
    );
  });

  it("blocks empty and long names locally using Unicode code points", async () => {
    const client = wizardClient(1);
    renderApplication(client, `/models/deploy?artifact_id=${ARTIFACT_ID}`);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "下一步" }));
    await user.click(screen.getByRole("checkbox"));
    await user.click(screen.getByRole("button", { name: "下一步" }));
    const input = screen.getByRole("textbox", { name: "实例名称" });
    for (const name of [" ", "x", "x".repeat(65)]) {
      fireEvent.change(input, { target: { value: name } });
      expect(input).toHaveAttribute("aria-invalid", "true");
      expect(screen.getByRole("button", { name: "下一步" })).toBeDisabled();
    }
    fireEvent.change(input, { target: { value: "𠮷".repeat(64) } });
    expect(screen.getByRole("button", { name: "下一步" })).toBeEnabled();
    expect(client.modelInstanceCreateCalls).toEqual([]);
    expect(instanceNameSchema.safeParse("bad\0name").success).toBe(false);
    const withoutName = { ...instance(), name: undefined };
    expect(() => parseModelInstance(withoutName)).toThrow();
  });

  it("displays the instance name in list and detail while preserving the model field", async () => {
    const client = wizardClient(1);
    vi.spyOn(client, "getModelArtifacts").mockResolvedValue(
      artifactPage([artifact()]),
    );
    const item = instance({
      name: "研发实例一号",
      internal_model_name: "mi_123456789abc",
      status: "Running",
    });
    client.modelInstanceResults[0] = { items: [item] };
    client.modelInstanceDetailResults.push(item);
    client.modelArtifactResults.push(artifactPage([artifact()]));
    renderApplication(client, "/models");
    const row = (await screen.findByText("研发实例一号")).closest("tr");
    if (!row) throw new Error("instance row missing");
    await userEvent.click(within(row).getByRole("link", { name: item.name }));
    expect(
      await screen.findByRole("heading", { name: /研发实例一号/ }),
    ).toBeVisible();
    expect(await screen.findByText("Qwen3-0.6B")).toBeVisible();
    expect(screen.queryByText("mi_123456789abc")).not.toBeInTheDocument();
  });

  it("shows display names in service binding options", async () => {
    const client = new FakeApiClient(ADMIN_ME);
    client.modelInstanceResults.push({
      items: [
        instance({
          name: "绑定名称",
          internal_model_name: "mi_123456789abc",
          status: "Running",
        }),
      ],
    });
    renderApplication(client, "/services");
    await userEvent.click(
      await screen.findByRole("button", { name: "创建服务" }),
    );
    const dialog = await screen.findByRole("dialog", { name: "创建服务" });
    expect(
      within(dialog).getByRole("option", { name: /绑定名称/ }),
    ).toBeInTheDocument();
    expect(
      within(dialog).queryByText(/mi_123456789abc/),
    ).not.toBeInTheDocument();
  });
});

const ARTIFACT_ID = uuid(10);
const MODEL_ID = uuid(11);
const INSTANCE_ID = uuid(12);

function uuid(seed: number) {
  return `0198f58a-2586-7e37-bbf5-${seed.toString().padStart(12, "0")}`;
}

function artifact(): ModelArtifactItem {
  return {
    created_at: "2026-09-15T02:00:00Z",
    display_name: "Qwen3-0.6B",
    id: ARTIFACT_ID,
    last_verified_at: "2026-09-15T02:00:00Z",
    model_id: MODEL_ID,
    path: "/models/qwen3",
    referencing_instances: 0,
    repository_id: "Qwen/Qwen3-0.6B",
    resolved_revision: "revision-1",
    size_bytes: 1_200_000_000,
    source: "ModelScope",
    status: "Ready",
    storage_type: "Managed",
    updated_at: "2026-09-15T02:00:00Z",
  };
}

function artifactPage(items: ModelArtifactItem[]) {
  return {
    items,
    next_cursor: null,
    summary: {
      active_download_tasks: 0,
      managed_bytes: items.reduce((sum, item) => sum + item.size_bytes, 0),
      storage_available_bytes: 100_000_000_000,
    },
  };
}

function catalogItem(): ModelCatalogItem {
  return {
    architecture: { state: "identified", value: "Qwen2ForCausalLM" },
    display_name: "Qwen3-0.6B",
    estimated_size_bytes: { state: "identified", value: 1_200_000_000 },
    id: MODEL_ID,
    local_availability: "artifact_ready",
    modalities: { state: "identified", value: ["text"] },
    references: { artifacts: 1, download_tasks: 1, model_instances: 0 },
    repository_id: "Qwen/Qwen3-0.6B",
    revision: "revision-1",
    source: "ModelScope",
  };
}

function catalogPage(items: ModelCatalogItem[]) {
  return { items, next_cursor: null };
}

function wizardClient(gpuCount: number) {
  const apiClient = new FakeApiClient(ADMIN_ME);
  apiClient.modelArtifactResults.push(artifactPage([artifact()]));
  apiClient.modelCatalogResults.push(catalogPage([catalogItem()]));
  const server = structuredClone(DEFAULT_SYSTEM_SERVER);
  if (server.agent.status !== "available") throw new Error("invalid fixture");
  server.agent.gpus.devices = Array.from({ length: gpuCount }, (_, index) =>
    gpu(index),
  );
  server.agent.gpus.discovery_state = "ready";
  server.agent.gpus.failure_reason = "none";
  apiClient.systemServerResults.push(
    server,
    structuredClone(server),
    structuredClone(server),
  );
  apiClient.modelInstanceResults.push({ items: [] });
  return apiClient;
}

function gpu(index: number) {
  return {
    display_index: index,
    health: "healthy" as const,
    health_reasons: [],
    memory_bytes: 51_539_607_552,
    model: "NVIDIA L20",
    uuid: `GPU-fixture-${String(index)}`,
  };
}

function instance(overrides: Partial<ModelInstance> = {}): ModelInstance {
  return {
    artifact_id: ARTIFACT_ID,
    config_version: 1,
    updated_at: "2026-10-08T09:00:00Z",
    context_length: 4096,
    error_code: null,
    gpu_memory_utilization: 0.9,
    restart_count: 0,
    gpu_uuids: ["GPU-fixture-0"],
    id: INSTANCE_ID,
    internal_endpoint: null,
    name: "Qwen3-0.6B",
    internal_model_name: "Qwen3-0.6B",
    runtime_type: "vllm",
    status: "Starting",
    tensor_parallel_size: 1,
    ...overrides,
  };
}

// FX-UI-07: reuse existing ready artifact, model and empty GPU inventory fixtures.
describe("UI_07 default names and list accessibility", () => {
  it("removes the organization and skips occupied names without overwriting edits", async () => {
    const client = wizardClient(1);
    client.modelCatalogResults[0] = catalogPage([
      { ...catalogItem(), display_name: "Org/Qwen3-0.6B" },
    ]);
    client.modelInstanceResults[0] = {
      items: [
        instance({ name: "Qwen3-0.6B", gpu_uuids: [], status: "Stopped" }),
        instance({
          id: uuid(13),
          name: "Qwen3-0.6B-2",
          gpu_uuids: [],
          status: "Stopped",
        }),
      ],
    };
    const { queryClient, container } = renderApplication(
      client,
      `/models/deploy?artifact_id=${ARTIFACT_ID}`,
    );
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "下一步" }));
    await user.click(screen.getByRole("checkbox"));
    await user.click(screen.getByRole("button", { name: "下一步" }));
    const input = screen.getByRole("textbox", { name: "实例名称" });
    expect(input).toHaveValue("Qwen3-0.6B-3");
    fireEvent.change(input, { target: { value: "手工名称" } });
    await queryClient.invalidateQueries({
      queryKey: ["model-deploy", "instances"],
    });
    expect(input).toHaveValue("手工名称");
    expect((await axe.run(container)).violations).toEqual([]);
  });

  it("shows the name without the short ID and passes axe", async () => {
    const client = wizardClient(1);
    client.modelInstanceResults[0] = {
      items: [instance({ name: "Qwen3-0.6B" })],
    };
    const { container } = renderApplication(client, "/models");
    const row = (
      await screen.findByRole("link", { name: "Qwen3-0.6B" })
    ).closest("tr");
    if (!row) throw new Error("instance row missing");
    expect(
      within(row).queryByText(INSTANCE_ID.slice(0, 8)),
    ).not.toBeInTheDocument();
    expect((await axe.run(container)).violations).toEqual([]);
  });
});
