import type { ApiClient } from "../api/client";
import type {
  GpuTelemetry,
  InstanceTelemetry,
} from "../api/telemetry-contracts";
import type { ReactNode } from "react";
import { Chart, InlineAlert } from "../components";
import type { GpuDevice } from "../api/system-contracts";
import { formatBytes } from "../system/system-page";
import { useGpuTelemetry, useInstanceTelemetry } from "./queries";

import { formatChartTime } from "./chart-time";

type InstancePoint = InstanceTelemetry["series"][number];
type GpuPoint = GpuTelemetry["series"][number];

export function InstanceMonitor({
  apiClient,
  gpuByUuid,
  gpuUuids,
  instanceId,
}: {
  apiClient: ApiClient;
  gpuByUuid: ReadonlyMap<string, GpuDevice>;
  gpuUuids: readonly string[];
  instanceId: string;
}) {
  const telemetry = useInstanceTelemetry(apiClient, instanceId);
  const gpus = useGpuTelemetry(apiClient);

  if (telemetry.isPending || gpus.isPending) {
    return (
      <section
        aria-busy="true"
        aria-label="监控正在加载"
        className="pw-monitor-section"
      >
        <h2>监控</h2>
        <div className="pw-monitor-grid">
          <span className="sk pw-monitor-card__skeleton" />
          <span className="sk pw-monitor-card__skeleton" />
        </div>
      </section>
    );
  }

  if (telemetry.data === undefined || gpus.data === undefined) {
    return (
      <section className="pw-monitor-section">
        <h2>监控</h2>
        <InlineAlert title="无法获取监控数据" tone="negative">
          最近一小时的运行状态无法显示。请检查平台连接后重试。
        </InlineAlert>
      </section>
    );
  }

  const data = telemetry.data;
  const unsupported = new Set(data.current.unsupported);
  const gpuTelemetryByUuid = new Map(
    gpus.data.items.map((item) => [item.uuid, item]),
  );
  return (
    <section className="pw-monitor-section">
      <header className="pw-monitor-section__head">
        <div>
          <h2>监控</h2>
          <p>最近 1 小时，每 5 秒更新。</p>
        </div>
        <span>
          {data.telemetry_status === "available" ? "实时" : "数据不可用"}
        </span>
      </header>
      {data.telemetry_status === "available" ? null : (
        <InlineAlert title="监控数据暂不完整" tone="notice">
          当前数据延迟或不可用，不影响模型实例状态与 AI 服务调用。
        </InlineAlert>
      )}
      <div className="pw-monitor-grid">
        <MetricPanel
          chart={
            unsupported.has("runtime_ttft_seconds") ? (
              <UnsupportedChart />
            ) : (
              ordinalChart(data.series, [
                ["ttft_p50_seconds", "P50"],
                ["ttft_p95_seconds", "P95"],
                ["ttft_p99_seconds", "P99"],
              ])
            )
          }
          current={
            unsupported.has("runtime_ttft_seconds")
              ? "不支持"
              : formatSeconds(data.current.ttft_p95_seconds)
          }
          title="TTFT P50 / P95 / P99"
        />
        <MetricPanel
          chart={
            unsupported.has("runtime_output_tokens_total") ? (
              <UnsupportedChart />
            ) : (
              singleChart(data.series, "output_tps", "输出 TPS")
            )
          }
          current={
            unsupported.has("runtime_output_tokens_total")
              ? "不支持"
              : formatRate(data.current.output_tps)
          }
          title="输出 TPS"
        />
        <MetricPanel
          chart={
            unsupported.has("runtime_requests_running") ||
            unsupported.has("runtime_requests_waiting") ? (
              <UnsupportedChart />
            ) : (
              ordinalChart(data.series, [
                ["running", "运行中"],
                ["waiting", "等待中"],
              ])
            )
          }
          current={`${unsupported.has("runtime_requests_running") ? "不支持" : formatCount(data.current.running)} / ${unsupported.has("runtime_requests_waiting") ? "不支持" : formatCount(data.current.waiting)}`}
          title="运行中 / 等待中"
        />
        <MetricPanel
          chart={
            unsupported.has("runtime_queue_time_seconds") ? (
              <UnsupportedChart />
            ) : (
              singleChart(
                data.series,
                "queue_time_p95_seconds",
                "Queue Time P95",
              )
            )
          }
          current={
            unsupported.has("runtime_queue_time_seconds")
              ? "不支持"
              : formatSeconds(data.current.queue_time_p95_seconds)
          }
          title="Queue Time P95"
        />
        <MetricPanel
          chart={
            unsupported.has("runtime_kv_cache_usage_ratio") ? (
              <UnsupportedChart />
            ) : (
              singleChart(data.series, "kv_cache_usage_ratio", "KV Cache")
            )
          }
          current={
            unsupported.has("runtime_kv_cache_usage_ratio")
              ? "不支持"
              : formatPercent(data.current.kv_cache_usage_ratio)
          }
          title="KV Cache"
        />
      </div>
      <div className="pw-gpu-monitor-list">
        {gpuUuids.map((uuid) => (
          <GpuMonitor
            device={gpuByUuid.get(uuid)}
            key={uuid}
            telemetry={gpuTelemetryByUuid.get(uuid)}
            uuid={uuid}
          />
        ))}
      </div>
    </section>
  );
}

function MetricPanel({
  chart,
  current,
  title,
}: {
  chart: ReactNode;
  current: string;
  title: string;
}) {
  return (
    <section className="pw-monitor-card">
      <header>
        <h3>{title}</h3>
        <strong>{current}</strong>
      </header>
      {chart}
    </section>
  );
}

function GpuMonitor({
  device,
  telemetry,
  uuid,
}: {
  device?: GpuDevice;
  telemetry?: GpuTelemetry;
  uuid: string;
}) {
  const current = telemetry?.current;
  const series = telemetry?.series ?? [];
  const label =
    device?.display_index === null || device === undefined
      ? "GPU"
      : `GPU ${String(device.display_index)}`;
  return (
    <section className="pw-gpu-monitor">
      <header>
        <div>
          <h3>{label}</h3>
          <span>{device?.model ?? uuid}</span>
        </div>
        <span>
          {telemetry?.telemetry_status === "available" ? "实时" : "数据不可用"}
        </span>
      </header>
      <div className="pw-gpu-monitor__metrics">
        <MetricPanel
          chart={singleGpuChart(series, "utilization_percent", "GPU 利用率")}
          current={formatPercentValue(current?.utilization_percent)}
          title="利用率"
        />
        <MetricPanel
          chart={singleGpuChart(series, "memory_used_bytes", "已用显存")}
          current={
            current?.memory_used_bytes == null
              ? "—"
              : formatBytes(current.memory_used_bytes)
          }
          title="已用显存"
        />
        <MetricPanel
          chart={singleGpuChart(series, "temperature_celsius", "温度")}
          current={
            current?.temperature_celsius == null
              ? "—"
              : `${current.temperature_celsius.toFixed(0)} °C`
          }
          title="温度"
        />
        <MetricPanel
          chart={singleGpuChart(series, "power_watts", "功耗")}
          current={
            current?.power_watts == null
              ? "—"
              : `${current.power_watts.toFixed(1)} W`
          }
          title="功耗"
        />
      </div>
    </section>
  );
}

type InstanceMetricName = keyof Omit<
  InstancePoint,
  "sampled_at" | "unsupported" | "quality" | "telemetry_status"
>;
type GpuMetricName = keyof Omit<
  GpuPoint,
  "sampled_at" | "quality" | "telemetry_status" | "health"
>;

function singleChart(
  points: readonly InstancePoint[],
  name: InstanceMetricName,
  label: string,
) {
  if (!points.some((point) => point[name] !== null)) return <MissingChart />;
  const timeAxis = {
    times: points.map((point) => Date.parse(point.sampled_at)),
    formatTick: (ms: number) =>
      formatChartTime(new Date(ms).toISOString(), "1h", "axis"),
    gapMs: 15_000,
  };
  return (
    <Chart
      ariaLabel={`${label}，最近 1 小时`}
      valueFormat={
        name.includes("seconds")
          ? { unit: "ms", scale: 1000 }
          : name === "kv_cache_usage_ratio"
            ? { unit: "%", scale: 100 }
            : name === "output_tps"
              ? { unit: "Tokens/s" }
              : { unit: "个", compact: true, integer: true }
      }
      categories={points.map((point) =>
        formatChartTime(point.sampled_at, "1h", "detail"),
      )}
      timeAxis={timeAxis}
      kind="ordinal"
      series={[
        {
          id: name,
          label,
          values: points.map((point) => point[name]),
        },
      ]}
    />
  );
}

function ordinalChart(
  points: readonly InstancePoint[],
  names:
    | readonly [
        readonly [InstanceMetricName, string],
        readonly [InstanceMetricName, string],
      ]
    | readonly [
        readonly [InstanceMetricName, string],
        readonly [InstanceMetricName, string],
        readonly [InstanceMetricName, string],
      ],
) {
  if (!points.some((point) => names.some(([name]) => point[name] !== null)))
    return <MissingChart />;
  const timeAxis = {
    times: points.map((point) => Date.parse(point.sampled_at)),
    formatTick: (ms: number) =>
      formatChartTime(new Date(ms).toISOString(), "1h", "axis"),
    gapMs: 15_000,
  };
  const [first, second] = names;
  const firstSeries = {
    id: first[0],
    label: first[1],
    values: points.map((point) => point[first[0]]),
  };
  const secondSeries = {
    id: second[0],
    label: second[1],
    values: points.map((point) => point[second[0]]),
  };
  if (names.length === 3) {
    const third = names[2];
    return (
      <Chart
        ariaLabel={`${first[1]}、${second[1]} 与 ${third[1]}，最近 1 小时`}
        valueFormat={
          first[0].includes("seconds")
            ? { unit: "ms", scale: 1000 }
            : { unit: "个", compact: true, integer: true }
        }
        categories={points.map((point) =>
          formatChartTime(point.sampled_at, "1h", "detail"),
        )}
        timeAxis={timeAxis}
        kind="ordinal"
        series={[
          firstSeries,
          secondSeries,
          {
            id: third[0],
            label: third[1],
            values: points.map((point) => point[third[0]]),
          },
        ]}
      />
    );
  }
  return (
    <Chart
      ariaLabel={`${first[1]} 与 ${second[1]}，最近 1 小时`}
      valueFormat={
        first[0].includes("seconds")
          ? { unit: "ms", scale: 1000 }
          : { unit: "个", compact: true, integer: true }
      }
      categories={points.map((point) =>
        formatChartTime(point.sampled_at, "1h", "detail"),
      )}
      timeAxis={timeAxis}
      kind="ordinal"
      series={[firstSeries, secondSeries]}
    />
  );
}

function singleGpuChart(
  points: readonly GpuPoint[],
  name: GpuMetricName,
  label: string,
) {
  if (!points.some((point) => point[name] !== null)) return <MissingChart />;
  const timeAxis = {
    times: points.map((point) => Date.parse(point.sampled_at)),
    formatTick: (ms: number) =>
      formatChartTime(new Date(ms).toISOString(), "1h", "axis"),
    gapMs: 15_000,
  };
  return (
    <Chart
      ariaLabel={`${label}，最近 1 小时`}
      valueFormat={
        name === "utilization_percent"
          ? { unit: "%" }
          : name === "memory_used_bytes"
            ? { unit: "GiB", scale: 1 / 1024 ** 3 }
            : name === "temperature_celsius"
              ? { unit: "°C" }
              : { unit: "W" }
      }
      categories={points.map((point) =>
        formatChartTime(point.sampled_at, "1h", "detail"),
      )}
      timeAxis={timeAxis}
      kind="ordinal"
      series={[
        {
          id: name,
          label,
          values: points.map((point) => point[name]),
        },
      ]}
    />
  );
}

function MissingChart() {
  return (
    <span aria-label="暂无数据" className="pw-monitor-card__empty">
      —
    </span>
  );
}

function UnsupportedChart() {
  return (
    <span aria-label="当前 Runtime 不支持" className="pw-monitor-card__empty">
      不支持
    </span>
  );
}

function formatSeconds(value: number | null) {
  return value === null ? "—" : `${(value * 1_000).toFixed(0)} ms`;
}

function formatRate(value: number | null) {
  return value === null ? "—" : `${value.toFixed(1)} tokens/s`;
}

function formatCount(value: number | null) {
  return value === null ? "—" : value.toLocaleString("zh-CN");
}

function formatPercent(value: number | null) {
  return value === null ? "—" : `${(value * 100).toFixed(0)}%`;
}

function formatPercentValue(value: number | null | undefined) {
  return value == null ? "—" : `${value.toFixed(0)}%`;
}
