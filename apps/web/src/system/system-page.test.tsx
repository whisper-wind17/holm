import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import axe from "axe-core";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type {
  SystemComponentsResponse,
  SystemServerResponse,
} from "../api/system-contracts";
import {
  ADMIN_ME,
  DEFAULT_SYSTEM_COMPONENTS,
  DEFAULT_SYSTEM_SERVER,
  FakeApiClient,
  renderApplication,
  systemServerWithUnavailableGpu,
  SYSTEM_OBSERVED_AT,
} from "../app/test-support";
import { SYSTEM_QUERY_POLICY } from "./queries";

const APP_CSS_PATH = resolve(
  dirname(fileURLToPath(import.meta.url)),
  "../styles/app.css",
);

describe("TC-GPU-17 consumption keeps GPU discovery states distinct", () => {
  it("renders a six-row skeleton without numbers during the initial load", async () => {
    const apiClient = new FakeApiClient(ADMIN_ME);
    vi.spyOn(apiClient, "getSystemServer").mockReturnValue(
      new Promise(() => undefined),
    );
    renderApplication(apiClient, "/system");

    const busy = await screen.findByLabelText("GPU，正在加载");
    expect(busy).toHaveAttribute("aria-busy", "true");
    expect(within(busy).queryByText(/[0-9—]/)).not.toBeInTheDocument();
    expect(busy.querySelectorAll("tbody tr")).toHaveLength(6);
    expect(screen.queryByText(/平台状态：正常/)).not.toBeInTheDocument();
  });

  it("renders Agent unavailable with a readable cause, never as zero GPUs", async () => {
    const apiClient = new FakeApiClient(ADMIN_ME);
    apiClient.systemServerResults.push({
      agent: { reason: "timeout", status: "unavailable" },
      observed_at: SYSTEM_OBSERVED_AT,
    });
    const { container } = renderApplication(apiClient, "/system");
    expect(await screen.findByText("无法读取服务器信息")).toBeVisible();
    expect(screen.getAllByText(/服务器检测服务响应超时/)).toHaveLength(2);
    expect(() => {
      validateUnavailableGpu(container);
    }).not.toThrow();
  });

  it("renders available Agent with unavailable GPU discovery and its concrete cause", async () => {
    const apiClient = new FakeApiClient(ADMIN_ME);
    apiClient.systemServerResults.push(systemServerWithUnavailableGpu());
    const { container } = renderApplication(apiClient, "/system");

    const alert = (await screen.findByText("GPU 信息暂不可用")).closest(
      '[role="status"]',
    );
    expect(alert).not.toBeNull();
    expect(
      within(alert as HTMLElement).getByText(/GPU 检测权限不足/),
    ).toBeVisible();
    expect(screen.queryByText("没有检测到 GPU")).not.toBeInTheDocument();
    expect(screen.queryByRole("cell", { name: /GPU/ })).not.toBeInTheDocument();
    expect(() => {
      validateUnavailableGpu(container);
    }).not.toThrow();
  });

  it("positive control rejects the page's real zero-device path as unavailable", async () => {
    const { container } = renderApplication(
      new FakeApiClient(ADMIN_ME),
      "/system",
    );
    await screen.findByText("没有检测到 GPU");
    expect(() => {
      validateUnavailableGpu(container);
    }).toThrow(/unavailable GPU was collapsed/);
  });

  it("renders a completed zero-device discovery as a true empty state", async () => {
    renderApplication(new FakeApiClient(ADMIN_ME), "/system");
    expect(await screen.findByText("没有检测到 GPU")).toBeVisible();
    expect(screen.getByText(/检测已完成/)).toBeVisible();
    expect(screen.queryByText("GPU 信息暂不可用")).not.toBeInTheDocument();
  });

  it("renders an arbitrary non-empty GPU inventory and all three Health states", async () => {
    const apiClient = new FakeApiClient(ADMIN_ME);
    apiClient.systemServerResults.push(serverWithGpuHealthStates());
    renderApplication(apiClient, "/system");

    expect(await screen.findByText("GPU 0")).toBeVisible();
    expect(screen.getByText("GPU 1")).toBeVisible();
    expect(screen.getByText("GPU 2")).toBeVisible();
    expect(screen.getByText("GPU 1 Health 异常")).toBeVisible();
    expect(screen.getByText("GPU 2 Health 未知")).toBeVisible();
    expect(screen.getAllByText("正常").length).toBeGreaterThan(0);
    expect(screen.getAllByText("异常").length).toBeGreaterThan(0);
    expect(screen.getAllByText("未知").length).toBeGreaterThan(0);
    expect(screen.queryByText("没有检测到 GPU")).not.toBeInTheDocument();
    expect(screen.queryByText("GPU 信息暂不可用")).not.toBeInTheDocument();
  });
});

describe("TC-HEALTH-22 unavailable metrics never become zero", () => {
  it("SYS_05 TC_HEALTH_22 renders four unsupplied GPU metrics and ledger values as em dashes", async () => {
    const apiClient = new FakeApiClient(ADMIN_ME);
    apiClient.systemServerResults.push(serverWithGpuHealthStates());
    const { container } = renderApplication(apiClient, "/system");
    await screen.findByText("GPU 0");
    validateMissingMetrics(container);

    await userEvent.click(screen.getByRole("tab", { name: /平台组件/ }));
    await userEvent.click(
      screen.getByRole("button", { name: "展开子模块（10）" }),
    );
    const ledgerRow = screen.getByText("调用账本写入器").closest("tr");
    expect(ledgerRow).not.toBeNull();
    expect(
      within(ledgerRow as HTMLElement).getAllByLabelText("暂无数据"),
    ).toHaveLength(2);
    expect(ledgerRow).toHaveTextContent("待写事件");
    expect(ledgerRow).toHaveTextContent("最旧事件");
  });

  it("positive control rejects a zero in any unsupplied metric", () => {
    const { container } = render(
      <table>
        <tbody>
          <tr>
            <td data-column="allocation">—</td>
            <td data-column="utilization">0</td>
            <td data-column="memory">—</td>
            <td data-column="temperature">—</td>
            <td data-column="power">—</td>
          </tr>
        </tbody>
      </table>,
    );
    expect(() => {
      validateMissingMetrics(container);
    }).toThrow(/unsupplied metric rendered without an em dash/);
  });
});

describe("TC-HEALTH-23 global platform status semantics", () => {
  it("shows a skeleton with no conclusion while requests are loading", async () => {
    const apiClient = new FakeApiClient(ADMIN_ME);
    vi.spyOn(apiClient, "getSystemServer").mockReturnValue(
      new Promise(() => undefined),
    );
    vi.spyOn(apiClient, "getSystemComponents").mockReturnValue(
      new Promise(() => undefined),
    );
    renderApplication(apiClient, "/overview");
    expect(await screen.findByLabelText("平台状态正在加载")).toBeVisible();
    expect(
      screen.queryByText(/平台状态：(正常|部分异常|不可用)/),
    ).not.toBeInTheDocument();
  });

  it("shows unavailable and a concrete reason when a request fails", async () => {
    const apiClient = new FakeApiClient(ADMIN_ME);
    apiClient.systemServerResults.push(new Error("network unavailable"));
    vi.spyOn(apiClient, "getSystemComponents").mockReturnValue(
      new Promise(() => undefined),
    );
    renderApplication(apiClient, "/overview");
    expect(await screen.findByText("平台状态：不可用")).toBeVisible();
    expect(screen.getByText("无法获取平台状态，请稍后重试")).toBeVisible();
  });

  it("positive control rejects mapping request failure to normal", () => {
    const { container } = render(
      <div>
        <strong>平台状态：正常</strong>
        <span>服务器与已部署平台组件运行正常</span>
      </div>,
    );
    expect(() => {
      validateFailedPlatformStatus(container);
    }).toThrow(/request failure was rendered as normal/);
  });

  it("lists each concrete component and GPU problem while ignoring not-deployed", async () => {
    const apiClient = new FakeApiClient(ADMIN_ME);
    apiClient.systemServerResults.push(serverWithGpuHealthStates());
    apiClient.systemComponentsResults.push(abnormalComponents());
    renderApplication(apiClient, "/overview");
    expect(await screen.findByText("平台状态：部分异常")).toBeVisible();
    const detail = screen.getByText(/遥测采集器不可用/);
    expect(detail).toHaveTextContent("GPU 1 Health 异常");
    expect(detail).toHaveTextContent("GPU 2 Health 未知");
    expect(detail).not.toHaveTextContent("尚未部署");
  });
});

describe("TC-HEALTH-24 explicit refresh and server observation time", () => {
  it("SYS_05 TC_HEALTH_24 refetches inventory on demand and uses the server observation time", async () => {
    const apiClient = new FakeApiClient(ADMIN_ME);
    const secondObservedAt = "2026-09-01T09:45:10Z";
    apiClient.systemServerResults.push(DEFAULT_SYSTEM_SERVER, {
      ...DEFAULT_SYSTEM_SERVER,
      observed_at: secondObservedAt,
    });
    renderApplication(apiClient, "/system");

    const initialTime = await screen.findByRole("time");
    expect(initialTime).toHaveAttribute("datetime", SYSTEM_OBSERVED_AT);
    expect(SYSTEM_QUERY_POLICY.refetchInterval).toBe(false);
    await userEvent.click(screen.getByRole("button", { name: "重新检测" }));
    await waitFor(() => {
      expect(apiClient.systemServerCalls).toBe(2);
    });
    await waitFor(() =>
      expect(screen.getByRole("time")).toHaveAttribute(
        "datetime",
        secondObservedAt,
      ),
    );
  });

  it("positive control rejects a browser-now detection time", () => {
    const { container } = render(
      <time dateTime="2035-01-01T00:00:00Z">浏览器时间</time>,
    );
    expect(() => {
      validateObservedAt(container, SYSTEM_OBSERVED_AT);
    }).toThrow(/detection time did not come from the server/);
  });

  it("keeps existing data visible and labels it while refreshing or stale", async () => {
    const apiClient = new FakeApiClient(ADMIN_ME);
    vi.spyOn(apiClient, "getSystemServer")
      .mockResolvedValueOnce(serverWithGpuHealthStates())
      .mockRejectedValueOnce(new Error("FX-SYS-05 refresh failed"))
      .mockReturnValueOnce(new Promise(() => undefined));
    renderApplication(apiClient, "/system");
    expect(await screen.findByText("GPU 0")).toBeVisible();
    expect(screen.queryByText("数据可能延迟")).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "重新检测" }));
    expect(
      await screen.findByText("刷新失败，当前显示上次检测结果。请重试。"),
    ).toBeVisible();
    expect(screen.getAllByText("数据可能延迟")).toHaveLength(2);
    expect(screen.getByText("GPU 0")).toBeVisible();
    await userEvent.click(
      within(
        screen
          .getByText("刷新失败，当前显示上次检测结果。请重试。")
          .closest('[role="alert"]') as HTMLElement,
      ).getByRole("button", {
        name: "重试",
      }),
    );
    expect(await screen.findByText("正在检测")).toBeVisible();
    expect(screen.getByText("GPU 0")).toBeVisible();
    expect(screen.queryByLabelText("GPU，正在加载")).not.toBeInTheDocument();
  });
});

describe("TC-HEALTH-25 page error, third state, narrow layout, and detail guard", () => {
  it("shows an ordered error message with a retry action that recovers", async () => {
    const apiClient = new FakeApiClient(ADMIN_ME);
    apiClient.systemServerResults.push(
      new Error("connection failed"),
      DEFAULT_SYSTEM_SERVER,
    );
    renderApplication(apiClient, "/system");
    expect(await screen.findByText("无法获取服务器信息")).toBeVisible();
    expect(screen.getByText(/服务器与 GPU 信息无法显示/)).toBeVisible();
    const alert = screen.getByRole("alert");
    await userEvent.click(within(alert).getByRole("button", { name: "重试" }));
    expect(await screen.findByText("没有检测到 GPU")).toBeVisible();
  });

  it("renders not-deployed as its own state and explains why it is not abnormal", async () => {
    renderApplication(new FakeApiClient(ADMIN_ME), "/system");
    await userEvent.click(await screen.findByRole("tab", { name: /平台组件/ }));
    expect(
      screen.getByText("8 个子模块尚未上报状态，不计入异常"),
    ).toBeVisible();
    await userEvent.click(
      screen.getByRole("button", { name: "展开子模块（10）" }),
    );
    expect(screen.getAllByText("未上报状态")).toHaveLength(8);
    expect(
      screen.getAllByText("该子模块尚未上报状态，不计入异常。"),
    ).toHaveLength(8);
    expect(screen.queryByText(/组件需要关注/)).not.toBeInTheDocument();
  });

  it("keeps essential columns and both tabs usable under the existing mobile rule", async () => {
    const css = readFileSync(APP_CSS_PATH, "utf8");
    expect(css.match(/767px/g)).toHaveLength(1);
    const mobile = mediaBlock(css, "(max-width: 767px)");
    expect(mobile).toContain('[data-column="allocation"]');
    expect(mobile).toContain('[data-column="version"]');

    const apiClient = new FakeApiClient(ADMIN_ME);
    apiClient.systemServerResults.push(serverWithGpuHealthStates());
    renderApplication(apiClient, "/system");
    expect(
      await screen.findByRole("columnheader", { name: "GPU" }),
    ).toBeVisible();
    expect(screen.getByRole("columnheader", { name: "Health" })).toBeVisible();
    await userEvent.click(screen.getByRole("tab", { name: /平台组件/ }));
    await userEvent.click(
      screen.getByRole("button", { name: "展开子模块（10）" }),
    );
    expect(screen.getByRole("columnheader", { name: "子模块" })).toBeVisible();
    expect(screen.getAllByRole("columnheader", { name: "状态" })).toHaveLength(
      3,
    );
    const scopedCss = readFileSync(
      resolve(dirname(fileURLToPath(import.meta.url)), "system-page.css"),
      "utf8",
    );
    expect(scopedCss).toContain(
      ".pw-system-page .pw-tabs__content[data-system-panel] [data-column]",
    );
    expect(scopedCss).toContain("display: table-cell");
    await userEvent.click(screen.getByRole("button", { name: "打开主导航" }));
    const drawer = await screen.findByRole("dialog", { name: "主导航" });
    expect(
      within(drawer).getByRole("link", { name: /平台状态：/ }),
    ).toBeVisible();
  });

  it("renders no orchestration details", async () => {
    const apiClient = new FakeApiClient(ADMIN_ME);
    apiClient.systemComponentsResults.push(abnormalComponents());
    const { container } = renderApplication(apiClient, "/system");
    await userEvent.click(await screen.findByRole("tab", { name: /平台组件/ }));
    expect(() => {
      validateNoOrchestrationDetails(container);
    }).not.toThrow();
  });

  it("positive control rejects an orchestration detail flowing through the real page", async () => {
    const apiClient = new FakeApiClient(ADMIN_ME);
    apiClient.systemComponentsResults.push(componentsWithOrchestrationDetail());
    const { container } = renderApplication(apiClient, "/system");
    await userEvent.click(await screen.findByRole("tab", { name: /平台组件/ }));

    expect(() => {
      validateNoOrchestrationDetails(container);
    }).toThrow(/orchestration detail leaked/);
  });
});

describe("TC-UI-01 system tabs axe coverage", () => {
  it.each([
    ["服务器正常态", "server", false],
    ["服务器异常态", "server", true],
    ["平台组件正常态", "components", false],
    ["平台组件异常态", "components", true],
  ] as const)(
    "has non-empty axe passes and no violations: %s",
    async (_name, tab, abnormal) => {
      const apiClient = new FakeApiClient(ADMIN_ME);
      apiClient.systemServerResults.push(
        abnormal ? serverWithGpuHealthStates() : serverWithHealthyGpu(),
      );
      apiClient.systemComponentsResults.push(
        abnormal ? abnormalComponents() : allReadyComponents(),
      );
      const { container } = renderApplication(apiClient, "/system");
      await screen.findByText("GPU 0");
      if (tab === "components") {
        await userEvent.click(screen.getByRole("tab", { name: /平台组件/ }));
      }
      const result = await axe.run(container, {
        rules: { "color-contrast": { enabled: false } },
      });
      expect(result.passes.flatMap((rule) => rule.nodes)).not.toHaveLength(0);
      expect(result.violations).toEqual([]);
    },
  );
});

function serverWithHealthyGpu() {
  const server = serverWithGpuHealthStates();
  if (server.agent.status !== "available") throw new Error("fixture error");
  const firstGpu = server.agent.gpus.devices[0];
  if (firstGpu === undefined) throw new Error("fixture has no GPU");
  server.agent.gpus.devices = [firstGpu];
  return server;
}

function serverWithGpuHealthStates(): SystemServerResponse {
  const server = structuredClone(DEFAULT_SYSTEM_SERVER);
  if (server.agent.status !== "available") throw new Error("fixture error");
  server.agent.gpus = {
    devices: [
      gpu(0, "healthy", []),
      gpu(1, "unhealthy", ["ecc_errors_detected"]),
      gpu(2, "unknown", ["query_failed"]),
    ],
    discovery_state: "ready",
    failure_reason: "none",
    snapshot_id: "snapshot-three-health-states",
    topology_links: [],
  };
  return server;
}

function gpu(
  displayIndex: number,
  health: "healthy" | "unhealthy" | "unknown",
  healthReasons: Array<
    | "ecc_errors_detected"
    | "protocol_invalid"
    | "query_failed"
    | "query_unsupported"
  >,
) {
  return {
    display_index: displayIndex,
    health,
    health_reasons: healthReasons,
    memory_bytes: 51_539_607_552,
    model: "NVIDIA L20",
    uuid: `GPU-test-${String(displayIndex)}`,
  };
}

function abnormalComponents(): SystemComponentsResponse {
  const response = structuredClone(DEFAULT_SYSTEM_COMPONENTS);
  const telemetry = response.components.find(
    ({ id }) => id === "Telemetry Collector",
  );
  if (telemetry === undefined) throw new Error("fixture error");
  telemetry.status = "unavailable";
  telemetry.reason = "heartbeat_stale";
  telemetry.version = { state: "known", value: "0.1.0" };
  response.summary = {
    abnormal_count: 1,
    not_deployed_count: 7,
    ready_count: 2,
    status: "degraded",
  };
  return response;
}

function allReadyComponents(): SystemComponentsResponse {
  const response = structuredClone(DEFAULT_SYSTEM_COMPONENTS);
  response.components = response.components.map((component) => ({
    ...component,
    reason: "self_ready",
    status: "ready",
    version: { state: "known", value: "0.1.0" },
  }));
  response.summary = {
    abnormal_count: 0,
    not_deployed_count: 0,
    ready_count: 10,
    status: "ready",
  };
  return response;
}

function componentsWithOrchestrationDetail(): SystemComponentsResponse {
  const response = structuredClone(DEFAULT_SYSTEM_COMPONENTS);
  const controlPlane = response.components.find(
    ({ id }) => id === "Control Plane",
  );
  if (controlPlane === undefined) throw new Error("fixture error");
  response.platform.build_version = "容器名 holm-control-api";
  return response;
}

function validateUnavailableGpu(container: HTMLElement) {
  if (
    container.textContent.includes("没有检测到 GPU") ||
    container.querySelector('td[data-column="gpu"]') !== null
  ) {
    throw new Error("unavailable GPU was collapsed into an empty result");
  }
  expect(container).toHaveTextContent(/无法读取服务器信息|GPU 信息暂不可用/);
}

function validateMissingMetrics(container: HTMLElement) {
  for (const column of ["utilization", "memory", "temperature", "power"]) {
    const cells = [
      ...container.querySelectorAll(`td[data-column="${column}"]`),
    ];
    expect(cells.length, `missing rendered ${column} cells`).toBeGreaterThan(0);
    for (const cell of cells) {
      if (!cell.textContent.includes("—") || cell.textContent.trim() === "0") {
        throw new Error("unsupplied metric rendered without an em dash");
      }
    }
  }
}

function validateFailedPlatformStatus(container: HTMLElement) {
  if (container.textContent.includes("平台状态：正常")) {
    throw new Error("request failure was rendered as normal");
  }
  expect(container).toHaveTextContent("平台状态：不可用");
  expect(container).toHaveTextContent("无法获取平台状态");
}

function validateObservedAt(container: HTMLElement, expected: string) {
  if (container.querySelector("time")?.getAttribute("datetime") !== expected) {
    throw new Error("detection time did not come from the server");
  }
}

function validateNoOrchestrationDetails(container: HTMLElement) {
  if (
    /容器|编排|重启次数|https?:\/\/|\/workspace\//.test(container.textContent)
  ) {
    throw new Error("orchestration detail leaked into the system page");
  }
}

function mediaBlock(css: string, query: string) {
  const marker = `@media ${query}`;
  const start = css.indexOf(marker);
  const opening = css.indexOf("{", start + marker.length);
  let depth = 0;
  for (let index = opening; index < css.length; index += 1) {
    if (css[index] === "{") depth += 1;
    if (css[index] === "}") depth -= 1;
    if (depth === 0) return css.slice(opening + 1, index);
  }
  throw new Error(`missing or unterminated media block ${query}`);
}
