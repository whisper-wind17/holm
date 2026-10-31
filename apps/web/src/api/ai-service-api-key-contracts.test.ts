import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import {
  parseAiServiceList,
  PUBLIC_MODEL_NAME_PATTERN,
  renameAiServiceRequestSchema,
} from "./ai-service-contracts";
import {
  ApiKeyContractError,
  parseApiKeyList,
  parseCreatedApiKey,
} from "./api-key-contracts";

describe("PROTO_07 OpenAPI-aligned service and API key contracts", () => {
  it("PROTO_07 parses the service fields exposed by the backend contract", () => {
    expect(
      parseAiServiceList({
        items: [
          {
            description: "日常对话",
            enabled: true,
            id: uuid(1),
            primary_instance_id: uuid(2),
            primary_instance_status: "Running",
            name: "研发助手",
            public_model_name: "qwen-mini",
            route_version: 1,
            runtime_status: "Available",
          },
        ],
      }).items[0]?.public_model_name,
    ).toBe("qwen-mini");
  });

  it("PROTO_07 rejects plaintext in a list response but accepts it in creation", () => {
    const created = {
      api_key: "holm_sk_lookup_secret_1234",
      created_at: "2026-09-15T03:00:00Z",
      display_prefix: "holm_sk_",
      expires_at: null,
      id: uuid(3),
      last4: "1234",
      last_used_at: null,
      name: "开发环境",
      revoked_at: null,
      service_ids: [],
      status: "Active",
    };

    expect(parseCreatedApiKey(created).api_key).toBe(created.api_key);
    expect(() =>
      parseApiKeyList({ items: [{ ...created, effective_services: [] }] }),
    ).toThrow(ApiKeyContractError);
  });
});

function uuid(seed: number) {
  return `0198f58a-2586-7e37-bbf5-${seed.toString().padStart(12, "0")}`;
}

it("M4_11A aligns rename fields and name constraints with the server request schema", () => {
  const schema = JSON.parse(
    readFileSync(
      resolve(
        dirname(fileURLToPath(import.meta.url)),
        "../../../../tests/contract/snapshots/m4_11a_rename_ai_service_request.json",
      ),
      "utf8",
    ),
  ) as {
    properties: Record<string, unknown>;
    required: string[];
    additionalProperties: boolean;
  };
  expect(Object.keys(renameAiServiceRequestSchema.shape).sort()).toEqual(
    Object.keys(schema.properties).sort(),
  );
  expect(schema.required.sort()).toEqual(
    Object.keys(renameAiServiceRequestSchema.shape).sort(),
  );
  expect(schema.additionalProperties).toBe(false);
  expect(schema.properties.public_model_name).toMatchObject({
    minLength: 2,
    maxLength: 64,
    pattern: `^${PUBLIC_MODEL_NAME_PATTERN}$`,
  });
  expect(schema.properties.expected_route_version).toMatchObject({
    type: "integer",
    minimum: 1,
  });
  for (const name of ["ab", "a".repeat(64), "model-1.v2_name"]) {
    expect(
      renameAiServiceRequestSchema.safeParse({
        public_model_name: name,
        confirm_public_model_name: name,
        expected_route_version: 1,
      }).success,
    ).toBe(true);
  }
});
