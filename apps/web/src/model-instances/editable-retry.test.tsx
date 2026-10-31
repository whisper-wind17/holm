import { screen, fireEvent, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import axe from "axe-core";
import { describe, it, expect, vi } from "vitest";
import { ApiError } from "../api/client";
import type { ModelInstance } from "../api/model-instance-contracts";
import type { ModelArtifactItem } from "../api/model-acquisition-contracts";
import type { ModelCatalogItem } from "../api/model-selection-contracts";
import {
  ADMIN_ME,
  DEFAULT_SYSTEM_SERVER,
  FakeApiClient,
  renderApplication,
} from "../app/test-support";

const navigations = vi.hoisted(() => vi.fn());
vi.mock("react-router", async (importOriginal) => {
  const original = await importOriginal<typeof import("react-router")>();
  return {
    ...original,
    useNavigate: () => {
      const navigate = original.useNavigate();
      return (...args: Parameters<typeof navigate>) => {
        navigations(...args);
        return navigate(...args);
      };
    },
  };
});

// FX-M3-13C: M3-10B catalog and inventory with a failed instance's nondefault configuration.
describe("M3_13C TC-UI-01/02/03/07 editable retry", () => {
  it.each(["detail", "modal"])(
    "opens from %s using only the source ID and creates a new instance",
    async (entry) => {
      const client = retryClient();
      client.modelInstanceCreateResults.push(instance({ id: uuid(99) }));
      const retry = vi.spyOn(client, "retryModelInstance");
      const stop = vi.spyOn(client, "stopModelInstance");
      const start = vi.spyOn(client, "startModelInstance");
      const remove = vi.spyOn(client, "deleteModelInstance");
      const source = failedInstance();
      vi.spyOn(client, "getModelInstance").mockResolvedValue(source);
      const originalSource = structuredClone(source);
      const updateService = vi.spyOn(client, "updateAiService");
      const { container } = renderApplication(
        client,
        `/models/instances/${INSTANCE_ID}`,
      );
      const user = userEvent.setup();
      const header = (await screen.findByRole("heading", { level: 1 })).closest(
        "header",
      );
      if (header === null) throw new Error("missing detail header");
      await within(header).findByRole("button", { name: "修改配置并重试" });
      if (entry === "modal") {
        await user.click(screen.getByRole("button", { name: "重试" }));
        await user.click(
          within(await screen.findByRole("dialog")).getByRole("button", {
            name: "修改配置并重试",
          }),
        );
      } else {
        // UI-28: the header button first opens the explanation dialog.
        await user.click(
          within(header).getByRole("button", { name: "修改配置并重试" }),
        );
        await user.click(
          within(await screen.findByRole("dialog")).getByRole("button", {
            name: "前往部署向导",
          }),
        );
      }
      expect(navigations).toHaveBeenCalledWith(
        `/models/deploy?retry_instance_id=${INSTANCE_ID}`,
      );
      // UI-29 goal 2: retry mode starts at the GPU step with the model locked.
      expect((await screen.findAllByRole("checkbox"))[0]).toBeChecked();
      expect(screen.queryByRole("radio")).not.toBeInTheDocument();
      expect(screen.getByRole("button", { name: "上一步" })).toBeDisabled();
      expect(
        screen.getByText(
          "以原实例的模型和配置为初始值，确认后新建一个模型实例；原实例保留，确认新实例正常后可自行删除。",
        ),
      ).toBeVisible();
      await user.click(screen.getByRole("button", { name: "下一步" }));
      expect(screen.getByRole("combobox", { name: "运行环境" })).toHaveValue(
        "vllm",
      );
      expect(screen.getByRole("combobox", { name: /^上下文长度/ })).toHaveValue(
        "8192",
      );
      await user.click(screen.getByRole("button", { name: "高级设置" }));
      expect(screen.getByRole("spinbutton", { name: /^显存比例/ })).toHaveValue(
        0.85,
      );
      const name = screen.getByRole("textbox", { name: "实例名称" });
      expect((name as HTMLInputElement).value).toBe("Qwen3-0.6B");
      name.focus();
      await user.keyboard("{Control>}a{/Control}重试后的实例");
      await user.selectOptions(
        screen.getByRole("combobox", { name: /^上下文长度/ }),
        "2048",
      );
      fireEvent.change(screen.getByRole("spinbutton", { name: /^显存比例/ }), {
        target: { value: "0.8" },
      });
      expect((await axe.run(container)).violations).toEqual([]);
      await user.click(screen.getByRole("button", { name: "下一步" }));
      await user.click(screen.getByRole("button", { name: "开始部署" }));
      await waitFor(() => {
        expect(client.modelInstanceCreateCalls).toHaveLength(1);
      });
      expect(client.modelInstanceCreateCalls[0]).toEqual({
        name: "重试后的实例",
        artifact_id: ARTIFACT_ID,
        gpu_uuids: ["GPU-fixture-0"],
        tensor_parallel_size: 1,
        runtime_type: "vllm",
        context_length: 2048,
        gpu_memory_utilization: 0.8,
      });
      expect(retry).not.toHaveBeenCalled();
      expect(stop).not.toHaveBeenCalled();
      expect(start).not.toHaveBeenCalled();
      expect(remove).not.toHaveBeenCalled();
      expect(source).toEqual(originalSource);
      await waitFor(() => {
        expect(navigations).toHaveBeenCalledWith(
          `/models/instances/${uuid(99)}`,
          { replace: true },
        );
      });
      expect(updateService).not.toHaveBeenCalled();
    },
  );

  it("keeps edited fields across policy/source refresh and cancellation has no write", async () => {
    const client = retryClient();
    const { queryClient } = renderApplication(client, retryPath());
    await configurationStep();
    fireEvent.change(screen.getByRole("textbox", { name: "实例名称" }), {
      target: { value: "保留编辑" },
    });
    await userEvent.selectOptions(
      screen.getByRole("combobox", { name: /^上下文长度/ }),
      "2048",
    );
    await userEvent.click(screen.getByRole("button", { name: "高级设置" }));
    fireEvent.change(screen.getByRole("spinbutton", { name: /^显存比例/ }), {
      target: { value: "0.82" },
    });
    await queryClient.invalidateQueries({ queryKey: ["model-deploy"] });
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "下一步" })).toBeEnabled(),
    );
    expect(screen.getByRole("textbox", { name: "实例名称" })).toHaveValue(
      "保留编辑",
    );
    expect(screen.getByRole("combobox", { name: /^上下文长度/ })).toHaveValue(
      "2048",
    );
    expect(screen.getByRole("spinbutton", { name: /^显存比例/ })).toHaveValue(
      0.82,
    );
    await userEvent.click(screen.getByRole("button", { name: "返回来源实例" }));
    expect(client.modelInstanceCreateCalls).toEqual([]);
  });

  it.each(["missing", "occupied", "unhealthy"])(
    "preserves %s old GPU selection until explicitly cleared",
    async (problem) => {
      const client = retryClient();
      const server = serverFixture();
      if (server.agent.status !== "available") throw new Error("fixture");
      if (problem === "missing") server.agent.gpus.devices = [gpu(1)];
      if (problem === "unhealthy")
        server.agent.gpus.devices = [
          { ...gpu(0), health: "unhealthy" },
          gpu(1),
        ];
      vi.spyOn(client, "getSystemServer").mockResolvedValue(server);
      if (problem === "occupied")
        vi.spyOn(client, "getModelInstances").mockResolvedValue({
          items: [instance({ status: "Running" })],
        });
      renderApplication(client, retryPath());
      await screen.findByRole("button", { name: "下一步" });
      expect(screen.getByText("所选设备已不可用")).toBeVisible();
      expect(screen.getByRole("button", { name: "下一步" })).toBeDisabled();
      await userEvent.click(
        screen.getByRole("button", { name: "清除失效选择" }),
      );
      const available = screen.getAllByRole("checkbox").at(-1);
      if (available === undefined) throw new Error("available device missing");
      await userEvent.click(available);
      await userEvent.click(screen.getByRole("button", { name: "下一步" }));
      expect(screen.getByRole("combobox", { name: /^上下文长度/ })).toHaveValue(
        "8192",
      );
    },
  );

  it.each(["runtime", "context"])(
    "shows unsupported old %s without substituting a default and permits correction",
    async (problem) => {
      const client = retryClient({
        runtime_type: problem === "runtime" ? "sglang" : "vllm",
        context_length: 7777,
      });
      renderApplication(client, retryPath());
      await configurationStep();
      expect(screen.getByRole("combobox", { name: /^上下文长度/ })).toHaveValue(
        "7777",
      );
      expect(
        screen.getByRole("option", { name: "7,777（上次配置，当前不可用）" }),
      ).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "下一步" })).toBeDisabled();
      if (problem === "runtime") {
        expect(screen.getByRole("combobox", { name: "运行环境" })).toHaveValue(
          "sglang",
        );
        await userEvent.selectOptions(
          screen.getByRole("combobox", { name: "运行环境" }),
          "vllm",
        );
      } else
        await userEvent.selectOptions(
          screen.getByRole("combobox", { name: /^上下文长度/ }),
          "2048",
        );
      expect(screen.getByRole("button", { name: "下一步" })).toBeEnabled();
    },
  );

  it.each([403, 404, 503, "Running", "artifact", "invalid-id"])(
    "blocks unusable source %s and never becomes ordinary deployment",
    async (problem) => {
      const client = retryClient();
      if (typeof problem === "number")
        vi.spyOn(client, "getModelInstance").mockRejectedValue(
          new ApiError({
            code: "unavailable",
            status: problem,
            responseMessage: "private source error",
          }),
        );
      if (problem === "Running")
        vi.spyOn(client, "getModelInstance").mockResolvedValue(
          failedInstance({ status: "Running" }),
        );
      if (problem === "artifact")
        vi.spyOn(client, "getModelArtifacts").mockResolvedValue(
          artifactPage([{ ...artifact(), status: "Missing" }]),
        );
      const { container } = renderApplication(
        client,
        problem === "invalid-id"
          ? "/models/deploy?retry_instance_id="
          : retryPath(),
      );
      expect(await screen.findByText("无法修改配置并重试")).toBeVisible();
      expect(
        screen.queryByRole("button", { name: "下一步" }),
      ).not.toBeInTheDocument();
      expect(container).not.toHaveTextContent("private source error");
      expect(client.modelInstanceCreateCalls).toEqual([]);
    },
  );

  it.each(["inventory-error", "unknown", "source-error", "artifact", "policy"])(
    "rechecks before create, blocks %s and preserves edits for recovery",
    async (problem) => {
      const client = retryClient();
      const { queryClient } = renderApplication(client, retryPath());
      await configurationStep();
      fireEvent.change(screen.getByRole("textbox", { name: "实例名称" }), {
        target: { value: "最终检查保留" },
      });
      await userEvent.click(screen.getByRole("button", { name: "下一步" }));
      if (problem === "inventory-error")
        vi.spyOn(client, "getSystemServer").mockRejectedValueOnce(
          new Error("offline"),
        );
      if (problem === "unknown") {
        const server = serverFixture();
        if (server.agent.status === "available")
          server.agent.gpus.discovery_state = "unavailable";
        vi.spyOn(client, "getSystemServer").mockResolvedValueOnce(server);
      }
      if (problem === "source-error")
        vi.spyOn(client, "getModelInstance").mockRejectedValueOnce(
          new Error("offline"),
        );
      if (problem === "artifact")
        vi.spyOn(client, "getModelArtifacts").mockResolvedValueOnce(
          artifactPage([]),
        );
      if (problem === "policy")
        vi.spyOn(client, "getModelDeploymentOptions").mockResolvedValueOnce({
          model_context_limit: { state: "unrecognized" },
          runtimes: [],
        });
      await userEvent.click(screen.getByRole("button", { name: "开始部署" }));
      await waitFor(() =>
        expect(screen.queryByText("正在部署")).not.toBeInTheDocument(),
      );
      expect(client.modelInstanceCreateCalls).toEqual([]);
      await queryClient.invalidateQueries({ queryKey: ["model-deploy"] });
      await waitFor(() =>
        expect(screen.getByRole("button", { name: "开始部署" })).toBeEnabled(),
      );
      await userEvent.click(screen.getByRole("button", { name: "上一步" }));
      expect(screen.getByRole("textbox", { name: "实例名称" })).toHaveValue(
        "最终检查保留",
      );
      expect(screen.getByRole("combobox", { name: /^上下文长度/ })).toHaveValue(
        "8192",
      );
    },
  );
  it("sends the user back to the GPU step when the recheck finds the GPU occupied (UI-29 goal 6)", async () => {
    const client = retryClient();
    renderApplication(client, retryPath());
    await configurationStep();
    fireEvent.change(screen.getByRole("textbox", { name: "实例名称" }), {
      target: { value: "最终检查保留" },
    });
    await userEvent.click(screen.getByRole("button", { name: "下一步" }));
    vi.spyOn(client, "getModelInstances").mockResolvedValueOnce({
      items: [instance({ id: uuid(77), name: "抢占者", status: "Starting" })],
    });
    await userEvent.click(screen.getByRole("button", { name: "开始部署" }));
    expect(
      await screen.findByText("所选 GPU 已被其他模型实例占用，请重新选择"),
    ).toBeVisible();
    expect(client.modelInstanceCreateCalls).toEqual([]);
    expect(screen.getByRole("button", { name: "上一步" })).toBeDisabled();
    // The occupied GPU is dropped from the selection; pick the other one.
    const boxes = screen.getAllByRole("checkbox");
    expect(boxes[0]).toBeDisabled();
    expect(boxes[0]).not.toBeChecked();
    await userEvent.click(boxes[1] as HTMLElement);
    await userEvent.click(screen.getByRole("button", { name: "下一步" }));
    expect(screen.getByRole("textbox", { name: "实例名称" })).toHaveValue(
      "最终检查保留",
    );
    expect(screen.getByRole("combobox", { name: /^上下文长度/ })).toHaveValue(
      "8192",
    );
  });
  it("locks confirmation while rechecking resources and prevents duplicate create", async () => {
    const client = retryClient();
    client.modelInstanceCreateResults.push(instance({ id: uuid(99) }));
    renderApplication(client, retryPath());
    await configurationStep();
    await userEvent.click(screen.getByRole("button", { name: "下一步" }));
    let release!: (value: ReturnType<typeof serverFixture>) => void;
    vi.spyOn(client, "getSystemServer").mockReturnValueOnce(
      new Promise((resolve) => {
        release = resolve;
      }),
    );
    const submit = screen.getByRole("button", { name: "开始部署" });
    fireEvent.click(submit);
    fireEvent.click(submit);
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "正在部署" })).toBeDisabled(),
    );
    expect(screen.getByRole("button", { name: "上一步" })).toBeDisabled();
    expect(client.modelInstanceCreateCalls).toEqual([]);
    release(serverFixture());
    await waitFor(() => {
      expect(client.modelInstanceCreateCalls).toHaveLength(1);
    });
  });
  it("finds the source artifact beyond the first page", async () => {
    const client = retryClient();
    const artifacts = vi
      .spyOn(client, "getModelArtifacts")
      .mockResolvedValueOnce({ ...artifactPage([]), next_cursor: "next-page" });
    renderApplication(client, retryPath());
    await configurationStep();
    expect(artifacts).toHaveBeenCalledWith({
      status: "Ready",
      cursor: "next-page",
    });
    expect(screen.getByRole("combobox", { name: /^上下文长度/ })).toHaveValue(
      "8192",
    );
  });
  it("fails visibly when artifact pagination repeats a cursor", async () => {
    const client = retryClient();
    const artifacts = vi
      .spyOn(client, "getModelArtifacts")
      .mockResolvedValue({ ...artifactPage([]), next_cursor: "repeated" });
    renderApplication(client, retryPath());
    expect(await screen.findByText("无法修改配置并重试")).toBeVisible();
    expect(artifacts).toHaveBeenCalledTimes(2);
    expect(
      screen.queryByRole("button", { name: "下一步" }),
    ).not.toBeInTheDocument();
    expect(client.modelInstanceCreateCalls).toEqual([]);
  });
});

async function configurationStep() {
  // UI-29: retry mode starts at the GPU step, so one click reaches step 3.
  await userEvent.click(await screen.findByRole("button", { name: "下一步" }));
  await screen.findByRole("textbox", { name: "实例名称" });
}
function retryPath() {
  return `/models/deploy?retry_instance_id=${INSTANCE_ID}`;
}
function failedInstance(overrides: Partial<ModelInstance> = {}) {
  return instance({
    status: "Failed",
    context_length: 8192,
    gpu_memory_utilization: 0.85,
    error_code: "runtime_out_of_memory",
    ...overrides,
  });
}
function serverFixture() {
  const server = structuredClone(DEFAULT_SYSTEM_SERVER);
  if (server.agent.status !== "available") throw new Error("fixture");
  server.agent.gpus.devices = [gpu(0), gpu(1)];
  server.agent.gpus.discovery_state = "ready";
  server.agent.gpus.failure_reason = "none";
  return server;
}
function retryClient(overrides: Partial<ModelInstance> = {}) {
  const client = wizardClient(2);
  vi.spyOn(client, "getModelInstance").mockResolvedValue(
    failedInstance(overrides),
  );
  vi.spyOn(client, "getModelArtifacts").mockResolvedValue(
    artifactPage([artifact()]),
  );
  vi.spyOn(client, "getSystemServer").mockResolvedValue(serverFixture());
  vi.spyOn(client, "getModelCatalog").mockResolvedValue(
    catalogPage([catalogItem()]),
  );
  return client;
}

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
