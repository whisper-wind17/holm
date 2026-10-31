import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import {
  ADMIN_ME,
  FakeApiClient,
  renderApplication,
} from "../app/test-support";
import { BOOTSTRAP_WIZARD_PATH } from "../setup/wizard-page";
import { PasswordPolicyFeedback } from "./password-policy";

// FX-FIX-16: synthetic passwords shared with the server boundary test.
const SEVEN = "Zq7!vR2";
const EIGHT = "Zq7!vR2x";

function field(id: string) {
  const element = document.getElementById(id);
  if (!(element instanceof HTMLInputElement)) {
    throw new Error(`missing form field ${id}`);
  }
  return element;
}

describe("FIX_16 password minimum", () => {
  it("shows seven as weak, eight as fair, and sixteen as strong", () => {
    const view = render(<PasswordPolicyFeedback password={SEVEN} />);
    expect(screen.getByText("至少 8 位（当前 7 位）")).toBeVisible();
    expect(screen.getByRole("meter", { name: "密码强度：弱" })).toBeVisible();
    view.rerender(<PasswordPolicyFeedback password={EIGHT} />);
    expect(screen.getByRole("meter", { name: "密码强度：一般" })).toBeVisible();
    expect(
      screen.getByText("至少 8 位（当前 8 位）").closest("li"),
    ).toHaveAttribute("data-satisfied", "true");
    view.rerender(<PasswordPolicyFeedback password={EIGHT + EIGHT} />);
    expect(screen.getByRole("meter", { name: "密码强度：强" })).toBeVisible();
  });

  it("blocks seven and submits eight in the first password change form", async () => {
    const api = new FakeApiClient({ ...ADMIN_ME, must_change_password: true });
    const change = vi.spyOn(api, "changePassword");
    renderApplication(api, "/change-password");
    const user = userEvent.setup();
    await screen.findByRole("heading", { name: "设置新密码" });
    await user.type(field("password-newPassword"), SEVEN);
    await user.type(field("password-confirmPassword"), SEVEN);
    await user.click(screen.getByRole("button", { name: "保存并进入平台" }));
    expect(await screen.findByText("新密码至少需要 8 个字符")).toBeVisible();
    expect(change).not.toHaveBeenCalled();
    await user.type(field("password-newPassword"), "x");
    await user.type(field("password-confirmPassword"), "x");
    await user.click(screen.getByRole("button", { name: "保存并进入平台" }));
    await waitFor(() => {
      expect(change).toHaveBeenCalledOnce();
    });
    expect(change.mock.calls[0]?.[0]).toMatchObject({
      new_password: EIGHT,
      confirm_password: EIGHT,
    });
  });

  it("blocks seven and submits eight in the administrator setup form", async () => {
    const api = new FakeApiClient();
    api.bootstrapInitialized = false;
    renderApplication(api, BOOTSTRAP_WIZARD_PATH);
    const user = userEvent.setup();
    await user.type(
      await screen.findByLabelText(/^初始化口令/),
      "bootstrap-token-fixture-value",
    );
    await user.click(screen.getByRole("button", { name: "开始检测" }));
    await screen.findByRole("heading", { name: "全部通过" });
    await user.click(screen.getByRole("button", { name: "下一步" }));
    await screen.findByRole("heading", { name: "创建第一个管理员" });
    await user.type(field("setup-username"), "alice");
    await user.type(field("setup-display-name"), "Alice Example");
    await user.type(field("setup-password"), SEVEN);
    await user.type(field("setup-confirm-password"), SEVEN);
    await user.click(screen.getByRole("button", { name: "创建并完成初始化" }));
    expect(await screen.findByText("密码至少需要 8 个字符")).toBeVisible();
    expect(api.bootstrapAdminCalls).toHaveLength(0);
    await user.type(field("setup-password"), "x");
    await user.type(field("setup-confirm-password"), "x");
    await user.click(screen.getByRole("button", { name: "创建并完成初始化" }));
    await waitFor(() => {
      expect(api.bootstrapAdminCalls).toHaveLength(1);
    });
    expect(api.bootstrapAdminCalls[0]?.body).toMatchObject({
      password: EIGHT,
      confirm_password: EIGHT,
    });
  });
});
