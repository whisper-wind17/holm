import { describe, expect, it } from "vitest";

import {
  DeploymentOptionsContractError,
  parseModelDeploymentOptions,
} from "./deployment-options-contracts";

function validResponse() {
  return {
    model_context_limit: { state: "identified", value: 8192 },
    runtimes: [
      {
        certified_gpu_counts: [1, 8],
        context_lengths: [2048, 4096, 8192],
        default_context_length: 4096,
        max_gpus_per_instance: 8,
        name: "vllm",
        version: "0.29.0",
      },
    ],
  };
}

describe("M3_10 deployment-options contract", () => {
  it("accepts the closed safe projection and preserves unknown model limits", () => {
    expect(parseModelDeploymentOptions(validResponse())).toEqual(
      validResponse(),
    );
    expect(
      parseModelDeploymentOptions({
        ...validResponse(),
        model_context_limit: { state: "unrecognized" },
      }).model_context_limit,
    ).toEqual({ state: "unrecognized" });
  });

  it.each([
    ["executable", { executable: "/workspace/private/runtime" }],
    ["argv token", { arguments: ["--tensor-parallel-size"] }],
  ])("rejects a leaked %s field", (_label, leaked) => {
    const response = validResponse();
    const runtime = response.runtimes[0];
    if (runtime === undefined) throw new Error("runtime fixture is missing");
    const unsafeResponse = {
      ...response,
      runtimes: [{ ...runtime, ...leaked }],
    };
    expect(() => parseModelDeploymentOptions(unsafeResponse)).toThrow(
      DeploymentOptionsContractError,
    );
  });
});
