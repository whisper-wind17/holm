import axe from "axe-core";
import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import {
  CATEGORY_PALETTE,
  Chart,
  ORDINAL_PALETTE,
  prepareCategoricalSeries,
  type ChartSeries,
} from "./chart";

const SERIES: readonly ChartSeries[] = [
  { id: "a", label: "服务 A", values: [1, 2] },
  { id: "b", label: "服务 B", values: [2, 3] },
  { id: "c", label: "服务 C", values: [3, 4] },
  { id: "d", label: "服务 D", values: [4, 5] },
];

describe("TC-UI-18 chart palette and shape contracts", () => {
  it("uses exactly three fixed categorical and ordinal token slots", () => {
    expect(CATEGORY_PALETTE).toEqual([
      "var(--pw-chart-cat-1)",
      "var(--pw-chart-cat-2)",
      "var(--pw-chart-cat-3)",
    ]);
    expect(ORDINAL_PALETTE).toEqual([
      "var(--pw-chart-ord-1)",
      "var(--pw-chart-ord-2)",
      "var(--pw-chart-ord-3)",
    ]);
    expect(CATEGORY_PALETTE.join(" ")).not.toMatch(/positive|notice|negative/);
  });

  it("groups overflow into Other without cycling a fourth hue", () => {
    const prepared = prepareCategoricalSeries(SERIES, {
      combine: (overflow) => ({
        id: "other",
        label: `其他 ${String(overflow.length)} 项`,
        values: overflow[0]?.values ?? [],
      }),
      kind: "other",
    });
    expect(prepared).toHaveLength(3);
    expect(prepared[2]?.label).toBe("其他 2 项");
  });

  it("renders an accessible plot and a data table entry", async () => {
    render(
      <Chart
        ariaLabel="每分钟请求数"
        categories={["10:00", "10:01"]}
        kind="categorical"
        overflowStrategy={{
          combine: (overflow) => ({
            id: "other",
            label: `其他 ${String(overflow.length)} 项`,
            values: overflow[0]?.values ?? [],
          }),
          kind: "other",
        }}
        series={SERIES}
      />,
    );
    expect(
      screen.getByRole("img", { name: "每分钟请求数" }),
    ).toBeInTheDocument();
    expect(document.querySelectorAll(".pw-chart__series")).toHaveLength(3);
    const tableButton = screen.getByRole("button", { name: "查看数据表" });
    await userEvent.click(tableButton);
    expect(screen.getByRole("table", { name: "每分钟请求数" })).toBeVisible();
    expect(tableButton).toHaveAttribute("aria-expanded", "true");
  });

  it("uses small multiples instead of assigning a fourth color", () => {
    render(
      <Chart
        ariaLabel="各服务延迟"
        categories={["10:00", "10:01"]}
        kind="categorical"
        overflowStrategy={{ kind: "small-multiples" }}
        series={SERIES}
      />,
    );
    expect(screen.getAllByRole("img")).toHaveLength(4);
    expect(
      document.querySelectorAll(".pw-chart__series--categorical-1"),
    ).toHaveLength(4);
    expect(
      document.querySelector(".pw-chart__series--categorical-4"),
    ).toBeNull();
  });
});

// FX-UI-20: deterministic chart samples for ticks, missing data and count formatting.
const UI20_SERIES = [
  { id: "p50", label: "P50", values: [110, 215, null] },
  { id: "p95", label: "P95", values: [null, 200, null] },
] as const;

function renderUI20() {
  return render(
    <Chart
      ariaLabel="UI-20 延迟"
      categories={["10:00", "10:01", "10:02"]}
      kind="ordinal"
      series={UI20_SERIES}
      valueFormat={{ unit: "ms" }}
    />,
  );
}

describe("UI-20 TC-UI-18 chart axes and interaction", () => {
  it("rounds the axis maximum and keeps missing samples disconnected", () => {
    const { container } = renderUI20();
    const ticks = container.querySelector(".pw-chart__ticks");
    expect(ticks?.textContent).toBe("250 ms125 ms0 ms");
    expect(container.querySelectorAll(".pw-chart__grid")).toHaveLength(4);
    expect(container.querySelectorAll(".pw-chart__series")).toHaveLength(2);
    expect(
      container.querySelector(".pw-chart__series")?.getAttribute("points"),
    ).toBe("0,55.28 50,18.320000000000007");
  });

  it("shows every series for hover and click, hides on leave, and exposes keyboard navigation", async () => {
    const { container } = renderUI20();
    const first = screen.getByRole("button", { name: /使用左右方向键/ });
    vi.spyOn(first, "getBoundingClientRect").mockReturnValue(
      new DOMRect(0, 0, 100, 100),
    );
    const canvas = container.querySelector(".pw-chart__canvas");
    if (!canvas) throw new Error("FX-UI-20 requires a canvas");
    fireEvent.mouseEnter(first, { clientX: 0 });
    let tip = screen.getByRole("tooltip");
    expect(within(tip).getByText("10:00")).toBeVisible();
    expect(within(tip).getByText("110 ms")).toBeVisible();
    expect(within(tip).getByText("暂无数据")).toBeVisible();
    expect(container.querySelectorAll(".pw-chart__dot")).toHaveLength(1);
    expect(tip.closest('[role="img"], [aria-hidden="true"]')).toBeNull();
    expect(
      container
        .querySelector(".pw-chart__crosshair")
        ?.closest('[role="img"], [aria-hidden="true"]'),
    ).toBeNull();
    fireEvent.mouseLeave(canvas);
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
    fireEvent.click(first, { clientX: 50, detail: 1 });
    tip = screen.getByRole("tooltip");
    expect(within(tip).getByText("10:01")).toBeVisible();
    expect(within(tip).getByText("215 ms")).toBeVisible();
    expect(within(tip).getByText("200 ms")).toBeVisible();
    expect(container.querySelectorAll(".pw-chart__dot")).toHaveLength(2);
    fireEvent.mouseLeave(canvas);
    await userEvent.tab();
    expect(first).toHaveFocus();
    await userEvent.keyboard("{ArrowRight}");
    expect(first).toHaveFocus();
    expect(screen.getByRole("tooltip")).toHaveTextContent("10:01");
    await userEvent.keyboard("{ArrowLeft}{Escape}");
    expect(first).toHaveFocus();
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
    await userEvent.tab();
    expect(screen.getByRole("button", { name: "查看数据表" })).toHaveFocus();
  });

  it("handles all-null and empty series without inventing zero samples", () => {
    const { container } = render(
      <Chart
        ariaLabel="UI-20 缺失"
        categories={["现在"]}
        kind="ordinal"
        series={[
          { id: "none", label: "延迟", values: [null] },
          { id: "empty", label: "空序列", values: [] },
        ]}
        valueFormat={{ unit: "ms" }}
      />,
    );
    expect(container.querySelector(".pw-chart__ticks")).toHaveTextContent(
      "1 ms0.5 ms0 ms",
    );
    fireEvent.click(screen.getByRole("button", { name: /使用左右方向键/ }));
    expect(
      within(screen.getByRole("tooltip")).getAllByText("暂无数据"),
    ).toHaveLength(2);
    expect(container.querySelector(".pw-chart__dot")).toBeNull();
    expect(container.querySelector(".pw-chart__series")).toBeNull();
  });

  it("formats grouped numbers, scaled units and axis-only M/B abbreviations", () => {
    const { container, rerender } = render(
      <Chart
        ariaLabel="UI-20 数量"
        categories={["现在"]}
        kind="ordinal"
        series={[{ id: "n", label: "请求数", values: [1_234_567] }]}
        valueFormat={{ unit: "次", compact: true }}
      />,
    );
    expect(container.querySelector(".pw-chart__ticks")).toHaveTextContent(
      "1.5M 次750,000 次0 次",
    );
    fireEvent.click(screen.getByRole("button", { name: /使用左右方向键/ }));
    expect(screen.getByRole("tooltip")).toHaveTextContent("1,234,567 次");
    rerender(
      <Chart
        ariaLabel="UI-20 数量"
        categories={["现在"]}
        kind="ordinal"
        series={[{ id: "n", label: "延迟", values: [2.15] }]}
        valueFormat={{ unit: "ms", scale: 1000 }}
      />,
    );
    expect(container.querySelector(".pw-chart__ticks")).toHaveTextContent(
      "2,500 ms1,250 ms0 ms",
    );
    expect(screen.getByRole("tooltip")).toHaveTextContent("2,150 ms");
  });

  it("passes axe with an open tooltip and preserves the accessible image", async () => {
    const { container } = renderUI20();
    const first = screen.getAllByRole("button", { name: /使用左右方向键/ })[0];
    if (!first) throw new Error("FX-UI-20 requires a column");
    fireEvent.click(first);
    expect(screen.getByRole("img", { name: "UI-20 延迟" })).toBeInTheDocument();
    const result = await axe.run(container, {
      rules: { "color-contrast": { enabled: false } },
    });
    expect(result.passes.flatMap((rule) => rule.nodes).length).toBeGreaterThan(
      0,
    );
    expect(result.violations).toEqual([]);
  });
});

// FX-UI-24: deterministic usage-shaped samples (Input/Output tokens per bucket).
const UI24_TOKENS = [
  { id: "input", label: "Input", values: [100, null, 40, null] },
  { id: "output", label: "Output", values: [150, 30, null, null] },
] as const;

function renderUI24Stacked() {
  return render(
    <Chart
      ariaLabel="UI-24 Tokens"
      categories={["10:00", "11:00", "12:00", "13:00"]}
      kind="ordinal"
      mark="bar"
      series={UI24_TOKENS}
      totalLabel="Total"
      valueFormat={{ unit: "Tokens", compact: true }}
    />,
  );
}

function barHeights(container: HTMLElement) {
  return [...container.querySelectorAll<HTMLElement>(".pw-chart__bar")].map(
    (bar) => bar.style.height,
  );
}

describe("UI-24 TC-UI-18 bar mark", () => {
  it("draws one bar per non-null sample on a zero baseline with whole ticks", () => {
    const { container } = render(
      <Chart
        ariaLabel="UI-24 Requests"
        categories={["10:00", "11:00", "12:00"]}
        kind="ordinal"
        mark="bar"
        series={[{ id: "requests", label: "Requests", values: [4, null, 9] }]}
        valueFormat={{ unit: "次" }}
      />,
    );
    expect(
      screen.getByRole("img", { name: "UI-24 Requests" }),
    ).toBeInTheDocument();
    expect(container.querySelector(".pw-chart__ticks")).toHaveTextContent(
      "10 次5 次0 次",
    );
    expect(container.querySelector(".pw-chart__series")).toBeNull();
    const columns = container.querySelectorAll(".pw-chart__bar-col");
    expect(columns).toHaveLength(3);
    expect(columns[1]?.querySelector(".pw-chart__bar")).toBeNull();
    expect(barHeights(container)).toEqual(["40%", "90%"]);
    expect(
      container.querySelectorAll(".pw-chart__bar.pw-chart__bar--end"),
    ).toHaveLength(2);
    expect(container.querySelector(".pw-chart__legend")).toBeNull();
  });

  it("stacks series bottom-up and scales the axis to the stacked total", () => {
    const { container } = renderUI24Stacked();
    expect(container.querySelector(".pw-chart__ticks")).toHaveTextContent(
      "250 Tokens125 Tokens0 Tokens",
    );
    const first = container.querySelectorAll(".pw-chart__bar-col")[0];
    const segments = first?.querySelectorAll<HTMLElement>(".pw-chart__bar");
    expect(segments).toHaveLength(2);
    expect(segments?.[0]).toHaveClass("pw-chart__swatch--ordinal-1");
    expect(segments?.[0]).not.toHaveClass("pw-chart__bar--end");
    expect(segments?.[0]?.style.height).toBe("40%");
    expect(segments?.[1]).toHaveClass(
      "pw-chart__swatch--ordinal-2",
      "pw-chart__bar--end",
    );
    expect(segments?.[1]?.style.height).toBe("60%");
    expect(
      container.querySelectorAll(".pw-chart__bar-col")[3]?.children,
    ).toHaveLength(0);
    const legend = screen.getByRole("list", { name: "图例" });
    expect(within(legend).getAllByRole("listitem")).toHaveLength(2);
  });

  it("dims the other columns on hover and lists every series plus the total", () => {
    const { container } = renderUI24Stacked();
    const hit = screen.getByRole("button", { name: /使用左右方向键/ });
    vi.spyOn(hit, "getBoundingClientRect").mockReturnValue(
      new DOMRect(0, 0, 100, 100),
    );
    fireEvent.mouseEnter(hit, { clientX: 10 });
    const tip = screen.getByRole("tooltip");
    expect(within(tip).getByText("10:00")).toBeVisible();
    expect(within(tip).getByText("100 Tokens")).toBeVisible();
    expect(within(tip).getByText("150 Tokens")).toBeVisible();
    expect(within(tip).getByText("Total")).toBeVisible();
    expect(within(tip).getByText("250 Tokens")).toBeVisible();
    const columns = [...container.querySelectorAll(".pw-chart__bar-col")];
    expect(
      columns.map((column) =>
        column.classList.contains("pw-chart__bar-col--dim"),
      ),
    ).toEqual([false, true, true, true]);
    expect(container.querySelector(".pw-chart__dot")).toBeNull();
    expect(container.querySelector(".pw-chart__crosshair")).toBeNull();

    // Band mapping: x=30 of 100 falls in the second of four columns.
    fireEvent.mouseMove(hit, { clientX: 30 });
    expect(screen.getByRole("tooltip")).toHaveTextContent("11:00");
    expect(
      within(screen.getByRole("tooltip")).getByText("暂无数据"),
    ).toBeVisible();
    expect(
      within(screen.getByRole("tooltip")).getAllByText("30 Tokens"),
    ).toHaveLength(2);

    fireEvent.mouseMove(hit, { clientX: 99 });
    const empty = screen.getByRole("tooltip");
    expect(empty).toHaveTextContent("13:00");
    expect(within(empty).getAllByText("暂无数据")).toHaveLength(3);

    fireEvent.mouseLeave(
      container.querySelector(".pw-chart__canvas") as Element,
    );
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
    expect(container.querySelector(".pw-chart__bar-col--dim")).toBeNull();
  });

  it("moves between columns with the keyboard", async () => {
    renderUI24Stacked();
    await userEvent.tab();
    expect(screen.getByRole("tooltip")).toHaveTextContent("10:00");
    await userEvent.keyboard("{ArrowRight}{ArrowRight}");
    const tip = screen.getByRole("tooltip");
    expect(tip).toHaveTextContent("12:00");
    expect(within(tip).getAllByText("40 Tokens")).toHaveLength(2);
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
  });

  it("keeps a total row in the data table for screen readers", async () => {
    renderUI24Stacked();
    await userEvent.click(screen.getByRole("button", { name: "查看数据表" }));
    const table = screen.getByRole("table", { name: "UI-24 Tokens" });
    const total = within(table).getByRole("row", { name: /^Total/ });
    const cells = within(total).getAllByRole("cell");
    expect(cells.slice(0, 3).map((cell) => cell.textContent)).toEqual([
      "250",
      "30",
      "40",
    ]);
    expect(
      within(cells[3] as HTMLElement).getByLabelText("暂无数据"),
    ).toBeVisible();
  });

  it("passes axe with an open bar tooltip", async () => {
    const { container } = renderUI24Stacked();
    fireEvent.click(screen.getByRole("button", { name: /使用左右方向键/ }));
    expect(screen.getByRole("tooltip")).toHaveTextContent("Total");
    const result = await axe.run(container, {
      rules: { "color-contrast": { enabled: false } },
    });
    expect(result.violations).toEqual([]);
  });
});
