import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import axe from "axe-core";
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type { ModelInstance } from "../api/model-instance-contracts";
import {
  ADMIN_ME,
  DEFAULT_SYSTEM_COMPONENTS,
  FakeApiClient,
  renderApplication,
  systemServerWithUnavailableGpu,
} from "../app/test-support";

// FX-UI-01: synthetic instances ordered independently of their IDs and input order.
function instance(index: number): ModelInstance {
  return {
    id: `0198f58a-2586-7e37-bbf5-${String(index).padStart(12, "0")}`,
    artifact_id: "0198f58a-2586-7e37-bbf5-000000001414",
    config_version: 1,
    context_length: 4096,
    error_code: null,
    gpu_memory_utilization: 0.8,
    restart_count: 0,
    gpu_uuids: ["FX-UI-01-GPU"],
    internal_endpoint: null,
    internal_model_name: "fx-model",
    name: `实例 ${String(index)}`,
    runtime_type: "vllm",
    status: "Running",
    tensor_parallel_size: 1,
    updated_at: `2026-10-08T09:0${String(index)}:00Z`,
  };
}

function client() {
  const api = new FakeApiClient(ADMIN_ME);
  api.modelInstanceResults.push({ items: [1, 6, 2, 4, 3, 5].map(instance) });
  return api;
}

const cards = [
  ["AI 服务 · 查看 AI 服务", "/services"],
  ["GPU · 查看 GPU", "/system?tab=gpu"],
  ["请求 · 查看监控", "/services?view=monitor"],
  ["今日用量 · 查看用量", "/usage"],
] as const;

describe("UI-15 UI-01 Overview", () => {
  it("UI-01 provides four complete keyboard links, today's usage, five recent instances and axe", async () => {
    const api = client();
    const summary = await api.getUsageSummary();
    api.usageSummaryResults.push({
      ...summary,
      today: { ...summary.today, requests: 123, total_tokens: 4567 },
    });
    renderApplication(api, "/overview");
    expect(await screen.findByText("系统运行正常")).toBeVisible();
    expect(await screen.findByText("123")).toBeVisible();
    for (const [name, target] of cards) {
      const link = screen.getByRole("link", { name });
      expect(link).toHaveAttribute("href", target);
      expect(within(link).getByRole("heading")).toBeVisible();
      expect(within(link).getAllByRole("definition").length).toBeGreaterThan(0);
    }
    expect(screen.getByText("123")).toBeVisible();
    expect(screen.getByText("4,567")).toBeVisible();
    expect(api.usageSummaryCalls.at(-1)).toBe(
      Intl.DateTimeFormat().resolvedOptions().timeZone,
    );
    const recent = screen.getByRole("region", {
      name: "模型实例 最近更新的 5 个",
    });
    expect(
      within(recent)
        .getAllByRole("link", { name: /查看实例详情/ })
        .map((link) => link.getAttribute("aria-label")),
    ).toEqual([6, 5, 4, 3, 2].map((i) => `实例 ${String(i)} · 查看实例详情`));
    expect(within(recent).getAllByText("1 张")).toHaveLength(5);
    expect(within(recent).getAllByText("vLLM")).toHaveLength(5);
    expect((await axe.run(document.body)).violations).toEqual([]);
    const link = screen.getByRole("link", { name: cards[2][0] });
    link.focus();
    await userEvent.setup().keyboard("{Enter}");
    expect(await screen.findByText(/监控视图：按 Error Rate/)).toBeVisible();
  });

  it("UI-01 distinguishes zero from missing with associated reasons and last update outside the grid", async () => {
    const api = client();
    const overview = await api.getTelemetryOverview();
    api.overviewTelemetryResults.push({
      ...overview,
      sampled_at: "2026-10-08T09:10:00Z",
      current: {
        ...overview.current,
        requests_1h: 0,
        error_rate_1h: null,
        window_started_at: "2026-10-08T09:05:00Z",
      },
    });
    renderApplication(api, "/overview");
    const link = await screen.findByRole("link", { name: cards[2][0] });
    expect(within(link).getAllByText("0")).toHaveLength(3);
    const mark = within(link).getByLabelText("暂无数据");
    expect(mark).toHaveTextContent("—");
    const reason = document.getElementById(
      mark.getAttribute("aria-describedby") ?? "",
    );
    expect(reason).toHaveTextContent("近 1 小时无调用");
    expect(reason).toHaveClass("pw-visually-hidden");
    expect(mark.closest(".pw-overview-missing")).toHaveAttribute(
      "title",
      "近 1 小时无调用",
    );
    expect(link.querySelector(".pw-overview-missing .pwi")).not.toBeNull();
    expect(within(link).queryByText(/^自 .* 起$/)).not.toBeInTheDocument();
  });

  it("UI-01 keeps other cards usable when usage is denied", async () => {
    const api = client();
    api.usageSummaryResults.push(new Error("forbidden"));
    renderApplication(api, "/overview");
    const usage = await screen.findByRole("link", { name: cards[3][0] });
    expect(await within(usage).findAllByText(/用量暂不可用/)).toHaveLength(3);
    for (const mark of within(usage).getAllByLabelText("暂无数据"))
      expect(mark).toHaveAccessibleDescription(/用量暂不可用/);
    expect(within(usage).queryByText("0")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: cards[0][0] })).toHaveAttribute(
      "href",
      "/services",
    );
  });

  it("UI-01 picks the first negative issue over notice and preserves sidebar details", async () => {
    const api = client();
    const components = structuredClone(DEFAULT_SYSTEM_COMPONENTS);
    for (const component of components.components) {
      if (component.id === "Control Plane") component.status = "degraded";
      if (component.id === "Gateway" || component.id === "Database")
        component.status = "unavailable";
    }
    api.systemComponentsResults.push(components);
    renderApplication(api, "/overview");
    const details = await screen.findByRole("link", { name: "查看详情" });
    const banner = details.parentElement;
    expect(banner).toHaveClass("pw-overview-conclusion--negative");
    expect(banner).toHaveTextContent("推理网关不可用");
    expect(banner).not.toHaveTextContent("控制平面降级");
    expect(
      screen.getByRole("link", { name: /平台状态：部分异常/ }),
    ).toHaveAccessibleName(/控制平面降级.*推理网关不可用/);
  });

  it("UI-01 describes the impact of unavailable GPU discovery without changing sidebar copy", async () => {
    const api = client();
    api.systemServerResults.push(systemServerWithUnavailableGpu());
    renderApplication(api, "/overview");
    const details = await screen.findByRole("link", { name: "查看详情" });
    expect(details.parentElement).toHaveTextContent(
      "GPU 信息不可用：GPU 检测权限不足。设备状态无法确认，部署前请查看系统详情。",
    );
    expect(
      screen.getByRole("link", { name: /平台状态：部分异常/ }),
    ).toHaveAccessibleName(
      "平台状态：部分异常。GPU 信息不可用：GPU 检测权限不足",
    );
  });

  it("UI-01 TC-UI-01 keeps four/two/one-column breakpoints and accessible narrow content", async () => {
    const css = readFileSync(
      resolve(dirname(fileURLToPath(import.meta.url)), "../styles/app.css"),
      "utf8",
    );
    expect(css).toMatch(
      /\.pw-overview-grid\s*\{[^}]*repeat\(4, minmax\(0, 1fr\)\)/,
    );
    for (const width of [1279, 1023]) {
      const block = css.slice(
        css.indexOf(`@media (max-width: ${String(width)}px)`),
      );
      expect(block).toMatch(
        /\.pw-overview-grid\s*\{[^}]*repeat\(2, minmax\(0, 1fr\)\)/,
      );
    }
    const mobile = css.slice(css.indexOf("@media (max-width: 767px)"));
    expect(mobile).toMatch(
      /\.pw-overview-grid,[^{]+\{\s*grid-template-columns: 1fr/,
    );
    vi.spyOn(window, "matchMedia").mockImplementation((query) => ({
      media: query,
      matches: true,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    }));
    renderApplication(client(), "/overview");
    expect(await screen.findByText("系统运行正常")).toBeVisible();
    expect(await screen.findByText(/近 1 小时无调用/)).toHaveClass(
      "pw-visually-hidden",
    );
    expect((await axe.run(document.body)).violations).toEqual([]);
  });

  it("UI-01 telemetry failure leaves available service counts unchanged", async () => {
    const api = client();
    const components = structuredClone(DEFAULT_SYSTEM_COMPONENTS);
    for (const component of components.components) {
      if (component.id === "Telemetry Collector")
        component.status = "unavailable";
    }
    api.systemComponentsResults.push(components);
    const overview = await api.getTelemetryOverview();
    api.overviewTelemetryResults.push({
      ...overview,
      current: { ...overview.current, services_available: 2 },
    });
    renderApplication(api, "/overview");
    expect(await screen.findByText("2 个 AI 服务可用")).toBeVisible();
    expect(
      (await screen.findByRole("link", { name: "查看详情" })).parentElement,
    ).toHaveTextContent("AI 服务可用性判断不受影响");
  });
});
