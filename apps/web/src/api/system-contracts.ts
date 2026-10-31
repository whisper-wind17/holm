import { z } from "zod";

export const OBSERVATION_UNKNOWN_REASONS = [
  "not_collected",
  "source_unavailable",
  "empty_value",
  "query_failed",
  "not_deployed",
  "protocol_invalid",
] as const;

export const SYSTEM_COMPONENT_IDS = [
  "Edge",
  "Control Plane",
  "Telemetry Collector",
  "Gateway",
  "Scheduler",
  "Download Manager",
  "Request Ledger Writer",
  "Maintenance",
  "Database",
  "Host Agent",
] as const;

export const SYSTEM_COMPONENT_STATUSES = [
  "ready",
  "degraded",
  "unavailable",
  "not_deployed",
] as const;

const observationUnknownReasonSchema = z.enum(OBSERVATION_UNKNOWN_REASONS);

const knownUInt64Schema = z.strictObject({
  state: z.literal("known"),
  value: z.number().int().nonnegative(),
});

const unknownUInt64Schema = z.strictObject({
  reason: observationUnknownReasonSchema,
  state: z.literal("unknown"),
});

const uint64ObservationSchema = z.discriminatedUnion("state", [
  knownUInt64Schema,
  unknownUInt64Schema,
]);

const knownStringSchema = z.strictObject({
  state: z.literal("known"),
  value: z.string().min(1),
});

const unknownStringSchema = z.strictObject({
  reason: observationUnknownReasonSchema,
  state: z.literal("unknown"),
});

const stringObservationSchema = z.discriminatedUnion("state", [
  knownStringSchema,
  unknownStringSchema,
]);

const unknownValueSchema = z.strictObject({
  reason: observationUnknownReasonSchema,
  state: z.literal("unknown"),
});

const gpuDeviceSchema = z.strictObject({
  display_index: z.number().int().nonnegative().nullable().default(null),
  health: z.enum(["healthy", "unhealthy", "unknown"]),
  health_reasons: z.array(
    z.enum([
      "ecc_errors_detected",
      "query_unsupported",
      "query_failed",
      "protocol_invalid",
    ]),
  ),
  memory_bytes: z.number().int().nonnegative(),
  model: z.string().min(1),
  uuid: z.string().min(1),
});

const gpuTopologyLinkSchema = z.strictObject({
  gpu_a_uuid: z.string().min(1),
  gpu_b_uuid: z.string().min(1),
  nvlink_state: z.enum(["present", "absent", "unsupported", "query_failed"]),
  p2p_read_state: z.enum(["present", "absent", "unsupported", "query_failed"]),
  pcie_relation: z
    .enum([
      "internal",
      "single_switch",
      "multiple_switches",
      "host_bridge",
      "numa_node",
      "system",
    ])
    .nullable()
    .default(null),
  pcie_state: z.enum(["present", "absent", "unsupported", "query_failed"]),
});

const gpuInventorySchema = z.strictObject({
  devices: z.array(gpuDeviceSchema),
  discovery_state: z.enum(["ready", "no_devices", "unavailable"]),
  failure_reason: z.enum([
    "none",
    "no_devices",
    "library_unavailable",
    "driver_unavailable",
    "permission_denied",
    "initialization_failed",
    "enumeration_failed",
    "snapshot_changed",
    "protocol_invalid",
  ]),
  snapshot_id: z.string().nullable().default(null),
  topology_links: z.array(gpuTopologyLinkSchema),
});

const hostInfoSchema = z.strictObject({
  available_disk_bytes: uint64ObservationSchema,
  cuda_driver_version: stringObservationSchema,
  driver_version: stringObservationSchema,
  logical_cpu_count: uint64ObservationSchema,
  total_disk_bytes: uint64ObservationSchema,
  total_memory_bytes: uint64ObservationSchema,
});

const agentUnavailableSchema = z.strictObject({
  reason: z.enum([
    "connection_failed",
    "timeout",
    "access_denied",
    "rpc_failed",
  ]),
  status: z.literal("unavailable"),
});

const agentAvailableSchema = z.strictObject({
  gpus: gpuInventorySchema,
  host: hostInfoSchema,
  status: z.literal("available"),
});

/** The Agent observation shared by ``/system/server`` and ``/bootstrap/check``.
 *
 * Exported so the initialization wizard consumes this one definition instead of
 * declaring a second Agent DTO for the same server model. */
export const agentObservationSchema = z.discriminatedUnion("status", [
  agentAvailableSchema,
  agentUnavailableSchema,
]);

const systemServerResponseSchema = z.strictObject({
  agent: agentObservationSchema,
  observed_at: z.iso.datetime({ offset: true }),
});

const noComponentDetailsSchema = z.strictObject({
  kind: z.literal("none"),
});

const knownTimestampSchema = z.strictObject({
  state: z.literal("known"),
  value: z.iso.datetime({ offset: true }),
});

const timestampObservationSchema = z.discriminatedUnion("state", [
  knownTimestampSchema,
  unknownValueSchema,
]);

const databaseDetailsSchema = z.strictObject({
  kind: z.literal("database"),
  schema_revision: stringObservationSchema,
});

const platformVersionSchema = z.strictObject({ build_version: z.string() });

const requestLedgerWriterDetailsSchema = z.strictObject({
  kind: z.literal("request_ledger_writer"),
  oldest_event_at: timestampObservationSchema,
  pending_event_count: uint64ObservationSchema,
});

const componentSummarySchema = z.strictObject({
  abnormal_count: z.number().int().nonnegative(),
  not_deployed_count: z.number().int().nonnegative(),
  ready_count: z.number().int().nonnegative(),
  status: z.enum(["ready", "degraded"]),
});

const systemComponentSchema = z.strictObject({
  details: z.discriminatedUnion("kind", [
    noComponentDetailsSchema,
    requestLedgerWriterDetailsSchema,
    databaseDetailsSchema,
  ]),
  id: z.enum(SYSTEM_COMPONENT_IDS),
  reason: z.enum([
    "probe_ready",
    "probe_unreachable",
    "gateway_route_snapshot_unavailable",
    "gateway_ledger_unavailable",
    "agent_available",
    "agent_unavailable",
    "self_ready",
    "dependency_ready",
    "dependency_unavailable",
    "heartbeat_fresh",
    "heartbeat_stale",
    "heartbeat_degraded",
    "heartbeat_stopping",
    "no_heartbeat",
    "no_signal_source",
    "signal_unavailable",
    "scheduler_backlog",
    "scheduler_leases_expired",
    "ledger_backlog",
    "ledger_unavailable",
  ]),
  signal_sources: z.array(
    z.enum([
      "self",
      "database",
      "heartbeat",
      "jobs",
      "ledger_backlog",
      "agent",
      "probe",
    ]),
  ),
  status: z.enum(SYSTEM_COMPONENT_STATUSES),
  version: stringObservationSchema,
});

export const HOST_ENVIRONMENT_IDS = [
  "nvidia_driver",
  "docker",
  "compose",
  "nvidia_container_toolkit",
] as const;
export const HOST_ENVIRONMENT_STATUSES = [
  "satisfied",
  "unsatisfied",
  "unknown",
] as const;
export const HOST_ENVIRONMENT_REASONS = [
  "meets_minimum",
  "below_minimum",
  "agent_unavailable",
  "current_unavailable",
  "minimum_not_declared",
  "version_unparseable",
] as const;
export const DRIVER_COMPATIBILITIES = [
  "compatible",
  "incompatible",
  "unknown",
] as const;
export const DRIVER_COMPATIBILITY_REASONS = [
  "meets_minimum",
  "below_minimum",
  "agent_unavailable",
  "driver_unavailable",
  "minimum_not_declared",
  "version_unparseable",
] as const;

const hostEnvironmentItemSchema = z.strictObject({
  id: z.enum(HOST_ENVIRONMENT_IDS),
  current: stringObservationSchema,
  minimum: z.string().nullable(),
  status: z.enum(HOST_ENVIRONMENT_STATUSES),
  reason: z.enum(HOST_ENVIRONMENT_REASONS),
});
const hostEnvironmentSchema = z.strictObject({
  cuda_driver_version: stringObservationSchema,
  items: z.array(hostEnvironmentItemSchema),
});
const inferenceEngineSchema = z.strictObject({
  name: z.string(),
  version: z.string(),
  launcher: z.string(),
  image_digest: z.string().nullable(),
  active_instance_count: z.number().int().nonnegative().nullable(),
  minimum_driver_version: z.string().nullable(),
  driver_compatibility: z.enum(DRIVER_COMPATIBILITIES),
  reason: z.enum(DRIVER_COMPATIBILITY_REASONS),
});

const systemComponentsResponseSchema = z.strictObject({
  components: z.array(systemComponentSchema),
  host_environment: hostEnvironmentSchema,
  inference_engines: z.array(inferenceEngineSchema),
  platform: platformVersionSchema,
  observed_at: z.iso.datetime({ offset: true }),
  summary: componentSummarySchema,
});

export const SYSTEM_SERVER_OBJECT_SCHEMAS = {
  AgentAvailableResponse: agentAvailableSchema,
  AgentUnavailableResponse: agentUnavailableSchema,
  GpuDeviceResponse: gpuDeviceSchema,
  GpuInventoryResponse: gpuInventorySchema,
  GpuTopologyLinkResponse: gpuTopologyLinkSchema,
  HostInfoResponse: hostInfoSchema,
  KnownString: knownStringSchema,
  KnownUInt64: knownUInt64Schema,
  SystemServerResponse: systemServerResponseSchema,
  UnknownString: unknownStringSchema,
  UnknownUInt64: unknownUInt64Schema,
} as const;

export const SYSTEM_COMPONENT_OBJECT_SCHEMAS = {
  HostEnvironmentItem: hostEnvironmentItemSchema,
  HostEnvironmentResponse: hostEnvironmentSchema,
  InferenceEngineResponse: inferenceEngineSchema,
  ComponentSummaryResponse: componentSummarySchema,
  DatabaseDetails: databaseDetailsSchema,
  PlatformVersionResponse: platformVersionSchema,
  KnownTimestamp: knownTimestampSchema,
  KnownUInt64: knownUInt64Schema,
  UnknownUInt64: unknownUInt64Schema,
  KnownString: knownStringSchema,
  NoComponentDetails: noComponentDetailsSchema,
  RequestLedgerWriterDetails: requestLedgerWriterDetailsSchema,
  SystemComponentResponse: systemComponentSchema,
  SystemComponentsResponse: systemComponentsResponseSchema,
  UnknownString: unknownStringSchema,
  UnknownValue: unknownValueSchema,
} as const;

export class SystemContractError extends Error {
  readonly code = "system_contract_error";

  constructor() {
    super("server returned an unsupported system response");
    this.name = "SystemContractError";
  }
}

export type AgentObservation = z.infer<typeof agentObservationSchema>;
export type SystemServerResponse = z.infer<typeof systemServerResponseSchema>;
export type SystemComponentsResponse = z.infer<
  typeof systemComponentsResponseSchema
>;
export type HostEnvironmentItem = z.infer<typeof hostEnvironmentItemSchema>;
export type InferenceEngine = z.infer<typeof inferenceEngineSchema>;
export type SystemComponent = z.infer<typeof systemComponentSchema>;
export type GpuDevice = z.infer<typeof gpuDeviceSchema>;
export type UInt64Observation = z.infer<typeof uint64ObservationSchema>;
export type StringObservation = z.infer<typeof stringObservationSchema>;

export function parseSystemServerResponse(value: unknown) {
  try {
    return systemServerResponseSchema.parse(value);
  } catch {
    throw new SystemContractError();
  }
}

export function parseSystemComponentsResponse(value: unknown) {
  let response: SystemComponentsResponse;
  try {
    response = systemComponentsResponseSchema.parse(value);
  } catch {
    throw new SystemContractError();
  }
  assertExactSystemComponentCoverage(response.components.map(({ id }) => id));
  return response;
}

export function assertExactSystemComponentCoverage(
  componentIds: readonly string[],
) {
  const actual = [...componentIds].sort();
  const expected = [...SYSTEM_COMPONENT_IDS].sort();
  if (
    actual.length !== expected.length ||
    actual.some((componentId, index) => componentId !== expected[index])
  ) {
    throw new SystemContractError();
  }
}
