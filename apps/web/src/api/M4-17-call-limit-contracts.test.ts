import { describe, expect, it } from "vitest";
import {
  parseUserDetail,
  parseUserIds,
  UserContractError,
} from "./user-contracts";

const userId = "00000000-0000-4000-8000-000000004171";
const serviceId = "00000000-0000-4000-8000-000000004172";
const limits = { max_concurrent_requests: 2, max_requests_per_minute: 3 };
const grant = {
  id: serviceId,
  name: "M4-17 service",
  public_model_name: "m417",
  description: "",
  enabled: true,
};
const detail = {
  id: userId,
  username: "m417",
  display_name: "M4-17",
  role: "user",
  status: "active",
  must_change_password: false,
  last_login_at: null,
  created_at: "2026-10-19T00:00:00Z",
  api_key_count: 0,
  authorized_service_count: 1,
  api_keys: [],
  authorized_services: [{ ...grant, ...limits }],
};

describe("M4_17 TC_GW_39 existing DTOs accept the added policy fields", () => {
  it("accepts current user-detail and service-permission responses", () => {
    expect(parseUserDetail(detail).authorized_services[0]).toMatchObject(
      limits,
    );
    expect(
      parseUserIds({ user_ids: [userId], limits: { [userId]: limits } }),
    ).toEqual({ user_ids: [userId], limits: { [userId]: limits } });
  });

  it("M4_18 rejects responses missing the backend call-limit fields", () => {
    expect(() =>
      parseUserDetail({ ...detail, authorized_services: [grant] }),
    ).toThrow(UserContractError);
    expect(() => parseUserIds({ user_ids: [userId] })).toThrow(
      UserContractError,
    );
  });

  it("still rejects unknown fields and invalid limits", () => {
    for (const invalid of [
      { ...limits, max_concurrent_requests: 0 },
      { ...limits, max_requests_per_minute: 601 },
      { ...limits, prompt: "FX-CALL-LIMITS-sentinel" },
    ]) {
      expect(() =>
        parseUserIds({ user_ids: [userId], limits: { [userId]: invalid } }),
      ).toThrow(UserContractError);
    }
    expect(() =>
      parseUserDetail({
        ...detail,
        authorized_services: [
          { ...grant, ...limits, internal_endpoint: "forbidden" },
        ],
      }),
    ).toThrow(UserContractError);
  });
});
