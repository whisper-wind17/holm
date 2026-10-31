import axe from "axe-core";
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ApiClient, ApiError } from "../api/client";
import {
  parseApiKey,
  parseCreateApiKeyRequest,
  parseRenameApiKeyRequest,
  type ApiKey,
} from "../api/api-key-contracts";
import { FakeApiClient, USER_ME, renderApplication } from "../app/test-support";

// FX-M4-03A: safe display metadata based on PROTO-07; no plaintext fixture needed.
const key: ApiKey = {
  created_at: "2026-09-01T00:00:00Z",
  display_prefix: "holm_sk_",
  expires_at: null,
  id: "0198f58a-2586-7e37-bbf5-fefe165e8401",
  last4: "1234",
  last_used_at: null,
  name: "开发环境",
  revoked_at: null,
  service_ids: [],
  status: "Active",
};

function clientWithKey(status: ApiKey["status"] = "Active") {
  const client = new FakeApiClient(USER_ME);
  const item = { ...key, status, effective_services: [] };
  const list = vi
    .spyOn(client, "getApiKeys")
    .mockResolvedValue({ items: [item] });
  return { client, item, list };
}

async function assertAccessible() {
  const result = await axe.run(document, {
    rules: { "color-contrast": { enabled: false } },
  });
  expect(result.passes.length).toBeGreaterThan(0);
  expect(
    result.violations.filter(
      (v) => v.impact === "critical" || v.impact === "serious",
    ),
  ).toEqual([]);
}

describe("M4-03A API key lifecycle", () => {
  it("M4-03A TC-UI-06 validates, keeps failed edits, restores focus, and refreshes only safe metadata", async () => {
    const { client, list } = clientWithKey();
    const rename = vi
      .spyOn(client, "renameApiKey")
      .mockRejectedValueOnce(
        new ApiError({
          code: "unavailable",
          status: 503,
          responseMessage: "保存失败，请重试。",
        }),
      )
      .mockResolvedValue({ ...key, name: "新名称" });
    const remove = vi.spyOn(client, "deleteApiKey");
    const { queryClient } = renderApplication(client, "/api-keys");
    const user = userEvent.setup();
    const trigger = await screen.findByRole("button", { name: "重命名" });
    await user.click(trigger);
    const dialog = screen.getByRole("dialog", { name: "重命名密钥" });
    const input = within(dialog).getByLabelText(/^名称/);
    await user.clear(input);
    await user.type(input, " ");
    await user.click(within(dialog).getByRole("button", { name: "保存名称" }));
    await waitFor(() => expect(input).toHaveFocus());
    expect(rename).not.toHaveBeenCalled();
    expect(remove).not.toHaveBeenCalled();
    await user.clear(input);
    await user.type(input, "新名称");
    await user.click(within(dialog).getByRole("button", { name: "保存名称" }));
    expect(await within(dialog).findByText("保存失败，请重试。")).toBeVisible();
    expect(input).toHaveValue("新名称");
    await assertAccessible();
    list.mockResolvedValue({
      items: [{ ...key, name: "新名称", effective_services: [] }],
    });
    await user.click(within(dialog).getByRole("button", { name: "保存名称" }));
    await waitFor(() =>
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
    );
    expect(await screen.findByText("新名称")).toBeVisible();
    expect(rename).toHaveBeenLastCalledWith(key.id, { name: "新名称" });
    expect(trigger).toHaveFocus();
    expect(
      JSON.stringify(
        queryClient
          .getQueryCache()
          .getAll()
          .map((q) => q.state.data),
      ),
    ).not.toContain('"api_key"');
    expect(queryClient.getMutationCache().getAll()).toEqual([]);
  });

  it.each(["Expired", "Revoked"] as const)(
    "M4-03A %s removal requires confirmation and retains failed dialog, then hides row",
    async (status) => {
      const { client, item, list } = clientWithKey(status);
      const remove = vi
        .spyOn(client, "deleteApiKey")
        .mockRejectedValueOnce(
          new ApiError({ code: "unavailable", status: 503 }),
        )
        .mockResolvedValue(item);
      renderApplication(client, "/api-keys");
      const user = userEvent.setup();
      await user.click(await screen.findByRole("button", { name: "删除记录" }));
      let dialog = screen.getByRole("dialog");
      expect(
        within(dialog).getByText(
          "仅从列表移除，历史用量保留。密钥不会恢复有效。",
        ),
      ).toBeVisible();
      await assertAccessible();
      await user.click(within(dialog).getByRole("button", { name: "取消" }));
      expect(remove).not.toHaveBeenCalled();
      await user.click(screen.getByRole("button", { name: "删除记录" }));
      dialog = screen.getByRole("dialog");
      await user.click(
        within(dialog).getByRole("button", { name: "删除记录" }),
      );
      expect(await within(dialog).findByText("删除未完成")).toBeVisible();
      list.mockResolvedValue({ items: [] });
      await user.click(
        within(dialog).getByRole("button", { name: "删除记录" }),
      );
      expect(await screen.findByText("还没有 API 密钥")).toBeVisible();
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
      expect(screen.getByRole("heading", { name: "API 密钥" })).toHaveFocus();
    },
  );

  it("M4-03A blocks duplicate pending submits and active deletion, supports cancel with Escape", async () => {
    const { client } = clientWithKey();
    let complete: ((key: ApiKey) => void) | undefined;
    const rename = vi.spyOn(client, "renameApiKey").mockImplementation(
      () =>
        new Promise((resolve) => {
          complete = resolve;
        }),
    );
    renderApplication(client, "/api-keys");
    const user = userEvent.setup();
    const trigger = await screen.findByRole("button", { name: "重命名" });
    expect(
      screen.queryByRole("button", { name: "删除记录" }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "撤销密钥" })).toBeVisible();
    await user.click(trigger);
    await user.keyboard("{Escape}");
    expect(trigger).toHaveFocus();
    expect(rename).not.toHaveBeenCalled();
    await user.click(trigger);
    const dialog = screen.getByRole("dialog");
    const submit = within(dialog).getByRole("button", { name: "保存名称" });
    await user.click(submit);
    expect(submit).toBeDisabled();
    const form = submit.closest("form");
    if (form === null) throw new Error("expected rename form");
    fireEvent.submit(form);
    await user.keyboard("{Escape}");
    expect(dialog).toBeVisible();
    expect(rename).toHaveBeenCalledTimes(1);
    complete?.(key);
    await waitFor(() =>
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
    );
  });

  it("M4-03A new writes reject unsupported names/fields while legacy reads remain compatible", () => {
    for (const name of ["", "x", "  ", " ab", "ab ", "a".repeat(41)]) {
      expect(() => parseCreateApiKeyRequest({ name })).toThrow();
      expect(() => parseRenameApiKeyRequest({ name })).toThrow();
    }
    for (const name of ["ab", "字".repeat(40)])
      expect(parseRenameApiKeyRequest({ name })).toEqual({ name });
    expect(() =>
      parseRenameApiKeyRequest({ name: "valid", status: "Active" }),
    ).toThrow();
    for (const name of ["x", "a".repeat(128)])
      expect(parseApiKey({ ...key, name }).name).toBe(name);
  });
});

it("M4-03A transports PATCH/DELETE with CSRF and only safe metadata", async () => {
  document.cookie =
    "__Host-pws_csrf=m403a-csrf; Path=/; Secure; SameSite=Strict";
  const fetchCall = vi.spyOn(globalThis, "fetch").mockImplementation(() =>
    Promise.resolve(
      new Response(JSON.stringify(key), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    ),
  );
  try {
    const client = new ApiClient();
    await client.renameApiKey(key.id, { name: "renamed" });
    await client.deleteApiKey(key.id);
    expect(fetchCall).toHaveBeenCalledTimes(2);
    for (const [index, method] of ["PATCH", "DELETE"].entries()) {
      const path: unknown = fetchCall.mock.calls[index]?.[0];
      const options: RequestInit | undefined = fetchCall.mock.calls[index]?.[1];
      expect(path).toBe(`/api/v1/me/api-keys/${key.id}`);
      expect(options?.method).toBe(method);
      expect(new Headers(options?.headers).get("X-CSRF-Token")).toBe(
        "m403a-csrf",
      );
    }
    expect(fetchCall.mock.calls[0]?.[1]?.body).toBe(
      JSON.stringify({ name: "renamed" }),
    );
    expect(fetchCall.mock.calls[1]?.[1]?.body).toBeUndefined();
  } finally {
    fetchCall.mockRestore();
    document.cookie = "__Host-pws_csrf=; Max-Age=0; Path=/";
  }
});
