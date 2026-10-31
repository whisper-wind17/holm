import axe from "axe-core";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Chart, type ChartMark } from "./chart";

// FX-UI-51: synthetic samples isolate missing values, endpoints and collection gaps.
function draw(
  values: readonly (number | null)[],
  times = values.map((_, index) => index * 5000),
  gapMs = 15000,
  mark: ChartMark = "line",
) {
  return (
    <Chart
      ariaLabel="UI-51"
      kind="ordinal"
      categories={times.map(String)}
      timeAxis={{ times, gapMs, formatTick: String }}
      mark={mark}
      series={[{ id: "a", label: "A", values }]}
    />
  );
}

const dots = (container: HTMLElement) => [
  ...container.querySelectorAll<HTMLElement>(".pw-chart__isolated-dot"),
];
const segments = (container: HTMLElement) =>
  [...container.querySelectorAll("polyline")].map((line) =>
    line.getAttribute("points"),
  );

describe("UI-51 TC-UI-08 isolated line samples", () => {
  it("shows a null-bounded sample outside the stretched SVG and retains hover and table access", async () => {
    const { container } = render(draw([null, 5, null]));
    expect(segments(container)).toEqual(["50,6"]);
    expect(dots(container)).toHaveLength(1);
    expect(dots(container)[0]).toHaveStyle({ left: "50%", top: "6%" });
    expect(dots(container)[0]).toHaveAttribute("aria-hidden", "true");
    expect(dots(container)[0]?.closest("svg")).toBeNull();
    expect(container.querySelector("svg")).toHaveAttribute(
      "preserveAspectRatio",
      "none",
    );
    expect(container.querySelectorAll(".pw-chart__dot")).toHaveLength(0);
    const hit = screen.getByRole("button", { name: /使用左右方向键/ });
    fireEvent.focus(hit);
    fireEvent.keyDown(hit, { key: "ArrowRight" });
    expect(container.querySelectorAll(".pw-chart__dot")).toHaveLength(1);
    expect(screen.getByRole("tooltip")).toHaveTextContent("5");
    expect(dots(container)).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: "查看数据表" }));
    expect(screen.getByRole("table")).toHaveTextContent("5");
    expect(
      (
        await axe.run(container, {
          rules: { "color-contrast": { enabled: false } },
        })
      ).violations,
    ).toEqual([]);
  });

  it.each([15000, 180000])(
    "marks gap-separated endpoints for gapMs=%s",
    (gapMs) => {
      const { container } = render(
        draw([5, 5, 5], [0, gapMs + 1, 2 * (gapMs + 1)], gapMs),
      );
      expect(dots(container).map((dot) => dot.style.left)).toEqual([
        "0%",
        "50%",
        "100%",
      ]);
      expect(segments(container)).toEqual(["0,6", "50,6", "100,6"]);
    },
  );

  it("retains continuous segment coordinates around an isolated zero and matches its series palette", () => {
    const values = [5, 5, null, 0, null, 5, 5];
    const { container } = render(
      <Chart
        ariaLabel="UI-51 mixed"
        kind="categorical"
        overflowStrategy={{ kind: "small-multiples" }}
        categories={values.map((_, index) => String(index))}
        series={[
          { id: "empty", label: "Empty", values: values.map(() => null) },
          { id: "mixed", label: "Mixed", values },
        ]}
      />,
    );
    expect(segments(container)).toEqual([
      "0,6 16.666666666666664,6",
      "50,94",
      "83.33333333333334,6 100,6",
    ]);
    expect(dots(container)).toHaveLength(1);
    expect(dots(container)[0]).toHaveClass("pw-chart__swatch--categorical-2");
    expect(dots(container)[0]).toHaveStyle({ left: "50%", top: "94%" });
  });

  it("handles a single sample, null endpoints and an empty series without inventing points", () => {
    const { container, rerender } = render(draw([5]));
    expect(dots(container)).toHaveLength(1);
    expect(dots(container)[0]).toHaveClass("pw-chart__swatch--ordinal-1");
    expect(dots(container)[0]).toHaveStyle({ left: "50%", top: "6%" });
    rerender(draw([5, null, 5]));
    expect(dots(container).map((dot) => dot.style.left)).toEqual([
      "0%",
      "100%",
    ]);
    rerender(draw([null, null]));
    expect(dots(container)).toHaveLength(0);
    expect(segments(container)).toEqual([]);
    rerender(draw([]));
    expect(dots(container)).toHaveLength(0);
  });

  it("does not mark a continuous segment at the gap threshold or alter bars", () => {
    const { container, rerender } = render(draw([5, 5], [0, 15000]));
    expect(dots(container)).toHaveLength(0);
    expect(segments(container)).toEqual(["0,6 100,6"]);
    rerender(draw([null, 5, null], undefined, undefined, "bar"));
    expect(dots(container)).toHaveLength(0);
    expect(segments(container)).toEqual([]);
    expect(container.querySelectorAll(".pw-chart__bar")).toHaveLength(1);
  });
});
