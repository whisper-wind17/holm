import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import axe from "axe-core";
import { describe, expect, it, vi } from "vitest";

import { parseModelArtifactDetail } from "../api/model-acquisition-contracts";
import type { ModelArtifactItem } from "../api/model-acquisition-contracts";
import type { ModelInstance } from "../api/model-instance-contracts";
import type { ModelCatalogItem } from "../api/model-selection-contracts";
import {
  ADMIN_ME,
  DEFAULT_SYSTEM_SERVER,
  FakeApiClient,
  renderApplication,
} from "../app/test-support";

// FX-UI-29: one 48 GiB GPU model, a small local model and a large one.
const GPU_MEMORY = 51_539_607_552;
const ARTIFACT_ID = uuid(10);
const MODEL_ID = uuid(11);

describe("UI_29 deploy wizard", () => {
  it("lists available local models with source, revision, size, architecture and context", async () => {
    const client = wizardClient({
      artifacts: [
        artifact(),
        artifact({
          id: uuid(20),
          model_id: uuid(21),
          display_name: "NoContext",
          source: "Hugging Face",
        }),
      ],
      contextByArtifact: { [ARTIFACT_ID]: 32768 },
    });
    const { container } = renderApplication(client, "/models/deploy");
    const radios = await screen.findAllByRole("radio");
    expect(radios).toHaveLength(2);
    const first = radios[0]?.closest("label") as HTMLElement;
    expect(within(first).getByText("Qwen3-0.6B")).toBeVisible();
    expect(within(first).getByText(/ModelScope/)).toBeVisible();
    const revision = within(first).getByText("3f2a9c1");
    expect(revision).toHaveAttribute("title", "3f2a9c1d4e5b6a7");
    expect(first).toHaveTextContent("1.1 GB");
    expect(first).toHaveTextContent("Qwen2ForCausalLM");
    await waitFor(() => {
      expect(first).toHaveTextContent("最长上下文 32,768");
    });
    const second = radios[1]?.closest("label") as HTMLElement;
    await waitFor(() => {
      expect(second).toHaveTextContent("最长上下文 未识别");
    });
    expect((await axe.run(container)).violations).toEqual([]);
    await userEvent.click(
      screen.getAllByRole("button", { name: "下载模型" })[0] as HTMLElement,
    );
    expect(
      await screen.findByRole("heading", { level: 1, name: "下载模型" }),
    ).toBeVisible();
  });

  it("explains where to download when no local model is available", async () => {
    const client = wizardClient({ artifacts: [] });
    renderApplication(client, "/models/deploy");
    expect(await screen.findByText("没有可用的本地模型")).toBeVisible();
    expect(
      screen.getByText(/先在本地模型页下载，下载完成后从本地模型行「部署」/),
    ).toBeVisible();
    await userEvent.click(screen.getByRole("button", { name: "下载模型" }));
    expect(
      await screen.findByRole("heading", { level: 1, name: "下载模型" }),
    ).toBeVisible();
  });

  it("blocks Next when the selected GPUs cannot hold the weights and allows enough GPUs", async () => {
    const client = wizardClient({
      artifacts: [artifact({ size_bytes: 100_000_000_000 })],
      gpuCount: 3,
    });
    renderApplication(client, `/models/deploy?artifact_id=${ARTIFACT_ID}`);
    await userEvent.click(
      await screen.findByRole("button", { name: "下一步" }),
    );
    expect(screen.getByText("按权重大小估算，至少需要 3 张")).toBeVisible();
    const boxes = screen.getAllByRole("checkbox");
    await userEvent.click(boxes[0] as HTMLElement);
    expect(
      screen.getByText("已选 1 张，权重放不下，至少需要 3 张"),
    ).toBeVisible();
    expect(screen.getByRole("button", { name: "下一步" })).toBeDisabled();
    await userEvent.click(boxes[1] as HTMLElement);
    expect(
      screen.getByText("已选 2 张，权重放不下，至少需要 3 张"),
    ).toBeVisible();
    await userEvent.click(boxes[2] as HTMLElement);
    expect(screen.queryByText(/权重放不下/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "下一步" })).toBeEnabled();
  });

  it("flags duplicate names inline and keeps advanced settings collapsed with a 0.80-0.95 range", async () => {
    const client = wizardClient({
      artifacts: [artifact()],
      instances: [
        instance({ name: "Qwen3-0.6B-2", status: "Stopped", gpu_uuids: [] }),
      ],
      contextByArtifact: { [ARTIFACT_ID]: 4096 },
      precision: ["bfloat16", "awq"],
    });
    const { container } = renderApplication(
      client,
      `/models/deploy?artifact_id=${ARTIFACT_ID}`,
    );
    await userEvent.click(
      await screen.findByRole("button", { name: "下一步" }),
    );
    await userEvent.click(screen.getAllByRole("checkbox")[0] as HTMLElement);
    await userEvent.click(screen.getByRole("button", { name: "下一步" }));
    // Collapsed by default; no concurrency setting at all.
    const toggle = screen.getByRole("button", { name: "高级设置" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("spinbutton")).not.toBeInTheDocument();
    expect(screen.queryByText(/最大并发请求数/)).not.toBeInTheDocument();

    const name = screen.getByRole("textbox", { name: "实例名称" });
    fireEvent.change(name, { target: { value: "Qwen3-0.6B-2" } });
    expect(screen.getByText("已有同名实例，换一个名称")).toBeVisible();
    expect(screen.getByRole("button", { name: "下一步" })).toBeDisabled();
    fireEvent.change(name, { target: { value: "Qwen3-0.6B" } });
    expect(screen.getByRole("button", { name: "下一步" })).toBeEnabled();

    await userEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    const memory = screen.getByRole("spinbutton", { name: /^显存比例/ });
    expect(memory).toHaveValue(0.9);
    expect(memory).toHaveAttribute("min", "0.8");
    expect(memory).toHaveAttribute("max", "0.95");
    expect(screen.getByRole("textbox", { name: "精度 / 量化" })).toHaveValue(
      "bfloat16 / awq",
    );
    expect(
      screen.getByRole("textbox", { name: "精度 / 量化" }),
    ).toHaveAttribute("readonly");
    fireEvent.change(memory, { target: { value: "0.97" } });
    expect(screen.getByText(/显存比例需在 0.80～0.95 之间/)).toBeVisible();
    expect(screen.getByRole("button", { name: "下一步" })).toBeDisabled();
    fireEvent.change(memory, { target: { value: "0.8" } });
    expect(screen.getByRole("button", { name: "下一步" })).toBeEnabled();
    expect((await axe.run(container)).violations).toEqual([]);
  });

  it("shows unrecognized precision when the detail lookup fails", async () => {
    const client = wizardClient({ artifacts: [artifact()], detailFails: true });
    renderApplication(client, `/models/deploy?artifact_id=${ARTIFACT_ID}`);
    await userEvent.click(
      await screen.findByRole("button", { name: "下一步" }),
    );
    await userEvent.click(screen.getAllByRole("checkbox")[0] as HTMLElement);
    await userEvent.click(screen.getByRole("button", { name: "下一步" }));
    await userEvent.click(screen.getByRole("button", { name: "高级设置" }));
    await waitFor(() => {
      expect(screen.getByRole("textbox", { name: "精度 / 量化" })).toHaveValue(
        "未识别 / 未识别",
      );
    });
  });

  it("summarizes four groups on the last step and returns to the GPU step when the GPU was taken", async () => {
    const client = wizardClient({
      artifacts: [artifact()],
      gpuCount: 2,
      contextByArtifact: { [ARTIFACT_ID]: 4096 },
      precision: ["bfloat16", null],
    });
    const { container } = renderApplication(
      client,
      `/models/deploy?artifact_id=${ARTIFACT_ID}`,
    );
    await userEvent.click(
      await screen.findByRole("button", { name: "下一步" }),
    );
    await userEvent.click(screen.getAllByRole("checkbox")[0] as HTMLElement);
    await userEvent.click(screen.getByRole("button", { name: "下一步" }));
    fireEvent.change(screen.getByRole("textbox", { name: "实例名称" }), {
      target: { value: "保留的名称" },
    });
    await userEvent.click(screen.getByRole("button", { name: "下一步" }));
    for (const title of ["模型", "模型文件", "资源", "运行配置"]) {
      expect(screen.getByRole("heading", { name: title })).toBeVisible();
    }
    expect(
      screen.getByText(
        "所选 GPU 在启动前重新校验本地模型并原子预留，期间被占用时需要重新选择 GPU。",
      ),
    ).toBeVisible();
    expect((await axe.run(container)).violations).toEqual([]);

    vi.spyOn(client, "getModelInstances").mockResolvedValue({
      items: [instance({ id: uuid(77), name: "抢先者", status: "Starting" })],
    });
    await userEvent.click(screen.getByRole("button", { name: "开始部署" }));
    expect(
      await screen.findByText("所选 GPU 已被其他模型实例占用，请重新选择"),
    ).toBeVisible();
    expect(client.modelInstanceCreateCalls).toEqual([]);
    expect(screen.getByRole("heading", { name: /^部署模型$/ })).toBeVisible();
    const boxes = screen.getAllByRole("checkbox");
    expect(boxes[0]).toBeDisabled();
    await userEvent.click(boxes[1] as HTMLElement);
    await userEvent.click(screen.getByRole("button", { name: "下一步" }));
    expect(screen.getByRole("textbox", { name: "实例名称" })).toHaveValue(
      "保留的名称",
    );
  });
});

interface WizardOptions {
  artifacts: ModelArtifactItem[];
  contextByArtifact?: Record<string, number>;
  detailFails?: boolean;
  gpuCount?: number;
  instances?: ModelInstance[];
  precision?: [string | null, string | null];
}

function wizardClient(options: WizardOptions) {
  const client = new FakeApiClient(ADMIN_ME);
  vi.spyOn(client, "getModelArtifacts").mockResolvedValue({
    items: options.artifacts,
    next_cursor: null,
    summary: {
      active_download_tasks: 0,
      managed_bytes: 0,
      storage_available_bytes: 100_000_000_000,
    },
  });
  vi.spyOn(client, "getModelCatalog").mockResolvedValue({
    items: options.artifacts.map((item) => catalogItem(item)),
    next_cursor: null,
  });
  const server = structuredClone(DEFAULT_SYSTEM_SERVER);
  if (server.agent.status !== "available") throw new Error("invalid fixture");
  server.agent.gpus.devices = Array.from(
    { length: options.gpuCount ?? 1 },
    (_, index) => gpu(index),
  );
  server.agent.gpus.discovery_state = "ready";
  server.agent.gpus.failure_reason = "none";
  vi.spyOn(client, "getSystemServer").mockImplementation(() =>
    Promise.resolve(structuredClone(server)),
  );
  vi.spyOn(client, "getModelInstances").mockResolvedValue({
    items: options.instances ?? [],
  });
  vi.spyOn(client, "artifactDetail").mockImplementation((id) => {
    if (options.detailFails === true)
      return Promise.reject(new Error("offline"));
    const item = options.artifacts.find((candidate) => candidate.id === id);
    return Promise.resolve(
      parseModelArtifactDetail({
        ...item,
        model: {
          architecture: null,
          modalities: null,
          precision: options.precision?.[0] ?? null,
          quantization: options.precision?.[1] ?? null,
          context_length: options.contextByArtifact?.[id] ?? null,
          license: null,
        },
        files: null,
        download_tasks: [],
        instances: [],
      }),
    );
  });
  return client;
}

function uuid(seed: number) {
  return `0198f58a-2586-7e37-bbf5-${seed.toString().padStart(12, "0")}`;
}

function artifact(
  overrides: Partial<ModelArtifactItem> = {},
): ModelArtifactItem {
  return {
    created_at: "2026-09-15T02:00:00Z",
    display_name: "Qwen3-0.6B",
    id: ARTIFACT_ID,
    last_verified_at: "2026-09-15T02:00:00Z",
    model_id: MODEL_ID,
    path: "/models/qwen3",
    referencing_instances: 0,
    repository_id: "Qwen/Qwen3-0.6B",
    resolved_revision: "3f2a9c1d4e5b6a7",
    size_bytes: 1_200_000_000,
    source: "ModelScope",
    status: "Ready",
    storage_type: "Managed",
    updated_at: "2026-09-15T02:00:00Z",
    ...overrides,
  };
}

function catalogItem(item: ModelArtifactItem): ModelCatalogItem {
  return {
    architecture: { state: "identified", value: "Qwen2ForCausalLM" },
    display_name: item.display_name,
    estimated_size_bytes: { state: "identified", value: item.size_bytes },
    id: item.model_id,
    local_availability: "artifact_ready",
    modalities: { state: "identified", value: ["text"] },
    references: { artifacts: 1, download_tasks: 1, model_instances: 0 },
    repository_id: item.repository_id,
    revision: item.resolved_revision ?? "—",
    source: item.source,
  };
}

function gpu(index: number) {
  return {
    display_index: index,
    health: "healthy" as const,
    health_reasons: [],
    memory_bytes: GPU_MEMORY,
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
    id: uuid(12),
    internal_endpoint: null,
    name: "Qwen3-0.6B",
    internal_model_name: "Qwen3-0.6B",
    runtime_type: "vllm",
    status: "Starting",
    tensor_parallel_size: 1,
    ...overrides,
  };
}
