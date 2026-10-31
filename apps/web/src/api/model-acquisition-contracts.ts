import { MODEL_INSTANCE_STATUSES } from "./model-instance-contracts";
import { z } from "zod";

export const DOWNLOAD_TASK_STATUSES = [
  "Queued",
  "Resolving",
  "Downloading",
  "Paused",
  "Verifying",
  "Publishing",
  "Completed",
  "Failed",
  "Cancelled",
] as const;

export const MODEL_ARTIFACT_STATUSES = [
  "Ready",
  "Corrupted",
  "Missing",
] as const;

export const ARTIFACT_STORAGE_TYPES = ["Managed", "External"] as const;
export const MODEL_ORIGIN_TYPES = [
  "Hugging Face",
  "ModelScope",
  "Local Path",
] as const;
export const DOWNLOAD_FAILURE_STAGES = [
  "Resolving",
  "Downloading",
  "Verifying",
  "Publishing",
] as const;
export const DOWNLOAD_COMMANDS = [
  "pause",
  "resume",
  "cancel",
  "retry",
] as const;
export const DOWNLOAD_ERROR_CODES = [
  "artifact_file_list_invalid",
  "artifact_manifest_hash_failed",
  "artifact_publish_failed",
  "artifact_required_files_invalid",
  "artifact_runtime_incompatible",
  "artifact_safetensors_invalid",
  "artifact_unsafe_file",
  "download_process_failed",
  "download_task_identity_mismatch",
  "download_task_state_mismatch",
  "duplicate_resolved_revision",
  "external_path_unreadable",
  "insufficient_storage",
  "model_layout_unsupported",
  "remote_code_not_allowed",
  "staging_cleanup_failed",
  "staging_filesystem_mismatch",
] as const;

const downloadTaskStatusSchema = z.enum(DOWNLOAD_TASK_STATUSES);
const modelArtifactStatusSchema = z.enum(MODEL_ARTIFACT_STATUSES);
const artifactStorageTypeSchema = z.enum(ARTIFACT_STORAGE_TYPES);
const modelOriginTypeSchema = z.enum(MODEL_ORIGIN_TYPES);
const downloadFailureStageSchema = z.enum(DOWNLOAD_FAILURE_STAGES);
const downloadCommandSchema = z.enum(DOWNLOAD_COMMANDS);
const downloadErrorCodeSchema = z.enum(DOWNLOAD_ERROR_CODES);

const downloadTaskItemSchema = z.strictObject({
  attempt: z.number().int().min(1),
  available_commands: z.array(downloadCommandSchema),
  completed_at: z.iso.datetime({ offset: true }).nullable(),
  created_at: z.iso.datetime({ offset: true }),
  display_name: z.string(),
  downloaded_bytes: z.number().int().nonnegative(),
  error_code: downloadErrorCodeSchema.nullable(),
  failure_stage: downloadFailureStageSchema.nullable(),
  id: z.uuid(),
  model_id: z.uuid(),
  repository_id: z.string(),
  revision: z.string().nullable(),
  source: modelOriginTypeSchema,
  staging_path: z.string().nullable(),
  status: downloadTaskStatusSchema,
  total_bytes: z.number().int().nonnegative().nullable(),
  updated_at: z.iso.datetime({ offset: true }),
});

const downloadTaskPageSchema = z.strictObject({
  items: z.array(downloadTaskItemSchema),
  next_cursor: z.string().min(1).nullable().default(null),
});

const modelArtifactItemSchema = z.strictObject({
  created_at: z.iso.datetime({ offset: true }),
  display_name: z.string(),
  id: z.uuid(),
  last_verified_at: z.iso.datetime({ offset: true }).nullable(),
  model_id: z.uuid(),
  path: z.string(),
  referencing_instances: z.number().int().nonnegative().nullable(),
  repository_id: z.string(),
  resolved_revision: z.string().nullable(),
  size_bytes: z.number().int().nonnegative(),
  source: modelOriginTypeSchema,
  status: modelArtifactStatusSchema,
  storage_type: artifactStorageTypeSchema,
  updated_at: z.iso.datetime({ offset: true }),
});

const modelArtifactSummarySchema = z.strictObject({
  active_download_tasks: z.number().int().nonnegative(),
  managed_bytes: z.number().int().nonnegative(),
  storage_available_bytes: z.number().int().nonnegative().nullable(),
});

const modelArtifactPageSchema = z.strictObject({
  items: z.array(modelArtifactItemSchema),
  next_cursor: z.string().min(1).nullable().default(null),
  summary: modelArtifactSummarySchema,
});

const downloadCommandResponseSchema = z.strictObject({
  attempt: z.number().int(),
  command: downloadCommandSchema,
  id: z.uuid(),
  staging_cleanup_requested: z.boolean(),
  status: downloadTaskStatusSchema,
});

const artifactDeleteResponseSchema = z.strictObject({
  id: z.uuid(),
  queued: z.boolean(),
});

const artifactDeletionStatusSchema = z.strictObject({
  status: z.enum(["Queued", "Running", "Succeeded", "Failed"]),
  error_code: z.string().nullable(),
});

const artifactVerificationStatusSchema = z.strictObject({
  job_id: z.uuid().nullable(),
  status: z.enum(["Queued", "Running", "Succeeded", "Failed"]).nullable(),
  error_code: z
    .enum(["artifact_verify_unsafe_path", "artifact_verify_failed"])
    .nullable(),
});
const artifactVerifyResponseSchema = z.strictObject({
  id: z.uuid(),
  queued: z.literal(true),
});
export const ARTIFACT_VERIFY_OBJECT_SCHEMAS = {
  ArtifactVerifyResponse: artifactVerifyResponseSchema,
} as const;
export const ARTIFACT_VERIFICATION_STATUS_OBJECT_SCHEMAS = {
  ArtifactVerificationStatusResponse: artifactVerificationStatusSchema,
} as const;
export type ArtifactVerifyResponse = z.infer<
  typeof artifactVerifyResponseSchema
>;
export function parseArtifactVerifyResponse(value: unknown) {
  return parse(artifactVerifyResponseSchema, value);
}
export type ArtifactVerificationStatus = z.infer<
  typeof artifactVerificationStatusSchema
>;
export function parseArtifactVerificationStatus(value: unknown) {
  return parse(artifactVerificationStatusSchema, value);
}

export const DOWNLOAD_TASK_OBJECT_SCHEMAS = {
  DownloadTaskItemResponse: downloadTaskItemSchema,
  "CursorPage[DownloadTaskItemResponse]": downloadTaskPageSchema,
} as const;

export const MODEL_ARTIFACT_OBJECT_SCHEMAS = {
  ModelArtifactItemResponse: modelArtifactItemSchema,
  ModelArtifactPageResponse: modelArtifactPageSchema,
  ModelArtifactSummaryResponse: modelArtifactSummarySchema,
} as const;

export const DOWNLOAD_COMMAND_OBJECT_SCHEMAS = {
  DownloadCommandResponse: downloadCommandResponseSchema,
} as const;

export class ModelAcquisitionContractError extends Error {
  readonly code = "model_acquisition_contract_error";

  constructor() {
    super("server returned an unsupported model acquisition response");
    this.name = "ModelAcquisitionContractError";
  }
}

export type DownloadTaskStatus = (typeof DOWNLOAD_TASK_STATUSES)[number];
export type ModelArtifactStatus = (typeof MODEL_ARTIFACT_STATUSES)[number];
export type ArtifactStorageType = (typeof ARTIFACT_STORAGE_TYPES)[number];
export type ModelOriginType = (typeof MODEL_ORIGIN_TYPES)[number];
export type DownloadFailureStage = (typeof DOWNLOAD_FAILURE_STAGES)[number];
export type DownloadCommand = (typeof DOWNLOAD_COMMANDS)[number];
export type DownloadErrorCode = (typeof DOWNLOAD_ERROR_CODES)[number];
export type DownloadTaskItem = z.infer<typeof downloadTaskItemSchema>;
export type DownloadTaskPage = z.infer<typeof downloadTaskPageSchema>;
export type ModelArtifactItem = z.infer<typeof modelArtifactItemSchema>;
export type ModelArtifactPage = z.infer<typeof modelArtifactPageSchema>;
export type DownloadCommandResponse = z.infer<
  typeof downloadCommandResponseSchema
>;
export type ArtifactDeletionStatus = z.infer<
  typeof artifactDeletionStatusSchema
>;
export type ArtifactDeleteResponse = z.infer<
  typeof artifactDeleteResponseSchema
>;

function parse<T>(schema: z.ZodType<T>, value: unknown): T {
  try {
    return schema.parse(value);
  } catch {
    throw new ModelAcquisitionContractError();
  }
}

export function parseDownloadTaskPage(value: unknown) {
  return parse(downloadTaskPageSchema, value);
}

export function parseModelArtifactPage(value: unknown) {
  return parse(modelArtifactPageSchema, value);
}

export function parseDownloadCommandResponse(value: unknown) {
  return parse(downloadCommandResponseSchema, value);
}

export function parseArtifactDeleteResponse(value: unknown) {
  return parse(artifactDeleteResponseSchema, value);
}

export function parseArtifactDeletionStatus(value: unknown) {
  return parse(artifactDeletionStatusSchema, value);
}

const downloadStatusCountsSchema = z.strictObject({
  counts: z.record(downloadTaskStatusSchema, z.number().int().nonnegative()),
});
const modelArtifactDetailSchema = modelArtifactItemSchema.extend({
  model: z.strictObject({
    architecture: z.string().nullable(),
    modalities: z.array(z.string()).nullable(),
    precision: z.string().nullable(),
    quantization: z.string().nullable(),
    context_length: z.number().int().nullable(),
    license: z.string().nullable(),
  }),
  files: z
    .array(
      z.strictObject({
        path: z.string(),
        size_bytes: z.number().int().nonnegative(),
        sha256: z.string().regex(/^[0-9a-f]{64}$/),
      }),
    )
    .min(1)
    .nullable(),
  download_tasks: z.array(
    z.strictObject({
      id: z.uuid(),
      status: downloadTaskStatusSchema,
      created_at: z.iso.datetime({ offset: true }),
      completed_at: z.iso.datetime({ offset: true }).nullable(),
    }),
  ),
  instances: z.array(
    z.strictObject({
      id: z.uuid(),
      name: z.string(),
      status: z.enum(MODEL_INSTANCE_STATUSES),
    }),
  ),
});
export type DownloadStatusCounts = z.infer<typeof downloadStatusCountsSchema>;
export type ModelArtifactDetail = z.infer<typeof modelArtifactDetailSchema>;
export function parseDownloadStatusCounts(value: unknown) {
  return parse(downloadStatusCountsSchema, value);
}
export function parseModelArtifactDetail(value: unknown) {
  return parse(modelArtifactDetailSchema, value);
}
