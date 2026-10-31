import { useQuery } from "@tanstack/react-query";

import type { ApiClient } from "../api/client";

export const AI_SERVICES_QUERY_KEY = ["ai-services"] as const;
export const AI_SERVICE_REFRESH_INTERVAL_MS = 5_000;

export function aiServiceQueryKey(id: string) {
  return ["ai-services", id] as const;
}

export function aiServicesQueryOptions(apiClient: ApiClient) {
  return {
    queryFn: () => apiClient.getAiServices(),
    queryKey: AI_SERVICES_QUERY_KEY,
    refetchInterval: AI_SERVICE_REFRESH_INTERVAL_MS,
  } as const;
}

export function aiServiceQueryOptions(apiClient: ApiClient, id: string) {
  return {
    queryFn: () => apiClient.getAiService(id),
    queryKey: aiServiceQueryKey(id),
    refetchInterval: AI_SERVICE_REFRESH_INTERVAL_MS,
  } as const;
}

export function useAiServices(apiClient: ApiClient) {
  return useQuery(aiServicesQueryOptions(apiClient));
}

export function useAiService(apiClient: ApiClient, id: string) {
  return useQuery(aiServiceQueryOptions(apiClient, id));
}
