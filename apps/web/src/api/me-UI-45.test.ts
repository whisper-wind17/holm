import { afterEach, expect, it, vi } from "vitest";
import { ApiClient } from "./client";
import {
  parsePersonalAiServiceList,
  parsePersonalProfile,
  UserContractError,
} from "./user-contracts";

// FX-UI-45: metadata-only personal contracts for UI-45.
const profile = {
  username: "alice",
  display_name: "Alice",
  role: "user",
  current_login_at: "2026-10-16T10:00:00Z",
  previous_login_at: null,
  created_at: "2026-10-01T10:00:00Z",
  authorized_service_count: 1,
};
const service = {
  id: "45000000-0000-4000-8000-000000000001",
  name: "Assistant",
  public_model_name: "assistant",
  description: "",
  available: false,
  enabled: false,
  last_used_at: "2026-10-16T09:00:00Z",
  last_succeeded_at: null,
};

afterEach(() => vi.restoreAllMocks());

it("UI_46 UI_45 parses personal timestamps and rejects unknown fields or missing new fields", () => {
  expect(parsePersonalAiServiceList({ items: [service] })).toEqual({
    items: [service],
  });
  expect(parsePersonalProfile(profile)).toEqual(profile);
  expect(
    parsePersonalProfile({ ...profile, previous_login_at: profile.created_at }),
  ).toMatchObject({
    previous_login_at: profile.created_at,
  });
  for (const extra of [{ busy: false }, { instance_id: service.id }]) {
    expect(() =>
      parsePersonalAiServiceList({ items: [{ ...service, ...extra }] }),
    ).toThrow(UserContractError);
  }
  for (const field of [
    "id",
    "enabled",
    "last_used_at",
    "last_succeeded_at",
  ] as const) {
    const incomplete = { ...service, [field]: undefined };
    expect(() => parsePersonalAiServiceList({ items: [incomplete] })).toThrow(
      UserContractError,
    );
  }
  expect(() =>
    parsePersonalProfile({ ...profile, password_hash: "forbidden" }),
  ).toThrow(UserContractError);
});

it("UI_45 client reads and updates personal profile with CSRF and validates responses", async () => {
  vi.spyOn(document, "cookie", "get").mockReturnValue(
    "__Host-pws_csrf=fixture-csrf",
  );
  const fetchMock = vi
    .spyOn(globalThis, "fetch")
    .mockImplementation(() =>
      Promise.resolve(new Response(JSON.stringify(profile))),
    );
  const client = new ApiClient();
  expect(await client.getMyProfile()).toEqual(profile);
  expect(fetchMock.mock.calls[0]?.[0]).toBe("/api/v1/me/profile");
  expect(await client.updateMyProfile({ display_name: "Alice" })).toEqual(
    profile,
  );
  const [url, request] = fetchMock.mock.calls[1] ?? [];
  expect(url).toBe("/api/v1/me/profile");
  expect(request?.method).toBe("PATCH");
  expect(JSON.parse(request?.body as string)).toEqual({
    display_name: "Alice",
  });
  expect(new Headers(request?.headers).get("X-CSRF-Token")).toBe(
    "fixture-csrf",
  );
});
