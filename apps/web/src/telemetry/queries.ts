import { useQuery } from "@tanstack/react-query";

import type { ApiClient } from "../api/client";

export const TELEMETRY_REFRESH_INTERVAL_MS = 5_000;

export function instanceTelemetryQueryKey(id: string) {
  return ["telemetry", "instances", id] as const;
}

export type ServiceTelemetryRange = "1h" | "6h" | "24h" | "7d";

export function serviceTelemetryQueryKey(
  id: string,
  range: ServiceTelemetryRange,
) {
  return ["telemetry", "services", id, range] as const;
}

export function useServiceTelemetry(
  apiClient: ApiClient,
  id: string,
  range: ServiceTelemetryRange,
) {
  return useQuery({
    enabled: id.length > 0,
    queryFn: () => apiClient.getServiceTelemetry(id, range),
    queryKey: serviceTelemetryQueryKey(id, range),
    refetchInterval: TELEMETRY_REFRESH_INTERVAL_MS,
  });
}

export function useInstanceTelemetry(apiClient: ApiClient, id: string) {
  return useQuery({
    enabled: id.length > 0,
    queryFn: () => apiClient.getInstanceTelemetry(id),
    queryKey: instanceTelemetryQueryKey(id),
    refetchInterval: TELEMETRY_REFRESH_INTERVAL_MS,
  });
}

export function useGpuTelemetry(apiClient: ApiClient) {
  return useQuery({
    queryFn: () => apiClient.getGpuTelemetry(),
    queryKey: ["telemetry", "gpus"],
    refetchInterval: TELEMETRY_REFRESH_INTERVAL_MS,
  });
}

export const ATTENTION_REFRESH_INTERVAL_MS = 30_000;

export function useOverviewAttention(apiClient: ApiClient) {
  return useQuery({
    queryFn: () => apiClient.getOverviewAttention(),
    queryKey: ["overview", "attention"],
    refetchInterval: ATTENTION_REFRESH_INTERVAL_MS,
  });
}

export function useOverviewTelemetry(apiClient: ApiClient) {
  return useQuery({
    queryFn: () => apiClient.getTelemetryOverview(),
    queryKey: ["telemetry", "overview"],
    refetchInterval: TELEMETRY_REFRESH_INTERVAL_MS,
  });
}
