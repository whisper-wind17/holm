import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router";

import type { ApiClient } from "../api/client";
import type {
  GpuDevice,
  StringObservation,
  SystemServerResponse,
  UInt64Observation,
} from "../api/system-contracts";
import {
  Button,
  DataTable,
  InlineAlert,
  MissingValue,
  StatusBadge,
  type TableColumn,
} from "../components";
import {
  agentUnavailableReason,
  gpuDiscoveryFailureReason,
  gpuHealthPresentation,
} from "./presentation";
import { useSystemQueries, useSystemLiveQueries } from "./queries";
import * as RadixTabs from "@radix-ui/react-tabs";
import { occupiedGpuInstances } from "../model-instances/gpu-availability";
import { ComponentsContent, componentAttention } from "./component-groups";
import { DiagnosticsTab } from "./diagnostics";
import { ModelSourcesTab } from "../model-sources/model-sources-tab";
import { formatObservedAt } from "./formatting";

export { formatBytes, formatObservedAt } from "./formatting";

const SYSTEM_TAB_VALUES = [
  "server",
  "components",
  "model-sources",
  "diagnostics",
] as const;
type SystemTab = (typeof SYSTEM_TAB_VALUES)[number];

export function SystemPage({ apiClient }: { apiClient: ApiClient }) {
  const heading = useRef<HTMLHeadingElement>(null);
  const [searchParams, setSearchParams] = useSearchParams();
  const requestedTab = searchParams.get("tab");
  const initialTab = useMemo(() => readSystemTab(requestedTab), []);
  const queries = useSystemQueries(apiClient, true);
  const live = useSystemLiveQueries(apiClient);
  const [detecting, setDetecting] = useState(false);
  const attentionCount = queries.components.data
    ? componentAttention(queries.components.data).length
    : 0;
  const isInitialLoading =
    queries.server.isPending || queries.components.isPending;
  const isRefreshing =
    detecting ||
    queries.server.isFetching ||
    (isInitialLoading && queries.components.isFetching);
  const stale =
    (queries.server.data !== undefined && queries.server.isError) ||
    (queries.components.data !== undefined && queries.components.isError);

  useEffect(() => {
    heading.current?.focus();
  }, []);

  useEffect(() => {
    if (
      requestedTab === null ||
      SYSTEM_TAB_VALUES.includes(requestedTab as SystemTab)
    ) {
      return;
    }
    const next = new URLSearchParams(searchParams);
    next.delete("tab");
    setSearchParams(next, { replace: true });
  }, [requestedTab, searchParams, setSearchParams]);

  function selectTab(value: string) {
    const next = new URLSearchParams(searchParams);
    if (value === "server") next.delete("tab");
    else next.set("tab", value);
    setSearchParams(next, { replace: true });
  }

  const observedAt = queries.server.data?.observed_at;

  return (
    <div className="pw-system-page">
      <header className="pw-system-page__head">
        <span>
          <h1 ref={heading} tabIndex={-1}>
            系统
          </h1>
          <p>查看服务器硬件、平台组件、模型源配置与诊断包。</p>
        </span>
        <span className="pw-system-page__actions">
          <span className="pw-system-page__observation">
            <span>检测时间</span>
            {observedAt === undefined ? (
              queries.server.isPending ? (
                <span
                  aria-label="检测时间正在加载"
                  className="sk pw-system-page__time-skeleton"
                />
              ) : (
                "—"
              )
            ) : (
              <time dateTime={observedAt}>{formatObservedAt(observedAt)}</time>
            )}
            {isRefreshing ? <span role="status">正在检测</span> : null}
            {stale && !isRefreshing ? (
              <StatusBadge
                icon="notice"
                note={
                  observedAt === undefined
                    ? undefined
                    : formatObservedAt(observedAt)
                }
                text="数据可能延迟"
                tone="idle"
              />
            ) : null}
          </span>
          <Button
            icon="loading"
            disabled={isInitialLoading || isRefreshing}
            loadingText="正在检测"
            onClick={() => {
              setDetecting(true);
              void Promise.all([
                queries.server.refetch(),
                queries.components.refetch(),
                live.telemetry.refetch(),
                live.instances.refetch(),
              ]).finally(() => {
                setDetecting(false);
              });
            }}
          >
            重新检测
          </Button>
        </span>
      </header>
      <RadixTabs.Root
        className="pw-tabs"
        defaultValue={initialTab}
        onValueChange={selectTab}
      >
        <RadixTabs.List className="pw-tabs__list" aria-label="系统信息">
          {(
            [
              ["server", "服务器"],
              ["components", "平台组件"],
              ["model-sources", "模型源"],
              ["diagnostics", "诊断包"],
            ] as const
          ).map(([value, label]) => (
            <RadixTabs.Trigger
              className="pw-tabs__trigger"
              key={value}
              value={value}
            >
              {label}
              {value === "components" && attentionCount > 0 ? (
                <span
                  className="pw-system-count"
                  aria-label={`${String(attentionCount)} 项需要关注`}
                >
                  {attentionCount}
                </span>
              ) : null}
            </RadixTabs.Trigger>
          ))}
        </RadixTabs.List>
        <RadixTabs.Content
          className="pw-tabs__content"
          data-system-panel="server"
          value="server"
        >
          <ServerTab query={queries.server} live={live} />
        </RadixTabs.Content>
        <RadixTabs.Content
          className="pw-tabs__content"
          data-system-panel="components"
          value="components"
        >
          <ComponentsTab query={queries.components} />
        </RadixTabs.Content>
        <RadixTabs.Content className="pw-tabs__content" value="model-sources">
          <ModelSourcesTab apiClient={apiClient} />
        </RadixTabs.Content>
        <RadixTabs.Content
          className="pw-tabs__content"
          data-system-panel="diagnostics"
          value="diagnostics"
        >
          <DiagnosticsTab apiClient={apiClient} />
        </RadixTabs.Content>
      </RadixTabs.Root>
    </div>
  );
}

export function readSystemTab(value: string | null): SystemTab {
  return SYSTEM_TAB_VALUES.includes(value as SystemTab)
    ? (value as SystemTab)
    : "server";
}

type ServerQuery = ReturnType<typeof useSystemQueries>["server"];
type ComponentsQuery = ReturnType<typeof useSystemQueries>["components"];

type LiveQueries = ReturnType<typeof useSystemLiveQueries>;

function ServerTab({ query, live }: { query: ServerQuery; live: LiveQueries }) {
  if (query.data === undefined && query.isError) {
    return (
      <QueryError
        description="服务器与 GPU 信息无法显示。请检查平台连接后重试。"
        onRetry={() => void query.refetch()}
        title="无法获取服务器信息"
      />
    );
  }
  if (query.data === undefined) {
    return <ServerLoading />;
  }
  return (
    <>
      <QueryStale failed={query.isError} onRetry={() => void query.refetch()} />
      <ServerContent response={query.data} live={live} />
    </>
  );
}

function ServerLoading() {
  const columns: readonly TableColumn<GpuDevice>[] = [
    "GPU",
    "型号 / 总显存",
    "分配",
    "利用率",
    "显存",
    "温度",
    "功耗",
    "Health",
  ].map((header) => ({ header, id: header, render: () => null }));
  return (
    <section aria-label="服务器信息正在加载" className="pw-system-stack">
      <section className="pw-system-section">
        <h2>GPU</h2>
        <DataTable
          caption="GPU"
          columns={columns}
          getRowKey={() => "loading"}
          loading
          rows={[]}
        />
      </section>
      <div aria-busy="true" className="pw-system-summary-grid">
        <SummarySkeleton title="主机" />
        <SummarySkeleton title="环境" />
      </div>
    </section>
  );
}

function SummarySkeleton({ title }: { title: string }) {
  return (
    <section aria-label={`${title}信息正在加载`} className="pw-system-panel">
      <h2>{title}</h2>
      <span className="sk pw-system-panel__skeleton" />
      <span className="sk pw-system-panel__skeleton" />
      <span className="sk pw-system-panel__skeleton" />
    </section>
  );
}

function ServerContent({
  response,
  live,
}: {
  response: SystemServerResponse;
  live: LiveQueries;
}) {
  if (response.agent.status === "unavailable") {
    return (
      <section className="pw-system-stack">
        <InlineAlert title="无法读取服务器信息" tone="negative">
          {agentUnavailableReason(response.agent.reason)}
          。GPU、主机与环境信息暂时无法显示；已运行的模型实例不受影响。请稍后点「重新检测」。
        </InlineAlert>
      </section>
    );
  }

  const { gpus, host } = response.agent;
  const healthAlerts = gpus.devices.filter(
    (device) => device.health !== "healthy",
  );

  const occupied = occupiedGpuInstances(live.instances.data?.items ?? []);
  const allocationKnown =
    live.instances.data !== undefined && !live.instances.isError;
  const allocated = gpus.devices.filter((gpu) => occupied.has(gpu.uuid)).length;
  const sample = (uuid: string) =>
    live.telemetry.data?.items.find((item) => item.uuid === uuid);
  const metricAvailable = (uuid: string) =>
    !live.telemetry.isError &&
    live.telemetry.data?.telemetry_status !== "unavailable" &&
    sample(uuid)?.telemetry_status !== "unavailable" &&
    sample(uuid) !== undefined;
  const external = gpus.devices.filter(
    (gpu) =>
      allocationKnown &&
      !occupied.has(gpu.uuid) &&
      metricAvailable(gpu.uuid) &&
      (sample(gpu.uuid)?.current.memory_used_bytes ?? 0) > 1073741824,
  );
  const unhealthy = gpus.devices.filter((gpu) => gpu.health === "unhealthy");
  const unknown = gpus.devices.filter((gpu) => gpu.health === "unknown");
  const missingTelemetry = gpus.devices.some(
    (gpu) =>
      !metricAvailable(gpu.uuid) ||
      Object.entries(sample(gpu.uuid)?.current ?? {}).some(
        ([key, value]) => key !== "health" && value === null,
      ),
  );

  return (
    <div className="pw-system-stack">
      {gpus.discovery_state === "ready" ? (
        <div
          className={`pw-system-summary ${unhealthy.length ? "pw-system-negative" : ""}`}
          aria-label="服务器概况"
        >
          <StatusBadge
            icon={
              unhealthy.length ? "error" : unknown.length ? "notice" : "success"
            }
            tone={
              unhealthy.length
                ? "negative"
                : unknown.length
                  ? "notice"
                  : "positive"
            }
            text={`${String(gpus.devices.length)} 张 GPU，${unhealthy.length ? `${String(unhealthy.length)} 张 Health 异常` : unknown.length ? `${String(unknown.length)} 张 Health 未知` : "Health 全部正常"}`}
          />
          {allocationKnown ? (
            <>
              <span>{allocated} 张已分配</span>
              <span>{gpus.devices.length - allocated} 张空闲</span>
            </>
          ) : (
            <span>分配信息暂不可用</span>
          )}
          {external.length ? (
            <span>{external.length} 张空闲卡有平台外显存占用</span>
          ) : null}
        </div>
      ) : null}
      {healthAlerts.map((gpu) => (
        <GpuHealthAlert
          gpu={gpu}
          key={gpu.uuid}
          instanceName={
            allocationKnown
              ? live.instances.data?.items.find(
                  (item) => item.id === occupied.get(gpu.uuid),
                )?.name
              : undefined
          }
        />
      ))}
      {missingTelemetry ? (
        <InlineAlert title="GPU 监控数据暂不可用" tone="notice">
          遥测采集器没有新数据，利用率、显存、温度与功耗显示「—」，悬停可看最后更新时间。GPU
          清单与 Health 不受影响，AI 服务可用性判断也不受影响。
        </InlineAlert>
      ) : null}
      {live.instances.isError ? (
        <QueryError
          title="无法获取分配信息"
          description="GPU 分配信息无法显示。请检查平台连接后重试。"
          onRetry={() => void live.instances.refetch()}
        />
      ) : null}
      <section className="pw-system-group pw-system-gpu" aria-label="GPU">
        <h2 className="pw-system-section__title">
          GPU
          {live.telemetry.data?.sampled_at ? (
            <span>
              利用率等指标采样于{" "}
              {formatObservedAt(live.telemetry.data.sampled_at)}
            </span>
          ) : null}
        </h2>
        {gpus.discovery_state === "unavailable" ? (
          <InlineAlert title="GPU 信息暂不可用" tone="notice">
            {gpuDiscoveryFailureReason(gpus.failure_reason)}，当前无法判断 GPU
            数量与 Health。请重新检测。
          </InlineAlert>
        ) : (
          <>
            <DataTable
              caption="GPU"
              columns={gpuColumns(live)}
              emptyContent={
                <span className="pw-system-empty">
                  <strong>没有检测到 GPU</strong>
                  <span>
                    检测已完成，这台服务器当前确实没有可用的 GPU 设备。
                  </span>
                </span>
              }
              getRowKey={(gpu) => gpu.uuid}
              rows={gpus.discovery_state === "no_devices" ? [] : gpus.devices}
            />
          </>
        )}
        <p className="pw-system-footnote">
          利用率、显存、温度与功耗每 10 秒自动刷新；GPU 清单与 Health
          在进入页面和点「重新检测」时更新。显存为已用 / 总量。
        </p>
      </section>
      <div className="pw-system-summary-grid">
        <HostPanel host={host} observedAt={response.observed_at} />
        <EnvironmentPanel host={host} observedAt={response.observed_at} />
      </div>
    </div>
  );
}

function GpuHealthAlert({
  gpu,
  instanceName,
}: {
  gpu: GpuDevice;
  instanceName?: string;
}) {
  const index =
    gpu.display_index === null ? gpu.uuid : String(gpu.display_index);
  if (gpu.health === "unhealthy") {
    return (
      <InlineAlert title={`GPU ${index} Health 异常`} tone="negative">
        运行在这张卡上的模型实例{instanceName ? `「${instanceName}」` : ""}
        可能出错。请检查服务器硬件。
      </InlineAlert>
    );
  }
  return (
    <InlineAlert title={`GPU ${index} Health 未知`} tone="notice">
      采集链路暂时无法确认设备状态，未知不等于异常。请重新检测。
    </InlineAlert>
  );
}

function MetricBar({
  percent,
  hot = false,
}: {
  percent: number;
  hot?: boolean;
}) {
  return (
    <div className="pw-system-bar" aria-hidden="true">
      <span
        className={hot ? "pw-system-bar__hot" : undefined}
        style={{ width: `${String(Math.min(100, Math.max(0, percent)))}%` }}
      />
    </div>
  );
}
function gib(value: number) {
  return `${(value / 1073741824).toFixed(1)} GiB`;
}
function gpuColumns(live: LiveQueries): readonly TableColumn<GpuDevice>[] {
  const occupied = occupiedGpuInstances(live.instances.data?.items ?? []);
  const sample = (gpu: GpuDevice) =>
    live.telemetry.data?.items.find((item) => item.uuid === gpu.uuid);
  const metric = (
    gpu: GpuDevice,
    key:
      | "utilization_percent"
      | "memory_used_bytes"
      | "temperature_celsius"
      | "power_watts",
  ) => {
    const item = sample(gpu);
    return live.telemetry.isError ||
      live.telemetry.data?.telemetry_status === "unavailable" ||
      item?.telemetry_status === "unavailable"
      ? null
      : (item?.current[key] ?? null);
  };
  const missing = (gpu: GpuDevice) => {
    const sampledAt = sample(gpu)?.sampled_at;
    const hint = `监控数据暂不可用 · 最后更新 ${sampledAt ? formatObservedAt(sampledAt) : "—"}`;
    return (
      <span title={hint} aria-label={hint}>
        —
      </span>
    );
  };
  return [
    {
      header: "GPU",
      id: "gpu",
      render: (gpu) => (
        <>
          <strong>GPU {gpu.display_index ?? "编号未知"}</strong>
          <span className="pw-system-sub pw-system-technical" title={gpu.uuid}>
            {gpu.uuid.slice(0, 12)}…
          </span>
        </>
      ),
    },
    {
      header: "型号 / 总显存",
      id: "model",
      render: (gpu) => (
        <>
          {gpu.model}
          <span className="pw-system-sub">{gib(gpu.memory_bytes)}</span>
        </>
      ),
    },
    {
      header: "分配",
      id: "allocation",
      render: (gpu) => {
        if (!live.instances.data || live.instances.isError)
          return <MissingValue kind="unavailable" reason="分配信息暂不可用" />;
        const instance = live.instances.data.items.find(
          (item) => item.id === occupied.get(gpu.uuid),
        );
        const memory = metric(gpu, "memory_used_bytes");
        return instance ? (
          <>
            <Link to={`/models/instances/${instance.id}`}>{instance.name}</Link>
            <span className="pw-system-sub">
              {instance.gpu_uuids.length > 1
                ? `与另 ${String(instance.gpu_uuids.length - 1)} 张 GPU 共同承载`
                : "单卡"}
            </span>
          </>
        ) : (
          <>
            空闲
            {memory !== null && memory > 1073741824 ? (
              <span
                className="pw-system-external"
                title="显存已被平台以外的进程占用，在这张卡上部署可能因显存不足而失败。"
              >
                平台外占用 {gib(memory)}
              </span>
            ) : null}
          </>
        );
      },
    },
    {
      header: "利用率",
      id: "utilization",
      numeric: true,
      render: (gpu) => {
        const value = metric(gpu, "utilization_percent");
        return value === null ? (
          missing(gpu)
        ) : (
          <>
            {value}%<MetricBar percent={value} hot={value >= 90} />
          </>
        );
      },
    },
    {
      header: "显存",
      id: "memory",
      numeric: true,
      render: (gpu) => {
        const value = metric(gpu, "memory_used_bytes");
        return value === null ? (
          missing(gpu)
        ) : (
          <>
            {(value / 1073741824).toFixed(1)} / {gib(gpu.memory_bytes)}
            <MetricBar
              percent={
                gpu.memory_bytes > 0 ? (value / gpu.memory_bytes) * 100 : 0
              }
            />
          </>
        );
      },
    },
    {
      header: "温度",
      id: "temperature",
      numeric: true,
      render: (gpu) => {
        const value = metric(gpu, "temperature_celsius");
        return value === null ? missing(gpu) : `${String(value)} °C`;
      },
    },
    {
      header: "功耗",
      id: "power",
      numeric: true,
      render: (gpu) => {
        const value = metric(gpu, "power_watts");
        return value === null ? missing(gpu) : `${value.toFixed(0)} W`;
      },
    },
    {
      header: "Health",
      id: "health",
      render: (gpu) => {
        const status = gpuHealthPresentation(gpu.health);
        return (
          <StatusBadge
            icon={status.icon}
            text={status.label}
            tone={status.tone}
          />
        );
      },
    },
  ];
}

function HostPanel({
  host,
  observedAt,
}: {
  host: Extract<SystemServerResponse["agent"], { status: "available" }>["host"];
  observedAt: string;
}) {
  return (
    <section className="pw-system-panel">
      <h2>主机</h2>
      <dl>
        <ObservationRow
          label="CPU"
          observedAt={observedAt}
          observation={host.logical_cpu_count}
          render={(value) => `${formatNumber(value)} 逻辑核`}
        />
        <ObservationRow
          label="内存"
          observedAt={observedAt}
          observation={host.total_memory_bytes}
          render={gib}
        />
        <DiskObservationRow
          available={host.available_disk_bytes}
          observedAt={observedAt}
          total={host.total_disk_bytes}
        />
      </dl>
    </section>
  );
}

function EnvironmentPanel({
  host,
  observedAt,
}: {
  host: Extract<SystemServerResponse["agent"], { status: "available" }>["host"];
  observedAt: string;
}) {
  return (
    <section className="pw-system-panel">
      <h2>环境</h2>
      <dl>
        <StringObservationRow
          label="NVIDIA Driver"
          observedAt={observedAt}
          observation={host.driver_version}
        />
        <StringObservationRow
          label="CUDA"
          observedAt={observedAt}
          observation={host.cuda_driver_version}
        />
      </dl>
      <p>Driver 与 CUDA 由服务器提供，版本变化可能影响模型实例兼容性。</p>
    </section>
  );
}

function ObservationRow({
  label,
  observation,
  observedAt,
  render,
}: {
  label: string;
  observation: UInt64Observation;
  observedAt: string;
  render: (value: number) => string;
}) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>
        {observation.state === "known" ? (
          <span className="pw-system-technical">
            {render(observation.value)}
          </span>
        ) : (
          <MissingValue
            kind="unavailable"
            lastUpdated={formatObservedAt(observedAt)}
            reason="监控数据暂不可用"
          />
        )}
      </dd>
    </div>
  );
}

function StringObservationRow({
  label,
  observation,
  observedAt,
}: {
  label: string;
  observation: StringObservation;
  observedAt: string;
}) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>
        {observation.state === "known" ? (
          <span className="pw-system-technical">{observation.value}</span>
        ) : (
          <MissingValue
            kind="unavailable"
            lastUpdated={formatObservedAt(observedAt)}
            reason="监控数据暂不可用"
          />
        )}
      </dd>
    </div>
  );
}

function DiskObservationRow({
  available,
  observedAt,
  total,
}: {
  available: UInt64Observation;
  observedAt: string;
  total: UInt64Observation;
}) {
  return (
    <div>
      <dt>磁盘</dt>
      <dd>
        {available.state === "known" && total.state === "known" ? (
          <span className="pw-system-technical">
            可用 {(available.value / 1099511627776).toFixed(1)} TiB / 共{" "}
            {(total.value / 1099511627776).toFixed(1)} TiB
            <MetricBar
              percent={
                total.value > 0
                  ? ((total.value - available.value) / total.value) * 100
                  : 0
              }
            />
          </span>
        ) : (
          <MissingValue
            kind="unavailable"
            lastUpdated={formatObservedAt(observedAt)}
            reason="监控数据暂不可用"
          />
        )}
      </dd>
    </div>
  );
}

function ComponentsTab({ query }: { query: ComponentsQuery }) {
  if (query.data === undefined && query.isError) {
    return (
      <QueryError
        description="宿主环境、Holm 与推理引擎的状态无法显示。请检查平台连接后重试。"
        onRetry={() => void query.refetch()}
        title="无法获取平台组件"
      />
    );
  }
  if (query.data === undefined) {
    return (
      <section aria-busy="true" aria-label="平台组件正在加载">
        <SummarySkeleton title="宿主环境" />
        <SummarySkeleton title="Holm" />
        <SummarySkeleton title="推理引擎" />
      </section>
    );
  }
  return (
    <>
      <QueryStale failed={query.isError} onRetry={() => void query.refetch()} />
      <ComponentsContent response={query.data} />
    </>
  );
}
function QueryStale({
  failed,
  onRetry,
}: {
  failed: boolean;
  onRetry: () => void;
}) {
  return failed ? (
    <QueryError
      title="数据可能延迟"
      description="刷新失败，当前显示上次检测结果。请重试。"
      onRetry={onRetry}
    />
  ) : null;
}

function QueryError({
  description,
  onRetry,
  title,
}: {
  description: string;
  onRetry: () => void;
  title: string;
}) {
  return (
    <InlineAlert title={title} tone="negative">
      <span>{description}</span>
      <Button onClick={onRetry} size="s">
        重试
      </Button>
    </InlineAlert>
  );
}

function formatNumber(value: number) {
  return new Intl.NumberFormat("zh-CN").format(value);
}
