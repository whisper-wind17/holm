import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import axe from "axe-core";
import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiClient, ApiError } from "../api/client";
import type { AiService } from "../api/ai-service-contracts";
import {
  ModelInstanceContractError,
  parseModelInstanceEventPage,
  type ModelInstance,
  type ModelInstanceEventPage,
  type ModelInstanceStatus,
} from "../api/model-instance-contracts";
import {
  ADMIN_ME,
  DEFAULT_SYSTEM_SERVER,
  FakeApiClient,
  renderApplication,
} from "../app/test-support";
import { INSTANCE_ACTIONS, type InstanceAction } from "./presentation";
import { INSTANCE_POLL_INTERVAL_MS } from "./queries";

const TEST_DIRECTORY = dirname(fileURLToPath(import.meta.url));
const APP_CSS_PATH = resolve(TEST_DIRECTORY, "../styles/app.css");
const INSTANCE_ID = uuid(1);
const ARTIFACT_ID = uuid(3);
const BODY_SENTINEL = "M3-11-PROMPT-BODY-SENTINEL";

afterEach(() => {
  vi.unstubAllGlobals();
  FakeEventSource.instances.length = 0;
  localStorage.clear();
  sessionStorage.clear();
});

describe("M3_11 PDD 9.3 instance action matrix", () => {
  it.each([
    ["Running", ["publish", "restart", "stop", "logs"]],
    ["Stopped", ["start", "delete", "logs"]],
    ["Failed", ["retry", "delete", "logs"]],
    ["WaitingForResource", ["reselect-gpus", "cancel"]],
    ["Starting", ["logs"]],
    ["Stopping", ["logs"]],
    ["Restarting", ["stop", "logs"]],
    ["Unhealthy", ["stop", "logs"]],
  ] satisfies readonly (readonly [
    ModelInstanceStatus,
    readonly InstanceAction[],
  ])[])("M3_11 exposes exactly the %s operations", (status, expected) => {
    expect(INSTANCE_ACTIONS[status]).toEqual(expected);
  });

  it("M3_11 renders all action labels and keeps transitional states read-only", async () => {
    const apiClient = listClient(
      (Object.keys(INSTANCE_ACTIONS) as ModelInstanceStatus[]).map(
        (status, index) =>
          instance({
            id: uuid(20 + index),
            name: status,
            internal_model_name: status,
            status,
          }),
      ),
    );
    renderApplication(apiClient, "/models");

    const table = await screen.findByRole("table", { name: "模型实例" });
    await within(table).findByText("Starting");
    const startingRow = within(table).getByText("Starting").closest("tr");
    const waitingRow = within(table)
      .getByText("WaitingForResource")
      .closest("tr");
    expect(buttonLabels(startingRow)).toEqual(["日志"]);
    expect(buttonLabels(waitingRow)).toEqual(["重新选择 GPU", "取消部署"]);
    expect(within(table).getByRole("button", { name: "重启" })).toBeVisible();
    expect(within(table).getByRole("button", { name: "重试" })).toBeVisible();
    expect(
      within(table).getByRole("button", { name: "发布服务" }),
    ).toBeVisible();
  });
});

describe("M3_11 dangerous actions and service bindings", () => {
  it("M3_11 explains all four consequences before allowing the action", async () => {
    const apiClient = listClient([
      instance({ status: "Running" }),
      instance({ id: uuid(4), status: "WaitingForResource" }),
      instance({ id: uuid(5), status: "Stopped" }),
    ]);
    apiClient.aiServiceResults.push({ items: [service()] });
    renderApplication(apiClient, "/models");
    const user = userEvent.setup();
    const table = await screen.findByRole("table", { name: "模型实例" });
    await within(table).findByText("运行中");

    await user.click(within(table).getByRole("button", { name: "停止" }));
    expect(
      screen.getByText(/停止会释放 GPU；已绑定的 AI 服务将停止提供模型响应/),
    ).toBeVisible();
    expect(screen.getByText("研发助手 · qwen-public")).toBeVisible();
    await expectNoBlockingAxeViolations(screen.getByRole("dialog"));
    await user.click(screen.getByRole("button", { name: "关闭" }));

    await user.click(within(table).getByRole("button", { name: "重启" }));
    expect(
      screen.getByText(/重启会保留原 GPU；已绑定的 AI 服务会短暂不可用/),
    ).toBeVisible();
    await user.click(screen.getByRole("button", { name: "关闭" }));

    await user.click(within(table).getByRole("button", { name: "取消部署" }));
    expect(
      screen.getByText(/只删除模型实例配置，不删除本地模型与下载任务/),
    ).toBeVisible();
    await user.click(screen.getByRole("button", { name: "关闭" }));

    await user.click(within(table).getByRole("button", { name: "删除" }));
    expect(
      screen.getByText(/删除后无法恢复，但不会删除本地模型文件/),
    ).toBeVisible();
  });

  it("UI_11 M3_11 deletes a bound instance directly after dialog confirmation", async () => {
    const apiClient = listClient([instance({ status: "Stopped" })]);
    apiClient.aiServiceResults.push({
      items: [
        service({
          primary_instance_status: "Stopped",
          runtime_status: "Unavailable",
        }),
      ],
    });
    renderApplication(apiClient, "/models");

    const table = await screen.findByRole("table", { name: "模型实例" });
    const deleteButton = await within(table).findByRole("button", {
      name: "删除",
    });
    await waitFor(() => expect(deleteButton).toBeEnabled());
    const remove = vi.spyOn(apiClient, "deleteModelInstance");
    const user = userEvent.setup();
    await user.click(deleteButton);
    const dialog = screen.getByRole("dialog");
    expect(
      within(dialog).getByRole("link", { name: "研发助手" }),
    ).toHaveAttribute("href", `/services/${service().id}`);
    expect(dialog).toHaveTextContent("qwen-public");
    expect(dialog).toHaveTextContent("model_not_found");
    expect(dialog).toHaveTextContent("历史用量与调用记录保留");
    expect(dialog).toHaveTextContent("本地模型文件不受影响");
    const confirm = within(dialog).getByRole("button", {
      name: "删除实例和服务",
    });
    expect(within(dialog).queryByRole("textbox")).not.toBeInTheDocument();
    expect(confirm).toBeEnabled();
    await expectNoBlockingAxeViolations(dialog);
    const serviceCalls = apiClient.aiServiceCalls;
    await user.click(confirm);
    await waitFor(() => {
      expect(remove).toHaveBeenCalledWith(INSTANCE_ID, true);
    });
    await waitFor(() => {
      expect(apiClient.aiServiceCalls).toBeGreaterThan(serviceCalls);
    });
  });
});

describe("M3_11 optimistic conflict recovery", () => {
  it("M3_11 keeps the selected GPUs and modal open after a 409", async () => {
    const apiClient = new FakeApiClient(ADMIN_ME);
    apiClient.modelInstanceDetailResults.push(
      instance({ status: "WaitingForResource" }),
    );
    const server = structuredClone(DEFAULT_SYSTEM_SERVER);
    if (server.agent.status !== "available") throw new Error("bad fixture");
    server.agent.gpus.devices = [gpu(0), gpu(1)];
    server.agent.gpus.discovery_state = "ready";
    server.agent.gpus.failure_reason = "none";
    apiClient.systemServerResults.push(server, structuredClone(server));
    const select = vi
      .spyOn(apiClient, "selectModelInstanceGpus")
      .mockRejectedValue(
        new ApiError({
          code: "invalid_instance_state",
          responseMessage: "internal conflict detail",
          status: 409,
        }),
      );
    renderApplication(apiClient, `/models/instances/${INSTANCE_ID}`);
    const user = userEvent.setup();

    await user.click(
      await screen.findByRole("button", { name: "重新选择 GPU" }),
    );
    const secondGpu = screen.getByRole("checkbox", { name: /GPU 1/ });
    await user.click(secondGpu);
    await user.click(screen.getByRole("button", { name: "确认重新选择" }));

    expect(select).toHaveBeenCalledWith(INSTANCE_ID, [
      "GPU-fixture-0",
      "GPU-fixture-1",
    ]);
    expect(await screen.findByRole("dialog")).toBeVisible();
    expect(secondGpu).toBeChecked();
    expect(screen.getByText(/当前 GPU 选择已保留，可直接重试/)).toBeVisible();
    expect(document.body).not.toHaveTextContent("internal conflict detail");
  });
});

describe("M3_11 component smoke sequence", () => {
  it("M3_11 runs restart, stop, wait, reselect, and cancel through the UI", async () => {
    const running = instance({
      name: "running-smoke",
      internal_model_name: "running-smoke",
      status: "Running",
    });
    const waiting = instance({
      id: uuid(6),
      name: "waiting-smoke",
      internal_model_name: "waiting-smoke",
      status: "WaitingForResource",
    });
    const apiClient = listClient([running, waiting]);
    for (let index = 0; index < 6; index += 1) {
      apiClient.modelInstanceResults.push({ items: [running, waiting] });
    }
    const server = structuredClone(DEFAULT_SYSTEM_SERVER);
    if (server.agent.status !== "available") throw new Error("bad fixture");
    server.agent.gpus.devices = [gpu(0), gpu(1)];
    server.agent.gpus.discovery_state = "ready";
    server.agent.gpus.failure_reason = "none";
    apiClient.systemServerResults.push(server, structuredClone(server));
    const restart = vi
      .spyOn(apiClient, "restartModelInstance")
      .mockResolvedValue(instance({ status: "Restarting" }));
    const stop = vi
      .spyOn(apiClient, "stopModelInstance")
      .mockResolvedValue(instance({ status: "Stopping" }));
    const wait = vi
      .spyOn(apiClient, "startModelInstance")
      .mockResolvedValue(instance({ id: waiting.id, status: "Starting" }));
    const reselect = vi
      .spyOn(apiClient, "selectModelInstanceGpus")
      .mockResolvedValue(instance({ id: waiting.id, status: "Starting" }));
    const cancel = vi
      .spyOn(apiClient, "cancelModelInstanceDeployment")
      .mockResolvedValue();
    renderApplication(apiClient, "/models");
    const user = userEvent.setup();
    const table = await screen.findByRole("table", { name: "模型实例" });
    await within(table).findByText("running-smoke");

    await user.click(
      within(instanceRow(table, "running-smoke")).getByRole("button", {
        name: "重启",
      }),
    );
    await user.click(screen.getByRole("button", { name: "确认重启" }));
    await waitFor(() => {
      expect(restart).toHaveBeenCalledWith(running.id);
    });

    await user.click(
      within(instanceRow(table, "running-smoke")).getByRole("button", {
        name: "停止",
      }),
    );
    await user.click(screen.getByRole("button", { name: "确认停止" }));
    await waitFor(() => {
      expect(stop).toHaveBeenCalledWith(running.id);
    });

    expect(
      within(instanceRow(table, "waiting-smoke")).queryByRole("button", {
        name: "等待原 GPU",
      }),
    ).not.toBeInTheDocument();
    expect(wait).not.toHaveBeenCalled();

    await user.click(
      within(instanceRow(table, "waiting-smoke")).getByRole("button", {
        name: "重新选择 GPU",
      }),
    );
    await user.click(screen.getByRole("checkbox", { name: /GPU 1/ }));
    await user.click(screen.getByRole("button", { name: "确认重新选择" }));
    await waitFor(() => {
      expect(reselect).toHaveBeenCalledWith(waiting.id, [
        "GPU-fixture-0",
        "GPU-fixture-1",
      ]);
    });

    await user.click(
      within(instanceRow(table, "waiting-smoke")).getByRole("button", {
        name: "取消部署",
      }),
    );
    await user.click(screen.getByRole("button", { name: "确认取消部署" }));
    await waitFor(() => {
      expect(cancel).toHaveBeenCalledWith(waiting.id);
    });
  });
});

describe("M3_11 instance event tab allowlist and pagination", () => {
  it("M3_11 renders newest-first cursor pages without body or internal fields", async () => {
    const apiClient = new FakeApiClient(ADMIN_ME);
    apiClient.modelInstanceDetailResults.push(instance({ status: "Running" }));
    const getEvents = vi
      .spyOn(apiClient, "getModelInstanceEvents")
      .mockResolvedValueOnce(
        eventPage(
          [
            event(10, "2026-09-21T03:00:00Z", "RestartRequested"),
            event(11, "2026-09-21T04:00:00Z", "StatusChanged"),
          ],
          "older-events",
        ),
      )
      .mockResolvedValueOnce(
        eventPage([event(12, "2026-09-21T01:00:00Z", "WaitingForGpu")], null),
      );
    renderApplication(apiClient, `/models/instances/${INSTANCE_ID}`);
    const user = userEvent.setup();

    await user.click(await screen.findByRole("tab", { name: "事件" }));
    const table = await screen.findByRole("table", { name: "模型实例事件" });
    expect(eventLabels(table)).toEqual(["状态：重启中 → 运行中", "已请求重启"]);
    await user.click(screen.getByRole("button", { name: "加载更多" }));
    await screen.findByText("继续等待原 GPU");
    expect(eventLabels(table)).toEqual([
      "状态：重启中 → 运行中",
      "已请求重启",
      "继续等待原 GPU",
    ]);
    expect(getEvents).toHaveBeenCalledWith(INSTANCE_ID, undefined, 20);
    expect(getEvents).toHaveBeenCalledWith(INSTANCE_ID, "older-events", 20);
    expect(findSentinelResidue(BODY_SENTINEL)).toEqual([]);

    const planted = document.createElement("span");
    planted.textContent = BODY_SENTINEL;
    document.body.append(planted);
    expect(findSentinelResidue(BODY_SENTINEL)).toContain("DOM");
    planted.remove();
  });

  it("M3_11 rejects non-empty event details and unknown response fields", () => {
    const safe = rawEventPage();
    expect(parseModelInstanceEventPage(safe).items).toHaveLength(1);
    expect(() =>
      parseModelInstanceEventPage({
        ...safe,
        items: [{ ...safe.items[0], detail: { prompt: BODY_SENTINEL } }],
      }),
    ).toThrow(ModelInstanceContractError);
    expect(() =>
      parseModelInstanceEventPage({
        ...safe,
        items: [{ ...safe.items[0], internal_endpoint: BODY_SENTINEL }],
      }),
    ).toThrow(ModelInstanceContractError);
  });

  it.each([
    ["empty", undefined, "暂无事件。"],
    [
      "permission",
      new ApiError({ code: "forbidden", status: 403 }),
      "当前账号不能查看此模型实例的事件。",
    ],
    ["error", new Error("offline"), "事件暂时无法显示，请稍后重试。"],
  ] as const)("M3_11 renders the event %s state", async (_, error, message) => {
    const apiClient = new FakeApiClient(ADMIN_ME);
    apiClient.modelInstanceDetailResults.push(instance({ status: "Stopped" }));
    const request = vi.spyOn(apiClient, "getModelInstanceEvents");
    if (error === undefined) request.mockResolvedValue(eventPage([], null));
    else request.mockRejectedValue(error);
    renderApplication(apiClient, `/models/instances/${INSTANCE_ID}`);

    await userEvent.click(await screen.findByRole("tab", { name: "事件" }));
    expect(await screen.findByText(message)).toBeVisible();
  });

  it("M3_11 renders the event loading state without placeholder values", async () => {
    const apiClient = new FakeApiClient(ADMIN_ME);
    apiClient.modelInstanceDetailResults.push(instance({ status: "Running" }));
    vi.spyOn(apiClient, "getModelInstanceEvents").mockReturnValue(
      new Promise<ModelInstanceEventPage>(() => undefined),
    );
    renderApplication(apiClient, `/models/instances/${INSTANCE_ID}#events`);

    expect(
      await screen.findByLabelText("模型实例事件正在加载"),
    ).toHaveAttribute("aria-busy", "true");
    expect(screen.queryByText("0")).not.toBeInTheDocument();
  });
});

describe("M3_11 log display boundary", () => {
  it("M3_11 hides body, internal address, container, and GPU path markers", async () => {
    const apiClient = new FakeApiClient(ADMIN_ME);
    apiClient.modelInstanceDetailResults.push(instance({ status: "Running" }));
    apiClient.modelInstanceLogResults.push({
      entries: [
        logEntry(0, `prompt=${BODY_SENTINEL}`),
        logEntry(1, "http://127.0.0.1:8123/internal"),
        logEntry(2, "container_id=secret-container"),
        logEntry(3, "/workspace/models/private/model.safetensors"),
        logEntry(4, "ready"),
      ],
      next_cursor: null,
    });
    renderApplication(apiClient, `/models/instances/${INSTANCE_ID}#logs`);

    const log = await screen.findByLabelText("模型实例日志");
    expect(log).toHaveTextContent("ready");
    expect(log).toHaveTextContent("[敏感内容已隐藏]");
    expect(log).not.toHaveTextContent(BODY_SENTINEL);
    expect(log).not.toHaveTextContent("127.0.0.1");
    expect(log).not.toHaveTextContent("secret-container");
    expect(log).not.toHaveTextContent("model.safetensors");
  });
});

describe("M3_11 SSE polling fallback", () => {
  it("M3_11 visibly falls back to 5-second polling and returns to SSE", async () => {
    vi.stubGlobal("EventSource", FakeEventSource);
    const apiClient = listClient([instance({ status: "Running" })]);
    apiClient.modelInstanceResults.push({
      items: [instance({ status: "Running" })],
    });
    renderApplication(apiClient, "/models");
    await screen.findByRole("table", { name: "模型实例" });
    const source = FakeEventSource.instances[0];
    expect(source).toBeDefined();
    expect(INSTANCE_POLL_INTERVAL_MS).toBe(5_000);

    act(() => source?.onerror?.(new Event("error")));
    expect(
      await screen.findByText(/已降级为每 5 秒轮询，数据可能延迟/),
    ).toBeVisible();
    expect(source?.closed).toBe(false);

    act(() => source?.emit("instance.route_changed"));
    await waitFor(() => {
      expect(apiClient.modelInstanceCalls).toBeGreaterThan(1);
    });
    act(() => source?.onopen?.(new Event("open")));
    await waitFor(() =>
      expect(screen.queryByText(/已降级为每 5 秒轮询/)).not.toBeInTheDocument(),
    );
    expect(FakeEventSource.instances).toHaveLength(1);

    act(() => source?.emit("resync_required"));
    expect(source?.closed).toBe(true);
    expect(FakeEventSource.instances).toHaveLength(2);
  });
});

describe("M3_11 client calls and responsive accessibility", () => {
  it("M3_11 calls the existing lifecycle and event endpoint shapes", async () => {
    vi.spyOn(document, "cookie", "get").mockReturnValue(
      "__Host-pws_csrf=m311-csrf",
    );
    const fetchMock = vi.spyOn(globalThis, "fetch");
    fetchMock
      .mockResolvedValueOnce(jsonResponse(instance({ status: "Restarting" })))
      .mockResolvedValueOnce(jsonResponse(instance({ status: "Starting" })))
      .mockResolvedValueOnce(jsonResponse(instance({ status: "Starting" })))
      .mockResolvedValueOnce(new Response(null, { status: 204 }))
      .mockResolvedValueOnce(jsonResponse(rawEventPage()));
    const client = new ApiClient();

    await client.restartModelInstance(INSTANCE_ID);
    await client.retryModelInstance(INSTANCE_ID);
    await client.selectModelInstanceGpus(INSTANCE_ID, ["GPU-2"]);
    await client.cancelModelInstanceDeployment(INSTANCE_ID);
    await client.getModelInstanceEvents(INSTANCE_ID, "opaque+/cursor", 20);

    expect(fetchMock.mock.calls.map(([path]) => path)).toEqual([
      `/api/v1/instances/${INSTANCE_ID}:restart`,
      `/api/v1/instances/${INSTANCE_ID}:retry`,
      `/api/v1/instances/${INSTANCE_ID}:select-gpus`,
      `/api/v1/instances/${INSTANCE_ID}:cancel`,
      `/api/v1/instances/${INSTANCE_ID}/events?page_size=20&cursor=opaque%2B%2Fcursor`,
    ]);
    const selectCall = fetchMock.mock.calls[2];
    if (selectCall === undefined || typeof selectCall[1]?.body !== "string") {
      throw new Error("select-gpus request body fixture is missing");
    }
    expect(JSON.parse(selectCall[1].body)).toEqual({
      gpu_uuids: ["GPU-2"],
    });
  });

  it("M3_11 keeps actions wrapping and detail headers stacked at narrow width", () => {
    const css = readFileSync(APP_CSS_PATH, "utf8");
    expect(css).toMatch(/\.pw-instance-actions\s*\{[^}]*flex-wrap:\s*wrap/s);
    const mobile = css.slice(css.indexOf("@media (max-width: 767px)"));
    expect(mobile).toContain(".pw-instance-detail__head");
    expect(mobile).toContain("flex-direction: column");
  });
});

describe("UI_11 deletion confirmation and request contract", () => {
  it("UI_11 keeps an unbound deletion parameter-free", async () => {
    const apiClient = listClient([instance({ status: "Stopped" })]);
    const remove = vi.spyOn(apiClient, "deleteModelInstance");
    renderApplication(apiClient, "/models");
    const user = userEvent.setup();
    const button = await screen.findByRole("button", { name: "删除" });
    await waitFor(() => expect(button).toBeEnabled());
    await user.click(button);
    expect(screen.queryByLabelText("输入实例名确认")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "确认删除" }));
    await waitFor(() => {
      expect(remove).toHaveBeenCalledWith(INSTANCE_ID);
    });
  });

  it("UI_11 allows bound Failed deletion on the detail page", async () => {
    const apiClient = listClient([]);
    const remove = vi.spyOn(apiClient, "deleteModelInstance");
    apiClient.modelInstanceDetailResults.push(instance({ status: "Failed" }));
    apiClient.aiServiceResults.push({ items: [service()] });
    renderApplication(apiClient, `/models/instances/${INSTANCE_ID}`);
    const user = userEvent.setup();
    const button = await screen.findByRole("button", { name: "删除" });
    await waitFor(() => expect(button).toBeEnabled());
    expect(
      screen.queryByText(/删除前请先更换或解除服务后端/),
    ).not.toBeInTheDocument();
    await user.click(button);
    const dialog = screen.getByRole("dialog");
    expect(
      within(dialog).getByRole("link", { name: "研发助手" }),
    ).toBeVisible();
    expect(within(dialog).queryByRole("textbox")).not.toBeInTheDocument();
    const confirm = within(dialog).getByRole("button", {
      name: "删除实例和服务",
    });
    expect(confirm).toBeEnabled();
    await expectNoBlockingAxeViolations(dialog);
    await user.click(confirm);
    await waitFor(() => {
      expect(remove).toHaveBeenCalledWith(INSTANCE_ID, true);
    });
  });

  it("UI_11 keeps delete blocked when bindings are unavailable", async () => {
    const apiClient = listClient([]);
    apiClient.modelInstanceDetailResults.push(instance({ status: "Stopped" }));
    apiClient.aiServiceResults.push(
      new ApiError({ code: "internal_error", status: 503 }),
    );
    renderApplication(apiClient, `/models/instances/${INSTANCE_ID}`);
    expect(await screen.findByRole("button", { name: "删除" })).toBeDisabled();
    expect(
      await screen.findByText(
        /服务绑定信息暂不可用，停止、重启和删除需等待绑定确认/,
      ),
    ).toBeVisible();
  });

  it("UI_11 sends the opt-in query only for cascade deletion", async () => {
    vi.spyOn(document, "cookie", "get").mockReturnValue(
      "__Host-pws_csrf=ui11-csrf",
    );
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(new Response(null, { status: 204 }));
    const client = new ApiClient();
    await client.deleteModelInstance(INSTANCE_ID);
    await client.deleteModelInstance(INSTANCE_ID, true);
    expect(fetchMock.mock.calls.map(([path]) => path)).toEqual([
      `/api/v1/instances/${INSTANCE_ID}`,
      `/api/v1/instances/${INSTANCE_ID}?delete_bound_services=true`,
    ]);
    expect(
      fetchMock.mock.calls.every(([, options]) => options?.method === "DELETE"),
    ).toBe(true);
  });
});

function listClient(items: ModelInstance[]) {
  const apiClient = new FakeApiClient(ADMIN_ME);
  apiClient.modelInstanceResults.push({ items });
  return apiClient;
}

function buttonLabels(row: HTMLTableRowElement | null) {
  if (row === null) throw new Error("row fixture missing");
  return within(row)
    .getAllByRole("button")
    .map(({ textContent }) => textContent);
}

function instanceRow(table: HTMLElement, name: string) {
  const row = within(table).getByText(name).closest("tr");
  if (row === null) throw new Error(`instance row is missing: ${name}`);
  return row;
}

function eventLabels(table: HTMLElement) {
  return within(table)
    .getAllByRole("row")
    .slice(1)
    .map((row) => within(row).getAllByRole("cell")[2]?.textContent);
}

function findSentinelResidue(sentinel: string) {
  const found: string[] = [];
  if (document.body.textContent.includes(sentinel)) found.push("DOM");
  for (const [name, storage] of [
    ["localStorage", localStorage],
    ["sessionStorage", sessionStorage],
  ] as const) {
    for (let index = 0; index < storage.length; index += 1) {
      const key = storage.key(index) ?? "";
      if (`${key}:${storage.getItem(key) ?? ""}`.includes(sentinel)) {
        found.push(name);
      }
    }
  }
  return found;
}

async function expectNoBlockingAxeViolations(context: Element | Document) {
  const result = await axe.run(context, {
    rules: { "color-contrast": { enabled: false } },
  });
  expect(result.passes.flatMap((rule) => rule.nodes)).not.toHaveLength(0);
  expect(
    result.violations.filter(
      ({ impact }) => impact === "critical" || impact === "serious",
    ),
  ).toEqual([]);
}

function instance(overrides: Partial<ModelInstance> = {}): ModelInstance {
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

function service(overrides: Partial<AiService> = {}): AiService {
  return {
    description: "bound service",
    enabled: true,
    id: uuid(8),
    primary_instance_id: INSTANCE_ID,
    primary_instance_status: "Running",
    name: "研发助手",
    public_model_name: "qwen-public",
    route_version: 1,
    runtime_status: "Available",
    ...overrides,
  };
}

function gpu(index: number) {
  return {
    display_index: index,
    health: "healthy" as const,
    health_reasons: [],
    memory_bytes: 51_539_607_552,
    model: "NVIDIA L20",
    uuid: `GPU-fixture-${String(index)}`,
  };
}

function logEntry(sequence: number, text: string) {
  return {
    original_character_count: text.length,
    sequence,
    text,
    truncated: false,
  };
}

function event(
  seed: number,
  occurredAt: string,
  eventType: ModelInstanceEventPage["items"][number]["event_type"],
): ModelInstanceEventPage["items"][number] {
  return {
    actor: "admin",
    detail: {},
    event_type: eventType,
    from_status: eventType === "StatusChanged" ? "Restarting" : "Running",
    id: uuid(seed),
    occurred_at: occurredAt,
    to_status: eventType === "StatusChanged" ? "Running" : "Running",
  };
}

function eventPage(
  items: ModelInstanceEventPage["items"],
  nextCursor: string | null,
): ModelInstanceEventPage {
  return { items, next_cursor: nextCursor };
}

function rawEventPage() {
  return {
    items: [event(9, "2026-09-21T02:00:00Z", "StatusChanged")],
    next_cursor: null,
  };
}

function jsonResponse(body: unknown) {
  return new Response(JSON.stringify(body), {
    headers: { "Content-Type": "application/json" },
    status: 200,
  });
}

function uuid(seed: number) {
  return `0198f58a-2586-7e37-bbf5-${seed.toString().padStart(12, "0")}`;
}

class FakeEventSource {
  static readonly instances: FakeEventSource[] = [];
  readonly listeners = new Map<string, Set<EventListener>>();
  readonly url: string;
  closed = false;
  onerror: ((event: Event) => void) | null = null;
  onopen: ((event: Event) => void) | null = null;

  constructor(url: string) {
    this.url = url;
    FakeEventSource.instances.push(this);
  }

  addEventListener(type: string, listener: EventListener) {
    const listeners = this.listeners.get(type) ?? new Set<EventListener>();
    listeners.add(listener);
    this.listeners.set(type, listeners);
  }

  removeEventListener(type: string, listener: EventListener) {
    this.listeners.get(type)?.delete(listener);
  }

  emit(type: string) {
    for (const listener of this.listeners.get(type) ?? []) {
      listener(new MessageEvent(type));
    }
  }

  close() {
    this.closed = true;
  }
}
