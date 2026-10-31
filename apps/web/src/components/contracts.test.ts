import { readFileSync, readdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const COMPONENT_DIRECTORY = dirname(fileURLToPath(import.meta.url));
const SOURCE_DIRECTORY = resolve(COMPONENT_DIRECTORY, "..");
const COMPONENT_CSS = resolve(SOURCE_DIRECTORY, "styles/components.css");
const ICON_CSS = resolve(SOURCE_DIRECTORY, "styles/icons.css");
const TOKEN_CSS = resolve(SOURCE_DIRECTORY, "styles/tokens.css");
const ICON_DIRECTORY = resolve(SOURCE_DIRECTORY, "assets/icons");

function cssRule(source: string, selector: string) {
  let searchFrom = 0;
  while (searchFrom < source.length) {
    const selectorStart = source.indexOf(selector, searchFrom);
    if (selectorStart < 0) {
      break;
    }
    const afterSelector = source.slice(selectorStart + selector.length);
    const rule = /^\s*\{([^}]*)\}/s.exec(afterSelector);
    const precedingCharacter = source.slice(0, selectorStart).trimEnd().at(-1);
    if (rule !== null && precedingCharacter !== ",") {
      return rule[1] ?? "";
    }
    searchFrom = selectorStart + selector.length;
  }
  expect.fail(`missing CSS rule ${selector}`);
}

function cssDeclarations(source: string, selector: string) {
  return new Map(
    cssRule(source, selector)
      .split(";")
      .map((declaration) => declaration.trim())
      .filter(Boolean)
      .map((declaration) => {
        const separator = declaration.indexOf(":");
        expect(separator, `invalid declaration in ${selector}`).toBeGreaterThan(
          0,
        );
        return [
          declaration.slice(0, separator).trim(),
          declaration.slice(separator + 1).trim(),
        ];
      }),
  );
}

describe("M1-11B fail-loud component resource contracts", () => {
  it("TC-UI-01 statically derives 44px pointer targets without jsdom layout", () => {
    const css = readFileSync(COMPONENT_CSS, "utf8");
    const tokens = readFileSync(TOKEN_CSS, "utf8");
    expect(tokens).toContain("--pw-hit-min: 44px");
    expect(cssDeclarations(css, ".pw-button").get("position")).toBe("relative");
    const buttonTarget = cssDeclarations(css, ".pw-button::after");
    for (const [property, value] of [
      ["position", "absolute"],
      ["top", "50%"],
      ["left", "50%"],
      ["width", "100%"],
      ["min-width", "var(--pw-hit-min)"],
      ["height", "100%"],
      ["min-height", "var(--pw-hit-min)"],
      ["content", '""'],
      ["transform", "translate(-50%, -50%)"],
    ] as const) {
      expect(
        buttonTarget.get(property),
        `${property} must preserve the hit area`,
      ).toBe(value);
    }
    expect(cssDeclarations(css, ".pw-input-target").get("min-height")).toBe(
      "var(--pw-hit-min)",
    );

    expect(
      readFileSync(resolve(COMPONENT_DIRECTORY, "button.tsx"), "utf8"),
    ).toContain('"pw-button"');
    const formSource = readFileSync(
      resolve(COMPONENT_DIRECTORY, "form.tsx"),
      "utf8",
    );
    expect(formSource).toContain('className="pw-input-target"');
    expect(formSource).toContain("onMouseDown={focusInput}");

    const componentFiles = readdirSync(COMPONENT_DIRECTORY)
      .filter((name) => name.endsWith(".tsx") && name !== "button.tsx")
      .map((name) => readFileSync(resolve(COMPONENT_DIRECTORY, name), "utf8"));
    expect(componentFiles.join("\n")).not.toMatch(/<button(?:\s|>)/);
  });

  it("TC-UI-08 defines observable hover, pressed, selected, disabled, loading, and error selectors", () => {
    const css = readFileSync(COMPONENT_CSS, "utf8");
    for (const selector of [
      ".pw-button:hover:not(:disabled)",
      ".pw-button:active:not(:disabled)",
      '.pw-button[data-selected="true"]',
      ".pw-button:disabled",
      '.pw-button[data-loading="true"]',
      '.pw-input-target[data-invalid="true"] .pw-input',
    ]) {
      expect(cssRule(css, selector)).not.toHaveLength(0);
    }
  });

  it("TC-UI-10 makes reduced-motion durations zero in both media and runtime paths", () => {
    const css = readFileSync(COMPONENT_CSS, "utf8");
    expect(css).toContain("@media (prefers-reduced-motion: reduce)");
    expect(css).toContain('data-reduced-motion="true"');
    expect(
      css.match(/(?:transition|animation)-duration:\s*0ms/g)?.length,
    ).toBeGreaterThanOrEqual(2);
  });

  it("keeps the PDD component dimensions connected to semantic tokens", () => {
    const css = readFileSync(COMPONENT_CSS, "utf8");
    expect(cssRule(css, ".pw-table thead th")).toContain(
      "z-index: var(--pw-z-sticky-thead)",
    );
    expect(cssRule(css, ".pw-table tbody td")).toContain(
      "height: var(--pw-row-h)",
    );
    for (const size of ["s", "m", "l", "xl"]) {
      expect(cssRule(css, `.pw-dialog--${size}`)).toContain(
        `var(--pw-modal-${size})`,
      );
    }
    expect(cssRule(css, ".pw-drawer")).toContain("var(--pw-drawer-w)");
    expect(cssRule(css, ".pw-toast-region")).toContain("var(--pw-z-toast)");
  });

  it("keeps icons.css references and vendored SVG files in exact two-way sync", () => {
    const css = readFileSync(ICON_CSS, "utf8");
    const referenced = [
      ...css.matchAll(/url\(\.\.\/assets\/icons\/([^)]+\.svg)\)/g),
    ].map((match) => match[1]);
    const uniqueReferenced = [...new Set(referenced)].sort();
    const vendored = readdirSync(ICON_DIRECTORY)
      .filter((name) => name.endsWith(".svg"))
      .sort();
    expect(uniqueReferenced).toHaveLength(48);
    expect(vendored).toEqual(uniqueReferenced);
  });
});
