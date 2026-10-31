import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import {
  ACCOUNT_NAME_MAX_LENGTH,
  ACCOUNT_NAME_MIN_LENGTH,
  ACCOUNT_NAME_PATTERN,
  BOOTSTRAP_TOKEN_HEADER,
  BootstrapContractError,
  DISPLAY_NAME_MAX_LENGTH,
  parseBootstrapBlocking,
  parseBootstrapCheckReport,
  parseBootstrapStatusResponse,
  parseBootstrapUsernameReason,
} from "../api/bootstrap-contracts";
import { PASSING_BOOTSTRAP_REPORT } from "../app/test-support";
import {
  BOOTSTRAP_CLIENT_ENUMS,
  BOOTSTRAP_PRESENTATION_KEYS,
  bootstrapErrorMessage,
  checkLabel,
  checkReasonPresentation,
  hostCheckReasonPresentation,
  outcomePresentation,
  usernameReasonMessage,
} from "./presentation";

const SETUP_DIRECTORY = dirname(fileURLToPath(import.meta.url));
const REPOSITORY_ROOT = resolve(SETUP_DIRECTORY, "../../../..");
const CHECKS_SOURCE = resolve(
  REPOSITORY_ROOT,
  "apps/control_api/src/holm_control_api/bootstrap_checks.py",
);
const SERVICE_SOURCE = resolve(
  REPOSITORY_ROOT,
  "apps/control_api/src/holm_control_api/bootstrap_service.py",
);
const ROUTES_SOURCE = resolve(
  REPOSITORY_ROOT,
  "apps/control_api/src/holm_control_api/bootstrap_routes.py",
);
const USERNAMES_SOURCE = resolve(
  REPOSITORY_ROOT,
  "packages/common/src/holm_common/usernames.py",
);

function readChecksSource() {
  return readFileSync(CHECKS_SOURCE, "utf8");
}

/** Values of one server `StrEnum`, read from the module that defines it. */
function pythonEnumValues(source: string, name: string) {
  const body = new RegExp(
    `class ${name}\\(StrEnum\\):([\\s\\S]*?)\\n\\n\\n(?:class |@|BOOTSTRAP)`,
  ).exec(source)?.[1];
  expect(body, `server enum ${name} must be discoverable`).toBeDefined();
  const values = [...(body ?? "").matchAll(/=\s*"([a-z_]+)"/g)].map(
    (match) => match[1] ?? "",
  );
  expect(
    values.length,
    `server enum ${name} must not read as empty`,
  ).toBeGreaterThan(0);
  return [...new Set(values)].sort();
}

function serverUsernameReasons(
  source = readFileSync(USERNAMES_SOURCE, "utf8"),
) {
  const codes = [...source.matchAll(/_fail\("([a-z_]+)"\)/g)].map(
    (match) => match[1] ?? "",
  );
  expect(
    codes.length,
    "server username reasons must be discoverable",
  ).toBeGreaterThan(0);
  return [...new Set(codes)].sort();
}

function serverErrorCodes() {
  const service = readFileSync(SERVICE_SOURCE, "utf8");
  const routes = readFileSync(ROUTES_SOURCE, "utf8");
  const declared = [
    ...service.matchAll(/^BOOTSTRAP_[A-Z_]*CODE: Final\[str\] = "([a-z_]+)"/gm),
  ].map((match) => match[1] ?? "");
  const inline = [...routes.matchAll(/\n\s+code="([a-z_]+)",/g)].map(
    (match) => match[1] ?? "",
  );
  expect(
    declared.length,
    "server error codes must be discoverable",
  ).toBeGreaterThan(0);
  return [...new Set([...declared, ...inline])].sort();
}

function pythonIntConstant(source: string, name: string) {
  const value = new RegExp(`${name}: Final\\[int\\] = (\\d+)`).exec(
    source,
  )?.[1];
  expect(value, `server constant ${name} must be discoverable`).toBeDefined();
  return Number(value);
}

function sorted(values: readonly string[]) {
  return [...values].sort();
}

describe("TC-UI-21 bootstrap enums are the server's, in both directions", () => {
  it.each([
    ["BootstrapCheckId", "checkIds"],
    ["BootstrapCheckOutcome", "outcomes"],
    ["BootstrapCheckReason", "reasons"],
  ] as const)(
    "keeps %s equal to the client enum and to the localized table",
    (enumName, key) => {
      const server = pythonEnumValues(readChecksSource(), enumName);
      expect(sorted(BOOTSTRAP_CLIENT_ENUMS[key])).toEqual(server);
      expect(sorted(BOOTSTRAP_PRESENTATION_KEYS[key])).toEqual(server);
    },
  );

  it("positive control: a value added on the server fails the check", () => {
    const mutated = readChecksSource().replace(
      '    GPU = "gpu"',
      '    GPU = "gpu"\n    NPU = "npu"',
    );
    expect(sorted(BOOTSTRAP_CLIENT_ENUMS.checkIds)).not.toEqual(
      pythonEnumValues(mutated, "BootstrapCheckId"),
    );
  });

  it("positive control: a value dropped from the client fails the check", () => {
    const server = pythonEnumValues(readChecksSource(), "BootstrapCheckReason");
    const shortened = sorted(BOOTSTRAP_CLIENT_ENUMS.reasons).filter(
      (reason) => reason !== "gpu_no_devices",
    );
    expect(shortened).not.toEqual(server);
  });

  it("keeps the username refusal codes equal to the server's", () => {
    expect(sorted(BOOTSTRAP_CLIENT_ENUMS.usernameReasons)).toEqual(
      serverUsernameReasons(),
    );
    expect(sorted(BOOTSTRAP_PRESENTATION_KEYS.usernameReasons)).toEqual(
      serverUsernameReasons(),
    );
  });

  it("positive control: a username code added on the server fails the check", () => {
    const mutated = readFileSync(USERNAMES_SOURCE, "utf8").replace(
      '_fail("whitespace")',
      '_fail("whitespace")\n    _fail("reserved_name")',
    );
    expect(sorted(BOOTSTRAP_CLIENT_ENUMS.usernameReasons)).not.toEqual(
      serverUsernameReasons(mutated),
    );
  });

  it("keeps the bootstrap error codes equal to the server's", () => {
    expect(sorted(BOOTSTRAP_CLIENT_ENUMS.errorCodes)).toEqual(
      serverErrorCodes(),
    );
    expect(sorted(BOOTSTRAP_PRESENTATION_KEYS.errorCodes)).toEqual(
      serverErrorCodes(),
    );
  });

  it("carries the Token in the header the server reads", () => {
    expect(readFileSync(SERVICE_SOURCE, "utf8")).toContain(
      `BOOTSTRAP_TOKEN_HEADER: Final[str] = "${BOOTSTRAP_TOKEN_HEADER}"`,
    );
  });

  it("keeps the PDD 8.1 account-name rule equal to the one server definition", () => {
    const source = readFileSync(USERNAMES_SOURCE, "utf8");
    expect(ACCOUNT_NAME_MIN_LENGTH).toBe(
      pythonIntConstant(source, "ACCOUNT_NAME_MIN_LENGTH"),
    );
    expect(ACCOUNT_NAME_MAX_LENGTH).toBe(
      pythonIntConstant(source, "ACCOUNT_NAME_MAX_LENGTH"),
    );
    expect(source).toContain(`re.compile(r"${ACCOUNT_NAME_PATTERN.source}")`);
    // PDD 8.1 line 689 and PDD 8.4 line 816 cap the display name at 80; the
    // server's own bound is looser, so the client stays inside it and never
    // accepts a name the server would reject.
    expect(DISPLAY_NAME_MAX_LENGTH).toBeLessThanOrEqual(
      pythonIntConstant(source, "DISPLAY_NAME_MAX_LENGTH"),
    );
    expect(DISPLAY_NAME_MAX_LENGTH).toBe(80);
  });
});

describe("TC-UI-21 unknown bootstrap identifiers fail loudly", () => {
  it.each([
    ["check id", () => checkLabel("npu")],
    ["outcome", () => outcomePresentation("skipped")],
    ["reason", () => checkReasonPresentation("future_reason")],
    ["username reason", () => usernameReasonMessage("reserved_name")],
    ["error code", () => bootstrapErrorMessage("teapot")],
  ])("throws a contract error for an unknown %s", (_label, act) => {
    expect(act).toThrow(BootstrapContractError);
  });

  it.each([
    [
      "unknown card id",
      {
        checks: [
          { id: "npu", outcome: "passed", reasons: ["gpu_devices_present"] },
        ],
      },
    ],
    [
      "unknown outcome",
      {
        checks: [
          { id: "gpu", outcome: "skipped", reasons: ["gpu_devices_present"] },
        ],
      },
    ],
    [
      "unknown reason",
      {
        checks: [{ id: "gpu", outcome: "passed", reasons: ["future_reason"] }],
      },
    ],
  ])("rejects a report carrying an %s", (_label, patch) => {
    expect(() =>
      parseBootstrapCheckReport({ ...PASSING_BOOTSTRAP_REPORT, ...patch }),
    ).toThrow(BootstrapContractError);
  });

  it("rejects a report that is missing one of the seven cards", () => {
    expect(() =>
      parseBootstrapCheckReport({
        ...PASSING_BOOTSTRAP_REPORT,
        checks: PASSING_BOOTSTRAP_REPORT.checks.slice(1),
      }),
    ).toThrow(BootstrapContractError);
  });

  it("accepts the full report unchanged", () => {
    expect(parseBootstrapCheckReport(PASSING_BOOTSTRAP_REPORT)).toEqual(
      PASSING_BOOTSTRAP_REPORT,
    );
  });

  it("rejects an extra field on the status response", () => {
    expect(() =>
      parseBootstrapStatusResponse({
        initialized: false,
        internal_note: "must fail",
        token_expires_at: "2026-09-02T03:50:00Z",
      }),
    ).toThrow(BootstrapContractError);
  });

  it("rejects an unknown blocking identifier instead of dropping it", () => {
    expect(() => parseBootstrapBlocking(["gpu", "npu"])).toThrow(
      BootstrapContractError,
    );
    expect(() => parseBootstrapBlocking([])).toThrow(BootstrapContractError);
    expect(parseBootstrapBlocking(["docker_compose"])).toEqual([
      "docker_compose",
    ]);
  });

  it("rejects an unknown username reason instead of rendering it", () => {
    expect(() => parseBootstrapUsernameReason("reserved_name")).toThrow(
      BootstrapContractError,
    );
    expect(parseBootstrapUsernameReason("too_short")).toBe("too_short");
  });
});

describe("SYS_06 host environment boundary fails loudly", () => {
  it.each(["id", "reason", "status"])("rejects an unknown host %s", (field) => {
    const report = structuredClone(PASSING_BOOTSTRAP_REPORT);
    Object.assign(requiredFixtureEntry(report.host_environment.items), {
      [field]: "future_value",
    });
    expect(() => parseBootstrapCheckReport(report)).toThrow(
      BootstrapContractError,
    );
  });
  it.each(["missing", "duplicate"])(
    "rejects %s host item coverage",
    (mutation) => {
      const report = structuredClone(PASSING_BOOTSTRAP_REPORT);
      if (mutation === "missing") report.host_environment.items.pop();
      else
        report.host_environment.items.push(
          requiredFixtureEntry(report.host_environment.items),
        );
      expect(() => parseBootstrapCheckReport(report)).toThrow(
        BootstrapContractError,
      );
    },
  );
  it.each(["cuda", "container_runtime", "platform_components"])(
    "rejects retired id %s",
    (id) => {
      const report = structuredClone(PASSING_BOOTSTRAP_REPORT);
      Object.assign(requiredFixtureEntry(report.checks), { id });
      expect(() => parseBootstrapCheckReport(report)).toThrow(
        BootstrapContractError,
      );
    },
  );
});

function requiredFixtureEntry<T>(items: T[]): T {
  const [first] = items;
  if (first === undefined)
    throw new Error("FX-SYS-06 requires a nonempty fixture");
  return first;
}

it("SYS_06 refuses a missing minimum for a definite host verdict", () => {
  const report = structuredClone(PASSING_BOOTSTRAP_REPORT);
  for (const item of report.host_environment.items) item.minimum = null;
  for (const reason of [
    "driver_meets_minimum",
    "driver_below_minimum",
    "docker_below_minimum",
    "compose_below_minimum",
    "toolkit_meets_minimum",
    "toolkit_below_minimum",
  ]) {
    expect(() => hostCheckReasonPresentation(reason, report)).toThrow(
      BootstrapContractError,
    );
  }
});
