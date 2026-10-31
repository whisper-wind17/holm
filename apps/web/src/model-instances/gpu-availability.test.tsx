import { fireEvent, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import axe from "axe-core";
import { describe, expect, it, vi } from "vitest";
import type { ModelArtifactItem } from "../api/model-acquisition-contracts";
import type { ModelCatalogItem } from "../api/model-selection-contracts";
import type { ModelInstance } from "../api/model-instance-contracts";
import {
  ADMIN_ME,
  USER_ME,
  DEFAULT_SYSTEM_SERVER,
  FakeApiClient,
  renderApplication,
} from "../app/test-support";

// FX-GPU-AVAILABILITY: isolated inventory derived from the existing PROTO_04 fixtures.
describe("M3_10A TC-GPU-04/09/13 TC-UI-01/02/08/15 GPU availability", () => {
  it.each([
    "Starting",
    "Running",
    "Restarting",
    "Unhealthy",
    "Stopping",
  ] as const)(
    "disables %s allocations and identifies their owner",
    async (status) => {
      const client = wizardClient(2);
      client.modelInstanceResults[0] = { items: [instance({ status })] };
      const { container } = renderApplication(
        client,
        `/models/deploy?artifact_id=${ARTIFACT_ID}`,
      );
      const user = userEvent.setup();
      await user.click(await screen.findByRole("button", { name: "下一步" }));
      expect(screen.getAllByRole("checkbox")[0]).toBeDisabled();
      expect(
        screen.getByText(`占用实例：Qwen3-0.6B · ${INSTANCE_ID.slice(0, 8)}`),
      ).toBeVisible();
      expect(screen.getAllByRole("checkbox")[1]).toBeEnabled();
      fireEvent.click(screen.getByRole("button", { name: "下一步" }));
      expect(client.modelInstanceCreateCalls).toEqual([]);
      if (status === "Restarting")
        expect((await axe.run(container)).violations).toEqual([]);
    },
  );
  it.each(["WaitingForResource", "Stopped", "Failed"] as const)(
    "does not reserve desired UUIDs for %s",
    async (status) => {
      const client = wizardClient(1);
      client.modelInstanceResults[0] = { items: [instance({ status })] };
      renderApplication(client, `/models/deploy?artifact_id=${ARTIFACT_ID}`);
      await userEvent.click(
        await screen.findByRole("button", { name: "下一步" }),
      );
      const checkbox = screen.getByRole("checkbox");
      checkbox.focus();
      await userEvent.keyboard(" ");
      expect(checkbox).toBeChecked();
      expect(screen.getByRole("button", { name: "下一步" })).toBeEnabled();
    },
  );
  it.each(["occupied", "unhealthy", "missing"])(
    "blocks refreshed %s selections at configuration and confirmation, then recovers",
    async (change) => {
      const client = wizardClient(2);
      const { queryClient } = renderApplication(
        client,
        `/models/deploy?artifact_id=${ARTIFACT_ID}`,
      );
      const user = userEvent.setup();
      await user.click(await screen.findByRole("button", { name: "下一步" }));
      await user.click(required(screen.getAllByRole("checkbox")[0]));
      await user.click(screen.getByRole("button", { name: "下一步" }));
      const original = queryClient.getQueryData([
        "model-deploy",
        "server",
      ]) as typeof DEFAULT_SYSTEM_SERVER;
      const changed = structuredClone(original);
      if (changed.agent.status !== "available")
        throw new Error("invalid fixture");
      if (change === "unhealthy")
        required(changed.agent.gpus.devices[0]).health = "unhealthy";
      if (change === "missing") changed.agent.gpus.devices.shift();
      const serverSpy = vi
        .spyOn(client, "getSystemServer")
        .mockResolvedValue(changed);
      const instances = vi
        .spyOn(client, "getModelInstances")
        .mockResolvedValue({
          items: change === "occupied" ? [instance()] : [],
        });
      await user.click(screen.getByRole("button", { name: "刷新资源" }));
      expect(await screen.findByText("所选设备已不可用")).toBeVisible();
      expect(screen.getByRole("button", { name: "下一步" })).toBeDisabled();
      fireEvent.click(screen.getByRole("button", { name: "下一步" }));
      expect(client.modelInstanceCreateCalls).toEqual([]);
      serverSpy.mockResolvedValue(original);
      instances.mockResolvedValue({ items: [] });
      await user.click(screen.getByRole("button", { name: "刷新资源" }));
      await waitFor(() =>
        expect(screen.getByRole("button", { name: "下一步" })).toBeEnabled(),
      );
      await user.click(screen.getByRole("button", { name: "下一步" }));
      serverSpy.mockResolvedValue(changed);
      instances.mockResolvedValue({
        items: change === "occupied" ? [instance()] : [],
      });
      await user.click(screen.getByRole("button", { name: "刷新资源" }));
      expect(await screen.findByText("所选设备已不可用")).toBeVisible();
      expect(screen.getByRole("button", { name: "开始部署" })).toBeDisabled();
      fireEvent.click(screen.getByRole("button", { name: "开始部署" }));
      expect(client.modelInstanceCreateCalls).toEqual([]);
      await user.click(screen.getByRole("button", { name: "清除失效选择" }));
      const available = required(
        screen
          .getAllByRole("checkbox")
          .find((input) => !(input as HTMLInputElement).disabled),
      );
      await user.click(available);
      await user.click(screen.getByRole("button", { name: "下一步" }));
      expect(screen.getByLabelText("并行数量")).toHaveValue("1");
      await user.click(screen.getByRole("button", { name: "下一步" }));
      await user.click(screen.getByRole("button", { name: "开始部署" }));
      await waitFor(() => {
        expect(client.modelInstanceCreateCalls[0]).toMatchObject({
          gpu_uuids: ["GPU-fixture-1"],
          tensor_parallel_size: 1,
        });
      });
    },
  );
  it.each(["server", "instances"])(
    "fails closed during %s refresh and cached failure, with retry recovery",
    async (source) => {
      const client = wizardClient(1);
      const { queryClient } = renderApplication(
        client,
        `/models/deploy?artifact_id=${ARTIFACT_ID}`,
      );
      const user = userEvent.setup();
      await user.click(await screen.findByRole("button", { name: "下一步" }));
      await user.click(screen.getByRole("checkbox"));
      await user.click(screen.getByRole("button", { name: "下一步" }));
      await user.click(screen.getByRole("button", { name: "下一步" }));
      const original = queryClient.getQueryData([
        "model-deploy",
        "server",
      ]) as typeof DEFAULT_SYSTEM_SERVER;
      const serverSpy = vi
        .spyOn(client, "getSystemServer")
        .mockResolvedValue(original);
      const instanceSpy = vi
        .spyOn(client, "getModelInstances")
        .mockResolvedValue({ items: [] });
      let reject!: (reason: Error) => void;
      if (source === "server")
        serverSpy.mockReturnValueOnce(
          new Promise((_, fail) => {
            reject = fail;
          }),
        );
      else
        instanceSpy.mockReturnValueOnce(
          new Promise((_, fail) => {
            reject = fail;
          }),
        );
      await user.click(screen.getByRole("button", { name: "刷新资源" }));
      expect(screen.getByRole("button", { name: "开始部署" })).toBeDisabled();
      reject(new Error("private dependency detail"));
      expect(await screen.findByText("资源信息已过期")).toBeVisible();
      expect(
        screen.queryByText("private dependency detail"),
      ).not.toBeInTheDocument();
      fireEvent.click(screen.getByRole("button", { name: "开始部署" }));
      expect(client.modelInstanceCreateCalls).toEqual([]);
      await user.click(screen.getByRole("button", { name: "刷新资源" }));
      await waitFor(() =>
        expect(screen.getByRole("button", { name: "开始部署" })).toBeEnabled(),
      );
    },
  );
  it("shows initial failure and reloads without fabricating resources", async () => {
    const client = wizardClient(1);
    const original = required(client.systemServerResults[0]);
    client.systemServerResults[0] = new Error("offline");
    const spy = vi
      .spyOn(client, "getSystemServer")
      .mockRejectedValue(new Error("offline"));
    renderApplication(client, `/models/deploy?artifact_id=${ARTIFACT_ID}`);
    expect(await screen.findByText("无法准备部署信息")).toBeVisible();
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
    if (original instanceof Error) throw original;
    spy.mockResolvedValue(original);
    await userEvent.click(screen.getByRole("button", { name: "重新加载" }));
    await userEvent.click(
      await screen.findByRole("button", { name: "下一步" }),
    );
    expect(screen.getByRole("checkbox")).toBeEnabled();
  });
  it.each(["no_devices", "unavailable", "unhealthy", "unknown", "occupied"])(
    "distinguishes %s inventory without allowing selection",
    async (kind) => {
      const client = wizardClient(1);
      const server = required(client.systemServerResults[0]);
      if (server instanceof Error || server.agent.status !== "available")
        throw new Error("invalid fixture");
      if (kind === "no_devices" || kind === "unavailable") {
        server.agent.gpus.discovery_state = kind;
        server.agent.gpus.devices = [];
      } else if (kind === "occupied")
        client.modelInstanceResults[0] = { items: [instance()] };
      else
        required(server.agent.gpus.devices[0]).health = kind as
          "unknown" | "unhealthy";
      vi.spyOn(client, "getSystemServer").mockResolvedValue(server);
      renderApplication(client, `/models/deploy?artifact_id=${ARTIFACT_ID}`);
      await userEvent.click(
        await screen.findByRole("button", { name: "下一步" }),
      );
      expect(
        screen.getByText(
          kind === "no_devices"
            ? "没有可选设备"
            : kind === "unavailable"
              ? "资源信息暂不可用"
              : "当前没有可用设备",
        ),
      ).toBeVisible();
      screen
        .queryAllByRole("checkbox")
        .forEach((input) => expect(input).toBeDisabled());
      expect(screen.getByRole("button", { name: "下一步" })).toBeDisabled();
      expect(client.modelInstanceCreateCalls).toEqual([]);
    },
  );
  it("keeps the deployment route administrator-only", async () => {
    const client = new FakeApiClient(USER_ME);
    renderApplication(client, "/models/deploy");
    await screen.findByRole("heading", { name: "无权限" });
    expect(client.systemServerCalls).toBe(0);
    expect(client.modelInstanceCreateCalls).toEqual([]);
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
  apiClient.systemServerResults.push(server, structuredClone(server));
  apiClient.modelInstanceResults.push({ items: [] });
  apiClient.modelInstanceCreateResults.push(instance());
  apiClient.modelInstanceDetailResults.push(instance());
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

function required<T>(value: T | undefined): T {
  if (value === undefined)
    throw new Error("FX-GPU-AVAILABILITY fixture missing");
  return value;
}
