import { useQuery } from "@tanstack/react-query";

import type { ApiClient } from "../api/client";

export const MODEL_SOURCES_QUERY_KEY = ["system", "model-sources"] as const;
export const MODEL_SOURCES_QUERY_POLICY = { refetchInterval: false } as const;

export function useModelSourcesQuery(apiClient: ApiClient) {
  return useQuery({
    queryFn: () => apiClient.getModelSources(),
    queryKey: MODEL_SOURCES_QUERY_KEY,
    ...MODEL_SOURCES_QUERY_POLICY,
  });
}
