import "@testing-library/jest-dom/vitest";

import { cleanup } from "@testing-library/react";
import { afterEach, vi } from "vitest";

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

Object.defineProperty(window, "matchMedia", {
  configurable: true,
  value: vi.fn().mockImplementation((query: string) => ({
    addEventListener: vi.fn(),
    addListener: vi.fn(),
    dispatchEvent: vi.fn(),
    matches: false,
    media: query,
    onchange: null,
    removeEventListener: vi.fn(),
    removeListener: vi.fn(),
  })),
});

Object.defineProperties(Element.prototype, {
  hasPointerCapture: {
    configurable: true,
    value: vi.fn().mockReturnValue(false),
  },
  releasePointerCapture: {
    configurable: true,
    value: vi.fn(),
  },
  setPointerCapture: {
    configurable: true,
    value: vi.fn(),
  },
});
