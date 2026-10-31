import { Chart } from "./chart";
import { StatusBadge } from "./status-badge";

export const MISSING_STATUS_TEXT = (
  // @ts-expect-error visible status text is a required part of the API.
  <StatusBadge icon="success" tone="positive" />
);

export const VALID_STATUS = (
  <StatusBadge icon="success" text="运行中" tone="positive" />
);

export const INVALID_DOUBLE_AXIS = (
  <Chart
    ariaLabel="请求"
    categories={["现在"]}
    kind="ordinal"
    // @ts-expect-error the chart API deliberately exposes no second Y axis.
    secondaryYAxis="延迟"
    series={[{ id: "p50", label: "P50", values: [1] }]}
  />
);

export const INVALID_CHART_MARK = (
  <Chart
    ariaLabel="请求"
    categories={["现在"]}
    kind="ordinal"
    // @ts-expect-error only line and bar marks are supported.
    mark="area"
    series={[{ id: "requests", label: "请求", values: [1] }]}
  />
);
