import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router";
import type { ApiClient } from "../api/client";
import type { UsageGroupBy, UsageReport } from "../api/usage-contracts";
import { useSession } from "../auth/session";
import { Button, InlineAlert } from "../components";

export type UserUsageRange = "today" | "week" | "month" | "keys";
export function usageWindow(range: UserUsageRange, now = new Date()) {
  const from =
    range === "today"
      ? new Date(now.getFullYear(), now.getMonth(), now.getDate())
      : range === "month"
        ? new Date(now.getFullYear(), now.getMonth(), 1)
        : new Date(now.getTime() - (range === "week" ? 7 : 30) * 86400000);
  return {
    from: from.toISOString(),
    to: now.toISOString(),
    timeZone: Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC",
  };
}
export function useUserUsage(
  apiClient: ApiClient,
  userId: string,
  range: UserUsageRange,
  groupBy: UsageGroupBy,
) {
  const session = useSession();
  const allowed =
    session.me?.capabilities.some(
      ({ capability }) => capability === "view_usage",
    ) === true;
  return useQuery({
    enabled: allowed,
    queryKey: [
      "user-detail-usage",
      userId,
      range,
      groupBy,
      new Date().toDateString(),
    ],
    queryFn: () =>
      apiClient.getUsage({ ...usageWindow(range), groupBy, userId }),
    refetchInterval: 30_000,
  });
}
export function UsageNumber({ value }: { value: number | null | undefined }) {
  const text =
    value == null
      ? "—"
      : value >= 1e9
        ? `${(value / 1e9).toFixed(1)}B`
        : value >= 1e6
          ? `${(value / 1e6).toFixed(1)}M`
          : value.toLocaleString("en-US");
  return (
    <span
      className={value === 0 ? "pw-user-detail__muted" : undefined}
      title={value == null ? undefined : value.toLocaleString("en-US")}
    >
      {text}
    </span>
  );
}
export function UserUsage({
  apiClient,
  userId,
}: {
  apiClient: ApiClient;
  userId: string;
}) {
  const [range, setRange] = useState<UserUsageRange>("today");
  const services = useUserUsage(apiClient, userId, range, "service");
  const keys = useUserUsage(apiClient, userId, range, "api_key");
  const summary = services.isError ? undefined : services.data?.summary;
  return (
    <section className="pw-user-detail__stack" aria-label="用量">
      <div className="pw-user-detail__row">
        <div role="group" aria-label="时间范围">
          {(
            [
              ["today", "今日"],
              ["week", "近 7 天"],
              ["month", "本月"],
            ] as const
          ).map(([id, label]) => (
            <Button
              key={id}
              aria-pressed={range === id}
              onClick={() => {
                setRange(id);
              }}
            >
              {label}
            </Button>
          ))}
        </div>
        <Link to={`/usage?user_id=${encodeURIComponent(userId)}`}>
          在「用量」页查看明细
        </Link>
      </div>
      {services.isError || keys.isError ? (
        <InlineAlert title="无法获取用量" tone="negative">
          用量暂时无法显示，请稍后重试。
          <Button
            onClick={() => {
              void services.refetch();
              void keys.refetch();
            }}
          >
            重试
          </Button>
        </InlineAlert>
      ) : null}
      <div className="pw-user-detail__metrics">
        {(
          [
            ["Requests", summary?.requests],
            ["Input Tokens", summary?.input_tokens],
            ["Output Tokens", summary?.output_tokens],
            ["Total Tokens", summary?.total_tokens],
          ] as const
        ).map(([label, value]) => (
          <div className="pw-user-detail__tile" key={label}>
            <span>{label}</span>
            <strong>
              <UsageNumber value={value} />
            </strong>
          </div>
        ))}
      </div>
      {services.isPending && services.fetchStatus === "fetching" ? (
        <p role="status">正在加载用量…</p>
      ) : null}
      {summary?.requests === 0 ? (
        <p>所选时间范围内没有调用记录。</p>
      ) : (
        <div className="pw-user-detail__grid">
          <UsageTable
            title="按 AI 服务"
            report={services.isError ? undefined : services.data}
          />
          <UsageTable
            title="按 API 密钥"
            report={keys.isError ? undefined : keys.data}
          />
        </div>
      )}
      <p className="pw-user-detail__muted">
        已撤销、已过期的密钥与已撤销授权的服务，历史用量照常计入。数字按 M／B
        缩写，悬停显示全数。
      </p>
    </section>
  );
}
function UsageTable({
  title,
  report,
}: {
  title: string;
  report: UsageReport | undefined;
}) {
  return (
    <div className="pw-user-detail__table">
      <table className="pw-table">
        <caption>{title}</caption>
        <thead>
          <tr>
            {["名称", "请求", "Input", "Output", "Total"].map((label) => (
              <th key={label} scope="col">
                {label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {report?.details.map((row, index) => (
            <tr key={row.group_id ?? index}>
              <th scope="row">
                {row.display_name}
                {row.status === "Expired"
                  ? "（已过期）"
                  : row.status === "Revoked"
                    ? "（已撤销）"
                    : ""}
              </th>
              {[
                row.requests,
                row.input_tokens,
                row.output_tokens,
                row.total_tokens,
              ].map((value, i) => (
                <td key={i}>
                  <UsageNumber value={value} />
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
