import { z } from "zod";
import { apiKeyNameSchema } from "../api/api-key-contracts";

export const createApiKeyFormSchema = z
  .object({
    name: apiKeyNameSchema,
    expiration: z.enum(["never", "30", "90", "180", "custom"]),
    customDate: z.string(),
  })
  .transform((values, context) => {
    const now = Date.now();
    if (values.expiration === "never")
      return { name: values.name, expires_at: null };
    // datetime-local has no offset: Date interprets it in the browser's local zone.
    const expiresAt =
      values.expiration === "custom"
        ? new Date(values.customDate).getTime()
        : now + Number(values.expiration) * 24 * 60 * 60 * 1000;
    if (!Number.isFinite(expiresAt) || expiresAt <= now) {
      context.addIssue({
        code: "custom",
        path: ["customDate"],
        message: "到期时间无效，请填写晚于当前时间的日期。",
      });
      return z.NEVER;
    }
    return { name: values.name, expires_at: new Date(expiresAt).toISOString() };
  });

export type CreateApiKeyFormInput = z.input<typeof createApiKeyFormSchema>;
export type CreateApiKeyFormOutput = z.output<typeof createApiKeyFormSchema>;
