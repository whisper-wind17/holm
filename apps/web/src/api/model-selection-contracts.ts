import { z } from "zod";

import {
  DOWNLOAD_TASK_STATUSES,
  MODEL_ORIGIN_TYPES,
} from "./model-acquisition-contracts";
import {
  MODEL_SOURCE_VALIDATION_ACTIONS,
  MODEL_SOURCE_VALIDATION_REASONS,
  MODEL_SOURCE_VALIDATION_STATUSES,
} from "./model-source-contracts";

export const LOCAL_AVAILABILITIES = [
  "artifact_ready",
  "download_active",
  "absent",
] as const;
export const RESOLVE_CONCLUSIONS = [
  "resolved",
  "external_path_accepted",
  "unresolved",
] as const;
export const REVISION_IMMUTABILITIES = [
  "immutable",
  "mutable_reference",
] as const;
export const METADATA_STATUSES = ["CONFIRMED", "UNCONFIRMED"] as const;
export const METADATA_KNOWLEDGE_STATES = [
  "identified",
  "unrecognized",
] as const;
export const DOWNLOAD_DISPOSITIONS = ["created", "joined_existing"] as const;

const originSchema = z.enum(MODEL_ORIGIN_TYPES);
const localAvailabilitySchema = z.enum(LOCAL_AVAILABILITIES);
const revisionImmutabilitySchema = z.enum(REVISION_IMMUTABILITIES);
const metadataStatusSchema = z.enum(METADATA_STATUSES);
const validationStatusSchema = z.enum(MODEL_SOURCE_VALIDATION_STATUSES);
const validationReasonSchema = z.enum(MODEL_SOURCE_VALIDATION_REASONS);
const validationActionSchema = z.enum(MODEL_SOURCE_VALIDATION_ACTIONS);
const downloadTaskStatusSchema = z.enum(DOWNLOAD_TASK_STATUSES);
const downloadDispositionSchema = z.enum(DOWNLOAD_DISPOSITIONS);

const unrecognizedValueSchema = z.strictObject({
  state: z.literal("unrecognized"),
});
const identifiedStringSchema = z.strictObject({
  state: z.literal("identified"),
  value: z.string(),
});
const identifiedIntegerSchema = z.strictObject({
  state: z.literal("identified"),
  value: z.number().int(),
});
const identifiedStringsSchema = z.strictObject({
  state: z.literal("identified"),
  value: z.array(z.string()),
});
const knowledgeStringSchema = z.union([
  identifiedStringSchema,
  unrecognizedValueSchema,
]);
const knowledgeIntegerSchema = z.union([
  identifiedIntegerSchema,
  unrecognizedValueSchema,
]);
const knowledgeStringsSchema = z.union([
  identifiedStringsSchema,
  unrecognizedValueSchema,
]);

const remoteFileSchema = z.strictObject({
  content_sha256: knowledgeStringSchema,
  path: z.string(),
  size_bytes: z.number().int(),
});
const identifiedFilesSchema = z.strictObject({
  state: z.literal("identified"),
  value: z.array(remoteFileSchema),
});
const knowledgeFilesSchema = z.union([
  identifiedFilesSchema,
  unrecognizedValueSchema,
]);

const resolvedModelSchema = z.strictObject({
  architecture: knowledgeStringSchema,
  candidate_runtimes: knowledgeStringsSchema,
  context_length: knowledgeIntegerSchema,
  files: knowledgeFilesSchema,
  license: knowledgeStringSchema,
  modalities: knowledgeStringsSchema,
  precision: knowledgeStringSchema,
  quantization: knowledgeStringSchema,
  repository_id: z.string(),
  requested_revision_norm: z.string(),
  resolved_revision: z.string(),
  revision_immutability: revisionImmutabilitySchema,
  source: originSchema,
  total_size_bytes: knowledgeIntegerSchema,
});

const resolvedPreviewResponseSchema = z.strictObject({
  deployable_layout: z.enum(["supported", "unsupported", "unknown"]),
  local_availability: localAvailabilitySchema,
  outcome: z.literal("resolved"),
  resolved: resolvedModelSchema,
});
const externalPathAcceptedResponseSchema = z.strictObject({
  control_plane_readable: z.literal(true),
  outcome: z.literal("external_path_accepted"),
  runtime_readability: z.literal("unverified"),
  runtime_readability_cause: z.literal("runtime_identity_check_unavailable"),
});
const unresolvedResponseSchema = z.strictObject({
  next_action: validationActionSchema,
  outcome: z.literal("unresolved"),
  reason_code: validationReasonSchema,
  status: validationStatusSchema.nullable(),
});

const resolveRequestSchema = z.strictObject({
  external_path: z.string().min(1).max(4096).nullable().optional(),
  repository_id: z.string().min(1).max(200).nullable().optional(),
  revision: z.string().min(1).max(256).nullable().optional(),
  source: originSchema,
});

const registeredModelResponseSchema = z.strictObject({
  architecture: knowledgeStringSchema,
  candidate_runtimes: knowledgeStringsSchema,
  context_length: knowledgeIntegerSchema,
  display_name: z.string(),
  estimated_size_bytes: knowledgeIntegerSchema,
  id: z.uuid(),
  license: knowledgeStringSchema,
  metadata_status: metadataStatusSchema,
  modalities: knowledgeStringsSchema,
  precision: knowledgeStringSchema,
  quantization: knowledgeStringSchema,
  replayed: z.boolean(),
  source: originSchema,
});

const downloadCreatedResponseSchema = z.strictObject({
  disposition: downloadDispositionSchema,
  id: z.uuid(),
  model_id: z.uuid(),
  replayed: z.boolean(),
  status: downloadTaskStatusSchema,
});

const modelReferenceCountsSchema = z.strictObject({
  artifacts: z.number().int().nonnegative(),
  download_tasks: z.number().int().nonnegative(),
  model_instances: z.number().int().nonnegative().nullable(),
});
const modelCatalogItemSchema = z.strictObject({
  architecture: knowledgeStringSchema,
  display_name: z.string(),
  estimated_size_bytes: knowledgeIntegerSchema,
  id: z.uuid(),
  local_availability: localAvailabilitySchema,
  modalities: knowledgeStringsSchema,
  references: modelReferenceCountsSchema,
  repository_id: z.string().nullable(),
  revision: z.string(),
  source: originSchema,
});
const modelCatalogPageSchema = z.strictObject({
  items: z.array(modelCatalogItemSchema),
  next_cursor: z.string().min(1).nullable().default(null),
});

const resolveResponseSchema = z.discriminatedUnion("outcome", [
  resolvedPreviewResponseSchema,
  externalPathAcceptedResponseSchema,
  unresolvedResponseSchema,
]);
const registrationResponseSchema = z.union([
  registeredModelResponseSchema,
  unresolvedResponseSchema,
]);
const downloadCreationResponseSchema = z.union([
  downloadCreatedResponseSchema,
  unresolvedResponseSchema,
]);

export const MODEL_SELECTION_OBJECT_SCHEMAS = {
  DownloadCreatedResponse: downloadCreatedResponseSchema,
  ExternalPathAcceptedResponse: externalPathAcceptedResponseSchema,
  ModelCatalogItemResponse: modelCatalogItemSchema,
  ModelReferenceCountsResponse: modelReferenceCountsSchema,
  RegisteredModelResponse: registeredModelResponseSchema,
  RegistrationUnresolvedResponse: unresolvedResponseSchema,
  RemoteFileResponse: remoteFileSchema,
  ResolveRequest: resolveRequestSchema,
  ResolvedModelResponse: resolvedModelSchema,
  ResolvedPreviewResponse: resolvedPreviewResponseSchema,
  UnrecognizedValue: unrecognizedValueSchema,
  UnresolvedResponse: unresolvedResponseSchema,
  "CursorPage[ModelCatalogItemResponse]": modelCatalogPageSchema,
  "IdentifiedValue[int]": identifiedIntegerSchema,
  "IdentifiedValue[str]": identifiedStringSchema,
  "IdentifiedValue[tuple[RemoteFileResponse, ...]]": identifiedFilesSchema,
  "IdentifiedValue[tuple[str, ...]]": identifiedStringsSchema,
} as const;

export const MODEL_SELECTION_ENUMS = {
  LocalAvailability: LOCAL_AVAILABILITIES,
  MetadataKnowledgeState: METADATA_KNOWLEDGE_STATES,
  MetadataStatus: METADATA_STATUSES,
  ResolveConclusion: RESOLVE_CONCLUSIONS,
  RevisionImmutability: REVISION_IMMUTABILITIES,
  disposition: DOWNLOAD_DISPOSITIONS,
} as const;

export class ModelSelectionContractError extends Error {
  readonly code = "model_selection_contract_error";

  constructor() {
    super("server returned an unsupported model-selection response");
    this.name = "ModelSelectionContractError";
  }
}

export type ResolveRequest = z.infer<typeof resolveRequestSchema>;
export type ResolveResponse = z.infer<typeof resolveResponseSchema>;
export type ResolvedPreviewResponse = z.infer<
  typeof resolvedPreviewResponseSchema
>;
export type RegisteredModelResponse = z.infer<
  typeof registeredModelResponseSchema
>;
export type RegistrationResponse = z.infer<typeof registrationResponseSchema>;
export type DownloadCreatedResponse = z.infer<
  typeof downloadCreatedResponseSchema
>;
export type DownloadCreationResponse = z.infer<
  typeof downloadCreationResponseSchema
>;
export type ModelCatalogItem = z.infer<typeof modelCatalogItemSchema>;
export type ModelCatalogPage = z.infer<typeof modelCatalogPageSchema>;
export type LocalAvailability = (typeof LOCAL_AVAILABILITIES)[number];

function parse<T>(schema: z.ZodType<T>, value: unknown): T {
  try {
    return schema.parse(value);
  } catch {
    throw new ModelSelectionContractError();
  }
}

export function parseResolveRequest(value: unknown) {
  return parse(resolveRequestSchema, value);
}

export function parseResolveResponse(value: unknown) {
  return parse(resolveResponseSchema, value);
}

export function parseRegistrationResponse(value: unknown) {
  return parse(registrationResponseSchema, value);
}

export function parseDownloadCreationResponse(value: unknown) {
  return parse(downloadCreationResponseSchema, value);
}

export function parseModelCatalogPage(value: unknown) {
  return parse(modelCatalogPageSchema, value);
}
