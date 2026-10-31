import axe from "axe-core";
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import type { ModelInstance } from "../api/model-instance-contracts";
import {
  ADMIN_ME,
  FakeApiClient,
  renderApplication,
} from "../app/test-support";

const INSTANCE_ID = "0198f58a-2586-7e37-bbf5-000000001414";

describe("PROTO_14 telemetry surfaces", () => {
  it("PROTO_14 shows the deploy-first-model guide for an empty system", async () => {
    const apiClient = new FakeApiClient(ADMIN_ME);
    apiClient.modelInstanceResults.push({ items: [] });
    renderApplication(apiClient, "/overview");

    expect(
      await screen.findByRole("heading", { name: "部署第一个模型" }),
    ).toBeVisible();
    expect(screen.getByRole("button", { name: "部署模型" })).toBeVisible();
    expect(screen.queryByText("请求")).not.toBeInTheDocument();
  });

  it("PROTO_14 renders unavailable overview values as em dashes, never zero", async () => {
    const apiClient = new FakeApiClient(ADMIN_ME);
    apiClient.modelInstanceResults.push({ items: [instance()] });
    apiClient.overviewTelemetryResults.push({
      current: {
        error_rate: null,
        requests_1h: null,
        error_rate_1h: null,
        window_started_at: null,
        gpu_allocated: null,
        gpu_available: null,
        gpu_total: null,
        requests_running: null,
        requests_waiting: null,
        ttft_p95_ms: null,
        services_available: null,
        services_starting: null,
        services_total: null,
        services_unavailable: null,
      },
      sampled_at: null,
      telemetry_status: "unavailable",
    });
    renderApplication(apiClient, "/overview");

    const gpuCard = (
      await screen.findByRole("heading", { name: "GPU" })
    ).closest("section");
    expect(gpuCard).not.toBeNull();
    expect(within(gpuCard as HTMLElement).getAllByText("—")).toHaveLength(3);
    expect(screen.getByText(/不影响 AI 服务调用/)).toBeVisible();
  });

  it("PROTO_14 marks missing AI service backend metrics as backend totals", async () => {
    const apiClient = new FakeApiClient(ADMIN_ME);
    apiClient.modelInstanceResults.push({ items: [instance()] });
    apiClient.aiServiceResults.push({
      items: [
        {
          description: "",
          enabled: true,
          id: "0198f58a-2586-7e37-bbf5-000000001415",
          primary_instance_id: INSTANCE_ID,
          primary_instance_status: "Running",
          name: "研发助手",
          public_model_name: "qwen-mini",
          route_version: 1,
          runtime_status: "Available",
        },
      ],
    });
    renderApplication(apiClient, "/services");

    const row = (await screen.findByText("qwen-mini")).closest("tr");
    expect(row).not.toBeNull();
    const waiting = (row as HTMLElement).querySelector(
      '[data-column="concurrency"]',
    );
    expect(waiting).not.toBeNull();
    const missing = within(waiting as HTMLElement).getAllByText("—");
    expect(missing).toHaveLength(2);
    for (const mark of missing) expect(mark).toBeVisible();
    expect(
      within(waiting as HTMLElement).queryByText("后端总量"),
    ).not.toBeInTheDocument();
    expect(
      within(waiting as HTMLElement).queryByText("共享后端"),
    ).not.toBeInTheDocument();
    expect(
      within(
        (row as HTMLElement).querySelector(
          '[data-column="today-requests"]',
        ) as HTMLElement,
      ).getByText("无权查看今日用量"),
    ).toBeVisible();
  });

  it("M6_04 distinguishes an unsupported metric from a collected zero", async () => {
    const apiClient = new FakeApiClient(ADMIN_ME);
    apiClient.modelInstanceDetailResults.push(instance());
    apiClient.instanceTelemetryResults.push({
      quality: "full",
      window_1h: {
        requests: null,
        error_rate: null,
        ttft_p95_ms: null,
        started_at: null,
      },
      current: {
        kv_cache_usage_ratio: 0,
        output_tps: 0,
        queue_time_p50_seconds: null,
        queue_time_p95_seconds: null,
        running: 0,
        ttft_p50_seconds: null,
        ttft_p95_seconds: null,
        ttft_p99_seconds: null,
        unsupported: ["runtime_ttft_seconds"],
        waiting: 0,
      },
      instance_id: INSTANCE_ID,
      sampled_at: "2026-09-21T00:00:00Z",
      series: [],
      telemetry_status: "available",
    });
    // UI-09B: overview and monitoring each read the same telemetry response.
    apiClient.instanceTelemetryResults.push(
      ...apiClient.instanceTelemetryResults,
    );
    renderApplication(apiClient, `/models/instances/${INSTANCE_ID}`);

    await userEvent
      .setup()
      .click(await screen.findByRole("tab", { name: "监控" }));
    const ttft = screen
      .getByRole("heading", {
        name: "TTFT P50 / P95 / P99",
      })
      .closest("section");
    const tps = screen
      .getByRole("heading", { name: "输出 TPS" })
      .closest("section");
    expect(ttft).not.toBeNull();
    expect(tps).not.toBeNull();
    expect(within(ttft as HTMLElement).getAllByText("不支持")).not.toHaveLength(
      0,
    );
    expect(within(tps as HTMLElement).getByText("0.0 tokens/s")).toBeVisible();
  });

  it("M6_08A renders TTFT P99 in the third ordinal slot without axe violations", async () => {
    const apiClient = new FakeApiClient(ADMIN_ME);
    apiClient.modelInstanceDetailResults.push(instance());
    apiClient.instanceTelemetryResults.push({
      quality: "full",
      window_1h: {
        requests: null,
        error_rate: null,
        ttft_p95_ms: null,
        started_at: null,
      },
      current: {
        kv_cache_usage_ratio: null,
        output_tps: null,
        queue_time_p50_seconds: null,
        queue_time_p95_seconds: null,
        running: null,
        ttft_p50_seconds: 0.1,
        ttft_p95_seconds: 0.75,
        ttft_p99_seconds: 0.95,
        unsupported: [],
        waiting: null,
      },
      instance_id: INSTANCE_ID,
      sampled_at: "2026-09-22T00:00:05Z",
      series: [
        {
          kv_cache_usage_ratio: null,
          output_tps: null,
          queue_time_p50_seconds: null,
          queue_time_p95_seconds: null,
          running: null,
          sampled_at: "2026-09-22T00:00:00Z",
          quality: "full",
          telemetry_status: "available",
          ttft_p50_seconds: 0.1,
          ttft_p95_seconds: 0.75,
          ttft_p99_seconds: 0.95,
          unsupported: [],
          waiting: null,
        },
      ],
      telemetry_status: "available",
    });
    // UI-09B: overview and monitoring each read the same telemetry response.
    apiClient.instanceTelemetryResults.push(
      ...apiClient.instanceTelemetryResults,
    );
    const { container } = renderApplication(
      apiClient,
      `/models/instances/${INSTANCE_ID}`,
    );

    await userEvent
      .setup()
      .click(await screen.findByRole("tab", { name: "监控" }));
    const ttft = screen
      .getByRole("heading", { name: "TTFT P50 / P95 / P99" })
      .closest("section");
    expect(ttft).not.toBeNull();
    const legend = within(ttft as HTMLElement).getByRole("list", {
      name: "图例",
    });
    expect(within(legend).getByText("P99")).toBeVisible();
    expect(
      (ttft as HTMLElement).querySelector(".pw-chart__series--ordinal-3"),
    ).not.toBeNull();

    const axeResult = await axe.run(container, {
      rules: { "color-contrast": { enabled: false } },
    });
    expect(axeResult.passes.flatMap((rule) => rule.nodes)).not.toHaveLength(0);
    expect(
      axeResult.violations.filter(
        ({ impact }) => impact === "critical" || impact === "serious",
      ),
    ).toEqual([]);
  });

  it("M6_08A renders an empty TTFT window as missing data instead of zero", async () => {
    const apiClient = new FakeApiClient(ADMIN_ME);
    apiClient.modelInstanceDetailResults.push(instance());
    apiClient.instanceTelemetryResults.push({
      quality: "full",
      window_1h: {
        requests: null,
        error_rate: null,
        ttft_p95_ms: null,
        started_at: null,
      },
      current: {
        kv_cache_usage_ratio: null,
        output_tps: null,
        queue_time_p50_seconds: null,
        queue_time_p95_seconds: null,
        running: null,
        ttft_p50_seconds: null,
        ttft_p95_seconds: null,
        ttft_p99_seconds: null,
        unsupported: [],
        waiting: null,
      },
      instance_id: INSTANCE_ID,
      sampled_at: null,
      series: [],
      telemetry_status: "available",
    });
    renderApplication(apiClient, `/models/instances/${INSTANCE_ID}`);

    await userEvent
      .setup()
      .click(await screen.findByRole("tab", { name: "监控" }));
    const ttft = screen
      .getByRole("heading", { name: "TTFT P50 / P95 / P99" })
      .closest("section");
    expect(ttft).not.toBeNull();
    expect(
      within(ttft as HTMLElement).getByLabelText("暂无数据"),
    ).toBeVisible();
    expect(within(ttft as HTMLElement).getAllByText("—")).toHaveLength(2);
    expect(
      within(ttft as HTMLElement).queryByText(/^0(?:\.0+)?(?: ms)?$/),
    ).toBeNull();
  });
});

function instance(): ModelInstance {
  return {
    artifact_id: "0198f58a-2586-7e37-bbf5-000000001413",
    config_version: 1,
    updated_at: "2026-10-08T09:00:00Z",
    context_length: 4096,
    error_code: null,
    gpu_memory_utilization: 0.9,
    restart_count: 0,
    gpu_uuids: ["GPU-fixture-2"],
    id: INSTANCE_ID,
    internal_endpoint: null,
    name: "Qwen2.5-0.5B",
    internal_model_name: "Qwen2.5-0.5B",
    runtime_type: "vllm",
    status: "Running",
    tensor_parallel_size: 1,
  };
}

describe("M6_11 TC-TM-03/05/06 TC-UI-08/18/20 service monitor", () => {
  const SERVICE_ID = "0198f58a-2586-7e37-bbf5-000000001415";
  function service() {
    return {
      description: "",
      enabled: true,
      id: SERVICE_ID,
      primary_instance_id: INSTANCE_ID,
      primary_instance_status: "Running" as const,
      name: "研发助手",
      public_model_name: "qwen-mini",
      route_version: 1,
      runtime_status: "Available" as const,
    };
  }
  it("M6_11 TC-UI-18 separates user and backend TTFT charts and switches range", async () => {
    const apiClient = new FakeApiClient(ADMIN_ME);
    apiClient.aiServiceDetailResults.push(service());
    apiClient.modelInstanceResults.push({ items: [instance()] });
    const empty = await apiClient.getServiceTelemetry(SERVICE_ID);
    const current = {
      ...empty.current,
      ttft_p50_ms: 10,
      ttft_p95_ms: 20,
      ttft_p99_ms: 30,
      e2e_latency_p50_ms: 100,
      e2e_latency_p95_ms: 200,
      e2e_latency_p99_ms: 300,
      output_tps_avg: 5,
      inflight_last: 2,
      error_rate: 0.1,
    };
    apiClient.serviceTelemetryResults.push({
      ...empty,
      telemetry_status: "available",
      sampled_at: "2026-09-23T00:00:00Z",
      current,
      series: [
        {
          ...current,
          sampled_at: "2026-09-23T00:00:00Z",
          quality: "full",
          telemetry_status: "available",
        },
      ],
    });
    renderApplication(apiClient, `/services/${SERVICE_ID}`);
    await userEvent
      .setup()
      .click(await screen.findByRole("tab", { name: "监控" }));
    expect(
      await screen.findByRole("img", { name: /用户侧 TTFT/ }),
    ).toBeVisible();
    expect(screen.getByText("当前 Runtime 不支持服务级排队耗时")).toBeVisible();
    const e2eChart = screen
      .getByRole("img", { name: /用户侧端到端耗时/ })
      .closest(".pw-chart") as HTMLElement;
    await userEvent
      .setup()
      .click(within(e2eChart).getByRole("button", { name: "查看数据表" }));
    const e2eTable = within(e2eChart).getByRole("table");
    expect(
      within(e2eTable).getByRole("row", { name: "P50 100" }),
    ).toBeVisible();
    expect(
      within(e2eTable).getByRole("row", { name: "P95 200" }),
    ).toBeVisible();
    expect(
      within(e2eTable).getByRole("row", { name: "P99 300" }),
    ).toBeVisible();
    await userEvent
      .setup()
      .selectOptions(screen.getByRole("combobox", { name: "时间范围" }), "6h");
    expect(screen.getByRole("combobox", { name: "时间范围" })).toHaveValue(
      "6h",
    );
    const result = await axe.run(document.body);
    expect(
      result.violations.filter(
        ({ impact }) => impact === "critical" || impact === "serious",
      ),
    ).toEqual([]);
  });

  it("FIX_10 TC-UI-18 shows a dash when service latency has no samples", async () => {
    const apiClient = new FakeApiClient(ADMIN_ME);
    apiClient.aiServiceDetailResults.push(service());
    apiClient.modelInstanceResults.push({ items: [instance()] });
    renderApplication(apiClient, `/services/${SERVICE_ID}`);
    await userEvent
      .setup()
      .click(await screen.findByRole("tab", { name: "监控" }));
    const panel = screen
      .getByRole("heading", { name: "用户侧端到端耗时 P50 / P95 / P99" })
      .closest(".pw-monitor-card") as HTMLElement;
    expect(
      within(panel.querySelector("strong") as HTMLElement).getByLabelText(
        "暂无数据",
      ),
    ).toHaveTextContent("—");
    const result = await axe.run(document.body);
    expect(
      result.violations.filter(
        ({ impact }) => impact === "critical" || impact === "serious",
      ),
    ).toEqual([]);
  });

  it("M6_11 TC-UI-08/20 retains service status when telemetry is unavailable", async () => {
    const apiClient = new FakeApiClient(ADMIN_ME);
    apiClient.aiServiceResults.push({ items: [service()] });
    apiClient.modelInstanceResults.push({ items: [instance()] });
    renderApplication(apiClient, "/services");
    const row = (await screen.findByText("qwen-mini")).closest(
      "tr",
    ) as HTMLElement;
    expect(within(row).getByRole("img", { name: "可用" })).toBeVisible();
    expect(
      within(
        row.querySelector('[data-column="ttft"]') as HTMLElement,
      ).getByLabelText("暂无数据"),
    ).toBeVisible();
    expect(
      within(
        row.querySelector('[data-column="ttft"]') as HTMLElement,
      ).getByLabelText("暂无数据"),
    ).toHaveAccessibleDescription("服务遥测未采集或窗口内无请求");
  });
});

describe("M6_11 Overview and service list metrics", () => {
  it("M6_11 TC-TM-06 opens the sorted monitor view with platform metrics", async () => {
    const apiClient = new FakeApiClient(ADMIN_ME);
    apiClient.modelInstanceResults.push({ items: [instance()] });
    const overview = await apiClient.getTelemetryOverview();
    apiClient.overviewTelemetryResults.push({
      ...overview,
      current: {
        ...overview.current,
        requests_running: 3,
        requests_1h: 25,
        error_rate_1h: 0.2,
        ttft_p95_ms: 125,
        error_rate: 0.2,
      },
    });
    renderApplication(apiClient, "/overview");
    expect(await screen.findByText("25")).toBeVisible();
    expect(screen.getByText("请求数（近 1 小时）")).toBeVisible();
    expect(screen.getByText("错误率（近 1 小时）")).toBeVisible();
    expect(
      within(screen.getByRole("link", { name: /请求 · 查看监控/ })).queryByText(
        "TTFT P95",
      ),
    ).not.toBeInTheDocument();
    expect(screen.getByText("20%")).toBeVisible();
    await userEvent
      .setup()
      .click(screen.getByRole("link", { name: /请求 · 查看监控/ }));
    expect(await screen.findByText(/监控视图：按 Error Rate/)).toBeVisible();
  });

  it("M6_11 TC-UI-20 shows today's requests from the usage report and shared backend totals", async () => {
    const apiClient = new FakeApiClient({
      ...ADMIN_ME,
      capabilities: [
        ...ADMIN_ME.capabilities,
        { capability: "view_usage", scope: "all" },
      ],
    });
    const first = {
      description: "",
      enabled: true,
      id: "0198f58a-2586-7e37-bbf5-000000001415",
      primary_instance_id: INSTANCE_ID,
      primary_instance_status: "Running" as const,
      name: "研发助手",
      public_model_name: "qwen-a",
      route_version: 1,
      runtime_status: "Available" as const,
    };
    const second = {
      ...first,
      id: "0198f58a-2586-7e37-bbf5-000000001416",
      name: "研发助手",
      public_model_name: "qwen-b",
    };
    apiClient.aiServiceResults.push({ items: [first, second] });
    apiClient.modelInstanceResults.push({ items: [instance()] });
    const usage = await apiClient.getUsage({
      from: "2026-09-23T00:00:00Z",
      to: "2026-09-23T01:00:00Z",
      groupBy: "service",
      timeZone: "UTC",
    });
    apiClient.usageResults.push({
      ...usage,
      details: [
        {
          display_name: "qwen-a",
          group_id: first.id,
          is_deleted: false,
          status: null,
          error_rate: null,
          error_requests: 0,
          input_tokens: 0,
          output_tokens: 0,
          partial_token_requests: 0,
          requests: 7,
          total_tokens: 0,
        },
      ],
    });
    renderApplication(apiClient, "/services");
    const firstRow = (await screen.findByText("qwen-a")).closest(
      "tr",
    ) as HTMLElement;
    expect(
      await within(
        firstRow.querySelector('[data-column="today-requests"]') as HTMLElement,
      ).findByText("7"),
    ).toBeVisible();
    expect(
      within(
        firstRow.querySelector('[data-column="concurrency"]') as HTMLElement,
      ).getByText(/共享后端/),
    ).toBeVisible();
  });
});
