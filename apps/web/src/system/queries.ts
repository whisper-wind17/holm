import { useQuery } from "@tanstack/react-query";

import type { ApiClient } from "../api/client";

export const SYSTEM_SERVER_QUERY_KEY = ["system", "server"] as const;
export const SYSTEM_COMPONENTS_QUERY_KEY = ["system", "components"] as const;
export const SYSTEM_QUERY_POLICY = { refetchInterval: false } as const;

export function useSystemQueries(apiClient: ApiClient, pollComponents = false) {
  const server = useQuery({
    queryFn: () => apiClient.getSystemServer(),
    queryKey: SYSTEM_SERVER_QUERY_KEY,
    refetchOnMount: "always",
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
    ...SYSTEM_QUERY_POLICY,
  });
  const components = useQuery({
    queryFn: () => apiClient.getSystemComponents(),
    queryKey: SYSTEM_COMPONENTS_QUERY_KEY,
    refetchInterval: pollComponents ? 30_000 : false,
  });
  return { components, server };
}

export function useSystemLiveQueries(apiClient: ApiClient) {
  const telemetry = useQuery({
    queryFn: () => apiClient.getGpuTelemetry(),
    queryKey: ["telemetry", "gpus"],
    refetchInterval: 10_000,
  });
  const instances = useQuery({
    queryFn: () => apiClient.getModelInstances(),
    queryKey: ["model-instances"],
    refetchInterval: 30_000,
  });
  return { telemetry, instances };
}
