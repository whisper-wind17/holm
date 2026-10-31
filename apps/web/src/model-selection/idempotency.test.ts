import { describe, expect, it } from "vitest";

import { ModelWriteIntentKeys } from "./idempotency";

const INTENT = {
  action: "download",
  repositoryId: "Qwen/Qwen3",
  source: "Hugging Face",
} as const;

describe("M2_13B idempotency intent lifecycle", () => {
  it("reuses the random key while an attempt has no determinate result", () => {
    const keys = new ModelWriteIntentKeys(sequence("random-a", "random-b"));
    const first = keys.acquire(INTENT);
    keys.settle(first, false);
    expect(keys.acquire(INTENT)).toBe(first);
  });

  it("uses a new random key after success for the same payload", () => {
    const keys = new ModelWriteIntentKeys(sequence("random-a", "random-b"));
    const first = keys.acquire(INTENT);
    keys.settle(first, true);
    expect(keys.acquire(INTENT)).toBe("random-b");
  });

  it("positive control catches replacing the key after a network failure", () => {
    const keys = new ModelWriteIntentKeys(sequence("random-a", "random-b"));
    const first = keys.acquire(INTENT);
    keys.settle(first, false);
    expect(keys.acquire(INTENT)).not.toBe("random-b");
  });

  it("positive control catches payload-derived reuse after a successful intent", () => {
    const keys = new ModelWriteIntentKeys(sequence("random-a", "random-b"));
    const first = keys.acquire(INTENT);
    keys.settle(first, true);
    expect(keys.acquire(INTENT)).not.toBe(first);
  });
});

function sequence(...values: string[]) {
  return () => {
    const value = values.shift();
    if (value === undefined) throw new Error("random key fixture exhausted");
    return value;
  };
}
