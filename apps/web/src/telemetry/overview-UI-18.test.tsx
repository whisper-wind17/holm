import axe from "axe-core";
import { screen, within } from "@testing-library/react";
import { expect, it } from "vitest";
import {
  ADMIN_ME,
  FakeApiClient,
  renderApplication,
} from "../app/test-support";

// FX-UI-18: synthetic instance and usage values based on UI-15 contract fixtures.
it("UI-18 keeps short today usage in three rows and short request metrics in columns, with axe", async () => {
  const api = new FakeApiClient(ADMIN_ME);
  api.modelInstanceResults.push({
    items: [
      {
        id: "0198f58a-2586-7e37-bbf5-000000001818",
        artifact_id: "0198f58a-2586-7e37-bbf5-000000001818",
        name: "验收实例",
        internal_model_name: "fixture",
        internal_endpoint: null,
        status: "Running",
        config_version: 1,
        context_length: 4096,
        error_code: null,
        gpu_memory_utilization: 0.8,
        restart_count: 0,
        gpu_uuids: ["FX-UI-18-GPU"],
        runtime_type: "vllm",
        tensor_parallel_size: 1,
        updated_at: "2026-10-10T10:00:00Z",
      },
    ],
  });
  renderApplication(api, "/overview");
  const usage = await screen.findByRole("link", {
    name: "今日用量 · 查看用量",
  });
  await within(usage).findByText("活跃用户");
  expect(usage.querySelector("dl")).toHaveClass(
    "pw-overview-card__metrics--list",
  );
  expect(usage.querySelectorAll("dl > div")).toHaveLength(3);
  expect(within(usage).getAllByText("0")).toHaveLength(3);
  const requests = screen.getByRole("link", { name: "请求 · 查看监控" });
  expect(requests.querySelector("dl")).not.toHaveClass(
    "pw-overview-card__metrics--list",
  );
  expect((await axe.run(document.body)).violations).toEqual([]);
});
