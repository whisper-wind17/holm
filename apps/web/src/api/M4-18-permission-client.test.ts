import { afterEach, expect, it, vi } from "vitest";
import { ApiClient } from "./client";

// FX-M4-18: metadata-only grant IDs and CSRF fixture, no API credentials.
const id = "00000000-0000-4000-8000-000000004181";
afterEach(() => vi.restoreAllMocks());
it.each(["user", "service"] as const)(
  "M4_18 %s PUT omits empty limits and transmits explicit null",
  async (side) => {
    vi.spyOn(document, "cookie", "get").mockReturnValue(
      "__Host-pws_csrf=m418-fixture",
    );
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation(() =>
      Promise.resolve(
        new Response(
          JSON.stringify(
            side === "user"
              ? { service_ids: [id] }
              : {
                  user_ids: [id],
                  limits: {
                    [id]: {
                      max_concurrent_requests: null,
                      max_requests_per_minute: 10,
                    },
                  },
                },
          ),
        ),
      ),
    );
    const client = new ApiClient();
    const call =
      side === "user"
        ? client.replaceUserPermissions.bind(client)
        : client.replaceServicePermissions.bind(client);
    await call(id, [id], {});
    const field = side === "user" ? "service_ids" : "user_ids";
    expect(JSON.parse(fetchMock.mock.calls[0]?.[1]?.body as string)).toEqual({
      [field]: [id],
    });
    const limits = {
      [id]: { max_concurrent_requests: null, max_requests_per_minute: 10 },
    };
    await call(id, [id], limits);
    expect(JSON.parse(fetchMock.mock.calls[1]?.[1]?.body as string)).toEqual({
      [field]: [id],
      limits,
    });
  },
);
