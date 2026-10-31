import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";
import type { ZodObject } from "zod";

import {
  assertExactModelSourceCoverage,
  MODEL_SOURCE_ENUMS,
  MODEL_SOURCE_OBJECT_SCHEMAS,
  ModelSourceContractError,
  parseModelSourceCredentialRequest,
  parseModelSourceListResponse,
  parseModelSourceResponse,
} from "./model-source-contracts";

interface JsonObjectSchema {
  properties?: Record<string, unknown>;
  type?: string;
}

interface JsonSchemaSnapshot extends JsonObjectSchema {
  $defs?: Record<string, JsonObjectSchema>;
}

const SNAPSHOT_DIRECTORY = resolve(
  dirname(fileURLToPath(import.meta.url)),
  "../../../../tests/contract/snapshots",
);

describe("M2_13A model-source snapshot axes", () => {
  it("keeps every strict-object field bidirectionally equal", () => {
    expect(() => {
      validateObjectAxis(snapshotObjects(), zodObjectFields());
    }).not.toThrow();
  });

  it("positive control fails when one zod field is deleted", () => {
    const fields = zodObjectFields();
    fields.ModelSourceResponse = fields.ModelSourceResponse?.filter(
      (field) => field !== "next_action",
    );
    expect(() => {
      validateObjectAxis(snapshotObjects(), fields);
    }).toThrow(/ModelSourceResponse/);
  });

  it("positive control fails when the snapshot has one extra field", () => {
    const objects = snapshotObjects();
    objects.ModelSourceCredentialRequest = [
      ...(objects.ModelSourceCredentialRequest ?? []),
      "future_field",
    ].sort();
    expect(() => {
      validateObjectAxis(objects, zodObjectFields());
    }).toThrow(/ModelSourceCredentialRequest/);
  });

  it("keeps all five enum value sets bidirectionally equal", () => {
    expect(frontendEnums()).toEqual(readJson("m2_13a_model_source_enums.json"));
  });

  it("positive control fails when the frontend enum has an extra member", () => {
    const mutated = frontendEnums();
    mutated.ModelSourceValidationStatus = [
      ...(mutated.ModelSourceValidationStatus ?? []),
      "future_status",
    ];
    expect(() => {
      validateEnumAxis(mutated);
    }).toThrow(/ModelSourceValidationStatus/);
  });
});

describe("M2_13A strict runtime parsing", () => {
  it("parses the complete two-source response and closed write request", () => {
    expect(parseModelSourceListResponse(rawModelSources())).toHaveLength(2);
    expect(
      parseModelSourceCredentialRequest({
        clear: false,
        credential: "source-token",
        current_password: "administrator-password",
        source_type: "Hugging Face",
      }),
    ).toMatchObject({ clear: false, source_type: "Hugging Face" });
  });

  it("positive control rejects an unknown enum value", () => {
    expect(() =>
      parseModelSourceResponse({
        ...rawModelSources()[0],
        verification_status: "future_status",
      }),
    ).toThrow(ModelSourceContractError);
  });

  it("positive control rejects a three-character tail", () => {
    expect(() =>
      parseModelSourceResponse({
        ...rawModelSources()[0],
        credential_last4: "abc",
      }),
    ).toThrow(ModelSourceContractError);
  });

  it.each([
    ["missing", [rawModelSources()[0]]],
    ["duplicate", [rawModelSources()[0], rawModelSources()[0]]],
  ])("positive control rejects %s source coverage", (_name, value) => {
    expect(() => parseModelSourceListResponse(value)).toThrow(
      ModelSourceContractError,
    );
  });

  it("positive control rejects a write with neither operation or both operations", () => {
    expect(() =>
      parseModelSourceCredentialRequest({
        clear: false,
        credential: null,
        current_password: "administrator-password",
        source_type: "ModelScope",
      }),
    ).toThrow(ModelSourceContractError);
    expect(() =>
      parseModelSourceCredentialRequest({
        clear: true,
        credential: "source-token",
        current_password: "administrator-password",
        source_type: "ModelScope",
      }),
    ).toThrow(ModelSourceContractError);
  });

  it("source coverage helper itself fails for an extra value", () => {
    expect(() => {
      assertExactModelSourceCoverage([
        "Hugging Face",
        "ModelScope",
        "FutureSource",
      ]);
    }).toThrow(ModelSourceContractError);
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
  const request = readSchema("m2_13a_model_source_credential_request.json");
  const response = readSchema("m2_13a_model_source_list_response.json");
  return {
    ModelSourceCredentialRequest: Object.keys(request.properties ?? {}).sort(),
    ModelSourceResponse: Object.keys(
      response.$defs?.ModelSourceResponse?.properties ?? {},
    ).sort(),
  } as Record<string, string[] | undefined>;
}

function zodObjectFields() {
  return Object.fromEntries(
    Object.entries(MODEL_SOURCE_OBJECT_SCHEMAS).map(([name, schema]) => [
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
    Object.entries(MODEL_SOURCE_ENUMS).map(([name, values]) => [
      name,
      [...values],
    ]),
  ) as Record<string, string[] | undefined>;
}

function validateEnumAxis(frontend: Record<string, string[] | undefined>) {
  const snapshot = readJson("m2_13a_model_source_enums.json");
  expect(Object.keys(frontend).sort()).toEqual(Object.keys(snapshot).sort());
  for (const [name, values] of Object.entries(frontend)) {
    expect(values, `${name} enum values`).toEqual(snapshot[name]);
  }
}

export function rawModelSources(): Array<Record<string, unknown>> {
  return [
    {
      configuration_state: "configured",
      credential_last4: "8f2a",
      next_action: null,
      source_type: "Hugging Face",
      verification_reason_code: "credential_valid",
      verification_status: "accessible",
      verified_at: "2026-09-11T05:00:00Z",
    },
    {
      configuration_state: "unconfigured",
      credential_last4: null,
      next_action: null,
      source_type: "ModelScope",
      verification_reason_code: null,
      verification_status: null,
      verified_at: null,
    },
  ];
}
