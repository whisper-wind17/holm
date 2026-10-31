import { describe, expect, it } from "vitest";
import { ADMIN_ME, FakeApiClient } from "../app/test-support";
import { parseInstanceTelemetry } from "./telemetry-contracts";

describe("UI-09A instance window contract (FX-UI-09A synthetic counters)", () => {
  it("parses known and unknown windows and preserves unsupported errors", async () => {
    const fixture = await new FakeApiClient(ADMIN_ME).getInstanceTelemetry(
      "00000000-0000-7000-8000-000000000909",
    );
    expect(parseInstanceTelemetry(fixture).window_1h.requests).toBeNull();
    const window = {
      requests: 100,
      error_rate: null,
      ttft_p95_ms: 95,
      started_at: "2026-10-09T00:00:00Z",
    };
    const parsed = parseInstanceTelemetry({
      ...fixture,
      window_1h: window,
      current: { ...fixture.current, unsupported: ["runtime_errors_total"] },
    });
    expect(parsed.window_1h).toEqual(window);
    expect(parsed.current.unsupported).toContain("runtime_errors_total");
    expect(() =>
      parseInstanceTelemetry({ ...fixture, window_1h: undefined }),
    ).toThrow();
    expect(() =>
      parseInstanceTelemetry({
        ...fixture,
        window_1h: { ...window, requests: "100" },
      }),
    ).toThrow();
  });
});
