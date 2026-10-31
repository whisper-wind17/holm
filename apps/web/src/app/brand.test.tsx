import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { BOOTSTRAP_WIZARD_PATH } from "../setup/wizard-page";
import { AppShell } from "./shell";
import {
  FakeApiClient,
  renderApplication,
  TestProviders,
  USER_ME,
} from "./test-support";

// FX-BRAND-01: reuse the existing in-memory session and bootstrap fixtures.
describe("BRAND-01 Holm display name", () => {
  it("UI_47 shows the login brand, footer, and browser title", async () => {
    renderApplication(new FakeApiClient(), "/login");
    await screen.findByRole("heading", { name: "登录" });
    expect(screen.getByText("Holm", { exact: true })).toBeVisible();
    expect(
      screen.queryByText("使用平台账号进入 Holm。"),
    ).not.toBeInTheDocument();
    expect(screen.getByText("忘记密码请联系管理员重置。")).toBeVisible();
    expect(screen.getByText("企业私有 AI 服务平台")).toBeVisible();
    expect(document.title).toBe("Holm");
  });

  it("shows the brand on first password change", async () => {
    renderApplication(
      new FakeApiClient({ ...USER_ME, must_change_password: true }),
      "/change-password",
    );
    await screen.findByRole("heading", { name: "设置新密码" });
    expect(screen.getByText("Holm", { exact: true })).toBeVisible();
  });

  it("shows the shared brand on initialization", async () => {
    const apiClient = new FakeApiClient();
    apiClient.bootstrapInitialized = false;
    renderApplication(apiClient, BOOTSTRAP_WIZARD_PATH);
    await screen.findByRole("heading", { name: "首次初始化" });
    expect(screen.getByText("Holm", { exact: true })).toBeVisible();
  });

  it("shows the sidebar brand and fallback topbar title", async () => {
    const apiClient = new FakeApiClient(USER_ME);
    const { container } = render(
      <TestProviders apiClient={apiClient}>
        <AppShell apiClient={apiClient} me={USER_ME} routes={[]}>
          <h1>品牌测试</h1>
        </AppShell>
      </TestProviders>,
    );
    await screen.findByRole("heading", { name: "品牌测试" });
    expect(container.querySelector(".pw-brand__name")).toHaveTextContent(
      "Holm",
    );
    expect(container.querySelector(".pw-topbar__title")).toHaveTextContent(
      "Holm",
    );
  });
});
