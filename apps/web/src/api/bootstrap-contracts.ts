import { z } from "zod";

import {
  agentObservationSchema,
  SYSTEM_COMPONENT_OBJECT_SCHEMAS,
  HOST_ENVIRONMENT_IDS,
} from "./system-contracts";

/**
 * Runtime boundary for the three Bootstrap endpoints.
 *
 * Every enum below is a transcription of the server's own `StrEnum`; the
 * bidirectional guard in `src/setup/contracts.test.ts` reads the Python source
 * and fails when either side gains or loses a value. Nothing here re-decides a
 * verdict: the client maps identifiers to Chinese sentences and refuses to
 * render an identifier it does not know.
 */

/** `X-PWS-Bootstrap-Token`. The plaintext never enters a URL. */
export const BOOTSTRAP_TOKEN_HEADER = "X-PWS-Bootstrap-Token";

export const BOOTSTRAP_STATUS_PATH = "/api/v1/bootstrap/status";
export const BOOTSTRAP_CHECK_PATH = "/api/v1/bootstrap/check";
export const BOOTSTRAP_ADMIN_PATH = "/api/v1/bootstrap/admin";

/** The seven detection cards, in the server's table order. */
export const BOOTSTRAP_CHECK_IDS = [
  "gpu",
  "nvidia_driver",
  "docker_compose",
  "nvidia_container_toolkit",
  "disk",
  "memory",
  "holm_components",
] as const;

export const BOOTSTRAP_CHECK_OUTCOMES = [
  "passed",
  "warning",
  "failed",
] as const;

export const BOOTSTRAP_CHECK_REASONS = [
  "agent_unavailable",
  "gpu_devices_present",
  "gpu_discovery_unavailable",
  "gpu_no_devices",
  "gpu_inventory_inconsistent",
  "gpu_unhealthy_devices",
  "gpu_health_unknown",
  "driver_meets_minimum",
  "driver_below_minimum",
  "driver_minimum_undetermined",
  "driver_version_unknown",
  "cuda_version_unknown",
  "disk_sufficient",
  "disk_below_recommended",
  "disk_below_minimum",
  "disk_capacity_unknown",
  "memory_sufficient",
  "memory_below_recommended",
  "memory_below_minimum",
  "memory_capacity_unknown",
  "docker_compose_meets_minimum",
  "docker_below_minimum",
  "compose_below_minimum",
  "docker_version_undetermined",
  "compose_version_undetermined",
  "toolkit_meets_minimum",
  "toolkit_below_minimum",
  "toolkit_version_undetermined",
  "components_ready",
  "components_not_deployed",
  "components_abnormal",
] as const;

/**
 * `UsernameError` codes raised by `holm_common.usernames`. The server, not
 * the client, decides whether an account name is acceptable; the client only
 * renders the refusal it was given.
 */
export const BOOTSTRAP_USERNAME_REASONS = [
  "empty",
  "invisible_character",
  "not_text",
  "too_long",
  "too_short",
  "unsupported_characters",
  "whitespace",
] as const;

/** Every error code the three Bootstrap endpoints can answer with. */
export const BOOTSTRAP_ERROR_CODES = [
  "bootstrap_checks_failed",
  "bootstrap_token_invalid",
  "invalid_password",
  "invalid_username",
  "not_found",
  "password_confirmation_mismatch",
  "username_taken",
] as const;

export type BootstrapCheckId = (typeof BOOTSTRAP_CHECK_IDS)[number];
export type BootstrapCheckOutcome = (typeof BOOTSTRAP_CHECK_OUTCOMES)[number];
export type BootstrapCheckReason = (typeof BOOTSTRAP_CHECK_REASONS)[number];
export type BootstrapUsernameReason =
  (typeof BOOTSTRAP_USERNAME_REASONS)[number];

/**
 * account-name shape. The single definition lives in
 * `holm_common.usernames`; these constants are held equal to it by
 * `src/setup/contracts.test.ts`, and the server stays the deciding party.
 */
export const ACCOUNT_NAME_MIN_LENGTH = 3;
export const ACCOUNT_NAME_MAX_LENGTH = 32;
export const ACCOUNT_NAME_PATTERN = /^[a-z0-9._-]+$/;
/** 0–80 characters, one rule for both. */
export const DISPLAY_NAME_MAX_LENGTH = 80;

export class BootstrapContractError extends Error {
  readonly code = "bootstrap_contract_error";

  constructor() {
    super("server returned an unsupported bootstrap response");
    this.name = "BootstrapContractError";
  }
}

const bootstrapStatusResponseSchema = z.strictObject({
  initialized: z.literal(false),
  token_expires_at: z.iso.datetime({ offset: true }),
});

const bootstrapCheckSchema = z.strictObject({
  id: z.enum(BOOTSTRAP_CHECK_IDS),
  outcome: z.enum(BOOTSTRAP_CHECK_OUTCOMES),
  reasons: z.array(z.enum(BOOTSTRAP_CHECK_REASONS)).min(1),
});

const bootstrapCheckReportSchema = z.strictObject({
  agent: agentObservationSchema,
  host_environment: SYSTEM_COMPONENT_OBJECT_SCHEMAS.HostEnvironmentResponse,
  blocking: z.array(z.enum(BOOTSTRAP_CHECK_IDS)),
  can_complete: z.boolean(),
  checks: z.array(bootstrapCheckSchema),
  observed_at: z.iso.datetime({ offset: true }),
});

const bootstrapAdminResponseSchema = z.strictObject({
  csrf_token: z.string().min(1),
  must_change_password: z.boolean(),
  system_initialized_at: z.iso.datetime({ offset: true }),
});

export type BootstrapStatusResponse = z.infer<
  typeof bootstrapStatusResponseSchema
>;
export type BootstrapCheck = z.infer<typeof bootstrapCheckSchema>;
export type BootstrapCheckReport = z.infer<typeof bootstrapCheckReportSchema>;
export type BootstrapAdminResponse = z.infer<
  typeof bootstrapAdminResponseSchema
>;

export interface BootstrapAdminRequest {
  confirm_password: string;
  display_name: string;
  password: string;
  username: string;
}

export function parseBootstrapStatusResponse(value: unknown) {
  try {
    return bootstrapStatusResponseSchema.parse(value);
  } catch {
    throw new BootstrapContractError();
  }
}

/**
 * Parse a detection report and refuse a partial one.
 *
 * The card coverage check is here rather than in the page: a report that omits
 * a card would otherwise render as "six cards, all fine", which is exactly the
 * silent pass the initialization blocking rule depends on not happening.
 */
export function parseBootstrapCheckReport(value: unknown) {
  let report: BootstrapCheckReport;
  try {
    report = bootstrapCheckReportSchema.parse(value);
  } catch {
    throw new BootstrapContractError();
  }
  assertExactCheckIdCoverage(report.checks.map(({ id }) => id));
  assertExactCoverage(
    report.host_environment.items.map(({ id }) => id),
    HOST_ENVIRONMENT_IDS,
  );
  return report;
}

export function parseBootstrapAdminResponse(value: unknown) {
  try {
    return bootstrapAdminResponseSchema.parse(value);
  } catch {
    throw new BootstrapContractError();
  }
}

export function assertExactCheckIdCoverage(checkIds: readonly string[]) {
  assertExactCoverage(checkIds, BOOTSTRAP_CHECK_IDS);
}

/** Fail when two identifier sets stop being equal in either direction. */
export function assertExactCoverage(
  actualIds: readonly string[],
  expectedIds: readonly string[],
) {
  const actual = [...actualIds].sort();
  const expected = [...expectedIds].sort();
  if (
    actual.length !== expected.length ||
    actual.some((value, index) => value !== expected[index])
  ) {
    throw new BootstrapContractError();
  }
}

/**
 * Read the failing card identifiers out of a 409 `bootstrap_checks_failed`.
 *
 * An unknown identifier throws rather than being dropped: silently ignoring one
 * would let the interface claim "nothing is blocking" while the server keeps
 * refusing, which is the exact loop the "failure blocks completion" rule
 * has to stay out of.
 */
export function parseBootstrapBlocking(value: unknown): BootstrapCheckId[] {
  if (!Array.isArray(value) || value.length === 0) {
    throw new BootstrapContractError();
  }
  return value.map((item) => {
    if (
      typeof item !== "string" ||
      !(BOOTSTRAP_CHECK_IDS as readonly string[]).includes(item)
    ) {
      throw new BootstrapContractError();
    }
    return item as BootstrapCheckId;
  });
}

export function parseBootstrapUsernameReason(value: unknown): string {
  if (
    typeof value !== "string" ||
    !(BOOTSTRAP_USERNAME_REASONS as readonly string[]).includes(value)
  ) {
    throw new BootstrapContractError();
  }
  return value;
}
