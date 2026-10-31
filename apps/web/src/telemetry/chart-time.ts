import type { ServiceTelemetryRange } from "./queries";

/** omit timeZone in production to use the browser's local time zone. */
export function formatChartTime(
  value: string,
  range: ServiceTelemetryRange,
  target: "axis" | "detail",
  timeZone?: string,
): string {
  const parts = new Intl.DateTimeFormat("zh-CN", {
    timeZone,
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hourCycle: "h23",
  }).formatToParts(new Date(value));
  const part = (type: Intl.DateTimeFormatPartTypes) =>
    parts.find((item) => item.type === type)?.value ?? "";
  const date = `${part("month")}-${part("day")}`;
  const time = `${part("hour")}:${part("minute")}`;
  if (range === "7d") return target === "axis" ? date : `${date} ${time}`;
  return target === "detail" && (range === "1h" || range === "6h")
    ? `${time}:${part("second")}`
    : time;
}
