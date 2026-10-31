import { describe, expect, it, vi } from "vitest";

import { ApiClient } from "./client";
import { parseRequestRecordErrorGroups } from "./request-record-contracts";

// FX-UI-19: metadata-only PDD 9.6 error summary, matching the SQL integration fixture.
const GROUP = {
  status: "failed",
  error_code: "backend_unavailable",
  count: 300,
  first_at: "2026-10-13T00:00:00Z",
  last_at: "2026-10-13T00:04:58Z",
  latest_request_id: "0198f58a-2586-7e37-bbf5-000000000111",
};

describe("UI-19 Request Record contracts", () => {
  it("preserves full-window counts and the unknown null error code", () => {
    const result = parseRequestRecordErrorGroups({
      items: [GROUP, { ...GROUP, status: "unknown", error_code: null }],
    });
    expect(result.items[0]?.count).toBe(300);
    expect(result.items[1]?.error_code).toBeNull();
    expect(parseRequestRecordErrorGroups({ items: [] })).toEqual({ items: [] });
  });

  it("rejects invalid summary fields and body or raw error additions", () => {
    for (const overrides of [
      { status: "completed" },
      { error_code: "raw-runtime-error" },
      { count: 0 },
      { count: 1.5 },
      { first_at: "yesterday" },
      { latest_request_id: "invalid" },
      ...["prompt", "messages", "response", "raw_error"].map((field) => ({
        [field]: "UI-19-body-sentinel",
      })),
    ]) {
      expect(() =>
        parseRequestRecordErrorGroups({ items: [{ ...GROUP, ...overrides }] }),
      ).toThrow();
    }
    expect(() =>
      parseRequestRecordErrorGroups({ items: Array(6).fill(GROUP) }),
    ).toThrow();
    expect(() =>
      parseRequestRecordErrorGroups({ items: [], prompt: "sentinel" }),
    ).toThrow();
  });

  it("encodes new and existing list filters together", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(
        new Response(JSON.stringify({ items: [], next_cursor: null })),
      );
    await new ApiClient().getRequestRecords({
      apiKeyId: GROUP.latest_request_id,
      errorCode: "backend_unavailable",
      from: "2026-10-13T08:00:00+08:00",
      to: "2026-10-13T09:00:00+08:00",
      cursor: "opaque+/=",
      requestId: GROUP.latest_request_id,
      serviceId: GROUP.latest_request_id,
      status: "failed",
      userId: GROUP.latest_request_id,
    });
    const [path] = fetchMock.mock.calls[0] ?? [];
    if (typeof path !== "string") throw new Error("Expected a request path");
    const url = new URL(path, "https://workspace.test");
    expect(url.pathname).toBe("/api/v1/requests");
    expect(Object.fromEntries(url.searchParams)).toEqual({
      api_key_id: GROUP.latest_request_id,
      error_code: "backend_unavailable",
      from: "2026-10-13T08:00:00+08:00",
      to: "2026-10-13T09:00:00+08:00",
      cursor: "opaque+/=",
      request_id: GROUP.latest_request_id,
      service_id: GROUP.latest_request_id,
      status: "failed",
      user_id: GROUP.latest_request_id,
    });
  });

  it("requests error groups with the required window and optional filters", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(new Response(JSON.stringify({ items: [GROUP] })));
    const result = await new ApiClient().getRequestRecordErrorGroups({
      from: GROUP.first_at,
      to: GROUP.last_at,
      serviceId: GROUP.latest_request_id,
    });
    expect(result.items).toEqual([GROUP]);
    const [path] = fetchMock.mock.calls[0] ?? [];
    if (typeof path !== "string") throw new Error("Expected a request path");
    const url = new URL(path, "https://workspace.test");
    expect(url.pathname).toBe("/api/v1/requests/error-groups");
    expect(Object.fromEntries(url.searchParams)).toEqual({
      from: GROUP.first_at,
      to: GROUP.last_at,
      service_id: GROUP.latest_request_id,
    });
  });
});
