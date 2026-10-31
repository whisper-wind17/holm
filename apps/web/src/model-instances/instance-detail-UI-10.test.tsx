import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { readFileSync } from "node:fs";
import { screen, within, waitFor } from "@testing-library/react";
import axe from "axe-core";
import { describe, expect, it, vi } from "vitest";
import type { ModelInstance } from "../api/model-instance-contracts";
import { ApiError } from "../api/client";
import {
  parseInstanceTelemetry,
  parseGpuTelemetryList,
} from "../api/telemetry-contracts";
import {
  ADMIN_ME,
  FakeApiClient,
  renderApplication,
} from "../app/test-support";
import {
  UI10_INSTANCE_ID,
  UI10_INSTANCE_RESPONSE,
  UI10_GPU_RESPONSE,
} from "../app/telemetry-test-fixtures";

// FX-UI-10: synthetic instance with a deliberately long service name.
function client(status: ModelInstance["status"]) {
  const api = new FakeApiClient(ADMIN_ME);
  api.modelInstanceDetailResults.push({
    id: UI10_INSTANCE_ID,
    name: "遥测实例",
    artifact_id: UI10_INSTANCE_ID,
    internal_model_name: "测试模型",
    internal_endpoint: null,
    status,
    config_version: 1,
    updated_at: "2026-10-09T04:00:00Z",
    error_code: null,
    gpu_uuids: ["GPU-UI10"],
    gpu_memory_utilization: 0.9,
    restart_count: 0,
    runtime_type: "vllm",
    tensor_parallel_size: 1,
    context_length: 4096,
  });
  vi.spyOn(api, "getInstanceTelemetry").mockImplementation(() =>
    Promise.resolve(parseInstanceTelemetry(UI10_INSTANCE_RESPONSE)),
  );
  vi.spyOn(api, "getGpuTelemetry").mockImplementation(() =>
    Promise.resolve(parseGpuTelemetryList(UI10_GPU_RESPONSE)),
  );
  vi.spyOn(api, "getAiServices").mockResolvedValue({
    items: [
      {
        id: UI10_INSTANCE_ID,
        name: "长服务名称".repeat(20),
        description: "",
        enabled: true,
        public_model_name: "ui10",
        primary_instance_id: UI10_INSTANCE_ID,
        primary_instance_status: status,
        runtime_status: "Unavailable",
        route_version: 1,
      },
    ],
  });
  return api;
}

describe("UI-10 TC-UI-01 instance detail", () => {
  it("shows available values through strict parsing and secondary running actions; passes axe", async () => {
    const { container } = renderApplication(
      client("Running"),
      `/models/instances/${UI10_INSTANCE_ID}`,
    );
    const title = await screen.findByRole("heading", { name: "遥测实例" });
    const header = title.closest("header");
    if (!header) throw new Error("missing detail header");
    for (const button of within(header).getAllByRole("button"))
      expect(button).toHaveClass("pw-button--secondary");
    const summary = await screen.findByRole("region", { name: "运行摘要" });
    await waitFor(() => expect(summary).toHaveTextContent("86.4 tokens/s"));
    expect(summary).toHaveTextContent("73 ms");
    expect(summary).toHaveTextContent("3.5 %");
    expect(summary).not.toHaveTextContent("遥测暂不可用");
    expect((await axe.run(container)).violations).toEqual([]);
  });
  it("UI-11 keeps start primary and enabled delete secondary", async () => {
    renderApplication(
      client("Stopped"),
      `/models/instances/${UI10_INSTANCE_ID}`,
    );
    await screen.findByRole("heading", { name: "遥测实例" });
    expect(screen.getByRole("button", { name: "启动" })).toHaveClass(
      "pw-button--primary",
    );
    const remove = screen.getByRole("button", { name: "删除" });
    await waitFor(() => expect(remove).toBeEnabled());
    expect(remove).toHaveClass("pw-button--secondary");
  });
  it("UI-11 keeps a constrained right-aligned reason when bindings are unavailable; passes axe", async () => {
    const api = client("Stopped");
    vi.spyOn(api, "getAiServices").mockRejectedValue(
      new ApiError({ code: "internal_error", status: 503 }),
    );
    const { container } = renderApplication(
      api,
      `/models/instances/${UI10_INSTANCE_ID}`,
    );
    const remove = await screen.findByRole("button", { name: "删除" });
    expect(remove).toBeDisabled();
    const area = remove.closest(".pw-instance-actions-detail");
    if (!area) throw new Error("missing detail actions");
    const reason = area.querySelector(".pw-instance-actions-detail__reason");
    if (!reason) throw new Error("missing disabled reason");
    expect(reason).toHaveTextContent(
      "服务绑定信息暂不可用，停止、重启和删除需等待绑定确认。",
    );
    expect(reason.previousElementSibling).toContainElement(remove);
    // jsdom cannot measure layout: assert the sizing/wrapping contract, not fake pixel bounds.
    const css = readFileSync(
      resolve(dirname(fileURLToPath(import.meta.url)), "../styles/app.css"),
      "utf8",
    );
    const areaRule = css.match(/\.pw-instance-actions-detail \{([^}]+)\}/)?.[1];
    expect(areaRule).toContain("grid-template-columns: minmax(0, 1fr)");
    expect(areaRule).toContain("max-width: 100%");
    const reasonRule = css.match(
      /\.pw-instance-actions-detail__reason \{([^}]+)\}/,
    )?.[1];
    expect(reasonRule).toContain("inline-size: 0");
    expect(reasonRule).toContain("min-inline-size: 100%");
    expect(reasonRule).toContain("overflow-wrap: anywhere");
    expect(reasonRule).toContain("text-align: right");
    expect((await axe.run(container)).violations).toEqual([]);
  });
});
