import { focusManager } from "@tanstack/react-query";
import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import axe from "axe-core";
import { describe, expect, it, vi } from "vitest";

import type { ListedApiKey } from "../api/api-key-contracts";
import { ApiError } from "../api/client";
import type { UsageGroup, UsageReport } from "../api/usage-contracts";
import {
  ADMIN_ME,
  FakeApiClient,
  USER_ME,
  renderApplication,
} from "../app/test-support";

// FX-M4-03E: safe PROTO-07 key metadata and synthetic aggregate numbers only.
const key: ListedApiKey = {
  created_at: "2026-09-01T00:00:00Z",
  display_prefix: "holm_sk_",
  expires_at: null,
  id: "0198f58a-2586-7e37-bbf5-fefe165e8401",
  last4: "1234",
  last_used_at: null,
  name: "应用密钥",
  revoked_at: null,
  service_ids: [],
  status: "Active",
  effective_services: [],
};
const unused = {
  ...key,
  id: "0198f58a-2586-7e37-bbf5-fefe165e8402",
  name: "未调用密钥",
};
const revoked: ListedApiKey = {
  ...key,
  id: "0198f58a-2586-7e37-bbf5-fefe165e8403",
  name: "撤销密钥",
  status: "Revoked",
};
const totals = {
  error_rate: 0,
  error_requests: 0,
  input_tokens: 10,
  output_tokens: 20,
  partial_token_requests: 0,
  requests: 3,
  total_tokens: 30,
};
function report(details: UsageGroup[] = []): UsageReport {
  return {
    bucket_width: "hour",
    details,
    from: "2026-10-08T00:00:00Z",
    to: "2026-10-08T12:00:00Z",
    tz: "UTC",
    summary: totals,
    trend: [],
  };
}
function group(
  item: ListedApiKey,
  values: Partial<UsageGroup> = {},
): UsageGroup {
  return {
    ...totals,
    display_name: item.name,
    group_id: item.id,
    is_deleted: false,
    status: item.status,
    ...values,
  };
}
function setup(me = USER_ME) {
  const client = new FakeApiClient(me);
  vi.spyOn(client, "getApiKeys").mockResolvedValue({
    items: [key, unused, revoked],
  });
  const usage = vi
    .spyOn(client, "getUsage")
    .mockResolvedValue(
      report([
        group(revoked, { is_deleted: true, requests: 7, total_tokens: null }),
        group(key),
      ]),
    );
  return { client, usage };
}
function cell(name: string, column: string) {
  const row = screen.getByRole("cell", { name }).closest("tr");
  if (row === null) throw new Error("missing key row");
  const value = row.querySelector(`[data-column="${column}"]`);
  if (value === null) throw new Error("missing usage cell");
  return value;
}

describe("M4-03E personal API key today usage", () => {
  it("M4-03E TC-UI-01/08 maps by key ID, keeps deleted/revoked usage, and distinguishes zero from unknown", async () => {
    const { client, usage } = setup();
    renderApplication(client, "/api-keys");
    await waitFor(() =>
      expect(cell("应用密钥", "today-tokens")).toHaveTextContent("30"),
    );
    expect(cell("应用密钥", "today-requests")).toHaveTextContent(/^3$/);
    expect(cell("未调用密钥", "today-requests")).toHaveTextContent(/^0$/);
    expect(cell("未调用密钥", "today-tokens")).toHaveTextContent(/^0$/);
    expect(cell("撤销密钥", "today-requests")).toHaveTextContent(/^7$/);
    expect(cell("撤销密钥", "today-tokens")).toHaveTextContent("未知");
    expect(cell("撤销密钥", "today-tokens")).not.toHaveTextContent("0");
    const now = new Date();
    const filters = usage.mock.calls[0]?.[0];
    if (filters === undefined) throw new Error("missing usage request");
    expect(usage).toHaveBeenCalledWith({
      from: new Date(
        now.getFullYear(),
        now.getMonth(),
        now.getDate(),
      ).toISOString(),
      to: filters.to,
      groupBy: "api_key",
      timeZone: Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC",
      userId: USER_ME.user.id,
    });
    expect(new Date(filters.to).getTime()).toBeLessThanOrEqual(Date.now());
    expect((await axe.run(document.body)).violations).toEqual([]);
  });

  it("M4-03E always filters administrator personal usage by the signed-in user", async () => {
    const { client, usage } = setup({
      ...ADMIN_ME,
      navigation: USER_ME.navigation,
      capabilities: [
        ...ADMIN_ME.capabilities,
        { capability: "view_usage", scope: "all" },
      ],
    });
    vi.spyOn(client, "getUser").mockResolvedValue({
      ...ADMIN_ME.user,
      api_key_count: 3,
      authorized_service_count: 1,
      created_at: "2026-10-01T00:00:00Z",
      authorized_services: [
        {
          max_concurrent_requests: null,
          max_requests_per_minute: null,
          id: key.id,
          name: "研发服务",
          public_model_name: "public-alpha",
          description: "研发服务",
          enabled: true,
        },
      ],
      api_keys: [],
      last_login_at: null,
      must_change_password: false,
      role: "admin",
      status: "active",
    });
    renderApplication(client, "/api-keys");
    await waitFor(() => {
      expect(usage).toHaveBeenCalled();
    });
    expect(
      usage.mock.calls.every(
        ([filters]) =>
          filters.userId === ADMIN_ME.user.id && filters.groupBy === "api_key",
      ),
    ).toBe(true);
    await waitFor(() =>
      expect(cell("应用密钥", "today-tokens")).toHaveTextContent("30"),
    );
  });

  it("M4-03E TC-UI-08 keeps pending and failed usage unavailable while key operations remain usable", async () => {
    const { client, usage } = setup();
    let reject: (error: Error) => void = () => {};
    usage.mockImplementation(
      () =>
        new Promise((_resolve, fail) => {
          reject = fail;
        }),
    );
    renderApplication(client, "/api-keys");
    await screen.findByRole("cell", { name: "应用密钥" });
    expect(cell("应用密钥", "today-requests")).toHaveTextContent("暂不可用");
    expect(cell("应用密钥", "today-tokens")).not.toHaveTextContent("0");
    await act(async () => {
      reject(new ApiError({ code: "unavailable", status: 503 }));
      await Promise.resolve();
    });
    expect(cell("应用密钥", "today-tokens")).toHaveTextContent("暂不可用");
    await userEvent
      .setup()
      .click(
        within(screen.getByRole("row", { name: /应用密钥/ })).getByRole(
          "button",
          { name: "重命名" },
        ),
      );
    expect(
      await screen.findByRole("dialog", { name: "重命名密钥" }),
    ).toBeVisible();
  });

  it("M4-03E TC-UI-08 hides cached usage after refresh failure and recovers on the next read", async () => {
    const { client, usage } = setup();
    const { queryClient } = renderApplication(client, "/api-keys");
    await waitFor(() =>
      expect(cell("应用密钥", "today-tokens")).toHaveTextContent("30"),
    );
    usage.mockRejectedValue(new ApiError({ code: "unavailable", status: 503 }));
    await act(() =>
      queryClient.invalidateQueries({ queryKey: ["api-key-today-usage"] }),
    );
    await waitFor(() =>
      expect(cell("应用密钥", "today-tokens")).toHaveTextContent("暂不可用"),
    );
    expect(cell("应用密钥", "today-requests")).toHaveTextContent("暂不可用");
    usage.mockResolvedValue(report());
    await act(() =>
      queryClient.invalidateQueries({ queryKey: ["api-key-today-usage"] }),
    );
    await waitFor(() =>
      expect(cell("应用密钥", "today-tokens")).toHaveTextContent(/^0$/),
    );
  });

  it("M4-03E does not query usage without the capability", async () => {
    const { client, usage } = setup({
      ...USER_ME,
      capabilities: USER_ME.capabilities.filter(
        ({ capability }) => capability !== "view_usage",
      ),
    });
    const { queryClient } = renderApplication(client, "/api-keys");
    await screen.findByRole("cell", { name: "应用密钥" });
    expect(usage).not.toHaveBeenCalled();
    expect(cell("应用密钥", "today-requests")).toHaveTextContent("暂不可用");
    // Even a forced fetch cannot turn a missing identity into an ALL query.
    const query = queryClient
      .getQueryCache()
      .find({ queryKey: ["api-key-today-usage"], exact: false });
    expect(query).toBeDefined();
    if (query === undefined) throw new Error("missing usage query");
    await expect(query.fetch()).rejects.toThrow(
      "personal usage requires a user",
    );
    expect(usage).not.toHaveBeenCalled();
  });

  it("M4-03E polls every 30 seconds, pauses while hidden, and stops on unmount", async () => {
    const { client, usage } = setup();
    vi.useFakeTimers();
    focusManager.setFocused(true);
    const view = renderApplication(client, "/api-keys");
    try {
      await act(() => vi.advanceTimersByTimeAsync(100));
      expect(usage).toHaveBeenCalledTimes(1);
      await act(() => vi.advanceTimersByTimeAsync(29_000));
      expect(usage).toHaveBeenCalledTimes(1);
      await act(() => vi.advanceTimersByTimeAsync(1_000));
      expect(usage).toHaveBeenCalledTimes(2);
      focusManager.setFocused(false);
      await act(() => vi.advanceTimersByTimeAsync(60_000));
      expect(usage).toHaveBeenCalledTimes(2);
      await act(async () => {
        focusManager.setFocused(true);
        await Promise.resolve();
      });
      await act(() => vi.advanceTimersByTimeAsync(30_000));
      expect(usage.mock.calls.length).toBeGreaterThan(2);
      view.unmount();
      const calls = usage.mock.calls.length;
      await act(() => vi.advanceTimersByTimeAsync(60_000));
      expect(usage).toHaveBeenCalledTimes(calls);
    } finally {
      view.unmount();
      focusManager.setFocused(undefined);
      vi.useRealTimers();
    }
  });

  it("M4-03E TC-UI-01 keeps both usage columns and key actions in the narrow-screen table", async () => {
    const originalWidth = window.innerWidth;
    Object.defineProperty(window, "innerWidth", {
      configurable: true,
      value: 375,
    });
    const { client } = setup();
    const view = renderApplication(client, "/api-keys");
    try {
      await waitFor(() =>
        expect(cell("应用密钥", "today-tokens")).toHaveTextContent("30"),
      );
      expect(
        screen.getByRole("columnheader", { name: "今日 Requests" }),
      ).toBeVisible();
      expect(
        screen.getByRole("columnheader", { name: "今日 Tokens" }),
      ).toBeVisible();
      expect(
        screen.getAllByRole("button", { name: "撤销密钥" })[0],
      ).toBeEnabled();
      expect(
        screen.getByRole("table", { name: "API 密钥" }).parentElement,
      ).toHaveClass("pw-table-wrap");
      expect((await axe.run(document.body)).violations).toEqual([]);
    } finally {
      view.unmount();
      Object.defineProperty(window, "innerWidth", {
        configurable: true,
        value: originalWidth,
      });
    }
  });
});
