import { z } from "zod";

import {
  MODEL_ORIGIN_TYPES,
  type ModelOriginType,
} from "./model-acquisition-contracts";

export const MODEL_SOURCE_CONFIGURATION_STATES = [
  "unconfigured",
  "configured",
  "key_unavailable",
] as const;

export const MODEL_SOURCE_VALIDATION_STATUSES = [
  "accessible",
  "invalid_credential",
  "permission_denied",
  "not_found",
  "rate_limited",
] as const;

export const MODEL_SOURCE_VALIDATION_ACTIONS = [
  "update_credential",
  "check_source_access",
  "check_source_reference",
  "check_source_reference_and_configure_credential",
  "retry_later",
  "install_validator",
] as const;

export const MODEL_SOURCE_VALIDATION_REASONS = [
  "credential_valid",
  "credential_invalid",
  "source_permission_denied",
  "source_not_found",
  "source_not_found_or_inaccessible",
  "source_rate_limited",
  "validator_unavailable",
  "validation_timeout",
  "validation_failed",
] as const;

export type ModelSourceType = Exclude<ModelOriginType, "Local Path">;
export type ModelSourceConfigurationState =
  (typeof MODEL_SOURCE_CONFIGURATION_STATES)[number];
export type ModelSourceValidationStatus =
  (typeof MODEL_SOURCE_VALIDATION_STATUSES)[number];
export type ModelSourceValidationAction =
  (typeof MODEL_SOURCE_VALIDATION_ACTIONS)[number];
export type ModelSourceValidationReason =
  (typeof MODEL_SOURCE_VALIDATION_REASONS)[number];

function modelSourceTypes(): readonly [ModelSourceType, ModelSourceType] {
  const values = MODEL_ORIGIN_TYPES.filter(
    (value): value is ModelSourceType => value !== "Local Path",
  );
  if (values.length !== 2 || new Set(values).size !== values.length) {
    throw new ModelSourceContractError();
  }
  const [first, second] = values;
  if (first === undefined || second === undefined) {
    throw new ModelSourceContractError();
  }
  return [first, second];
}

export const MODEL_SOURCE_TYPES = modelSourceTypes();

const modelSourceTypeSchema = z.enum(MODEL_SOURCE_TYPES);
const modelSourceConfigurationStateSchema = z.enum(
  MODEL_SOURCE_CONFIGURATION_STATES,
);
const modelSourceValidationStatusSchema = z.enum(
  MODEL_SOURCE_VALIDATION_STATUSES,
);
const modelSourceValidationActionSchema = z.enum(
  MODEL_SOURCE_VALIDATION_ACTIONS,
);
const modelSourceValidationReasonSchema = z.enum(
  MODEL_SOURCE_VALIDATION_REASONS,
);

export const modelSourceResponseSchema = z.strictObject({
  configuration_state: modelSourceConfigurationStateSchema,
  credential_last4: z.string().length(4).nullable(),
  next_action: modelSourceValidationActionSchema.nullable(),
  source_type: modelSourceTypeSchema,
  verification_reason_code: modelSourceValidationReasonSchema.nullable(),
  verification_status: modelSourceValidationStatusSchema.nullable(),
  verified_at: z.iso.datetime({ offset: true }).nullable(),
});

export const modelSourceCredentialRequestSchema = z
  .strictObject({
    clear: z.boolean().default(false),
    confirm_unverified: z.boolean().optional(),
    credential: z.string().min(1).max(4096).nullable().default(null),
    current_password: z.string().min(1).max(4096).nullable().default(null),
    source_type: modelSourceTypeSchema,
  })
  .superRefine((value, context) => {
    if (value.clear === (value.credential !== null)) {
      context.addIssue({
        code: "custom",
        message: "provide a credential or request clear",
        path: ["credential"],
      });
    }
  });

const modelSourceListResponseSchema = z.array(modelSourceResponseSchema);

export const MODEL_SOURCE_OBJECT_SCHEMAS = {
  ModelSourceCredentialRequest: modelSourceCredentialRequestSchema,
  ModelSourceResponse: modelSourceResponseSchema,
} as const;

export const MODEL_SOURCE_ENUMS = {
  ModelSourceConfigurationState: MODEL_SOURCE_CONFIGURATION_STATES,
  ModelSourceType: MODEL_SOURCE_TYPES,
  ModelSourceValidationAction: MODEL_SOURCE_VALIDATION_ACTIONS,
  ModelSourceValidationReason: MODEL_SOURCE_VALIDATION_REASONS,
  ModelSourceValidationStatus: MODEL_SOURCE_VALIDATION_STATUSES,
} as const;

export class ModelSourceContractError extends Error {
  readonly code = "model_source_contract_error";

  constructor() {
    super("server returned an unsupported model-source response");
    this.name = "ModelSourceContractError";
  }
}

export type ModelSourceResponse = z.infer<typeof modelSourceResponseSchema>;
export type ModelSourceCredentialRequest = z.input<
  typeof modelSourceCredentialRequestSchema
>;

export function parseModelSourceResponse(value: unknown) {
  try {
    return modelSourceResponseSchema.parse(value);
  } catch {
    throw new ModelSourceContractError();
  }
}

export function parseModelSourceListResponse(value: unknown) {
  let response: ModelSourceResponse[];
  try {
    response = modelSourceListResponseSchema.parse(value);
  } catch {
    throw new ModelSourceContractError();
  }
  assertExactModelSourceCoverage(
    response.map(({ source_type }) => source_type),
  );
  return response;
}

export function parseModelSourceCredentialRequest(value: unknown) {
  try {
    return modelSourceCredentialRequestSchema.parse(value);
  } catch {
    throw new ModelSourceContractError();
  }
}

export function assertExactModelSourceCoverage(sourceTypes: readonly string[]) {
  const actual = [...sourceTypes].sort();
  const expected = [...MODEL_SOURCE_TYPES].sort();
  if (
    actual.length !== expected.length ||
    actual.some((sourceType, index) => sourceType !== expected[index])
  ) {
    throw new ModelSourceContractError();
  }
}
