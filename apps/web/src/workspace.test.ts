import { describe, expect, it } from "vitest";

import { WEB_WORKSPACE_NAME } from "./workspace";

describe("ENG_M0_01 web workspace", () => {
  it("keeps the minimal TypeScript module importable", () => {
    expect(WEB_WORKSPACE_NAME).toBe("@holm/web");
  });
});
