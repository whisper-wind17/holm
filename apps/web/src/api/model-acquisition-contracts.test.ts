import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";
import type { ZodObject } from "zod";

import {
  ARTIFACT_STORAGE_TYPES,
  ARTIFACT_VERIFY_OBJECT_SCHEMAS,
  ARTIFACT_VERIFICATION_STATUS_OBJECT_SCHEMAS,
  DOWNLOAD_COMMAND_OBJECT_SCHEMAS,
  DOWNLOAD_COMMANDS,
  DOWNLOAD_ERROR_CODES,
  DOWNLOAD_FAILURE_STAGES,
  DOWNLOAD_TASK_OBJECT_SCHEMAS,
  DOWNLOAD_TASK_STATUSES,
  MODEL_ARTIFACT_OBJECT_SCHEMAS,
  MODEL_ARTIFACT_STATUSES,
  MODEL_ORIGIN_TYPES,
  ModelAcquisitionContractError,
  parseDownloadCommandResponse,
  parseDownloadTaskPage,
  parseModelArtifactPage,
} from "./model-acquisition-contracts";

interface JsonObjectSchema {
  enum?: string[];
  properties?: Record<string, unknown>;
  type?: string;
}

interface JsonSchemaSnapshot extends JsonObjectSchema {
  $defs?: Record<string, JsonObjectSchema>;
  title?: string;
}

const TEST_DIRECTORY = dirname(fileURLToPath(import.meta.url));
const SNAPSHOT_DIRECTORY = resolve(
  TEST_DIRECTORY,
  "../../../../tests/contract/snapshots",
);

describe("M2_12B JSON Schema to zod axes", () => {
  it.each([
    ["m2_10c_artifact_verify.json", ARTIFACT_VERIFY_OBJECT_SCHEMAS],
    [
      "m2_10c_artifact_verification_status.json",
      ARTIFACT_VERIFICATION_STATUS_OBJECT_SCHEMAS,
    ],
    ["m2_12b_download_tasks.json", DOWNLOAD_TASK_OBJECT_SCHEMAS],
    ["m2_12b_model_artifacts.json", MODEL_ARTIFACT_OBJECT_SCHEMAS],
    ["m2_12b_download_command.json", DOWNLOAD_COMMAND_OBJECT_SCHEMAS],
  ] as const)(
    "keeps every object field bidirectionally equal: %s",
    (name, registry) => {
      expect(() => {
        validateSnapshotAxis(readSnapshot(name), registryFields(registry));
      }).not.toThrow();
    },
  );

  it("positive control fails when one zod field is deleted", () => {
    const snapshot = readSnapshot("m2_12b_download_tasks.json");
    const fields = registryFields(DOWNLOAD_TASK_OBJECT_SCHEMAS);
    fields.DownloadTaskItemResponse = fields.DownloadTaskItemResponse?.filter(
      (field) => field !== "total_bytes",
    );
    expect(() => {
      validateSnapshotAxis(snapshot, fields);
    }).toThrow(/DownloadTaskItemResponse/);
  });

  it("positive control fails when the snapshot has a field absent from zod", () => {
    const snapshot = readSnapshot("m2_12b_model_artifacts.json");
    snapshot.properties = {
      ...snapshot.properties,
      injected_contract_field: {},
    };
    expect(() => {
      validateSnapshotAxis(
        snapshot,
        registryFields(MODEL_ARTIFACT_OBJECT_SCHEMAS),
      );
    }).toThrow(/ModelArtifactPageResponse/);
  });

  it("keeps every enum and error code bidirectionally equal", () => {
    const download = readSnapshot("m2_12b_download_tasks.json");
    const artifacts = readSnapshot("m2_12b_model_artifacts.json");
    const command = readSnapshot("m2_12b_download_command.json");
    expect(snapshotEnum(download, "DownloadTaskStatus")).toEqual([
      ...DOWNLOAD_TASK_STATUSES,
    ]);
    expect(snapshotEnum(download, "DownloadFailureStage")).toEqual([
      ...DOWNLOAD_FAILURE_STAGES,
    ]);
    expect(snapshotEnum(download, "ModelOriginType")).toEqual([
      ...MODEL_ORIGIN_TYPES,
    ]);
    expect(snapshotEnum(artifacts, "ModelArtifactStatus")).toEqual([
      ...MODEL_ARTIFACT_STATUSES,
    ]);
    expect(snapshotEnum(artifacts, "ArtifactStorageType")).toEqual([
      ...ARTIFACT_STORAGE_TYPES,
    ]);
    expect(snapshotEnum(command, "DownloadCommand")).toEqual([
      ...DOWNLOAD_COMMANDS,
    ]);
    expect(readJson("m2_12b_download_error_codes.json")).toEqual([
      ...DOWNLOAD_ERROR_CODES,
    ]);
  });

  it.each([
    [
      "download status",
      () =>
        parseDownloadTaskPage({
          items: [validDownloadTask({ status: "FutureStatus" })],
          next_cursor: null,
        }),
    ],
    [
      "model origin",
      () =>
        parseDownloadTaskPage({
          items: [validDownloadTask({ source: "FutureSource" })],
          next_cursor: null,
        }),
    ],
    [
      "failure stage",
      () =>
        parseDownloadTaskPage({
          items: [validDownloadTask({ failure_stage: "FutureStage" })],
          next_cursor: null,
        }),
    ],
    [
      "available command",
      () =>
        parseDownloadTaskPage({
          items: [validDownloadTask({ available_commands: ["future"] })],
          next_cursor: null,
        }),
    ],
    [
      "error code",
      () =>
        parseDownloadTaskPage({
          items: [validDownloadTask({ error_code: "future_error" })],
          next_cursor: null,
        }),
    ],
    [
      "artifact status",
      () =>
        parseModelArtifactPage(validArtifactPage({ status: "FutureStatus" })),
    ],
    [
      "storage type",
      () =>
        parseModelArtifactPage(
          validArtifactPage({ storage_type: "FutureStorage" }),
        ),
    ],
    [
      "command response",
      () =>
        parseDownloadCommandResponse({
          attempt: 1,
          command: "future",
          id: "0198f58a-2586-7e37-bbf5-fefe165e8082",
          staging_cleanup_requested: false,
          status: "Queued",
        }),
    ],
  ])(
    "throws a contract error for an unknown %s returned by the server",
    (_label, parse) => {
      expect(parse).toThrow(ModelAcquisitionContractError);
    },
  );
});

function readJson(name: string): unknown {
  return JSON.parse(readFileSync(resolve(SNAPSHOT_DIRECTORY, name), "utf8"));
}

function readSnapshot(name: string) {
  return readJson(name) as JsonSchemaSnapshot;
}

function registryFields(registry: Readonly<Record<string, ZodObject>>) {
  return Object.fromEntries(
    Object.entries(registry).map(([name, schema]) => [
      name,
      Object.keys(schema.shape).sort(),
    ]),
  ) as Record<string, string[] | undefined>;
}

function validateSnapshotAxis(
  snapshot: JsonSchemaSnapshot,
  fields: Record<string, string[] | undefined>,
) {
  const snapshotObjects = Object.fromEntries(
    Object.entries(snapshot.$defs ?? {})
      .filter(([, schema]) => schema.type === "object")
      .map(([name, schema]) => [
        name,
        Object.keys(schema.properties ?? {}).sort(),
      ]),
  );
  const rootName = snapshot.title;
  if (rootName === undefined || fields[rootName] === undefined) {
    throw new Error("missing named response root");
  }
  snapshotObjects[rootName] = Object.keys(snapshot.properties ?? {}).sort();
  expect(Object.keys(fields).sort()).toEqual(
    Object.keys(snapshotObjects).sort(),
  );
  for (const [name, zodFields] of Object.entries(fields)) {
    expect(zodFields, `${name} zod fields`).toEqual(snapshotObjects[name]);
  }
}

function snapshotEnum(snapshot: JsonSchemaSnapshot, name: string) {
  const values = snapshot.$defs?.[name]?.enum;
  if (values === undefined) throw new Error(`snapshot enum ${name} is missing`);
  return values;
}

function validDownloadTask(overrides: Record<string, unknown> = {}) {
  return {
    attempt: 1,
    available_commands: ["cancel"],
    completed_at: null,
    created_at: "2026-09-11T01:00:00Z",
    display_name: "Qwen3",
    downloaded_bytes: 0,
    error_code: null,
    failure_stage: null,
    id: "0198f58a-2586-7e37-bbf5-fefe165e8082",
    model_id: "0198f58a-2586-7e37-bbf5-fefe165e8083",
    repository_id: "Qwen/Qwen3",
    revision: null,
    source: "Hugging Face",
    staging_path: null,
    status: "Queued",
    total_bytes: null,
    updated_at: "2026-09-11T01:00:00Z",
    ...overrides,
  };
}

function validArtifactPage(overrides: Record<string, unknown> = {}) {
  return {
    items: [
      {
        created_at: "2026-09-11T00:00:00Z",
        display_name: "Qwen3",
        id: "0198f58a-2586-7e37-bbf5-fefe165e8084",
        last_verified_at: "2026-09-11T00:00:00Z",
        model_id: "0198f58a-2586-7e37-bbf5-fefe165e8083",
        path: "/models/qwen3",
        referencing_instances: null,
        repository_id: "Qwen/Qwen3",
        resolved_revision: "abc123",
        size_bytes: 1024,
        source: "Hugging Face",
        status: "Ready",
        storage_type: "Managed",
        updated_at: "2026-09-11T01:00:00Z",
        ...overrides,
      },
    ],
    next_cursor: null,
    summary: {
      active_download_tasks: 0,
      managed_bytes: 1024,
      storage_available_bytes: null,
    },
  };
}
