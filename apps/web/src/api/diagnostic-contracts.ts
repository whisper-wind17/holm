import { z } from "zod";

// DiagnosticManifest.json / DiagnosticBundleRequest are the server contracts.
// The diagnostic contract fixture is checked by both Python and browser tests.
export const diagnosticRequestSchema = z.strictObject({
  window_minutes: z.number().int().min(1).max(1440),
  end_at: z.iso.datetime({ offset: true }).optional(),
  request_ids: z.array(z.uuid()).max(20),
  instance_id: z.uuid().nullable(),
});
export type DiagnosticRequest = z.infer<typeof diagnosticRequestSchema>;
const itemSchema = z.strictObject({
  key: z.enum([
    "component_versions",
    "configuration",
    "health",
    "structured_events",
    "metrics",
    "request_records",
    "sanitized_logs",
  ]),
  path: z.string(),
  status: z.enum(["included", "not_included"]),
  count: z.number().int().nonnegative(),
  bytes: z.number().int().nonnegative(),
  truncated: z.boolean(),
  reason: z.string().nullable(),
});
export const diagnosticManifestSchema = z.strictObject({
  format_version: z.literal(1),
  generated_at: z.iso.datetime({ offset: true }),
  window: z.strictObject({
    from: z.iso.datetime({ offset: true }),
    to: z.iso.datetime({ offset: true }),
  }),
  request_ids: z.array(z.uuid()).max(20),
  instance_id: z.uuid().nullable(),
  limits: z.strictObject({
    max_window_minutes: z.literal(1440),
    max_request_ids: z.literal(20),
    max_events: z.number().int().positive(),
    max_log_lines: z.number().int().positive(),
    max_item_bytes: z.number().int().positive(),
    max_content_bytes: z.number().int().positive(),
  }),
  content_bytes: z.number().int().nonnegative(),
  archive_files: z.array(z.string()),
  items: z.array(itemSchema).max(7),
});
export type DiagnosticManifest = z.infer<typeof diagnosticManifestSchema>;
export interface DiagnosticPreview {
  manifest: DiagnosticManifest;
  receipt: string;
  expiresAt: number;
}
