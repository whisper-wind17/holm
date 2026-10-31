import { z } from "zod";

const requestRecordStatusSchema = z.enum([
  "received",
  "in_progress",
  "completed",
  "failed",
  "aborted",
  "rejected",
  "unknown",
]);

const requestFinishReasonSchema = z.enum([
  "stop",
  "length",
  "tool_calls",
  "content_filter",
  "other",
]);

const requestRecordErrorCodeSchema = z.enum([
  "invalid_request",
  "invalid_api_key",
  "api_key_expired",
  "service_not_authorized",
  "model_not_found",
  "rate_limit_exceeded",
  "service_disabled",
  "backend_unavailable",
  "gateway_overloaded",
  "usage_storage_unavailable",
  "control_plane_unavailable",
  "inference_timeout",
]);

const requestRecordSchema = z.strictObject({
  api_key_display_prefix: z.string().nullable(),
  api_key_id: z.uuid().nullable(),
  api_key_name: z.string().nullable(),
  api_key_last4: z.string().nullable(),
  completed_at: z.iso.datetime({ offset: true }).nullable(),
  duration_ms: z.number().int().nonnegative().nullable(),
  error_code: requestRecordErrorCodeSchema.nullable(),
  finish_reason: requestFinishReasonSchema.nullable(),
  http_status: z.number().int().min(100).max(599).nullable(),
  input_tokens: z.number().int().nonnegative().nullable(),
  instance_id: z.uuid().nullable(),
  output_tokens: z.number().int().nonnegative().nullable(),
  public_model_name: z.string().nullable(),
  received_at: z.iso.datetime({ offset: true }),
  request_id: z.uuid(),
  service_id: z.uuid().nullable(),
  status: requestRecordStatusSchema,
  stream: z.boolean().nullable(),
  ttft_ms: z.number().int().nonnegative().nullable(),
  user_id: z.uuid().nullable(),
  username: z.string().nullable(),
});

const requestRecordPageSchema = z.strictObject({
  items: z.array(requestRecordSchema),
  next_cursor: z.string().min(1).nullable(),
});

export type RequestRecord = z.infer<typeof requestRecordSchema>;
export type RequestRecordPage = z.infer<typeof requestRecordPageSchema>;
export type RequestRecordStatus = z.infer<typeof requestRecordStatusSchema>;

export interface RequestRecordFilters {
  apiKeyId?: string;
  errorCode?: z.infer<typeof requestRecordErrorCodeSchema>;
  from?: string;
  to?: string;
  cursor?: string;
  requestId?: string;
  serviceId?: string;
  status?: RequestRecordStatus;
  userId?: string;
}

export function parseRequestRecordPage(value: unknown) {
  return requestRecordPageSchema.parse(value);
}

const requestRecordErrorGroupSchema = z.strictObject({
  status: z.enum(["failed", "rejected", "unknown"]),
  error_code: requestRecordErrorCodeSchema.nullable(),
  count: z.number().int().positive(),
  first_at: z.iso.datetime({ offset: true }),
  last_at: z.iso.datetime({ offset: true }),
  latest_request_id: z.uuid(),
});

const requestRecordErrorGroupsSchema = z.strictObject({
  items: z.array(requestRecordErrorGroupSchema).max(5),
});

export type RequestRecordErrorGroup = z.infer<
  typeof requestRecordErrorGroupSchema
>;
export type RequestRecordErrorGroups = z.infer<
  typeof requestRecordErrorGroupsSchema
>;

export interface RequestRecordErrorGroupFilters {
  from: string;
  to?: string;
  serviceId?: string;
}

export function parseRequestRecordErrorGroups(value: unknown) {
  return requestRecordErrorGroupsSchema.parse(value);
}
