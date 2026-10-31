/** summary Token counts only; requests, detail rows and exports stay exact. */
export function formatSummaryCount(value: number): string {
  const millions = value / 1_000_000;
  if (value >= 1_000_000_000 || Number(millions.toFixed(1)) >= 1000)
    return `${String(Number((value / 1_000_000_000).toFixed(1)))}B`;
  if (value >= 1_000_000) return `${String(Number(millions.toFixed(1)))}M`;
  return value.toLocaleString("zh-CN");
}
