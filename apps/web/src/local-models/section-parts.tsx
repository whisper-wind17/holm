import { Button, InlineAlert } from "../components";
import { formatObservedAt } from "../system/system-page";

export function SectionHeading({
  count,
  refreshing,
  summary,
  title,
}: {
  count?: number;
  refreshing: boolean;
  /** Replaces the "N 项" count, e.g. the download-task group tally. */
  summary?: string;
  title: string;
}) {
  return (
    <header className="pw-models-section__head">
      <span>
        <h2>{title}</h2>
        {summary !== undefined ? (
          <span>{summary}</span>
        ) : count === undefined ? null : (
          <span>{count} 项</span>
        )}
      </span>
      {refreshing ? <span role="status">正在刷新</span> : null}
    </header>
  );
}

export function StaleNotice({
  dataUpdatedAt,
  interval,
  label,
  stale,
}: {
  dataUpdatedAt: number;
  interval: number;
  label: string;
  stale: boolean;
}) {
  if (!stale || dataUpdatedAt === 0) return null;
  const refreshedAt = new Date(dataUpdatedAt).toISOString();
  return (
    <InlineAlert title={`${label}数据可能延迟`} tone="notice">
      自动刷新失败，仍显示最近一次成功取得的数据。最近成功刷新：
      <time dateTime={refreshedAt}>{formatObservedAt(refreshedAt)}</time>
      ；系统将每
      {String(interval / 1000)} 秒继续尝试。
    </InlineAlert>
  );
}

export function ReadError({
  onRetry,
  title,
}: {
  onRetry: () => void;
  title: string;
}) {
  return (
    <InlineAlert title={title} tone="negative">
      数据无法显示，请检查平台连接后重试。
      <Button onClick={onRetry} size="s">
        重试
      </Button>
    </InlineAlert>
  );
}
