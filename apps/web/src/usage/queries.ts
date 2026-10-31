import { useQuery } from "@tanstack/react-query";

import type { ApiClient } from "../api/client";
import type { UsageFilters } from "../api/usage-contracts";

export function useUsageSummary(
  apiClient: ApiClient,
  timeZone: string,
  enabled = true,
) {
  return useQuery({
    enabled,
    queryFn: () => apiClient.getUsageSummary(timeZone),
    queryKey: ["usage-summary", timeZone],
  });
}

export function useUsage(
  apiClient: ApiClient,
  filters: UsageFilters,
  enabled = true,
) {
  return useQuery({
    enabled,
    queryFn: () => apiClient.getUsage(filters),
    queryKey: ["usage", filters],
  });
}
