import { describe, expect, it } from "vitest";

import {
  assertLocalAvailabilityPresentations,
  LOCAL_AVAILABILITY_PRESENTATIONS,
  localAvailabilityPresentation,
} from "./presentation";

describe("M2_13B local availability presentation mapping", () => {
  it("covers the server vocabulary exactly", () => {
    expect(() => {
      assertLocalAvailabilityPresentations();
    }).not.toThrow();
  });

  it("positive control fails when one mapping is deleted", () => {
    const mapping = LOCAL_AVAILABILITY_PRESENTATIONS as Record<string, unknown>;
    const saved = mapping.absent;
    delete mapping.absent;
    try {
      expect(() => {
        assertLocalAvailabilityPresentations();
      }).toThrow();
    } finally {
      mapping.absent = saved;
    }
  });

  it("positive control rejects an unknown enum value", () => {
    expect(() => localAvailabilityPresentation("future_state")).toThrow();
  });
});
