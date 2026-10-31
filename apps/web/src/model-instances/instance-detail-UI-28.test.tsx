import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import axe from "axe-core";
import { describe, expect, it, vi } from "vitest";

import { parseModelArtifactDetail } from "../api/model-acquisition-contracts";
import type {
  ModelInstance,
  ModelInstanceEvent,
} from "../api/model-instance-contracts";
import {
  ADMIN_ME,
  DEFAULT_SYSTEM_SERVER,
  FakeApiClient,
  renderApplication,
} from "../app/test-support";

// FX-UI-28: synthetic instance, events, log lines and local-model detail.
const ID = "0198f58a-2586-7e37-bbf5-000000000028";
const ARTIFACT = "0198f58a-2586-7e37-bbf5-000000000029";
const MODEL = "0198f58a-2586-7e37-bbf5-000000000030";

function base(status: ModelInstance["status"], extra = {}): ModelInstance {
  return {
    artifact_id: ARTIFACT,
    config_version: 2,
    context_length: 4096,
    error_code: null,
    gpu_memory_utilization: 0.9,
    gpu_uuids: ["GPU-UI28"],
    id: ID,
    internal_endpoint: "http://127.0.0.1:18006/v1",
    internal_model_name: "mi_ui28",
    name: "ui28-instance",
    restart_count: 0,
    runtime_type: "vllm",
    status,
    tensor_parallel_size: 1,
    updated_at: "2026-10-14T04:00:00Z",
    ...extra,
  };
}

function ev(
  seed: number,
  to: ModelInstanceEvent["to_status"],
  from: ModelInstanceEvent["from_status"] = null,
  actor: ModelInstanceEvent["actor"] = "controller",
): ModelInstanceEvent {
  return {
    actor,
    detail: {},
    event_type: "StatusChanged",
    from_status: from,
    id: `0198f58a-2586-7e37-bbf5-${String(seed).padStart(12, "0")}`,
    occurred_at: `2026-10-14T04:0${String(seed)}:00Z`,
    to_status: to,
  };
}

function setup(
  status: ModelInstance["status"],
  options: {
    events?: ModelInstanceEvent[];
    extra?: object;
    lines?: string[];
  } = {},
) {
  const api = new FakeApiClient(ADMIN_ME);
  api.modelInstanceDetailResults.push(base(status, options.extra));
  const server = structuredClone(DEFAULT_SYSTEM_SERVER);
  if (server.agent.status !== "available") throw new Error("fixture");
  server.agent.gpus.devices = [
    {
      uuid: "GPU-UI28",
      display_index: 2,
      model: "L20",
      memory_bytes: 48 * 1024 ** 3,
      health: "healthy",
      health_reasons: [],
    },
  ];
  server.agent.gpus.discovery_state = "ready";
  vi.spyOn(api, "getSystemServer").mockResolvedValue(server);
  vi.spyOn(api, "getModelInstanceEvents").mockResolvedValue({
    items: options.events ?? [],
    next_cursor: null,
  });
  vi.spyOn(api, "getAiServices").mockResolvedValue({ items: [] });
  vi.spyOn(api, "artifactDetail").mockResolvedValue(
    parseModelArtifactDetail({
      created_at: "2026-10-01T00:00:00Z",
      display_name: "Qwen3",
      id: ARTIFACT,
      last_verified_at: null,
      model_id: MODEL,
      path: "/models/qwen3",
      referencing_instances: 1,
      repository_id: "Qwen/Qwen3",
      resolved_revision: "rev-abc",
      size_bytes: 1024,
      source: "Hugging Face",
      status: "Ready",
      storage_type: "Managed",
      updated_at: "2026-10-01T00:00:00Z",
      model: {
        architecture: null,
        modalities: null,
        precision: "BF16",
        quantization: null,
        context_length: null,
        license: null,
      },
      files: null,
      download_tasks: [],
      instances: [],
    }),
  );
  if (options.lines !== undefined)
    api.modelInstanceLogResults.push({
      entries: options.lines.map((text, sequence) => ({
        original_character_count: text.length,
        sequence,
        text,
        truncated: false,
      })),
      next_cursor: null,
    });
  return api;
}

describe("UI-28 instance detail", () => {
  it("orders tabs per PDD 9.4 and passes axe", async () => {
    const { container } = renderApplication(
      setup("Running"),
      `/models/instances/${ID}`,
    );
    await screen.findByRole("heading", { name: "ui28-instance", level: 1 });
    expect(screen.getAllByRole("tab").map((tab) => tab.textContent)).toEqual([
      "概览",
      "监控",
      "配置",
      "日志",
      "事件",
    ]);
    expect((await axe.run(container)).violations).toEqual([]);
  });

  it.each([
    ["Unhealthy", "实例运行异常", /5、15、45 秒间隔自动重启/],
    ["Starting", "启动中", /启动完成后关联的 AI 服务自动可用/],
    ["WaitingForResource", "等待 GPU 资源", /正在等待 GPU 2 释放/],
  ] as const)("shows the %s banner", async (status, title, text) => {
    renderApplication(setup(status), `/models/instances/${ID}`);
    const banner = (
      await screen.findByText(title, { selector: "strong" })
    ).closest(".pw-alert");
    expect(banner).toHaveTextContent(text);
  });

  it("shows the restart count and jumps to ERROR logs from the unhealthy banner", async () => {
    const api = setup("Unhealthy", {
      lines: ["INFO ready", "ERROR boom", "WARNING slow", "plain line"],
    });
    renderApplication(api, `/models/instances/${ID}`);
    const user = userEvent.setup();
    await user.click(
      await screen.findByRole("button", { name: "查看错误日志" }),
    );
    const log = await screen.findByLabelText("模型实例日志");
    expect(log).toHaveTextContent("ERROR boom");
    expect(log).not.toHaveTextContent("INFO ready");
    expect(screen.getByRole("combobox", { name: "级别" })).toHaveValue("ERROR");
  });

  it("states the automatic restart number", async () => {
    renderApplication(
      setup("Restarting", { extra: { restart_count: 2 } }),
      `/models/instances/${ID}`,
    );
    expect(
      await screen.findByText(/第 2 次自动重启（上限 3 次）/),
    ).toBeVisible();
  });

  it("shows deployment progress for waiting and fresh starts, not restarts", async () => {
    const first = renderApplication(
      setup("WaitingForResource"),
      `/models/instances/${ID}`,
    );
    expect(
      await screen.findByRole("region", { name: "部署进度" }),
    ).toBeVisible();
    first.unmount();
    const fresh = renderApplication(
      setup("Starting", { events: [ev(1, "Starting")] }),
      `/models/instances/${ID}`,
    );
    const progress = await screen.findByRole("region", { name: "部署进度" });
    expect(progress).toHaveTextContent("启动 Runtime");
    expect(progress).toHaveTextContent("已用");
    fresh.unmount();
    renderApplication(
      setup("Starting", {
        events: [ev(2, "Starting", "Stopped"), ev(1, "Stopped")],
      }),
      `/models/instances/${ID}`,
    );
    await screen.findByRole("img", { name: "启动中" });
    await waitFor(() => {
      expect(screen.queryByRole("region", { name: "部署进度" })).toBeNull();
    });
  });

  it("opens the explanation dialog before navigating for failed instances", async () => {
    renderApplication(
      setup("Failed", { extra: { error_code: "runtime_out_of_memory" } }),
      `/models/instances/${ID}`,
    );
    const user = userEvent.setup();
    const head = (
      await screen.findByRole("heading", { name: "ui28-instance", level: 1 })
    ).closest("header");
    if (head === null) throw new Error("header");
    await user.click(
      within(head).getByRole("button", { name: "修改配置并重试" }),
    );
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent("模型不能换");
    expect(dialog).toHaveTextContent("GPU 显存不足");
    await user.click(
      within(dialog).getByRole("button", { name: "前往部署向导" }),
    );
    await waitFor(() => {
      expect(screen.queryByRole("dialog", { name: /修改配置/ })).toBeNull();
    });
  });

  it("shows configuration fields, unrecognised values and the admin-only endpoint", async () => {
    const api = setup("Stopped");
    renderApplication(api, `/models/instances/${ID}#configuration`);
    const section = (
      await screen.findByRole("heading", { name: "配置" })
    ).closest("section");
    if (section === null) throw new Error("section");
    await waitFor(() =>
      expect(section).toHaveTextContent("Hugging Face · Qwen/Qwen3"),
    );
    expect(section).toHaveTextContent("rev-abc");
    expect(section).toHaveTextContent("BF16");
    expect(section).toHaveTextContent("未识别");
    expect(section).toHaveTextContent("vLLM 0.29.0");
    expect(section).toHaveTextContent("已释放，启动时优先使用");
    expect(section).toHaveTextContent("仅管理员可见");
    expect(section).toHaveTextContent("http://127.0.0.1:18006/v1");
  });

  it("keeps other configuration fields when model detail fails", async () => {
    const api = setup("Running");
    vi.spyOn(api, "artifactDetail").mockRejectedValue(new Error("offline"));
    renderApplication(api, `/models/instances/${ID}#configuration`);
    expect(
      await screen.findByText("模型信息暂不可用", { selector: "strong" }),
    ).toBeVisible();
    expect(screen.getByText("Tensor Parallel")).toBeVisible();
  });

  it("filters logs by keyword and level, copies only visible text, and never persists", async () => {
    const setItem = vi.spyOn(Storage.prototype, "setItem");
    const api = setup("Running", {
      lines: [
        "INFO ready",
        "ERROR boom",
        "WARN slow",
        "plain",
        "prompt=secret",
      ],
    });
    renderApplication(api, `/models/instances/${ID}#logs`);
    const user = userEvent.setup();
    const log = await screen.findByLabelText("模型实例日志");
    expect(log).toHaveTextContent("[敏感内容已隐藏]");
    expect(screen.getByRole("checkbox", { name: "自动滚动" })).toBeDisabled();
    await user.selectOptions(
      screen.getByRole("combobox", { name: "级别" }),
      "WARN",
    );
    expect(log).toHaveTextContent("WARN slow");
    expect(log).not.toHaveTextContent("plain");
    await user.selectOptions(
      screen.getByRole("combobox", { name: "级别" }),
      "ALL",
    );
    await user.type(
      screen.getByRole("searchbox", { name: "关键词" }),
      "secret",
    );
    expect(await screen.findByText("没有符合筛选条件的日志")).toBeVisible();
    await user.clear(screen.getByRole("searchbox", { name: "关键词" }));
    await user.type(screen.getByRole("searchbox", { name: "关键词" }), "boom");
    expect(screen.getByText(/当前筛选显示 1 行/)).toBeVisible();
    await user.click(screen.getByRole("button", { name: "复制可见日志" }));
    expect(await navigator.clipboard.readText()).toBe("ERROR boom");
    expect(setItem).not.toHaveBeenCalled();
    expect(window.location.search).toBe("");
    expect(window.location.hash).not.toContain("boom");
  });

  it("lists events with operator, action and result columns", async () => {
    renderApplication(
      setup("Failed", {
        events: [
          ev(1, "Running", "Starting", "admin"),
          ev(2, "Failed", "Running"),
        ],
      }),
      `/models/instances/${ID}#events`,
    );
    const table = await screen.findByRole("table", { name: "模型实例事件" });
    expect(
      within(table)
        .getAllByRole("columnheader")
        .map((cell) => cell.textContent),
    ).toEqual(["时间", "操作者", "动作", "结果"]);
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows[0]).toHaveTextContent("平台状态：运行中 → 运行失败失败");
    expect(rows[1]).toHaveTextContent("管理员状态：启动中 → 运行中成功");
  });
});
