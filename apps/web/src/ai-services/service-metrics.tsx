import { useQueries } from "@tanstack/react-query";
import { useMemo } from "react";
import type { ApiClient } from "../api/client";
import type { AiService } from "../api/ai-service-contracts";
import type {
  InstanceTelemetry,
  ServiceTelemetry,
} from "../api/telemetry-contracts";
import { MissingValue } from "../components";
import {
  instanceTelemetryQueryKey,
  serviceTelemetryQueryKey,
  TELEMETRY_REFRESH_INTERVAL_MS,
} from "../telemetry/queries";

export function useServiceListMetrics(
  apiClient: ApiClient,
  rows: readonly AiService[],
) {
  const telemetryQueries = useQueries({
    queries: [
      ...new Set(rows.map((service) => service.primary_instance_id)),
    ].map((id) => ({
      queryFn: () => apiClient.getInstanceTelemetry(id),
      queryKey: instanceTelemetryQueryKey(id),
      refetchInterval: TELEMETRY_REFRESH_INTERVAL_MS,
    })),
  });
  const telemetryByInstanceId = useMemo(() => {
    const values = new Map<string, InstanceTelemetry>();
    for (const query of telemetryQueries) {
      if (query.data !== undefined)
        values.set(query.data.instance_id, query.data);
    }
    return values;
  }, [telemetryQueries]);
  const serviceTelemetryQueries = useQueries({
    queries: rows.map(({ id }) => ({
      queryFn: () => apiClient.getServiceTelemetry(id),
      queryKey: serviceTelemetryQueryKey(id, "1h"),
      refetchInterval: TELEMETRY_REFRESH_INTERVAL_MS,
    })),
  });
  const telemetryByServiceId = new Map<string, ServiceTelemetry>();
  for (const query of serviceTelemetryQueries) {
    if (query.data !== undefined)
      telemetryByServiceId.set(query.data.service_id, query.data);
  }
  return {
    telemetryByInstanceId,
    telemetryByServiceId,
    telemetryQueries,
    serviceTelemetryQueries,
  };
}

export function ServiceWindowMetric({
  telemetry,
  metric,
}: {
  telemetry: ServiceTelemetry | undefined;
  metric: "ttft_p95_ms" | "error_rate";
}) {
  return (
    <ServiceMetric
      value={telemetry?.window_1h[metric]}
      reason={
        telemetry?.window_1h.requests === 0
          ? "近 1 小时无调用"
          : "服务遥测未采集或窗口内无请求"
      }
      digits={metric === "error_rate" ? 1 : 0}
      scale={metric === "error_rate" ? 100 : 1}
      suffix={metric === "error_rate" ? "%" : " ms"}
    />
  );
}

export function ServiceMetric({
  value,
  digits = 0,
  scale = 1,
  suffix = "",
  reason = "服务遥测未采集或窗口内无请求",
}: {
  value: number | null | undefined;
  digits?: number;
  scale?: number;
  suffix?: string;
  reason?: string;
}) {
  return value == null ? (
    <MissingValue kind="unavailable" reason={reason} />
  ) : (
    <span>
      {(value * scale).toFixed(digits)}
      {suffix ? <span className="pw-service-metric-unit">{suffix}</span> : null}
    </span>
  );
}
