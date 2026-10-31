import { useState } from "react";
import type { AiService } from "../api/ai-service-contracts";
import type { ApiClient } from "../api/client";
import type {
  InstanceTelemetry,
  ServiceTelemetry,
} from "../api/telemetry-contracts";
import { Button, Chart, InlineAlert, MissingValue } from "../components";
import {
  useInstanceTelemetry,
  useServiceTelemetry,
  type ServiceTelemetryRange,
} from "../telemetry/queries";

import { formatChartTime } from "../telemetry/chart-time";

const RANGES: readonly [ServiceTelemetryRange, string][] = [
  ["1h", "1 小时"],
  ["6h", "6 小时"],
  ["24h", "24 小时"],
  ["7d", "7 天"],
];
type Point = ServiceTelemetry["series"][number];
type Metric = keyof ServiceTelemetry["current"];

export function ServiceMonitor({
  apiClient,
  service,
  instanceName,
}: {
  apiClient: ApiClient;
  service: AiService;
  instanceName: string;
}) {
  const [range, setRange] = useState<ServiceTelemetryRange>("1h");
  const telemetry = useServiceTelemetry(apiClient, service.id, range);
  const backend = useInstanceTelemetry(apiClient, service.primary_instance_id);
  if (telemetry.isPending || backend.isPending)
    return (
      <section
        aria-busy="true"
        aria-label="服务监控正在加载"
        className="pw-monitor-section"
      >
        <span className="sk pw-monitor-card__skeleton" />
      </section>
    );
  if (telemetry.data === undefined || backend.data === undefined)
    return (
      <section className="pw-monitor-section">
        <InlineAlert title="无法获取服务监控" tone="negative">
          监控数据无法显示。请检查平台连接后重试。
          <Button
            onClick={() =>
              void Promise.all([telemetry.refetch(), backend.refetch()])
            }
            size="s"
          >
            重试
          </Button>
        </InlineAlert>
      </section>
    );
  const data = telemetry.data;
  const instance = backend.data;
  const backendTimeAxis = {
    times: instance.series.map((point) => Date.parse(point.sampled_at)),
    formatTick: (ms: number) =>
      formatChartTime(new Date(ms).toISOString(), "1h", "axis"),
    gapMs: 15_000,
  };
  const serviceReason =
    data.telemetry_status === "unavailable"
      ? "服务遥测不可用"
      : "所选时间范围内暂无请求样本";
  const backendReason =
    instance.telemetry_status === "unavailable"
      ? "后端遥测不可用"
      : "后端暂无样本";
  const unsupported = new Set(instance.current.unsupported);
  const backendReasonFor = (
    metric: InstanceTelemetry["current"]["unsupported"][number],
  ) => (unsupported.has(metric) ? "当前 Runtime 不支持该指标" : backendReason);
  return (
    <section className="pw-monitor-section">
      <header className="pw-monitor-section__head">
        <div>
          <h2>监控</h2>
          <p>用户侧指标来自 Gateway；后端指标为模型实例总量。</p>
        </div>
        <label>
          时间范围{" "}
          <select
            aria-label="时间范围"
            value={range}
            onChange={(event) => {
              setRange(event.target.value as ServiceTelemetryRange);
            }}
          >
            {RANGES.map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </label>
      </header>
      {data.telemetry_status === "available" ? null : (
        <InlineAlert title="监控数据延迟或不可用" tone="notice">
          当前数值可能缺失，不影响 AI 服务的可用状态。
          {data.sampled_at
            ? `最后更新：${new Date(data.sampled_at).toLocaleString("zh-CN")}`
            : "尚无更新时间。"}
        </InlineAlert>
      )}
      <div className="pw-monitor-grid">
        <MetricPanel
          title="用户侧 TTFT P50 / P95 / P99"
          value={format(data.current.ttft_p95_ms, " ms")}
          reason={serviceReason}
          chart={
            <ServiceChart
              points={data.series}
              range={range}
              reason={serviceReason}
              title="用户侧 TTFT"
              metrics={[
                ["ttft_p50_ms", "P50"],
                ["ttft_p95_ms", "P95"],
                ["ttft_p99_ms", "P99"],
              ]}
            />
          }
        />
        <MetricPanel
          title="用户侧端到端耗时 P50 / P95 / P99"
          value={format(data.current.e2e_latency_p95_ms, " ms")}
          reason={serviceReason}
          chart={
            <ServiceChart
              points={data.series}
              range={range}
              reason={serviceReason}
              title="用户侧端到端耗时"
              metrics={[
                ["e2e_latency_p50_ms", "P50"],
                ["e2e_latency_p95_ms", "P95"],
                ["e2e_latency_p99_ms", "P99"],
              ]}
            />
          }
        />
        <MetricPanel
          title="服务输出 TPS"
          value={format(data.current.output_tps_avg, " tokens/s")}
          reason={serviceReason}
          chart={
            <ServiceChart
              points={data.series}
              range={range}
              reason={serviceReason}
              title="服务输出 TPS"
              metrics={[["output_tps_avg", "TPS"]]}
            />
          }
        />
        <MetricPanel
          title="Gateway In-flight"
          value={format(data.current.inflight_last)}
          reason={serviceReason}
          chart={
            <ServiceChart
              points={data.series}
              range={range}
              reason={serviceReason}
              title="Gateway In-flight"
              metrics={[["inflight_last", "运行中"]]}
            />
          }
        />
        <MetricPanel
          title="用户侧 TPOT P50 / P95"
          value={format(data.current.tpot_p95_ms, " ms")}
          reason={serviceReason}
          chart={
            <ServiceChart
              points={data.series}
              range={range}
              reason={serviceReason}
              title="用户侧 TPOT"
              metrics={[
                ["tpot_p50_ms", "P50"],
                ["tpot_p95_ms", "P95"],
              ]}
            />
          }
        />
        <MetricPanel
          title="Request Rate"
          value={format(data.current.request_rate_avg, " requests/s")}
          reason={serviceReason}
          chart={
            <ServiceChart
              points={data.series}
              range={range}
              reason={serviceReason}
              title="Request Rate"
              metrics={[["request_rate_avg", "请求率"]]}
            />
          }
        />
        <MetricPanel
          title="Error Rate"
          value={format(data.current.error_rate, "%", 100)}
          reason={serviceReason}
          chart={
            <ServiceChart
              points={data.series}
              range={range}
              reason={serviceReason}
              title="Error Rate"
              metrics={[["error_rate", "错误率"]]}
            />
          }
        />
        <section className="pw-monitor-card">
          <h3>服务 Queue Time</h3>
          <p>当前 Runtime 不支持服务级排队耗时</p>
        </section>
      </div>
      <h3>后端 · {instanceName}</h3>
      <p>
        健康状态：
        {service.primary_instance_status === "Running" ? "运行中" : "未就绪"}
        。以下为后端总量。
      </p>
      {instance.telemetry_status === "available" ? null : (
        <InlineAlert title="后端监控数据不可用" tone="notice">
          {backendReason}
        </InlineAlert>
      )}
      <div className="pw-monitor-grid">
        <MetricPanel
          title="后端 TTFT P50 / P95 / P99"
          value={format(instance.current.ttft_p95_seconds, " s")}
          reason={backendReasonFor("runtime_ttft_seconds")}
          chart={
            instance.series.some(
              (point) =>
                point.ttft_p50_seconds !== null ||
                point.ttft_p95_seconds !== null ||
                point.ttft_p99_seconds !== null,
            ) ? (
              <Chart
                ariaLabel="后端 TTFT，最近 1 小时"
                valueFormat={{ unit: "ms", scale: 1000 }}
                kind="ordinal"
                categories={instance.series.map((point) =>
                  formatChartTime(point.sampled_at, "1h", "detail"),
                )}
                timeAxis={backendTimeAxis}
                series={
                  [
                    ["ttft_p50_seconds", "P50"],
                    ["ttft_p95_seconds", "P95"],
                    ["ttft_p99_seconds", "P99"],
                  ].map(([id, label]) => ({
                    id,
                    label,
                    values: instance.series.map(
                      (point) =>
                        point[
                          id as
                            | "ttft_p50_seconds"
                            | "ttft_p95_seconds"
                            | "ttft_p99_seconds"
                        ],
                    ),
                  })) as [
                    { id: string; label: string; values: (number | null)[] },
                    { id: string; label: string; values: (number | null)[] },
                    { id: string; label: string; values: (number | null)[] },
                  ]
                }
              />
            ) : (
              <MissingValue
                kind="unavailable"
                reason={backendReasonFor("runtime_ttft_seconds")}
              />
            )
          }
        />
        <MetricPanel
          title="后端 Running / Waiting"
          value={
            instance.current.running === null ||
            instance.current.waiting === null
              ? null
              : `${instance.current.running.toLocaleString("zh-CN")} / ${instance.current.waiting.toLocaleString("zh-CN")}`
          }
          reason={backendReasonFor("runtime_requests_running")}
        />
        <MetricPanel
          title="后端 Queue Time P95"
          value={format(instance.current.queue_time_p95_seconds, " s")}
          reason={backendReasonFor("runtime_queue_time_seconds")}
        />
        <MetricPanel
          title="后端 KV Cache"
          value={format(instance.current.kv_cache_usage_ratio, "%", 100)}
          reason={backendReasonFor("runtime_kv_cache_usage_ratio")}
        />
      </div>
    </section>
  );
}

function MetricPanel({
  title,
  value,
  reason,
  chart,
}: {
  title: string;
  value: string | null;
  reason: string;
  chart?: React.ReactNode;
}) {
  return (
    <section className="pw-monitor-card">
      <header>
        <h3>{title}</h3>
        <strong>
          {value ?? <MissingValue kind="unavailable" reason={reason} />}
        </strong>
      </header>
      {chart}
    </section>
  );
}
function format(value: number | null, suffix = "", scale = 1) {
  return value === null
    ? null
    : `${(value * scale).toFixed(suffix === "%" ? 1 : 0)}${suffix}`;
}
function ServiceChart({
  points,
  metrics,
  title,
  range,
  reason,
}: {
  points: readonly Point[];
  metrics: readonly (readonly [Metric, string])[];
  title: string;
  range: ServiceTelemetryRange;
  reason: string;
}) {
  if (
    !points.some((point) => metrics.some(([metric]) => point[metric] !== null))
  )
    return <MissingValue kind="unavailable" reason={reason} />;
  const timeAxis = {
    times: points.map((point) => Date.parse(point.sampled_at)),
    formatTick: (ms: number) =>
      formatChartTime(new Date(ms).toISOString(), range, "axis"),
    gapMs: range === "1h" ? 15_000 : 180_000,
  };
  const series = metrics.map(([id, label]) => ({
    id,
    label,
    values: points.map((point) => point[id]),
  }));
  const first = series[0];
  const second = series[1];
  const third = series[2];
  if (first === undefined)
    return <MissingValue kind="unavailable" reason={reason} />;
  if (series.length === 1)
    return (
      <Chart
        ariaLabel={`${title}，最近 ${range}`}
        valueFormat={
          first.id.endsWith("_ms")
            ? { unit: "ms" }
            : first.id === "error_rate"
              ? { unit: "%", scale: 100 }
              : first.id === "output_tps_avg"
                ? { unit: "Tokens/s" }
                : first.id === "request_rate_avg"
                  ? { unit: "次/s", compact: true }
                  : { unit: "个", compact: true, integer: true }
        }
        kind="ordinal"
        categories={points.map((point) =>
          formatChartTime(point.sampled_at, range, "detail"),
        )}
        timeAxis={timeAxis}
        series={[first]}
      />
    );
  if (second !== undefined && third === undefined)
    return (
      <Chart
        ariaLabel={`${title}，最近 ${range}`}
        valueFormat={
          first.id.endsWith("_ms")
            ? { unit: "ms" }
            : first.id === "error_rate"
              ? { unit: "%", scale: 100 }
              : first.id === "output_tps_avg"
                ? { unit: "Tokens/s" }
                : first.id === "request_rate_avg"
                  ? { unit: "次/s", compact: true }
                  : { unit: "个", compact: true, integer: true }
        }
        kind="ordinal"
        categories={points.map((point) =>
          formatChartTime(point.sampled_at, range, "detail"),
        )}
        timeAxis={timeAxis}
        series={[first, second]}
      />
    );
  if (second === undefined || third === undefined)
    return <MissingValue kind="unavailable" reason={reason} />;
  return (
    <Chart
      ariaLabel={`${title}，最近 ${range}`}
      valueFormat={
        first.id.endsWith("_ms")
          ? { unit: "ms" }
          : first.id === "error_rate"
            ? { unit: "%", scale: 100 }
            : first.id === "output_tps_avg"
              ? { unit: "Tokens/s" }
              : first.id === "request_rate_avg"
                ? { unit: "次/s", compact: true }
                : { unit: "个", compact: true, integer: true }
      }
      kind="ordinal"
      categories={points.map((point) =>
        formatChartTime(point.sampled_at, range, "detail"),
      )}
      timeAxis={timeAxis}
      series={[first, second, third]}
    />
  );
}
