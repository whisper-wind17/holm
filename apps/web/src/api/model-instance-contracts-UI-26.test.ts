import { describe, expect, it } from "vitest";
import {
  ModelInstanceContractError,
  parseModelInstance,
  parseModelInstanceList,
} from "./model-instance-contracts";

// FX-UI-26: synthetic response following the ModelInstanceResponse contract.
const instance = {
  id: "43000000-0000-4000-8000-000000000001",
  artifact_id: "42000000-0000-4000-8000-000000000001",
  name: "FX-UI-26 instance",
  runtime_type: "vllm",
  gpu_uuids: ["GPU-UI-26"],
  tensor_parallel_size: 1,
  context_length: 4096,
  gpu_memory_utilization: 0.9,
  internal_model_name: "mi_ui26",
  internal_endpoint: null,
  status: "Restarting",
  error_code: null,
  config_version: 1,
  restart_count: 2,
  updated_at: "2026-10-14T00:00:00Z",
};

describe("UI-26 restart count contract", () => {
  it.each([0, 1, 2, 3])(
    "parses restart_count %i in detail and list",
    (restart_count) => {
      const value = { ...instance, restart_count };
      expect(parseModelInstance(value).restart_count).toBe(restart_count);
      expect(
        parseModelInstanceList({ items: [value] }).items[0]?.restart_count,
      ).toBe(restart_count);
    },
  );

  it.each([undefined, null, -1, 0.5, "2"])(
    "rejects invalid restart_count %s",
    (restart_count) => {
      const value = { ...instance, restart_count };
      expect(() => parseModelInstance(value)).toThrow(
        ModelInstanceContractError,
      );
      expect(() => parseModelInstanceList({ items: [value] })).toThrow(
        ModelInstanceContractError,
      );
    },
  );

  it("requires restart_count and still rejects unknown fields", () => {
    const missing: Record<string, unknown> = { ...instance };
    delete missing.restart_count;
    expect(() => parseModelInstance(missing)).toThrow(
      ModelInstanceContractError,
    );
    expect(() => parseModelInstance({ ...instance, unknown: true })).toThrow(
      ModelInstanceContractError,
    );
  });
});
