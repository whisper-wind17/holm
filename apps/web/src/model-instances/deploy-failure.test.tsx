import axe from "axe-core";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ApiError } from "../api/client";
import type {
  ModelInstance,
  ModelInstanceEvent,
} from "../api/model-instance-contracts";
import {
  ADMIN_ME,
  DEFAULT_SYSTEM_SERVER,
  FakeApiClient,
  renderApplication,
} from "../app/test-support";
import { instanceFailurePresentation } from "./presentation";

describe("M3_13B TC-UI-07 deployment failure details", () => {
  it("shows all six OOM failure elements and opens the existing log tab", async () => {
    const apiClient = new FakeApiClient(ADMIN_ME);
    apiClient.modelInstanceDetailResults.push(
      instance({ error_code: "runtime_out_of_memory" }),
    );
    vi.spyOn(apiClient, "getSystemServer").mockResolvedValue(serverWithGpus(1));
    vi.spyOn(apiClient, "getModelInstanceEvents").mockResolvedValue({
      items: [failedEvent("2026-09-22T10:30:00+08:00")],
      next_cursor: null,
    });
    const { container } = renderApplication(
      apiClient,
      `/models/instances/${INSTANCE_ID}`,
    );
    const user = userEvent.setup();

    expect(await screen.findByText("加载权重")).toBeVisible();
    expect(
      screen.getByText("GPU 显存不足，模型实例未能完成部署。"),
    ).toBeVisible();
    expect(await screen.findByText("NVIDIA L20 48 GB × 1")).toBeVisible();
    expect(
      screen.getByText("可尝试使用 2 张同型号 GPU，或降低 Context Length。"),
    ).toBeVisible();
    expect(factValue("事件时间")).not.toMatch(/加载|暂不可用/);
    const logs = screen.getByRole("button", { name: "查看详细日志" });
    expect(logs).toBeVisible();

    await user.click(logs);
    await waitFor(() => {
      expect(screen.getByRole("tab", { name: "日志" })).toHaveAttribute(
        "data-state",
        "active",
      );
    });
    expect((await axe.run(container)).violations).toEqual([]);
  });

  it.each([
    ["runtime_out_of_memory", "加载权重"],
    ["runtime_start_failed", "启动模型"],
    ["artifact_verification_failed", "校验模型文件"],
    ["artifact_verification_timeout", "校验模型文件"],
    ["runtime_start_timeout", "启动模型"],
    ["runtime_exited", "运行模型"],
    ["runtime_restart_failed", "自动恢复"],
    ["runtime_health_failed", "健康检查"],
    ["runtime_gpu_mismatch", "校验 GPU"],
    ["runtime_not_certified", "兼容性检查"],
    ["gpu_inventory_unavailable", "检查 GPU"],
    ["invalid_request", "校验配置"],
  ])("maps the stable reason %s to a complete presentation", (code, stage) => {
    const presentation = instanceFailurePresentation(code);

    expect(presentation.stage).toBe(stage);
    expect(presentation.reason).not.toHaveLength(0);
    expect(presentation.advice).toContain("可尝试");
  });

  it("keeps all six elements explicit when the reason and supporting data are unavailable", async () => {
    const apiClient = new FakeApiClient(ADMIN_ME);
    apiClient.modelInstanceDetailResults.push(
      instance({
        error_code: "future_internal_failure",
        internal_endpoint: "http://127.0.0.1:8123/private-runtime",
      }),
    );
    apiClient.systemServerResults.push(new Error("inventory offline"));
    vi.spyOn(apiClient, "getModelInstanceEvents").mockRejectedValue(
      new ApiError({
        code: "forbidden",
        responseMessage: "private event detail",
        status: 403,
      }),
    );
    const { container } = renderApplication(
      apiClient,
      `/models/instances/${INSTANCE_ID}`,
    );

    expect(await screen.findByText("未知")).toBeVisible();
    expect(
      screen.getByText(
        "部署未能完成，模型实例当前不可用。请检查系统状态后重试。",
      ),
    ).toBeVisible();
    await waitFor(() => {
      expect(factValue("已选资源")).toBe("GPU 信息暂不可用");
    });
    expect(factValue("建议")).toContain("可尝试");
    await waitFor(() => {
      expect(factValue("事件时间")).toBe("事件时间暂不可用");
    });
    expect(screen.getByRole("button", { name: "查看详细日志" })).toBeVisible();
    expect(container).not.toHaveTextContent("future_internal_failure");
    expect(container).not.toHaveTextContent("127.0.0.1");
    expect(container).not.toHaveTextContent("private event detail");
  });

  it("shows honest loading values instead of zeroes or fabricated event times", async () => {
    const apiClient = new FakeApiClient(ADMIN_ME);
    apiClient.modelInstanceDetailResults.push(
      instance({ error_code: "runtime_start_timeout" }),
    );
    vi.spyOn(apiClient, "getSystemServer").mockReturnValue(
      new Promise(() => undefined),
    );
    vi.spyOn(apiClient, "getModelInstanceEvents").mockReturnValue(
      new Promise(() => undefined),
    );
    renderApplication(apiClient, `/models/instances/${INSTANCE_ID}`);

    expect(await screen.findByText("GPU 信息正在加载")).toBeVisible();
    expect(factValue("事件时间")).toBe("事件时间正在加载");
    expect(factValue("已选资源")).not.toBe("0");
  });
});

describe("M3_13B TC-UI-07 retry configuration validation", () => {
  it.each([
    [
      "occupied",
      serverWithGpus(1),
      [
        instance(),
        instance({
          id: uuid(9),
          name: "another-model",
          internal_model_name: "another-model",
          status: "Running",
        }),
      ],
      "所选 GPU 已被其他模型实例占用",
      "NVIDIA L20 48 GB × 1",
    ],
    [
      "missing",
      serverWithGpus(0),
      [instance()],
      "所选 GPU 已不在当前清单中",
      "GPU 信息暂不可用",
    ],
  ] as const)(
    "disables retry when the selected GPU is %s",
    async (_, latestServer, latestInstances, expected, expectedResource) => {
      const failed = instance();
      const apiClient = new FakeApiClient(ADMIN_ME);
      apiClient.modelInstanceResults.push(
        { items: [failed] },
        { items: [...latestInstances] },
      );
      vi.spyOn(apiClient, "getSystemServer").mockResolvedValue(latestServer);
      renderApplication(apiClient, "/models");
      const user = userEvent.setup();

      const table = await screen.findByRole("table", { name: "模型实例" });
      await user.click(
        await within(table).findByRole("button", { name: "重试" }),
      );
      const dialog = await screen.findByRole("dialog");

      expect(within(dialog).getByText(expectedResource)).toBeVisible();
      expect(dialogFactValue(dialog, "并行数量")).toBe("1");
      expect(dialogFactValue(dialog, "Context Length")).toBe("4,096");
      expect(
        await within(dialog).findByText(new RegExp(expected)),
      ).toBeVisible();
      expect(
        within(dialog).getByRole("button", { name: "确认重试" }),
      ).toBeDisabled();
    },
  );

  it("rechecks the latest inventory immediately before retrying", async () => {
    const failed = instance();
    const apiClient = new FakeApiClient(ADMIN_ME);
    apiClient.modelInstanceResults.push(
      { items: [failed] },
      { items: [failed] },
      { items: [failed] },
      { items: [] },
    );
    const inventory = vi
      .spyOn(apiClient, "getSystemServer")
      .mockResolvedValue(serverWithGpus(1));
    const retry = vi
      .spyOn(apiClient, "retryModelInstance")
      .mockResolvedValue(instance({ status: "Starting" }));
    renderApplication(apiClient, "/models");
    const user = userEvent.setup();

    const table = await screen.findByRole("table", { name: "模型实例" });
    await user.click(
      await within(table).findByRole("button", { name: "重试" }),
    );
    const dialog = await screen.findByRole("dialog");
    expect(await within(dialog).findByText("GPU 当前可用")).toBeVisible();
    await user.click(within(dialog).getByRole("button", { name: "确认重试" }));

    await waitFor(() => {
      expect(retry).toHaveBeenCalledWith(INSTANCE_ID);
    });
    expect(inventory.mock.calls.length).toBeGreaterThanOrEqual(3);
    expect(apiClient.modelInstanceCalls).toBeGreaterThanOrEqual(3);
  });
});

function factValue(label: string) {
  const term = screen.getByText(label, { selector: "dt" });
  return term.nextElementSibling?.textContent ?? "";
}

function dialogFactValue(dialog: HTMLElement, label: string) {
  const term = within(dialog).getByText(label, { selector: "dt" });
  return term.nextElementSibling?.textContent ?? "";
}

function serverWithGpus(count: number) {
  const server = structuredClone(DEFAULT_SYSTEM_SERVER);
  if (server.agent.status !== "available") throw new Error("invalid fixture");
  server.agent.gpus.devices = Array.from({ length: count }, (_, index) =>
    gpu(index),
  );
  server.agent.gpus.discovery_state = "ready";
  server.agent.gpus.failure_reason = "none";
  return server;
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

function failedEvent(occurredAt: string): ModelInstanceEvent {
  return {
    actor: "controller",
    detail: {},
    event_type: "StatusChanged",
    from_status: "Starting",
    id: uuid(7),
    occurred_at: occurredAt,
    to_status: "Failed",
  };
}

function instance(overrides: Partial<ModelInstance> = {}): ModelInstance {
  return {
    artifact_id: uuid(2),
    config_version: 1,
    updated_at: "2026-10-08T09:00:00Z",
    context_length: 4096,
    error_code: "runtime_out_of_memory",
    gpu_memory_utilization: 0.9,
    restart_count: 0,
    gpu_uuids: ["GPU-fixture-0"],
    id: INSTANCE_ID,
    internal_endpoint: null,
    name: "Qwen3-0.6B",
    internal_model_name: "Qwen3-0.6B",
    runtime_type: "vllm",
    status: "Failed",
    tensor_parallel_size: 1,
    ...overrides,
  };
}

function uuid(seed: number) {
  return `0198f58a-2586-7e37-bbf5-${seed.toString().padStart(12, "0")}`;
}

const INSTANCE_ID = uuid(1);
