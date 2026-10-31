import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import axe from "axe-core";
import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import {
  MODEL_INSTANCE_STATUSES,
  ModelInstanceContractError,
  type ModelInstance,
  type ModelInstanceStatus,
  parseModelInstance,
} from "../api/model-instance-contracts";
import {
  ADMIN_ME,
  FakeApiClient,
  renderApplication,
} from "../app/test-support";

const TEST_DIRECTORY = dirname(fileURLToPath(import.meta.url));
const SERVER_MODEL_INSTANCES_SOURCE = resolve(
  TEST_DIRECTORY,
  "../../../../packages/domain/src/holm_domain/model_instances.py",
);

function sortedSet(values: readonly string[]) {
  return [...new Set(values)].sort();
}

function serverModelInstanceStatuses(
  source = readFileSync(SERVER_MODEL_INSTANCES_SOURCE, "utf8"),
) {
  const enumBody =
    // 锚到「下一个 class」而不是具体的 GpuAllocationStatus：M3-01 在两者之间
    // 插入了 ModelInstanceEventType，写死下一个类名会把事件枚举读成状态。
    /class ModelInstanceStatus\(StrEnum\):([\s\S]*?)\n\n\nclass /.exec(
      source,
    )?.[1];
  expect(
    enumBody,
    "server ModelInstanceStatus enum must be discoverable",
  ).toBeDefined();
  const values = [
    ...(enumBody ?? "").matchAll(/=\s*"([A-Za-z][A-Za-z0-9]*)"/g),
  ].map((match) => match[1] ?? "");
  expect(
    values.length,
    "server ModelInstanceStatus enum must not read as empty",
  ).toBeGreaterThan(0);
  return sortedSet(values);
}

function assertExactStatusCoverage(source?: string) {
  expect(sortedSet(MODEL_INSTANCE_STATUSES)).toEqual(
    serverModelInstanceStatuses(source),
  );
}

describe("FIX_01 model instance status contract", () => {
  it("keeps the frontend status constant equal to the backend enum", () => {
    assertExactStatusCoverage();

    const mutatedServer = readFileSync(
      SERVER_MODEL_INSTANCES_SOURCE,
      "utf8",
    ).replace(
      '    FAILED = "Failed"',
      '    FAILED = "Failed"\n    FUTURE_STATE = "FutureState"',
    );
    expect(() => {
      assertExactStatusCoverage(mutatedServer);
    }).toThrow("expected");
  });

  it("accepts the two backend states and still rejects an unknown state", () => {
    for (const status of ["Restarting", "Unhealthy"] as const) {
      expect(parseModelInstance(instance({ status })).status).toBe(status);
    }
    expect(() =>
      parseModelInstance({ ...instance(), status: "FutureState" }),
    ).toThrow(ModelInstanceContractError);
  });

  it.each([
    ["Restarting", "重启中"],
    ["Unhealthy", "运行异常"],
  ] as const)(
    "renders %s in the list and details without axe violations",
    async (status, label) => {
      const listClient = new FakeApiClient(ADMIN_ME);
      listClient.modelInstanceResults.push({ items: [instance({ status })] });
      const list = renderApplication(listClient, "/models");

      const table = await screen.findByRole("table", { name: "模型实例" });
      expect(await within(table).findByText(label)).toBeVisible();
      let axeResult = await axe.run(document.body, {
        rules: { "color-contrast": { enabled: false } },
      });
      expect(axeResult.passes.flatMap((rule) => rule.nodes)).not.toHaveLength(
        0,
      );
      expect(axeResult.violations).toEqual([]);
      list.unmount();

      const detailClient = new FakeApiClient(ADMIN_ME);
      detailClient.modelInstanceDetailResults.push(instance({ status }));
      renderApplication(detailClient, `/models/instances/${INSTANCE_ID}`);

      expect((await screen.findAllByText(label)).length).toBeGreaterThan(0);
      axeResult = await axe.run(document.body, {
        rules: { "color-contrast": { enabled: false } },
      });
      expect(axeResult.passes.flatMap((rule) => rule.nodes)).not.toHaveLength(
        0,
      );
      expect(axeResult.violations).toEqual([]);
    },
  );
});

const ARTIFACT_ID = uuid(1);
const INSTANCE_ID = uuid(2);

function uuid(seed: number) {
  return `0198f58a-2586-7e37-bbf5-${seed.toString().padStart(12, "0")}`;
}

function instance(
  overrides: Partial<ModelInstance> & { status?: ModelInstanceStatus } = {},
): ModelInstance {
  return {
    artifact_id: ARTIFACT_ID,
    config_version: 1,
    updated_at: "2026-10-08T09:00:00Z",
    context_length: 4096,
    error_code: null,
    gpu_memory_utilization: 0.9,
    restart_count: 0,
    gpu_uuids: ["GPU-fixture-0"],
    id: INSTANCE_ID,
    internal_endpoint: null,
    name: "Qwen3-0.6B",
    internal_model_name: "Qwen3-0.6B",
    runtime_type: "vllm",
    status: "Running",
    tensor_parallel_size: 1,
    ...overrides,
  };
}
