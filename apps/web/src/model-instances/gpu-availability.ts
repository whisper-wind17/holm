import type { ModelInstance } from "../api/model-instance-contracts";

const OCCUPIED_GPU_STATUSES = new Set<ModelInstance["status"]>([
  "Restarting",
  "Running",
  "Starting",
  "Stopping",
  "Unhealthy",
]);

// Waiting/terminal instances keep desired UUIDs after releasing allocations.
export function occupiedGpuInstances(
  instances: readonly ModelInstance[],
  excludedInstanceId?: string,
): ReadonlyMap<string, string> {
  const occupied = new Map<string, string>();
  for (const instance of instances) {
    if (
      instance.id === excludedInstanceId ||
      !OCCUPIED_GPU_STATUSES.has(instance.status)
    )
      continue;
    for (const uuid of instance.gpu_uuids) occupied.set(uuid, instance.id);
  }
  return occupied;
}
