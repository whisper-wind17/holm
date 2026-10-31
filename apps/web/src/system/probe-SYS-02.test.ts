import { describe, expect, it } from "vitest";

import {
  parseSystemComponentsResponse,
  SystemContractError,
  type SystemComponentsResponse,
} from "../api/system-contracts";
import { DEFAULT_SYSTEM_COMPONENTS } from "../app/test-support";
import { componentImpact } from "./presentation";

describe("SYS_02 TC_HEALTH_18_19_20 probe contract and presentation", () => {
  it.each([
    ["probe_ready", "健康检查通过"],
    ["probe_unreachable", "健康检查无响应"],
    ["gateway_route_snapshot_unavailable", "路由数据未同步，暂不能接收调用"],
    ["gateway_ledger_unavailable", "调用记录暂不可写，暂不能接收调用"],
  ])("parses and presents %s", (reason, label) => {
    const payload = structuredClone(DEFAULT_SYSTEM_COMPONENTS);
    Object.assign(gateway(payload), {
      reason,
      signal_sources: ["probe"],
      status: reason === "probe_ready" ? "ready" : "unavailable",
    });
    const parsed = parseSystemComponentsResponse(payload);
    expect(gateway(parsed).signal_sources).toEqual(["probe"]);
    expect(componentImpact(gateway(parsed))).toBe(label);
  });

  it.each([
    { reason: "future_probe_reason" },
    { signal_sources: ["future_probe"] },
    { future_probe_field: true },
  ])("rejects unknown probe contract values %j", (invalid) => {
    const payload = structuredClone(DEFAULT_SYSTEM_COMPONENTS);
    Object.assign(gateway(payload), invalid);
    expect(() => parseSystemComponentsResponse(payload)).toThrow(
      SystemContractError,
    );
  });
});

function gateway(payload: SystemComponentsResponse) {
  const component = payload.components.find((item) => item.id === "Gateway");
  if (!component) throw new Error("FX-SYS-02 Gateway fixture missing");
  return component;
}
