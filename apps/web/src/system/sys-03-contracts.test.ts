import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import {
  parseSystemComponentsResponse,
  SystemContractError,
  HOST_ENVIRONMENT_IDS,
  HOST_ENVIRONMENT_STATUSES,
  HOST_ENVIRONMENT_REASONS,
  DRIVER_COMPATIBILITIES,
  DRIVER_COMPATIBILITY_REASONS,
} from "../api/system-contracts";
import {
  DEFAULT_SYSTEM_COMPONENTS,
  DEFAULT_SYSTEM_SERVER,
} from "../app/test-support";
import {
  HOST_ENVIRONMENT_NAMES,
  HOST_ENVIRONMENT_STATUS_PRESENTATIONS,
  HOST_ENVIRONMENT_REASON_PRESENTATIONS,
  DRIVER_COMPATIBILITY_PRESENTATIONS,
  DRIVER_COMPATIBILITY_REASON_PRESENTATIONS,
  INFERENCE_ENGINE_NAMES,
  hostEnvironmentName,
  hostEnvironmentStatusPresentation,
  hostEnvironmentReason,
  driverCompatibilityPresentation,
  driverCompatibilityReason,
  inferenceEngineName,
  assertExactComponentPresentationKeys,
  buildPlatformStatus,
} from "./presentation";

// FX-SYS-03 extends the shared system fixture with an incompatible process engine.
function fixture() {
  return {
    ...structuredClone(DEFAULT_SYSTEM_COMPONENTS),
    inference_engines: [
      {
        name: "vllm",
        version: "0.29.0",
        launcher: "process",
        image_digest: null,
        active_instance_count: null,
        minimum_driver_version: "580.65.06",
        driver_compatibility: "incompatible",
        reason: "below_minimum",
      },
    ],
  };
}

const snapshot = JSON.parse(
  readFileSync(
    resolve(
      dirname(fileURLToPath(import.meta.url)),
      "../../../../tests/contract/snapshots/m1_09a_system_components.json",
    ),
    "utf8",
  ),
) as { $defs: Record<string, { enum: string[] }> };

describe("SYS_03 TC_HEALTH_18_19_20_21_26_27 host and engine contracts", () => {
  it("accepts both new groups and preserves null count and image", () => {
    expect(parseSystemComponentsResponse(fixture())).toEqual(fixture());
  });
  it.each([
    ["HostEnvironmentId", HOST_ENVIRONMENT_IDS, HOST_ENVIRONMENT_NAMES],
    [
      "HostEnvironmentStatus",
      HOST_ENVIRONMENT_STATUSES,
      HOST_ENVIRONMENT_STATUS_PRESENTATIONS,
    ],
    [
      "HostEnvironmentReason",
      HOST_ENVIRONMENT_REASONS,
      HOST_ENVIRONMENT_REASON_PRESENTATIONS,
    ],
    [
      "DriverCompatibility",
      DRIVER_COMPATIBILITIES,
      DRIVER_COMPATIBILITY_PRESENTATIONS,
    ],
    [
      "DriverCompatibilityReason",
      DRIVER_COMPATIBILITY_REASONS,
      DRIVER_COMPATIBILITY_REASON_PRESENTATIONS,
    ],
  ] as const)(
    "maps every server enum exactly once: %s",
    (name, values, mapping) => {
      const expected = required(snapshot.$defs[name]).enum;
      expect([...values].sort()).toEqual([...expected].sort());
      expect(Object.keys(mapping).sort()).toEqual([...expected].sort());
      // Both missing and extra mappings must fail the existing exactness guard.
      expect(() => {
        assertExactComponentPresentationKeys(
          Object.keys(mapping).slice(1),
          expected,
        );
      }).toThrow(SystemContractError);
      expect(() => {
        assertExactComponentPresentationKeys(
          [...Object.keys(mapping), "future"],
          expected,
        );
      }).toThrow(SystemContractError);
    },
  );
  it("names the two catalog engines and renders the exact driver instructions", () => {
    expect(INFERENCE_ENGINE_NAMES).toEqual({ vllm: "vLLM", sglang: "SGLang" });
    expect(
      driverCompatibilityReason("meets_minimum", "580.65.06", "595.45.04"),
    ).toBe("要求驱动 ≥ 580.65.06");
    expect(
      driverCompatibilityReason("below_minimum", "580.65.06", "579.99"),
    ).toBe("需先升级驱动：要求 ≥ 580.65.06，当前 579.99。");
    expect(hostEnvironmentReason("meets_minimum")).toBe("");
  });
  it.each([
    hostEnvironmentName,
    hostEnvironmentStatusPresentation,
    hostEnvironmentReason,
    driverCompatibilityPresentation,
    inferenceEngineName,
    (value: string) => driverCompatibilityReason(value, null, null),
  ])("rejects unknown presentation values", (present) => {
    expect(() => present("future")).toThrow(SystemContractError);
  });
  it.each(["id", "status", "reason"])("rejects unknown host %s", (field) => {
    const response = fixture();
    Object.assign(required(response.host_environment.items[0]), {
      [field]: "future",
    });
    expect(() => parseSystemComponentsResponse(response)).toThrow(
      SystemContractError,
    );
  });
  it.each(["driver_compatibility", "reason"])(
    "rejects unknown engine %s",
    (field) => {
      const response = fixture();
      Object.assign(required(response.inference_engines[0]), {
        [field]: "future",
      });
      expect(() => parseSystemComponentsResponse(response)).toThrow(
        SystemContractError,
      );
    },
  );
  it.each(["host", "engine", "environment"])(
    "rejects unknown fields in %s",
    (target) => {
      const response = fixture();
      const object =
        target === "host"
          ? required(response.host_environment.items[0])
          : target === "engine"
            ? required(response.inference_engines[0])
            : response.host_environment;
      Object.assign(object, { executable: "/private/sentinel" });
      expect(() => parseSystemComponentsResponse(response)).toThrow(
        SystemContractError,
      );
    },
  );
  it("does not add environment or driver failures to the global platform status", () => {
    const response = fixture();
    Object.assign(required(response.host_environment.items[0]), {
      status: "unsatisfied",
      reason: "below_minimum",
    });
    expect(
      buildPlatformStatus(
        DEFAULT_SYSTEM_SERVER,
        parseSystemComponentsResponse(response),
      ),
    ).toEqual(
      buildPlatformStatus(DEFAULT_SYSTEM_SERVER, DEFAULT_SYSTEM_COMPONENTS),
    );
  });
});

function required<T>(value: T | undefined): T {
  if (value === undefined)
    throw new Error("FX-SYS-03 fixture or snapshot entry is missing");
  return value;
}
