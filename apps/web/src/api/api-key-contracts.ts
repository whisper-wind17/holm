import { z } from "zod";
import { serviceDisplayNameSchema } from "./ai-service-contracts";

export const API_KEY_STATUSES = ["Active", "Expired", "Revoked"] as const;

const apiKeyStatusSchema = z.enum(API_KEY_STATUSES);
const apiKeySchema = z.strictObject({
  created_at: z.iso.datetime({ offset: true }),
  display_prefix: z.string().min(1),
  expires_at: z.iso.datetime({ offset: true }).nullable(),
  id: z.uuid(),
  last4: z.string().length(4),
  last_used_at: z.iso.datetime({ offset: true }).nullable(),
  name: z.string().min(1).max(128),
  revoked_at: z.iso.datetime({ offset: true }).nullable(),
  service_ids: z.array(z.uuid()),
  status: apiKeyStatusSchema,
});

const listedApiKeySchema = apiKeySchema.extend({
  effective_services: z.array(
    z.strictObject({
      id: z.uuid(),
      name: serviceDisplayNameSchema,
      public_model_name: z.string().min(1),
    }),
  ),
});
const apiKeyListSchema = z.strictObject({ items: z.array(listedApiKeySchema) });
const createdApiKeySchema = apiKeySchema.extend({
  api_key: z.string().startsWith("holm_sk_"),
});
export const apiKeyNameSchema = z
  .string()
  .min(2, "请输入 2～40 个字符")
  .max(40, "请输入 2～40 个字符")
  .refine(
    (value) => value.trim().length > 0 && value === value.trim(),
    "名称不能空白或含首尾空白",
  );
export const renameApiKeyRequestSchema = z.strictObject({
  name: apiKeyNameSchema,
});
const createApiKeyRequestSchema = z.strictObject({
  expires_at: z.iso.datetime({ offset: true }).nullable().default(null),
  name: apiKeyNameSchema,
  service_ids: z.array(z.uuid()).default([]),
});

export class ApiKeyContractError extends Error {
  readonly code = "api_key_contract_error";

  constructor() {
    super("server returned an unsupported API key response");
    this.name = "ApiKeyContractError";
  }
}

export type ApiKeyStatus = (typeof API_KEY_STATUSES)[number];
export type ApiKey = z.infer<typeof apiKeySchema>;
export type ListedApiKey = z.infer<typeof listedApiKeySchema>;
export type ApiKeyList = z.infer<typeof apiKeyListSchema>;
export type CreatedApiKey = z.infer<typeof createdApiKeySchema>;
export type RenameApiKeyRequest = z.infer<typeof renameApiKeyRequestSchema>;
export type CreateApiKeyRequest = z.input<typeof createApiKeyRequestSchema>;

function parse<T>(schema: z.ZodType<T>, value: unknown): T {
  try {
    return schema.parse(value);
  } catch {
    throw new ApiKeyContractError();
  }
}

export function parseApiKey(value: unknown) {
  return parse(apiKeySchema, value);
}

export function parseApiKeyList(value: unknown) {
  return parse(apiKeyListSchema, value);
}

export function parseCreatedApiKey(value: unknown) {
  return parse(createdApiKeySchema, value);
}

export function parseCreateApiKeyRequest(value: unknown) {
  return parse(createApiKeyRequestSchema, value);
}

export function parseRenameApiKeyRequest(value: unknown) {
  return parse(renameApiKeyRequestSchema, value);
}
