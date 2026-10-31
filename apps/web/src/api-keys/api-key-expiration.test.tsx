import { UI_46_PERSONAL_SERVICE } from "../app/test-support";
import axe from "axe-core";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client";
import { FakeApiClient, USER_ME, renderApplication } from "../app/test-support";
import { createApiKeyFormSchema } from "./api-key-expiration";

// FX-M4-03C: fixed submission clock and local calendar input, no real credentials.
const openedAt = Date.parse("2029-12-01T00:00:00Z");
const submittedAt = Date.parse("2029-12-01T02:00:00Z");

afterEach(() => vi.restoreAllMocks());

async function openForm() {
  const client = new FakeApiClient(USER_ME);
  client.myAiServiceResults.push({ items: [UI_46_PERSONAL_SERVICE] });
  const create = vi.spyOn(client, "createApiKey").mockRejectedValue(
    new ApiError({
      code: "unavailable",
      status: 503,
      responseMessage: "创建暂时失败，请重试。",
    }),
  );
  renderApplication(client, "/api-keys");
  const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: "创建密钥" }));
  const dialog = screen.getByRole("dialog", { name: "创建密钥" });
  await user.type(within(dialog).getByLabelText(/^名称/), "开发环境");
  return {
    user,
    dialog,
    create,
    select: within(dialog).getByRole("combobox", { name: "有效期" }),
    submit: within(dialog).getByRole("button", { name: "创建密钥" }),
  };
}

describe("M4-03C API key expiration", () => {
  it.each(["never", "30", "90", "180"])(
    "M4-03C submits %s from submission time",
    async (value) => {
      const clock = vi.spyOn(Date, "now").mockReturnValue(openedAt);
      const { user, select, submit, create } = await openForm();
      expect(select).toHaveValue("never");
      expect(screen.queryByLabelText(/^到期时间/)).not.toBeInTheDocument();
      await user.selectOptions(select, value);
      clock.mockReturnValue(submittedAt);
      await user.click(submit);
      await waitFor(() => {
        expect(create).toHaveBeenCalledTimes(1);
      });
      expect(create).toHaveBeenCalledWith({
        name: "开发环境",
        service_ids: [],
        expires_at:
          value === "never"
            ? null
            : new Date(submittedAt + Number(value) * 86400000).toISOString(),
      });
      expect(select).toHaveValue(value);
    },
  );

  it("M4-03C TC-UI-01/02/03 validates custom dates, associates errors, and retains failed input for retry", async () => {
    vi.spyOn(Date, "now").mockReturnValue(submittedAt);
    const { user, select, submit, create, dialog } = await openForm();
    select.focus();
    expect(select).toHaveFocus();
    await user.selectOptions(select, "custom");
    await user.tab();
    const date = within(dialog).getByLabelText(/^到期时间/);
    expect(date).toHaveFocus();
    expect(date).toHaveAccessibleDescription(
      "按浏览器本地时间填写，须晚于当前时间。",
    );
    for (const value of ["", "not-a-date", "2020-01-01T12:30"]) {
      fireEvent.change(date, { target: { value } });
      await user.click(submit);
      await waitFor(() => expect(date).toHaveAttribute("aria-invalid", "true"));
      expect(date).toHaveAccessibleDescription(/到期时间无效/);
      expect(create).not.toHaveBeenCalled();
    }
    const result = await axe.run(document, {
      rules: { "color-contrast": { enabled: false } },
    });
    expect(result.passes.length).toBeGreaterThan(0);
    expect(
      result.violations.filter(
        (item) => item.impact === "critical" || item.impact === "serious",
      ),
    ).toEqual([]);
    fireEvent.change(date, { target: { value: "2030-01-02T12:30" } });
    await user.click(submit);
    expect(
      await within(dialog).findByText("创建暂时失败，请重试。"),
    ).toBeVisible();
    expect(select).toHaveValue("custom");
    expect(date).toHaveValue("2030-01-02T12:30");
    expect(create).toHaveBeenLastCalledWith({
      name: "开发环境",
      service_ids: [],
      expires_at: new Date(2030, 0, 2, 12, 30).toISOString(),
    });
    await user.click(submit);
    await waitFor(() => {
      expect(create).toHaveBeenCalledTimes(2);
    });
    // Switching away cannot submit the old custom timestamp.
    await user.selectOptions(select, "never");
    await user.click(submit);
    await waitFor(() => {
      expect(create).toHaveBeenCalledTimes(3);
    });
    expect(create.mock.calls[2]?.[0].expires_at).toBeNull();
  });

  it("M4-03C resets closed forms and keeps name validation", async () => {
    const { user, select, submit, create, dialog } = await openForm();
    await user.selectOptions(select, "custom");
    fireEvent.change(within(dialog).getByLabelText(/^到期时间/), {
      target: { value: "2030-01-02T12:30" },
    });
    await user.keyboard("{Escape}");
    await user.click(screen.getByRole("button", { name: "创建密钥" }));
    const reopened = screen.getByRole("dialog", { name: "创建密钥" });
    expect(within(reopened).getByRole("combobox")).toHaveValue("never");
    expect(within(reopened).getByLabelText(/^名称/)).toHaveValue("");
    await user.click(
      within(reopened).getByRole("button", { name: "创建密钥" }),
    );
    await waitFor(() =>
      expect(within(reopened).getByLabelText(/^名称/)).toHaveAttribute(
        "aria-invalid",
        "true",
      ),
    );
    expect(create).not.toHaveBeenCalled();
    await user.selectOptions(within(reopened).getByRole("combobox"), "custom");
    expect(within(reopened).getByLabelText(/^到期时间/)).toHaveValue("");
    expect(submit).not.toBeInTheDocument();
  });

  it.each(["", "invalid", "2020-01-01T12:30"])(
    "M4-03C rejects raw invalid custom value %s",
    (customDate) => {
      vi.spyOn(Date, "now").mockReturnValue(submittedAt);
      expect(
        createApiKeyFormSchema.safeParse({
          name: "开发环境",
          expiration: "custom",
          customDate,
        }).success,
      ).toBe(false);
    },
  );
});
