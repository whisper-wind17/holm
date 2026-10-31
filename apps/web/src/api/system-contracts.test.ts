import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";
import type { ZodObject } from "zod";

import {
  parseSystemComponentsResponse,
  parseSystemServerResponse,
  SYSTEM_COMPONENT_OBJECT_SCHEMAS,
  SYSTEM_SERVER_OBJECT_SCHEMAS,
  SystemContractError,
} from "./system-contracts";
import {
  DEFAULT_SYSTEM_COMPONENTS,
  DEFAULT_SYSTEM_SERVER,
} from "../app/test-support";

interface JsonObjectSchema {
  properties?: Record<string, unknown>;
  type?: string;
}

interface JsonSchemaSnapshot extends JsonObjectSchema {
  $defs: Record<string, JsonObjectSchema>;
}

const TEST_DIRECTORY = dirname(fileURLToPath(import.meta.url));
const SNAPSHOT_DIRECTORY = resolve(
  TEST_DIRECTORY,
  "../../../../tests/contract/snapshots",
);

describe("TC-HEALTH-18 system JSON Schema to zod field axis", () => {
  it.each([
    ["m1_09a_system_server.json", SYSTEM_SERVER_OBJECT_SCHEMAS],
    ["m1_09a_system_components.json", SYSTEM_COMPONENT_OBJECT_SCHEMAS],
  ] as const)(
    "keeps every object field bidirectionally equal: %s",
    (name, registry) => {
      expect(() => {
        validateSnapshotAxis(readSnapshot(name), registryFields(registry));
      }).not.toThrow();
    },
  );

  it("positive control fails when one zod field is deleted", () => {
    const snapshot = readSnapshot("m1_09a_system_server.json");
    const fields = registryFields(SYSTEM_SERVER_OBJECT_SCHEMAS);
    fields.SystemServerResponse = fields.SystemServerResponse?.filter(
      (field) => field !== "agent",
    );
    expect(() => {
      validateSnapshotAxis(snapshot, fields);
    }).toThrow(/SystemServerResponse/);
  });

  it("positive control fails when the snapshot has a field absent from zod", () => {
    const snapshot = readSnapshot("m1_09a_system_components.json");
    snapshot.properties = {
      ...snapshot.properties,
      injected_contract_field: {},
    };
    expect(() => {
      validateSnapshotAxis(
        snapshot,
        registryFields(SYSTEM_COMPONENT_OBJECT_SCHEMAS),
      );
    }).toThrow(/SystemComponentsResponse/);
  });
});

describe("TC-HEALTH-19 fail-loud system enums and strict responses", () => {
  it("parses the current strict server and component contracts", () => {
    expect(parseSystemServerResponse(DEFAULT_SYSTEM_SERVER)).toEqual(
      DEFAULT_SYSTEM_SERVER,
    );
    expect(parseSystemComponentsResponse(DEFAULT_SYSTEM_COMPONENTS)).toEqual(
      DEFAULT_SYSTEM_COMPONENTS,
    );
  });

  it.each([
    ["component id", "components.0.id", "Future Component"],
    ["component status", "components.0.status", "starting"],
    ["GPU discovery failure", "agent.gpus.failure_reason", "driver_changed"],
    ["GPU Health", "agent.gpus.devices.0.health", "warning"],
  ])("throws a contract error for unknown %s", (_label, path, value) => {
    const source = path.startsWith("components")
      ? structuredClone(DEFAULT_SYSTEM_COMPONENTS)
      : serverWithGpu();
    setPath(source, path, value);
    const parse = path.startsWith("components")
      ? parseSystemComponentsResponse
      : parseSystemServerResponse;
    expect(() => parse(source)).toThrow(SystemContractError);
  });

  it("rejects an extra runtime response field", () => {
    expect(() =>
      parseSystemServerResponse({
        ...DEFAULT_SYSTEM_SERVER,
        extra_internal_field: "must fail",
      }),
    ).toThrow(SystemContractError);
  });
});

function readSnapshot(name: string) {
  return JSON.parse(
    readFileSync(resolve(SNAPSHOT_DIRECTORY, name), "utf8"),
  ) as JsonSchemaSnapshot;
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
    Object.entries(snapshot.$defs)
      .filter(([, schema]) => schema.type === "object")
      .map(([name, schema]) => [
        name,
        Object.keys(schema.properties ?? {}).sort(),
      ]),
  );
  const actualRootName = Object.keys(fields).find(
    (name) =>
      name === "SystemServerResponse" || name === "SystemComponentsResponse",
  );
  if (actualRootName === undefined) {
    throw new Error("missing named response root");
  }
  snapshotObjects[actualRootName] = Object.keys(
    snapshot.properties ?? {},
  ).sort();
  expect(Object.keys(fields).sort()).toEqual(
    Object.keys(snapshotObjects).sort(),
  );
  for (const [name, zodFields] of Object.entries(fields)) {
    expect(zodFields, `${name} zod fields`).toEqual(snapshotObjects[name]);
  }
}

function serverWithGpu() {
  const server = structuredClone(DEFAULT_SYSTEM_SERVER);
  if (server.agent.status !== "available") {
    throw new Error("test fixture must expose the agent");
  }
  server.agent.gpus = {
    devices: [
      {
        display_index: 0,
        health: "healthy",
        health_reasons: [],
        memory_bytes: 51_539_607_552,
        model: "NVIDIA L20",
        uuid: "GPU-test-0",
      },
    ],
    discovery_state: "ready",
    failure_reason: "none",
    snapshot_id: "snapshot-test",
    topology_links: [],
  };
  return server;
}

function setPath(
  source: Record<string, unknown>,
  path: string,
  value: unknown,
) {
  const segments = path.split(".");
  let target: unknown = source;
  for (const segment of segments.slice(0, -1)) {
    target = Array.isArray(target)
      ? target[Number(segment)]
      : (target as Record<string, unknown>)[segment];
  }
  const last = segments.at(-1);
  if (last === undefined) throw new Error("empty path");
  if (Array.isArray(target)) target[Number(last)] = value;
  else (target as Record<string, unknown>)[last] = value;
}
