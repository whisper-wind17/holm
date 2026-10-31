import { useEffect, useMemo, useRef, useState } from "react";

import { ApiError, type ApiClient } from "../api/client";
import { Button, InlineAlert } from "../components";
import { detectLogLevel, type LogLevel } from "./presentation";
import { useInstanceLogs } from "./queries";

const SENSITIVE_LOG_PATTERN =
  /\b(?:prompt|messages?|request[ _-]?body|response[ _-]?body|container(?:[ _-]?id)?|internal[ _-]?endpoint)\b|https?:\/\/|\/(?:workspace|models|dev\/nvidia)(?:\/|\b)|cuda_visible_devices|GPU-[0-9a-f-]{8,}/i;

/**
 * The display text is the only text that is ever filtered, shown or copied:
 * filtering the raw text would let the keyword box probe hidden content.
 */
export function safeLogText(text: string) {
  return SENSITIVE_LOG_PATTERN.test(text) ? "[敏感内容已隐藏]" : text;
}

export type LogLevelFilter = "ALL" | LogLevel;

/** A request from outside the tab (for example the banner's "查看错误日志"). */
export interface LogLevelRequest {
  level: LogLevelFilter;
}

const LEVEL_OPTIONS: ReadonlyArray<{ label: string; value: LogLevelFilter }> = [
  { label: "全部级别", value: "ALL" },
  { label: "ERROR", value: "ERROR" },
  { label: "WARN", value: "WARN" },
  { label: "INFO", value: "INFO" },
];

/**
 * logs. Lines live only in memory (query cache and React
 * state): no storage, no URL, so the filter keyword never leaves the page.
 */
export function InstanceLogs({
  apiClient,
  instanceId,
  levelRequest,
}: {
  apiClient: ApiClient;
  instanceId: string;
  levelRequest?: LogLevelRequest;
}) {
  const logs = useInstanceLogs(apiClient, instanceId);
  const [keyword, setKeyword] = useState("");
  const [level, setLevel] = useState<LogLevelFilter>(
    levelRequest?.level ?? "ALL",
  );
  const [atBottom, setAtBottom] = useState(false);
  const [autoScroll, setAutoScroll] = useState(false);
  const [copyState, setCopyState] = useState<"copied" | "failed">();
  const content = useRef<HTMLPreElement>(null);

  useEffect(() => {
    if (levelRequest !== undefined) setLevel(levelRequest.level);
  }, [levelRequest]);

  const lines = useMemo(
    () =>
      (logs.data?.entries ?? []).map((entry) => {
        const text = `${safeLogText(entry.text)}${entry.truncated ? " …（已截断）" : ""}`;
        return { level: detectLogLevel(text), sequence: entry.sequence, text };
      }),
    [logs.data],
  );
  const needle = keyword.trim().toLowerCase();
  const visible = useMemo(
    () =>
      lines.filter(
        (line) =>
          (level === "ALL" || line.level === level) &&
          (needle === "" || line.text.toLowerCase().includes(needle)),
      ),
    [level, lines, needle],
  );

  useEffect(() => {
    const element = content.current;
    if (autoScroll && element !== null)
      element.scrollTop = element.scrollHeight;
  }, [autoScroll, visible]);

  function onScroll() {
    const element = content.current;
    if (element === null) return;
    const reached =
      element.scrollHeight - element.scrollTop - element.clientHeight <= 1;
    setAtBottom(reached);
    if (!reached) setAutoScroll(false);
  }

  function jumpToLatest() {
    const element = content.current;
    if (element !== null) element.scrollTop = element.scrollHeight;
    setAtBottom(true);
  }

  async function copyVisible() {
    try {
      await navigator.clipboard.writeText(
        visible.map(({ text }) => text).join("\n"),
      );
      setCopyState("copied");
    } catch {
      setCopyState("failed");
    }
  }

  if (logs.isPending) {
    return (
      <section
        aria-busy="true"
        aria-label="模型实例日志正在加载"
        className="pw-instance-detail__section"
      >
        <span className="sk pw-instance-detail__skeleton" />
      </section>
    );
  }
  if (logs.data === undefined) {
    const permissionDenied =
      logs.error instanceof ApiError && logs.error.status === 403;
    return (
      <section className="pw-instance-detail__section">
        <InlineAlert
          title={
            permissionDenied ? "没有查看日志的权限" : "无法获取模型实例日志"
          }
          tone="negative"
        >
          {permissionDenied
            ? "当前账号不能查看此模型实例的日志。"
            : "日志暂时无法显示，请稍后重试。"}
        </InlineAlert>
      </section>
    );
  }
  if (lines.length === 0) {
    return (
      <section className="pw-instance-detail__section pw-instance-logs">
        <h2>日志</h2>
        <p className="pw-instance-logs__empty">暂无日志。</p>
      </section>
    );
  }
  return (
    <section className="pw-instance-detail__section pw-instance-logs">
      <h2>日志</h2>
      {logs.isError || !logs.data.complete ? (
        <InlineAlert title="更多日志未加载" tone="negative">
          已加载的日志仍可查看，请稍后重试。
        </InlineAlert>
      ) : null}
      <div className="pw-instance-logs__toolbar">
        <label>
          关键词
          <input
            onChange={(event) => {
              setKeyword(event.target.value);
            }}
            placeholder="筛选日志内容"
            type="search"
            value={keyword}
          />
        </label>
        <label>
          级别
          <select
            onChange={(event) => {
              setLevel(event.target.value as LogLevelFilter);
            }}
            value={level}
          >
            {LEVEL_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </label>
        <label className="pw-instance-logs__auto">
          <input
            checked={autoScroll}
            disabled={!atBottom && !autoScroll}
            onChange={(event) => {
              setAutoScroll(event.target.checked);
            }}
            type="checkbox"
          />
          自动滚动
        </label>
        <Button
          onClick={() => {
            void copyVisible();
          }}
          size="s"
        >
          复制可见日志
        </Button>
        {atBottom ? null : (
          <Button onClick={jumpToLatest} size="s" variant="ghost">
            滚动到最新
          </Button>
        )}
      </div>
      <p className="pw-instance-logs__note">
        最近 500 行 · 当前筛选显示 {visible.length} 行 · 令牌、凭证与 API
        密钥已在服务端脱敏
        {autoScroll || atBottom ? "" : " · 滚动到底部后才能开启自动滚动"}
      </p>
      {copyState === undefined ? null : (
        <p className="pw-instance-logs__note" role="status">
          {copyState === "copied"
            ? `已复制 ${String(visible.length)} 行日志`
            : "复制失败，请手动选择日志文本。"}
        </p>
      )}
      <pre
        aria-label="模型实例日志"
        className="pw-instance-logs__content"
        onScroll={onScroll}
        ref={content}
        tabIndex={0}
      >
        {visible.map((line) => (
          <span data-level={line.level ?? "none"} key={line.sequence}>
            {line.text}
            {"\n"}
          </span>
        ))}
      </pre>
      {visible.length === 0 ? (
        <p className="pw-instance-logs__empty">没有符合筛选条件的日志</p>
      ) : null}
    </section>
  );
}
