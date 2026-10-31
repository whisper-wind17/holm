import {
  QueryClient,
  QueryClientProvider,
  focusManager,
} from "@tanstack/react-query";
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
  cleanup,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import axe from "axe-core";
import { ApiError } from "../api/client";
import type { ModelInstance } from "../api/model-instance-contracts";
import type { GpuTelemetryList } from "../api/telemetry-contracts";
import {
  ADMIN_ME,
  DEFAULT_SYSTEM_COMPONENTS,
  DEFAULT_SYSTEM_SERVER,
  FakeApiClient,
  SYSTEM_OBSERVED_AT,
} from "../app/test-support";
import { componentAttention, hostRows } from "./component-groups";
import { SystemPage } from "./system-page";

// FX-SYS-05: eight synthetic GPUs and SUBS/H_DEMO/E_DEMO from UI-System.dc.html.
const GIB = 1073741824;
function server() {
  const value = structuredClone(DEFAULT_SYSTEM_SERVER);
  if (value.agent.status !== "available")
    throw new Error("FX-SYS-05 agent missing");
  value.agent.gpus = {
    discovery_state: "ready",
    failure_reason: "none",
    snapshot_id: "FX-SYS-05",
    topology_links: [],
    devices: Array.from({ length: 8 }, (_, index) => ({
      uuid: `GPU-FX-SYS-05-${String(index)}`,
      display_index: index,
      model: "NVIDIA L20",
      memory_bytes: 48 * GIB,
      health: "healthy",
      health_reasons: [],
    })),
  };
  return value;
}
function components() {
  const value = structuredClone(DEFAULT_SYSTEM_COMPONENTS);
  value.components.forEach((item) => {
    item.status = "ready";
    item.reason = "self_ready";
    item.version = { state: "known", value: "1.0.0" };
  });
  value.host_environment.items.forEach((item) => {
    item.current = {
      state: "known",
      value: item.id === "nvidia_driver" ? "595.45.04" : "27.5.1",
    };
    item.minimum = item.id === "nvidia_driver" ? "570" : "24.0";
    item.status = "satisfied";
    item.reason = "meets_minimum";
  });
  value.inference_engines = [
    {
      name: "vllm",
      version: "0.29.0",
      launcher: "container",
      image_digest: `registry.internal/sensitive/path@sha256:${"a".repeat(58)}3f9a1c`,
      active_instance_count: 2,
      minimum_driver_version: "570",
      driver_compatibility: "compatible",
      reason: "meets_minimum",
    },
    {
      name: "sglang",
      version: "0.5.13",
      launcher: "process",
      image_digest: null,
      active_instance_count: 0,
      minimum_driver_version: "570",
      driver_compatibility: "compatible",
      reason: "meets_minimum",
    },
  ];
  const database = required(
    value.components.find((item) => item.id === "Database"),
  );
  database.version = { state: "known", value: "16.15" };
  database.details = {
    kind: "database",
    schema_revision: { state: "known", value: "20261014_0040" },
  };
  const ledger = required(
    value.components.find((item) => item.id === "Request Ledger Writer"),
  );
  ledger.details = {
    kind: "request_ledger_writer",
    pending_event_count: { state: "known", value: 1284 },
    oldest_event_at: { state: "known", value: SYSTEM_OBSERVED_AT },
  };
  value.summary = {
    status: "ready",
    ready_count: 10,
    not_deployed_count: 0,
    abnormal_count: 0,
  };
  return value;
}
function telemetry(): GpuTelemetryList {
  return {
    quality: "full",
    sampled_at: SYSTEM_OBSERVED_AT,
    telemetry_status: "available",
    items: Array.from({ length: 8 }, (_, index) => ({
      uuid: `GPU-FX-SYS-05-${String(index)}`,
      quality: "full",
      sampled_at: SYSTEM_OBSERVED_AT,
      telemetry_status: "available",
      series: [],
      current: {
        health: "healthy",
        utilization_percent: index === 0 ? 72 : 0,
        memory_used_bytes: Math.round(
          (index < 2 ? 44.1 : index === 2 ? 1.5 : 0.6) * GIB,
        ),
        temperature_celsius: 42,
        power_watts: 79.9,
      },
    })),
  };
}
function instance(status: ModelInstance["status"] = "Running"): ModelInstance {
  return {
    id: "00000000-0000-4000-8000-000000000001",
    artifact_id: "00000000-0000-4000-8000-000000000002",
    name: "qwen-chat-a",
    status,
    gpu_uuids: ["GPU-FX-SYS-05-0", "GPU-FX-SYS-05-1"],
    tensor_parallel_size: 2,
    config_version: 1,
    context_length: 4096,
    error_code: null,
    gpu_memory_utilization: 0.9,
    internal_endpoint: "http://internal.example:9000",
    internal_model_name: "private-internal-model",
    runtime_type: "vllm",
    restart_count: 0,
    updated_at: SYSTEM_OBSERVED_AT,
  };
}
function setup(tab = "server", api = new FakeApiClient(ADMIN_ME)) {
  const queries = new QueryClient({
    defaultOptions: {
      queries: { retry: false, staleTime: Infinity },
      mutations: { retry: false },
    },
  });
  const view = render(
    <QueryClientProvider client={queries}>
      <MemoryRouter initialEntries={[`/system?tab=${tab}`]}>
        <SystemPage apiClient={api} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { ...view, api, queries };
}
class SystemApiClient extends FakeApiClient {
  override getSystemServer = vi.fn(() => Promise.resolve(server()));
  override getSystemComponents = vi.fn(() => Promise.resolve(components()));
  override getGpuTelemetry = vi.fn(() => Promise.resolve(telemetry()));
  override getModelInstances = vi.fn(() =>
    Promise.resolve({ items: [instance()] }),
  );
}
function apiFixture() {
  return new SystemApiClient(ADMIN_ME);
}
function required<T>(value: T | null | undefined): T {
  if (value === null || value === undefined)
    throw new Error("FX-SYS-05 missing fixture or rendered element");
  return value;
}

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
  focusManager.setFocused(undefined);
});

describe("SYS_05 approved system prototype", () => {
  it("TC_HEALTH_29 renders eight GPUs, shared allocations, links and external memory above 1 GiB only", async () => {
    const api = apiFixture();
    const { container } = setup("server", api);
    await screen.findByText("GPU 0");
    const table = screen.getByRole("table", { name: "GPU" });
    expect(within(table).getAllByRole("row")).toHaveLength(9);
    expect(screen.getByText("8 张 GPU，Health 全部正常")).toBeVisible();
    expect(screen.getByText("2 张已分配")).toBeVisible();
    expect(screen.getByText("6 张空闲")).toBeVisible();
    expect(screen.getAllByText("与另 1 张 GPU 共同承载")).toHaveLength(2);
    screen
      .getAllByRole("link", { name: "qwen-chat-a" })
      .forEach((link) =>
        expect(link).toHaveAttribute(
          "href",
          "/models/instances/00000000-0000-4000-8000-000000000001",
        ),
      );
    expect(screen.getByText("平台外占用 1.5 GiB")).toHaveAttribute(
      "title",
      expect.stringContaining("显存不足"),
    );
    expect(screen.queryByText("平台外占用 0.6 GiB")).not.toBeInTheDocument();
    expect(screen.getByText("1 张空闲卡有平台外显存占用")).toBeVisible();
    expect(screen.getByText("72%")).toBeVisible();
    expect(within(table).getAllByText("80 W")).toHaveLength(8);
    expect(container).not.toHaveTextContent("internal.example");
    expect(container).not.toHaveTextContent("private-internal-model");
    expect(
      (
        await axe.run(container, {
          rules: { "color-contrast": { enabled: false } },
        })
      ).violations,
    ).toEqual([]);
  });
  it.each([
    "Starting",
    "Running",
    "Restarting",
    "Stopping",
    "Unhealthy",
    "WaitingForResource",
    "Stopped",
    "Failed",
  ] as const)(
    "TC_HEALTH_29 shares allocation semantics for %s",
    async (status) => {
      const api = apiFixture();
      vi.mocked(api.getModelInstances).mockResolvedValue({
        items: [instance(status)],
      });
      setup("server", api);
      await screen.findByText("GPU 0");
      const occupied = [
        "Starting",
        "Running",
        "Restarting",
        "Stopping",
        "Unhealthy",
      ].includes(status);
      expect(
        screen.queryAllByRole("link", { name: "qwen-chat-a" }),
      ).toHaveLength(occupied ? 2 : 0);
      expect(
        screen.getByText(occupied ? "2 张已分配" : "0 张已分配"),
      ).toBeVisible();
    },
  );
  it("TC_HEALTH_22 renders null metrics as dashes with sampled time; telemetry failure keeps inventory and Health", async () => {
    const api = apiFixture();
    const value = telemetry();
    value.items.forEach((item) => {
      item.current.utilization_percent = null;
      item.current.memory_used_bytes = null;
      item.current.temperature_celsius = null;
      item.current.power_watts = null;
    });
    vi.mocked(api.getGpuTelemetry).mockResolvedValue(value);
    setup("server", api);
    await screen.findByText("GPU 0");
    const table = screen.getByRole("table", { name: "GPU" });
    for (const id of ["utilization", "memory", "temperature", "power"]) {
      const cells = table.querySelectorAll(`td[data-column="${id}"]`);
      expect(cells).toHaveLength(8);
      cells.forEach((cell) => {
        expect(cell).toHaveTextContent(/^—$/);
        expect(cell.querySelector("[title]")).toHaveAttribute(
          "title",
          expect.stringContaining("2026"),
        );
      });
    }
    expect(screen.getByText(/GPU 清单与 Health 不受影响/)).toBeVisible();
    vi.mocked(api.getGpuTelemetry).mockRejectedValue(
      new Error("http://internal.example/secret"),
    );
    await userEvent.click(screen.getByRole("button", { name: "重新检测" }));
    await waitFor(() => {
      expect(api.getGpuTelemetry).toHaveBeenCalledTimes(2);
    });
    expect(within(table).getAllByText("正常")).toHaveLength(8);
    expect(screen.getAllByRole("link", { name: "qwen-chat-a" })).toHaveLength(
      2,
    );
    expect(screen.queryByText(/internal.example/)).not.toBeInTheDocument();
  });
  it("TC_HEALTH_25 does not call unknown allocation free and permits retry", async () => {
    const api = apiFixture();
    vi.mocked(api.getModelInstances).mockRejectedValueOnce(
      new ApiError({
        status: 403,
        code: "permission_denied",
        responseMessage: "/secret/path",
      }),
    );
    setup("server", api);
    await screen.findByText("无法获取分配信息");
    expect(screen.queryAllByText("空闲")).toHaveLength(0);
    expect(screen.queryByText(/平台外占用/)).not.toBeInTheDocument();
    await userEvent.click(
      within(screen.getByRole("alert")).getByRole("button", {
        name: "重试",
      }),
    );
    expect(await screen.findByText("2 张已分配")).toBeVisible();
  });
  it("TC_HEALTH_28 counts all three groups, merges Docker/Compose once and sorts abnormal before quiet", async () => {
    const api = apiFixture(),
      data = components();
    for (const item of data.host_environment.items)
      if (item.id === "docker" || item.id === "compose") {
        item.status = "unsatisfied";
        item.reason = "below_minimum";
      }
    required(data.components.find((item) => item.id === "Gateway")).status =
      "unavailable";
    required(
      data.components.find((item) => item.id === "Request Ledger Writer"),
    ).status = "degraded";
    required(data.components.find((item) => item.id === "Edge")).status =
      "not_deployed";
    required(data.inference_engines[1]).driver_compatibility = "incompatible";
    required(data.inference_engines[1]).reason = "below_minimum";
    required(data.inference_engines[1]).minimum_driver_version = "600";
    vi.mocked(api.getSystemComponents).mockResolvedValue(data);
    const { container } = setup("components", api);
    expect(
      await screen.findByRole("button", { name: "收起子模块（10）" }),
    ).toHaveAttribute("aria-expanded", "true");
    expect(
      within(screen.getByRole("tab", { name: /平台组件/ })).getByLabelText(
        "4 项需要关注",
      ),
    ).toBeVisible();
    expect(screen.getByRole("alert")).toHaveTextContent("4 项需要关注");
    const rows = within(
      screen.getByRole("table", { name: "Holm 子模块" }),
    ).getAllByRole("row");
    expect(rows[1]).toHaveTextContent("推理网关");
    expect(rows[2]).toHaveTextContent("调用账本写入器");
    expect(rows.at(-1)).toHaveTextContent("接入层");
    expect(rows.at(-1)).toHaveTextContent("未上报状态");
    expect(screen.getByText("数据库").closest("tr")).toHaveTextContent(
      "PostgreSQL 16.15 · Schema 20261014_0040",
    );
    expect(rows[2]).toHaveTextContent("待写事件 1284");
    expect(rows[2]).toHaveTextContent("最旧事件 2026");
    expect(
      screen.getByText("需先升级驱动：要求 ≥ 600，当前 595.45.04。"),
    ).toBeVisible();
    expect(container).not.toHaveTextContent("registry.internal");
    expect(screen.getByText("sha256:…3f9a1c")).toBeVisible();
    expect(
      (
        await axe.run(container, {
          rules: { "color-contrast": { enabled: false } },
        })
      ).violations,
    ).toEqual([]);
  });
  it("TC_HEALTH_28 collapses all-ready Holm and does not count unknown host or engine states", async () => {
    const api = apiFixture(),
      data = components();
    required(data.host_environment.items[0]).status = "unknown";
    required(data.host_environment.items[0]).reason = "minimum_not_declared";
    required(data.host_environment.items[0]).minimum = null;
    required(data.inference_engines[0]).driver_compatibility = "unknown";
    required(data.inference_engines[0]).reason = "minimum_not_declared";
    required(data.inference_engines[0]).active_instance_count = null;
    vi.mocked(api.getSystemComponents).mockResolvedValue(data);
    setup("components", api);
    expect(await screen.findByText("全部就绪")).toBeVisible();
    expect(
      screen.getByRole("button", { name: "展开子模块（10）" }),
    ).toHaveAttribute("aria-expanded", "false");
    expect(
      screen.queryByRole("table", { name: "Holm 子模块" }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "平台组件" })).toBeVisible();
    expect(screen.getByText("宿主环境、Holm 与推理引擎均无异常")).toBeVisible();
    expect(screen.getAllByText("无法判断")).toHaveLength(2);
    expect(screen.getByText("发行清单尚未声明最低版本。")).toBeVisible();
    expect(screen.getByText("兼容清单尚未声明最低驱动版本。")).toBeVisible();
    const row = required(screen.getByText("vLLM").closest("tr"));
    expect(row.querySelector('[data-column="instances"]')).toHaveTextContent(
      /^—$/,
    );
  });
  it.each([
    ["satisfied", "satisfied", "satisfied"],
    ["unknown", "satisfied", "unknown"],
    ["satisfied", "unknown", "unknown"],
    ["unknown", "unsatisfied", "unsatisfied"],
    ["unsatisfied", "unknown", "unsatisfied"],
  ] as const)(
    "TC_HEALTH_26 merges %s/%s to %s with side-specific reasons",
    (dockerStatus, composeStatus, expected) => {
      const data = components();
      const docker = required(
        data.host_environment.items.find((item) => item.id === "docker"),
      );
      const compose = required(
        data.host_environment.items.find((item) => item.id === "compose"),
      );
      docker.status = dockerStatus;
      compose.status = composeStatus;
      for (const item of [docker, compose])
        item.reason =
          item.status === "satisfied"
            ? "meets_minimum"
            : item.status === "unsatisfied"
              ? "below_minimum"
              : "current_unavailable";
      const row = required(hostRows(data)[1]);
      expect(row.status).toBe(expected);
      expect(row.note.includes("Docker：")).toBe(dockerStatus !== "satisfied");
      expect(row.note.includes("Compose：")).toBe(
        composeStatus !== "satisfied",
      );
      expect(componentAttention(data)).toHaveLength(
        expected === "unsatisfied" ? 1 : 0,
      );
    },
  );
  it("TC_HEALTH_20 rejects a missing host row instead of silently showing a healthy summary", () => {
    const value = components();
    expect(hostRows(value)).toHaveLength(3);
    value.host_environment.items = value.host_environment.items.filter(
      (item) => item.id !== "compose",
    );
    expect(() => componentAttention(value)).toThrow(
      "unsupported system response",
    );
  });
  it("TC_HEALTH_30 polls telemetry at 10s, components/instances at 30s and inventory only on detection", async () => {
    vi.useFakeTimers();
    const api = apiFixture();
    setup("server", api);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });
    expect(api.getSystemServer).toHaveBeenCalledTimes(1);
    expect(api.getGpuTelemetry).toHaveBeenCalledTimes(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    expect(api.getGpuTelemetry).toHaveBeenCalledTimes(2);
    expect(api.getSystemComponents).toHaveBeenCalledTimes(1);
    expect(api.getModelInstances).toHaveBeenCalledTimes(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(20_000);
    });
    expect(api.getGpuTelemetry).toHaveBeenCalledTimes(4);
    expect(api.getSystemComponents).toHaveBeenCalledTimes(2);
    expect(api.getModelInstances).toHaveBeenCalledTimes(2);
    expect(api.getSystemServer).toHaveBeenCalledTimes(1);
    fireEvent.mouseDown(screen.getByRole("tab", { name: "平台组件" }), {
      button: 0,
      ctrlKey: false,
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000);
    });
    expect(api.getSystemServer).toHaveBeenCalledTimes(1);
    expect(api.getSystemComponents).toHaveBeenCalledTimes(3);
    expect(api.getModelInstances).toHaveBeenCalledTimes(3);
    expect(api.getGpuTelemetry).toHaveBeenCalledTimes(7);
    fireEvent.click(screen.getByRole("button", { name: "重新检测" }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });
    expect(api.getSystemServer).toHaveBeenCalledTimes(2);
    expect(api.getSystemComponents).toHaveBeenCalledTimes(4);
    expect(api.getModelInstances).toHaveBeenCalledTimes(4);
    expect(api.getGpuTelemetry).toHaveBeenCalledTimes(8);
    focusManager.setFocused(false);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000);
    });
    expect(api.getGpuTelemetry).toHaveBeenCalledTimes(8);
    expect(api.getSystemComponents).toHaveBeenCalledTimes(4);
    cleanup();
  });
});
