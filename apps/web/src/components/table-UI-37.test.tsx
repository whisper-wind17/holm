import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const components = readFileSync(
  resolve(dirname(fileURLToPath(import.meta.url)), "../styles/components.css"),
  "utf8",
);
const app = readFileSync(
  resolve(dirname(fileURLToPath(import.meta.url)), "../styles/app.css"),
  "utf8",
);
const tokens = readFileSync(
  resolve(dirname(fileURLToPath(import.meta.url)), "../styles/tokens.css"),
  "utf8",
);
function rule(css: string, selector: string) {
  const start = css.indexOf(`${selector} {`);
  expect(start, selector).toBeGreaterThanOrEqual(0);
  return css.slice(start, css.indexOf("}", start) + 1);
}

describe("UI-37 table alignment", () => {
  it("left aligns numeric, grouped, action and chart table cells", () => {
    for (const selector of [
      ".pw-table__numeric",
      ".pw-table tbody td",
      ".pw-table thead th",
      ".pw-table__empty",
      ".pw-chart__table td",
    ]) {
      expect(rule(components, selector)).toContain("text-align: start");
    }
    expect(rule(tokens, ".tbl .num")).toContain("text-align: start");
    for (const selector of [
      ".pw-services-page .pw-table__numeric",
      ".pw-services-page .pw-table__header-groups th",
      '.pw-services-page [data-column="actions"]',
    ]) {
      expect(rule(app, selector)).toContain("text-align: start");
    }
    expect(rule(app, ".pw-services-page .pw-service-actions")).toContain(
      "justify-content: flex-start",
    );
    expect(rule(app, ".pw-user-actions")).toContain(
      "justify-content: flex-start",
    );
    expect(app).not.toMatch(
      /\.pw-instances-section \.pw-instance-actions__logs\s*\{[^}]*margin-inline-start:\s*auto/,
    );
  });
  it("offsets the first Ghost action by its own padding and preserves hit targets", () => {
    expect(components).toContain(
      ".pw-table td > .pw-button--ghost:first-child",
    );
    expect(components).toContain(
      ".pw-table td > .pw-button--ghost.pw-button--s:first-child",
    );
    expect(components).toContain(
      "margin-inline-start: calc(-1 * var(--pw-space-8))",
    );
    expect(components).toContain(
      "margin-inline-start: calc(-1 * var(--pw-space-8))",
    );
    for (const selector of [
      ".pw-service-actions",
      ".pw-instance-actions",
      ".pw-user-actions",
      ".pw-models-row-actions__buttons",
      ".pw-models-row-actions > span:first-child",
      ".pw-service-form__actions",
    ]) {
      expect(components).toContain(selector);
    }
    expect(rule(components, ".pw-button")).toContain(
      "min-width: var(--pw-hit-min)",
    );
    expect(rule(components, ".pw-button::after")).toContain(
      "min-height: var(--pw-hit-min)",
    );
    expect(rule(components, ".pw-button--ghost:disabled")).toContain(
      "background: transparent",
    );
    expect(rule(components, ".pw-button:disabled")).toContain(
      "background: var(--pw-disabled-fill)",
    );
  });
  it("fits compact user columns with one-line actions and no vertical scrolling", () => {
    for (const column of [
      "role",
      "services",
      "keys",
      "tokens",
      "last-login",
      "status",
      "actions",
    ]) {
      expect(rule(app, `.pw-users-page [data-column="${column}"]`)).toContain(
        "width:",
      );
    }
    expect(app).not.toContain('.pw-users-page [data-column="user"] {');
    expect(rule(app, '.pw-users-page [data-column="last-login"]')).toContain(
      "white-space: nowrap",
    );
    expect(rule(app, ".pw-user-actions")).toContain("flex-wrap: nowrap");
    expect(rule(app, ".pw-users-page .pw-table__numeric")).toContain(
      "font-family: var(--pw-font-sans)",
    );
    expect(rule(app, ".pw-users-page .pw-table__numeric")).toContain(
      "font-variant-numeric: tabular-nums",
    );
    expect(rule(app, ".pw-users-page .pw-table-wrap")).toContain(
      "overflow-y: hidden",
    );
    expect(
      rule(
        app,
        ".pw-user-muted,\n.pw-user-row--deleted,\n.pw-user-row--deleted .pw-service-identity > a",
      ),
    ).toContain("color: var(--pw-text-tertiary)");
  });
});

describe("UI-39 table layout", () => {
  it("keeps headings unbroken and compact Ghost actions inside tables", () => {
    expect(rule(components, ".pw-table thead th")).toContain(
      "white-space: nowrap",
    );
    const button = rule(components, ".pw-table .pw-button--ghost");
    for (const declaration of [
      "height: var(--pw-control-h-s)",
      "padding-inline: var(--pw-space-8)",
      "font-size: var(--pw-size-table-head)",
      "min-width: 0",
    ])
      expect(button).toContain(declaration);
    expect(rule(components, ".pw-button::after")).toContain(
      "min-width: var(--pw-hit-min)",
    );
    expect(rule(components, ".pw-button::after")).toContain(
      "min-height: var(--pw-hit-min)",
    );
    for (const selector of [
      ".pw-models-stack .pw-table tbody td",
      ".pw-user-detail__table td",
    ]) {
      expect(rule(app, selector)).toContain("overflow-wrap: normal");
    }
    expect(rule(app, ".pw-table .pw-service-identity")).toContain(
      "overflow-wrap: normal",
    );
    expect(app).not.toMatch(
      /[^{}]*\.pw-table[^{}]*\{[^}]*overflow-wrap:\s*anywhere/,
    );
  });

  it("sizes the two tables from fixed columns and a minimum flexible identity column", () => {
    function pixels(expression: string) {
      // Only the spacing-token arithmetic used by these widths is accepted.
      const resolved = expression
        .replace(/var\(--pw-space-(\d+)\)/g, "$1")
        .replace(/^calc\((.*)\)$/s, "$1")
        .trim();
      if (
        !/^\d+(?:\s*\*\s*\d+)?(?:\s*\+\s*\d+(?:\s*\*\s*\d+)?)*$/.test(resolved)
      ) {
        throw new Error(`Unsupported width: ${expression}`);
      }
      return resolved
        .split("+")
        .reduce(
          (sum, term) =>
            sum +
            term
              .split("*")
              .reduce((product, factor) => product * Number(factor.trim()), 1),
          0,
        );
    }
    function width(selector: string, property = "width") {
      const value = rule(app, selector)
        .replace(/\/\*[\s\S]*?\*\//g, "")
        .match(new RegExp(`(?:^|[;{])\\s*${property}: ([^;]+);`))?.[1];
      if (!value) throw new Error(`Missing ${property}: ${selector}`);
      return pixels(value);
    }
    for (const [column, expected] of Object.entries({
      role: 80,
      services: 64,
      keys: 100,
      tokens: 104,
      "last-login": 164,
      status: 88,
      actions: 176,
    })) {
      expect(width(`.pw-users-page [data-column="${column}"]`)).toBe(expected);
    }
    for (const [column, expected] of Object.entries({
      status: 110,
      gpus: 140,
      running: 76,
      ttft: 92,
      tps: 92,
      updated: 140,
      actions: 224,
    })) {
      expect(width(`.pw-instances-section th[data-column="${column}"]`)).toBe(
        expected,
      );
    }
    expect(width(".pw-instances-section .pw-table", "min-width")).toBe(1094);
    expect(width(".pw-users-page .pw-table", "min-width")).toBe(956);
    expect(() => pixels("50%")).toThrow("Unsupported width");
    expect(rule(app, ".pw-instances-section .pw-table__numeric")).toContain(
      "font-family: var(--pw-font-sans)",
    );
    expect(rule(app, ".pw-instances-section .pw-table__numeric")).toContain(
      "white-space: nowrap",
    );
    expect(rule(app, ".pw-instance-status")).toContain(
      "gap: var(--pw-space-8)",
    );
    expect(rule(app, ".pw-user-status")).toContain("gap: var(--pw-space-8)");
  });
});

it("UI-39 truncates authorized names and request IDs without splitting or spilling", () => {
  for (const selector of [
    ".pw-models-name > strong,\n.pw-models-name__link",
    ".pw-models-request-id[title]",
    ".pw-overview-table__link strong",
  ]) {
    const styles = rule(app, selector);
    expect(styles).toContain("overflow: hidden");
    expect(styles).toContain("white-space: nowrap");
    expect(styles).toContain("text-overflow: ellipsis");
  }
  expect(rule(app, ".pw-models-row-actions .pw-alert > span")).toContain(
    "min-width: 0",
  );
  expect(rule(app, ".pw-overview-table__link")).toContain("width: 0");
  expect(rule(app, ".pw-overview-table__link")).toContain("min-width: 100%");
  expect(rule(app, ".pw-overview-table__link strong")).toContain(
    "min-width: 0",
  );
});
