import { useQuery } from "@tanstack/react-query";

import type { ApiClient } from "../api/client";
import type { RequestRecordFilters } from "../api/request-record-contracts";

export function useRequestRecords(
  apiClient: ApiClient,
  filters: RequestRecordFilters,
) {
  return useQuery({
    queryFn: () => apiClient.getRequestRecords(filters),
    queryKey: ["request-records", filters],
  });
}

export interface RequestRecordKeyWindow {
  from: string;
  timeZone: string;
  to: string;
}

/**
 * Administrators have no full API key list endpoint (api_key_routes.py only
 * serves /me/api-keys), so the API key filter offers the keys that have usage
 * in the retention window, taken from the usage report grouped by API key
 * (the same source the Usage page key filter uses).
 */
export function useRequestRecordKeyOptions(
  apiClient: ApiClient,
  serviceId: string | undefined,
  range: RequestRecordKeyWindow,
) {
  return useQuery({
    queryFn: () =>
      apiClient.getUsage({
        from: range.from,
        groupBy: "api_key",
        ...(serviceId === undefined ? {} : { serviceId }),
        timeZone: range.timeZone,
        to: range.to,
      }),
    queryKey: ["request-record-key-options", serviceId ?? null, range],
    select: (report) =>
      report.details.flatMap((group) =>
        group.group_id === null
          ? []
          : [
              {
                id: group.group_id,
                label: `${group.display_name}${group.is_deleted ? "（已移除）" : ""}`,
              },
            ],
      ),
  });
}
