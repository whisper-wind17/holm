import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    environment: "jsdom",
    setupFiles: ["src/test/setup.ts"],
  },
  build: {
    lib: {
      entry: "src/workspace.ts",
      fileName: "workspace",
      formats: ["es"],
    },
  },
});
