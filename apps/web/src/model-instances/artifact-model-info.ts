import { useQuery } from "@tanstack/react-query";

import type { ApiClient } from "../api/client";

/** precision, quantization and context length exist only on the artifact detail. */
export function useArtifactModelInfo(
  apiClient: ApiClient,
  artifactId: string | undefined,
) {
  const detail = useQuery({
    enabled: artifactId !== undefined && artifactId !== "",
    queryFn: () => apiClient.artifactDetail(artifactId ?? ""),
    queryKey: ["model-deploy", "artifact-detail", artifactId],
  });
  const pending = detail.isPending && detail.fetchStatus !== "idle";
  const model = detail.data?.model;
  return {
    pending,
    contextLength: model?.context_length ?? null,
    precision: model?.precision ?? null,
    quantization: model?.quantization ?? null,
  };
}

/** Null or failed lookups read as unrecognized, never as 0. */
export function modelInfoText(
  value: string | number | null,
  pending: boolean,
): string {
  if (pending) return "读取中";
  if (value === null) return "未识别";
  return typeof value === "number" ? value.toLocaleString("zh-CN") : value;
}

/** Weight-only lower bound; KV Cache cannot be estimated from the available data. */
export const WEIGHT_MEMORY_FRACTION = 0.9;

export function minimumGpuCount(
  sizeBytes: number,
  memoryBytesPerGpu: number,
): number | undefined {
  if (!(memoryBytesPerGpu > 0)) return undefined;
  return Math.max(
    1,
    Math.ceil(sizeBytes / (memoryBytesPerGpu * WEIGHT_MEMORY_FRACTION)),
  );
}
