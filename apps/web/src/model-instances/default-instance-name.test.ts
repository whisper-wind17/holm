import { describe, expect, it } from "vitest";
import samples from "../../../../tests/fixtures/instance_names/UI-07.json";
import { defaultInstanceName } from "./instance-names";

// FX-UI-07: same PDD 8.2 examples consumed by the Python unit test.
describe("UI_07 shared default names", () => {
  it.each(samples)(
    "$model_name -> $expected",
    ({ model_name, occupied, expected }) => {
      expect(defaultInstanceName(model_name, occupied)).toBe(expected);
    },
  );
});
