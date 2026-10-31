import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import {
  MODEL_SOURCE_VALIDATION_STATUSES,
  ModelSourceContractError,
} from "../api/model-source-contracts";
import { ORIGIN_TYPE_LABELS } from "../local-models/presentation";
import {
  assertDistinctValidationPresentations,
  assertExactMappingKeys,
  assertModelSourcePresentationMappings,
  MODEL_SOURCE_TYPE_LABELS,
  MODEL_SOURCE_VALIDATION_PRESENTATIONS,
  modelSourceValidationPresentation,
} from "./presentation";

describe("M2_13A exact and reusable presentation vocabulary", () => {
  it("keeps all five mappings exact and reuses the existing source names", () => {
    expect(() => {
      assertModelSourcePresentationMappings();
    }).not.toThrow();
    expect(MODEL_SOURCE_TYPE_LABELS["Hugging Face"]).toBe(
      ORIGIN_TYPE_LABELS["Hugging Face"],
    );
    expect(MODEL_SOURCE_TYPE_LABELS.ModelScope).toBe(
      ORIGIN_TYPE_LABELS.ModelScope,
    );
    const source = readFileSync(
      resolve(dirname(fileURLToPath(import.meta.url)), "presentation.ts"),
      "utf8",
    );
    expect(source).not.toMatch(/Hugging Face|ModelScope/);
  });

  it("gives every validation conclusion a distinct icon and Chinese label", () => {
    const presentations = MODEL_SOURCE_VALIDATION_STATUSES.map(
      (status) => MODEL_SOURCE_VALIDATION_PRESENTATIONS[status],
    );
    expect(new Set(presentations.map(({ icon }) => icon)).size).toBe(5);
    expect(new Set(presentations.map(({ label }) => label)).size).toBe(5);
    expect(MODEL_SOURCE_VALIDATION_PRESENTATIONS.rate_limited).not.toEqual(
      MODEL_SOURCE_VALIDATION_PRESENTATIONS.invalid_credential,
    );
    expect(
      MODEL_SOURCE_VALIDATION_PRESENTATIONS.rate_limited.label,
    ).not.toMatch(/凭证|错误|过期/);
  });

  it("positive control rejects rate limiting collapsed into invalid credentials", () => {
    const collapsed = {
      ...MODEL_SOURCE_VALIDATION_PRESENTATIONS,
      rate_limited: MODEL_SOURCE_VALIDATION_PRESENTATIONS.invalid_credential,
    };
    expect(() => {
      assertDistinctValidationPresentations(Object.values(collapsed));
    }).toThrow(ModelSourceContractError);
  });

  it("positive control rejects a deleted or server-absent mapping", () => {
    expect(() => {
      assertExactMappingKeys(
        Object.keys(MODEL_SOURCE_VALIDATION_PRESENTATIONS).filter(
          (key) => key !== "accessible",
        ),
        MODEL_SOURCE_VALIDATION_STATUSES,
      );
    }).toThrow(ModelSourceContractError);
    expect(() => {
      assertExactMappingKeys(
        [
          ...Object.keys(MODEL_SOURCE_VALIDATION_PRESENTATIONS),
          "future_status",
        ],
        MODEL_SOURCE_VALIDATION_STATUSES,
      );
    }).toThrow(ModelSourceContractError);
  });

  it("positive control rejects an unknown presentation value", () => {
    expect(() => modelSourceValidationPresentation("future_status")).toThrow(
      ModelSourceContractError,
    );
  });
});
