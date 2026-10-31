import { z } from "zod";

const currentUserSchema = z.strictObject({
  display_name: z.string(),
  id: z.uuid(),
  username: z.string(),
});

const capabilitySchema = z.strictObject({
  capability: z.string().min(1),
  scope: z.string().min(1),
});

const navigationSchema = z.strictObject({
  id: z.string().min(1),
  label: z.string().min(1),
  required_capability: z.string().min(1),
});

/** Runtime boundary aligned with Control API's closed MeResponse model. */
export const meResponseSchema = z.strictObject({
  capabilities: z.array(capabilitySchema),
  must_change_password: z.boolean(),
  navigation: z.array(navigationSchema),
  role: z.enum(["admin", "user"]),
  user: currentUserSchema,
});

export type MeResponse = z.infer<typeof meResponseSchema>;
export type NavigationResponse = z.infer<typeof navigationSchema>;

const errorEnvelopeSchema = z.object({
  error: z.object({
    code: z.string().min(1),
    details: z
      .looseObject({
        password_policy: z.unknown().optional(),
        retry_after_seconds: z.number().int().nonnegative().optional(),
      })
      .optional(),
    message: z.string().min(1).optional(),
    request_id: z.string().min(1).optional(),
  }),
});

export function safeErrorEnvelope(value: unknown) {
  return errorEnvelopeSchema.safeParse(value);
}

export interface PasswordChangeRequest {
  confirm_password: string;
  current_password: string | null;
  new_password: string;
}

export interface LoginRequest {
  password: string;
  username: string;
}
