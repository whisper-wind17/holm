import axe from "axe-core";
import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { ModelInstance } from "../api/model-instance-contracts";
import {
  ADMIN_ME,
  FakeApiClient,
  renderApplication,
} from "../app/test-support";

const INSTANCE_ID = "0198f58a-2586-7e37-bbf5-000000000506";

describe("UI-15 M5-06 Overview needs-attention section", () => {
  it("M5-06 shows the unknown-request count and latest time when items exist", async () => {
    const apiClient = new FakeApiClient(ADMIN_ME);
    apiClient.modelInstanceResults.push({ items: [instance()] });
    apiClient.overviewAttentionResults.push({
      items: [
        {
          count: 3,
          kind: "unknown_requests",
          severity: "notice",
          target_id: null,
          target_name: null,
          detail: null,
          latest_at: "2026-09-17T08:30:00Z",
        },
      ],
    });
    renderApplication(apiClient, "/overview");

    const heading = await screen.findByRole("heading", { name: "需要处理" });
    const section = heading.closest("section");
    expect(section).not.toBeNull();
    const scope = within(section as HTMLElement);
    expect(
      await scope.findByText("3 个调用在平台重启前未收到最终结果"),
    ).toBeVisible();
    expect(
      (section as HTMLElement).querySelector(
        'time[datetime="2026-09-17T08:30:00Z"]',
      ),
    ).not.toBeNull();

    const result = await axe.run(document, {
      rules: { "color-contrast": { enabled: false } },
    });
    expect(result.passes.flatMap((rule) => rule.nodes)).not.toHaveLength(0);
    expect(
      result.violations.filter(
        (violation) =>
          violation.impact === "critical" || violation.impact === "serious",
      ),
    ).toEqual([]);
  });

  it("M5-06 keeps the empty section when there is nothing to handle", async () => {
    const apiClient = new FakeApiClient(ADMIN_ME);
    apiClient.modelInstanceResults.push({ items: [instance()] });
    apiClient.overviewAttentionResults.push({ items: [] });
    renderApplication(apiClient, "/overview");

    expect(await screen.findByRole("heading", { name: "GPU" })).toBeVisible();
    expect(await screen.findByText("暂无需要处理的事项")).toBeVisible();
    expect(screen.getByRole("heading", { name: "需要处理" })).toBeVisible();
    expect(screen.queryByText(/未收到最终结果/)).not.toBeInTheDocument();
  });

  it("M5-06 keeps the overview usable when the attention query fails", async () => {
    const apiClient = new FakeApiClient(ADMIN_ME);
    apiClient.modelInstanceResults.push({ items: [instance()] });
    apiClient.overviewAttentionResults.push(new Error("unavailable"));
    renderApplication(apiClient, "/overview");

    expect(await screen.findByText("无法获取需要处理的事项")).toBeVisible();
    expect(screen.getByRole("heading", { name: "GPU" })).toBeVisible();
  });
});

function instance(): ModelInstance {
  return {
    artifact_id: "0198f58a-2586-7e37-bbf5-000000000507",
    config_version: 1,
    updated_at: "2026-10-08T09:00:00Z",
    context_length: 4096,
    error_code: null,
    gpu_memory_utilization: 0.9,
    restart_count: 0,
    gpu_uuids: ["GPU-fixture-2"],
    id: INSTANCE_ID,
    internal_endpoint: null,
    name: "Qwen2.5-0.5B",
    internal_model_name: "Qwen2.5-0.5B",
    runtime_type: "vllm",
    status: "Running",
    tensor_parallel_size: 1,
  };
}
