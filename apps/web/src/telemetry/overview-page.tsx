import type { AiService } from "../api/ai-service-contracts";
import type { ModelInstance } from "../api/model-instance-contracts";
import { PRODUCT_NAME } from "../app/brand";
import { useAiServices } from "../ai-services/queries";
import {
  ServiceMetric,
  ServiceWindowMetric,
  useServiceListMetrics,
} from "../ai-services/service-metrics";
import { failureStageLabel } from "../local-models/presentation";
import { formatSummaryCount } from "./format-summary-count";
import { useEffect, useId, useRef, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router";

import type { ApiClient } from "../api/client";
import type { OverviewAttentionItem } from "../api/telemetry-contracts";
import {
  Button,
  Icon,
  InlineAlert,
  StatusBadge,
  MissingValue,
} from "../components";
import {
  instanceStatusPresentation,
  runtimeLabel,
} from "../model-instances/presentation";
import {
  buildPlatformStatus,
  unavailablePlatformStatus,
} from "../system/presentation";
import { useSystemQueries } from "../system/queries";
import { useUsageSummary } from "../usage/queries";
import { useModelInstances } from "../model-instances/queries";
import {
  useOverviewAttention,
  useOverviewTelemetry,
  useInstanceTelemetry,
} from "./queries";

export function OverviewPage({ apiClient }: { apiClient: ApiClient }) {
  const heading = useRef<HTMLHeadingElement>(null);
  const navigate = useNavigate();
  const location = useLocation();
  const [initializedUsername, setInitializedUsername] = useState<
    string | undefined
  >(() => {
    const state = location.state as { initializedUsername?: unknown } | null;
    return typeof state?.initializedUsername === "string"
      ? state.initializedUsername
      : undefined;
  });
  useEffect(() => {
    if (initializedUsername !== undefined) {
      // Consume the transient handoff so refreshing cannot replay completion.
      void navigate(location.pathname, { replace: true, state: null });
    }
  }, []);
  const initializationNotice =
    initializedUsername === undefined ? null : (
      <InlineAlert
        title={`系统已就绪。管理员 ${initializedUsername} 已创建，初始化入口已永久关闭。`}
        tone="positive"
      >
        <Button
          size="s"
          onClick={() => {
            setInitializedUsername(undefined);
          }}
        >
          知道了
        </Button>
      </InlineAlert>
    );
  const overview = useOverviewTelemetry(apiClient);
  const instances = useModelInstances(apiClient);
  const system = useSystemQueries(apiClient);
  const services = useAiServices(apiClient);

  useEffect(() => {
    document.title = `总览 · ${PRODUCT_NAME}`;
    return () => {
      document.title = PRODUCT_NAME;
    };
  }, []);
  // A completed query must not steal focus from navigation or an open dialog.
  useEffect(() => heading.current?.focus(), []);

  if (overview.isPending || instances.isPending) {
    return (
      <section
        aria-busy="true"
        aria-label="总览正在加载"
        className="pw-overview-page"
      >
        {initializationNotice}
        <span className="sk pw-overview-page__heading-skeleton" />
        <div className="pw-overview-grid">
          <span className="sk pw-overview-card__skeleton" />
          <span className="sk pw-overview-card__skeleton" />
          <span className="sk pw-overview-card__skeleton" />
          <span className="sk pw-overview-card__skeleton" />
        </div>
      </section>
    );
  }

  if (overview.data === undefined || instances.data === undefined) {
    return (
      <section className="pw-overview-page">
        {initializationNotice}
        <h1 ref={heading} tabIndex={-1}>
          总览
        </h1>
        <InlineAlert title="无法获取平台总览" tone="negative">
          实时状态无法显示。请检查平台连接后重试。
          <Button
            onClick={() => {
              void Promise.all([overview.refetch(), instances.refetch()]);
            }}
            size="s"
          >
            重试
          </Button>
        </InlineAlert>
      </section>
    );
  }

  if (instances.data.items.length === 0) {
    return (
      <section className="pw-overview-page pw-overview-page--empty">
        {initializationNotice}
        <h1 ref={heading} tabIndex={-1}>
          总览
        </h1>
        <div className="pw-overview-empty">
          <div>
            <h2>部署第一个模型</h2>
            <p>完成部署后，这里会显示资源、请求与 AI 服务的实时状态。</p>
          </div>
          <Button
            onClick={() => void navigate("/models/deploy")}
            variant="primary"
          >
            部署模型
          </Button>
        </div>
      </section>
    );
  }

  const values = overview.data.current;
  return (
    <section className="pw-overview-page">
      {initializationNotice}
      <header className="pw-overview-page__head">
        <div>
          <h1 ref={heading} tabIndex={-1}>
            总览
          </h1>
          <div className="pw-overview-page__subtitle">
            <p>平台资源与服务状态，每 5 秒更新。</p>
            <TelemetryFreshness
              sampledAt={overview.data.sampled_at}
              status={overview.data.telemetry_status}
            />
          </div>
        </div>
      </header>
      <SystemConclusion
        queries={system}
        available={values.services_available}
        total={values.services_total}
        services={services.data?.items}
      />
      {overview.data.telemetry_status === "available" ? null : (
        <p className="pw-overview-note">
          部分监控数据延迟或不可用，不影响 AI 服务调用。请稍后刷新。
        </p>
      )}
      <div className="pw-overview-grid">
        <OverviewCard
          label="AI 服务"
          to="/services"
          target="查看 AI 服务"
          sampledAt={overview.data.sampled_at}
          metrics={[
            ["可用", values.services_available],
            ["启动中", values.services_starting],
            ["异常", values.services_unavailable],
            ["总数", values.services_total],
          ]}
        />
        <OverviewCard
          label="GPU"
          to="/system?tab=gpu"
          target="查看 GPU"
          sampledAt={overview.data.sampled_at}
          metrics={[
            ["已分配", values.gpu_allocated],
            ["可用", values.gpu_available],
            ["总数", values.gpu_total],
          ]}
        />
        <OverviewCard
          label="请求"
          to="/services?view=monitor"
          target="查看监控"
          sampledAt={overview.data.sampled_at}
          metrics={[
            ["运行中", values.requests_running],
            ["等待中", values.requests_waiting],
            ["请求数（近 1 小时）", values.requests_1h, "", undefined, true],
            [
              "错误率（近 1 小时）",
              values.requests_1h === 0 || values.error_rate_1h === null
                ? null
                : values.error_rate_1h * 100,
              "%",
              values.requests_1h === 0 ? "近 1 小时无调用" : undefined,
            ],
          ]}
        />
        <TodayUsageCard apiClient={apiClient} />
      </div>
      <OverviewServices apiClient={apiClient} services={services} />
      <section
        className="pw-overview-panel"
        aria-labelledby="pw-overview-recent-title"
      >
        <h2 id="pw-overview-recent-title">
          模型实例 <small>最近更新的 5 个</small>
        </h2>
        <div className="pw-table-wrap">
          <table className="pw-table pw-overview-table">
            <caption className="pw-visually-hidden">模型实例</caption>
            <thead>
              <tr>
                <th scope="col">实例</th>
                <th scope="col">状态</th>
                <th
                  scope="col"
                  className="pw-overview-table__optional pw-table__numeric"
                >
                  GPU
                </th>
                <th scope="col" className="pw-overview-table__optional">
                  Runtime
                </th>
                <th
                  scope="col"
                  className="pw-overview-table__optional pw-table__numeric"
                >
                  当前请求
                </th>
              </tr>
            </thead>
            <tbody>
              {[...instances.data.items]
                .sort(
                  (a, b) => Date.parse(b.updated_at) - Date.parse(a.updated_at),
                )
                .slice(0, 5)
                .map((instance) => (
                  <OverviewInstance
                    key={instance.id}
                    instance={instance}
                    apiClient={apiClient}
                  />
                ))}
            </tbody>
          </table>
        </div>
        <Link className="pw-overview-panel__footer" to="/models">
          查看全部模型实例
        </Link>
      </section>
      <AttentionSection apiClient={apiClient} />
    </section>
  );
}

function AttentionSection({ apiClient }: { apiClient: ApiClient }) {
  const attention = useOverviewAttention(apiClient);

  return (
    <section
      aria-labelledby="pw-overview-attention-title"
      className="pw-overview-panel pw-overview-attention"
    >
      <h2 id="pw-overview-attention-title">需要处理</h2>
      {attention.isPending ? (
        <div
          aria-busy="true"
          aria-label="需要处理正在加载"
          className="sk pw-overview-attention__skeleton"
        />
      ) : attention.isError ? (
        <InlineAlert title="无法获取需要处理的事项" tone="notice">
          待处理事项暂时无法显示，不影响 AI 服务调用。请稍后重试。
          <Button size="s" onClick={() => void attention.refetch()}>
            重试
          </Button>
        </InlineAlert>
      ) : attention.data.items.length === 0 ? (
        <p className="pw-overview-panel__empty">暂无需要处理的事项</p>
      ) : (
        <ul>
          {attention.data.items.map((item) => (
            <li key={`${item.kind}-${item.target_id ?? "summary"}`}>
              <AttentionEntry item={item} />
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function AttentionEntry({ item }: { item: OverviewAttentionItem }) {
  const name = item.target_name ?? "";
  const instanceTo = `/models/instances/${item.target_id ?? ""}`;
  const entries = {
    service_unavailable: {
      title: `AI 服务 ${name} 不可用`,
      impact: `${item.detail === "no_backend" ? "未绑定后端实例" : "后端实例不可用"}，调用返回 503。可在服务设置中换绑运行中的实例，或修改实例配置后重试。`,
      action: "查看服务",
      to: `/services/${item.target_id ?? ""}`,
    },
    instance_failed: {
      title: `模型实例 ${name} ${instanceStatusPresentation("Failed").label}`,
      impact:
        "实例已停止提供推理，计算资源已释放。可查看日志、修改配置重试或删除。",
      action: "查看实例",
      to: instanceTo,
    },
    instance_unhealthy: {
      title: `模型实例 ${name} ${instanceStatusPresentation("Unhealthy").label}`,
      impact: "仍占用计算资源，当前无法正常提供推理。可查看日志或停止实例。",
      action: "查看实例",
      to: instanceTo,
    },
    gpu_unhealthy: {
      title: `${name} 异常`,
      impact: "不能用于新的部署。请查看设备状态并处理异常。",
      action: "查看 GPU",
      to: "/system?tab=gpu",
    },
    download_failed: {
      title: `下载任务 ${name} 下载失败`,
      impact: `${item.kind === "download_failed" && item.detail ? `在「${failureStageLabel(item.detail)}」阶段失败，` : "模型文件尚未就绪，"}可继续或重新下载。`,
      action: "查看下载任务",
      to: "/models?tab=local-models",
    },
    telemetry_interrupted: {
      title: "遥测采集中断",
      impact: `${item.latest_at ? `自 ${formatTime(item.latest_at)} 起` : "暂时"}未收到监控数据，请求指标、服务指标与实例当前请求暂停更新，不影响 AI 服务调用。请检查采集组件。`,
      action: "查看系统组件",
      to: "/system?tab=components",
    },
    unknown_requests: {
      title: `${item.count.toLocaleString("zh-CN")} 个调用在平台重启前未收到最终结果`,
      impact:
        "统计范围为最近 24 小时，这些调用的用量可能不完整。请检查系统组件。",
      action: "查看系统组件",
      to: "/system?tab=components",
    },
  };
  const entry = entries[item.kind];
  return (
    <Link
      className={`pw-overview-attention__entry pw-overview-attention__entry--${item.severity}`}
      to={entry.to}
    >
      <Icon name={item.severity === "negative" ? "error" : "notice"} size="s" />
      <span>
        <strong>{entry.title}</strong>
        <span className="pw-overview-attention__impact">{entry.impact}</span>
        {item.kind === "unknown_requests" && item.latest_at ? (
          <span className="pw-overview-note">
            最近一次：
            <time dateTime={item.latest_at}>
              {new Date(item.latest_at).toLocaleString("zh-CN", {
                day: "2-digit",
                hour: "2-digit",
                minute: "2-digit",
                month: "2-digit",
              })}
            </time>
          </span>
        ) : null}
      </span>
      <span className="pw-overview-attention__action">
        {entry.action}
        <Icon name="chevronRight" size="s" />
      </span>
    </Link>
  );
}

function formatTime(value: string) {
  return new Date(value).toLocaleTimeString("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
  });
}

function SystemConclusion({
  queries,
  available,
  total,
  services,
}: {
  queries: ReturnType<typeof useSystemQueries>;
  available: number | null;
  total: number | null;
  services: readonly AiService[] | undefined;
}) {
  const failed = queries.server.isError || queries.components.isError;
  if (!failed && (queries.server.isPending || queries.components.isPending)) {
    return (
      <div
        aria-busy="true"
        aria-label="系统结论正在加载"
        className="sk pw-overview-conclusion"
      />
    );
  }
  const status =
    failed || !queries.server.data || !queries.components.data
      ? unavailablePlatformStatus()
      : buildPlatformStatus(queries.server.data, queries.components.data);
  const issue = status.mostSevereIssue;
  if (available === 0 && total !== null && total > 0) {
    const allDisabled =
      services?.length === total &&
      services.every((service) => !service.enabled);
    return (
      <div className="pw-overview-conclusion pw-overview-conclusion--notice">
        <strong>当前没有可用的 AI 服务</strong>
        <span>
          {allDisabled
            ? `${String(total)} 个服务均已停用，调用将返回 503。启用服务后恢复。`
            : "服务正在启动或后端实例不可用，调用将返回 503。请查看服务状态并处理。"}
        </span>
        <Link to="/services">查看详情</Link>
      </div>
    );
  }
  return (
    <div
      className={`pw-overview-conclusion pw-overview-conclusion--${issue?.tone ?? status.tone}`}
    >
      <span>
        {available === null
          ? "AI 服务可用数量暂不可用"
          : `${String(available)} 个 AI 服务可用`}
      </span>
      <span>
        <Icon name={issue ? "notice" : status.icon} size="s" />
        {status.label === "正常"
          ? "系统运行正常"
          : (issue?.detail ?? status.detail)}
      </span>
      {status.label === "正常" ? null : <Link to="/system">查看详情</Link>}
    </div>
  );
}

function TodayUsageCard({ apiClient }: { apiClient: ApiClient }) {
  const timeZone = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  const usage = useUsageSummary(apiClient, timeZone);
  return (
    <OverviewCard
      label="今日用量"
      forceListLayout
      to="/usage"
      target="查看用量"
      loading={usage.isPending}
      sampledAt={usage.data?.data_updated_at ?? null}
      reason="用量暂不可用"
      metrics={[
        [
          "Requests",
          usage.isError ? null : (usage.data?.today.requests ?? null),
          "",
          undefined,
          true,
        ],
        [
          "Total Tokens",
          usage.isError
            ? null
            : usage.data?.today.requests === 0
              ? 0
              : (usage.data?.today.total_tokens ?? null),
          "",
          undefined,
          true,
        ],
        [
          "活跃用户",
          usage.isError ? null : (usage.data?.today_active_users ?? null),
        ],
      ]}
    />
  );
}

function OverviewMissing({ reason }: { reason: string }) {
  const reasonId = useId();
  return (
    <span className="pw-overview-missing" title={reason}>
      <span aria-label="暂无数据" aria-describedby={reasonId}>
        —
      </span>
      <span title={reason}>
        <Icon name="notice" size="xs" />
      </span>
      <span id={reasonId} className="pw-visually-hidden">
        {reason}
      </span>
    </span>
  );
}

function OverviewCard({
  label,
  metrics,
  to,
  target,
  sampledAt,
  reason = "监控数据暂不可用",
  loading = false,
  forceListLayout = false,
}: {
  label: string;
  metrics: readonly (readonly [
    string,
    number | null,
    string?,
    string?,
    boolean?,
  ])[];
  to: string;
  target: string;
  sampledAt: string | null;
  reason?: string;
  loading?: boolean;
  forceListLayout?: boolean;
}) {
  const displayedMetrics = metrics.map(
    ([metricLabel, value, unit = "", specificReason, compact]) => {
      const fullValue =
        value === null ? null : `${value.toLocaleString("zh-CN")}${unit}`;
      const text =
        value !== null && compact ? formatSummaryCount(value) : fullValue;
      return {
        metricLabel,
        specificReason,
        text,
        fullValue:
          compact || (text !== null && text.length > 6)
            ? (fullValue ?? undefined)
            : undefined,
      };
    },
  );
  const listLayout =
    forceListLayout ||
    displayedMetrics.some(({ text }) => text !== null && text.length > 6);
  return (
    <section className="pw-overview-card">
      <Link to={to} aria-label={`${label} · ${target}`}>
        <h2>
          {label}
          <Icon name="chevronRight" size="s" />
        </h2>
        {loading ? (
          <div
            aria-busy="true"
            aria-label={`${label}正在加载`}
            className="sk pw-overview-card__skeleton"
          />
        ) : (
          <dl
            className={
              listLayout ? "pw-overview-card__metrics--list" : undefined
            }
          >
            {displayedMetrics.map(
              ({ metricLabel, text, specificReason, fullValue }) => (
                <div key={metricLabel}>
                  <dt>{metricLabel}</dt>
                  <dd>
                    {text === null ? (
                      <OverviewMissing
                        reason={
                          specificReason ??
                          `${reason}；${sampledAt ? `最后更新 ${formatTime(sampledAt)}` : "暂无更新时间"}`
                        }
                      />
                    ) : fullValue !== undefined ? (
                      <span title={fullValue} aria-label={fullValue}>
                        {text}
                      </span>
                    ) : (
                      text
                    )}
                  </dd>
                </div>
              ),
            )}
          </dl>
        )}
      </Link>
    </section>
  );
}

function OverviewServices({
  apiClient,
  services,
}: {
  apiClient: ApiClient;
  services: ReturnType<typeof useAiServices>;
}) {
  const rows = services.data?.items ?? [];
  const { telemetryByServiceId, telemetryByInstanceId } = useServiceListMetrics(
    apiClient,
    rows,
  );
  const navigate = useNavigate();
  const order = { Unavailable: 0, Degraded: 1, Available: 2, Disabled: 3 };
  const presentation = {
    Unavailable: { label: "不可用", tone: "negative" },
    Degraded: { label: "启动中", tone: "notice" },
    Available: { label: "可用", tone: "positive" },
    Disabled: { label: "已停用", tone: "idle" },
  };
  return (
    <section
      className="pw-overview-panel"
      aria-labelledby="pw-overview-services-title"
    >
      <h2 id="pw-overview-services-title">AI 服务</h2>
      {services.isError ? (
        <InlineAlert title="无法获取 AI 服务" tone="notice">
          服务列表无法显示。请稍后重试。
          <Button size="s" onClick={() => void services.refetch()}>
            重试
          </Button>
        </InlineAlert>
      ) : (
        <div
          className="pw-table-wrap"
          aria-busy={services.isPending}
          aria-label={services.isPending ? "AI 服务正在加载" : undefined}
        >
          <table className="pw-table pw-overview-table">
            <caption className="pw-visually-hidden">AI 服务</caption>
            <thead>
              <tr>
                <th scope="col">服务</th>
                {[
                  ["TTFT P95", "近 1 小时", false],
                  ["输出 TPS", "当前", true],
                  ["错误率", "近 1 小时", false],
                  ["并发 / 排队", "当前", true],
                ].map(([label, window, optional]) => (
                  <th
                    key={String(label)}
                    scope="col"
                    className={`pw-table__numeric ${optional ? "pw-overview-table__optional" : ""}`}
                  >
                    {label}
                    <span className="pw-overview-table__window">{window}</span>
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {services.isPending
                ? Array.from({ length: 6 }, (_, i) => (
                    <tr key={i} aria-hidden="true">
                      {Array.from({ length: 5 }, (_, j) => (
                        <td key={j}>
                          <span className="sk pw-table__skeleton" />
                        </td>
                      ))}
                    </tr>
                  ))
                : [...rows]
                    .sort(
                      (a, b) =>
                        order[a.runtime_status] - order[b.runtime_status] ||
                        a.name.localeCompare(b.name, "zh-CN"),
                    )
                    .map((service) => {
                      const status = presentation[service.runtime_status];
                      const inactive =
                        service.runtime_status === "Disabled" ||
                        service.primary_instance_status !== "Running";
                      const telemetry = telemetryByServiceId.get(service.id);
                      const reason = ["Unavailable", "Degraded"].includes(
                        service.runtime_status,
                      )
                        ? ` · 后端${service.primary_instance_status === null ? "实例缺失" : `实例${instanceStatusPresentation(service.primary_instance_status).label}`}`
                        : "";
                      return (
                        <tr
                          key={service.id}
                          className="pw-overview-table__row"
                          onClick={(event) => {
                            if (!(event.target as HTMLElement).closest("a"))
                              void navigate(`/services/${service.id}`);
                          }}
                        >
                          <td>
                            <Link
                              className="pw-overview-table__link"
                              to={`/services/${service.id}`}
                              aria-label={`${service.name} · 查看服务详情`}
                            >
                              <span className="pw-overview-table__name">
                                <span
                                  className={`pw-service-signal pw-service-signal--${status.tone}`}
                                  aria-hidden="true"
                                />
                                <strong title={service.name}>
                                  {service.name}
                                </strong>
                              </span>
                              <span className="pw-overview-table__status">
                                {status.label}
                                {reason}
                              </span>
                            </Link>
                          </td>
                          <td className="pw-table__numeric">
                            {inactive ? (
                              "—"
                            ) : (
                              <ServiceWindowMetric
                                telemetry={telemetry}
                                metric="ttft_p95_ms"
                              />
                            )}
                          </td>
                          <td className="pw-table__numeric pw-overview-table__optional">
                            {inactive ? (
                              "—"
                            ) : (
                              <ServiceMetric
                                value={telemetry?.current.output_tps_avg}
                                digits={1}
                              />
                            )}
                          </td>
                          <td className="pw-table__numeric">
                            {inactive ? (
                              "—"
                            ) : (
                              <ServiceWindowMetric
                                telemetry={telemetry}
                                metric="error_rate"
                              />
                            )}
                          </td>
                          <td className="pw-table__numeric pw-overview-table__optional">
                            {inactive ? (
                              "—"
                            ) : (
                              <>
                                <ServiceMetric
                                  value={telemetry?.current.inflight_last}
                                />{" "}
                                /{" "}
                                <ServiceMetric
                                  value={
                                    telemetryByInstanceId.get(
                                      service.primary_instance_id,
                                    )?.current.waiting
                                  }
                                  reason="后端遥测未采集"
                                />
                              </>
                            )}
                          </td>
                        </tr>
                      );
                    })}
            </tbody>
          </table>
          {!services.isPending && rows.length === 0 ? (
            <p className="pw-overview-panel__empty">暂无 AI 服务</p>
          ) : null}
        </div>
      )}
      <Link className="pw-overview-panel__footer" to="/services">
        查看全部 AI 服务
      </Link>
    </section>
  );
}

function OverviewInstance({
  apiClient,
  instance,
}: {
  apiClient: ApiClient;
  instance: ModelInstance;
}) {
  const telemetry = useInstanceTelemetry(apiClient, instance.id);
  const navigate = useNavigate();
  const status = instanceStatusPresentation(instance.status);
  const to = `/models/instances/${instance.id}`;
  return (
    <tr
      className="pw-overview-table__row"
      onClick={(event) => {
        if (!(event.target as HTMLElement).closest("a")) void navigate(to);
      }}
    >
      <td>
        <Link
          className="pw-overview-table__link"
          to={to}
          aria-label={`${instance.name} · 查看实例详情`}
        >
          <strong title={instance.name}>{instance.name}</strong>
        </Link>
      </td>
      <td>
        <StatusBadge {...status} text={status.label} />
      </td>
      <td className="pw-table__numeric pw-overview-table__optional">
        {instance.gpu_uuids.length} 张
      </td>
      <td className="pw-overview-table__optional">
        {runtimeLabel(instance.runtime_type)}
      </td>
      <td className="pw-table__numeric pw-overview-table__optional">
        {instance.status !== "Running" ? (
          "—"
        ) : telemetry.data?.current.running == null ? (
          <MissingValue
            kind="unavailable"
            reason={`当前请求暂不可用；${telemetry.data?.sampled_at ? `最后更新 ${formatTime(telemetry.data.sampled_at)}` : "暂无更新时间"}`}
          />
        ) : (
          telemetry.data.current.running.toLocaleString("zh-CN")
        )}
      </td>
    </tr>
  );
}

function TelemetryFreshness({
  sampledAt,
  status,
}: {
  sampledAt: string | null;
  status: "available" | "delayed" | "unavailable";
}) {
  const statusText =
    status === "available"
      ? "实时"
      : status === "delayed"
        ? "数据延迟"
        : "数据不可用";
  return (
    <span
      className={`pw-telemetry-freshness pw-telemetry-freshness--${status}`}
    >
      <span>{statusText}</span>
      {sampledAt === null ? null : (
        <time dateTime={sampledAt}>
          {new Date(sampledAt).toLocaleTimeString("zh-CN", {
            hour: "2-digit",
            minute: "2-digit",
            second: "2-digit",
          })}
        </time>
      )}
    </span>
  );
}
