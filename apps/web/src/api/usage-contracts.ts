import { z } from "zod";

export const USAGE_GROUPS = ["service", "user", "api_key"] as const;

const usageTotalsSchema = z.strictObject({
  error_rate: z.number().min(0).max(1).nullable(),
  error_requests: z.number().int().nonnegative(),
  input_tokens: z.number().int().nonnegative().nullable(),
  output_tokens: z.number().int().nonnegative().nullable(),
  partial_token_requests: z.number().int().nonnegative(),
  requests: z.number().int().nonnegative(),
  total_tokens: z.number().int().nonnegative().nullable(),
});

const usageGroupSchema = usageTotalsSchema.extend({
  api_key_name: z.string().nullish(),
  owner_username: z.string().nullish(),
  owner_is_deleted: z.boolean().nullish(),
  display_name: z.string().min(1),
  group_id: z.uuid().nullable(),
  is_deleted: z.boolean(),
  status: z.enum(["Active", "Expired", "Revoked"]).nullable(),
});

const usageTrendPointSchema = z.strictObject({
  bucket_start: z.iso.datetime({ offset: true }),
  input_tokens: z.number().int().nonnegative().nullable(),
  output_tokens: z.number().int().nonnegative().nullable(),
  requests: z.number().int().nonnegative(),
  total_tokens: z.number().int().nonnegative().nullable(),
});

const usageSummarySchema = z.strictObject({
  today_active_users: z.number().int().nonnegative(),
  data_updated_at: z.iso.datetime({ offset: true }).nullable(),
  this_month: usageTotalsSchema,
  today: usageTotalsSchema,
});

const usageReportSchema = z.strictObject({
  bucket_width: z.enum(["hour", "day"]),
  details: z.array(usageGroupSchema),
  from: z.iso.datetime({ offset: true }),
  summary: usageTotalsSchema,
  to: z.iso.datetime({ offset: true }),
  trend: z.array(usageTrendPointSchema),
  tz: z.string().min(1),
});

export type UsageGroupBy = (typeof USAGE_GROUPS)[number];
export type UsageTotals = z.infer<typeof usageTotalsSchema>;
export type UsageGroup = z.infer<typeof usageGroupSchema>;
export type UsageTrendPoint = z.infer<typeof usageTrendPointSchema>;
export type UsageSummary = z.infer<typeof usageSummarySchema>;
export type UsageReport = z.infer<typeof usageReportSchema>;

export interface UsageFilters {
  apiKeyId?: string;
  from: string;
  groupBy: UsageGroupBy;
  serviceId?: string;
  timeZone: string;
  to: string;
  userId?: string;
}

export class UsageContractError extends Error {
  readonly code = "usage_contract_error";

  constructor() {
    super("server returned an unsupported Usage response");
    this.name = "UsageContractError";
  }
}

function parse<T>(schema: z.ZodType<T>, value: unknown): T {
  try {
    return schema.parse(value);
  } catch {
    throw new UsageContractError();
  }
}

export const parseUsageSummary = (value: unknown) =>
  parse(usageSummarySchema, value);
export const parseUsageReport = (value: unknown) =>
  parse(usageReportSchema, value);
