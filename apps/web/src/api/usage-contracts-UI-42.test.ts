import { describe, expect, it } from "vitest";

import { parseUsageReport, UsageContractError } from "./usage-contracts";

// FX-UI-42: synthetic response matching UsageGroupResponse and TC-UI-17.
const totals = {
  error_rate: 0,
  error_requests: 0,
  input_tokens: 4,
  output_tokens: 1,
  partial_token_requests: 0,
  requests: 1,
  total_tokens: 5,
};
const group = {
  ...totals,
  display_name: "sk-••••1234",
  group_id: "13000000-0000-4000-8000-000000000001",
  is_deleted: false,
  status: "Active",
};
const report = (fields: object) => ({
  bucket_width: "hour",
  details: [{ ...group, ...fields }],
  from: "2026-10-15T00:00:00Z",
  to: "2026-10-15T01:00:00Z",
  summary: totals,
  trend: [],
  tz: "UTC",
});

describe("UI-42 TC-UI-17 usage group contract", () => {
  it.each([
    {},
    { api_key_name: null, owner_username: null, owner_is_deleted: null },
    { api_key_name: "key", owner_username: "member", owner_is_deleted: false },
    { api_key_name: "key", owner_username: "member", owner_is_deleted: true },
  ])("accepts new metadata and existing fixtures: %j", (fields) => {
    expect(parseUsageReport(report(fields)).details).toEqual([
      { ...group, ...fields },
    ]);
  });

  it.each([
    { api_key_name: 123 },
    { owner_username: false },
    { owner_is_deleted: "false" },
    { secret_hmac: "not-allowed" },
    { lookup_id: "not-allowed" },
    { pepper_key_id: "not-allowed" },
    { plaintext: "not-allowed" },
  ])("rejects invalid metadata or secret fields: %j", (fields) => {
    expect(() => parseUsageReport(report(fields))).toThrow(UsageContractError);
  });
});
