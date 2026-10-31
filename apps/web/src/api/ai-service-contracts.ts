import { z } from "zod";

export const serviceDisplayNameSchema = z
  .string()
  .transform((value) => value.trim())
  .refine(
    (value) =>
      !value.includes("\0") &&
      Array.from(value).length >= 2 &&
      Array.from(value).length <= 40,
    "服务名称需为 2～40 个字符",
  );
export const renameServiceDisplayNameSchema = z.strictObject({
  name: serviceDisplayNameSchema,
  expected_route_version: z.number().int().positive(),
});
export type RenameServiceDisplayNameRequest = z.infer<
  typeof renameServiceDisplayNameSchema
>;

export const PUBLIC_MODEL_NAME_PATTERN = "[a-z0-9._-]{2,64}";

// JavaScript $ also matches before a final newline; require the actual end.
export const renameAiServiceRequestSchema = z
  .strictObject({
    public_model_name: z
      .string()
      .regex(
        new RegExp(`^${PUBLIC_MODEL_NAME_PATTERN}$(?![\\s\\S])`),
        "API 模型名需为 2～64 位小写字母、数字、点、下划线或短横线，例如 qwen3-32b",
      ),
    confirm_public_model_name: z.string(),
    expected_route_version: z.number().int().positive(),
  })
  .refine(
    (value) => value.confirm_public_model_name === value.public_model_name,
    {
      message: "请再次输入相同的新 API 模型名",
      path: ["confirm_public_model_name"],
    },
  );
export type RenameAiServiceRequest = z.infer<
  typeof renameAiServiceRequestSchema
>;

export const AI_SERVICE_RUNTIME_STATUSES = [
  "Disabled",
  "Available",
  "Degraded",
  "Unavailable",
] as const;

export const AI_SERVICE_PRIMARY_INSTANCE_STATUSES = [
  "WaitingForResource",
  "Starting",
  "Running",
  "Restarting",
  "Unhealthy",
  "Stopping",
  "Stopped",
  "Failed",
] as const;

const aiServiceSchema = z.strictObject({
  name: serviceDisplayNameSchema,
  description: z.string().max(2000),
  enabled: z.boolean(),
  id: z.uuid(),
  primary_instance_id: z.uuid(),
  primary_instance_status: z
    .enum(AI_SERVICE_PRIMARY_INSTANCE_STATUSES)
    .nullable(),
  public_model_name: z.string().min(1).max(255),
  route_version: z.number().int().positive(),
  runtime_status: z.enum(AI_SERVICE_RUNTIME_STATUSES),
});

const aiServiceListSchema = z.strictObject({
  items: z.array(aiServiceSchema),
});

const createAiServiceRequestSchema = z.strictObject({
  name: serviceDisplayNameSchema.optional(),
  description: z.string().max(2000).default(""),
  enabled: z.boolean().default(true),
  primary_instance_id: z.uuid(),
  public_model_name: z.string().min(1).max(255),
});

const updateAiServiceRequestSchema = z
  .strictObject({
    description: z.string().max(2000).optional(),
    enabled: z.boolean().optional(),
    primary_instance_id: z.uuid().optional(),
  })
  .refine((value) => Object.keys(value).length > 0);

export class AiServiceContractError extends Error {
  readonly code = "ai_service_contract_error";

  constructor() {
    super("server returned an unsupported AI service response");
    this.name = "AiServiceContractError";
  }
}

export type AiService = z.infer<typeof aiServiceSchema>;
export type AiServiceRuntimeStatus =
  (typeof AI_SERVICE_RUNTIME_STATUSES)[number];
export type AiServicePrimaryInstanceStatus =
  (typeof AI_SERVICE_PRIMARY_INSTANCE_STATUSES)[number];
export type AiServiceList = z.infer<typeof aiServiceListSchema>;
export type CreateAiServiceRequest = z.input<
  typeof createAiServiceRequestSchema
>;
export type UpdateAiServiceRequest = z.input<
  typeof updateAiServiceRequestSchema
>;

function parse<T>(schema: z.ZodType<T>, value: unknown): T {
  try {
    return schema.parse(value);
  } catch {
    throw new AiServiceContractError();
  }
}

export function parseAiService(value: unknown) {
  return parse(aiServiceSchema, value);
}

export function parseAiServiceList(value: unknown) {
  return parse(aiServiceListSchema, value);
}

export function parseCreateAiServiceRequest(value: unknown) {
  return parse(createAiServiceRequestSchema, value);
}

export function parseRenameAiServiceRequest(value: unknown) {
  return parse(renameAiServiceRequestSchema, value);
}

export function parseUpdateAiServiceRequest(value: unknown) {
  return parse(updateAiServiceRequestSchema, value);
}
