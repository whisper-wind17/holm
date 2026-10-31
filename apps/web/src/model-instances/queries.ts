import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import type { ApiClient } from "../api/client";
import type { ModelInstanceLogEntry } from "../api/model-instance-contracts";

export const MODEL_INSTANCES_QUERY_KEY = ["model-instances"] as const;
export const INSTANCE_POLL_INTERVAL_MS = 5_000;

export type InstanceRealtimeStatus = "connecting" | "live" | "polling";

export function useModelInstances(apiClient: ApiClient) {
  const realtimeStatus = useInstanceRealtimeInvalidation();
  const query = useQuery({
    queryFn: () => apiClient.getModelInstances(),
    queryKey: MODEL_INSTANCES_QUERY_KEY,
    refetchInterval:
      realtimeStatus === "polling" ? INSTANCE_POLL_INTERVAL_MS : false,
  });
  return { ...query, realtimeStatus };
}

export function modelInstanceQueryKey(id: string) {
  return ["model-instances", id] as const;
}

export function useModelInstance(apiClient: ApiClient, id: string) {
  const realtimeStatus = useInstanceRealtimeInvalidation();
  const query = useQuery({
    queryFn: () => apiClient.getModelInstance(id),
    queryKey: modelInstanceQueryKey(id),
    refetchInterval:
      realtimeStatus === "polling" ? INSTANCE_POLL_INTERVAL_MS : false,
  });
  return { ...query, realtimeStatus };
}

function useInstanceRealtimeInvalidation(): InstanceRealtimeStatus {
  const queryClient = useQueryClient();
  const [status, setStatus] = useState<InstanceRealtimeStatus>("connecting");

  useEffect(() => {
    if (typeof EventSource === "undefined") {
      setStatus("polling");
      return;
    }

    let disposed = false;
    let source: EventSource | undefined;
    const refresh = () => {
      void queryClient.invalidateQueries({
        queryKey: MODEL_INSTANCES_QUERY_KEY,
      });
    };

    const connect = (): void => {
      const nextSource = new EventSource("/api/v1/events");
      source = nextSource;
      nextSource.onopen = () => {
        setStatus("live");
      };
      nextSource.onerror = () => {
        // Keep the source open: native EventSource reconnects with Last-Event-ID.
        setStatus("polling");
      };
      nextSource.addEventListener("instance.route_changed", refresh);
      nextSource.addEventListener("resync_required", () => {
        refresh();
        // The replay anchor is no longer usable. Reconnect without it after
        // refetching instead of sending the same stale Last-Event-ID forever.
        nextSource.close();
        if (!disposed) {
          setStatus("connecting");
          connect();
        }
      });
    };
    connect();
    return () => {
      disposed = true;
      source?.close();
    };
  }, [queryClient]);

  return status;
}

// Host Agent keeps the newest 500 lines and serves at most 100 per page.
export const INSTANCE_LOG_PAGE_SIZE = 100;
export const INSTANCE_LOG_MAX_PAGES = 5;

export interface InstanceLogSnapshot {
  entries: ModelInstanceLogEntry[];
  /** False when a later page failed and only the earlier lines are shown. */
  complete: boolean;
}

/**
 * reads the retained log window page by page. Log text lives only in
 * this query cache (memory); it is never written to storage or the URL.
 */
export function useInstanceLogs(
  apiClient: ApiClient,
  instanceId: string,
  options: { enabled?: boolean; refetchInterval?: number | false } = {},
) {
  return useQuery({
    enabled: options.enabled ?? true,
    queryFn: async (): Promise<InstanceLogSnapshot> => {
      const bySequence = new Map<number, ModelInstanceLogEntry>();
      let cursor: string | undefined;
      for (let page = 0; page < INSTANCE_LOG_MAX_PAGES; page += 1) {
        let result;
        try {
          result = await apiClient.getModelInstanceLogs(
            instanceId,
            cursor,
            INSTANCE_LOG_PAGE_SIZE,
          );
        } catch (error) {
          if (page === 0) throw error;
          return { complete: false, entries: sortedEntries(bySequence) };
        }
        for (const entry of result.entries)
          bySequence.set(entry.sequence, entry);
        if (result.next_cursor === null) break;
        cursor = result.next_cursor;
      }
      return { complete: true, entries: sortedEntries(bySequence) };
    },
    queryKey: ["model-instance-logs", instanceId, "recent"],
    refetchInterval: options.refetchInterval ?? false,
  });
}

function sortedEntries(entries: ReadonlyMap<number, ModelInstanceLogEntry>) {
  return [...entries.values()].sort((a, b) => a.sequence - b.sequence);
}
