import { useQuery } from "@tanstack/react-query";

import type { ApiClient } from "../api/client";

export const API_KEYS_QUERY_KEY = ["api-keys"] as const;

export const API_KEY_REFRESH_INTERVAL_MS = 5_000;

export function useApiKeyTodayUsage(apiClient: ApiClient, userId?: string) {
  const now = new Date();
  const todayStart = new Date(
    now.getFullYear(),
    now.getMonth(),
    now.getDate(),
  ).toISOString();
  const timeZone = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  return useQuery({
    enabled: userId !== undefined,
    queryFn: () => {
      // Personal usage must never become an unfiltered administrator query.
      if (userId === undefined)
        throw new Error("personal usage requires a user");
      return apiClient.getUsage({
        from: todayStart,
        to: new Date().toISOString(),
        groupBy: "api_key",
        timeZone,
        userId,
      });
    },
    queryKey: ["api-key-today-usage", userId, todayStart, timeZone],
    refetchInterval: 30_000,
    refetchIntervalInBackground: false,
  });
}

export function useApiKeys(apiClient: ApiClient) {
  return useQuery({
    queryFn: () => apiClient.getApiKeys(),
    queryKey: API_KEYS_QUERY_KEY,
    refetchInterval: API_KEY_REFRESH_INTERVAL_MS,
  });
}
