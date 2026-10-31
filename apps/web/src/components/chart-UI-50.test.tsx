import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ServiceMonitor } from "../ai-services/service-monitor";
import { InstanceMonitor } from "../telemetry/instance-monitor";
import { ADMIN_ME, FakeApiClient } from "../app/test-support";
import axe from "axe-core";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { Chart } from "./chart";

// FX-UI-50: deterministic jitter, missing sample and collector outage.
const times = [0, 4500, 10000, 15000, 40000];
function draw(values: readonly (number | null)[], integer = false) {
  return (
    <Chart
      ariaLabel="UI-50"
      kind="ordinal"
      categories={times.map(String)}
      timeAxis={{ times, gapMs: 15000, formatTick: String }}
      valueFormat={{ unit: integer ? "个" : "ms", integer }}
      series={[{ id: "a", label: "A", values }]}
    />
  );
}

describe("UI-50 TC-UI-18 time positioning and ticks", () => {
  it("positions jittered samples by time and breaks at null and long gaps", () => {
    const { container } = render(draw([10, 20, null, 30, 40]));
    const segments = [...container.querySelectorAll("polyline")].map((line) =>
      line
        .getAttribute("points")
        ?.split(" ")
        .map((p) => Number(p.split(",")[0])),
    );
    expect(segments).toEqual([[0, 11.25], [37.5], [100]]);
    const ticks = [
      ...container.querySelectorAll<HTMLElement>(".pw-chart__x-ticks span"),
    ];
    expect(ticks.map((tick) => tick.textContent)).toEqual([
      "0",
      "10000",
      "20000",
      "30000",
      "40000",
    ]);
    expect(ticks.map((tick) => tick.style.left)).toEqual([
      "0%",
      "25%",
      "50%",
      "75%",
      "100%",
    ]);
    fireEvent.click(screen.getByRole("button", { name: "查看数据表" }));
    expect(screen.getAllByRole("columnheader")).toHaveLength(6);
  });

  it("snaps pointer and keyboard markers to samples and exposes missing values", async () => {
    const { container } = render(draw([10, 20, null, 30, 40]));
    const hit = screen.getByRole("button", { name: /使用左右方向键/ });
    vi.spyOn(hit, "getBoundingClientRect").mockReturnValue(
      new DOMRect(0, 0, 100, 100),
    );
    fireEvent.mouseMove(hit, { clientX: 12 });
    expect(
      screen.getByRole("tooltip").querySelector("strong"),
    ).toHaveTextContent("4500");
    expect(container.querySelector(".pw-chart__crosshair")).toHaveStyle({
      left: "11.25%",
    });
    fireEvent.keyDown(hit, { key: "ArrowRight" });
    expect(screen.getByRole("tooltip")).toHaveTextContent("—");
    expect(container.querySelector(".pw-chart__dot")).toBeNull();
    expect(container.querySelector(".pw-chart__crosshair")).toHaveStyle({
      left: "25%",
    });
    fireEvent.keyDown(hit, { key: "ArrowRight" });
    expect(container.querySelector(".pw-chart__dot")).toHaveStyle({
      left: "37.5%",
    });
    expect(
      (
        await axe.run(container, {
          rules: { "color-contrast": { enabled: false } },
        })
      ).violations,
    ).toEqual([]);
  });

  it("rounds 59700 to 60000 and keeps count tick values integral", () => {
    const { container, rerender } = render(draw([59700]));
    expect(container.querySelector(".pw-chart__ticks")).toHaveTextContent(
      "60,000 ms30,000 ms0 ms",
    );
    for (const value of [0, 1, 3, 5, 25]) {
      rerender(draw([value], true));
      const ticks = [
        ...container.querySelectorAll<HTMLElement>(".pw-chart__ticks span"),
      ];
      const numbers = ticks.map((tick) =>
        Number(tick.textContent.replace(" 个", "")),
      );
      expect(numbers.every(Number.isInteger)).toBe(true);
      expect(new Set(numbers).size).toBe(numbers.length);
      if (value <= 1) expect(numbers).toEqual([1, 0]);
      for (const [index, tick] of ticks.entries()) {
        expect(parseFloat(tick.style.top)).toBeCloseTo(
          94 - ((numbers[index] ?? NaN) / (numbers[0] ?? NaN)) * 88,
        );
      }
    }
  });

  it("handles a single timestamp without invalid coordinates", () => {
    const { container } = render(
      <Chart
        ariaLabel="UI-50 single"
        kind="ordinal"
        categories={["now"]}
        timeAxis={{ times: [1000], gapMs: 15000, formatTick: String }}
        series={[{ id: "a", label: "A", values: [1] }]}
      />,
    );
    expect(container.querySelector("polyline")).toHaveAttribute(
      "points",
      "50,6",
    );
    expect(
      container.querySelector(".pw-chart__x-ticks span"),
    ).toHaveTextContent("1000");
  });
});

// FX-UI-50 monitor fixtures use the existing API defaults with partial metric coverage.
describe("UI-50 monitor integration", () => {
  it("keeps service chart domains aligned and applies the range-specific gap threshold", async () => {
    const api = new FakeApiClient(ADMIN_ME);
    const base = await api.getServiceTelemetry("ui50");
    const sampled = [0, 5000, 65000].map((offset) =>
      new Date(Date.UTC(2026, 9, 28) + offset).toISOString(),
    );
    vi.spyOn(api, "getServiceTelemetry").mockResolvedValue({
      ...base,
      telemetry_status: "available",
      series: sampled.map((sampled_at, index) => ({
        ...base.current,
        sampled_at,
        quality: "full",
        telemetry_status: "available",
        inflight_last: index === 1 ? null : 1,
        ttft_p50_ms: index === 0 ? null : 59700,
      })),
    });
    const { container } = render(
      <QueryClientProvider
        client={
          new QueryClient({ defaultOptions: { queries: { retry: false } } })
        }
      >
        <ServiceMonitor
          apiClient={api}
          instanceName="UI50"
          service={{
            id: "ui50",
            primary_instance_id: "instance",
            primary_instance_status: "Running",
            name: "UI50",
            description: "",
            public_model_name: "ui50",
            enabled: true,
            route_version: 1,
            runtime_status: "Available",
          }}
        />
      </QueryClientProvider>,
    );
    await screen.findByRole("img", { name: "用户侧 TTFT，最近 1h" });
    const axes = [...container.querySelectorAll(".pw-chart__x-ticks")];
    expect(axes.length).toBeGreaterThanOrEqual(2);
    expect(new Set(axes.map((axis) => axis.textContent)).size).toBe(1);
    expect(
      [...container.querySelectorAll(".pw-chart__ticks")].some(
        (axis) => axis.textContent === "1 个0 个",
      ),
    ).toBe(true);
    const ttft = () => screen.getByRole("img", { name: /用户侧 TTFT/ });
    expect(ttft().querySelectorAll("polyline")).toHaveLength(2);
    fireEvent.change(screen.getByRole("combobox", { name: "时间范围" }), {
      target: { value: "6h" },
    });
    await screen.findByRole("img", { name: "用户侧 TTFT，最近 6h" });
    expect(ttft().querySelectorAll("polyline")).toHaveLength(1);
  });

  it("retains partial instance quantiles and shows all-null metrics as missing", async () => {
    const api = new FakeApiClient(ADMIN_ME);
    const base = await api.getInstanceTelemetry("ui50");
    vi.spyOn(api, "getInstanceTelemetry").mockResolvedValue({
      ...base,
      telemetry_status: "available",
      series: [0, 5000, 10000].map((offset, index) => ({
        ...base.current,
        sampled_at: new Date(Date.UTC(2026, 9, 28) + offset).toISOString(),
        quality: "full",
        telemetry_status: "available",
        ttft_p50_seconds: index === 1 ? null : 0.1,
        running: index === 0 ? null : 1,
      })),
    });
    const { container } = render(
      <QueryClientProvider
        client={
          new QueryClient({ defaultOptions: { queries: { retry: false } } })
        }
      >
        <InstanceMonitor
          apiClient={api}
          instanceId="ui50"
          gpuUuids={[]}
          gpuByUuid={new Map()}
        />
      </QueryClientProvider>,
    );
    await screen.findByRole("img", { name: "P50、P95 与 P99，最近 1 小时" });
    expect(container.querySelectorAll(".pw-chart")).toHaveLength(2);
    const axes = [...container.querySelectorAll(".pw-chart__x-ticks")];
    expect(axes[0]?.textContent).toBe(axes[1]?.textContent);
    expect(screen.getAllByLabelText("暂无数据").length).toBeGreaterThan(0);
    expect(
      container.querySelectorAll(".pw-chart__series--ordinal-1"),
    ).toHaveLength(3);
  });
});
