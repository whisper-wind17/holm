import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import axe from "axe-core";
import { describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client";
import type { ModelInstance } from "../api/model-instance-contracts";
import type { InstanceTelemetry } from "../api/telemetry-contracts";
import {
  ADMIN_ME,
  DEFAULT_SYSTEM_SERVER,
  FakeApiClient,
  renderApplication,
} from "../app/test-support";
import { UI10_INSTANCE_RESPONSE } from "../app/telemetry-test-fixtures";

// FX-UI-27: synthetic instances/inventory derived from existing contracts;
// telemetry reuses FX-UI-10's recorded response shape, never prototype data.
function item(
  name: string,
  status: ModelInstance["status"] = "Running",
  overrides: Partial<ModelInstance> = {},
): ModelInstance {
  return {
    id: name,
    name,
    status,
    artifact_id: "artifact",
    config_version: 1,
    updated_at: "2026-10-14T04:00:00Z",
    context_length: 4096,
    error_code: null,
    gpu_memory_utilization: 0.9,
    restart_count: 0,
    gpu_uuids: ["gpu-0"],
    internal_endpoint: null,
    internal_model_name: "internal",
    runtime_type: "vllm",
    tensor_parallel_size: 1,
    ...overrides,
  };
}
function setup(items: ModelInstance[]) {
  const api = new FakeApiClient(ADMIN_ME);
  const list = vi.spyOn(api, "getModelInstances").mockResolvedValue({ items });
  const server = structuredClone(DEFAULT_SYSTEM_SERVER);
  if (server.agent.status !== "available")
    throw new Error("FX-UI-27 inventory unavailable");
  server.agent.gpus.devices = Array.from({ length: 4 }, (_, i) => ({
    uuid: `gpu-${String(i)}`,
    display_index: i,
    health: i === 3 ? ("unhealthy" as const) : ("healthy" as const),
    health_reasons: [],
    model: "Test GPU",
    memory_bytes: 24 * 1024 ** 3,
  }));
  vi.spyOn(api, "getSystemServer").mockResolvedValue(server);
  vi.spyOn(api, "getModelArtifacts").mockResolvedValue({
    items: [
      {
        id: "artifact",
        display_name: "本地测试模型",
        model_id: "model",
        created_at: "2026-10-14T04:00:00Z",
        updated_at: "2026-10-14T04:00:00Z",
        last_verified_at: null,
        path: "/fixture",
        referencing_instances: 1,
        repository_id: "test/model",
        resolved_revision: "rev",
        size_bytes: 1024,
        source: "ModelScope",
        status: "Ready",
        storage_type: "Managed",
      },
    ],
    next_cursor: null,
    summary: {
      active_download_tasks: 0,
      managed_bytes: 1024,
      storage_available_bytes: 2048,
    },
  });
  const telemetry = vi
    .spyOn(api, "getInstanceTelemetry")
    .mockImplementation((id) =>
      Promise.resolve({
        ...structuredClone(UI10_INSTANCE_RESPONSE),
        instance_id: id,
      } as InstanceTelemetry),
    );
  return { api, list, telemetry };
}
async function row(name: string) {
  return (await screen.findByRole("link", { name })).closest(
    "tr",
  ) as HTMLTableRowElement;
}
function cell(row: HTMLElement, column: string) {
  const result = row.querySelector(`[data-column="${column}"]`);
  if (!(result instanceof HTMLElement)) throw new Error(`missing ${column}`);
  return result;
}

describe("UI-27 instance list", () => {
  it("UI-27 renders eight columns, model/runtime, GPU, updated time and live metrics with one query per row; axe", async () => {
    const { api, telemetry } = setup([item("实例一")]);
    const { container } = renderApplication(api, "/models");
    const target = await row("实例一");
    expect(
      screen.getAllByRole("columnheader").map((header) => header.textContent),
    ).toEqual([
      "实例 · 模型",
      "状态",
      "GPU",
      "运行请求",
      "TTFT P95近 1 小时",
      "输出 TPS",
      "更新时间",
      "操作",
    ]);
    const ttftHeader = screen.getByRole("columnheader", {
      name: "TTFT P95（近 1 小时）",
    });
    expect(
      ttftHeader.querySelector(".pw-instance-column-window"),
    ).toHaveTextContent("近 1 小时");
    expect(cell(target, "status").querySelector(".pw-status-badge")).toBeNull();
    expect(
      cell(target, "status").querySelector(".pw-service-signal--positive"),
    ).toHaveAttribute("aria-hidden", "true");
    expect(cell(target, "status")).toHaveTextContent("运行中");
    expect(
      within(target).getByRole("link", { name: "实例一" }),
    ).toHaveAttribute("title", "实例一");
    await waitFor(() => expect(cell(target, "running")).toHaveTextContent("7"));
    expect(cell(target, "ttft")).toHaveTextContent("73 ms");
    expect(cell(target, "tps")).toHaveTextContent("86.4 tok/s");
    expect(cell(target, "instance")).toHaveTextContent(
      "本地测试模型vLLM · TP 1 · Context 4,096",
    );
    expect(cell(target, "gpus")).toHaveTextContent("1 × Test GPU · GPU 0");
    expect(cell(target, "updated").querySelector("time")).toHaveAttribute(
      "datetime",
      "2026-10-14T04:00:00Z",
    );
    expect(
      within(target)
        .getAllByRole("button")
        .map((button) => button.textContent),
    ).toEqual(["发布服务", "重启", "停止", "日志"]);
    expect(telemetry).toHaveBeenCalledTimes(1);
    expect((await axe.run(container)).violations).toEqual([]);
  });

  it("UI-27 shows status reasons, released/unknown GPU identity and exception-first recent sorting", async () => {
    const { api } = setup([
      item("旧运行", "Running", { updated_at: "2026-10-12T00:00:00Z" }),
      item("已停止", "Stopped", { gpu_uuids: ["missing"] }),
      item("失败", "Failed", {
        error_code: "runtime_out_of_memory",
        updated_at: "2026-10-13T00:00:00Z",
      }),
      item("异常", "Unhealthy", { error_code: "future" }),
      item("重启", "Restarting", { restart_count: 2 }),
      item("启动", "Starting"),
      item("等待", "WaitingForResource"),
    ]);
    renderApplication(api, "/models");
    await row("旧运行");
    const links = within(
      screen.getByRole("table", { name: "模型实例" }),
    ).getAllByRole("link");
    expect(links.map((link) => link.textContent)).toEqual([
      "异常",
      "失败",
      "已停止",
      "重启",
      "启动",
      "等待",
      "旧运行",
    ]);
    expect(cell(await row("异常"), "status")).toHaveTextContent(
      "健康检查未通过",
    );
    expect(cell(await row("失败"), "status")).toHaveTextContent("加载权重");
    expect(cell(await row("重启"), "status")).toHaveTextContent(
      "第 2 次自动重启（上限 3 次）",
    );
    expect(cell(await row("启动"), "status")).toHaveTextContent(
      "正在启动 Runtime",
    );
    expect(cell(await row("等待"), "gpus")).toHaveTextContent("等待 GPU 0");
    expect(cell(await row("已停止"), "gpus")).toHaveTextContent(
      "已释放（原 GPU ?）",
    );
    expect(cell(await row("失败"), "gpus")).toHaveTextContent(
      "已释放（原 GPU 0）",
    );
    for (const [name, reason] of [
      ["已停止", "实例未运行"],
      ["异常", "实例运行异常"],
    ] as const) {
      for (const column of ["running", "ttft", "tps"])
        expect(
          within(cell(await row(name), column)).getByTitle(reason),
        ).toHaveTextContent("—");
    }
  });

  it("UI-27 keeps null metrics unavailable and explains missing one-hour requests", async () => {
    const { api, telemetry } = setup([item("无数据")]);
    const data = structuredClone(UI10_INSTANCE_RESPONSE) as InstanceTelemetry;
    data.current.running = null;
    data.current.output_tps = null;
    data.window_1h.ttft_p95_ms = null;
    telemetry.mockResolvedValue(data);
    renderApplication(api, "/models");
    const target = await row("无数据");
    await within(cell(target, "ttft")).findByTitle("近 1 小时无请求");
    expect(
      within(cell(target, "running")).getByTitle("运行请求暂不可用"),
    ).toHaveTextContent("—");
    expect(
      within(cell(target, "tps")).getByTitle("输出 TPS暂不可用"),
    ).toHaveTextContent("—");
    expect(
      cell(target, "ttft").querySelector("[aria-describedby]"),
    ).toHaveAccessibleDescription("近 1 小时无请求");
  });

  it("UI-27 filters by instance/model, status, Runtime and GPU and clears an empty result", async () => {
    const { api } = setup([
      item("Alpha"),
      item("Beta", "Stopped", { runtime_type: "sglang", gpu_uuids: ["gpu-1"] }),
    ]);
    renderApplication(api, "/models");
    await row("Alpha");
    fireEvent.change(screen.getByRole("searchbox"), {
      target: { value: "本地测试模型" },
    });
    expect(await row("Alpha")).toBeVisible();
    expect(await row("Beta")).toBeVisible();
    fireEvent.change(screen.getByRole("searchbox"), {
      target: { value: "alp" },
    });
    expect(
      screen.queryByRole("link", { name: "Beta" }),
    ).not.toBeInTheDocument();
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "" } });
    for (const [label, value] of [
      ["状态", "Stopped"],
      ["Runtime", "sglang"],
      ["GPU", "gpu-1"],
    ]) {
      fireEvent.change(screen.getByRole("combobox", { name: label }), {
        target: { value },
      });
      expect(
        screen.queryByRole("link", { name: "Alpha" }),
      ).not.toBeInTheDocument();
      expect(await row("Beta")).toBeVisible();
      fireEvent.change(screen.getByRole("combobox", { name: label }), {
        target: { value: "" },
      });
    }
    fireEvent.change(screen.getByRole("searchbox"), {
      target: { value: "无此实例" },
    });
    expect(await screen.findByText("没有符合筛选条件的模型实例")).toBeVisible();
    await userEvent.click(screen.getByRole("button", { name: "清除筛选" }));
    expect(await row("Alpha")).toBeVisible();
    expect(screen.getByRole("searchbox")).toHaveValue("");
  });

  it.each(["Restarting", "Unhealthy"] as const)(
    "UI-27 %s stop abandons automatic restart",
    async (status) => {
      const { api } = setup([item("待停止", status)]);
      const stop = vi
        .spyOn(api, "stopModelInstance")
        .mockResolvedValue(item("待停止", "Stopping"));
      renderApplication(api, "/models");
      await userEvent.click(
        within(await row("待停止")).getByRole("button", { name: "停止" }),
      );
      const dialog = screen.getByRole("dialog");
      expect(within(dialog).getByText("停止后不再自动重启")).toBeVisible();
      await userEvent.click(
        within(dialog).getByRole("button", { name: "确认停止" }),
      );
      await waitFor(() => {
        expect(stop).toHaveBeenCalledWith("待停止");
      });
    },
  );

  it("UI-27 uses exactly the two responsive breakpoints without an expansion row", () => {
    const css = readFileSync(
      resolve(dirname(fileURLToPath(import.meta.url)), "../styles/app.css"),
      "utf8",
    );
    expect(css.match(/@media \(max-width: 1179px\)/g)).toHaveLength(1);
    expect(css.indexOf("@media (max-width: 1179px)")).toBeGreaterThan(
      css.indexOf("@media (max-width: 1279px)"),
    );
    const medium = css.slice(
      css.indexOf("@media (max-width: 1179px)"),
      css.indexOf("@media (max-width: 1023px)"),
    );
    expect(medium).toContain('[data-column="updated"]');
    const narrow = css.slice(
      css.indexOf("@media (max-width: 1023px)"),
      css.indexOf("@media (max-width: 767px)"),
    );
    for (const id of ["running", "ttft", "tps"])
      expect(narrow).toContain(`[data-column="${id}"]`);
    expect(narrow).toMatch(
      /\.pw-instance-metrics-summary\s*\{\s*display: block/,
    );
    expect(css.match(/@media \(max-width: 767px\)/g)).toHaveLength(1);
  });
});

describe("UI-27 start GPU conflict", () => {
  async function prepare(occupied = true) {
    const stopped = item("待启动", "Stopped");
    const { api, list } = setup([
      stopped,
      ...(occupied ? [item("占用实例")] : []),
    ]);
    const start = vi
      .spyOn(api, "startModelInstance")
      .mockResolvedValue({ ...stopped, status: "WaitingForResource" });
    const select = vi
      .spyOn(api, "selectModelInstanceGpus")
      .mockResolvedValue({ ...stopped, status: "Starting" });
    renderApplication(api, "/models");
    const target = await row("待启动");
    await userEvent.click(within(target).getByRole("button", { name: "启动" }));
    return { api, list, start, select, target, stopped };
  }
  async function choose() {
    await userEvent.click(screen.getByRole("button", { name: "重新选择 GPU" }));
    const dialog = screen.getByRole("dialog", { name: "重新选择 GPU" });
    expect(
      within(dialog).getByRole("checkbox", { name: /GPU 0/ }),
    ).toBeDisabled();
    expect(
      within(dialog).getByRole("checkbox", { name: /GPU 3/ }),
    ).toBeDisabled();
    expect(
      within(dialog).getByRole("button", { name: "确认重新选择" }),
    ).toBeDisabled();
    await userEvent.click(
      within(dialog).getByRole("checkbox", { name: /GPU 1/ }),
    );
    await userEvent.click(
      within(dialog).getByRole("checkbox", { name: /GPU 2/ }),
    );
    expect(
      within(dialog).getByRole("button", { name: "确认重新选择" }),
    ).toBeDisabled();
    await userEvent.click(
      within(dialog).getByRole("checkbox", { name: /GPU 2/ }),
    );
    await userEvent.click(
      within(dialog).getByRole("button", { name: "确认重新选择" }),
    );
  }
  it("UI-27 starts directly when original GPUs are free", async () => {
    const { start, select } = await prepare(false);
    expect(start).toHaveBeenCalledWith("待启动");
    expect(select).not.toHaveBeenCalled();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
  it("UI-27 lists occupying names and waits for original GPUs only after confirmation", async () => {
    const { start, select } = await prepare();
    const dialog = screen.getByRole("dialog", { name: "原 GPU 已被占用" });
    expect(dialog).toHaveTextContent("占用实例");
    expect(start).not.toHaveBeenCalled();
    expect((await axe.run(dialog)).violations).toEqual([]);
    await userEvent.click(
      within(dialog).getByRole("button", { name: "等待原 GPU" }),
    );
    expect(start).toHaveBeenCalledWith("待启动");
    expect(select).not.toHaveBeenCalled();
  });
  it("UI-27 selects the required free healthy cards before start then select-gpus", async () => {
    const { start, select } = await prepare();
    expect(start).not.toHaveBeenCalled();
    await choose();
    await waitFor(() => {
      expect(select).toHaveBeenCalledWith("待启动", ["gpu-1"]);
    });
    expect(start.mock.invocationCallOrder[0]).toBeLessThan(
      select.mock.invocationCallOrder[0] ?? 0,
    );
  });
  it("UI-27 start returning Starting skips select-gpus and explains the original GPUs", async () => {
    const { start, select, stopped } = await prepare();
    start.mockResolvedValue({ ...stopped, status: "Starting" });
    await choose();
    expect(
      await screen.findByText("原 GPU 已空出，实例已在原 GPU 上启动"),
    ).toBeVisible();
    expect(select).not.toHaveBeenCalled();
  });
  it("UI-27 select failure refreshes to waiting with an inline error and reselect action", async () => {
    const { list, select, stopped } = await prepare();
    list.mockResolvedValue({
      items: [{ ...stopped, status: "WaitingForResource" }, item("占用实例")],
    });
    select.mockRejectedValue(
      new ApiError({
        code: "gpu_unavailable",
        status: 422,
        responseMessage: "所选 GPU 已被占用，请重新选择。",
      }),
    );
    await choose();
    const target = await row("待启动");
    await waitFor(() =>
      expect(within(target).getByRole("alert")).toHaveTextContent(
        "所选 GPU 已被占用，请重新选择。",
      ),
    );
    expect(
      within(target).getByRole("button", { name: "重新选择 GPU" }),
    ).toBeVisible();
    expect(
      within(target).queryByRole("button", { name: "启动" }),
    ).not.toBeInTheDocument();
    expect(select).toHaveBeenCalledTimes(1);
  });
});
