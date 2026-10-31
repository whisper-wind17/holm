import { ADMIN_ME, FakeApiClient } from "../app/test-support";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import {
  UI10_INSTANCE_RESPONSE,
  UI10_GPU_RESPONSE,
} from "../app/telemetry-test-fixtures";
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { z } from "zod";
import {
  parseInstanceTelemetry,
  parseGpuTelemetryList,
  instanceTelemetrySchema,
  gpuTelemetryListSchema,
  serviceTelemetrySchema,
  overviewTelemetrySchema,
} from "./telemetry-contracts";

// FX-UI-10: same backend response field snapshot as the Python guard.
const snapshot: unknown = JSON.parse(
  readFileSync(
    resolve(
      dirname(fileURLToPath(import.meta.url)),
      "../../../../tests/fixtures/contracts/telemetry-response-fields.json",
    ),
    "utf8",
  ),
);
const gpu = gpuTelemetryListSchema.shape.items.element;
const schemas = {
  InstanceTelemetryResponse: instanceTelemetrySchema,
  InstanceMetricValuesResponse: instanceTelemetrySchema.shape.current,
  InstancePointResponse: instanceTelemetrySchema.shape.series.element,
  ServiceRequestWindowResponse: instanceTelemetrySchema.shape.window_1h,
  GpuTelemetryListResponse: gpuTelemetryListSchema,
  GpuTelemetryResponse: gpu,
  GpuMetricValuesResponse: gpu.shape.current,
  GpuPointResponse: gpu.shape.series.element,
  ServiceTelemetryResponse: serviceTelemetrySchema,
  ServiceMetricValuesResponse: serviceTelemetrySchema.shape.current,
  ServicePointResponse: serviceTelemetrySchema.shape.series.element,
  OverviewTelemetryResponse: overviewTelemetrySchema,
  OverviewValuesResponse: overviewTelemetrySchema.shape.current,
};
function assertFieldsMatch(objects: Record<string, z.ZodObject>) {
  expect(
    Object.fromEntries(
      Object.entries(objects).map(([name, schema]) => [
        name,
        Object.keys(schema.shape).sort(),
      ]),
    ),
  ).toEqual(snapshot);
}

describe("UI-10 TC-TM-09 telemetry field guard", () => {
  it("matches every response and nested point against the backend snapshot", () => {
    assertFieldsMatch(schemas);
    expect(serviceTelemetrySchema.shape.window_1h).toBe(
      instanceTelemetrySchema.shape.window_1h,
    );
  });
  it("positive control: fails when zod omits quality", () => {
    expect(() => {
      assertFieldsMatch({
        ...schemas,
        InstanceTelemetryResponse: instanceTelemetrySchema.omit({
          quality: true,
        }),
      });
    }).toThrow();
  });
});

describe("UI-10 real response shape", () => {
  it("parses current and series quality, status and GPU health", () => {
    expect(parseInstanceTelemetry(UI10_INSTANCE_RESPONSE)).toEqual(
      UI10_INSTANCE_RESPONSE,
    );
    expect(parseGpuTelemetryList(UI10_GPU_RESPONSE)).toEqual(UI10_GPU_RESPONSE);
  });
  it("rejects extra fields at every response and nested object", async () => {
    const api = new FakeApiClient(ADMIN_ME);
    const service = await api.getServiceTelemetry(
      UI10_INSTANCE_RESPONSE.instance_id,
    );
    const overview = await api.getTelemetryOverview();
    const item = UI10_GPU_RESPONSE.items[0];
    if (!item) throw new Error("missing FX-UI-10 GPU");
    const samples: Record<keyof typeof schemas, unknown> = {
      InstanceTelemetryResponse: UI10_INSTANCE_RESPONSE,
      InstanceMetricValuesResponse: UI10_INSTANCE_RESPONSE.current,
      InstancePointResponse: UI10_INSTANCE_RESPONSE.series[0],
      ServiceRequestWindowResponse: UI10_INSTANCE_RESPONSE.window_1h,
      GpuTelemetryListResponse: UI10_GPU_RESPONSE,
      GpuTelemetryResponse: item,
      GpuMetricValuesResponse: item.current,
      GpuPointResponse: item.series[0],
      ServiceTelemetryResponse: service,
      ServiceMetricValuesResponse: service.current,
      ServicePointResponse: {
        ...service.current,
        sampled_at: UI10_INSTANCE_RESPONSE.sampled_at,
        quality: "partial",
        telemetry_status: "unavailable",
      },
      OverviewTelemetryResponse: overview,
      OverviewValuesResponse: overview.current,
    };
    for (const [name, schema] of Object.entries(schemas)) {
      const sample = schema.parse(samples[name as keyof typeof schemas]);
      expect(
        schema.safeParse({ ...sample, unexpected: "body-sentinel" }).success,
      ).toBe(false);
    }
  });
  it("rejects missing and invalid metadata without swallowing the error", () => {
    expect(() =>
      parseInstanceTelemetry({ ...UI10_INSTANCE_RESPONSE, quality: undefined }),
    ).toThrow();
    expect(() =>
      parseInstanceTelemetry({ ...UI10_INSTANCE_RESPONSE, quality: "unknown" }),
    ).toThrow();
    expect(() => {
      parseInstanceTelemetry({
        ...UI10_INSTANCE_RESPONSE,
        series: [
          { ...UI10_INSTANCE_RESPONSE.series[0], telemetry_status: "unknown" },
        ],
      });
    }).toThrow();
    const gpuItem = UI10_GPU_RESPONSE.items[0];
    if (!gpuItem) throw new Error("missing FX-UI-10 GPU");
    expect(() => {
      parseGpuTelemetryList({
        ...UI10_GPU_RESPONSE,
        items: [{ ...gpuItem, current: { ...gpuItem.current, health: 1 } }],
      });
    }).toThrow();
    expect(
      parseGpuTelemetryList({
        ...UI10_GPU_RESPONSE,
        items: [{ ...gpuItem, current: { ...gpuItem.current, health: null } }],
      }).items[0]?.current.health,
    ).toBeNull();
  });
});
