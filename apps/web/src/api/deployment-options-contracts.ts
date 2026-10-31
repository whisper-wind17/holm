import { z } from "zod";

import { RUNTIME_TYPES } from "./model-instance-contracts";

const runtimeTypeSchema = z.enum(RUNTIME_TYPES);
const identifiedContextLimitSchema = z.strictObject({
  state: z.literal("identified"),
  value: z.number().int().positive(),
});
const unrecognizedContextLimitSchema = z.strictObject({
  state: z.literal("unrecognized"),
});

const deploymentRuntimeOptionSchema = z.strictObject({
  certified_gpu_counts: z.array(z.number().int().positive()),
  context_lengths: z.array(z.number().int().positive()),
  default_context_length: z.number().int().positive().nullable(),
  max_gpus_per_instance: z.number().int().positive(),
  name: runtimeTypeSchema,
  version: z.string().min(1),
});

const modelDeploymentOptionsSchema = z.strictObject({
  model_context_limit: z.union([
    identifiedContextLimitSchema,
    unrecognizedContextLimitSchema,
  ]),
  runtimes: z.array(deploymentRuntimeOptionSchema),
});

export class DeploymentOptionsContractError extends Error {
  readonly code = "deployment_options_contract_error";

  constructor() {
    super("server returned unsupported deployment options");
    this.name = "DeploymentOptionsContractError";
  }
}

export type DeploymentRuntimeOption = z.infer<
  typeof deploymentRuntimeOptionSchema
>;
export type ModelDeploymentOptions = z.infer<
  typeof modelDeploymentOptionsSchema
>;

export function parseModelDeploymentOptions(value: unknown) {
  try {
    return modelDeploymentOptionsSchema.parse(value);
  } catch {
    throw new DeploymentOptionsContractError();
  }
}
