import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import axe from "axe-core";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../api/client";
import type {
  BootstrapCheckId,
  BootstrapCheckOutcome,
  BootstrapCheckReason,
  BootstrapCheckReport,
} from "../api/bootstrap-contracts";
import { ApplicationRoutes } from "../app/application";
import { AppErrorBoundary } from "../app/error-boundary";
import {
  ADMIN_ME,
  FakeApiClient,
  PASSING_BOOTSTRAP_REPORT,
  bootstrapAgent,
  renderApplication,
} from "../app/test-support";
import { SessionProvider } from "../auth/session";
import { BOOTSTRAP_WIZARD_PATH } from "./wizard-page";

const WIZARD_TOKEN = "bootstrap-token-fixture-value";
const WIZARD_PASSWORD = "长口令 passphrase 2026";
const WIZARD_SOURCE = resolve(
  dirname(fileURLToPath(import.meta.url)),
  "wizard-page.tsx",
);

/**
 * Everything below renders the wizard through `ApplicationRoutes` at the real
 * route, so a positive control has to survive the whole path: routing, query,
 * contract parsing, step state and markup. A fragment rendered in isolation
 * proves only that a helper still works (see LESSONS.md, M1-09B).
 */
function renderWizard(
  apiClient: FakeApiClient,
  initialEntry = BOOTSTRAP_WIZARD_PATH,
) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <AppErrorBoundary>
        <MemoryRouter initialEntries={[initialEntry]}>
          <SessionProvider apiClient={apiClient}>
            <ApplicationRoutes apiClient={apiClient} />
          </SessionProvider>
        </MemoryRouter>
      </AppErrorBoundary>
    </QueryClientProvider>,
  );
}

function uninitializedClient() {
  const apiClient = new FakeApiClient();
  apiClient.bootstrapInitialized = false;
  return apiClient;
}

function field(id: string) {
  const element = document.getElementById(id);
  if (!(element instanceof HTMLInputElement)) {
    throw new Error(`missing form field ${id}`);
  }
  return element;
}

function card(label: string) {
  return screen.getByRole("region", { name: new RegExp(`^${label} `) });
}

async function startChecks(user: ReturnType<typeof userEvent.setup>) {
  await user.type(await screen.findByLabelText(/^初始化口令/), WIZARD_TOKEN);
  await user.click(screen.getByRole("button", { name: "开始检测" }));
}

async function fillAdministrator(
  user: ReturnType<typeof userEvent.setup>,
  password = WIZARD_PASSWORD,
) {
  await user.type(field("setup-username"), "chen.pei");
  await user.type(field("setup-display-name"), "陈培");
  await user.type(field("setup-password"), password);
  await user.type(field("setup-confirm-password"), password);
  await user.click(screen.getByRole("button", { name: "创建并完成初始化" }));
}

function withCheck(
  report: BootstrapCheckReport,
  id: BootstrapCheckId,
  outcome: BootstrapCheckOutcome,
  reasons: readonly BootstrapCheckReason[],
): BootstrapCheckReport {
  const checks = report.checks.map((check) =>
    check.id === id ? { id, outcome, reasons: [...reasons] } : check,
  );
  const blocking = checks
    .filter((check) => check.outcome === "failed")
    .map((check) => check.id);
  return { ...report, blocking, can_complete: blocking.length === 0, checks };
}

const UNAVAILABLE_GPU_REPORT = withCheck(
  {
    ...PASSING_BOOTSTRAP_REPORT,
    agent: {
      ...bootstrapAgent(),
      gpus: {
        devices: [],
        discovery_state: "unavailable",
        failure_reason: "permission_denied",
        snapshot_id: null,
        topology_links: [],
      },
      status: "available",
    },
  },
  "gpu",
  "failed",
  ["gpu_discovery_unavailable"],
);

const NO_DEVICES_REPORT = withCheck(
  {
    ...PASSING_BOOTSTRAP_REPORT,
    agent: {
      ...bootstrapAgent(),
      gpus: {
        devices: [],
        discovery_state: "no_devices",
        failure_reason: "no_devices",
        snapshot_id: null,
        topology_links: [],
      },
      status: "available",
    },
  },
  "gpu",
  "failed",
  ["gpu_no_devices"],
);

const UNKNOWN_MEMORY_REPORT = withCheck(
  {
    ...PASSING_BOOTSTRAP_REPORT,
    agent: {
      ...bootstrapAgent(),
      host: {
        ...bootstrapAgent().host,
        total_memory_bytes: { reason: "query_failed", state: "unknown" },
      },
      status: "available",
    },
  },
  "memory",
  "failed",
  ["memory_capacity_unknown"],
);

const FAILED_RUNTIME_REPORT = withCheck(
  {
    ...PASSING_BOOTSTRAP_REPORT,
    host_environment: {
      ...PASSING_BOOTSTRAP_REPORT.host_environment,
      items: PASSING_BOOTSTRAP_REPORT.host_environment.items.map((item) =>
        item.id === "docker"
          ? {
              ...item,
              current: { state: "known", value: "26.0" },
              status: "unsatisfied",
              reason: "below_minimum",
            }
          : item,
      ),
    },
  },
  "docker_compose",
  "failed",
  ["docker_below_minimum"],
);

const WARNING_DISK_REPORT = withCheck(
  PASSING_BOOTSTRAP_REPORT,
  "disk",
  "warning",
  ["disk_below_recommended"],
);

afterEach(() => {
  window.history.replaceState(null, "", "/");
  localStorage.clear();
  sessionStorage.clear();
});

describe("UI_47 TC-UI-14 PDD 8.1 三步向导主流程", () => {
  it("walks 欢迎 → 系统检测 → 创建管理员 → 系统已就绪 and enters Overview", async () => {
    const apiClient = uninitializedClient();
    renderWizard(apiClient);
    const user = userEvent.setup();

    expect(
      await screen.findByRole("heading", { name: "首次初始化" }),
    ).toBeVisible();
    expect(
      screen.getByText("将这台 GPU 服务器配置为团队 AI 服务平台。"),
    ).toBeVisible();

    await startChecks(user);
    expect(
      await screen.findByRole("heading", { name: "全部通过" }),
    ).toBeVisible();
    expect(apiClient.bootstrapCheckCalls).toEqual([WIZARD_TOKEN]);

    await user.click(screen.getByRole("button", { name: "下一步" }));
    expect(
      await screen.findByRole("heading", { name: "创建第一个管理员" }),
    ).toBeVisible();

    await fillAdministrator(user);
    expect(await screen.findByRole("heading", { name: "总览" })).toBeVisible();

    // The session came from /bootstrap/admin. The plaintext password
    // is never replayed through /auth/login.
    expect(apiClient.writes).toEqual(["bootstrap-admin"]);
    expect(apiClient.bootstrapAdminCalls).toEqual([
      {
        body: {
          confirm_password: WIZARD_PASSWORD,
          display_name: "陈培",
          password: WIZARD_PASSWORD,
          username: "chen.pei",
        },
        token: WIZARD_TOKEN,
      },
    ]);

    expect(
      screen.queryByRole("button", { name: "进入 Overview" }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByText(
        "系统已就绪。管理员 chen.pei 已创建，初始化入口已永久关闭。",
      ),
    ).toBeVisible();
    await user.click(screen.getByRole("button", { name: "知道了" }));
    expect(
      screen.queryByText(
        "系统已就绪。管理员 chen.pei 已创建，初始化入口已永久关闭。",
      ),
    ).not.toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "总览" })).toBeVisible();
  });

  it("shows GPU 编号、型号、显存 and Health for a device list of any length", async () => {
    const apiClient = uninitializedClient();
    renderWizard(apiClient);
    const user = userEvent.setup();
    await startChecks(user);

    const gpuCard = await waitFor(() => card("GPU"));
    expect(
      within(gpuCard).getByText("检测到 8 张 GPU，Health 全部正常"),
    ).toBeVisible();
    const tiles = within(gpuCard).getAllByRole("listitem");
    expect(tiles).toHaveLength(8);
    expect(within(tiles[0] as HTMLElement).getByText("GPU 0")).toBeVisible();
    expect(within(tiles[7] as HTMLElement).getByText("GPU 7")).toBeVisible();
    expect(
      within(tiles[0] as HTMLElement).getByText("NVIDIA L20"),
    ).toBeVisible();
    expect(within(tiles[0] as HTMLElement).getByText("48 GB")).toBeVisible();
    expect(within(tiles[0] as HTMLElement).getByText("正常")).toBeVisible();
  });

  it("reaches the wizard before the session gate can redirect to /login", async () => {
    const apiClient = uninitializedClient();
    renderWizard(apiClient);
    expect(
      await screen.findByRole("heading", { name: "首次初始化" }),
    ).toBeVisible();
    expect(screen.queryByRole("heading", { name: "登录" })).toBeNull();
    // The wizard never consumed the session; it only ever asked /bootstrap/*.
    expect(apiClient.bootstrapStatusCalls).toEqual([""]);
  });

  it("sends an anonymous visitor of any path to the wizard while uninitialized", async () => {
    renderWizard(uninitializedClient(), "/services");
    expect(
      await screen.findByRole("heading", { name: "首次初始化" }),
    ).toBeVisible();
  });
});

describe("UI_47 TC-UI-22 FX-NV-01 检测中骨架屏，且「读不到」不等于「没有」", () => {
  it("renders a skeleton instead of a device count while detection is running", async () => {
    const apiClient = uninitializedClient();
    vi.spyOn(apiClient, "runBootstrapChecks").mockReturnValue(
      new Promise(() => undefined),
    );
    renderWizard(apiClient);
    const user = userEvent.setup();
    await startChecks(user);

    const busy = await screen.findByRole("heading", {
      name: "正在检测这台服务器",
    });
    expect(busy.closest("section")).toHaveAttribute("aria-busy", "true");
    expect(screen.getByText("正在枚举设备，尚未确定数量。")).toBeVisible();
    expect(screen.queryByText(/没有检测到 GPU/)).toBeNull();
    expect(screen.queryByText(/检测到 0 张/)).toBeNull();
    expect(screen.queryByText(/^0 /)).toBeNull();
  });

  it("says GPU 信息暂不可用 for UNAVAILABLE and never says 没有检测到 GPU", async () => {
    const apiClient = uninitializedClient();
    apiClient.bootstrapCheckDefault = UNAVAILABLE_GPU_REPORT;
    renderWizard(apiClient);
    const user = userEvent.setup();
    await startChecks(user);

    const gpuCard = await waitFor(() => card("GPU"));
    expect(
      within(gpuCard).getByText(
        /GPU 信息暂不可用：GPU 检测权限不足，当前无法判断 GPU 数量与 Health。/,
      ),
    ).toBeVisible();
    expect(
      within(gpuCard).getByText("GPU 检测没有完成，当前读不到 GPU 信息"),
    ).toBeVisible();
    expect(screen.queryByText(/没有检测到 GPU/)).toBeNull();
  });

  it("says 没有检测到 GPU 设备 for NO_DEVICES with a different reason", async () => {
    const apiClient = uninitializedClient();
    apiClient.bootstrapCheckDefault = NO_DEVICES_REPORT;
    renderWizard(apiClient);
    const user = userEvent.setup();
    await startChecks(user);

    const gpuCard = await waitFor(() => card("GPU"));
    expect(within(gpuCard).getByText("没有检测到 GPU 设备")).toBeVisible();
    expect(
      within(gpuCard).getByText("检测已完成，这台服务器没有可用的 GPU 设备"),
    ).toBeVisible();
    expect(screen.queryByText(/GPU 信息暂不可用/)).toBeNull();
  });

  it("renders — and 监控数据暂不可用 for a capacity the host could not report", async () => {
    const apiClient = uninitializedClient();
    apiClient.bootstrapCheckDefault = UNKNOWN_MEMORY_REPORT;
    renderWizard(apiClient);
    const user = userEvent.setup();
    await startChecks(user);

    const memoryCard = await waitFor(() => card("内存"));
    expect(within(memoryCard).getByLabelText("暂无数据")).toHaveTextContent(
      "—",
    );
    expect(within(memoryCard).getByText(/监控数据暂不可用/)).toBeVisible();
    expect(within(memoryCard).queryByText(/^0 B/)).toBeNull();
    expect(within(memoryCard).queryByText(/0 GB/)).toBeNull();
  });
});

describe("UI_47 TC-UI-23 TC-UI-14 失败阻止完成，警告不阻止", () => {
  it("disables 下一步 and offers 重新检测 plus a remedy while a card failed", async () => {
    const apiClient = uninitializedClient();
    apiClient.bootstrapCheckDefault = FAILED_RUNTIME_REPORT;
    renderWizard(apiClient);
    const user = userEvent.setup();
    await startChecks(user);

    expect(
      await screen.findByRole("heading", { name: "1 项未通过" }),
    ).toBeVisible();
    expect(screen.getByRole("button", { name: "下一步" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "重新检测" })).toBeEnabled();
    const runtimeCard = card("Docker／Compose");
    expect(within(runtimeCard).getByText("失败")).toBeVisible();
    expect(
      within(runtimeCard).getByText(/请在宿主机升级 Docker/),
    ).toBeVisible();
    expect(
      screen.queryByRole("heading", { name: "创建第一个管理员" }),
    ).toBeNull();
  });

  it("lets a warning continue and states its impact", async () => {
    const apiClient = uninitializedClient();
    apiClient.bootstrapCheckDefault = WARNING_DISK_REPORT;
    renderWizard(apiClient);
    const user = userEvent.setup();
    await startChecks(user);

    expect(
      await screen.findByRole("heading", { name: "1 项有警告" }),
    ).toBeVisible();
    const diskCard = card("磁盘");
    expect(within(diskCard).getByText("警告")).toBeVisible();
    expect(
      within(diskCard).getByText(
        "可以继续初始化，但能同时保存的模型有限，较大的模型可能下载失败。",
      ),
    ).toBeVisible();

    const proceed = screen.getByRole("button", { name: "了解影响，继续" });
    expect(proceed).toBeEnabled();
    await user.click(proceed);
    expect(
      await screen.findByRole("heading", { name: "创建第一个管理员" }),
    ).toBeVisible();
  });

  it("returns to step two and marks the card the server refused on", async () => {
    const apiClient = uninitializedClient();
    apiClient.bootstrapAdminErrors = [
      new ApiError({
        code: "bootstrap_checks_failed",
        details: { blocking: ["docker_compose"] },
        status: 409,
      }),
    ];
    renderWizard(apiClient);
    const user = userEvent.setup();
    await startChecks(user);
    await user.click(await screen.findByRole("button", { name: "下一步" }));
    await fillAdministrator(user);

    expect(
      await screen.findByRole("heading", { name: "1 项未通过" }),
    ).toBeVisible();
    const runtimeCard = card("Docker／Compose");
    expect(within(runtimeCard).getByText("失败")).toBeVisible();
    expect(screen.getByRole("button", { name: "下一步" })).toBeDisabled();
    expect(screen.getByRole("alert")).toHaveTextContent("Docker／Compose");
  });
});

describe("UI_47 TC-UI-24 TC-SEC-14 初始化口令只在内存里", () => {
  it("leaves no Token in storage or the URL after the whole wizard", async () => {
    const apiClient = uninitializedClient();
    renderWizard(apiClient);
    const user = userEvent.setup();
    await startChecks(user);
    await user.click(await screen.findByRole("button", { name: "下一步" }));
    await fillAdministrator(user);
    await screen.findByRole("heading", { name: "总览" });

    expect(collectTokenResidue(WIZARD_TOKEN)).toEqual([]);
  });

  it("positive control finds a Token planted in sessionStorage", () => {
    sessionStorage.setItem("pw-setup-token", WIZARD_TOKEN);
    expect(collectTokenResidue(WIZARD_TOKEN)).toEqual([
      "sessionStorage:pw-setup-token",
    ]);
  });

  it("strips a Token that arrived in the link fragment from the address", async () => {
    window.history.replaceState(null, "", `/setup#token=${WIZARD_TOKEN}`);
    const apiClient = uninitializedClient();
    renderWizard(apiClient);
    await screen.findByRole("heading", { name: "首次初始化" });

    await waitFor(() => {
      expect(window.location.hash).toBe("");
    });
    expect(collectTokenResidue(WIZARD_TOKEN)).toEqual([]);

    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "开始检测" }));
    await screen.findByRole("heading", { name: "全部通过" });
    expect(apiClient.bootstrapCheckCalls).toEqual([WIZARD_TOKEN]);
  });

  it("asks for the Token again instead of failing silently after a reload", async () => {
    const apiClient = uninitializedClient();
    renderWizard(apiClient);
    await screen.findByRole("heading", { name: "首次初始化" });
    expect(
      screen.getByText(
        "见安装终端末尾的链接，或在服务器上执行 sudo cat /etc/holm/secrets/bootstrap-token",
      ),
    ).toBeVisible();
  });

  it("returns to the welcome step when the server refuses the Token", async () => {
    const apiClient = uninitializedClient();
    apiClient.bootstrapCheckDefault = new ApiError({
      code: "bootstrap_token_invalid",
      status: 401,
    });
    renderWizard(apiClient);
    const user = userEvent.setup();
    await startChecks(user);

    expect(
      await screen.findByText(/初始化口令无效或已失效，本次操作未执行。/),
    ).toBeVisible();
    expect(screen.getByLabelText(/^初始化口令/)).toHaveValue("");
  });
});

describe("UI_47 TC-PWD-01 密码条件清单在向导与改密页逐条相等", () => {
  it("renders the same condition list from the same server violations", async () => {
    const violations = [
      { code: "common_password" },
      { code: "similar_to_username" },
    ];

    const wizardClient = uninitializedClient();
    wizardClient.bootstrapAdminErrors = [
      new ApiError({
        code: "invalid_password",
        details: { password_policy: violations },
        status: 400,
      }),
    ];
    const wizard = renderWizard(wizardClient);
    const user = userEvent.setup();
    await startChecks(user);
    await user.click(await screen.findByRole("button", { name: "下一步" }));
    await fillAdministrator(user);
    await screen.findByText(
      "密码不符合要求，管理员未创建，请按下方条件修改后重试。",
    );
    const fromWizard = conditionTexts();
    wizard.unmount();

    const passwordClient = new FakeApiClient(ADMIN_ME);
    passwordClient.changePasswordErrors = [
      new ApiError({
        code: "invalid_new_password",
        details: { password_policy: violations },
        status: 400,
      }),
    ];
    renderApplication(passwordClient, "/change-password");
    await screen.findByRole("heading", { name: "修改密码" });
    await user.type(field("password-currentPassword"), "current passphrase 1");
    await user.type(field("password-newPassword"), WIZARD_PASSWORD);
    await user.type(field("password-confirmPassword"), WIZARD_PASSWORD);
    await user.click(screen.getByRole("button", { name: "修改密码" }));
    await screen.findByText("新密码不符合安全要求，请更换后重试。");
    const fromPasswordPage = conditionTexts();

    expect(fromWizard).toHaveLength(6);
    expect(fromWizard).toEqual(fromPasswordPage);
  });
});

describe("UI_47 TC-UI-25 TC-UI-14 已初始化后 404 是正常路径", () => {
  it("renders the login entry rather than an error page", async () => {
    const apiClient = new FakeApiClient();
    renderWizard(apiClient);

    expect(
      await screen.findByRole("heading", { name: "已完成初始化" }),
    ).toBeVisible();
    expect(
      screen.queryByRole("heading", { name: "页面暂时无法显示" }),
    ).toBeNull();

    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "前往登录" }));
    expect(await screen.findByRole("heading", { name: "登录" })).toBeVisible();
  });

  it("keeps an anonymous visitor on the login page when initialized", async () => {
    renderWizard(new FakeApiClient(), "/overview");
    expect(await screen.findByRole("heading", { name: "登录" })).toBeVisible();
  });

  it("closes the flow when /bootstrap/admin answers 404 mid-flow", async () => {
    const apiClient = uninitializedClient();
    apiClient.bootstrapAdminErrors = [
      new ApiError({ code: "not_found", status: 404 }),
    ];
    renderWizard(apiClient);
    const user = userEvent.setup();
    await startChecks(user);
    await user.click(await screen.findByRole("button", { name: "下一步" }));
    await fillAdministrator(user);

    expect(
      await screen.findByRole("heading", { name: "已完成初始化" }),
    ).toBeVisible();
  });
});

describe("UI_47 TC-UI-23 TC-UI-14 服务端拒绝在界面上可见", () => {
  it.each([
    [
      "invalid_username",
      new ApiError({
        code: "invalid_username",
        details: { reason_code: "unsupported_characters" },
        status: 400,
      }),
      "用户名只能使用小写字母、数字、点、下划线和短横线，管理员未创建。例如：chen.pei。",
    ],
    [
      "username_taken",
      new ApiError({ code: "username_taken", status: 409 }),
      "该用户名已被占用，管理员未创建，请换一个用户名。",
    ],
  ])("renders the %s refusal beside the field", async (_label, error, text) => {
    const apiClient = uninitializedClient();
    apiClient.bootstrapAdminErrors = [error];
    renderWizard(apiClient);
    const user = userEvent.setup();
    await startChecks(user);
    await user.click(await screen.findByRole("button", { name: "下一步" }));
    await fillAdministrator(user);

    expect(await screen.findByText(text)).toBeVisible();
    expect(
      screen.getByRole("heading", { name: "创建第一个管理员" }),
    ).toBeVisible();
  });

  it("shows an inline failure and a retry when the detection call fails", async () => {
    const apiClient = uninitializedClient();
    apiClient.bootstrapCheckResults = [
      new ApiError({ code: "internal_error", status: 500 }),
    ];
    renderWizard(apiClient);
    const user = userEvent.setup();
    await startChecks(user);

    expect(await screen.findByText("无法完成系统检测")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "重新检测" }));
    expect(
      await screen.findByRole("heading", { name: "全部通过" }),
    ).toBeVisible();
  });

  it("throws a contract error rather than drawing an unknown reason", async () => {
    const apiClient = uninitializedClient();
    apiClient.bootstrapCheckDefault = {
      ...PASSING_BOOTSTRAP_REPORT,
      checks: PASSING_BOOTSTRAP_REPORT.checks.map((check) =>
        check.id === "gpu"
          ? {
              ...check,
              reasons: ["gpu_thermal_throttling" as BootstrapCheckReason],
            }
          : check,
      ),
    };
    renderWizard(apiClient);
    const user = userEvent.setup();
    await startChecks(user);

    expect(
      await screen.findByRole("heading", { name: "页面暂时无法显示" }),
    ).toBeVisible();
    expect(screen.getByText("bootstrap_contract_error")).toBeVisible();
  });
});

describe("UI_47 TC-UI-22 TC-UI-14 状态与状态覆盖", () => {
  it("carries every verdict in text, never in colour alone", async () => {
    const apiClient = uninitializedClient();
    apiClient.bootstrapCheckDefault = withCheck(
      WARNING_DISK_REPORT,
      "docker_compose",
      "failed",
      ["docker_below_minimum"],
    );
    renderWizard(apiClient);
    const user = userEvent.setup();
    await startChecks(user);
    await screen.findByRole("heading", { name: "1 项未通过" });

    expect(within(card("GPU")).getByText("通过")).toBeVisible();
    expect(within(card("磁盘")).getByText("警告")).toBeVisible();
    expect(within(card("Docker／Compose")).getByText("失败")).toBeVisible();
  });

  it("shows a stale marker and refetches on 重新检测", async () => {
    const apiClient = uninitializedClient();
    renderWizard(apiClient);
    const user = userEvent.setup();
    await startChecks(user);
    await screen.findByRole("heading", { name: "全部通过" });

    expect(screen.getByText("检测结果可能已过期")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "重新检测" }));
    await waitFor(() => {
      expect(apiClient.bootstrapCheckCalls).toHaveLength(2);
    });
  });

  it("declares Permission and Empty inapplicable rather than faking them", () => {
    const source = readFileSync(WIZARD_SOURCE, "utf8");
    // Neither state exists in this flow: the wizard is anonymous by
    // construction, and the report always carries all seven cards. The
    // reasoning is written into the module docstring; this guard fails if
    // somebody later adds a fabricated one.
    expect(source).toContain("**Permission** — the flow is anonymous");
    expect(source).toContain("**Empty** — a detection report always carries");
    expect(source).not.toMatch(/EmptyState|PermissionPage/);
  });
});

function conditionTexts() {
  return within(screen.getByRole("region", { name: "密码安全要求" }))
    .getAllByRole("listitem")
    .map((item) => item.textContent);
}

function collectTokenResidue(token: string) {
  const findings: string[] = [];
  for (const [storageName, storage] of [
    ["localStorage", localStorage],
    ["sessionStorage", sessionStorage],
  ] as const) {
    for (let index = 0; index < storage.length; index += 1) {
      const key = storage.key(index);
      if (key === null) {
        continue;
      }
      if (key.includes(token) || (storage.getItem(key) ?? "").includes(token)) {
        findings.push(`${storageName}:${key}`);
      }
    }
  }
  if (
    `${window.location.href}${window.location.search}${window.location.hash}`.includes(
      token,
    )
  ) {
    findings.push("location");
  }
  // Only data-* attributes: a password input still holding what the operator
  // just pasted is the field doing its job, not residue.
  for (const element of document.querySelectorAll<HTMLElement>("*")) {
    for (const attribute of element.getAttributeNames()) {
      if (!attribute.startsWith("data-")) {
        continue;
      }
      if ((element.getAttribute(attribute) ?? "").includes(token)) {
        findings.push(`dom:${attribute}`);
      }
    }
  }
  return findings;
}

// FX-SYS-06: seven-card prototype states, real response shapes, synthetic versions.
describe("SYS_06 TC_INIT_01_04_05 prototype detection cards", () => {
  it("renders seven ordered cards with current versions and minima and passes axe", async () => {
    const client = uninitializedClient();
    const { container } = renderWizard(client);
    await startChecks(userEvent.setup());
    await screen.findByRole("heading", { name: "全部通过" });
    expect(
      [...container.querySelectorAll(".pw-setup-check__head strong")].map(
        (node) => node.textContent,
      ),
    ).toEqual([
      "GPU",
      "NVIDIA 驱动",
      "Docker／Compose",
      "NVIDIA Container Toolkit",
      "磁盘",
      "内存",
      "Holm 组件",
    ]);
    for (const text of [
      "595.45.04（CUDA 13.2），满足最低要求 580.65.06",
      "Docker 28.3.2、Compose 2.38.2，满足最低要求",
      "1.17.8，满足最低要求 1.16",
      "10 个子模块全部就绪",
    ]) {
      expect(screen.getByText(text)).toBeVisible();
    }
    expect((await axe.run(container)).violations).toEqual([]);
  });

  it("warns for unreadable container versions and allows continuing", async () => {
    const client = uninitializedClient();
    const report = structuredClone(PASSING_BOOTSTRAP_REPORT);
    for (const item of report.host_environment.items) {
      if (item.id !== "nvidia_driver") {
        item.current = { state: "unknown", reason: "source_unavailable" };
        item.status = "unknown";
        item.reason = "current_unavailable";
      }
    }
    client.bootstrapCheckDefault = withCheck(
      withCheck(report, "docker_compose", "warning", [
        "docker_version_undetermined",
        "compose_version_undetermined",
      ]),
      "nvidia_container_toolkit",
      "warning",
      ["toolkit_version_undetermined"],
    );
    const { container } = renderWizard(client);
    const user = userEvent.setup();
    await startChecks(user);
    await screen.findByRole("heading", { name: "2 项有警告" });
    for (const name of ["Docker", "Compose", "NVIDIA Container Toolkit"])
      expect(screen.getByText(`读不到 ${name} 版本`)).toBeVisible();
    expect(
      within(card("Docker／Compose")).getAllByText(/这项警告不阻止初始化/),
    ).toHaveLength(2);
    expect((await axe.run(container)).violations).toEqual([]);
    await user.click(screen.getByRole("button", { name: "了解影响，继续" }));
    expect(
      await screen.findByRole("heading", { name: "创建第一个管理员" }),
    ).toBeVisible();
  });

  it("shows the prototype driver and toolkit failures and blocks continuing", async () => {
    const client = uninitializedClient();
    const report = structuredClone(PASSING_BOOTSTRAP_REPORT);
    for (const item of report.host_environment.items) {
      if (item.id === "nvidia_driver") {
        item.current = { state: "unknown", reason: "source_unavailable" };
        item.status = "unknown";
        item.reason = "current_unavailable";
      }
      if (item.id === "nvidia_container_toolkit") {
        item.current = { state: "known", value: "1.14.6" };
        item.status = "unsatisfied";
        item.reason = "below_minimum";
      }
    }
    client.bootstrapCheckDefault = withCheck(
      withCheck(report, "nvidia_driver", "failed", ["driver_version_unknown"]),
      "nvidia_container_toolkit",
      "failed",
      ["toolkit_below_minimum"],
    );
    const { container } = renderWizard(client);
    await startChecks(userEvent.setup());
    await screen.findByRole("heading", { name: "2 项未通过" });
    expect(screen.getByText("读不到 NVIDIA 驱动版本")).toBeVisible();
    expect(screen.getByText("1.14.6，低于最低要求 1.16")).toBeVisible();
    expect(
      screen.getByText(
        "模型实例无法拿到 GPU。请升级 NVIDIA Container Toolkit 后点「重新检测」。",
      ),
    ).toBeVisible();
    expect(screen.getByRole("button", { name: "下一步" })).toBeDisabled();
    expect((await axe.run(container)).violations).toEqual([]);
  });
});

it("SYS_06 TC_INIT_04 preserves mixed Docker failure and Compose warning without promising continuation", async () => {
  const client = uninitializedClient();
  const report = structuredClone(FAILED_RUNTIME_REPORT);
  for (const item of report.host_environment.items) {
    if (item.id === "compose") {
      item.current = { state: "unknown", reason: "source_unavailable" };
      item.status = "unknown";
      item.reason = "current_unavailable";
    }
  }
  client.bootstrapCheckDefault = withCheck(report, "docker_compose", "failed", [
    "docker_below_minimum",
    "compose_version_undetermined",
  ]);
  renderWizard(client);
  await startChecks(userEvent.setup());
  await screen.findByRole("heading", { name: "1 项未通过" });
  const docker = within(card("Docker／Compose"));
  expect(docker.getByText("Docker 26.0，低于最低要求 27.0")).toBeVisible();
  expect(docker.getByText("读不到 Compose 版本")).toBeVisible();
  expect(
    docker.getByText(
      "无法确认平台组件的运行环境。这项警告不阻止初始化，请在宿主机检查 Compose 与发行清单。",
    ),
  ).toBeVisible();
  expect(docker.queryByText(/可以继续初始化/)).toBeNull();
  expect(screen.getByRole("button", { name: "下一步" })).toBeDisabled();
});
