// FX-UI-10: synthetic values in the 测试端口 response shape recorded in UI-10.md.
export const UI10_INSTANCE_ID = "0198f58a-2586-7e37-bbf5-000000000010";
const sampledAt = "2026-10-09T04:00:00Z";
const instanceCurrent = {
  running: 7,
  waiting: 2,
  output_tps: 86.4,
  kv_cache_usage_ratio: 0.27,
  ttft_p50_seconds: 0.004,
  ttft_p95_seconds: 0.009,
  ttft_p99_seconds: 0.015,
  queue_time_p50_seconds: null,
  queue_time_p95_seconds: null,
  unsupported: [],
};
const gpuCurrent = {
  health: "GPU_HEALTH_STATE_HEALTHY",
  utilization_percent: 43,
  memory_used_bytes: 4294967296,
  temperature_celsius: 56,
  power_watts: 127,
};
export const UI10_INSTANCE_RESPONSE = {
  instance_id: UI10_INSTANCE_ID,
  sampled_at: sampledAt,
  quality: "full",
  telemetry_status: "available",
  current: instanceCurrent,
  series: [
    {
      ...instanceCurrent,
      sampled_at: sampledAt,
      quality: "full",
      telemetry_status: "available",
    },
  ],
  window_1h: {
    requests: 200,
    error_rate: 0.035,
    ttft_p95_ms: 73,
    started_at: "2026-10-09T03:00:00Z",
  },
};
export const UI10_GPU_RESPONSE = {
  sampled_at: sampledAt,
  quality: "full",
  telemetry_status: "available",
  items: [
    {
      uuid: "GPU-UI10",
      sampled_at: sampledAt,
      quality: "full",
      telemetry_status: "available",
      current: gpuCurrent,
      series: [
        {
          ...gpuCurrent,
          sampled_at: sampledAt,
          quality: "full",
          telemetry_status: "available",
        },
      ],
    },
  ],
};
