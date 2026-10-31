import { Icon } from "./icon";

export const MINIMUM_PASSWORD_CHARACTERS = 8;

export const PASSWORD_POLICY_REASON_LABELS = {
  common_password: "这个密码在常见弱密码列表中",
  not_printable: "只使用可打印字符",
  similar_to_display_name: "不与显示名称相似",
  similar_to_username: "不与用户名相似",
  too_long: "不超过 128 位",
  too_short: `至少 ${String(MINIMUM_PASSWORD_CHARACTERS)} 位`,
} as const;

export type PasswordPolicyReasonCode =
  keyof typeof PASSWORD_POLICY_REASON_LABELS;

export interface PasswordPolicyViolation {
  code: PasswordPolicyReasonCode;
  current_length?: number;
}

export class PasswordPolicyContractError extends Error {
  readonly code = "password_policy_contract_error";

  constructor() {
    super("server returned an unsupported password policy reason");
    this.name = "PasswordPolicyContractError";
  }
}

const POLICY_REASON_ORDER = Object.keys(
  PASSWORD_POLICY_REASON_LABELS,
) as PasswordPolicyReasonCode[];

export function parsePasswordPolicyViolations(
  value: unknown,
): PasswordPolicyViolation[] {
  if (!Array.isArray(value)) {
    throw new PasswordPolicyContractError();
  }
  return value.map((item) => {
    if (typeof item !== "object" || item === null || Array.isArray(item)) {
      throw new PasswordPolicyContractError();
    }
    const record = item as Record<string, unknown>;
    const keys = Object.keys(record);
    if (
      keys.some((key) => key !== "code" && key !== "current_length") ||
      typeof record.code !== "string" ||
      !(record.code in PASSWORD_POLICY_REASON_LABELS) ||
      (record.current_length !== undefined &&
        (!Number.isInteger(record.current_length) ||
          (record.current_length as number) < 0))
    ) {
      throw new PasswordPolicyContractError();
    }
    const code = record.code as PasswordPolicyReasonCode;
    const currentLength = record.current_length as number | undefined;
    if (
      (code === "too_short" || code === "too_long") !==
      (currentLength !== undefined)
    ) {
      throw new PasswordPolicyContractError();
    }
    return {
      code,
      ...(currentLength === undefined ? {} : { current_length: currentLength }),
    };
  });
}

export function passwordStrength(
  password: string,
  violations: readonly PasswordPolicyViolation[] = [],
) {
  const length = Array.from(password).length;
  if (
    length < MINIMUM_PASSWORD_CHARACTERS ||
    length > 128 ||
    violations.some(({ code }) => code === "common_password")
  ) {
    return "weak" as const;
  }
  return length < 16 ? ("fair" as const) : ("strong" as const);
}

function containsControlCharacter(value: string) {
  return Array.from(value).some((character) => {
    const codePoint = character.codePointAt(0);
    return (
      codePoint !== undefined &&
      (codePoint <= 31 || (codePoint >= 127 && codePoint <= 159))
    );
  });
}

export function PasswordPolicyFeedback({
  password,
  violations = [],
}: {
  password: string;
  violations?: readonly PasswordPolicyViolation[];
}) {
  const currentLength = Array.from(password).length;
  const serverViolations = new Map(
    violations.map((violation) => [violation.code, violation]),
  );
  const locallyFailed = new Set<PasswordPolicyReasonCode>();
  if (currentLength < MINIMUM_PASSWORD_CHARACTERS)
    locallyFailed.add("too_short");
  if (currentLength > 128) locallyFailed.add("too_long");
  if (containsControlCharacter(password)) locallyFailed.add("not_printable");
  const strength = passwordStrength(password, violations);
  const strengthLabel = { fair: "一般", strong: "强", weak: "弱" }[strength];

  return (
    <section aria-label="密码安全要求" className="pw-password-policy">
      <div className="pw-password-strength">
        <span>密码强度</span>
        <strong>{strengthLabel}</strong>
      </div>
      <div
        aria-label={`密码强度：${strengthLabel}`}
        aria-valuemax={3}
        aria-valuemin={1}
        aria-valuenow={{ fair: 2, strong: 3, weak: 1 }[strength]}
        className="pw-password-strength__bar"
        data-strength={strength}
        role="meter"
      >
        <span />
        <span />
        <span />
      </div>
      <ul className="pw-password-conditions">
        {POLICY_REASON_ORDER.map((code) => {
          const serverViolation = serverViolations.get(code);
          const failed =
            serverViolation !== undefined || locallyFailed.has(code);
          const length = serverViolation?.current_length ?? currentLength;
          const label =
            code === "too_short"
              ? `至少 ${String(MINIMUM_PASSWORD_CHARACTERS)} 位（当前 ${String(length)} 位）`
              : code === "too_long"
                ? `不超过 128 位（当前 ${String(length)} 位）`
                : PASSWORD_POLICY_REASON_LABELS[code];
          return (
            <li data-satisfied={!failed} key={code}>
              <Icon name={failed ? "error" : "check"} size="xs" />
              <span>{label}</span>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
