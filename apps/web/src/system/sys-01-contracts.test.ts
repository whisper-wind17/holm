import { describe, expect, it } from "vitest";

import {
  parseSystemComponentsResponse,
  SystemContractError,
} from "../api/system-contracts";
import { DEFAULT_SYSTEM_COMPONENTS } from "../app/test-support";

// FX-SYS-01 extends the shared M1-09A system response fixture.
describe("SYS_01 TC_HEALTH_15_18_19 component contract", () => {
  it("preserves platform/schema versions and measured ledger values", () => {
    const response = {
      ...DEFAULT_SYSTEM_COMPONENTS,
      components: DEFAULT_SYSTEM_COMPONENTS.components.map((component) => ({
        ...component,
        details:
          component.id === "Database"
            ? {
                kind: "database",
                schema_revision: { state: "known", value: "20261014_0040" },
              }
            : component.id === "Request Ledger Writer"
              ? {
                  kind: "request_ledger_writer",
                  pending_event_count: { state: "known", value: 3 },
                  oldest_event_at: {
                    state: "known",
                    value: "2026-10-15T00:00:00Z",
                  },
                }
              : component.details,
      })),
    };
    expect(parseSystemComponentsResponse(response)).toEqual(response);
  });

  it.each(["Future Agent", "vLLM Adapter", "SGLang Adapter"])(
    "rejects unknown or removed component %s",
    (id) => {
      expect(() =>
        parseSystemComponentsResponse({
          ...DEFAULT_SYSTEM_COMPONENTS,
          components: [{ ...DEFAULT_SYSTEM_COMPONENTS.components[0], id }],
        }),
      ).toThrow(SystemContractError);
    },
  );

  it("requires the platform version", () => {
    const response: Record<string, unknown> = { ...DEFAULT_SYSTEM_COMPONENTS };
    delete response.platform;
    expect(() => parseSystemComponentsResponse(response)).toThrow(
      SystemContractError,
    );
  });
});
