import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import {
  PASSWORD_POLICY_REASON_LABELS,
  PasswordPolicyContractError,
  PasswordPolicyFeedback,
  parsePasswordPolicyViolations,
  passwordStrength,
} from "./password-policy";

const COMPONENT_DIRECTORY = dirname(fileURLToPath(import.meta.url));
const SERVER_POLICY_SOURCE = resolve(
  COMPONENT_DIRECTORY,
  "../../../../packages/security/src/holm_security/password_policy.py",
);

function serverReasonCodes(
  source = readFileSync(SERVER_POLICY_SOURCE, "utf8"),
) {
  const enumBody =
    /class PasswordPolicyReason\(StrEnum\):([\s\S]*?)\n\n@dataclass/.exec(
      source,
    )?.[1];
  expect(
    enumBody,
    "server password-policy enum must be discoverable",
  ).toBeDefined();
  return [...(enumBody ?? "").matchAll(/=\s*"([a-z_]+)"/g)]
    .map((match) => match[1])
    .sort();
}

function assertExactReasonCoverage(source?: string) {
  expect(serverReasonCodes(source)).toEqual(
    Object.keys(PASSWORD_POLICY_REASON_LABELS).sort(),
  );
}

describe("TC-UI-01 password policy feedback", () => {
  it("keeps the server enum and localized client mapping exactly bidirectional", () => {
    assertExactReasonCoverage();

    const mutatedServer = readFileSync(SERVER_POLICY_SOURCE, "utf8").replace(
      '    TOO_SHORT = "too_short"',
      '    TOO_SHORT = "too_short"\n    FUTURE_RULE = "future_rule"',
    );
    expect(() => {
      assertExactReasonCoverage(mutatedServer);
    }).toThrow("expected");
  });

  it("throws a contract error for an unknown reason instead of hiding it", () => {
    expect(() =>
      parsePasswordPolicyViolations([{ code: "future_rule" }]),
    ).toThrow(PasswordPolicyContractError);
  });

  it("uses only length and dictionary status for strength", () => {
    expect(passwordStrength("abcdefghijklmno!")).toBe(
      passwordStrength("ABCDEFGHIJKLMNOP"),
    );
    expect(passwordStrength("abcdefghijklmnop")).toBe("strong");
    expect(
      passwordStrength("abcdefghijklmnop", [{ code: "common_password" }]),
    ).toBe("weak");
  });

  it("shows current length and visually emphasizes concrete failed conditions", () => {
    render(
      <PasswordPolicyFeedback
        password="letmein"
        violations={parsePasswordPolicyViolations([
          { code: "too_short", current_length: 7 },
          { code: "common_password" },
        ])}
      />,
    );
    expect(screen.getByText("至少 8 位（当前 7 位）")).toBeVisible();
    expect(screen.getByText("这个密码在常见弱密码列表中")).toBeVisible();
    expect(screen.getByRole("meter", { name: "密码强度：弱" })).toBeVisible();
    const failed = document.querySelectorAll('[data-satisfied="false"]');
    expect(failed.length).toBeGreaterThanOrEqual(2);
    expect(
      within(failed[0] as HTMLElement).getByText(/常见弱密码|至少 8 位/),
    ).toBeVisible();
  });
});
