import axe from "axe-core";
import { screen, within, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { AiService } from "../api/ai-service-contracts";
import type { ModelInstance } from "../api/model-instance-contracts";
import type { OverviewAttentionItem } from "../api/telemetry-contracts";
import {
  ADMIN_ME,
  FakeApiClient,
  renderApplication,
} from "../app/test-support";
import { formatSummaryCount } from "./format-summary-count";

// FX-UI-15: synthetic contract values from UI-13; no production records or payloads.
const ID = "0198f58a-2586-7e37-bbf5-000000001515";
const INSTANCE: ModelInstance = {
  id: ID,
  artifact_id: ID,
  name: "验收实例",
  internal_model_name: "fixture",
  internal_endpoint: null,
  status: "Running",
  config_version: 1,
  context_length: 4096,
  error_code: null,
  gpu_memory_utilization: 0.8,
  restart_count: 0,
  gpu_uuids: ["FX-UI-15-GPU"],
  runtime_type: "vllm",
  tensor_parallel_size: 1,
  updated_at: "2026-10-10T10:00:00Z",
};
function rowOf(element: HTMLElement | undefined) {
  if (!element) throw new Error("FX-UI-15 expected element");
  const row = element.closest("tr");
  if (!row) throw new Error("FX-UI-15 expected table row");
  return row;
}
function client() {
  const api = new FakeApiClient(ADMIN_ME);
  api.modelInstanceResults.push({ items: [INSTANCE] });
  return api;
}
function service(
  status: AiService["runtime_status"],
  index: number,
): AiService {
  return {
    id: `0198f58a-2586-7e37-bbf5-${String(index).padStart(12, "0")}`,
    name: `服务${status}`,
    description: "",
    enabled: status !== "Disabled",
    primary_instance_id: ID,
    primary_instance_status:
      status === "Degraded"
        ? "Starting"
        : status === "Unavailable"
          ? "Failed"
          : "Running",
    public_model_name: `fx-${String(index)}`,
    route_version: 1,
    runtime_status: status,
  };
}

// FX-UI-39: synthetic long names based on FX-UI-15, with unchanged contracts.
it("UI-39 preserves full overview service and instance names", async () => {
  const name = "LongOverviewName".repeat(8);
  const api = new FakeApiClient(ADMIN_ME);
  api.modelInstanceResults.push({ items: [{ ...INSTANCE, name }] });
  api.aiServiceResults.push({ items: [{ ...service("Available", 1), name }] });
  renderApplication(api, "/overview");
  for (const label of ["查看服务详情", "查看实例详情"]) {
    const link = await screen.findByRole("link", {
      name: `${name} · ${label}`,
    });
    expect(link).toHaveAccessibleName(`${name} · ${label}`);
    expect(within(link).getByText(name)).toHaveAttribute("title", name);
  }
});

describe("UI-15 overview prototype states", () => {
  it.each([
    [999999, "999,999"],
    [1000000, "1M"],
    [4120553, "4.1M"],
    [1284503112, "1.3B"],
    [999950000, "1B"],
  ] as const)("UI-15 formats summary %s as %s", (value, label) => {
    expect(formatSummaryCount(value)).toBe(label);
  });

  it("UI-15 normal: exact Requests, compact Tokens with full accessible value, active users and create entry", async () => {
    const api = client();
    const summary = await api.getUsageSummary();
    api.usageSummaryResults.push({
      ...summary,
      today: { ...summary.today, requests: 1052310, total_tokens: 1284503112 },
      today_active_users: 52,
    });
    renderApplication(api, "/overview");
    const usage = await screen.findByRole("link", {
      name: "今日用量 · 查看用量",
    });
    expect(await within(usage).findByText("1.1M")).toBeVisible();
    expect(within(usage).getByText("1.3B")).toHaveAccessibleName(
      "1,284,503,112",
    );
    expect(within(usage).getByText("1.3B")).toHaveAttribute(
      "title",
      "1,284,503,112",
    );
    expect(within(usage).getByText("52")).toBeVisible();
    expect(within(usage).getByText("活跃用户")).toBeVisible();
    expect(document.title).toBe("总览 · Holm");
    // UI-15B: the populated overview header has no action buttons.
    const header = screen
      .getByRole("heading", { name: "总览" })
      .closest("header");
    if (!header) throw new Error("UI-15B expected overview header");
    expect(within(header).queryByRole("button")).not.toBeInTheDocument();
  });

  it("UI-15 8180 state: all disabled notice, requests zero and three known zero usage values", async () => {
    const api = client();
    const overview = await api.getTelemetryOverview();
    api.overviewTelemetryResults.push({
      ...overview,
      current: {
        ...overview.current,
        services_total: 2,
        services_available: 0,
      },
    });
    api.aiServiceResults.push({
      items: [service("Disabled", 1), service("Disabled", 2)],
    });
    const summary = await api.getUsageSummary();
    api.usageSummaryResults.push({
      ...summary,
      today: { ...summary.today, requests: 0, total_tokens: null },
      today_active_users: 0,
    });
    renderApplication(api, "/overview");
    expect(await screen.findByText("当前没有可用的 AI 服务")).toBeVisible();
    const details = screen.getByRole("link", { name: "查看详情" });
    expect(details).toHaveAttribute("href", "/services");
    expect(details.parentElement).toHaveClass("pw-overview-conclusion--notice");
    expect(details.parentElement).toHaveTextContent(
      "2 个服务均已停用，调用将返回 503。启用服务后恢复。",
    );
    const usage = screen.getByRole("link", { name: "今日用量 · 查看用量" });
    await waitFor(() => {
      expect(within(usage).getAllByText("0")).toHaveLength(3);
    });
    const requests = screen.getByRole("link", { name: "请求 · 查看监控" });
    expect(within(requests).getAllByText("0")).toHaveLength(3);
    expect(
      within(requests).getByLabelText("暂无数据"),
    ).toHaveAccessibleDescription("近 1 小时无调用");
  });

  it("UI-15 lists every service in abnormal-first order and reuses window versus current metrics", async () => {
    const api = client();
    const services = [
      service("Disabled", 1),
      service("Available", 2),
      service("Degraded", 3),
      service("Unavailable", 4),
      service("Available", 5),
      service("Disabled", 6),
    ];
    api.aiServiceResults.push({ items: services });
    const telemetry = await api.getServiceTelemetry(service("Available", 2).id);
    vi.spyOn(api, "getServiceTelemetry").mockImplementation((id) =>
      Promise.resolve({
        ...telemetry,
        service_id: id,
        window_1h: {
          requests: 12,
          error_rate: 0.125,
          ttft_p95_ms: 182,
          started_at: null,
        },
        current: {
          ...telemetry.current,
          output_tps_avg: 54.2,
          inflight_last: 4,
          ttft_p95_ms: 999,
          error_rate: 0.9,
        },
      }),
    );
    const instance = await api.getInstanceTelemetry(ID);
    vi.spyOn(api, "getInstanceTelemetry").mockResolvedValue({
      ...instance,
      current: { ...instance.current, waiting: 5, running: 7 },
    });
    renderApplication(api, "/overview");
    const table = await screen.findByRole("table", { name: "AI 服务" });
    await within(table).findAllByText("54.2");
    const links = within(table).getAllByRole("link");
    expect(links.map((link) => link.getAttribute("href"))).toEqual(
      [4, 3, 2, 5, 1, 6].map(
        (i) =>
          `/services/0198f58a-2586-7e37-bbf5-${String(i).padStart(12, "0")}`,
      ),
    );
    const row = rowOf(links[2]);
    expect(row).toHaveTextContent("182 ms");
    expect(row).toHaveTextContent("12.5%");
    expect(row).toHaveTextContent("4 / 5");
    expect(row).not.toHaveTextContent("999");
    for (const link of links.slice(4)) {
      const disabled = within(rowOf(link));
      expect(disabled.getAllByText("—")).toHaveLength(4);
      expect(disabled.queryByLabelText("暂无数据")).not.toBeInTheDocument();
    }
    expect(
      await within(screen.getByRole("table", { name: "模型实例" })).findByText(
        "7",
      ),
    ).toBeVisible();
    expect((await axe.run(document.body)).violations).toEqual([]);
  });

  it.each(["service", "instance"])(
    "UI-15 %s rows support both mouse and keyboard navigation",
    async (kind) => {
      const api = client();
      const svc = service("Available", 1);
      api.aiServiceResults.push({ items: [svc] });
      renderApplication(api, "/overview");
      const link = await screen.findByRole("link", {
        name:
          kind === "service"
            ? `${svc.name} · 查看服务详情`
            : `${INSTANCE.name} · 查看实例详情`,
      });
      const target =
        kind === "service" ? `/services/${svc.id}` : `/models/instances/${ID}`;
      expect(link).toHaveAttribute("href", target);
      const user = userEvent.setup();
      // Clicking the numeric cell exercises the whole row rather than just its name.
      const cell = within(rowOf(link)).getAllByRole("cell")[4];
      if (!cell) throw new Error("FX-UI-15 expected metric cell");
      await user.click(cell);
      await waitFor(() =>
        expect(
          screen.queryByRole("heading", { name: "总览" }),
        ).not.toBeInTheDocument(),
      );
    },
  );

  it.each(["service", "instance"])(
    "UI-15 %s row link activates with Enter",
    async (kind) => {
      const api = client();
      const svc = service("Available", 1);
      api.aiServiceResults.push({ items: [svc] });
      renderApplication(api, "/overview");
      const link = await screen.findByRole("link", {
        name:
          kind === "service"
            ? `${svc.name} · 查看服务详情`
            : `${INSTANCE.name} · 查看实例详情`,
      });
      link.focus();
      await userEvent.setup().keyboard("{Enter}");
      await waitFor(() =>
        expect(
          screen.queryByRole("heading", { name: "总览" }),
        ).not.toBeInTheDocument(),
      );
    },
  );

  it("UI-15 renders all seven attention kinds, repeated objects, severity order and destinations at the bottom", async () => {
    const api = client();
    const kinds = [
      "service_unavailable",
      "instance_failed",
      "instance_unhealthy",
      "gpu_unhealthy",
      "download_failed",
      "telemetry_interrupted",
      "unknown_requests",
      "instance_failed",
    ] as const;
    const items: OverviewAttentionItem[] = kinds.map((kind, i) => ({
      kind,
      count: kind === "unknown_requests" ? 12 : 1,
      severity: [
        "download_failed",
        "telemetry_interrupted",
        "unknown_requests",
      ].includes(kind)
        ? "notice"
        : "negative",
      target_id: `target-${String(i)}`,
      target_name: `对象${String(i)}`,
      detail:
        kind === "download_failed"
          ? "Downloading"
          : kind === "service_unavailable"
            ? "Failed"
            : null,
      latest_at: "2026-10-10T10:04:00Z",
    }));
    // API owns negative-first ordering; render the complete response without slicing.
    items.sort(
      (a, b) =>
        Number(a.severity === "notice") - Number(b.severity === "notice"),
    );
    api.overviewAttentionResults.push({ items });
    renderApplication(api, "/overview");
    const section = await screen.findByRole("region", { name: "需要处理" });
    const links = await within(section).findAllByRole("link");
    expect(links).toHaveLength(8);
    expect(links.map((link) => link.getAttribute("href"))).toEqual([
      "/services/target-0",
      "/models/instances/target-1",
      "/models/instances/target-2",
      "/system?tab=gpu",
      "/models/instances/target-7",
      "/models?tab=local-models",
      "/system?tab=components",
      "/system?tab=components",
    ]);
    expect(links[1]).toHaveTextContent("运行失败");
    expect(links[2]).toHaveTextContent("运行异常");
    expect(links[5]).toHaveTextContent("下载");
    expect(links[6]).toHaveTextContent("未收到监控数据");
    expect(links[7]).toHaveTextContent("12 个调用");
    expect(section.parentElement?.lastElementChild).toBe(section);
    expect((await axe.run(document.body)).violations).toEqual([]);
  });

  it("UI-15 telemetry interruption preserves service state and distinguishes missing from zero and stopped", async () => {
    const api = client();
    const overview = await api.getTelemetryOverview();
    api.overviewTelemetryResults.push({
      ...overview,
      telemetry_status: "unavailable",
      sampled_at: "2026-10-10T10:04:00Z",
      current: {
        ...overview.current,
        services_available: 1,
        requests_running: null,
        requests_waiting: null,
        requests_1h: null,
        error_rate_1h: null,
      },
    });
    api.aiServiceResults.push({ items: [service("Available", 1)] });
    api.modelInstanceResults.splice(0, 1, {
      items: [
        INSTANCE,
        {
          ...INSTANCE,
          id: ID.replace(/515$/, "516"),
          name: "停止实例",
          status: "Stopped",
        },
      ],
    });
    renderApplication(api, "/overview");
    expect(await screen.findByText("1 个 AI 服务可用")).toBeVisible();
    const requests = screen.getByRole("link", { name: "请求 · 查看监控" });
    expect(within(requests).queryByText("0")).not.toBeInTheDocument();
    for (const mark of within(requests).getAllByLabelText("暂无数据"))
      expect(mark).toHaveAccessibleDescription(/监控数据暂不可用；最后更新/);
    const running = rowOf(
      screen.getByRole("link", { name: "验收实例 · 查看实例详情" }),
    );
    expect(
      await within(running).findByLabelText("暂无数据"),
    ).toHaveAccessibleDescription("当前请求暂不可用；暂无更新时间");
    const stopped = rowOf(
      screen.getByRole("link", { name: "停止实例 · 查看实例详情" }),
    );
    expect(within(stopped).getByText("—")).toBeVisible();
    expect(
      within(stopped).queryByLabelText("暂无数据"),
    ).not.toBeInTheDocument();
  });

  it("UI-15 loading, page failure and retry are distinct from missing metrics", async () => {
    const api = client();
    let reject!: (reason: Error) => void;
    const pending = new Promise<never>((_, r) => {
      reject = r;
    });
    vi.spyOn(api, "getTelemetryOverview").mockImplementationOnce(() => pending);
    renderApplication(api, "/overview");
    expect(await screen.findByLabelText("总览正在加载")).toHaveAttribute(
      "aria-busy",
      "true",
    );
    expect(screen.queryByText("—")).not.toBeInTheDocument();
    reject(new Error("unavailable"));
    expect(await screen.findByText("无法获取平台总览")).toBeVisible();
    await userEvent.setup().click(screen.getByRole("button", { name: "重试" }));
    expect(
      await screen.findByRole("heading", { name: "部署第一个模型" }),
    ).toBeVisible();
  });

  it("UI-15 late overview data preserves the focused help trigger", async () => {
    const api = client();
    const overview = await api.getTelemetryOverview();
    let deliver: (value: typeof overview) => void = () => {
      throw new Error("FX-UI-15 deferred query not initialized");
    };
    const response = new Promise<typeof overview>((resolve) => {
      deliver = resolve;
    });
    vi.spyOn(api, "getTelemetryOverview").mockReturnValue(response);
    renderApplication(api, "/overview");
    await screen.findByLabelText("总览正在加载");
    const help = screen.getByRole("button", { name: "帮助" });
    help.focus();
    deliver(overview);
    await screen.findByRole("heading", { name: "总览" });
    expect(help).toHaveFocus();
    await userEvent.setup().keyboard("{Enter}");
    expect(
      await screen.findByRole("dialog", { name: "帮助中心" }),
    ).toBeVisible();
  });

  it("UI-15 retries attention failure independently", async () => {
    const api = client();
    api.overviewAttentionResults.push(new Error("unavailable"), { items: [] });
    renderApplication(api, "/overview");
    expect(await screen.findByText("无法获取需要处理的事项")).toBeVisible();
    const section = screen.getByRole("region", { name: "需要处理" });
    await userEvent
      .setup()
      .click(within(section).getByRole("button", { name: "重试" }));
    expect(
      await within(section).findByText("暂无需要处理的事项"),
    ).toBeVisible();
    expect(screen.getByRole("link", { name: "请求 · 查看监控" })).toBeVisible();
  });
});
