import { describe, expect, it } from "vitest";

import { API_KEY_PLACEHOLDER, buildAccessExamples } from "./access-examples";

describe("PROTO_18 access examples", () => {
  it("UI_46 PROTO_18 emits copy-ready curl and Python snippets without diff markers", () => {
    const examples = buildAccessExamples(
      "https://workspace.example/",
      "qwen-mini",
    );
    const curlLines = examples.curl.split("\n");

    expect(examples.baseUrl).toBe("https://workspace.example/v1");
    expect(curlLines).toHaveLength(4);
    for (const line of curlLines.slice(0, -1)) {
      expect(line.at(-2)).toBe(" ");
      expect(line.at(-1)).toBe(String.fromCharCode(92));
    }
    for (const line of curlLines.slice(1)) {
      expect(line.startsWith("  ")).toBe(true);
      expect(line).not.toMatch(/^\s*\+/);
    }
    expect(examples.curl).toContain(`Bearer ${API_KEY_PLACEHOLDER}`);
    expect(examples.curl).toContain('"model":"qwen-mini"');

    expect(examples.python.split("\n")).toEqual([
      "import os",
      "",
      "from openai import OpenAI",
      "",
      "client = OpenAI(",
      '    api_key=os.environ["HOLM_API_KEY"],',
      '    base_url="https://workspace.example/v1",',
      ")",
      "response = client.chat.completions.create(",
      '    model="qwen-mini",',
      '    messages=[{"role": "user", "content": "你好"}],',
      ")",
    ]);
  });

  it("PROTO_18 does not duplicate an existing v1 suffix", () => {
    const examples = buildAccessExamples(
      "https://workspace.example/v1/",
      "<API 模型名>",
    );

    expect(examples.baseUrl).toBe("https://workspace.example/v1");
    expect(examples.curl).toContain('"model":"<API 模型名>"');
    expect(examples.python).toContain('model="<API 模型名>"');
  });
});

// FX-UI-48: public example endpoint variants; no live endpoint or secret.
it.each([
  "https://workspace.example/v1",
  "https://workspace.example",
  "https://workspace.example/v1/",
  "https://workspace.example/",
])("UI_48 normalizes Claude Code endpoint %s", (endpoint) => {
  expect(buildAccessExamples(endpoint, "coding").claudeCode).toBe(
    [
      "export ANTHROPIC_BASE_URL=https://workspace.example",
      "export ANTHROPIC_API_KEY=$HOLM_API_KEY",
      "export ANTHROPIC_MODEL=coding",
      "claude",
    ].join("\n"),
  );
});
