import { readFileSync, readdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ApiError } from "../api/client";
import { AppErrorBoundary } from "./error-boundary";
import { buildNavigationRoutes, NavigationContractError } from "./navigation";
import {
  ADMIN_ME,
  FakeApiClient,
  renderApplication,
  USER_ME,
} from "./test-support";

describe("TC-SEC-13 server-derived navigation and route boundary", () => {
  it("keeps exact Admin and User route snapshots derived from the response", () => {
    expect(buildNavigationRoutes(ADMIN_ME.navigation)).toMatchInlineSnapshot(`
      [
        {
          "icon": "home",
          "id": "overview",
          "label": "总览",
          "path": "/overview",
        },
        {
          "icon": "apiServices",
          "id": "ai_services",
          "label": "AI 服务",
          "path": "/services",
        },
        {
          "icon": "layers",
          "id": "models",
          "label": "模型",
          "path": "/models",
        },
        {
          "icon": "users",
          "id": "users",
          "label": "用户",
          "path": "/users",
        },
        {
          "icon": "usage",
          "id": "usage",
          "label": "用量",
          "path": "/usage",
        },
        {
          "icon": "system",
          "id": "system",
          "label": "系统",
          "path": "/system",
        },
      ]
    `);
    expect(buildNavigationRoutes(USER_ME.navigation)).toMatchInlineSnapshot(`
      [
        {
          "icon": "apiServices",
          "id": "ai_services",
          "label": "AI 服务",
          "path": "/services",
        },
        {
          "icon": "key",
          "id": "api_keys",
          "label": "API 密钥",
          "path": "/api-keys",
        },
        {
          "icon": "usage",
          "id": "usage",
          "label": "用量",
          "path": "/usage",
        },
      ]
    `);
  });

  it("changes the route snapshot when the server omits one navigation item", () => {
    const full = buildNavigationRoutes(ADMIN_ME.navigation);
    const reduced = buildNavigationRoutes(ADMIN_ME.navigation.slice(0, -1));
    expect(reduced).toEqual(full.slice(0, -1));
    expect(reduced).not.toEqual(full);
  });

  it("fails loudly for an unknown navigation id and shows a non-blank boundary", () => {
    const unknown = [
      ...USER_ME.navigation,
      {
        id: "future_page",
        label: "未来页面",
        required_capability: "future_capability",
      },
    ];
    expect(() => buildNavigationRoutes(unknown)).toThrow(
      NavigationContractError,
    );

    function BrokenNavigation() {
      buildNavigationRoutes(unknown);
      return null;
    }
    render(
      <AppErrorBoundary>
        <BrokenNavigation />
      </AppErrorBoundary>,
    );
    expect(
      screen.getByRole("heading", { name: "页面暂时无法显示" }),
    ).toBeVisible();
    expect(screen.getByText("navigation_contract_error")).toBeVisible();
  });

  it("does not construct or render administrator routes for a User session", async () => {
    const routes = buildNavigationRoutes(USER_ME.navigation);
    expect(routes.map((route) => route.path)).toEqual([
      "/services",
      "/api-keys",
      "/usage",
    ]);
    renderApplication(new FakeApiClient(USER_ME), "/system");
    expect(
      await screen.findByRole("heading", { name: "无权限" }),
    ).toBeVisible();
    expect(
      screen.queryByRole("link", { name: "系统" }),
    ).not.toBeInTheDocument();
    expect(screen.queryByText("平台状态：待确认")).not.toBeInTheDocument();
  });
});

describe("TC-UI-02 keyboard login, focus, and target recovery", () => {
  it("completes login by keyboard, preserves the target, and focuses its H1", async () => {
    const apiClient = new FakeApiClient();
    apiClient.loginResult = ADMIN_ME;
    renderApplication(apiClient, "/models");
    const user = userEvent.setup();

    const username = await screen.findByLabelText(/^用户名/);
    await user.tab();
    expect(username).toHaveFocus();
    await user.keyboard("admin");
    await user.tab();
    expect(screen.getByLabelText(/^密码/)).toHaveFocus();
    await user.keyboard("correct horse battery staple");
    await user.tab();
    expect(screen.getByRole("button", { name: "显示密码" })).toHaveFocus();
    await user.tab();
    expect(screen.getByRole("button", { name: "登录" })).toHaveFocus();
    await user.keyboard("{Enter}");

    const heading = await screen.findByRole("heading", { name: "模型" });
    await waitFor(() => expect(heading).toHaveFocus());
    expect(screen.getByRole("link", { name: "跳到主内容" })).toBeVisible();
  });

  it("shows login's own 401 without a global redirect loop", async () => {
    const apiClient = new FakeApiClient();
    apiClient.loginError = new ApiError({
      code: "invalid_credentials",
      status: 401,
    });
    renderApplication(apiClient, "/services");
    const user = userEvent.setup();

    await user.type(await screen.findByLabelText(/^用户名/), "missing");
    await user.type(screen.getByLabelText(/^密码/), "wrong-password");
    await user.click(screen.getByRole("button", { name: "登录" }));

    expect(await screen.findByText("用户名或密码不正确")).toBeVisible();
    expect(screen.getByRole("heading", { name: "登录" })).toBeVisible();
    expect(screen.getByLabelText(/^密码/)).toHaveFocus();
  });

  it("returns to login when a protected request reports 401", async () => {
    const apiClient = new FakeApiClient(ADMIN_ME);
    renderApplication(apiClient, "/overview");
    expect(await screen.findByRole("heading", { name: "总览" })).toBeVisible();
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          error: {
            code: "authentication_required",
            details: {},
            request_id: "0198f58a-2586-7e37-bbf5-fefe165e8080",
          },
        }),
        { status: 401 },
      ),
    );

    await expect(apiClient.request("/api/v1/protected")).rejects.toMatchObject({
      status: 401,
    });
    expect(await screen.findByRole("heading", { name: "登录" })).toBeVisible();
  });
});

describe("TC-SEC-16 server-owned login lock display", () => {
  it("UI_47 renders the server retry_after_seconds as a countdown", async () => {
    const apiClient = new FakeApiClient();
    apiClient.loginError = new ApiError({
      code: "login_locked",
      retryAfterSeconds: 173,
      status: 429,
    });
    renderApplication(apiClient, "/services");
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText(/^用户名/), "locked-user");
    await user.type(screen.getByLabelText(/^密码/), "wrong-password");
    await user.click(screen.getByRole("button", { name: "登录" }));
    expect(await screen.findByText("请在 2 分 53 秒后重试。")).toBeVisible();
  });
});

describe("TC-SEC-13 temporary-password route guard", () => {
  it.each(ADMIN_ME.navigation.map((item) => item.id))(
    "routes every server navigation id through password change: %s",
    async (id) => {
      const temporary = { ...ADMIN_ME, must_change_password: true };
      const path = buildNavigationRoutes(temporary.navigation).find(
        (route) => route.id === id,
      )?.path;
      expect(path).toBeDefined();
      renderApplication(new FakeApiClient(temporary), path);
      expect(
        await screen.findByRole("heading", { name: "设置新密码" }),
      ).toBeVisible();
      expect(screen.queryByRole("navigation")).not.toBeInTheDocument();
    },
  );

  it("releases the guard only after password change succeeds", async () => {
    const temporary = { ...USER_ME, must_change_password: true };
    const apiClient = new FakeApiClient(temporary);
    renderApplication(apiClient, "/usage");
    const user = userEvent.setup();
    await user.type(
      await screen.findByLabelText(/^新密码/),
      "长口令 passphrase 2026",
    );
    await user.type(
      screen.getByLabelText(/^确认新密码/),
      "长口令 passphrase 2026",
    );
    await user.click(screen.getByRole("button", { name: "保存并进入平台" }));
    expect(await screen.findByRole("heading", { name: "用量" })).toBeVisible();
    expect(apiClient.writes).toContain("password");
  });
});

describe("TC-SEC-14 browser residue enumeration", () => {
  it("leaves no password or CSRF value after login, navigation, write, and logout", async () => {
    localStorage.setItem("unrelated-local", "safe");
    sessionStorage.setItem("unrelated-session", "safe");
    document.cookie = "__Host-pws_csrf=csrf-residue-sentinel; Path=/";
    const password = "browser-residue-password";
    const apiClient = new FakeApiClient();
    apiClient.loginResult = ADMIN_ME;
    renderApplication(apiClient, "/overview");
    const user = userEvent.setup();

    await user.type(await screen.findByLabelText(/^用户名/), "admin");
    await user.type(screen.getByLabelText(/^密码/), password);
    await user.click(screen.getByRole("button", { name: "登录" }));
    await screen.findByRole("heading", { name: "总览" });
    await apiClient.changePassword({
      confirm_password: "another secure passphrase",
      current_password: password,
      new_password: "another secure passphrase",
    });
    await user.click(screen.getByLabelText("打开用户菜单"));
    await user.click(screen.getByRole("button", { name: "退出登录" }));
    await screen.findByRole("heading", { name: "登录" });

    expect(
      collectSensitiveBrowserResidue([
        password,
        "csrf-residue-sentinel",
        "session-residue-sentinel",
      ]),
    ).toEqual([]);
    expect(apiClient.writes).toEqual(["login", "password", "logout"]);
  });

  it("positive control finds a CSRF value under an unknown localStorage key", () => {
    localStorage.setItem(
      "a-key-the-app-never-defined",
      "csrf-positive-control",
    );
    expect(collectSensitiveBrowserResidue(["csrf-positive-control"])).toEqual([
      "localStorage:a-key-the-app-never-defined",
    ]);
  });

  it("production sources have no browser persistence write capability", () => {
    const sourceRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
    const files = productionFiles(sourceRoot);
    const source = files.map((file) => readFileSync(file, "utf8")).join("\n");
    expect(source).not.toMatch(/localStorage|sessionStorage|indexedDB/);
  });
});

function collectSensitiveBrowserResidue(sensitiveValues: readonly string[]) {
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
      const value = storage.getItem(key) ?? "";
      if (
        sensitiveValues.some(
          (secret) => key.includes(secret) || value.includes(secret),
        )
      ) {
        findings.push(`${storageName}:${key}`);
      }
    }
  }
  const renderedLocation = `${location.href}${location.search}${location.hash}`;
  if (sensitiveValues.some((secret) => renderedLocation.includes(secret))) {
    findings.push("location");
  }
  for (const element of document.querySelectorAll<HTMLElement>("*")) {
    for (const attribute of element.getAttributeNames()) {
      if (!attribute.startsWith("data-")) {
        continue;
      }
      const value = element.getAttribute(attribute) ?? "";
      if (sensitiveValues.some((secret) => value.includes(secret))) {
        findings.push(`dom:${attribute}`);
      }
    }
  }
  return findings;
}

function productionFiles(directory: string): string[] {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const path = resolve(directory, entry.name);
    if (entry.isDirectory()) {
      return productionFiles(path);
    }
    return /\.(ts|tsx)$/.test(entry.name) && !entry.name.includes(".test.")
      ? [path]
      : [];
  });
}
