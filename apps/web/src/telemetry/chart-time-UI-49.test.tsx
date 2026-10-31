import axe from "axe-core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ServiceMonitor } from "../ai-services/service-monitor";
import { ADMIN_ME, FakeApiClient } from "../app/test-support";
import { Chart } from "../components/chart";
import { formatChartTime } from "./chart-time";

// FX-UI-49: synthetic timestamps, including the UI-49 microsecond shape.
const sample = "2026-10-28T07:54:16.985742Z";

describe("UI-49 TC-UI-18 local chart time", () => {
  it.each([
    ["1h", "15:54", "15:54:16"],
    ["6h", "15:54", "15:54:16"],
    ["24h", "15:54", "15:54"],
    ["7d", "10-28", "10-28 15:54"],
  ] as const)(
    "formats %s axis and detail in the selected local zone",
    (range, axis, detail) => {
      expect(formatChartTime(sample, range, "axis", "Asia/Shanghai")).toBe(
        axis,
      );
      expect(formatChartTime(sample, range, "detail", "Asia/Shanghai")).toBe(
        detail,
      );
    },
  );

  it("pads midnight and includes the date across days", () => {
    const before = "2026-10-28T15:59:59Z";
    const after = "2026-10-28T16:00:00Z";
    expect(formatChartTime(before, "1h", "detail", "Asia/Shanghai")).toBe(
      "23:59:59",
    );
    expect(formatChartTime(after, "6h", "detail", "Asia/Shanghai")).toBe(
      "00:00:00",
    );
    expect(formatChartTime(before, "7d", "detail", "Asia/Shanghai")).toBe(
      "10-28 23:59",
    );
    expect(formatChartTime(after, "7d", "detail", "Asia/Shanghai")).toBe(
      "10-29 00:00",
    );
    expect(formatChartTime(after, "7d", "detail", "UTC")).toBe("10-28 16:00");
    expect(formatChartTime(sample, "1h", "detail")).toBe(
      new Date(sample).toLocaleTimeString("zh-CN", {
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
        hourCycle: "h23",
      }),
    );
  });

  it.each(["line", "bar"] as const)(
    "renders evenly sampled %s ticks including both ends and all short categories",
    async (mark) => {
      const categories = Array.from(
        { length: 13 },
        (_, i) => `10:${String(i).padStart(2, "0")}`,
      );
      const draw = (labels: string[]) => (
        <Chart
          ariaLabel="FX-UI-49"
          categories={labels}
          tickLabel={(_, label) => label}
          kind="ordinal"
          mark={mark}
          series={[{ id: "a", label: "A", values: labels.map(() => 1) }]}
        />
      );
      const { container, rerender } = render(draw(categories));
      const ticks = () =>
        [...container.querySelectorAll(".pw-chart__x-ticks span")].map(
          (tick) => tick.textContent,
        );
      expect(ticks()).toEqual(["10:00", "10:03", "10:06", "10:09", "10:12"]);
      for (const count of [4, 3, 2, 1, 0]) {
        rerender(draw(categories.slice(0, count)));
        expect(ticks()).toEqual(categories.slice(0, count));
      }
      rerender(draw(categories));
      fireEvent.focus(screen.getByRole("button", { name: /使用左右方向键/ }));
      expect(
        (
          await axe.run(container, {
            rules: { "color-contrast": { enabled: false } },
          })
        ).violations,
      ).toEqual([]);
    },
  );

  it("formats service and backend tooltip/table times without losing any series", async () => {
    const api = new FakeApiClient(ADMIN_ME);
    const empty = await api.getServiceTelemetry("FX-UI-49-service");
    const backend = await api.getInstanceTelemetry("FX-UI-49-instance");
    vi.spyOn(api, "getServiceTelemetry").mockResolvedValue({
      ...empty,
      telemetry_status: "available",
      series: [
        {
          ...empty.current,
          sampled_at: sample,
          telemetry_status: "available",
          quality: "full",
          ttft_p50_ms: 10,
          ttft_p95_ms: 20,
          ttft_p99_ms: 30,
        },
      ],
    });
    vi.spyOn(api, "getInstanceTelemetry").mockResolvedValue({
      ...backend,
      telemetry_status: "available",
      series: [
        {
          ...backend.current,
          sampled_at: sample,
          telemetry_status: "available",
          quality: "full",
          ttft_p50_seconds: 0.01,
          ttft_p95_seconds: 0.02,
          ttft_p99_seconds: 0.03,
        },
      ],
    });
    render(
      <QueryClientProvider
        client={
          new QueryClient({ defaultOptions: { queries: { retry: false } } })
        }
      >
        <ServiceMonitor
          apiClient={api}
          instanceName="测试实例"
          service={{
            id: "FX-UI-49-service",
            primary_instance_id: "FX-UI-49-instance",
            primary_instance_status: "Running",
            name: "测试服务",
            description: "",
            public_model_name: "fx-ui-49",
            enabled: true,
            route_version: 1,
            runtime_status: "Available",
          }}
        />
      </QueryClientProvider>,
    );
    await screen.findByRole("img", { name: "用户侧 TTFT，最近 1h" });
    for (const range of ["1h", "6h", "24h", "7d"] as const) {
      fireEvent.change(screen.getByRole("combobox", { name: "时间范围" }), {
        target: { value: range },
      });
      const plot = (
        await screen.findByRole("img", { name: `用户侧 TTFT，最近 ${range}` })
      ).closest(".pw-chart") as HTMLElement;
      fireEvent.focus(
        within(plot).getByRole("button", { name: /使用左右方向键/ }),
      );
      const tip = within(plot).getByRole("tooltip");
      const detail = formatChartTime(sample, range, "detail");
      expect(tip.querySelector("strong")).toHaveTextContent(detail);
      expect(tip.textContent).not.toMatch(/T\d{2}:|\.985742|Z/);
      for (const name of ["P50", "P95", "P99"])
        expect(within(tip).getByText(name)).toBeVisible();
      expect(plot.querySelector(".pw-chart__x-ticks")).toHaveTextContent(
        formatChartTime(sample, range, "axis"),
      );
      const button = within(plot).getByRole("button", { name: /数据表/ });
      if (button.getAttribute("aria-expanded") === "false")
        fireEvent.click(button);
      expect(
        within(plot).getByRole("columnheader", { name: detail }),
      ).toBeVisible();
    }
    const backendPlot = screen
      .getByRole("img", { name: "后端 TTFT，最近 1 小时" })
      .closest(".pw-chart") as HTMLElement;
    fireEvent.focus(
      within(backendPlot).getByRole("button", { name: /使用左右方向键/ }),
    );
    expect(
      within(backendPlot).getByRole("tooltip").querySelector("strong"),
    ).toHaveTextContent(formatChartTime(sample, "1h", "detail"));
  });
});
