import {
  useInfiniteQuery,
  useQuery,
  type InfiniteData,
} from "@tanstack/react-query";

import type { ApiClient } from "../api/client";
import type {
  DownloadStatusCounts,
  DownloadTaskPage,
  DownloadTaskStatus,
  ModelArtifactPage,
  ModelArtifactStatus,
  ModelOriginType,
} from "../api/model-acquisition-contracts";

/** Prefix of the "in progress and failed" list; polled and refetched together. */
export const DOWNLOAD_TASKS_QUERY_KEY = ["models", "downloads"] as const;
/** Deliberately outside the prefix above: loaded only after the group is expanded. */
export const FINISHED_DOWNLOADS_QUERY_KEY = [
  "models",
  "downloads-finished",
] as const;
export const DOWNLOAD_COUNTS_QUERY_KEY = ["models", "download-counts"] as const;
export const MODEL_ARTIFACTS_QUERY_KEY = ["models", "artifacts"] as const;
export const DOWNLOAD_POLL_INTERVAL_MS = 5_000;
export const ARTIFACT_POLL_INTERVAL_MS = 10_000;

/** in-progress tasks and failed tasks are always listed. */
export const ACTIVE_DOWNLOAD_STATUSES = [
  "Queued",
  "Resolving",
  "Downloading",
  "Paused",
  "Verifying",
  "Publishing",
] as const satisfies readonly DownloadTaskStatus[];
export const LIVE_DOWNLOAD_STATUSES = [
  ...ACTIVE_DOWNLOAD_STATUSES,
  "Failed",
] as const satisfies readonly DownloadTaskStatus[];
export const FINISHED_DOWNLOAD_STATUSES = [
  "Completed",
  "Cancelled",
] as const satisfies readonly DownloadTaskStatus[];

export interface ArtifactFilters {
  search?: string;
  source?: ModelOriginType;
  status?: ModelArtifactStatus;
}

export function useModelQueries(
  apiClient: ApiClient,
  filters: ArtifactFilters,
) {
  const downloads = useInfiniteQuery<
    DownloadTaskPage,
    Error,
    InfiniteData<DownloadTaskPage>,
    typeof DOWNLOAD_TASKS_QUERY_KEY,
    string | undefined
  >({
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam }) =>
      apiClient.getDownloadTasks(pageParam, LIVE_DOWNLOAD_STATUSES),
    queryKey: DOWNLOAD_TASKS_QUERY_KEY,
    refetchInterval: DOWNLOAD_POLL_INTERVAL_MS,
  });
  const artifactQueryKey = [...MODEL_ARTIFACTS_QUERY_KEY, filters] as const;
  const artifacts = useInfiniteQuery<
    ModelArtifactPage,
    Error,
    InfiniteData<ModelArtifactPage>,
    typeof artifactQueryKey,
    string | undefined
  >({
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam }) =>
      apiClient.getModelArtifacts({ cursor: pageParam, ...filters }),
    queryKey: artifactQueryKey,
    refetchInterval: ARTIFACT_POLL_INTERVAL_MS,
  });
  return { artifacts, downloads };
}

/** Counts per status; they drive the section header and the "已结束" footer. */
export function useDownloadStatusCounts(apiClient: ApiClient) {
  return useQuery<DownloadStatusCounts>({
    queryFn: () => apiClient.downloadStatusCounts(),
    queryKey: DOWNLOAD_COUNTS_QUERY_KEY,
    refetchInterval: DOWNLOAD_POLL_INTERVAL_MS,
  });
}

/** Completed and Cancelled tasks, newest first; fetched only while expanded. */
export function useFinishedDownloads(apiClient: ApiClient, enabled: boolean) {
  return useInfiniteQuery<
    DownloadTaskPage,
    Error,
    InfiniteData<DownloadTaskPage>,
    typeof FINISHED_DOWNLOADS_QUERY_KEY,
    string | undefined
  >({
    enabled,
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam }) =>
      apiClient.getDownloadTasks(pageParam, FINISHED_DOWNLOAD_STATUSES),
    queryKey: FINISHED_DOWNLOADS_QUERY_KEY,
    refetchInterval: DOWNLOAD_POLL_INTERVAL_MS,
  });
}

/** Shared by the list-row and detail-page 重新校验 actions; polls only while a job is active. */
export function useArtifactVerification(apiClient: ApiClient, id: string) {
  return useQuery({
    queryKey: ["artifact-verification", id],
    queryFn: () => apiClient.getArtifactVerificationStatus(id),
    refetchInterval: (query) =>
      query.state.data?.status === "Queued" ||
      query.state.data?.status === "Running"
        ? 1000
        : false,
  });
}
