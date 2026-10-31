import { z } from "zod";

export const TELEMETRY_STATUSES = [
  "available",
  "delayed",
  "unavailable",
] as const;

const telemetryQualitySchema = z.enum(["full", "partial"]);
const telemetryStatusSchema = z.enum(TELEMETRY_STATUSES);
const sampledAtSchema = z.iso.datetime({ offset: true }).nullable();
const metricSchema = z.number().nonnegative().nullable();
export const CANONICAL_RUNTIME_METRICS = [
  "runtime_ttft_seconds",
  "runtime_tpot_seconds",
  "runtime_e2e_latency_seconds",
  "runtime_output_tokens_total",
  "runtime_requests_total",
  "runtime_requests_running",
  "runtime_requests_waiting",
  "runtime_queue_time_seconds",
  "runtime_kv_cache_usage_ratio",
  "runtime_errors_total",
] as const;
const canonicalRuntimeMetricSchema = z.enum(CANONICAL_RUNTIME_METRICS);

const instanceMetricValuesSchema = z.strictObject({
  kv_cache_usage_ratio: metricSchema,
  output_tps: metricSchema,
  queue_time_p50_seconds: metricSchema,
  queue_time_p95_seconds: metricSchema,
  running: metricSchema,
  ttft_p50_seconds: metricSchema,
  ttft_p95_seconds: metricSchema,
  ttft_p99_seconds: metricSchema,
  unsupported: z.array(canonicalRuntimeMetricSchema),
  waiting: metricSchema,
});

const instancePointSchema = instanceMetricValuesSchema.extend({
  sampled_at: z.iso.datetime({ offset: true }),
  quality: telemetryQualitySchema,
  telemetry_status: telemetryStatusSchema,
});

const requestWindowSchema = z.strictObject({
  requests: z.number().int().nonnegative().nullable(),
  error_rate: metricSchema,
  ttft_p95_ms: metricSchema,
  started_at: sampledAtSchema,
});

export const instanceTelemetrySchema = z.strictObject({
  quality: telemetryQualitySchema,
  window_1h: requestWindowSchema,
  current: instanceMetricValuesSchema,
  instance_id: z.uuid(),
  sampled_at: sampledAtSchema,
  series: z.array(instancePointSchema),
  telemetry_status: telemetryStatusSchema,
});

const gpuMetricValuesSchema = z.strictObject({
  health: z.string().nullable(),
  memory_used_bytes: z.number().int().nonnegative().nullable(),
  power_watts: metricSchema,
  temperature_celsius: z.number().nullable(),
  utilization_percent: metricSchema,
});

const gpuPointSchema = gpuMetricValuesSchema.extend({
  sampled_at: z.iso.datetime({ offset: true }),
  quality: telemetryQualitySchema,
  telemetry_status: telemetryStatusSchema,
});

const gpuTelemetrySchema = z.strictObject({
  quality: telemetryQualitySchema,
  current: gpuMetricValuesSchema,
  sampled_at: sampledAtSchema,
  series: z.array(gpuPointSchema),
  telemetry_status: telemetryStatusSchema,
  uuid: z.string().min(1),
});

export const gpuTelemetryListSchema = z.strictObject({
  quality: telemetryQualitySchema,
  items: z.array(gpuTelemetrySchema),
  sampled_at: sampledAtSchema,
  telemetry_status: telemetryStatusSchema,
});

const serviceMetricValuesSchema = z.strictObject({
  ttft_p50_ms: metricSchema,
  ttft_p95_ms: metricSchema,
  ttft_p99_ms: metricSchema,
  tpot_p50_ms: metricSchema,
  tpot_p95_ms: metricSchema,
  e2e_latency_p50_ms: metricSchema,
  e2e_latency_p95_ms: metricSchema,
  e2e_latency_p99_ms: metricSchema,
  output_tps_avg: metricSchema,
  output_tps_max: metricSchema,
  inflight_avg: metricSchema,
  inflight_max: metricSchema,
  inflight_last: metricSchema,
  request_rate_avg: metricSchema,
  accepted_requests: z.number().int().nonnegative().nullable(),
  rejected_requests: z.number().int().nonnegative().nullable(),
  client_disconnects: z.number().int().nonnegative().nullable(),
  error_requests: z.number().int().nonnegative().nullable(),
  error_rate: metricSchema,
});

export const serviceTelemetrySchema = z.strictObject({
  window_1h: requestWindowSchema,
  service_id: z.uuid(),
  sampled_at: sampledAtSchema,
  telemetry_status: telemetryStatusSchema,
  quality: telemetryQualitySchema,
  current: serviceMetricValuesSchema,
  series: z.array(
    serviceMetricValuesSchema.extend({
      sampled_at: z.iso.datetime({ offset: true }),
      telemetry_status: telemetryStatusSchema,
      quality: telemetryQualitySchema,
    }),
  ),
});

const overviewValuesSchema = z.strictObject({
  requests_1h: z.number().int().nonnegative().nullable(),
  error_rate_1h: metricSchema,
  window_started_at: z.iso.datetime({ offset: true }).nullable(),
  error_rate: metricSchema,
  gpu_allocated: z.number().int().nonnegative().nullable(),
  gpu_available: z.number().int().nonnegative().nullable(),
  gpu_total: z.number().int().nonnegative().nullable(),
  requests_running: metricSchema,
  requests_waiting: metricSchema,
  ttft_p95_ms: metricSchema,
  services_available: z.number().int().nonnegative().nullable(),
  services_starting: z.number().int().nonnegative().nullable(),
  services_total: z.number().int().nonnegative().nullable(),
  services_unavailable: z.number().int().nonnegative().nullable(),
});

export const overviewTelemetrySchema = z.strictObject({
  current: overviewValuesSchema,
  sampled_at: sampledAtSchema,
  telemetry_status: telemetryStatusSchema,
});

// the closed attention contract shared with the Overview redesign.
export const ATTENTION_KINDS = [
  "service_unavailable",
  "instance_failed",
  "instance_unhealthy",
  "gpu_unhealthy",
  "download_failed",
  "telemetry_interrupted",
  "unknown_requests",
] as const;

const overviewAttentionItemSchema = z.strictObject({
  kind: z.enum(ATTENTION_KINDS),
  severity: z.enum(["negative", "notice"]),
  target_id: z.string().nullable(),
  target_name: z.string().nullable(),
  count: z.number().int().positive(),
  latest_at: z.iso.datetime({ offset: true }).nullable(),
  detail: z.string().nullable(),
});

const overviewAttentionSchema = z.strictObject({
  items: z.array(overviewAttentionItemSchema),
});

export type TelemetryStatus = z.infer<typeof telemetryStatusSchema>;
export type InstanceMetricValues = z.infer<typeof instanceMetricValuesSchema>;
export type InstanceTelemetry = z.infer<typeof instanceTelemetrySchema>;
export type GpuMetricValues = z.infer<typeof gpuMetricValuesSchema>;
export type GpuTelemetry = z.infer<typeof gpuTelemetrySchema>;
export type GpuTelemetryList = z.infer<typeof gpuTelemetryListSchema>;
export type OverviewTelemetry = z.infer<typeof overviewTelemetrySchema>;
export type ServiceTelemetry = z.infer<typeof serviceTelemetrySchema>;
export type OverviewAttentionItem = z.infer<typeof overviewAttentionItemSchema>;
export type OverviewAttention = z.infer<typeof overviewAttentionSchema>;

export function parseInstanceTelemetry(value: unknown): InstanceTelemetry {
  return instanceTelemetrySchema.parse(value);
}

export function parseGpuTelemetryList(value: unknown): GpuTelemetryList {
  return gpuTelemetryListSchema.parse(value);
}

export function parseOverviewTelemetry(value: unknown): OverviewTelemetry {
  return overviewTelemetrySchema.parse(value);
}

export function parseServiceTelemetry(value: unknown): ServiceTelemetry {
  return serviceTelemetrySchema.parse(value);
}

export function parseOverviewAttention(value: unknown): OverviewAttention {
  return overviewAttentionSchema.parse(value);
}
