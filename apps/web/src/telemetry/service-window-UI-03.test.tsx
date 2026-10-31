import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import axe from "axe-core";
import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import {
  parseServiceTelemetry,
  type ServiceTelemetry,
} from "../api/telemetry-contracts";
import {
  ADMIN_ME,
  FakeApiClient,
  renderApplication,
  systemServerWithUnavailableGpu,
} from "../app/test-support";
import { MissingValue } from "../components/data-table";

// FX-UI-03: synthetic service; instantaneous values deliberately differ from the hour.
const serviceId = "00000000-0000-7000-8000-000000000303";
async function client(window: ServiceTelemetry["window_1h"]) {
  const api = new FakeApiClient(ADMIN_ME);
  api.aiServiceResults.push({
    items: [
      {
        id: serviceId,
        name: "窗口测试服务",
        public_model_name: "window-model",
        description: "用于对比采样窗口",
        enabled: true,
        primary_instance_id: "00000000-0000-7000-8000-000000000304",
        primary_instance_status: "Running",
        route_version: 1,
        runtime_status: "Available",
      },
    ],
  });
  const empty = await api.getServiceTelemetry(serviceId);
  const telemetry = {
    ...empty,
    sampled_at: "2026-10-08T10:00:00Z",
    telemetry_status: "available" as const,
    window_1h: window,
    current: {
      ...empty.current,
      output_tps_avg: 0,
      ttft_p95_ms: 999,
      error_rate: 0.9,
    },
  };
  api.serviceTelemetryResults.push(parseServiceTelemetry(telemetry));
  return api;
}

async function row() {
  return (await screen.findByText("window-model")).closest("tr") as HTMLElement;
}
function cell(element: HTMLElement, column: string) {
  return element.querySelector(`[data-column="${column}"]`) as HTMLElement;
}

describe("UI_03 TC-TM-05 TC-UI-08 service list window", () => {
  it("uses hour metrics and current TPS, labels both desktop and expanded metrics, and passes axe", async () => {
    // UI-05: exercise the same metrics after switching to full-row disclosure.
    let width = 1600;
    const listeners: Array<() => void> = [];
    const originalMatchMedia = Object.getOwnPropertyDescriptor(
      window,
      "matchMedia",
    );
    if (!originalMatchMedia) throw new Error("FX-UI-05 matchMedia is missing");
    const matchMedia: typeof window.matchMedia = (query) => ({
      get matches() {
        return width <= 1023;
      },
      media: query,
      onchange: null,
      addEventListener: (_event: string, listener: unknown) => {
        listeners.push(listener as () => void);
      },
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn(),
    });
    Object.defineProperty(window, "matchMedia", {
      configurable: true,
      value: matchMedia,
    });
    try {
      renderApplication(
        await client({
          requests: 100,
          error_rate: 0.02,
          ttft_p95_ms: 95,
          started_at: "2026-10-08T09:30:00Z",
        }),
        "/services",
      );
      const serviceRow = await row();
      expect(
        screen.getByRole("columnheader", { name: "TTFT P95（近 1 小时）" }),
      ).toBeVisible();
      expect(
        screen.getByRole("columnheader", { name: "错误率（近 1 小时）" }),
      ).toBeVisible();
      expect(
        screen.getByRole("columnheader", { name: "输出 TPS（当前）" }),
      ).toBeVisible();
      expect(cell(serviceRow, "ttft")).toHaveTextContent("95 ms");
      expect(cell(serviceRow, "error-rate")).toHaveTextContent("2.0%");
      expect(cell(serviceRow, "service-tps")).toHaveTextContent("0.0");
      expect(
        serviceRow.querySelector('time[datetime="2026-10-08T09:30:00Z"]'),
      ).toBeVisible();
      act(() => {
        width = 600;
        listeners.forEach((listener) => {
          listener();
        });
      });
      expect(
        screen.getByRole("columnheader", { name: "TTFT P95（近 1 小时）" }),
      ).toBeVisible();
      expect(cell(serviceRow, "ttft")).toHaveTextContent("95 ms");
      expect(document.querySelector(".pw-table__expanded-row")).toBeNull();
      expect(
        (await axe.run(document.body)).violations.filter(
          ({ impact }) => impact === "critical" || impact === "serious",
        ),
      ).toEqual([]);
    } finally {
      Object.defineProperty(window, "matchMedia", originalMatchMedia);
    }
  });

  it("describes no calls without replacing a measured zero TPS, and omits start notes for a full hour", async () => {
    renderApplication(
      await client({
        requests: 0,
        error_rate: null,
        ttft_p95_ms: null,
        started_at: "2026-10-08T09:00:05Z",
      }),
      "/services",
    );
    const serviceRow = await row();
    for (const column of ["ttft", "error-rate"]) {
      const mark = within(cell(serviceRow, column)).getByLabelText("暂无数据");
      expect(mark).toHaveTextContent("—");
      expect(mark).toHaveAccessibleDescription("近 1 小时无调用");
      expect(mark.parentElement).toHaveAttribute("title", "近 1 小时无调用");
    }
    expect(cell(serviceRow, "service-tps")).toHaveTextContent("0.0");
    expect(serviceRow.querySelector("time")).toBeNull();
    expect(within(serviceRow).getByRole("img", { name: "可用" })).toBeVisible();
  });

  it("keeps unknown counters distinct from no calls", async () => {
    renderApplication(
      await client({
        requests: null,
        error_rate: null,
        ttft_p95_ms: null,
        started_at: "2026-10-08T10:00:00Z",
      }),
      "/services",
    );
    const serviceRow = await row();
    expect(
      within(cell(serviceRow, "ttft")).getByLabelText("暂无数据"),
    ).toHaveAccessibleDescription("服务遥测未采集或窗口内无请求");
  });

  it("renders only mark, notice and optional time while preserving accessible and hover reasons", async () => {
    render(
      <main>
        <MissingValue
          kind="unavailable"
          lastUpdated="10:30"
          reason="监控数据暂不可用"
        />
        <MissingValue kind="not-applicable" reason="不再适用" />
      </main>,
    );
    const marks = screen.getAllByLabelText("暂无数据");
    expect(marks[0]).toHaveAccessibleDescription(
      "监控数据暂不可用，最后更新：10:30",
    );
    expect(marks[0]?.parentElement).toHaveAttribute(
      "title",
      "监控数据暂不可用",
    );
    expect(screen.getByText("监控数据暂不可用，最后更新：10:30")).toHaveClass(
      "pw-visually-hidden",
    );
    expect(screen.getByText("最后更新：10:30")).toBeVisible();
    expect(marks[1]).toHaveAccessibleDescription("不再适用");
    expect(marks[1]?.parentElement?.querySelector(".i-clock")).toBeNull();
    expect(
      (await axe.run(document.body)).violations.filter(
        ({ impact }) => impact === "critical" || impact === "serious",
      ),
    ).toEqual([]);
  });

  it("SYS_05 TC_HEALTH_25 retains system-page missing-value descriptions", async () => {
    const api = new FakeApiClient(ADMIN_ME);
    api.systemServerResults.push(systemServerWithUnavailableGpu());
    renderApplication(api, "/system");
    await userEvent
      .setup()
      .click(await screen.findByRole("tab", { name: "平台组件" }));
    await userEvent
      .setup()
      .click(screen.getByRole("button", { name: "展开子模块（10）" }));
    const marks = await screen.findAllByLabelText("暂无数据");
    for (const mark of marks) {
      const reason = document.getElementById(
        mark.getAttribute("aria-describedby") ?? "",
      );
      expect(reason?.textContent.length).toBeGreaterThan(0);
      expect(reason).toHaveClass("pw-visually-hidden");
      expect(mark).toHaveAccessibleDescription();
    }
  });

  it("UI_08 keeps desktop widths bounded with grouped headers and nine width tokens", async () => {
    const css = readFileSync(
      resolve(dirname(fileURLToPath(import.meta.url)), "../styles/app.css"),
      "utf8",
    );
    expect(css).toMatch(
      /\.pw-services-page \.pw-table\s*\{[^}]*table-layout: fixed;/,
    );
    expect(css).not.toMatch(
      /\.pw-services-page \.pw-table[^}]*min-width: var\(--pw-content-max\)/,
    );
    const tokens = readFileSync(
      resolve(dirname(fileURLToPath(import.meta.url)), "../styles/tokens.css"),
      "utf8",
    );
    expect(tokens.match(/--pw-svc-col-[\w-]+: [^;]+;/g)).toEqual([
      "--pw-svc-col-name: 200px;",
      "--pw-svc-col-instance: 220px;",
      "--pw-svc-col-authorized-users: 76px;",
      "--pw-svc-col-today-requests: 88px;",
      "--pw-svc-col-ttft: 100px;",
      "--pw-svc-col-error-rate: 84px;",
      "--pw-svc-col-service-tps: 84px;",
      "--pw-svc-col-concurrency: 104px;",
      "--pw-svc-col-actions: 150px;",
    ]);
    renderApplication(
      await client({
        requests: 100,
        error_rate: 0.02,
        ttft_p95_ms: 95,
        started_at: "2026-10-08T09:30:00Z",
      }),
      "/services",
    );
    const table = (await row()).closest("table") as HTMLElement;
    expect(
      [...table.querySelectorAll('th[scope="colgroup"]')].map((header) => [
        header.textContent,
        header.getAttribute("colspan"),
      ]),
    ).toEqual([
      ["今日", "1"],
      ["近 1 小时", "2"],
      ["当前", "2"],
    ]);
  });
});
