import { describe, expect, it } from "vitest";

import { parseRequestRecordPage } from "./request-record-contracts";

const RECORD = {
  api_key_display_prefix: null,
  api_key_id: null,
  api_key_name: null,
  api_key_last4: null,
  completed_at: null,
  duration_ms: null,
  error_code: null,
  finish_reason: null,
  http_status: null,
  input_tokens: null,
  instance_id: null,
  output_tokens: null,
  public_model_name: null,
  received_at: "2026-09-20T12:00:00Z",
  request_id: "0198f58a-2586-7e37-bbf5-000000000111",
  service_id: null,
  status: "unknown",
  stream: null,
  ttft_ms: null,
  user_id: null,
  username: null,
} as const;

describe("M5_11 Request Record browser contract", () => {
  it("accepts all seven ledger states and preserves unavailable values as null", () => {
    const states = [
      "received",
      "in_progress",
      "rejected",
      "completed",
      "failed",
      "aborted",
      "unknown",
    ] as const;

    for (const status of states) {
      const parsed = parseRequestRecordPage({
        items: [{ ...RECORD, status }],
        next_cursor: null,
      });
      expect(parsed.items[0]?.status).toBe(status);
      expect(parsed.items[0]?.duration_ms).toBeNull();
    }
  });

  it("rejects body content and raw error fields outside the response allowlist", () => {
    for (const forbidden of [
      "prompt",
      "messages",
      "response",
      "raw_error",
    ] as const) {
      expect(() =>
        parseRequestRecordPage({
          items: [{ ...RECORD, [forbidden]: "M5_11_BODY_SENTINEL" }],
          next_cursor: null,
        }),
      ).toThrow();
    }
  });
});
