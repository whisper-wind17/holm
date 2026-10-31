import axe from "axe-core";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import {
  ADMIN_ME,
  FakeApiClient,
  renderApplication,
  USER_ME,
} from "./test-support";
import { BOOTSTRAP_WIZARD_PATH } from "../setup/wizard-page";

describe("TC-UI-01 app-shell axe scans", () => {
  it("has no Critical or Serious violations on the login page", async () => {
    renderApplication(new FakeApiClient(), "/login");
    await screen.findByRole("heading", { name: "登录" });
    await expectNoBlockingAxeViolations(document);
  });

  it("has no Critical or Serious violations in the Admin shell", async () => {
    renderApplication(new FakeApiClient(ADMIN_ME), "/overview");
    await screen.findByRole("heading", { name: "总览" });
    await expectNoBlockingAxeViolations(document);
  });

  it("has no Critical or Serious violations in the User shell", async () => {
    renderApplication(new FakeApiClient(USER_ME), "/services");
    await screen.findByRole("heading", { name: "AI 服务" });
    await expectNoBlockingAxeViolations(document);
  });

  it("has no Critical or Serious violations with the narrow-screen drawer open", async () => {
    renderApplication(new FakeApiClient(ADMIN_ME), "/overview");
    const user = userEvent.setup();
    await screen.findByRole("heading", { name: "总览" });
    await user.click(screen.getByRole("button", { name: "打开主导航" }));
    expect(await screen.findByRole("dialog", { name: "主导航" })).toBeVisible();
    await expectNoBlockingAxeViolations(document);
  });
});

describe("TC-UI-14 initialization wizard axe scans", () => {
  it("UI_47 has no Critical or Serious violations on each wizard state", async () => {
    const apiClient = new FakeApiClient();
    apiClient.bootstrapInitialized = false;
    renderApplication(apiClient, BOOTSTRAP_WIZARD_PATH);
    const user = userEvent.setup();

    await screen.findByRole("heading", { name: "首次初始化" });
    await expectNoBlockingAxeViolations(document);

    await user.type(
      screen.getByLabelText(/^初始化口令/),
      "axe-bootstrap-token",
    );
    await user.click(screen.getByRole("button", { name: "开始检测" }));
    await screen.findByRole("heading", { name: "全部通过" });
    await expectNoBlockingAxeViolations(document);

    await user.click(screen.getByRole("button", { name: "下一步" }));
    await screen.findByRole("heading", { name: "创建第一个管理员" });
    await expectNoBlockingAxeViolations(document);
  });

  it("has no Critical or Serious violations on the closed-flow panel", async () => {
    renderApplication(new FakeApiClient(), BOOTSTRAP_WIZARD_PATH);
    await screen.findByRole("heading", { name: "已完成初始化" });
    await expectNoBlockingAxeViolations(document);
  });
});

async function expectNoBlockingAxeViolations(context: Element | Document) {
  const result = await axe.run(context, {
    rules: {
      // jsdom has no computed-color canvas; the token contrast gate owns this rule.
      "color-contrast": { enabled: false },
    },
  });
  expect(
    result.passes.flatMap((rule) => rule.nodes),
    "axe must prove that it inspected real nodes",
  ).not.toHaveLength(0);
  expect(
    result.violations.filter(
      (violation) =>
        violation.impact === "critical" || violation.impact === "serious",
    ),
  ).toEqual([]);
}
