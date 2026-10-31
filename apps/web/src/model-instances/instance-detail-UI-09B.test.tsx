import { screen, within, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import axe from "axe-core";
import { describe, expect, it, vi } from "vitest";
import type {
  ModelInstance,
  ModelInstanceEvent,
} from "../api/model-instance-contracts";
import type { InstanceTelemetry } from "../api/telemetry-contracts";
import { ApiError } from "../api/client";
import {
  ADMIN_ME,
  DEFAULT_SYSTEM_SERVER,
  FakeApiClient,
  renderApplication,
} from "../app/test-support";

// FX-UI-09B: synthetic API responses derived from the existing instance, telemetry,
// GPU inventory and event contracts; values intentionally differ from the mockups.
const ID = "0198f58a-2586-7e37-bbf5-000000000012";
const SERVICE_ID = "0198f58a-2586-7e37-bbf5-000000000008";
const item: ModelInstance = {
  id: ID,
  name: "研发实例",
  artifact_id: "0198f58a-2586-7e37-bbf5-000000000010",
  internal_model_name: "测试模型",
  internal_endpoint: null,
  status: "Running",
  config_version: 1,
  updated_at: "2026-10-09T04:00:00Z",
  error_code: null,
  gpu_uuids: ["GPU-UI09B"],
  gpu_memory_utilization: 0.9,
  restart_count: 0,
  runtime_type: "vllm",
  tensor_parallel_size: 1,
  context_length: 4096,
};
const telemetry: InstanceTelemetry = {
  instance_id: ID,
  sampled_at: "2026-10-09T04:00:00Z",
  telemetry_status: "available",
  quality: "full",
  series: [],
  window_1h: {
    requests: 200,
    error_rate: 0.035,
    ttft_p95_ms: 73,
    started_at: "2026-10-09T03:45:00Z",
  },
  current: {
    running: 7,
    waiting: 2,
    output_tps: 86.4,
    kv_cache_usage_ratio: 0.27,
    ttft_p50_seconds: 0.004,
    ttft_p95_seconds: 0.009,
    ttft_p99_seconds: 0.015,
    queue_time_p50_seconds: null,
    queue_time_p95_seconds: null,
    unsupported: [],
  },
};
function client(status: ModelInstance["status"] = "Running") {
  const api = new FakeApiClient(ADMIN_ME);
  api.modelInstanceDetailResults.push({ ...item, status });
  vi.spyOn(api, "getInstanceTelemetry").mockResolvedValue(
    structuredClone(telemetry),
  );
  const server = structuredClone(DEFAULT_SYSTEM_SERVER);
  if (server.agent.status !== "available")
    throw new Error("invalid inventory fixture");
  server.agent.gpus.devices = [
    {
      uuid: "GPU-UI09B",
      display_index: 3,
      model: "测试 GPU",
      memory_bytes: 16 * 1024 ** 3,
      health: "healthy",
      health_reasons: [],
    },
  ];
  server.agent.gpus.discovery_state = "ready";
  vi.spyOn(api, "getSystemServer").mockResolvedValue(server);
  vi.spyOn(api, "getGpuTelemetry").mockResolvedValue({
    sampled_at: telemetry.sampled_at,
    telemetry_status: "available",
    quality: "full",
    items: [
      {
        uuid: "GPU-UI09B",
        sampled_at: telemetry.sampled_at,
        telemetry_status: "available",
        quality: "full",
        series: [],
        current: {
          health: "GPU_HEALTH_STATE_HEALTHY",
          utilization_percent: 43,
          memory_used_bytes: 4 * 1024 ** 3,
          temperature_celsius: 56,
          power_watts: 127,
        },
      },
    ],
  });
  vi.spyOn(api, "getAiServices").mockResolvedValue({
    items: [
      {
        id: SERVICE_ID,
        name: "研发助手",
        description: "团队内测",
        enabled: true,
        public_model_name: "research",
        primary_instance_id: ID,
        primary_instance_status: status,
        runtime_status: status === "Running" ? "Available" : "Unavailable",
        route_version: 1,
      },
    ],
  });
  vi.spyOn(api, "getModelInstanceEvents").mockResolvedValue({
    items: [],
    next_cursor: null,
  });
  return api;
}
function renderDetail(api: FakeApiClient) {
  return renderApplication(api, `/models/instances/${ID}`);
}
function fact(region: HTMLElement, label: string) {
  const value = within(region)
    .getByText(label)
    .closest("div")
    ?.querySelector("dd");
  if (!value) throw new Error(`missing fact: ${label}`);
  return value;
}

describe("UI-09B instance detail", () => {
  it("TC-UI-01 renders running header, six metrics with window provenance, GPU and services; passes axe", async () => {
    const { container } = renderDetail(client());
    const title = await screen.findByRole("heading", {
      level: 1,
      name: "研发实例",
    });
    expect(title).not.toHaveTextContent(ID.slice(0, 8));
    expect(
      within(screen.getByRole("navigation", { name: "面包屑" })).getByRole(
        "link",
        { name: "模型实例" },
      ),
    ).toHaveAttribute("href", "/models");
    expect(container.querySelector(".pw-topbar__title")).toHaveTextContent(
      "模型",
    );
    const head = title.closest("header");
    if (head === null) throw new Error("missing header");
    expect(within(head).getByRole("img", { name: "运行中" })).toHaveClass(
      "pw-service-signal--positive",
    );
    expect(head).toHaveTextContent("测试模型 · vLLM · 1 × 测试 GPU");
    expect(
      within(head)
        .getAllByRole("button")
        .map((button) => button.textContent),
    ).toEqual(["发布服务", "重启", "停止"]);
    const summary = screen.getByRole("region", { name: "运行摘要" });
    await waitFor(() =>
      expect(fact(summary, "当前请求")).toHaveTextContent("7"),
    );
    expect(fact(summary, "等待请求")).toHaveTextContent("2");
    expect(fact(summary, "TTFT P95（近 1 小时）")).toHaveTextContent("73 ms");
    expect(fact(summary, "输出 TPS")).toHaveTextContent("86.4 tokens/s");
    expect(fact(summary, "错误率（近 1 小时）")).toHaveTextContent("3.5 %");
    expect(fact(summary, "KV Cache 使用率")).toHaveTextContent("27 %");
    expect(summary).toHaveTextContent("遥测不足 1 小时，起算于");
    const gpu = screen.getByRole("region", { name: "GPU" });
    expect(gpu).toHaveTextContent("43 %");
    expect(gpu).toHaveTextContent("4 GB / 16 GB");
    expect(gpu).toHaveTextContent("56 °C");
    expect(gpu).toHaveTextContent("127 W");
    expect(gpu).toHaveTextContent("Health：健康");
    expect(
      within(gpu).getByRole("meter", { name: "显存使用比例" }),
    ).toHaveAttribute("aria-valuenow", "25");
    const services = screen.getByRole("region", { name: "关联服务" });
    expect(
      within(services).getByRole("link", { name: "研发助手" }),
    ).toHaveAttribute("href", `/services/${SERVICE_ID}`);
    expect(services).toHaveTextContent("可用 · 团队内测");
    expect((await axe.run(container)).violations).toEqual([]);
  });

  it.each([
    ["Stopped", "已停止", "idle", ["启动", "删除"]],
    ["Failed", "运行失败", "negative", ["修改配置并重试", "重试", "删除"]],
    ["Starting", "启动中", "notice", []],
    ["Restarting", "重启中", "notice", ["停止"]],
    ["WaitingForResource", "等待 GPU", "idle", ["重新选择 GPU", "取消部署"]],
    ["Unhealthy", "运行异常", "negative", ["停止"]],
  ] as const)(
    "UI-09B shows %s signal and only its applicable lifecycle actions",
    async (state, label, tone, actions) => {
      renderDetail(client(state));
      const head = (
        await screen.findByRole("heading", { name: "研发实例", level: 1 })
      ).closest("header");
      if (head === null) throw new Error("missing header");
      expect(within(head).getByRole("img", { name: label })).toHaveClass(
        `pw-service-signal--${tone}`,
      );
      expect(
        within(head)
          .queryAllByRole("button")
          .map((button) => button.textContent),
      ).toEqual(actions);
      expect(
        screen.getByRole("region", { name: "运行摘要" }),
      ).toHaveTextContent("实例未运行，没有实时指标。");
      if (state === "Stopped" || state === "Failed") {
        expect(
          within(head).getByRole("button", { name: "删除" }),
        ).toBeEnabled();
        expect(within(head).queryByRole("button", { name: /请先/ })).toBeNull();
      }
      if (state === "Stopped") {
        expect(head).toHaveTextContent("GPU 已释放");
        const gpu = screen.getByRole("region", { name: "GPU" });
        expect(gpu).toHaveTextContent("16 GB · 已释放");
        expect(gpu).not.toHaveTextContent(/利用率|温度|功耗|Health/);
        expect(within(gpu).queryByRole("meter")).toBeNull();
      }
    },
  );

  it.each(["unknown", "unsupported", "empty", "offline"])(
    "UI-09B keeps %s telemetry unknown without fabricated zeros",
    async (state) => {
      const api = client();
      const data = structuredClone(telemetry);
      if (state === "unsupported")
        data.current.unsupported = ["runtime_errors_total"];
      else
        data.window_1h = {
          requests: state === "empty" ? 0 : null,
          ttft_p95_ms: null,
          error_rate: null,
          started_at: null,
        };
      if (state === "offline")
        vi.spyOn(api, "getInstanceTelemetry").mockRejectedValue(
          new ApiError({
            code: "forbidden",
            status: 403,
            responseMessage: "private-sentinel",
          }),
        );
      else vi.spyOn(api, "getInstanceTelemetry").mockResolvedValue(data);
      renderDetail(api);
      const summary = await screen.findByRole("region", { name: "运行摘要" });
      await waitFor(() =>
        expect(summary).not.toHaveTextContent("遥测正在加载"),
      );
      expect(fact(summary, "错误率（近 1 小时）")).toHaveTextContent("—");
      expect(fact(summary, "错误率（近 1 小时）")).not.toHaveTextContent("0 %");
      if (state === "unsupported")
        expect(summary).toHaveTextContent("该 Runtime 不提供错误计数");
      if (state === "empty")
        expect(fact(summary, "TTFT P95（近 1 小时）")).toHaveTextContent(
          "近 1 小时无请求",
        );
      if (state === "offline") {
        expect(summary).toHaveTextContent("没有查看遥测的权限");
        expect(fact(summary, "当前请求")).toHaveTextContent("—");
        expect(document.body).not.toHaveTextContent("private-sentinel");
      }
    },
  );

  it("UI-09B TC-UI-02 copies the full ID and opens complete configuration and all events", async () => {
    const api = client("Stopped");
    const events: ModelInstanceEvent[] = [1, 4, 2, 3].map((n) => ({
      id: `0198f58a-2586-7e37-bbf5-${String(n).padStart(12, "0")}`,
      instance_id: ID,
      occurred_at: `2026-10-09T0${String(n)}:00:00Z`,
      event_type: "StatusChanged",
      actor: "admin",
      from_status: "Running",
      to_status: "Stopped",
      detail: {},
    }));
    vi.spyOn(api, "getModelInstanceEvents").mockResolvedValue({
      items: events,
      next_cursor: null,
    });
    const user = userEvent.setup();
    const clipboard = vi.spyOn(navigator.clipboard, "writeText");
    renderDetail(api);
    const recent = await screen.findByRole("region", { name: "近期事件" });
    await waitFor(() => {
      expect(within(recent).getAllByRole("listitem")).toHaveLength(3);
    });
    expect(
      [...recent.querySelectorAll("time")].map((time) => time.dateTime),
    ).toEqual([
      "2026-10-09T04:00:00Z",
      "2026-10-09T03:00:00Z",
      "2026-10-09T02:00:00Z",
    ]);
    await user.click(screen.getByRole("button", { name: "复制实例 ID" }));
    expect(clipboard).toHaveBeenCalledWith(ID);
    await user.click(screen.getByRole("link", { name: "完整配置" }));
    expect(screen.getByRole("tab", { name: "配置" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    await user.click(screen.getByRole("tab", { name: "概览" }));
    await user.click(screen.getByRole("link", { name: "查看全部" }));
    expect(screen.getByRole("tab", { name: "事件" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(
      within(
        await screen.findByRole("table", { name: "模型实例事件" }),
      ).getAllByRole("row"),
    ).toHaveLength(5);
  });

  it("UI-09B preserves safe disabled actions when binding information fails", async () => {
    const api = client();
    vi.spyOn(api, "getAiServices").mockRejectedValue(new Error("offline"));
    renderDetail(api);
    await screen.findByText("无法确认服务绑定");
    expect(screen.getByRole("button", { name: "停止" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "重启" })).toBeDisabled();
    expect(
      screen.getByText(
        "服务绑定信息暂不可用，停止、重启和删除需等待绑定确认。",
      ),
    ).toBeVisible();
  });
  it("UI-09B renders GPU collection failure and empty related data without zero placeholders", async () => {
    const api = client();
    vi.spyOn(api, "getGpuTelemetry").mockRejectedValue(new Error("offline"));
    vi.spyOn(api, "getAiServices").mockResolvedValue({ items: [] });
    renderDetail(api);
    const gpu = await screen.findByRole("region", { name: "GPU" });
    await screen.findByText("暂无关联服务。");
    expect(fact(gpu, "利用率")).toHaveTextContent("—");
    expect(fact(gpu, "显存")).toHaveTextContent("—");
    expect(fact(gpu, "温度")).toHaveTextContent("—");
    expect(fact(gpu, "功耗")).toHaveTextContent("—");
    expect(within(gpu).queryByRole("meter")).toBeNull();
    expect(screen.getByRole("region", { name: "近期事件" })).toHaveTextContent(
      "暂无事件。",
    );
    expect(
      fact(screen.getByRole("region", { name: "基本信息" }), "本地模型大小"),
    ).toHaveTextContent("—");
  });

  it("UI-09B distinguishes delayed telemetry and a full window from shortened windows", async () => {
    const api = client();
    const data = structuredClone(telemetry);
    data.telemetry_status = "delayed";
    data.window_1h.started_at = "2026-10-09T03:00:00Z";
    vi.spyOn(api, "getInstanceTelemetry").mockResolvedValue(data);
    renderDetail(api);
    const summary = await screen.findByRole("region", { name: "运行摘要" });
    await waitFor(() => expect(summary).toHaveTextContent("监控数据延迟"));
    expect(summary).toHaveTextContent("最后更新：");
    expect(summary).not.toHaveTextContent("遥测不足 1 小时");
    expect(fact(summary, "TTFT P95（近 1 小时）")).toHaveTextContent("73 ms");
    expect(screen.getByRole("img", { name: "运行中" })).toBeVisible();
  });
});
