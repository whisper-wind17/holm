import { HelpButton } from "../help/help-center";
import { HELP_TOPICS } from "../help/content";
import { useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Link, useLocation, useNavigate, useParams } from "react-router";

import { ApiError, type ApiClient } from "../api/client";
import type { ModelInstanceEvent } from "../api/model-instance-contracts";
import {
  Button,
  DataTable,
  InlineAlert,
  MissingValue,
  TruncatedValue,
  Tabs,
  type TableColumn,
} from "../components";
import { formatBytes } from "../system/system-page";
import { InstanceActions } from "./instance-actions";
import { InstanceConfiguration } from "./instance-detail-config";
import {
  DeploymentProgress,
  InstanceStatusBanner,
  isFreshDeployment,
} from "./instance-detail-status";
import { EditRetryDialog } from "./edit-retry-dialog";
import {
  InstanceLogs,
  type LogLevelFilter,
  type LogLevelRequest,
} from "./instance-logs";
import {
  instanceFailurePresentation,
  instanceStatusPresentation,
  runtimeLabel,
} from "./presentation";
import { useModelInstance } from "./queries";
import { useGpuTelemetry, useInstanceTelemetry } from "../telemetry/queries";
import type { ModelInstance } from "../api/model-instance-contracts";
import type { GpuDevice } from "../api/system-contracts";
import type { AiService } from "../api/ai-service-contracts";
import { InstanceMonitor } from "../telemetry/instance-monitor";

export function ModelInstanceDetailPage({
  apiClient,
}: {
  apiClient: ApiClient;
}) {
  const heading = useRef<HTMLHeadingElement>(null);
  const location = useLocation();
  const navigate = useNavigate();
  const { instanceId = "" } = useParams();
  const instance = useModelInstance(apiClient, instanceId);
  const artifacts = useQuery({
    queryFn: () => apiClient.getModelArtifacts(),
    queryKey: ["model-instance-detail", "artifacts"],
  });
  const server = useQuery({
    queryFn: () => apiClient.getSystemServer(),
    queryKey: ["model-instance-detail", "server"],
  });
  const services = useQuery({
    queryFn: () => apiClient.getAiServices(),
    queryKey: ["ai-services"],
  });
  const failureEvents = useQuery({
    enabled: instance.data !== undefined,
    refetchInterval: 5_000,
    staleTime: 5_000,
    queryFn: () => apiClient.getModelInstanceEvents(instanceId, undefined, 20),
    queryKey: ["model-instance-events", instanceId, undefined],
  });
  const [commandError, setCommandError] = useState<ApiError>();
  // banner and progress buttons jump to a tab. Tabs only take a default
  // value, so each jump bumps the nonce to remount it on the requested tab.
  const [jump, setJump] = useState<{
    nonce: number;
    tab: string;
  }>();
  const [levelRequest, setLevelRequest] = useState<LogLevelRequest>();

  useEffect(() => heading.current?.focus(), [instance.data?.id]);

  const artifact = artifacts.data?.items.find(
    ({ id }) => id === instance.data?.artifact_id,
  );
  const gpuByUuid = useMemo(() => {
    const devices =
      server.data?.agent.status === "available"
        ? server.data.agent.gpus.devices
        : [];
    return new Map(devices.map((gpu) => [gpu.uuid, gpu]));
  }, [server.data]);
  const gpuDevices =
    server.data?.agent.status === "available"
      ? server.data.agent.gpus.devices
      : [];

  if (instance.isPending) {
    return (
      <section
        aria-busy="true"
        aria-label="模型实例详情正在加载"
        className="pw-instance-detail"
      >
        <span className="sk pw-instance-detail__skeleton" />
        <span className="sk pw-instance-detail__skeleton" />
      </section>
    );
  }
  if (instance.data === undefined) {
    return (
      <section className="pw-instance-detail">
        <Button
          onClick={() => void navigate("/models")}
          size="s"
          variant="ghost"
        >
          返回模型实例
        </Button>
        <InlineAlert title="无法获取模型实例详情" tone="negative">
          详情无法显示。请确认模型实例仍然存在，然后从列表重新进入。
        </InlineAlert>
      </section>
    );
  }

  const item = instance.data;
  const status = instanceStatusPresentation(item.status);
  const failure = instanceFailurePresentation(item.error_code);
  const failedAt = failureEvents.data?.items
    .filter(({ to_status }) => to_status === "Failed")
    .sort(
      (left, right) =>
        Date.parse(right.occurred_at) - Date.parse(left.occurred_at),
    )[0]?.occurred_at;
  const selectedResources = selectedGpuResources(
    item.gpu_uuids,
    server.isPending,
    server.isError ||
      server.data?.agent.status !== "available" ||
      server.data.agent.gpus.discovery_state !== "ready",
    gpuByUuid,
  );
  const gpuLabels = waitingGpuLabels(item.gpu_uuids, gpuByUuid);
  const modelName = artifact?.display_name ?? item.internal_model_name;
  function showLogs(level: LogLevelFilter) {
    setLevelRequest({ level });
    setJump((previous) => ({ nonce: (previous?.nonce ?? 0) + 1, tab: "logs" }));
    void navigate({ hash: "#logs" });
  }
  const failureEventTime = failureEvents.isPending
    ? "事件时间正在加载"
    : failureEvents.isError || failedAt === undefined
      ? "事件时间暂不可用"
      : new Date(failedAt).toLocaleString("zh-CN", { hour12: false });
  return (
    <article className="pw-instance-detail">
      <nav aria-label="面包屑" className="pw-instance-detail__breadcrumb">
        <Link to="/models">模型实例</Link>
        <span aria-hidden="true">/</span>
        <span aria-current="page">{item.name}</span>
      </nav>
      <header className="pw-instance-detail__head">
        <div className="pw-instance-detail__identity">
          <div className="pw-instance-detail__title">
            <Signal
              label={status.label}
              tone={
                item.status === "WaitingForResource"
                  ? "idle"
                  : status.tone === "info"
                    ? "notice"
                    : status.tone
              }
            />
            <h1 ref={heading} tabIndex={-1}>
              {item.name}
            </h1>
            <span
              className="pw-instance-detail__status"
              data-tone={
                item.status === "WaitingForResource"
                  ? "idle"
                  : status.tone === "info"
                    ? "notice"
                    : status.tone
              }
            >
              {status.label}
            </span>
          </div>
          <p>
            {artifact?.display_name ?? item.internal_model_name} ·{" "}
            {runtimeLabel(item.runtime_type)} · {item.gpu_uuids.length} ×{" "}
            {item.gpu_uuids.length > 0 &&
            item.gpu_uuids.every((uuid) => gpuByUuid.has(uuid)) ? (
              [
                ...new Set(
                  item.gpu_uuids.map((uuid) => gpuByUuid.get(uuid)?.model),
                ),
              ].join("、")
            ) : (
              <MissingValue kind="unavailable" reason="GPU 型号暂不可用" />
            )}
            {item.status === "Stopped" ? " · GPU 已释放" : ""}
          </p>
        </div>
        <InstanceActions
          apiClient={apiClient}
          mode="detail"
          bindingState={
            services.isError
              ? "error"
              : services.data === undefined
                ? "loading"
                : "ready"
          }
          boundServices={(services.data?.items ?? []).filter(
            ({ primary_instance_id }) => primary_instance_id === item.id,
          )}
          gpuDevices={gpuDevices}
          instance={item}
          modelName={modelName}
          onError={setCommandError}
          onRemoved={() => void navigate("/models")}
        />
      </header>
      {commandError === undefined ? null : (
        <InlineAlert title="操作未完成" tone="negative">
          {commandError.status === 409
            ? "配置已更新，请刷新后重新提交；未提交的选择仍会保留。"
            : (commandError.responseMessage ??
              "模型实例状态未改变。请刷新详情后重试。")}
        </InlineAlert>
      )}
      {instance.realtimeStatus === "polling" ? (
        <InlineAlert title="实时连接已中断" tone="notice">
          已降级为每 5 秒轮询，数据可能延迟；连接恢复后会自动回到实时更新。
        </InlineAlert>
      ) : instance.realtimeStatus === "connecting" ? (
        <InlineAlert title="正在连接实时更新" tone="info">
          当前数据可能延迟，连接建立后会自动更新。
        </InlineAlert>
      ) : null}
      {services.isError ? (
        <InlineAlert title="无法确认服务绑定" tone="negative">
          停止、重启和删除暂不可用，以免影响未知的 AI 服务。请恢复连接后重试。
        </InlineAlert>
      ) : services.isFetching && services.data !== undefined ? (
        <InlineAlert title="服务绑定正在更新" tone="info">
          当前数据仍可查看，危险操作会在更新完成后恢复。
        </InlineAlert>
      ) : null}
      <InstanceStatusBanner
        gpuLabels={gpuLabels}
        instance={item}
        onViewErrorLogs={() => {
          showLogs("ERROR");
        }}
      />
      {item.status === "Failed" ? (
        <InlineAlert title="模型实例运行失败" tone="negative">
          <HelpButton topic={HELP_TOPICS.troubleshooting.id}>
            查看故障排查帮助
          </HelpButton>
          <dl className="pw-instance-detail__facts">
            <Fact label="失败阶段" value={failure.stage} />
            <Fact label="原因" value={failure.reason} />
            <Fact label="已选资源" value={selectedResources} />
            <Fact label="建议" value={failure.advice} />
            <Fact label="事件时间" value={failureEventTime} />
          </dl>
          <EditRetryDialog
            gpuText={selectedResources}
            instance={item}
            modelName={modelName}
            variant="primary"
          />
          <Button
            onClick={() => {
              showLogs("ALL");
            }}
            size="s"
            variant="ghost"
          >
            查看详细日志
          </Button>
        </InlineAlert>
      ) : null}
      <Tabs
        aria-label="模型实例详情"
        defaultValue={
          jump?.tab ??
          (location.hash === "#logs"
            ? "logs"
            : location.hash === "#events"
              ? "events"
              : location.hash === "#configuration"
                ? "configuration"
                : "overview")
        }
        key={`${location.hash}:${String(jump?.nonce ?? 0)}`}
        items={[
          {
            content: (
              <div className="pw-instance-overview">
                <div className="pw-instance-overview__main">
                  {failureEvents.data !== undefined &&
                  isFreshDeployment(item, failureEvents.data.items) ? (
                    <DeploymentProgress
                      apiClient={apiClient}
                      events={failureEvents.data.items}
                      gpuLabels={gpuLabels}
                      instance={item}
                      onViewLogs={() => {
                        showLogs("ALL");
                      }}
                    />
                  ) : null}
                  <RunningSummary apiClient={apiClient} instance={item} />
                  <OverviewGpus
                    apiClient={apiClient}
                    instance={item}
                    gpuByUuid={gpuByUuid}
                  />
                  <section
                    aria-labelledby="instance-recent-events"
                    className="pw-instance-overview__panel"
                  >
                    <div className="pw-instance-overview__panel-head">
                      <h2 id="instance-recent-events">近期事件</h2>
                      <Link to="#events">查看全部</Link>
                    </div>
                    {failureEvents.isPending ? (
                      <p role="status">事件正在加载</p>
                    ) : failureEvents.isError ? (
                      <MissingValue
                        kind="unavailable"
                        reason={
                          failureEvents.error instanceof ApiError &&
                          failureEvents.error.status === 403
                            ? "没有查看事件的权限"
                            : "近期事件暂不可用，请稍后重试"
                        }
                      />
                    ) : failureEvents.data.items.length === 0 ? (
                      <p>暂无事件。</p>
                    ) : (
                      <ol className="pw-instance-recent-events">
                        {[...failureEvents.data.items]
                          .sort(
                            (a, b) =>
                              Date.parse(b.occurred_at) -
                              Date.parse(a.occurred_at),
                          )
                          .slice(0, 3)
                          .map((event) => (
                            <li key={event.id}>
                              <time dateTime={event.occurred_at}>
                                {new Date(event.occurred_at).toLocaleString(
                                  "zh-CN",
                                  { hour12: false },
                                )}
                              </time>
                              <div>
                                {EVENT_LABELS[event.event_type]}
                                <small>
                                  {ACTOR_LABELS[event.actor]}
                                  {event.to_status === null
                                    ? ""
                                    : ` · ${instanceStatusPresentation(event.to_status).label}`}
                                </small>
                              </div>
                            </li>
                          ))}
                      </ol>
                    )}
                  </section>
                </div>
                <div className="pw-instance-overview__side">
                  <section
                    aria-labelledby="instance-services"
                    className="pw-instance-overview__panel"
                  >
                    <h2 id="instance-services">关联服务</h2>
                    {services.isPending ? (
                      <p role="status">服务正在加载</p>
                    ) : services.isError ? (
                      <MissingValue
                        kind="unavailable"
                        reason="关联服务暂不可用，请恢复连接后重试"
                      />
                    ) : (
                      <RelatedServices
                        services={services.data.items.filter(
                          ({ primary_instance_id }) =>
                            primary_instance_id === item.id,
                        )}
                      />
                    )}
                  </section>
                  <section
                    aria-labelledby="instance-information"
                    className="pw-instance-overview__panel"
                  >
                    <h2 id="instance-information">基本信息</h2>
                    <dl className="pw-instance-overview__information">
                      <Fact
                        label="模型"
                        value={
                          artifact?.display_name ?? item.internal_model_name
                        }
                      />
                      <Fact
                        label="本地模型大小"
                        value={
                          artifact === undefined ? (
                            <MissingValue
                              kind="unavailable"
                              reason="本地模型大小暂不可用"
                            />
                          ) : (
                            formatBytes(artifact.size_bytes)
                          )
                        }
                      />
                      <Fact
                        label="Runtime"
                        value={runtimeLabel(item.runtime_type)}
                      />
                      <Fact
                        label="Tensor Parallel"
                        value={String(item.tensor_parallel_size)}
                      />
                      <Fact
                        label="上下文长度"
                        value={item.context_length.toLocaleString("zh-CN")}
                      />
                      <Fact
                        label="实例 ID"
                        value={
                          <TruncatedValue
                            value={item.id}
                            copyLabel="复制实例 ID"
                          />
                        }
                      />
                    </dl>
                    <Link to="#configuration">完整配置</Link>
                  </section>
                </div>
              </div>
            ),
            label: "概览",
            value: "overview",
          },
          {
            content: (
              <InstanceMonitor
                apiClient={apiClient}
                gpuByUuid={gpuByUuid}
                gpuUuids={item.gpu_uuids}
                instanceId={item.id}
              />
            ),
            label: "监控",
            value: "monitoring",
          },
          {
            content: (
              <InstanceConfiguration
                apiClient={apiClient}
                gpuByUuid={gpuByUuid}
                instance={item}
                modelId={artifact?.model_id}
              />
            ),
            label: "配置",
            value: "configuration",
          },
          {
            content: (
              <InstanceLogs
                apiClient={apiClient}
                instanceId={item.id}
                levelRequest={levelRequest}
              />
            ),
            label: "日志",
            value: "logs",
          },
          {
            content: (
              <InstanceEvents apiClient={apiClient} instanceId={item.id} />
            ),
            label: "事件",
            value: "events",
          },
        ]}
      />
    </article>
  );
}

function Signal({
  label,
  tone,
}: {
  label: string;
  tone: "positive" | "notice" | "negative" | "idle";
}) {
  return (
    <span
      className={`pw-service-signal pw-service-signal--${tone}`}
      role="img"
      aria-label={label}
      title={label}
    />
  );
}

function RelatedServices({ services }: { services: readonly AiService[] }) {
  if (services.length === 0) return <p>暂无关联服务。</p>;
  const presentations = {
    Available: { label: "可用", tone: "positive" },
    Degraded: { label: "启动中", tone: "notice" },
    Unavailable: { label: "不可用", tone: "negative" },
    Disabled: { label: "已停用", tone: "idle" },
  } as const;
  return (
    <ul className="pw-instance-related-services">
      {services.map((service) => {
        const status = presentations[service.runtime_status];
        return (
          <li key={service.id} className="pw-service-name">
            <Signal {...status} />
            <div>
              <Link to={`/services/${service.id}`}>{service.name}</Link>
              <p>
                {status.label}
                {service.description ? ` · ${service.description}` : ""}
              </p>
            </div>
          </li>
        );
      })}
    </ul>
  );
}

function RunningSummary({
  apiClient,
  instance,
}: {
  apiClient: ApiClient;
  instance: ModelInstance;
}) {
  return (
    <section
      aria-labelledby="instance-summary"
      className="pw-instance-overview__panel"
    >
      <h2 id="instance-summary">运行摘要</h2>
      {instance.status === "Running" ? (
        <RunningMetrics apiClient={apiClient} instanceId={instance.id} />
      ) : (
        <p className="pw-instance-overview__empty">
          实例未运行，没有实时指标。启动后显示当前请求、等待请求、TTFT P95、输出
          TPS、错误率与 KV Cache 使用率。
        </p>
      )}
    </section>
  );
}

function RunningMetrics({
  apiClient,
  instanceId,
}: {
  apiClient: ApiClient;
  instanceId: string;
}) {
  const telemetry = useInstanceTelemetry(apiClient, instanceId);
  const data = telemetry.data;
  const current = data?.current;
  const window = data?.window_1h;
  const unavailable =
    telemetry.isError || data?.telemetry_status === "unavailable";
  const reason = telemetry.isPending
    ? "遥测正在加载"
    : telemetry.error instanceof ApiError && telemetry.error.status === 403
      ? "没有查看遥测的权限"
      : "遥测暂不可用，请稍后重试";
  const unsupportedErrors = current?.unsupported.includes(
    "runtime_errors_total",
  );
  const windowReason =
    window?.requests === 0 ? "近 1 小时无请求" : "近 1 小时数据暂不可用";
  const windowValue = (value: number | null | undefined, unit: string) =>
    metricValue(
      unavailable || window?.requests === 0 ? null : value,
      unit,
      unavailable ? reason : windowReason,
    );
  return (
    <>
      {telemetry.isPending ? (
        <p role="status">遥测正在加载</p>
      ) : telemetry.isError || data?.telemetry_status !== "available" ? (
        <p className="pw-instance-overview__note">
          {data?.telemetry_status === "delayed" && !telemetry.isError
            ? "监控数据延迟"
            : reason}
          {data?.sampled_at
            ? ` · 最后更新：${new Date(data.sampled_at).toLocaleString("zh-CN", { hour12: false })}`
            : ""}
        </p>
      ) : null}
      <dl className="pw-instance-summary-metrics">
        <Fact
          label="当前请求"
          value={metricValue(
            unavailable ||
              current?.unsupported.includes("runtime_requests_running")
              ? null
              : current?.running,
            "",
            reason,
          )}
        />
        <Fact
          label="等待请求"
          value={metricValue(
            unavailable ||
              current?.unsupported.includes("runtime_requests_waiting")
              ? null
              : current?.waiting,
            "",
            reason,
          )}
        />
        <Fact
          label="TTFT P95（近 1 小时）"
          value={
            current?.unsupported.includes("runtime_ttft_seconds") ? (
              <MissingValue
                kind="not-applicable"
                reason="该 Runtime 不提供 TTFT"
              />
            ) : (
              windowValue(window?.ttft_p95_ms, "ms")
            )
          }
        />
        <Fact
          label="输出 TPS"
          value={metricValue(
            unavailable ||
              current?.unsupported.includes("runtime_output_tokens_total")
              ? null
              : current?.output_tps,
            "tokens/s",
            reason,
          )}
        />
        <Fact
          label="错误率（近 1 小时）"
          value={
            unsupportedErrors ? (
              <MissingValue
                kind="not-applicable"
                reason="该 Runtime 不提供错误计数"
              />
            ) : (
              windowValue(
                window?.error_rate == null ? null : window.error_rate * 100,
                "%",
              )
            )
          }
        />
        <Fact
          label="KV Cache 使用率"
          value={metricValue(
            unavailable ||
              current?.unsupported.includes("runtime_kv_cache_usage_ratio") ||
              current?.kv_cache_usage_ratio == null
              ? null
              : current.kv_cache_usage_ratio * 100,
            "%",
            reason,
          )}
        />
      </dl>
      {unsupportedErrors ? (
        <p className="pw-instance-overview__note">该 Runtime 不提供错误计数</p>
      ) : null}
      {window?.started_at &&
      data?.sampled_at &&
      Date.parse(data.sampled_at) - Date.parse(window.started_at) <
        3_600_000 ? (
        <p className="pw-instance-overview__note">
          TTFT P95 与错误率：遥测不足 1 小时，起算于{" "}
          {new Date(window.started_at).toLocaleString("zh-CN", {
            hour12: false,
          })}
        </p>
      ) : null}
    </>
  );
}

function metricValue(
  value: number | null | undefined,
  unit: string,
  reason = "监控数据暂不可用",
) {
  return value == null ? (
    <MissingValue kind="unavailable" reason={reason} />
  ) : (
    <>
      {value.toLocaleString("zh-CN", { maximumFractionDigits: 1 })}
      {unit ? <small> {unit}</small> : null}
    </>
  );
}

function OverviewGpus({
  apiClient,
  instance,
  gpuByUuid,
}: {
  apiClient: ApiClient;
  instance: ModelInstance;
  gpuByUuid: ReadonlyMap<string, GpuDevice>;
}) {
  const telemetry = useGpuTelemetry(apiClient);
  return (
    <section
      aria-labelledby="instance-gpus"
      className="pw-instance-overview__panel"
    >
      <h2 id="instance-gpus">GPU</h2>
      <div className="pw-instance-gpu-cards">
        {instance.gpu_uuids.map((uuid) => {
          const gpu = gpuByUuid.get(uuid);
          const data = telemetry.data?.items.find(
            (entry) => entry.uuid === uuid,
          );
          const current =
            !telemetry.isError && data?.telemetry_status !== "unavailable"
              ? data?.current
              : undefined;
          const memoryRatio =
            current?.memory_used_bytes != null &&
            gpu !== undefined &&
            gpu.memory_bytes > 0
              ? (current.memory_used_bytes / gpu.memory_bytes) * 100
              : undefined;
          return (
            <div className="pw-instance-gpu-card" key={uuid}>
              <strong>
                {gpu === undefined ? (
                  <MissingValue
                    kind="unavailable"
                    reason="GPU 型号与显存暂不可用"
                  />
                ) : (
                  `GPU ${gpu.display_index === null ? "—" : String(gpu.display_index)} · ${gpu.model}`
                )}
              </strong>
              {instance.status === "Stopped" ? (
                <span>
                  {gpu === undefined ? (
                    <MissingValue
                      kind="unavailable"
                      reason="显存总量暂不可用"
                    />
                  ) : (
                    formatBytes(gpu.memory_bytes)
                  )}{" "}
                  · 已释放
                </span>
              ) : (
                <>
                  <span>
                    Health：
                    {gpu?.health === "healthy" ? (
                      "健康"
                    ) : gpu?.health === "unhealthy" ? (
                      "健康异常"
                    ) : (
                      <MissingValue
                        kind="unavailable"
                        reason="GPU 健康状态暂不可用"
                      />
                    )}
                  </span>
                  {data?.telemetry_status === "delayed" &&
                  !telemetry.isError ? (
                    <span>
                      监控数据延迟
                      {data.sampled_at
                        ? ` · 最后更新：${new Date(data.sampled_at).toLocaleTimeString("zh-CN")}`
                        : ""}
                    </span>
                  ) : null}
                  <dl className="pw-instance-gpu-metrics pw-instance-gpu-metrics--wide">
                    <Fact
                      label="利用率"
                      value={metricValue(current?.utilization_percent, "%")}
                    />
                  </dl>
                  {current?.utilization_percent == null ? null : (
                    <div
                      className="pw-instance-gpu-memory"
                      role="meter"
                      aria-label="GPU 利用率"
                      aria-valuemin={0}
                      aria-valuemax={100}
                      aria-valuenow={Math.min(100, current.utilization_percent)}
                    >
                      <span
                        style={{
                          width: `${String(Math.min(100, current.utilization_percent))}%`,
                        }}
                      />
                    </div>
                  )}
                  <dl className="pw-instance-gpu-metrics pw-instance-gpu-metrics--wide">
                    <Fact
                      label="显存"
                      value={
                        current?.memory_used_bytes == null ||
                        gpu === undefined ? (
                          <MissingValue
                            kind="unavailable"
                            reason="显存用量暂不可用"
                          />
                        ) : (
                          `${formatBytes(current.memory_used_bytes)} / ${formatBytes(gpu.memory_bytes)}`
                        )
                      }
                    />
                  </dl>
                  {memoryRatio === undefined ? null : (
                    <div
                      className="pw-instance-gpu-memory"
                      role="meter"
                      aria-label="显存使用比例"
                      aria-valuemin={0}
                      aria-valuemax={100}
                      aria-valuenow={Math.min(100, memoryRatio)}
                    >
                      <span
                        style={{
                          width: `${String(Math.min(100, memoryRatio))}%`,
                        }}
                      />
                    </div>
                  )}
                  <dl className="pw-instance-gpu-metrics">
                    <Fact
                      label="温度"
                      value={metricValue(current?.temperature_celsius, "°C")}
                    />
                    <Fact
                      label="功耗"
                      value={metricValue(current?.power_watts, "W")}
                    />
                  </dl>
                </>
              )}
            </div>
          );
        })}
      </div>
      {instance.gpu_uuids.length === 0 ? <p>没有分配 GPU。</p> : null}
    </section>
  );
}

function InstanceEvents({
  apiClient,
  instanceId,
}: {
  apiClient: ApiClient;
  instanceId: string;
}) {
  const [cursor, setCursor] = useState<string>();
  const [loadedItems, setLoadedItems] = useState<ModelInstanceEvent[]>([]);
  const events = useQuery({
    queryFn: () => apiClient.getModelInstanceEvents(instanceId, cursor, 20),
    staleTime: 5_000,
    queryKey: ["model-instance-events", instanceId, cursor],
  });
  const items = [...loadedItems, ...(events.data?.items ?? [])]
    .filter(
      (event, index, all) =>
        all.findIndex(({ id }) => id === event.id) === index,
    )
    .sort(
      (left, right) =>
        Date.parse(right.occurred_at) - Date.parse(left.occurred_at),
    );
  const error = events.error instanceof ApiError ? events.error : undefined;

  function loadMore() {
    const page = events.data;
    if (page?.next_cursor === null || page === undefined) return;
    setLoadedItems(items);
    setCursor(page.next_cursor);
  }

  if (events.isPending && loadedItems.length === 0) {
    return (
      <section
        aria-busy="true"
        aria-label="模型实例事件正在加载"
        className="pw-instance-detail__section"
      >
        <span className="sk pw-instance-detail__skeleton" />
      </section>
    );
  }
  if (items.length === 0 && events.isError) {
    const permissionDenied = error?.status === 403;
    return (
      <section className="pw-instance-detail__section">
        <InlineAlert
          title={
            permissionDenied ? "没有查看事件的权限" : "无法获取模型实例事件"
          }
          tone="negative"
        >
          {permissionDenied
            ? "当前账号不能查看此模型实例的事件。"
            : "事件暂时无法显示，请稍后重试。"}
        </InlineAlert>
      </section>
    );
  }
  return (
    <section className="pw-instance-detail__section">
      <h2>事件</h2>
      {events.isError ? (
        <InlineAlert title="更多事件未加载" tone="negative">
          已加载的事件仍可查看，请稍后重试。
        </InlineAlert>
      ) : events.isFetching && items.length > 0 ? (
        <InlineAlert title="事件正在更新" tone="info">
          已加载的事件仍可查看。
        </InlineAlert>
      ) : null}
      <DataTable
        caption="模型实例事件"
        columns={EVENT_COLUMNS}
        emptyContent="暂无事件。"
        getRowKey={({ id }) => id}
        rows={items}
      />
      {events.data?.next_cursor === null || events.data === undefined ? null : (
        <Button
          disabled={events.isFetching}
          onClick={loadMore}
          size="s"
          variant="ghost"
        >
          {events.isFetching ? "正在加载" : "加载更多"}
        </Button>
      )}
    </section>
  );
}

const EVENT_COLUMNS: readonly TableColumn<ModelInstanceEvent>[] = [
  {
    header: "时间",
    id: "occurred-at",
    render: ({ occurred_at }) =>
      new Date(occurred_at).toLocaleString("zh-CN", { hour12: false }),
  },
  {
    header: "操作者",
    id: "actor",
    render: ({ actor }) => ACTOR_LABELS[actor],
  },
  {
    header: "动作",
    id: "action",
    render: ({ event_type, from_status, to_status }) => {
      if (to_status === null || from_status === to_status)
        return EVENT_LABELS[event_type];
      const from =
        from_status === null
          ? "—"
          : instanceStatusPresentation(from_status).label;
      const change = `状态：${from} → ${instanceStatusPresentation(to_status).label}`;
      return event_type === "StatusChanged"
        ? change
        : `${EVENT_LABELS[event_type]}（${change}）`;
    },
  },
  {
    header: "结果",
    id: "result",
    render: ({ to_status }) => (to_status === "Failed" ? "失败" : "成功"),
  },
];

const EVENT_LABELS: Readonly<Record<ModelInstanceEvent["event_type"], string>> =
  {
    DeploymentCancelled: "已取消部署",
    GpuSelectionChanged: "已重新选择 GPU",
    RestartRequested: "已请求重启",
    RetryRequested: "已请求重试",
    StatusChanged: "状态已变化",
    WaitingForGpu: "继续等待原 GPU",
  };

const ACTOR_LABELS: Readonly<Record<ModelInstanceEvent["actor"], string>> = {
  admin: "管理员",
  controller: "平台",
  system: "平台",
};

function Fact({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}

function selectedGpuResources(
  gpuUuids: readonly string[],
  loading: boolean,
  unavailable: boolean,
  gpuByUuid: ReadonlyMap<string, { memory_bytes: number; model: string }>,
) {
  if (loading) return "GPU 信息正在加载";
  if (unavailable || gpuUuids.length === 0) return "GPU 信息暂不可用";
  const selected = gpuUuids.map((uuid) => gpuByUuid.get(uuid));
  if (selected.some((gpu) => gpu === undefined)) return "GPU 信息暂不可用";
  const groups = new Map<string, number>();
  for (const gpu of selected) {
    if (gpu === undefined) continue;
    const label = `${gpu.model} ${formatBytes(gpu.memory_bytes)}`;
    groups.set(label, (groups.get(label) ?? 0) + 1);
  }
  return [...groups]
    .map(([label, count]) => `${label} × ${String(count)}`)
    .join("、");
}

function waitingGpuLabels(
  gpuUuids: readonly string[],
  gpuByUuid: ReadonlyMap<string, GpuDevice>,
) {
  if (gpuUuids.length === 0) return undefined;
  const labels: string[] = [];
  for (const uuid of gpuUuids) {
    const index = gpuByUuid.get(uuid)?.display_index;
    if (index === undefined || index === null) return undefined;
    labels.push(`GPU ${String(index)}`);
  }
  return labels.join("、");
}
