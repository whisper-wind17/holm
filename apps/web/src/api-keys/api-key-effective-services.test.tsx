import axe from "axe-core";
import { act, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client";
import {
  ApiKeyContractError,
  parseApiKeyList,
  type ListedApiKey,
} from "../api/api-key-contracts";
import {
  ADMIN_ME,
  FakeApiClient,
  USER_ME,
  renderApplication,
} from "../app/test-support";
import { API_KEYS_QUERY_KEY } from "./queries";

// FX-M4-03B: PROTO-07 safe display metadata with two public service identities.
const alpha = {
  id: "0198f58a-2586-7e37-bbf5-fefe165e8402",
  name: "研发服务",
  public_model_name: "public-alpha",
};
const beta = {
  id: "0198f58a-2586-7e37-bbf5-fefe165e8403",
  name: "文档服务",
  public_model_name: "public-beta",
};
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
  effective_services: [alpha, beta],
};

async function assertAccessible() {
  const result = await axe.run(document, {
    rules: { "color-contrast": { enabled: false } },
  });
  expect(result.passes.length).toBeGreaterThan(0);
  expect(
    result.violations.filter(
      (item) => item.impact === "critical" || item.impact === "serious",
    ),
  ).toEqual([]);
}

describe("M4-03B effective API key services", () => {
  it("M4-03B TC-UI-01/08 shows safe public names for the user and updates revoked grants", async () => {
    const client = new FakeApiClient(USER_ME);
    const list = vi
      .spyOn(client, "getApiKeys")
      .mockResolvedValue({ items: [key] });
    const { queryClient } = renderApplication(client, "/api-keys");
    expect(
      await screen.findByText(
        "研发服务 · public-alpha、文档服务 · public-beta",
      ),
    ).toBeVisible();
    expect(screen.queryByText("全部服务")).not.toBeInTheDocument();
    await assertAccessible();
    list.mockResolvedValue({
      items: [{ ...key, effective_services: [beta] }],
    });
    await act(() =>
      queryClient.invalidateQueries({ queryKey: API_KEYS_QUERY_KEY }),
    );
    expect(await screen.findByText("文档服务 · public-beta")).toBeVisible();
    expect(screen.queryByText(/public-alpha/)).not.toBeInTheDocument();
    list.mockResolvedValue({
      items: [{ ...key, service_ids: [alpha.id], effective_services: [] }],
    });
    await act(() =>
      queryClient.invalidateQueries({ queryKey: API_KEYS_QUERY_KEY }),
    );
    expect(await screen.findByText("无可用服务")).toBeVisible();
    expect(screen.getByRole("button", { name: "撤销密钥" })).toBeEnabled();
    expect(screen.getByText("有效")).toBeVisible();
    expect(screen.queryByText("全部服务")).not.toBeInTheDocument();
    await assertAccessible();
    list.mockResolvedValue({ items: [key] });
    await act(() =>
      queryClient.invalidateQueries({ queryKey: API_KEYS_QUERY_KEY }),
    );
    expect(
      await screen.findByText(
        "研发服务 · public-alpha、文档服务 · public-beta",
      ),
    ).toBeVisible();
  });

  it("M4-03B records the existing administrator route gap without inventing navigation", async () => {
    const client = new FakeApiClient(ADMIN_ME);
    const list = vi.spyOn(client, "getApiKeys");
    renderApplication(client, "/api-keys");
    expect(
      await screen.findByRole("heading", { name: "无权限" }),
    ).toBeVisible();
    expect(list).not.toHaveBeenCalled();
    expect(
      screen.queryByRole("link", { name: "个人 API 密钥" }),
    ).not.toBeInTheDocument();
  });

  it.each(["Expired", "Revoked"] as const)(
    "M4-03B TC-UI-08 keeps intrinsic %s status when the effective range is empty",
    async (status) => {
      const client = new FakeApiClient(USER_ME);
      vi.spyOn(client, "getApiKeys").mockResolvedValue({
        items: [{ ...key, status, effective_services: [] }],
      });
      renderApplication(client, "/api-keys");
      expect(await screen.findByText("无可用服务")).toBeVisible();
      expect(
        screen.getByText(status === "Expired" ? "已过期" : "已撤销"),
      ).toBeVisible();
      expect(screen.getByRole("button", { name: "删除记录" })).toBeEnabled();
      expect(
        screen.queryByRole("button", { name: "撤销密钥" }),
      ).not.toBeInTheDocument();
    },
  );

  it("M4-03B TC-UI-08 distinguishes failed reads from empty ranges and retries", async () => {
    const client = new FakeApiClient(USER_ME);
    const list = vi
      .spyOn(client, "getApiKeys")
      .mockRejectedValue(new ApiError({ code: "unavailable", status: 503 }));
    renderApplication(client, "/api-keys");
    expect(await screen.findByText("无法获取 API 密钥")).toBeVisible();
    expect(screen.queryByText("无可用服务")).not.toBeInTheDocument();
    expect(screen.queryByText("全部服务")).not.toBeInTheDocument();
    expect(screen.queryByText("还没有 API 密钥")).not.toBeInTheDocument();
    await assertAccessible();
    list.mockResolvedValue({ items: [key] });
    await userEvent.setup().click(screen.getByRole("button", { name: "重试" }));
    expect(
      await screen.findByText(
        "研发服务 · public-alpha、文档服务 · public-beta",
      ),
    ).toBeVisible();
  });

  it("M4-03B TC-UI-08 marks cached ranges stale when refresh fails and clears the warning after retry", async () => {
    const client = new FakeApiClient(USER_ME);
    const list = vi
      .spyOn(client, "getApiKeys")
      .mockResolvedValue({ items: [key] });
    const { queryClient } = renderApplication(client, "/api-keys");
    await screen.findByText("研发服务 · public-alpha、文档服务 · public-beta");
    list.mockRejectedValue(new ApiKeyContractError());
    await act(() =>
      queryClient.invalidateQueries({ queryKey: API_KEYS_QUERY_KEY }),
    );
    expect(await screen.findByText("API 密钥数据可能过期")).toBeVisible();
    expect(
      screen.getByText("研发服务 · public-alpha、文档服务 · public-beta"),
    ).toBeVisible();
    expect(screen.queryByText("无可用服务")).not.toBeInTheDocument();
    await assertAccessible();
    list.mockResolvedValue({ items: [{ ...key, effective_services: [] }] });
    await userEvent.setup().click(screen.getByRole("button", { name: "重试" }));
    expect(await screen.findByText("无可用服务")).toBeVisible();
    expect(screen.queryByText("API 密钥数据可能过期")).not.toBeInTheDocument();
  });

  it("M4-03B refreshes an open page without navigation or focus and stops after unmount", async () => {
    const client = new FakeApiClient(USER_ME);
    const list = vi
      .spyOn(client, "getApiKeys")
      .mockResolvedValue({ items: [key] });
    const view = renderApplication(client, "/api-keys");
    await screen.findByText("研发服务 · public-alpha、文档服务 · public-beta");
    list.mockResolvedValue({ items: [{ ...key, effective_services: [] }] });
    await waitFor(() => expect(screen.getByText("无可用服务")).toBeVisible(), {
      timeout: 7_000,
    });
    view.unmount();
    const calls = list.mock.calls.length;
    vi.useFakeTimers();
    try {
      await vi.advanceTimersByTimeAsync(10_000);
      expect(list).toHaveBeenCalledTimes(calls);
    } finally {
      vi.useRealTimers();
    }
  }, 10_000);

  it("M4-03B TC-UI-06/08 requires explicit safe effective metadata without falling back to inventory scope", () => {
    expect(
      parseApiKeyList({ items: [key] }).items[0]?.effective_services,
    ).toEqual([alpha, beta]);
    const legacy: Partial<ListedApiKey> = { ...key };
    delete legacy.effective_services;
    for (const item of [
      legacy,
      { ...key, effective_services: null },
      {
        ...key,
        effective_services: [{ ...alpha, internal_endpoint: "forbidden" }],
      },
      { ...key, api_key: "forbidden" },
    ]) {
      expect(() => parseApiKeyList({ items: [item] })).toThrow(
        ApiKeyContractError,
      );
    }
  });
});
