import { useQuery } from "@tanstack/react-query";
import {
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";

import { useSearchParams } from "react-router";

import type { ApiClient } from "../api/client";
import type {
  UsageFilters,
  UsageGroup,
  UsageGroupBy,
  UsageTotals,
} from "../api/usage-contracts";
import { useSession } from "../auth/session";
import {
  Button,
  Chart,
  DataTable,
  EmptyState,
  InlineAlert,
  StatusBadge,
  type TableColumn,
} from "../components";
import { useUsage, useUsageSummary } from "./queries";

type DatePreset = "today" | "7d" | "30d" | "custom";

interface ServiceOption {
  id: string;
  label: string;
}

export interface LockedUsageService {
  id: string;
  label: string;
}

export function UsagePage({ apiClient }: { apiClient: ApiClient }) {
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => heading.current?.focus(), []);
  const [searchParams] = useSearchParams();
  return (
    <section className="pw-usage-page">
      <header className="pw-usage-page__head">
        <div>
          <h1 ref={heading} tabIndex={-1}>
            用量
          </h1>
        </div>
      </header>
      <UsagePanel
        apiClient={apiClient}
        initialServiceId={searchParams.get("service_id") ?? ""}
        initialUserId={searchParams.get("user_id") ?? ""}
      />
    </section>
  );
}

export function UsagePanel({
  apiClient,
  lockedService,
  initialServiceId = "",
  initialUserId = "",
}: {
  apiClient: ApiClient;
  lockedService?: LockedUsageService;
  initialServiceId?: string;
  initialUserId?: string;
}) {
  const session = useSession();
  const isAdmin = session.me?.role === "admin";
  const timeZone = browserTimeZone();
  const now = useMemo(() => new Date(), []);
  const [preset, setPreset] = useState<DatePreset>("7d");
  const [customFrom, setCustomFrom] = useState(() => dateInputValue(now));
  const [customTo, setCustomTo] = useState(() => dateInputValue(now));
  const [serviceId, setServiceId] = useState(
    lockedService?.id ?? initialServiceId,
  );
  const [userId, setUserId] = useState(isAdmin ? initialUserId : "");
  const [selectedKeyName, setSelectedKeyName] = useState("");
  const [clearedKeyName, setClearedKeyName] = useState<string | null>(null);
  const [apiKeyId, setApiKeyId] = useState("");
  const [groupBy, setGroupBy] = useState<UsageGroupBy>(
    lockedService ? (isAdmin ? "user" : "api_key") : "service",
  );
  const keySelectId = useId();
  const keyAvailabilityDescriptionId = useId();
  const range = useMemo(
    () => usageRange(now, preset, customFrom, customTo),
    [customFrom, customTo, now, preset],
  );

  const services = useQuery({
    enabled: isAdmin,
    queryFn: async (): Promise<ServiceOption[]> => {
      const response = await apiClient.getAiServices();
      return response.items.map((service) => ({
        id: service.id,
        label: `${service.name} · ${service.public_model_name}`,
      }));
    },
    queryKey: ["usage-service-options", isAdmin],
  });
  const users = useQuery({
    enabled: isAdmin,
    queryFn: () => apiClient.getUsers({ includeDeleted: true }),
    queryKey: ["usage-user-options"],
  });

  const filters: UsageFilters = {
    ...(apiKeyId === "" ? {} : { apiKeyId }),
    from: range.from.toISOString(),
    groupBy,
    ...(lockedService === undefined && serviceId === ""
      ? {}
      : { serviceId: lockedService?.id ?? serviceId }),
    timeZone,
    to: range.to.toISOString(),
    ...(isAdmin && userId !== "" ? { userId } : {}),
  };
  const report = useUsage(apiClient, filters, range.valid);
  const serviceUsageOptions = useUsage(
    apiClient,
    {
      ...filters,
      apiKeyId: undefined,
      groupBy: "service",
      serviceId: undefined,
      userId: undefined,
    },
    range.valid && lockedService === undefined,
  );
  const keyOptions = useUsage(
    apiClient,
    { ...filters, apiKeyId: undefined, groupBy: "api_key" },
    range.valid,
  );
  const allKeyOptions = useUsage(
    apiClient,
    {
      ...filters,
      apiKeyId: undefined,
      groupBy: "api_key",
      serviceId: undefined,
      userId: undefined,
    },
    range.valid,
  );
  const summary = useUsageSummary(
    apiClient,
    timeZone,
    lockedService === undefined,
  );

  const retry = () => {
    void Promise.all([
      report.refetch(),
      keyOptions.refetch(),
      allKeyOptions.refetch(),
      lockedService === undefined ? summary.refetch() : Promise.resolve(),
    ]);
  };

  const availableKeys = usageKeyGroups(keyOptions.data?.details);
  const availableKeyIds = new Set(availableKeys.map((group) => group.group_id));
  const excludedKeys = usageKeyGroups(allKeyOptions.data?.details).filter(
    (group) => !availableKeyIds.has(group.group_id),
  );
  const exclusionReason = keyExclusionReason(
    lockedService !== undefined || serviceId !== "",
    isAdmin && userId !== "",
  );
  const keyAvailabilityDescription = allKeyOptions.isError
    ? "无法确认完整 API 密钥列表。当前筛选范围内的 API 密钥仍可选择，请稍后重试。"
    : excludedKeys.length > 0 && exclusionReason !== null
      ? `以下 API 密钥已保留但不可选择：${excludedKeys
          .map(
            (group) => `${keyDisplayName(group, isAdmin)}：${exclusionReason}`,
          )
          .join("；")}。`
      : null;

  // Only a successful result for the new scope can invalidate the selection.
  useEffect(() => {
    if (
      range.valid &&
      apiKeyId !== "" &&
      keyOptions.isSuccess &&
      !keyOptions.isFetching &&
      !keyOptions.data.details.some((group) => group.group_id === apiKeyId)
    ) {
      setClearedKeyName(selectedKeyName);
      setApiKeyId("");
    }
  }, [
    apiKeyId,
    keyOptions.data,
    keyOptions.isFetching,
    keyOptions.isSuccess,
    range.valid,
    selectedKeyName,
  ]);

  const serviceOptions = isAdmin ? [...(services.data ?? [])] : [];
  for (const group of serviceUsageOptions.data?.details ?? []) {
    if (
      group.group_id !== null &&
      (!isAdmin || group.is_deleted) &&
      !serviceOptions.some((service) => service.id === group.group_id)
    ) {
      serviceOptions.push({
        id: group.group_id,
        label: `${group.display_name}${group.is_deleted ? "（已删除）" : ""}`,
      });
    }
  }
  const hasFilters =
    (lockedService === undefined && serviceId !== "") ||
    userId !== "" ||
    apiKeyId !== "";
  const clearFilters = () => {
    if (lockedService === undefined) setServiceId("");
    setUserId("");
    setApiKeyId("");
    setClearedKeyName(null);
  };
  const drillDown = (row: UsageGroup) => {
    if (row.group_id === null) return;
    if (groupBy === "service") {
      if (lockedService === undefined) setServiceId(row.group_id);
      setGroupBy(isAdmin ? "user" : "api_key");
    } else if (groupBy === "user") {
      setUserId(row.group_id);
      setGroupBy("api_key");
    } else {
      setApiKeyId(row.group_id);
      setSelectedKeyName(row.api_key_name ?? row.display_name);
      setClearedKeyName(null);
      setGroupBy(lockedService ? (isAdmin ? "user" : "api_key") : "service");
    }
  };

  return (
    <div className="pw-usage-panel">
      {lockedService === undefined ? (
        <UsageSummaryCards
          loading={summary.isPending}
          thisMonth={summary.data?.this_month}
          today={summary.data?.today}
        />
      ) : (
        <p className="pw-usage-panel__scope">
          当前范围：AI 服务 <strong>{lockedService.label}</strong>
        </p>
      )}

      <form
        className="pw-usage-filters"
        onSubmit={(event) => {
          event.preventDefault();
        }}
      >
        <div className="pw-usage-range-controls">
          <div className="pw-usage-segments" role="group" aria-label="时间范围">
            {(
              [
                ["today", "今日"],
                ["7d", "近 7 天"],
                ["30d", "近 30 天"],
                ["custom", "自定义"],
              ] as const
            ).map(([id, label]) => (
              <Button
                key={id}
                size="s"
                aria-pressed={preset === id}
                onClick={() => {
                  setPreset(id);
                }}
              >
                {label}
              </Button>
            ))}
          </div>
          {range.valid ? (
            <span className="pw-usage-filters__help">
              {rangeLabel(range.from, range.to, now)}
            </span>
          ) : null}
        </div>
        {preset === "custom" ? (
          <div className="pw-usage-filters__dates">
            <label>
              <span>开始日期</span>
              <input
                onChange={(event) => {
                  setCustomFrom(event.currentTarget.value);
                }}
                type="date"
                value={customFrom}
              />
            </label>
            <label>
              <span>结束日期</span>
              <input
                onChange={(event) => {
                  setCustomTo(event.currentTarget.value);
                }}
                type="date"
                value={customTo}
              />
            </label>
          </div>
        ) : null}
        {lockedService === undefined ? (
          <label>
            <span>AI 服务</span>
            <select
              onChange={(event) => {
                setServiceId(event.currentTarget.value);
              }}
              value={serviceId}
            >
              <option value="">全部 AI 服务</option>
              {serviceOptions.map((service) => (
                <option key={service.id} value={service.id}>
                  {service.label}
                </option>
              ))}
            </select>
          </label>
        ) : null}
        {isAdmin ? (
          <label>
            <span>用户</span>
            <select
              onChange={(event) => {
                setUserId(event.currentTarget.value);
              }}
              value={userId}
            >
              <option value="">全部用户</option>
              {(users.data?.items ?? []).map((user) => (
                <option key={user.id} value={user.id}>
                  {user.username}
                  {user.status === "deleted" ? "（已删除）" : ""}
                </option>
              ))}
            </select>
          </label>
        ) : null}
        <div className="pw-usage-filters__field">
          <label htmlFor={keySelectId}>API 密钥</label>
          <select
            aria-describedby={
              keyAvailabilityDescription === null
                ? undefined
                : keyAvailabilityDescriptionId
            }
            id={keySelectId}
            onChange={(event) => {
              setApiKeyId(event.currentTarget.value);
              const selected = availableKeys.find(
                (group) => group.group_id === event.currentTarget.value,
              );
              setSelectedKeyName(
                selected?.api_key_name ?? selected?.display_name ?? "",
              );
              setClearedKeyName(null);
            }}
            value={apiKeyId}
          >
            <option value="">全部 API 密钥</option>
            {apiKeyId !== "" &&
            !availableKeyIds.has(apiKeyId) &&
            !excludedKeys.some((key) => key.group_id === apiKeyId) ? (
              <option disabled value={apiKeyId}>
                {selectedKeyName}
              </option>
            ) : null}
            {availableKeys.map((group) => (
              <option key={group.group_id} value={group.group_id}>
                {keyDisplayName(group, isAdmin)}
              </option>
            ))}
            {excludedKeys.map((group) => (
              <option disabled key={group.group_id} value={group.group_id}>
                {keyDisplayName(group, isAdmin)}
              </option>
            ))}
          </select>
          {keyAvailabilityDescription === null ? null : (
            <span
              className="pw-usage-filters__help"
              id={keyAvailabilityDescriptionId}
              role={allKeyOptions.isError ? "status" : undefined}
            >
              {keyAvailabilityDescription}
            </span>
          )}
          {clearedKeyName !== null ? (
            <span className="pw-usage-filters__help" role="status">
              已清除 API 密钥筛选：「{clearedKeyName}
              」在当前服务、用户和时间范围内没有调用
            </span>
          ) : null}
        </div>
        {hasFilters ? (
          <Button
            className="pw-usage-filters__clear"
            variant="ghost"
            onClick={clearFilters}
          >
            清除筛选
          </Button>
        ) : null}
      </form>

      {!range.valid ? (
        <InlineAlert title="日期范围无效" tone="negative">
          {range.error}
        </InlineAlert>
      ) : report.isError || summary.isError ? (
        <InlineAlert title="无法获取用量数据" tone="negative">
          用量卡片与趋势暂时无法显示。请检查平台连接后重试。
          <Button onClick={retry} size="s">
            重试
          </Button>
        </InlineAlert>
      ) : report.isPending ? (
        <UsageLoading />
      ) : report.data.summary.requests === 0 ? (
        <EmptyState
          description="可以调整日期或筛选条件查看其他范围。"
          icon="usage"
          title="所选范围内没有调用"
          variant="search-no-results"
        />
      ) : (
        <>
          <dl className="pw-usage-range-summary" aria-label="所选范围汇总">
            <UsageMetric
              label="Requests"
              value={report.data.summary.requests}
            />
            <UsageMetric
              label="Input Tokens"
              value={report.data.summary.input_tokens}
            />
            <UsageMetric
              label="Output Tokens"
              value={report.data.summary.output_tokens}
            />
            <UsageMetric
              label="Total Tokens"
              value={report.data.summary.total_tokens}
            />
            <UsageMetric
              label="Error Requests"
              value={report.data.summary.error_requests}
            />
            <UsageMetric
              label="Error Rate"
              value={formatRate(report.data.summary.error_rate)}
            />
          </dl>
          {report.data.summary.partial_token_requests > 0 ? (
            <p className="pw-usage-filters__help">
              其中{" "}
              {report.data.summary.partial_token_requests.toLocaleString(
                "zh-CN",
              )}{" "}
              次调用异常中断，Token 按 Gateway 已确认的实际数量计入
            </p>
          ) : null}
          <div className="pw-usage-charts">
            <UsageChartPanel title="Requests 趋势">
              <Chart
                tickLabel={(_, category) => category}
                ariaLabel="Requests 趋势"
                mark="bar"
                valueFormat={{ unit: "次", compact: true }}
                categories={report.data.trend.map((point) =>
                  bucketLabel(
                    point.bucket_start,
                    report.data.bucket_width,
                    timeZone,
                  ),
                )}
                kind="ordinal"
                series={[
                  {
                    id: "requests",
                    label: "Requests",
                    values: report.data.trend.map((point) => point.requests),
                  },
                ]}
              />
            </UsageChartPanel>
            <UsageChartPanel title="Tokens 趋势">
              <Chart
                tickLabel={(_, category) => category}
                ariaLabel="Tokens 趋势"
                mark="bar"
                totalLabel="Total"
                valueFormat={{ unit: "Tokens", compact: true }}
                categories={report.data.trend.map((point) =>
                  bucketLabel(
                    point.bucket_start,
                    report.data.bucket_width,
                    timeZone,
                  ),
                )}
                kind="ordinal"
                series={[
                  {
                    id: "input-tokens",
                    label: "Input",
                    values: report.data.trend.map(
                      (point) => point.input_tokens,
                    ),
                  },
                  {
                    id: "output-tokens",
                    label: "Output",
                    values: report.data.trend.map(
                      (point) => point.output_tokens,
                    ),
                  },
                ]}
              />
            </UsageChartPanel>
          </div>
          <section className="pw-usage-details">
            <header>
              <div>
                <h2>明细</h2>
                <p>按维度查看请求、Token 与错误占比。</p>
              </div>
              <div
                className="pw-usage-segments"
                role="group"
                aria-label="分组方式"
              >
                {(
                  [
                    ["service", "按服务"],
                    ["user", "按用户"],
                    ["api_key", "按密钥"],
                  ] as const
                )
                  .filter(
                    ([id]) =>
                      (isAdmin || id !== "user") &&
                      (lockedService === undefined || id !== "service"),
                  )
                  .map(([id, label]) => (
                    <Button
                      key={id}
                      size="s"
                      aria-pressed={groupBy === id}
                      onClick={() => {
                        setGroupBy(id);
                      }}
                    >
                      {label}
                    </Button>
                  ))}
              </div>
            </header>
            <DataTable
              caption="用量明细"
              columns={usageColumns(
                groupBy,
                isAdmin,
                report.data.summary.total_tokens,
                drillDown,
              )}
              getRowKey={(row) => row.group_id ?? row.display_name}
              rows={[...report.data.details].sort(
                (a, b) => (b.total_tokens ?? -1) - (a.total_tokens ?? -1),
              )}
            />
          </section>
        </>
      )}
    </div>
  );
}

function UsageSummaryCards({
  loading,
  thisMonth,
  today,
}: {
  loading: boolean;
  thisMonth?: UsageTotals;
  today?: UsageTotals;
}) {
  return (
    <section
      className="pw-usage-summary"
      aria-busy={loading}
      aria-label="用量摘要"
    >
      <UsagePeriodCard label="今日" loading={loading} totals={today} />
      <UsagePeriodCard label="本月" loading={loading} totals={thisMonth} />
    </section>
  );
}

function UsagePeriodCard({
  label,
  loading,
  totals,
}: {
  label: string;
  loading: boolean;
  totals?: UsageTotals;
}) {
  const empty = totals === undefined || totals.requests === 0;
  return (
    <article className="pw-usage-period">
      <div className="pw-usage-period__heading">
        <h2>{label}</h2>
        {!loading && totals?.requests === 0 ? <span>暂无调用</span> : null}
      </div>
      <dl>
        <UsageMetric
          label="Requests"
          loading={loading}
          value={empty ? null : totals.requests}
        />
        <UsageMetric
          label="Input Tokens"
          loading={loading}
          value={empty ? null : totals.input_tokens}
        />
        <UsageMetric
          label="Output Tokens"
          loading={loading}
          value={empty ? null : totals.output_tokens}
        />
        <UsageMetric
          label="Total Tokens"
          loading={loading}
          value={empty ? null : totals.total_tokens}
        />
      </dl>
    </article>
  );
}

function UsageMetric({
  label,
  loading = false,
  value,
}: {
  label: string;
  loading?: boolean;
  value: number | string | null;
}) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>
        {loading ? (
          <span className="sk" />
        ) : (
          <span title={formatValue(value)} aria-label={formatValue(value)}>
            {compactValue(value)}
          </span>
        )}
      </dd>
    </div>
  );
}

function UsageChartPanel({
  children,
  title,
}: {
  children: ReactNode;
  title: string;
}) {
  return (
    <section className="pw-usage-chart-panel">
      <h2>{title}</h2>
      {children}
    </section>
  );
}

function UsageLoading() {
  return (
    <div
      aria-busy="true"
      aria-label="用量正在加载"
      className="pw-usage-loading"
    >
      <span className="sk" />
      <span className="sk" />
    </div>
  );
}

function usageColumns(
  groupBy: UsageGroupBy,
  isAdmin: boolean,
  total: number | null,
  onDrillDown: (row: UsageGroup) => void,
): TableColumn<UsageGroup>[] {
  return [
    {
      header: "名称",
      id: "dimension",
      render: (row) => {
        const name =
          groupBy === "api_key"
            ? (row.api_key_name ?? row.display_name)
            : row.display_name;
        return (
          <span className="pw-usage-dimension">
            <Button
              className="pw-usage-name"
              variant="ghost"
              size="s"
              title={name}
              disabled={row.group_id === null}
              onClick={() => {
                onDrillDown(row);
              }}
            >
              {name}
            </Button>
            {row.status === null ? null : <KeyStatus status={row.status} />}
            {row.is_deleted ? (
              <span>
                {groupBy === "api_key" ? "已从密钥列表移除" : "已删除"}
              </span>
            ) : null}
          </span>
        );
      },
    },
    ...(isAdmin && groupBy === "api_key"
      ? [
          {
            header: "所属用户",
            id: "owner",
            render: (row: UsageGroup) => ownerName(row),
          },
        ]
      : []),
    metricColumn("Requests", "requests", (row) => row.requests),
    metricColumn("Input Tokens", "input", (row) => row.input_tokens),
    metricColumn("Output Tokens", "output", (row) => row.output_tokens),
    metricColumn("Total Tokens", "total", (row) => row.total_tokens),
    metricColumn("占比", "share", (row) =>
      row.total_tokens === null || total === null || total === 0
        ? null
        : `${((row.total_tokens / total) * 100).toFixed(1)}%`,
    ),
    metricColumn("Error Requests", "errors", (row) => row.error_requests),
    metricColumn("Error Rate", "error-rate", (row) =>
      formatRate(row.error_rate),
    ),
  ];
}

function metricColumn(
  header: string,
  id: string,
  value: (row: UsageGroup) => number | string | null,
): TableColumn<UsageGroup> {
  return {
    header,
    id,
    numeric: true,
    render: (row) => formatValue(value(row)),
  };
}

function KeyStatus({ status }: { status: "Active" | "Expired" | "Revoked" }) {
  const presentation =
    status === "Active"
      ? { icon: "success" as const, text: "生效中", tone: "positive" as const }
      : status === "Expired"
        ? { icon: "notice" as const, text: "已过期", tone: "notice" as const }
        : { icon: "pause" as const, text: "已撤销", tone: "idle" as const };
  return <StatusBadge {...presentation} />;
}

function keyStatusText(status: "Active" | "Expired" | "Revoked") {
  return status === "Active"
    ? "生效中"
    : status === "Expired"
      ? "已过期"
      : "已撤销";
}

function usageKeyGroups(groups: readonly UsageGroup[] | undefined) {
  return (groups ?? []).filter(
    (group): group is UsageGroup & { group_id: string } =>
      group.group_id !== null,
  );
}

function ownerName(group: UsageGroup) {
  return `${group.owner_username ?? "—"}${group.owner_is_deleted ? "（已删除）" : ""}`;
}

function keyDisplayName(group: UsageGroup, isAdmin: boolean) {
  return `${group.api_key_name ?? group.display_name}${isAdmin ? ` · ${ownerName(group)}` : ""}${
    group.status === null ? "" : ` · ${keyStatusText(group.status)}`
  }${group.is_deleted ? " · 已从密钥列表移除" : ""}`;
}

function keyExclusionReason(serviceSelected: boolean, userSelected: boolean) {
  if (serviceSelected && userSelected) {
    return "不在所选 AI 服务或用户范围内";
  }
  if (serviceSelected) return "不在所选 AI 服务范围内";
  if (userSelected) return "不属于所选用户";
  return null;
}

function browserTimeZone() {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
}

function dateInputValue(value: Date) {
  const year = String(value.getFullYear());
  const month = String(value.getMonth() + 1).padStart(2, "0");
  const day = String(value.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function localMidnight(value: Date) {
  return new Date(value.getFullYear(), value.getMonth(), value.getDate());
}

function usageRange(
  now: Date,
  preset: DatePreset,
  customFrom: string,
  customTo: string,
) {
  let from: Date;
  let to: Date;
  if (preset === "custom") {
    from = new Date(`${customFrom}T00:00:00`);
    to = new Date(`${customTo}T00:00:00`);
    to.setDate(to.getDate() + 1);
  } else {
    from = localMidnight(now);
    to = new Date(now);
    if (preset === "7d") from.setDate(from.getDate() - 6);
    if (preset === "30d") from.setDate(from.getDate() - 29);
  }
  const duration = to.getTime() - from.getTime();
  const error = !Number.isFinite(duration)
    ? "请选择开始日期和结束日期。"
    : duration <= 0
      ? "结束日期早于开始日期，请重新选择。"
      : duration > 31 * 24 * 60 * 60 * 1_000
        ? "日期范围超过 31 天，请缩短范围。"
        : preset === "custom" &&
            (customFrom > dateInputValue(now) || customTo > dateInputValue(now))
          ? "日期不能晚于今天，请重新选择。"
          : null;
  // Invalid/cleared date inputs must not throw while disabled query keys are built.
  return {
    from: Number.isFinite(from.getTime()) ? from : localMidnight(now),
    to: Number.isFinite(to.getTime()) ? to : now,
    valid: error === null,
    error,
  };
}

function rangeLabel(from: Date, to: Date, now: Date) {
  const includesToday = to > localMidnight(now);
  const end = to > now ? now : new Date(to.getTime() - 1);
  const date = (value: Date) => dateInputValue(value).slice(5);
  const time = new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(now);
  return `${date(from)} 至 ${date(end)}${includesToday ? `（今日到 ${time}）` : ""} · ${to.getTime() - from.getTime() <= 86400000 ? "按小时" : "按天"}`;
}

function compactValue(value: number | string | null) {
  if (typeof value !== "number" || value < 1e6) return formatValue(value);
  return `${(value / (value >= 1e9 ? 1e9 : 1e6)).toFixed(1).replace(/\.0$/, "")}${value >= 1e9 ? "B" : "M"}`;
}

function bucketLabel(value: string, width: "hour" | "day", timeZone: string) {
  return new Intl.DateTimeFormat("zh-CN", {
    day: "2-digit",
    ...(width === "hour" ? { hour: "2-digit", hour12: false } : {}),
    month: "2-digit",
    timeZone,
  }).format(new Date(value));
}

function formatRate(value: number | null) {
  return value === null
    ? null
    : new Intl.NumberFormat("zh-CN", {
        maximumFractionDigits: 1,
        style: "percent",
      }).format(value);
}

function formatValue(value: number | string | null) {
  if (value === null) return "—";
  return typeof value === "number" ? value.toLocaleString("zh-CN") : value;
}
