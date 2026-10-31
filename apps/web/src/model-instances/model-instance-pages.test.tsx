import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import axe from "axe-core";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ApiError } from "../api/client";
import type { ModelArtifactItem } from "../api/model-acquisition-contracts";
import type { ModelInstance } from "../api/model-instance-contracts";
import type { ModelCatalogItem } from "../api/model-selection-contracts";
import {
  ADMIN_ME,
  DEFAULT_SYSTEM_SERVER,
  FakeApiClient,
  renderApplication,
} from "../app/test-support";

describe("PROTO_04 model instance UI", () => {
  it("PROTO_04 submits the wizard with TP equal to the selected GPU count", async () => {
    const apiClient = new FakeApiClient(ADMIN_ME);
    const artifactItem = artifact();
    const created = instance({ status: "Starting" });
    apiClient.modelArtifactResults.push(artifactPage([artifactItem]));
    apiClient.modelCatalogResults.push(catalogPage([catalogItem()]));
    const server = structuredClone(DEFAULT_SYSTEM_SERVER);
    if (server.agent.status !== "available") throw new Error("invalid fixture");
    server.agent.gpus.devices = [gpu(0), gpu(1)];
    server.agent.gpus.discovery_state = "ready";
    server.agent.gpus.failure_reason = "none";
    apiClient.systemServerResults.push(server, structuredClone(server));
    apiClient.modelInstanceResults.push({ items: [] });
    apiClient.modelInstanceCreateResults.push(created);
    apiClient.modelInstanceDetailResults.push(created);
    renderApplication(
      apiClient,
      `/models/deploy?artifact_id=${artifactItem.id}`,
    );
    const user = userEvent.setup();

    await screen.findByRole("heading", { name: "部署模型" });
    await user.click(await screen.findByRole("button", { name: "下一步" }));
    const checkboxes = screen.getAllByRole("checkbox");
    expect(checkboxes).toHaveLength(2);
    await user.click(checkboxes[0] as HTMLElement);
    await user.click(checkboxes[1] as HTMLElement);
    await user.click(screen.getByRole("button", { name: "下一步" }));
    expect(screen.getByLabelText("并行数量")).toHaveValue("2");
    const defaultName = screen.getByRole<HTMLInputElement>("textbox", {
      name: "实例名称",
    }).value;
    expect(defaultName).toBe("Qwen3-0.6B");
    // M7-15A TC-UI-02/03: help must preserve the actual deployment draft.
    const helpTrigger = screen.getByRole("button", {
      name: "查看首次部署帮助",
    });
    await user.click(helpTrigger);
    const help = await screen.findByRole("dialog", { name: "帮助中心" });
    expect(
      within(help).getByRole("heading", { name: "四步的顺序不能颠倒" }),
    ).toBeVisible();
    await user.click(within(help).getByRole("button", { name: "关闭帮助" }));
    await waitFor(() => expect(helpTrigger).toHaveFocus());
    expect(screen.getByRole("textbox", { name: "实例名称" })).toHaveValue(
      defaultName,
    );
    expect(screen.getByLabelText("并行数量")).toHaveValue("2");
    expect(apiClient.modelInstanceCreateCalls).toEqual([]);

    await user.click(screen.getByRole("button", { name: "下一步" }));
    await user.click(screen.getByRole("button", { name: "开始部署" }));

    await waitFor(() => {
      expect(apiClient.modelInstanceCreateCalls).toEqual([
        {
          name: defaultName,
          artifact_id: artifactItem.id,
          context_length: 4096,
          gpu_memory_utilization: 0.9,
          gpu_uuids: ["GPU-fixture-0", "GPU-fixture-1"],
          runtime_type: "vllm",
          tensor_parallel_size: 2,
        },
      ]);
    });
    expect((await screen.findAllByText("启动中")).length).toBeGreaterThan(0);
  });

  it("PROTO_04 renders lifecycle actions allowed by each list status", async () => {
    const apiClient = new FakeApiClient(ADMIN_ME);
    apiClient.modelInstanceResults.push({
      items: [
        instance({
          id: uuid(20),
          name: "running-model",
          internal_model_name: "running-model",
          status: "Running",
        }),
        instance({
          id: uuid(21),
          name: "stopped-model",
          internal_model_name: "stopped-model",
          status: "Stopped",
        }),
        instance({
          id: uuid(22),
          name: "starting-model",
          internal_model_name: "starting-model",
          status: "Starting",
        }),
      ],
    });
    renderApplication(apiClient, "/models");

    const table = await screen.findByRole("table", { name: "模型实例" });
    await within(table).findByText("running-model");
    const runningRow = within(table).getByText("running-model").closest("tr");
    const stoppedRow = within(table).getByText("stopped-model").closest("tr");
    const startingRow = within(table).getByText("starting-model").closest("tr");
    expect(runningRow).not.toBeNull();
    expect(stoppedRow).not.toBeNull();
    expect(startingRow).not.toBeNull();
    expect(
      within(runningRow as HTMLElement).getByRole("button", { name: "停止" }),
    ).toBeVisible();
    expect(
      within(runningRow as HTMLElement).queryByRole("button", { name: "删除" }),
    ).toBeNull();
    expect(
      within(stoppedRow as HTMLElement).getByRole("button", { name: "启动" }),
    ).toBeVisible();
    expect(
      within(stoppedRow as HTMLElement).getByRole("button", { name: "删除" }),
    ).toBeVisible();
    expect(
      within(startingRow as HTMLElement)
        .getAllByRole("button")
        .map(({ textContent }) => textContent),
    ).toEqual(["日志"]);
  });

  it("PROTO_04 omits the internal endpoint from instance details", async () => {
    const apiClient = new FakeApiClient(ADMIN_ME);
    apiClient.modelInstanceDetailResults.push(
      instance({
        error_code: "future_internal_failure",
        internal_endpoint: "http://127.0.0.1:8123/private-runtime",
        status: "Failed",
      }),
    );
    vi.spyOn(apiClient, "getModelInstanceEvents").mockResolvedValue({
      items: [],
      next_cursor: null,
    });
    const { container } = renderApplication(
      apiClient,
      `/models/instances/${INSTANCE_ID}`,
    );

    expect(
      await screen.findByText(
        "部署未能完成，模型实例当前不可用。请检查系统状态后重试。",
      ),
    ).toBeVisible();
    expect(container).not.toHaveTextContent(
      "http://127.0.0.1:8123/private-runtime",
    );
    expect(container).not.toHaveTextContent("future_internal_failure");
    expect(screen.queryByText("内部 Endpoint")).not.toBeInTheDocument();
  });
});

describe("M3_10 TC-UI-15 deployment policies", () => {
  it("derives Context choices bounded by the model limit", async () => {
    const bounded = wizardClient(2);
    bounded.modelDeploymentOptionsResults.push(
      deploymentOptions({
        contextLengths: [2048, 4096, 8192],
        contextLimit: 8192,
      }),
    );
    renderApplication(bounded, `/models/deploy?artifact_id=${ARTIFACT_ID}`);
    const user = userEvent.setup();

    await user.click(await screen.findByRole("button", { name: "下一步" }));
    await user.click(screen.getAllByRole("checkbox")[0] as HTMLElement);
    await user.click(screen.getByRole("button", { name: "下一步" }));

    const context = screen.getByRole("combobox", {
      name: /^上下文长度/,
    });
    expect(within(context).getAllByRole("option")).toHaveLength(3);
    expect(
      within(context).queryByRole("option", { name: "16,384" }),
    ).toBeNull();
    expect(screen.getByText("可用档位已按模型上限 8,192 筛选。")).toBeVisible();
  });

  it("states that an unknown model limit is unknown", async () => {
    const unknown = wizardClient(1);
    unknown.modelDeploymentOptionsResults.push(deploymentOptions());
    renderApplication(unknown, `/models/deploy?artifact_id=${ARTIFACT_ID}`);
    const user = userEvent.setup();

    await user.click(await screen.findByRole("button", { name: "下一步" }));
    await user.click(screen.getByRole("checkbox"));
    await user.click(screen.getByRole("button", { name: "下一步" }));
    expect(
      screen.getByText("模型上限未识别，当前显示兼容清单中的全部可用档位。"),
    ).toBeVisible();
  });

  it("TC-INV-02 selects all eight fixture devices without a fixed count control", async () => {
    const apiClient = wizardClient(8);
    apiClient.modelDeploymentOptionsResults.push(
      deploymentOptions({ certifiedGpuCounts: [8] }),
    );
    const { container } = renderApplication(
      apiClient,
      `/models/deploy?artifact_id=${ARTIFACT_ID}`,
    );
    const user = userEvent.setup();

    await user.click(await screen.findByRole("button", { name: "下一步" }));
    const devices = screen.getAllByRole("checkbox");
    expect(devices).toHaveLength(8);
    for (const device of devices) await user.click(device);
    expect(screen.queryByRole("radio", { name: /张|台/ })).toBeNull();
    await user.click(screen.getByRole("button", { name: "下一步" }));

    expect(screen.getByLabelText("并行数量")).toHaveValue("8");
    expect((await axe.run(container)).violations).toEqual([]);
  });

  it("FIX-08 TC-GPU-03 accepts uncertified device counts and rejects invalid Context", async () => {
    const apiClient = wizardClient(3);
    apiClient.modelDeploymentOptionsResults.push(
      deploymentOptions({
        certifiedGpuCounts: [1, 3],
        contextLengths: [2048, 4096, 8192],
      }),
    );
    renderApplication(apiClient, `/models/deploy?artifact_id=${ARTIFACT_ID}`);
    const user = userEvent.setup();

    await user.click(await screen.findByRole("button", { name: "下一步" }));
    const devices = screen.getAllByRole("checkbox");
    await user.click(devices[0] as HTMLElement);
    await user.click(devices[1] as HTMLElement);
    expect(screen.getByRole("button", { name: "下一步" })).toBeEnabled();
    await user.click(devices[2] as HTMLElement);
    await user.click(screen.getByRole("button", { name: "下一步" }));
    const context = screen.getByRole("combobox", {
      name: /^上下文长度/,
    });
    fireEvent.change(context, { target: { value: "32768" } });
    expect(
      screen.getByText("请选择当前模型可用的上下文长度档位。"),
    ).toBeVisible();
    expect(screen.getByRole("button", { name: "下一步" })).toBeDisabled();
    expect(apiClient.modelInstanceCreateCalls).toEqual([]);
  });
});

describe("M3_08 model instance logs", () => {
  it("M3_08 loads cursor pages without duplicating log lines", async () => {
    const apiClient = new FakeApiClient(ADMIN_ME);
    apiClient.modelInstanceDetailResults.push(instance({ status: "Running" }));
    apiClient.modelInstanceLogResults.push(
      {
        entries: [logEntry(0, "startup"), logEntry(1, "ready")],
        next_cursor: "cursor-page-2",
      },
      {
        entries: [logEntry(2, "serving")],
        next_cursor: null,
      },
    );
    renderApplication(apiClient, `/models/instances/${INSTANCE_ID}`);
    const user = userEvent.setup();

    await user.click(await screen.findByRole("tab", { name: "日志" }));
    expect(await screen.findByText("startup")).toBeVisible();
    expect(screen.getByText("ready")).toBeVisible();
    // UI-28: the log tab walks the cursor itself until the 500-line window is full.
    expect(await screen.findByText("serving")).toBeVisible();
    expect(screen.getAllByText("startup")).toHaveLength(1);
    expect(apiClient.modelInstanceLogCalls).toEqual([
      { cursor: undefined, id: INSTANCE_ID, pageSize: 100 },
      { cursor: "cursor-page-2", id: INSTANCE_ID, pageSize: 100 },
    ]);
  });

  it("M3_08 renders the log empty state", async () => {
    const apiClient = new FakeApiClient(ADMIN_ME);
    apiClient.modelInstanceDetailResults.push(instance({ status: "Stopped" }));
    apiClient.modelInstanceLogResults.push({ entries: [], next_cursor: null });
    renderApplication(apiClient, `/models/instances/${INSTANCE_ID}`);
    const user = userEvent.setup();

    await user.click(await screen.findByRole("tab", { name: "日志" }));
    expect(await screen.findByText("暂无日志。")).toBeVisible();
  });

  it("M3_08 renders the log permission state without exposing error details", async () => {
    const apiClient = new FakeApiClient(ADMIN_ME);
    apiClient.modelInstanceDetailResults.push(instance({ status: "Running" }));
    apiClient.modelInstanceLogResults.push(
      new ApiError({
        code: "forbidden",
        responseMessage: "internal permission sentinel",
        status: 403,
      }),
    );
    const { container } = renderApplication(
      apiClient,
      `/models/instances/${INSTANCE_ID}`,
    );
    const user = userEvent.setup();

    await user.click(await screen.findByRole("tab", { name: "日志" }));
    expect(
      await screen.findByText("当前账号不能查看此模型实例的日志。"),
    ).toBeVisible();
    expect(container).not.toHaveTextContent("internal permission sentinel");
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

function deploymentOptions(
  options: {
    certifiedGpuCounts?: number[];
    contextLengths?: number[];
    contextLimit?: number;
  } = {},
) {
  return {
    model_context_limit:
      options.contextLimit === undefined
        ? ({ state: "unrecognized" } as const)
        : ({ state: "identified", value: options.contextLimit } as const),
    runtimes: [
      {
        certified_gpu_counts: options.certifiedGpuCounts ?? [
          1, 2, 3, 4, 5, 6, 7, 8,
        ],
        context_lengths: options.contextLengths ?? [
          2048, 4096, 8192, 16384, 32768,
        ],
        default_context_length: 4096,
        max_gpus_per_instance: 8,
        name: "vllm" as const,
        version: "0.29.0",
      },
    ],
  };
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
  apiClient.systemServerResults.push(server, structuredClone(server));
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

function logEntry(sequence: number, text: string) {
  return {
    original_character_count: text.length,
    sequence,
    text,
    truncated: false,
  };
}
