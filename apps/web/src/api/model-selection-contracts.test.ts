import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";
import type { ZodObject } from "zod";

import {
  MODEL_SELECTION_ENUMS,
  MODEL_SELECTION_OBJECT_SCHEMAS,
  ModelSelectionContractError,
  parseModelCatalogPage,
  parseResolveResponse,
} from "./model-selection-contracts";

interface JsonObjectSchema {
  properties?: Record<string, unknown>;
  title?: string;
  type?: string;
}

interface JsonSchemaSnapshot extends JsonObjectSchema {
  $defs?: Record<string, JsonObjectSchema>;
}

const SNAPSHOT_DIRECTORY = resolve(
  dirname(fileURLToPath(import.meta.url)),
  "../../../../tests/contract/snapshots",
);
const SNAPSHOTS = [
  "m2_13b_resolve_request.json",
  "m2_13b_resolved_preview_response.json",
  "m2_13b_external_path_accepted_response.json",
  "m2_13b_unresolved_response.json",
  "m2_13b_registered_model_response.json",
  "m2_13b_registration_unresolved_response.json",
  "m2_13b_download_created_response.json",
  "m2_13b_model_catalog_page.json",
] as const;

describe("M2_13B JSON Schema to zod axes", () => {
  it("keeps every strict-object field bidirectionally equal", () => {
    expect(() => {
      validateObjectAxis(snapshotObjects(), zodObjectFields());
    }).not.toThrow();
  });

  it("positive control fails when one zod field is deleted", () => {
    const fields = zodObjectFields();
    fields.ResolvedModelResponse = fields.ResolvedModelResponse?.filter(
      (field) => field !== "resolved_revision",
    );
    expect(() => {
      validateObjectAxis(snapshotObjects(), fields);
    }).toThrow(/ResolvedModelResponse/);
  });

  it("positive control fails when the snapshot has one extra field", () => {
    const objects = snapshotObjects();
    objects.DownloadCreatedResponse = [
      ...(objects.DownloadCreatedResponse ?? []),
      "future_field",
    ].sort();
    expect(() => {
      validateObjectAxis(objects, zodObjectFields());
    }).toThrow(/DownloadCreatedResponse/);
  });

  it("keeps every requested enum value set bidirectionally equal", () => {
    expect(frontendEnums()).toEqual(
      readJson("m2_13b_model_selection_enums.json"),
    );
  });

  it("positive control fails when the frontend enum has a server-absent member", () => {
    const values = frontendEnums();
    values.LocalAvailability = [
      ...(values.LocalAvailability ?? []),
      "future_availability",
    ];
    expect(() => {
      validateEnumAxis(values);
    }).toThrow(/LocalAvailability/);
  });
});

describe("M2_13B strict runtime parsing", () => {
  it("positive control rejects an unknown enum value", () => {
    expect(() =>
      parseModelCatalogPage({
        items: [validCatalogItem({ local_availability: "future_state" })],
        next_cursor: null,
      }),
    ).toThrow(ModelSelectionContractError);
  });

  it("positive control rejects an unknown outcome", () => {
    expect(() => parseResolveResponse({ outcome: "future_outcome" })).toThrow(
      ModelSelectionContractError,
    );
  });
});

function readJson(name: string) {
  return JSON.parse(
    readFileSync(resolve(SNAPSHOT_DIRECTORY, name), "utf8"),
  ) as Record<string, string[]>;
}

function readSchema(name: string) {
  return JSON.parse(
    readFileSync(resolve(SNAPSHOT_DIRECTORY, name), "utf8"),
  ) as JsonSchemaSnapshot;
}

function snapshotObjects() {
  const objects: Record<string, string[] | undefined> = {};
  for (const name of SNAPSHOTS) {
    const snapshot = readSchema(name);
    collectObject(objects, snapshot);
    for (const definition of Object.values(snapshot.$defs ?? {})) {
      collectObject(objects, definition);
    }
  }
  return objects;
}

function collectObject(
  objects: Record<string, string[] | undefined>,
  schema: JsonObjectSchema,
) {
  if (schema.type !== "object" || schema.title === undefined) return;
  const fields = Object.keys(schema.properties ?? {}).sort();
  const existing = objects[schema.title];
  if (existing !== undefined) expect(fields, schema.title).toEqual(existing);
  objects[schema.title] = fields;
}

function zodObjectFields() {
  return Object.fromEntries(
    Object.entries(MODEL_SELECTION_OBJECT_SCHEMAS).map(([name, schema]) => [
      name,
      Object.keys((schema as ZodObject).shape).sort(),
    ]),
  ) as Record<string, string[] | undefined>;
}

function validateObjectAxis(
  snapshot: Record<string, string[] | undefined>,
  zod: Record<string, string[] | undefined>,
) {
  expect(Object.keys(zod).sort()).toEqual(Object.keys(snapshot).sort());
  for (const [name, fields] of Object.entries(zod)) {
    expect(fields, `${name} zod fields`).toEqual(snapshot[name]);
  }
}

function frontendEnums() {
  return Object.fromEntries(
    Object.entries(MODEL_SELECTION_ENUMS).map(([name, values]) => [
      name,
      [...values],
    ]),
  ) as Record<string, string[] | undefined>;
}

function validateEnumAxis(frontend: Record<string, string[] | undefined>) {
  const snapshot = readJson("m2_13b_model_selection_enums.json");
  expect(Object.keys(frontend).sort()).toEqual(Object.keys(snapshot).sort());
  for (const [name, values] of Object.entries(frontend)) {
    expect(values, `${name} enum values`).toEqual(snapshot[name]);
  }
}

export function validCatalogItem(overrides: Record<string, unknown> = {}) {
  return {
    architecture: { state: "identified", value: "Qwen3ForCausalLM" },
    display_name: "Catalog display name",
    estimated_size_bytes: { state: "identified", value: 1024 },
    id: "0198f58a-2586-7e37-bbf5-fefe165e8083",
    local_availability: "absent",
    modalities: { state: "identified", value: ["text"] },
    references: { artifacts: 0, download_tasks: 0, model_instances: null },
    repository_id: "Qwen/Qwen3",
    revision: "—",
    source: "Hugging Face",
    ...overrides,
  };
}
