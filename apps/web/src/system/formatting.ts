export function formatObservedAt(value: string) {
  return new Intl.DateTimeFormat("zh-CN", {
    day: "2-digit",
    hour: "2-digit",
    hourCycle: "h23",
    minute: "2-digit",
    month: "2-digit",
    second: "2-digit",
    year: "numeric",
  }).format(new Date(value));
}

/** Shared with the initialization wizard so byte rendering has one form. */
export function formatBytes(value: number) {
  const units = ["B", "KB", "MB", "GB", "TB"] as const;
  let amount = value;
  let unitIndex = 0;
  while (amount >= 1024 && unitIndex < units.length - 1) {
    amount /= 1024;
    unitIndex += 1;
  }
  const unit = units[unitIndex];
  if (unit === undefined) {
    throw new Error("byte unit index is outside the supported range");
  }
  return `${new Intl.NumberFormat("zh-CN", {
    maximumFractionDigits: amount < 10 && unitIndex > 0 ? 1 : 0,
  }).format(amount)} ${unit}`;
}
