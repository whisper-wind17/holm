import { z } from "zod";

export const MODEL_INSTANCE_STATUSES = [
  "WaitingForResource",
  "Starting",
  "Running",
  "Restarting",
  "Unhealthy",
  "Stopping",
  "Stopped",
  "Failed",
] as const;

export const RUNTIME_TYPES = ["vllm", "sglang"] as const;

export const MODEL_INSTANCE_EVENT_TYPES = [
  "StatusChanged",
  "RestartRequested",
  "RetryRequested",
  "DeploymentCancelled",
  "GpuSelectionChanged",
  "WaitingForGpu",
] as const;

const modelInstanceStatusSchema = z.enum(MODEL_INSTANCE_STATUSES);
const runtimeTypeSchema = z.enum(RUNTIME_TYPES);

export const instanceNameSchema = z
  .string()
  .trim()
  .refine(
    (name) =>
      !name.includes("\0") &&
      Array.from(name).length >= 2 &&
      Array.from(name).length <= 64,
    "实例名称须为 2～64 个字符，请修改名称。",
  );

const modelInstanceSchema = z.strictObject({
  artifact_id: z.uuid(),
  config_version: z.number().int().positive(),
  context_length: z.number().int().positive(),
  error_code: z.string().nullable(),
  gpu_memory_utilization: z.number().positive().max(1),
  gpu_uuids: z.array(z.string().min(1)),
  id: z.uuid(),
  internal_endpoint: z.string().nullable(),
  internal_model_name: z.string().min(1),
  name: instanceNameSchema,
  runtime_type: runtimeTypeSchema,
  status: modelInstanceStatusSchema,
  restart_count: z.number().int().nonnegative(),
  updated_at: z.iso.datetime({ offset: true }),
  tensor_parallel_size: z.number().int().positive(),
});

const modelInstanceListSchema = z.strictObject({
  items: z.array(modelInstanceSchema),
});

const modelInstanceLogEntrySchema = z.strictObject({
  original_character_count: z.number().int().nonnegative(),
  sequence: z.number().int().nonnegative(),
  text: z.string(),
  truncated: z.boolean(),
});

const modelInstanceLogPageSchema = z.strictObject({
  entries: z.array(modelInstanceLogEntrySchema).max(100),
  next_cursor: z.string().min(1).nullable(),
});

const modelInstanceEventSchema = z.strictObject({
  actor: z.enum(["admin", "controller", "system"]),
  detail: z.strictObject({}),
  event_type: z.enum(MODEL_INSTANCE_EVENT_TYPES),
  from_status: modelInstanceStatusSchema.nullable(),
  id: z.uuid(),
  occurred_at: z.iso.datetime({ offset: true }),
  to_status: modelInstanceStatusSchema.nullable(),
});

const modelInstanceEventPageSchema = z.strictObject({
  items: z.array(modelInstanceEventSchema).max(100),
  next_cursor: z.string().min(1).nullable(),
});

const createModelInstanceRequestSchema = z
  .strictObject({
    name: instanceNameSchema.optional(),
    artifact_id: z.uuid(),
    context_length: z.number().int().positive(),
    gpu_memory_utilization: z.number().positive().max(1),
    gpu_uuids: z.array(z.string().min(1)).min(1),
    runtime_type: runtimeTypeSchema.default("vllm"),
    tensor_parallel_size: z.number().int().positive(),
  })
  .refine(
    ({ gpu_uuids, tensor_parallel_size }) =>
      new Set(gpu_uuids).size === gpu_uuids.length &&
      tensor_parallel_size === gpu_uuids.length,
  );

export class ModelInstanceContractError extends Error {
  readonly code = "model_instance_contract_error";

  constructor() {
    super("server returned an unsupported model instance response");
    this.name = "ModelInstanceContractError";
  }
}

export type ModelInstanceStatus = (typeof MODEL_INSTANCE_STATUSES)[number];
export type RuntimeType = (typeof RUNTIME_TYPES)[number];
export type ModelInstance = z.infer<typeof modelInstanceSchema>;
export type ModelInstanceList = z.infer<typeof modelInstanceListSchema>;
export type ModelInstanceLogEntry = z.infer<typeof modelInstanceLogEntrySchema>;
export type ModelInstanceLogPage = z.infer<typeof modelInstanceLogPageSchema>;
export type ModelInstanceEvent = z.infer<typeof modelInstanceEventSchema>;
export type ModelInstanceEventPage = z.infer<
  typeof modelInstanceEventPageSchema
>;
export type CreateModelInstanceRequest = z.input<
  typeof createModelInstanceRequestSchema
>;

function parse<T>(schema: z.ZodType<T>, value: unknown): T {
  try {
    return schema.parse(value);
  } catch {
    throw new ModelInstanceContractError();
  }
}

export function parseModelInstance(value: unknown) {
  return parse(modelInstanceSchema, value);
}

export function parseModelInstanceList(value: unknown) {
  return parse(modelInstanceListSchema, value);
}

export function parseModelInstanceLogPage(value: unknown) {
  return parse(modelInstanceLogPageSchema, value);
}

export function parseModelInstanceEventPage(value: unknown) {
  return parse(modelInstanceEventPageSchema, value);
}

export function parseCreateModelInstanceRequest(value: unknown) {
  return parse(createModelInstanceRequestSchema, value);
}
