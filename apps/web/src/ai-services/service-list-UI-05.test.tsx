import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import axe from "axe-core";
import { describe, expect, it, vi } from "vitest";

import type { AiService } from "../api/ai-service-contracts";
import type { ModelInstance } from "../api/model-instance-contracts";
import {
  ADMIN_ME,
  FakeApiClient,
  renderApplication,
} from "../app/test-support";
import { DataTable } from "../components/data-table";

// FX-UI-05: synthetic services; default name follows domain instance_names.py.
const instanceId = "0198f58a-2586-7e37-bbf5-000000000046";
const defaultName = `Qwen/Qwen2.5-0.5B-Instruct-${instanceId.replaceAll("-", "")}`;
const modelInstance: ModelInstance = {
  id: instanceId,
  name: defaultName,
  artifact_id: "0198f58a-2586-7e37-bbf5-000000000045",
  config_version: 1,
  context_length: 4096,
  error_code: null,
  gpu_memory_utilization: 0.9,
  restart_count: 0,
  gpu_uuids: ["GPU-FX-UI-05"],
  internal_endpoint: null,
  internal_model_name: "Qwen/Qwen2.5-0.5B-Instruct",
  runtime_type: "vllm",
  status: "Running",
  updated_at: "2026-10-09T00:00:00Z",
  tensor_parallel_size: 1,
};
const firstService: AiService = {
  id: "0198f58a-2586-7e37-bbf5-000000000051",
  name: "review-model",
  public_model_name: "review-model",
  description: "",
  enabled: true,
  primary_instance_id: instanceId,
  primary_instance_status: "Running",
  route_version: 1,
  runtime_status: "Available",
};
const secondService: AiService = {
  ...firstService,
  id: "0198f58a-2586-7e37-bbf5-000000000052",
  name: "文档助手",
  public_model_name: "docs-model",
  description: "用于长文档检索与摘要",
};

function viewport(initialWidth: number) {
  let width = initialWidth;
  const callbacks = new Set<() => void>();
  vi.spyOn(window, "matchMedia").mockImplementation((query) => ({
    get matches() {
      return width <= Number(/max-width: (\d+)px/.exec(query)?.[1] ?? -1);
    },
    media: query,
    onchange: null,
    addEventListener: (_event: string, callback: unknown) =>
      callbacks.add(callback as () => void),
    removeEventListener: (_event: string, callback: unknown) =>
      callbacks.delete(callback as () => void),
    addListener: vi.fn(),
    removeListener: vi.fn(),
    dispatchEvent: vi.fn(),
  }));
  return (nextWidth: number) => {
    act(() => {
      width = nextWidth;
      callbacks.forEach((callback) => {
        callback();
      });
    });
  };
}

async function showServices(
  services = [firstService, secondService],
  instance = modelInstance,
) {
  const api = new FakeApiClient(ADMIN_ME);
  api.aiServiceResults.push({ items: services });
  api.modelInstanceResults.push({ items: [instance] });
  renderApplication(api, "/services");
  return (await screen.findByText(firstService.name)).closest(
    "tr",
  ) as HTMLElement;
}

function columns() {
  return within(screen.getByRole("table", { name: "AI 服务" }))
    .getAllByRole("columnheader")
    .filter((column) => column.getAttribute("scope") === "col")
    .map((column) => column.getAttribute("data-column"));
}

describe("UI_08 TC-UI-08 responsive service comparison (replaces UI_05)", () => {
  it("shows nine columns at 1600, 1366 and 1024 without disclosure", async () => {
    const resize = viewport(1600);
    await showServices();
    const full = [
      "name",
      "instance",
      "authorized-users",
      "today-requests",
      "ttft",
      "error-rate",
      "service-tps",
      "concurrency",
      "actions",
    ];
    expect(columns()).toEqual(full);
    for (const width of [1366, 1024]) {
      resize(width);
      expect(columns()).toEqual(full);
    }
    expect(
      screen.queryByRole("button", { name: "全部展开" }),
    ).not.toBeInTheDocument();
    expect(document.querySelector(".pw-table__expanded-row")).toBeNull();
    expect(document.querySelector('[data-column="expand"]')).toBeNull();
  });

  it("groups windows over the exact metric columns and passes axe", async () => {
    viewport(1366);
    const row = await showServices();
    const table = screen.getByRole("table", { name: "AI 服务" });
    const groups = table.querySelectorAll('th[scope="colgroup"]');
    expect(
      [...groups].map((th) => [th.textContent, th.getAttribute("colspan")]),
    ).toEqual([
      ["今日", "1"],
      ["近 1 小时", "2"],
      ["当前", "2"],
    ]);
    expect(table.querySelectorAll('th[scope="col"]')).toHaveLength(9);
    expect(within(row).getByText("共享后端")).toBeVisible();
    expect(
      (await axe.run(document.body)).violations.filter(
        ({ impact }) => impact === "critical" || impact === "serious",
      ),
    ).toEqual([]);
  });

  it("shows five columns below 1024 and updates groups on resize", async () => {
    const resize = viewport(1023);
    await showServices([firstService]);
    const narrow = ["name", "instance", "ttft", "error-rate", "actions"];
    expect(columns()).toEqual(narrow);
    resize(767);
    expect(columns()).toEqual(narrow);
    const table = screen.getByRole("table", { name: "AI 服务" });
    expect(
      [...table.querySelectorAll('th[scope="colgroup"]')].map(
        (th) => th.textContent,
      ),
    ).toEqual(["近 1 小时"]);
    expect(table.querySelectorAll("col")).toHaveLength(5);
    expect(document.querySelector(".pw-table__expanded-row")).toBeNull();
    resize(1024);
    expect(columns()).toHaveLength(9);
  });

  it("deduplicates secondary identity, preserves full accessible values and copies the full default instance name", async () => {
    viewport(1600);
    const user = userEvent.setup();
    const copy = vi.spyOn(navigator.clipboard, "writeText");
    const row = await showServices();
    const identity = row.querySelector('[data-column="name"]') as HTMLElement;
    expect(within(identity).getAllByText(firstService.name)).toHaveLength(1);
    expect(identity.textContent).toBe(firstService.name);
    expect(screen.queryByText("暂无描述")).not.toBeInTheDocument();
    expect(screen.getByText(secondService.description)).toHaveAttribute(
      "title",
      secondService.description,
    );
    expect(screen.getByText(secondService.public_model_name)).toHaveAttribute(
      "aria-label",
      secondService.public_model_name,
    );
    const name = within(row).getByText("Qwen2.5-0.5B-Instruct");
    expect(name).toHaveAttribute("title", defaultName);
    expect(name).toHaveAttribute("aria-label", defaultName);
    expect(within(row).queryByText("…0046")).not.toBeInTheDocument();
    expect(row.querySelector('[data-column="instance"] .pw-status')).toBeNull();
    await user.click(
      within(row).getByRole("button", { name: `复制实例名 ${defaultName}` }),
    );
    expect(copy).toHaveBeenCalledWith(defaultName);
  });

  it("keeps custom instance names verbatim and offers their full copy", async () => {
    viewport(1600);
    const user = userEvent.setup();
    const copy = vi.spyOn(navigator.clipboard, "writeText");
    const name = "研发/自定义实例";
    const row = await showServices([firstService], { ...modelInstance, name });
    expect(within(row).getByText(name)).toHaveAttribute("title", name);
    expect(within(row).queryByText("…0046")).not.toBeInTheDocument();
    await user.click(
      within(row).getByRole("button", { name: `复制实例名 ${name}` }),
    );
    expect(copy).toHaveBeenCalledWith(name);
  });

  it("preserves exact unexpanded DataTable markup and handles empty/loading disclosure", () => {
    const props = {
      caption: "FX-UI-05",
      columns: [{ id: "name", header: "名称", render: (row: string) => row }],
      getRowKey: (row: string) => row,
      rows: ["甲"],
    };
    const view = render(<DataTable {...props} />);
    expect(view.container.innerHTML).toBe(
      '<div aria-busy="false" aria-label="FX-UI-05" class="pw-table-wrap"><table class="pw-table"><caption class="pw-visually-hidden">FX-UI-05</caption><thead><tr><th data-column="name">名称</th></tr></thead><tbody><tr><td class="" data-column="name">甲</td></tr></tbody></table></div>',
    );
    const expansion = {
      label: (row: string) => row,
      render: (row: string) => row,
    };
    view.rerender(<DataTable {...props} expansion={expansion} rows={[]} />);
    expect(screen.getByRole("button", { name: "全部展开" })).toBeDisabled();
    expect(screen.getByRole("cell", { name: "暂无数据" })).toHaveAttribute(
      "colspan",
      "2",
    );
    view.rerender(<DataTable {...props} expansion={expansion} loading />);
    expect(screen.getByRole("button", { name: "全部展开" })).toBeDisabled();
    expect(view.container.querySelectorAll("tbody tr")).toHaveLength(6);
  });
});

describe("UI_08 PDD 9.5 column width tokens", () => {
  const css = readFileSync(
    resolve(dirname(fileURLToPath(import.meta.url)), "../styles/app.css"),
    "utf8",
  );
  const tokens = readFileSync(
    resolve(dirname(fileURLToPath(import.meta.url)), "../styles/tokens.css"),
    "utf8",
  );
  it("uses nine exact widths and lets the service column absorb spare space", () => {
    const widths = {
      name: 200,
      instance: 220,
      "authorized-users": 76,
      "today-requests": 88,
      ttft: 100,
      "error-rate": 84,
      "service-tps": 84,
      concurrency: 104,
      actions: 150,
    };
    for (const [id, width] of Object.entries(widths)) {
      expect(tokens).toContain(`--pw-svc-col-${id}: ${String(width)}px;`);
      if (id !== "name")
        expect(css.replace(/\s+/g, " ")).toContain(
          `col[data-column="${id}"] { width: var(--pw-svc-col-${id}); }`,
        );
    }
    expect(tokens.match(/--pw-svc-col-[\w-]+:/g)).toHaveLength(9);
    expect(css).not.toContain("1439px");
    expect(css).not.toContain("pw-service-expanded-metrics");
    expect(css).not.toContain('col[data-column="name"]');
    expect(css).toMatch(
      /\.pw-services-page \.pw-table__numeric\s*\{[^}]*text-align: start !important;[^}]*font-variant-numeric: tabular-nums;/,
    );
  });
});

// FX-UI-08: all four contract states plus duplicate backend, no live platform.
describe("UI_08 TC-UI-08 service signals and filters", () => {
  const variants: AiService[] = [
    {
      ...firstService,
      id: "00000000-0000-7000-8000-000000000081",
      name: "D-停用",
      public_model_name: "disabled",
      runtime_status: "Disabled",
      enabled: false,
      primary_instance_status: "Stopped",
    },
    {
      ...firstService,
      id: "00000000-0000-7000-8000-000000000082",
      name: "C-可用",
      public_model_name: "healthy",
    },
    {
      ...firstService,
      id: "00000000-0000-7000-8000-000000000083",
      name: "B-启动",
      public_model_name: "starting",
      runtime_status: "Degraded",
      primary_instance_status: "Starting",
    },
    {
      ...firstService,
      id: "00000000-0000-7000-8000-000000000084",
      name: "A-停止",
      public_model_name: "stopped",
      runtime_status: "Unavailable",
      primary_instance_status: "Stopped",
    },
    {
      ...firstService,
      id: "00000000-0000-7000-8000-000000000085",
      name: "A-失败",
      public_model_name: "failed",
      runtime_status: "Unavailable",
      primary_instance_status: "Failed",
    },
  ];
  async function setup() {
    viewport(1366);
    const api = new FakeApiClient(ADMIN_ME);
    api.aiServiceResults.push({ items: variants });
    api.modelInstanceResults.push({ items: [modelInstance] });
    renderApplication(api, "/services");
    await screen.findByRole("link", { name: "C-可用" });
    return api;
  }
  function serviceRow(name: string) {
    return screen.getByRole("link", { name }).closest("tr") as HTMLElement;
  }
  function names() {
    return [
      ...screen
        .getByRole("table", { name: "AI 服务" })
        .querySelectorAll('tbody [data-column="name"] a'),
    ].map((link) => link.textContent);
  }
  it("sorts four statuses then names, exposes one signal per row and no instance badge", async () => {
    await setup();
    expect(names()).toEqual(["A-失败", "A-停止", "B-启动", "C-可用", "D-停用"]);
    for (const [name, label, tone] of [
      ["A-停止", "不可用", "negative"],
      ["B-启动", "启动中", "notice"],
      ["C-可用", "可用", "positive"],
      ["D-停用", "已停用", "idle"],
    ] as const) {
      const row = serviceRow(name);
      expect(within(row).getByRole("img", { name: label })).toHaveClass(
        `pw-service-signal--${tone}`,
      );
      expect(row.querySelectorAll(".pw-service-signal")).toHaveLength(1);
      expect(row.querySelectorAll(".pw-status")).toHaveLength(0);
    }
    expect(serviceRow("A-停止")).toHaveTextContent("不可用 · 后端实例已停止");
    expect(serviceRow("A-失败")).toHaveTextContent("不可用 · 后端实例运行失败");
    expect(serviceRow("B-启动")).toHaveTextContent("启动中 · 后端实例启动中");
    expect(
      within(serviceRow("A-停止")).getByRole("link", { name: "前往处理" }),
    ).toHaveAttribute("href", `/models/instances/${instanceId}`);
    expect(
      within(serviceRow("C-可用")).queryByRole("link", { name: "前往处理" }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "C-可用" })).toHaveAttribute(
      "href",
      "/services/00000000-0000-7000-8000-000000000082",
    );
  });
  it("distinguishes inactive dash from running missing metrics and keeps warning in the filter row", async () => {
    await setup();
    for (const id of ["ttft", "error-rate", "service-tps", "concurrency"]) {
      const inactive = serviceRow("A-停止").querySelector(
        `[data-column="${id}"]`,
      ) as HTMLElement;
      expect(inactive.textContent).toBe("—");
      expect(
        inactive.querySelector(".pw-service-inactive-metric"),
      ).not.toBeNull();
      expect(inactive.querySelector(".pw-missing-value")).toBeNull();
      expect(inactive.querySelector("[aria-describedby]")).toBeNull();
      const active = serviceRow("C-可用").querySelector(
        `[data-column="${id}"]`,
      ) as HTMLElement;
      expect(
        within(active).getAllByLabelText("暂无数据")[0],
      ).toHaveAccessibleDescription();
    }
    expect(within(serviceRow("C-可用")).getByText("共享后端")).toBeVisible();
    const warning = await screen.findByText("监控数据延迟或不可用");
    expect(warning.closest(".pw-service-filters")).not.toBeNull();
    expect(warning.closest(".pw-alert")).toBeNull();
  });
  it("combines counted status filters with name or model search and an empty result", async () => {
    await setup();
    const user = userEvent.setup();
    const all = screen.getByRole("button", { name: "全部 5" });
    expect(all).toHaveAttribute("aria-pressed", "true");
    await user.click(screen.getByRole("button", { name: "需处理 3" }));
    expect(names()).toEqual(["A-失败", "A-停止", "B-启动"]);
    const search = screen.getByRole("searchbox", {
      name: "搜索服务或 API 模型名",
    });
    await user.type(search, "STARTING");
    expect(names()).toEqual(["B-启动"]);
    await user.clear(search);
    await user.type(search, "A-停止");
    expect(names()).toEqual(["A-停止"]);
    await user.click(screen.getByRole("button", { name: "可用 1" }));
    expect(screen.getByText("没有匹配的 AI 服务")).toBeVisible();
    await user.clear(search);
    expect(names()).toEqual(["C-可用"]);
    await user.click(screen.getByRole("button", { name: "已停用 1" }));
    expect(names()).toEqual(["D-停用"]);
    await user.click(all);
    expect(names()).toHaveLength(5);
  });
  it.each([true, false])(
    "keeps the enabled=%s toggle with applicable delete and sends the existing command",
    async (enabled) => {
      const api = await setup();
      const service = variants[enabled ? 1 : 0];
      if (!service) throw new Error("FX-UI-08 missing toggle fixture");
      api.aiServiceUpdateResults.push({ ...service, enabled: !enabled });
      const row = serviceRow(service.name);
      const actions = row.querySelector(
        '[data-column="actions"]',
      ) as HTMLElement;
      expect(within(actions).getAllByRole("button")).toHaveLength(
        !service.enabled ||
          ["Stopped", "Failed"].includes(service.primary_instance_status ?? "")
          ? 2
          : 1,
      );
      await userEvent.setup().click(
        within(actions).getByRole("button", {
          name: enabled ? "停用" : "启用",
        }),
      );
      expect(api.aiServiceUpdateCalls).toEqual([
        { id: service.id, body: { enabled: !enabled } },
      ]);
    },
  );
});
