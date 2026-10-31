import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { expect, it } from "vitest";

it("UI-06 TC-UI-02 rejects heading :focus rules, including a positive control", () => {
  const css = readFileSync(
    resolve(dirname(fileURLToPath(import.meta.url)), "../styles/app.css"),
    "utf8",
  );
  const validateHeadingFocus = (source: string) => {
    expect(source).not.toMatch(/(?:\bh1|-heading):focus(?![\w-])/);
  };

  validateHeadingFocus(css);

  // FX-UI-06: restore the original heading ring in memory as a positive control.
  const damaged = `${css}\nh1:focus {
    outline: var(--pw-focus-ring-width) solid var(--pw-focus-ring);
  }`;
  expect(() => {
    validateHeadingFocus(damaged);
  }).toThrow();
});
