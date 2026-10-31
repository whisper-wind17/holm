import { useId, useState, type MouseEvent } from "react";

import { Button } from "./button";
import { MissingValue } from "./data-table";

export interface ChartSeries {
  id: string;
  label: string;
  values: readonly (number | null)[];
}

export type ChartOverflowStrategy =
  | {
      combine: (series: readonly ChartSeries[]) => ChartSeries;
      kind: "other";
    }
  | {
      kind: "small-multiples";
    };

export interface ChartValueFormat {
  unit?: string;
  /** Converts source values before choosing ticks (for example seconds to ms). */
  scale?: number;
  /** count abbreviations apply only to axis ticks. */
  compact?: boolean;
  integer?: boolean;
}

/** Times are chronological epoch milliseconds, one per category. */
export interface ChartTimeAxis {
  times: readonly number[];
  formatTick: (time: number) => string;
  gapMs: number;
}

export type ChartMark = "line" | "bar";

interface ChartBaseProps {
  timeAxis?: ChartTimeAxis;
  valueFormat?: ChartValueFormat;
  ariaLabel: string;
  categories: readonly string[];
  /** Caller-formatted x-axis labels; the chart does not interpret time. */
  tickLabel?: (index: number, category: string) => string;
  /** bars stack multiple series; omitted keeps the line chart. */
  mark?: ChartMark;
  /** label of the stacked-bar sum row in the tooltip and data table. */
  totalLabel?: string;
}

export interface CategoricalChartProps extends ChartBaseProps {
  kind: "categorical";
  overflowStrategy: ChartOverflowStrategy;
  series: readonly ChartSeries[];
}

type OrdinalSeries =
  | readonly [ChartSeries]
  | readonly [ChartSeries, ChartSeries]
  | readonly [ChartSeries, ChartSeries, ChartSeries];

export interface OrdinalChartProps extends ChartBaseProps {
  kind: "ordinal";
  series: OrdinalSeries;
}

export type ChartProps = CategoricalChartProps | OrdinalChartProps;

export const CATEGORY_PALETTE = [
  "var(--pw-chart-cat-1)",
  "var(--pw-chart-cat-2)",
  "var(--pw-chart-cat-3)",
] as const;

export const ORDINAL_PALETTE = [
  "var(--pw-chart-ord-1)",
  "var(--pw-chart-ord-2)",
  "var(--pw-chart-ord-3)",
] as const;

export function prepareCategoricalSeries(
  series: readonly ChartSeries[],
  overflowStrategy: ChartOverflowStrategy,
) {
  if (
    series.length <= CATEGORY_PALETTE.length ||
    overflowStrategy.kind === "small-multiples"
  ) {
    return series;
  }
  return [
    ...series.slice(0, CATEGORY_PALETTE.length - 1),
    overflowStrategy.combine(series.slice(CATEGORY_PALETTE.length - 1)),
  ];
}

function pointSegments(
  values: readonly (number | null)[],
  maximum: number,
  position: (index: number) => number,
  timeAxis?: ChartTimeAxis,
) {
  const segments: string[] = [];
  let current: string[] = [];
  values.forEach((value, index) => {
    const time = timeAxis?.times[index];
    const previousTime = timeAxis?.times[index - 1];
    if (
      timeAxis &&
      time !== undefined &&
      previousTime !== undefined &&
      time - previousTime > timeAxis.gapMs
    ) {
      if (current.length > 0) segments.push(current.join(" "));
      current = [];
    }
    if (value === null) {
      if (current.length > 0) segments.push(current.join(" "));
      current = [];
      return;
    }
    const x = position(index);
    const y = 94 - (Math.max(0, value) / maximum) * 88;
    current.push(`${String(x)},${String(y)}`);
  });
  if (current.length > 0) segments.push(current.join(" "));
  return segments;
}

function niceMaximum(value: number) {
  if (value <= 0) return 1;
  const magnitude = 10 ** Math.floor(Math.log10(value));
  const step =
    [1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10].find(
      (candidate) => candidate * magnitude >= value,
    ) ?? 10;
  return step * magnitude;
}

function formatChartValue(
  value: number,
  format: ChartValueFormat,
  tick = false,
) {
  let divisor = 1;
  let suffix = "";
  if (tick && format.compact && value >= 1_000_000) {
    divisor = value >= 1_000_000_000 ? 1_000_000_000 : 1_000_000;
    suffix = divisor === 1_000_000_000 ? "B" : "M";
  }
  const number = (value / divisor).toLocaleString("en-US", {
    maximumFractionDigits: divisor === 1 ? 6 : 1,
  });
  return `${number}${suffix}${format.unit ? ` ${format.unit}` : ""}`;
}

function xPosition(index: number, count: number) {
  return count <= 1 ? 50 : (index / (count - 1)) * 100;
}

function barCenter(index: number, count: number) {
  return ((index + 0.5) / Math.max(1, count)) * 100;
}

/** Null only when every series lacks the sample, matching the usage API total. */
function stackTotal(series: readonly ChartSeries[], index: number) {
  const values = series
    .map((item) => item.values[index])
    .filter((value): value is number => value != null);
  return values.length === 0
    ? null
    : values.reduce((sum, value) => sum + value, 0);
}

function ChartPlot({
  label,
  palette,
  series,
  categories,
  tickLabel,
  timeAxis,
  mark,
  totalLabel,
  valueFormat = {},
}: {
  label: string;
  palette: "categorical" | "ordinal";
  series: readonly ChartSeries[];
  categories: readonly string[];
  tickLabel?: (index: number, category: string) => string;
  mark: ChartMark;
  totalLabel: string;
  timeAxis?: ChartTimeAxis;
  valueFormat?: ChartValueFormat;
}) {
  // Bar categories retain their equal-width bands even if a time axis is supplied.
  const axis = mark === "line" ? timeAxis : undefined;
  const start = axis?.times[0] ?? 0;
  const end = axis?.times[axis.times.length - 1] ?? start;
  const tickCount = axis && end > start ? 5 : Math.min(5, categories.length);
  const tickIndices = Array.from({ length: tickCount }, (_, index) =>
    tickCount === 1
      ? 0
      : Math.round((index * (categories.length - 1)) / (tickCount - 1)),
  );
  const [activeIndex, setActiveIndex] = useState<number | null>(null);
  const [focusIndex, setFocusIndex] = useState(0);
  const tooltipId = useId();
  const scale = valueFormat.scale ?? 1;
  const bar = mark === "bar";
  const stacked = bar && series.length >= 2;
  const niceMax = niceMaximum(
    bar
      ? categories.reduce(
          (max, _, index) =>
            Math.max(
              max,
              series.reduce(
                (sum, item) => sum + Math.max(0, item.values[index] ?? 0),
                0,
              ) * scale,
            ),
          0,
        )
      : series.reduce(
          (max, item) =>
            item.values.reduce<number>(
              (result, value) => Math.max(result, (value ?? 0) * scale),
              max,
            ),
          0,
        ),
  );
  const maximum = valueFormat.integer
    ? Math.max(1, Math.ceil(niceMax))
    : niceMax;
  const ticks = valueFormat.integer
    ? maximum === 1
      ? [1, 0]
      : [maximum, Math.floor(maximum / 2), 0]
    : [maximum, maximum / 2, 0];
  const markX = (index: number) =>
    bar
      ? barCenter(index, categories.length)
      : axis
        ? end === start
          ? 50
          : (((axis.times[index] ?? start) - start) / (end - start)) * 100
        : xPosition(index, categories.length);
  const active =
    activeIndex !== null && activeIndex < categories.length
      ? activeIndex
      : null;
  const activeTotal =
    stacked && active !== null ? stackTotal(series, active) : null;
  // Lines snap to the nearest sample; bars own an equal band per category.
  const columnAt = (clientX: number, bounds: DOMRect) => {
    const fraction = (clientX - bounds.left) / bounds.width;
    if (axis) {
      return axis.times.reduce(
        (nearest, _, index) =>
          Math.abs(markX(index) - fraction * 100) <
          Math.abs(markX(nearest) - fraction * 100)
            ? index
            : nearest,
        0,
      );
    }
    const index = bar
      ? Math.floor(fraction * categories.length)
      : Math.round(fraction * (categories.length - 1));
    return Math.max(0, Math.min(categories.length - 1, index));
  };
  const selectColumn = (event: MouseEvent<HTMLButtonElement>) => {
    const bounds = event.currentTarget.getBoundingClientRect();
    if (bounds.width > 0) {
      setActiveIndex(columnAt(event.clientX, bounds));
    }
  };
  const swatchClass = (index: number) =>
    `pw-chart__swatch pw-chart__swatch--${palette}-${String(index + 1)}`;
  return (
    <div className="pw-chart__plot">
      <div className="pw-chart__frame">
        <div className="pw-chart__ticks">
          {ticks.map((value, index) => (
            <span
              key={index}
              style={{ top: `${String(94 - (value / maximum) * 88)}%` }}
              title={formatChartValue(value, valueFormat)}
              aria-label={formatChartValue(value, valueFormat)}
            >
              {formatChartValue(value, valueFormat, true)}
            </span>
          ))}
        </div>
        <div
          className="pw-chart__canvas"
          onMouseLeave={() => {
            setActiveIndex(null);
          }}
        >
          <div role="img" aria-label={label}>
            <svg
              aria-hidden="true"
              preserveAspectRatio="none"
              viewBox="0 0 100 100"
            >
              {ticks
                .map((value) => 94 - (value / maximum) * 88)
                .map((y) => (
                  <line
                    key={y}
                    className="pw-chart__grid"
                    x1="0"
                    x2="100"
                    y1={y}
                    y2={y}
                  />
                ))}
              <line className="pw-chart__grid" x1="0" x2="0" y1="6" y2="94" />
              {bar
                ? null
                : series.flatMap((item, index) =>
                    pointSegments(
                      categories.map((_, i) => item.values[i] ?? null),
                      maximum / scale,
                      markX,
                      axis,
                    ).map((points, segmentIndex) => (
                      <polyline
                        className={`pw-chart__series pw-chart__series--${palette}-${String(index + 1)}`}
                        key={`${item.id}-${String(segmentIndex)}`}
                        points={points}
                      />
                    )),
                  )}
            </svg>
            {/* HTML markers stay circular when the SVG stretches on either axis. */}
            {bar
              ? null
              : series.flatMap((item, index) =>
                  pointSegments(
                    categories.map((_, i) => item.values[i] ?? null),
                    maximum / scale,
                    markX,
                    axis,
                  ).flatMap((points, segmentIndex) => {
                    if (points.includes(" ")) return [];
                    const [x, y] = points.split(",");
                    return (
                      <span
                        aria-hidden="true"
                        className={`pw-chart__isolated-dot pw-chart__swatch--${palette}-${String(index + 1)}`}
                        key={`${item.id}-${String(segmentIndex)}`}
                        style={{ left: `${String(x)}%`, top: `${String(y)}%` }}
                      />
                    );
                  }),
                )}
            {bar ? (
              <div aria-hidden="true" className="pw-chart__bars">
                {categories.map((category, column) => {
                  const segments = series.flatMap((item, index) => {
                    const value = item.values[column];
                    return value == null || value <= 0
                      ? []
                      : [{ index, id: item.id, value }];
                  });
                  return (
                    <div
                      className={`pw-chart__bar-col${active !== null && active !== column ? " pw-chart__bar-col--dim" : ""}`}
                      key={`${category}-${String(column)}`}
                      style={{
                        left: `${String((column / categories.length) * 100)}%`,
                        width: `${String(100 / categories.length)}%`,
                      }}
                    >
                      {segments.map((segment, position) => (
                        <span
                          className={`pw-chart__bar pw-chart__swatch--${palette}-${String(segment.index + 1)}${position === segments.length - 1 ? " pw-chart__bar--end" : ""}`}
                          key={segment.id}
                          style={{
                            height: `${String(((segment.value * scale) / maximum) * 100)}%`,
                          }}
                        />
                      ))}
                    </div>
                  );
                })}
              </div>
            ) : null}
          </div>
          {categories.length > 0 ? (
            <Button
              className="pw-chart__hit"
              variant="ghost"
              aria-label={`${label}，使用左右方向键查看数值`}
              aria-describedby={active !== null ? tooltipId : undefined}
              onMouseEnter={selectColumn}
              onMouseMove={selectColumn}
              onClick={(event) => {
                const bounds = event.currentTarget.getBoundingClientRect();
                setActiveIndex(
                  event.detail > 0 && bounds.width > 0
                    ? columnAt(event.clientX, bounds)
                    : Math.max(0, Math.min(categories.length - 1, focusIndex)),
                );
              }}
              onFocus={() => {
                setActiveIndex(Math.min(focusIndex, categories.length - 1));
              }}
              onBlur={() => {
                setActiveIndex(null);
              }}
              onKeyDown={(event) => {
                if (event.key === "Escape") {
                  setActiveIndex(null);
                  return;
                }
                if (event.key !== "ArrowLeft" && event.key !== "ArrowRight")
                  return;
                event.preventDefault();
                const next = Math.max(
                  0,
                  Math.min(
                    categories.length - 1,
                    (active ?? focusIndex) +
                      (event.key === "ArrowRight" ? 1 : -1),
                  ),
                );
                setFocusIndex(next);
                setActiveIndex(next);
              }}
            />
          ) : null}
          {active !== null ? (
            <>
              {bar ? null : (
                <span
                  className="pw-chart__crosshair"
                  style={{
                    left: `${String(markX(active))}%`,
                  }}
                />
              )}
              {(bar ? [] : series).map((item, index) => {
                const value = item.values[active];
                return value == null ? null : (
                  <span
                    key={item.id}
                    className={`pw-chart__dot ${swatchClass(index)}`}
                    style={{
                      left: `${String(markX(active))}%`,
                      top: `${String(94 - ((value * scale) / maximum) * 88)}%`,
                    }}
                  />
                );
              })}
              <div
                className={`pw-chart__tooltip${markX(active) > 50 ? " pw-chart__tooltip--left" : ""}`}
                role="tooltip"
                id={tooltipId}
              >
                <strong>{categories[active]}</strong>
                {series.map((item, index) => (
                  <div className="pw-chart__tooltip-row" key={item.id}>
                    <span className={swatchClass(index)} aria-hidden="true" />
                    <span>{item.label}</span>
                    <b>
                      {item.values[active] == null
                        ? axis
                          ? "—"
                          : "暂无数据"
                        : formatChartValue(
                            item.values[active] * scale,
                            valueFormat,
                          )}
                    </b>
                  </div>
                ))}
                {stacked ? (
                  <div className="pw-chart__tooltip-row">
                    <span>{totalLabel}</span>
                    <b>
                      {activeTotal === null
                        ? "暂无数据"
                        : formatChartValue(activeTotal * scale, valueFormat)}
                    </b>
                  </div>
                ) : null}
              </div>
            </>
          ) : null}
        </div>
        {(axis || tickLabel) && tickCount > 0 ? (
          <div className="pw-chart__x-ticks">
            {tickIndices.map((index, position) => {
              const x = axis ? xPosition(position, tickCount) : markX(index);
              const previous = tickIndices[position - 1];
              const next = tickIndices[position + 1];
              const halfGap = Math.min(
                previous === undefined
                  ? 100
                  : (x -
                      (axis
                        ? xPosition(position - 1, tickCount)
                        : markX(previous))) /
                      2,
                next === undefined
                  ? 100
                  : ((axis ? xPosition(position + 1, tickCount) : markX(next)) -
                      x) /
                      2,
              );
              const first = position === 0;
              const last = position === tickCount - 1;
              return (
                <span
                  key={position}
                  style={{
                    left: `${String(x)}%`,
                    width: `${String(tickCount === 1 ? 100 : first || last ? halfGap : halfGap * 2)}%`,
                    transform: `translateX(${first && tickCount > 1 ? "0" : last && tickCount > 1 ? "-100%" : "-50%"})`,
                    textAlign:
                      first && tickCount > 1
                        ? "left"
                        : last && tickCount > 1
                          ? "right"
                          : "center",
                  }}
                >
                  {axis
                    ? axis.formatTick(start + ((end - start) * x) / 100)
                    : tickLabel?.(index, categories[index] ?? "")}
                </span>
              );
            })}
          </div>
        ) : null}
      </div>
      {series.length >= 2 ? (
        <ul aria-label="图例" className="pw-chart__legend">
          {series.map((item, index) => (
            <li key={item.id}>
              <span aria-hidden="true" className={swatchClass(index)} />
              {item.label}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

export function Chart({
  ariaLabel,
  categories,
  tickLabel,
  timeAxis,
  series,
  valueFormat,
  mark = "line",
  totalLabel = "合计",
  ...props
}: ChartProps) {
  const tableId = useId();
  const [tableVisible, setTableVisible] = useState(false);
  const preparedSeries =
    props.kind === "categorical"
      ? prepareCategoricalSeries(series, props.overflowStrategy)
      : series;
  const smallMultiples =
    props.kind === "categorical" &&
    props.overflowStrategy.kind === "small-multiples" &&
    preparedSeries.length > CATEGORY_PALETTE.length;
  const stackedBars =
    mark === "bar" && !smallMultiples && preparedSeries.length >= 2;

  return (
    <section aria-label={ariaLabel} className="pw-chart">
      {smallMultiples ? (
        <div className="pw-chart__multiples">
          {preparedSeries.map((item) => (
            <ChartPlot
              categories={categories}
              tickLabel={tickLabel}
              timeAxis={timeAxis}
              valueFormat={valueFormat}
              key={item.id}
              label={`${ariaLabel}：${item.label}`}
              mark={mark}
              palette="categorical"
              series={[item]}
              totalLabel={totalLabel}
            />
          ))}
        </div>
      ) : (
        <ChartPlot
          categories={categories}
          tickLabel={tickLabel}
          timeAxis={timeAxis}
          valueFormat={valueFormat}
          label={ariaLabel}
          mark={mark}
          palette={props.kind}
          series={preparedSeries}
          totalLabel={totalLabel}
        />
      )}
      <Button
        aria-controls={tableId}
        aria-expanded={tableVisible}
        onClick={() => {
          setTableVisible((visible) => !visible);
        }}
        size="s"
        variant="ghost"
      >
        {tableVisible ? "隐藏数据表" : "查看数据表"}
      </Button>
      <div hidden={!tableVisible} id={tableId}>
        <table className="pw-chart__table">
          <caption>{ariaLabel}</caption>
          <thead>
            <tr>
              <th scope="col">项目</th>
              {categories.map((category, index) => (
                <th scope="col" key={`${category}-${String(index)}`}>
                  {category}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {preparedSeries.map((item) => (
              <tr key={item.id}>
                <th scope="row">{item.label}</th>
                {categories.map((category, index) => (
                  <td key={`${category}-${String(index)}`}>
                    {item.values[index] ?? (
                      <MissingValue
                        kind="unavailable"
                        reason="此时间点暂无数据"
                      />
                    )}
                  </td>
                ))}
              </tr>
            ))}
            {stackedBars ? (
              <tr>
                <th scope="row">{totalLabel}</th>
                {categories.map((category, index) => (
                  <td key={`${category}-${String(index)}`}>
                    {stackTotal(preparedSeries, index) ?? (
                      <MissingValue
                        kind="unavailable"
                        reason="此时间点暂无数据"
                      />
                    )}
                  </td>
                ))}
              </tr>
            ) : null}
          </tbody>
        </table>
      </div>
    </section>
  );
}
