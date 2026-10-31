import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ApiError } from "../api/client";
import { ICON_CLASS_NAMES } from "../components";
import { SafeErrorPage } from "./error-boundary";

const APP_DIRECTORY = dirname(fileURLToPath(import.meta.url));
const SOURCE_DIRECTORY = resolve(APP_DIRECTORY, "..");
const APP_CSS_PATH = resolve(SOURCE_DIRECTORY, "styles/app.css");
const ICON_CSS_PATH = resolve(SOURCE_DIRECTORY, "styles/icons.css");

describe("TC-UI-01 fail-loud app-shell contracts", () => {
  it("derives all four responsive states from real CSS media rules", () => {
    expect(() => {
      validateResponsiveCss(readFileSync(APP_CSS_PATH, "utf8"));
    }).not.toThrow();
  });

  it("positive control fails when the under-1024 drawer rule is deleted", () => {
    const css = readFileSync(APP_CSS_PATH, "utf8");
    const damaged = removeMediaBlock(css, "(max-width: 1023px)");
    expect(() => {
      validateResponsiveCss(damaged);
    }).toThrow(/missing responsive media block/);
  });

  it("keeps each breakpoint literal in the one CSS definition only", () => {
    const css = readFileSync(APP_CSS_PATH, "utf8");
    for (const breakpoint of ["1279px", "1179px", "1023px", "767px"]) {
      expect(css.match(new RegExp(breakpoint, "g"))).toHaveLength(1);
    }
  });
});

describe("TC-UI-01 shell dimension token guard", () => {
  const allowedLiterals = new Map([
    [
      "1279px",
      "PDD 6.8 upper edge for the 1024–1279 collapsed-navigation range",
    ],
    [
      "1179px",
      "PDD 9.3 upper edge below which the model-instance, download-task and local-model tables hide 更新时间 / 校验时间 (<1180 px)",
    ],
    ["1023px", "PDD 6.8 upper edge for the 768–1023 drawer range"],
    ["767px", "PDD 6.8 upper edge for the under-768 mobile range"],
    [
      "100vh",
      "viewport relationship; not a fixed component shape or spacing value",
    ],
  ]);

  it("uses only documented, live exceptions and rejects every other literal", () => {
    expect(() => {
      validateDimensionLiterals(readFileSync(APP_CSS_PATH, "utf8"));
    }).not.toThrow();
  });

  it("positive control rejects an unapproved 13px shell dimension", () => {
    const css = `${readFileSync(APP_CSS_PATH, "utf8")}\n.bad { padding: 13px; }`;
    expect(() => {
      validateDimensionLiterals(css);
    }).toThrow(/13px/);
  });

  function validateDimensionLiterals(css: string) {
    const discovered = new Set(
      css.match(/(?<![-\w.])\d+(?:\.\d+)?(?:px|rem|em|vh|vw|dvh)\b/g) ?? [],
    );
    const unknown = [...discovered].filter(
      (literal) => !allowedLiterals.has(literal),
    );
    expect(
      unknown,
      `unapproved shell dimension literal(s): ${unknown.join(", ")}`,
    ).toEqual([]);
    const unused = [...allowedLiterals.keys()].filter(
      (literal) => !discovered.has(literal),
    );
    expect(unused, `unused exception(s): ${unused.join(", ")}`).toEqual([]);
    for (const reason of allowedLiterals.values()) {
      expect(reason.length).toBeGreaterThan(20);
    }
  }
});

describe("TC-UI-01 icon component-to-CSS axis", () => {
  it("maps every exposed icon name to an existing icons.css class", () => {
    expect(() => {
      validateIconClasses(
        ICON_CLASS_NAMES,
        readFileSync(ICON_CSS_PATH, "utf8"),
      );
    }).not.toThrow();
  });

  it("positive control fails for a missing CSS class", () => {
    expect(() => {
      validateIconClasses(
        { ...ICON_CLASS_NAMES, menu: "i-class-that-does-not-exist" },
        readFileSync(ICON_CSS_PATH, "utf8"),
      );
    }).toThrow(/i-class-that-does-not-exist/);
  });
});

describe("TC-UI-01 sanitized error boundary", () => {
  it("shows a stable code and copyable Request ID", () => {
    render(
      <SafeErrorPage
        error={
          new ApiError({
            code: "backend_unavailable",
            requestId: "0198f58a-2586-7e37-bbf5-fefe165e8080",
            status: 503,
          })
        }
      />,
    );
    expect(screen.getByText("backend_unavailable")).toBeVisible();
    expect(
      screen.getByText("0198f58a-2586-7e37-bbf5-fefe165e8080"),
    ).toBeVisible();
    expect(
      screen.getByRole("button", { name: "复制 Request ID" }),
    ).toBeVisible();
  });

  it("positive control never renders a backend message, URL, or stack", () => {
    const internal = "http://runtime.internal:8123/private/container/path";
    render(<SafeErrorPage error={new Error(internal)} />);
    expect(screen.queryByText(new RegExp(internal))).not.toBeInTheDocument();
    expect(document.body.textContent).not.toContain("runtime.internal");
    expect(document.body.textContent).not.toContain("container/path");
  });
});

function validateResponsiveCss(css: string) {
  const collapsed = mediaBlock(css, "(max-width: 1279px)");
  expect(collapsed).toContain(
    "grid-template-columns: var(--pw-nav-w-collapsed)",
  );
  expect(collapsed).toContain(".pw-sidebar .pw-navigation__label");

  const drawer = mediaBlock(css, "(max-width: 1023px)");
  expect(drawer).toContain(".pw-sidebar");
  expect(drawer).toContain("display: none");
  expect(drawer).toContain(".pw-navigation-trigger");
  expect(drawer).toContain("display: inline-flex");

  const mobile = mediaBlock(css, "(max-width: 767px)");
  expect(mobile).toContain(".pw-navigation-drawer .pw-navigation__item");
  expect(mobile).toContain("height: var(--pw-control-h-l)");
}

function mediaBlock(css: string, query: string) {
  const marker = `@media ${query}`;
  const start = css.indexOf(marker);
  if (start < 0) {
    throw new Error(`missing responsive media block ${query}`);
  }
  const opening = css.indexOf("{", start + marker.length);
  let depth = 0;
  for (let index = opening; index < css.length; index += 1) {
    const character = css[index];
    if (character === "{") depth += 1;
    if (character === "}") depth -= 1;
    if (depth === 0) return css.slice(opening + 1, index);
  }
  throw new Error(`unterminated responsive media block ${query}`);
}

function removeMediaBlock(css: string, query: string) {
  const marker = `@media ${query}`;
  const start = css.indexOf(marker);
  const block = mediaBlock(css, query);
  return `${css.slice(0, start)}${css.slice(start + marker.length + block.length + 2)}`;
}

function validateIconClasses(
  mapping: Readonly<Record<string, string>>,
  css: string,
) {
  for (const className of Object.values(mapping)) {
    expect(css, `missing icon CSS class ${className}`).toMatch(
      new RegExp(`\\.${className}\\s*\\{`),
    );
  }
}
