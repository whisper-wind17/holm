import { useQuery } from "@tanstack/react-query";

import { ApiError, type ApiClient } from "../api/client";

export const BOOTSTRAP_GATE_QUERY_KEY = ["bootstrap", "gate"] as const;
export const BOOTSTRAP_CHECK_QUERY_KEY = ["bootstrap", "check"] as const;

/** Whether this machine still has to be initialized. */
export type BootstrapGate = "initialized" | "uninitialized";

/**
 * Ask `/bootstrap/status` which of the two first-run worlds we are in.
 *
 * The API closes all three Bootstrap paths with **404** once the platform is
 * initialized, so 404 is the ordinary answer for every machine that has ever
 * been set up — not an error, and never an error page. Without a Token an
 * uninitialized platform answers 401 ("this Token is not usable"), which is why
 * 401 is what proves the flow is still open.
 *
 * Anything else — 500, a network failure, an unparsable body — resolves to
 * `initialized`, i.e. the login page. The wizard is the higher-privilege path
 * (it creates the first administrator), so an ambiguous signal must not be
 * allowed to offer it.
 */
export function useBootstrapGate(apiClient: ApiClient, enabled = true) {
  return useQuery<BootstrapGate>({
    enabled,
    queryFn: async () => {
      try {
        await apiClient.getBootstrapStatus("");
        return "uninitialized";
      } catch (error) {
        if (error instanceof ApiError && error.status === 401) {
          return "uninitialized";
        }
        return "initialized";
      }
    },
    queryKey: BOOTSTRAP_GATE_QUERY_KEY,
    retry: false,
  });
}

/**
 * Initialization system detection. The verdict is recomputed by the server on every call, so
 * "重新检测" is a refetch of this query and nothing else.
 */
export function useBootstrapChecks(
  apiClient: ApiClient,
  token: string,
  attempt: number,
  enabled: boolean,
) {
  return useQuery({
    enabled,
    queryFn: () => apiClient.runBootstrapChecks(token),
    // `attempt` changes when a different Token is presented, so a refusal
    // cached against the previous one cannot outlive it.
    queryKey: [...BOOTSTRAP_CHECK_QUERY_KEY, attempt],
    retry: false,
  });
}
