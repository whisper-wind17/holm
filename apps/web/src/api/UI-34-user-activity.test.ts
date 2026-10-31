import { afterEach, expect, it, vi } from "vitest";
import { ApiClient } from "./client";
import { parseUserActivityPage, UserContractError } from "./user-contracts";

// FX-UI-34: public activity response, without credential storage fields.
const item = {
  id: "01900000-0000-7000-8000-000000000001",
  created_at: "2026-10-14T00:00:00Z",
  action: "api_key.change",
  operation: "revoke",
  result: "succeeded",
  reason_code: null,
  actor: {
    type: "user",
    id: "01900000-0000-7000-8000-000000000002",
    username: "admin",
    display_name: "管理员",
  },
  api_key: {
    id: "01900000-0000-7000-8000-000000000003",
    name: "CLI",
    display_prefix: "holm_sk_",
    last4: "abcd",
  },
  added_service_ids: [],
  removed_service_ids: [],
  limit_changed_service_ids: [],
};
afterEach(() => vi.restoreAllMocks());

it("UI-34 parses public metadata and legacy/null actors, rejects extra credential fields", () => {
  expect(
    parseUserActivityPage({ items: [item], next_cursor: null }).items[0],
  ).toEqual(item);
  expect(
    parseUserActivityPage({
      items: [
        {
          ...item,
          operation: null,
          api_key: null,
          actor: {
            type: "system",
            id: null,
            username: null,
            display_name: null,
          },
        },
      ],
      next_cursor: "next",
    }).next_cursor,
  ).toBe("next");
  for (const bad of [
    { ...item, details: {} },
    { ...item, api_key: { ...item.api_key, secret_hmac: "sentinel" } },
    { ...item, actor: { ...item.actor, password_hash: "sentinel" } },
    { ...item, action: "auth.logout" },
  ])
    expect(() =>
      parseUserActivityPage({ items: [bad], next_cursor: null }),
    ).toThrow(UserContractError);
});

it("UI-34 client encodes repeated actions, cursor and limit and parses the page", async () => {
  const page = { items: [item], next_cursor: "next" };
  const fetchMock = vi
    .spyOn(globalThis, "fetch")
    .mockResolvedValue(new Response(JSON.stringify(page)));
  expect(
    await new ApiClient().getUserActivity("user/id", {
      actions: ["account.change", "api_key.change"],
      cursor: "opaque+/=",
      limit: 2,
    }),
  ).toEqual(page);
  expect(fetchMock.mock.calls[0]?.[0]).toBe(
    "/api/v1/users/user%2Fid/activity?action=account.change&action=api_key.change&cursor=opaque%2B%2F%3D&limit=2",
  );
});
