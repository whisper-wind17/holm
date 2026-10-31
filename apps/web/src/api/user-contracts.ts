import { z } from "zod";
import { serviceDisplayNameSchema } from "./ai-service-contracts";

export const USER_ROLES = ["admin", "user"] as const;
export const USER_STATUSES = ["active", "disabled", "deleted"] as const;

const userRoleSchema = z.enum(USER_ROLES);
const userStatusSchema = z.enum(USER_STATUSES);
const userOverviewSchema = z.strictObject({
  api_key_count: z.number().int().nonnegative(),
  authorized_service_count: z.number().int().nonnegative(),
  display_name: z.string().max(80),
  id: z.uuid(),
  last_login_at: z.iso.datetime({ offset: true }).nullable(),
  must_change_password: z.boolean(),
  role: userRoleSchema,
  status: userStatusSchema,
  username: z.string().min(3).max(32),
});
const userListSchema = z.strictObject({ items: z.array(userOverviewSchema) });
const createdUserSchema = userOverviewSchema.extend({
  temporary_password: z.string().min(1),
});
const permissionLimitsSchema = z.strictObject({
  max_concurrent_requests: z.number().int().min(1).max(64).nullable(),
  max_requests_per_minute: z.number().int().min(1).max(600).nullable(),
});
const grantedServiceSchema = z.strictObject({
  ...permissionLimitsSchema.shape,
  description: z.string(),
  enabled: z.boolean(),
  id: z.uuid(),
  name: serviceDisplayNameSchema,
  public_model_name: z.string().min(1),
});
const safeApiKeySchema = z.strictObject({
  display_prefix: z.string().min(1),
  expires_at: z.iso.datetime({ offset: true }).nullable(),
  id: z.uuid(),
  last4: z.string().length(4),
  last_used_at: z.iso.datetime({ offset: true }).nullable(),
  name: z.string(),
  status: z.enum(["Active", "Expired", "Revoked"]),
});
const userDetailSchema = userOverviewSchema.extend({
  created_at: z.iso.datetime({ offset: true }),
  api_keys: z.array(safeApiKeySchema),
  authorized_services: z.array(grantedServiceSchema),
});
const createUserRequestSchema = z.strictObject({
  display_name: z.string().max(80).default(""),
  role: userRoleSchema.default("user"),
  username: z
    .string()
    .min(3)
    .max(32)
    .regex(/^[A-Za-z0-9._-]+$/),
});
const resetPasswordSchema = z.strictObject({
  must_change_password: z.boolean(),
  temporary_password: z.string().min(1),
});
const serviceIdsSchema = z.strictObject({ service_ids: z.array(z.uuid()) });
const userIdsSchema = z.strictObject({
  user_ids: z.array(z.uuid()),
  limits: z.record(z.uuid(), permissionLimitsSchema),
});
const personalAiServiceSchema = z.strictObject({
  available: z.boolean(),
  enabled: z.boolean(),
  id: z.uuid(),
  last_used_at: z.iso.datetime({ offset: true }).nullable(),
  last_succeeded_at: z.iso.datetime({ offset: true }).nullable(),
  description: z.string(),
  name: serviceDisplayNameSchema,
  public_model_name: z.string().min(1),
});
const personalAiServiceListSchema = z.strictObject({
  items: z.array(personalAiServiceSchema),
});

export class UserContractError extends Error {
  readonly code = "user_contract_error";

  constructor() {
    super("server returned an unsupported user management response");
    this.name = "UserContractError";
  }
}

export type PermissionLimits = z.infer<typeof permissionLimitsSchema>;
export type PermissionLimitsMap = Record<string, PermissionLimits>;

export type UserRole = (typeof USER_ROLES)[number];
export type UserStatus = (typeof USER_STATUSES)[number];
export type UserOverview = z.infer<typeof userOverviewSchema>;
export type UserList = z.infer<typeof userListSchema>;
export type CreatedUser = z.infer<typeof createdUserSchema>;
export type UserDetail = z.infer<typeof userDetailSchema>;
export type SafeApiKey = z.infer<typeof safeApiKeySchema>;
export type CreateUserRequest = z.input<typeof createUserRequestSchema>;
export type ResetPassword = z.infer<typeof resetPasswordSchema>;
export type ServiceIds = z.infer<typeof serviceIdsSchema>;
export type UserIds = z.infer<typeof userIdsSchema>;
export type PersonalAiService = z.infer<typeof personalAiServiceSchema>;
export type PersonalAiServiceList = z.infer<typeof personalAiServiceListSchema>;

function parse<T>(schema: z.ZodType<T>, value: unknown): T {
  try {
    return schema.parse(value);
  } catch {
    throw new UserContractError();
  }
}

export const parseUserOverview = (value: unknown) =>
  parse(userOverviewSchema, value);
export const parseUserList = (value: unknown) => parse(userListSchema, value);
export const parseCreatedUser = (value: unknown) =>
  parse(createdUserSchema, value);
export const parseUserDetail = (value: unknown) =>
  parse(userDetailSchema, value);
export const parseSafeApiKey = (value: unknown) =>
  parse(safeApiKeySchema, value);
export const parseCreateUserRequest = (value: unknown) =>
  parse(createUserRequestSchema, value);
export const parseResetPassword = (value: unknown) =>
  parse(resetPasswordSchema, value);
export const parseServiceIds = (value: unknown) =>
  parse(serviceIdsSchema, value);
export const parseUserIds = (value: unknown) => parse(userIdsSchema, value);
export const parsePersonalAiServiceList = (value: unknown) =>
  parse(personalAiServiceListSchema, value);

export const USER_ACTIVITY_ACTIONS = [
  "auth.login",
  "account.password_change",
  "account.change",
  "permission.change",
  "api_key.change",
] as const;
export const userActivityItemSchema = z.strictObject({
  id: z.uuid(),
  created_at: z.iso.datetime({ offset: true }),
  action: z.enum(USER_ACTIVITY_ACTIONS),
  operation: z.string().nullable(),
  result: z.enum(["succeeded", "failed", "denied"]),
  reason_code: z.string().nullable(),
  actor: z.strictObject({
    type: z.enum(["user", "system", "anonymous"]),
    id: z.uuid().nullable(),
    username: z.string().nullable(),
    display_name: z.string().nullable(),
  }),
  api_key: z
    .strictObject({
      id: z.uuid(),
      name: z.string(),
      display_prefix: z.string(),
      last4: z.string().length(4),
    })
    .nullable(),
  added_service_ids: z.array(z.uuid()),
  removed_service_ids: z.array(z.uuid()),
  limit_changed_service_ids: z.array(z.uuid()),
});
export const userActivityPageSchema = z.strictObject({
  items: z.array(userActivityItemSchema),
  next_cursor: z.string().nullable(),
});
export type UserActivityAction = (typeof USER_ACTIVITY_ACTIONS)[number];
export type UserActivityItem = z.infer<typeof userActivityItemSchema>;
export type UserActivityPage = z.infer<typeof userActivityPageSchema>;
export const parseUserActivityPage = (value: unknown) =>
  parse(userActivityPageSchema, value);

const personalProfileSchema = z.strictObject({
  username: z.string().min(3).max(32),
  display_name: z.string().max(80),
  role: userRoleSchema,
  current_login_at: z.iso.datetime({ offset: true }),
  previous_login_at: z.iso.datetime({ offset: true }).nullable(),
  created_at: z.iso.datetime({ offset: true }),
  authorized_service_count: z.number().int().nonnegative(),
});
export type PersonalProfile = z.infer<typeof personalProfileSchema>;
export type UpdateMyProfileRequest = { display_name: string };
export const parsePersonalProfile = (value: unknown) =>
  parse(personalProfileSchema, value);
