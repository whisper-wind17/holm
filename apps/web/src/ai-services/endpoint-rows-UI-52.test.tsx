import axe from "axe-core";
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { buildAccessExamples } from "../api/access-examples";
import type { AiService } from "../api/ai-service-contracts";
import type { ModelInstance } from "../api/model-instance-contracts";
import {
  ADMIN_ME,
  USER_ME,
  FakeApiClient,
  renderApplication,
  UI_46_PERSONAL_SERVICE,
} from "../app/test-support";

// FX-UI-52: synthetic running instance and service, based on FX-UI-21 and FX-UI-46.
const service: AiService = {
  id: UI_46_PERSONAL_SERVICE.id,
  name: "研发助手",
  public_model_name: "qwen-mini",
  description: "",
  enabled: true,
  primary_instance_id: "52000000-0000-4000-8000-000000000001",
  primary_instance_status: "Running",
  route_version: 1,
  runtime_status: "Available",
};
const instance: ModelInstance = {
  id: service.primary_instance_id,
  artifact_id: "52000000-0000-4000-8000-000000000002",
  name: "运行实例",
  status: "Running",
  config_version: 1,
  context_length: 4096,
  error_code: null,
  gpu_memory_utilization: 0.9,
  restart_count: 0,
  gpu_uuids: ["FX-UI-52-GPU"],
  internal_endpoint: null,
  internal_model_name: "fx-ui-52",
  runtime_type: "vllm",
  tensor_parallel_size: 1,
  updated_at: "2026-10-30T00:00:00Z",
};
afterEach(() => vi.restoreAllMocks());

function endpointRows(scope: HTMLElement) {
  const label = within(scope).getByText("Endpoint", { selector: "dt" });
  const values = label.nextElementSibling;
  if (!(values instanceof HTMLElement))
    throw new Error("Missing Endpoint values");
  expect(values.children).toHaveLength(2);
  for (const [index, protocol, address] of [
    [0, "OpenAI", `${window.location.origin}/v1`],
    [1, "Anthropic", window.location.origin],
  ] as const) {
    const row = values.children[index];
    if (!(row instanceof HTMLElement)) throw new Error("Missing Endpoint row");
    expect(within(row).getByText(`${protocol} 兼容`)).toBeVisible();
    expect(within(row).getByText(address, { selector: "code" })).toBeVisible();
  }
  return values;
}

it.each(["personal", "admin"] as const)(
  "UI-52 TC-UI-26 %s detail shows and copies both Endpoint rows with accessible controls",
  async (role) => {
    const user = userEvent.setup();
    const api = new FakeApiClient(role === "personal" ? USER_ME : ADMIN_ME);
    vi.spyOn(api, "getMyAiServices").mockResolvedValue({
      items: [UI_46_PERSONAL_SERVICE],
    });
    vi.spyOn(api, "getAiService").mockResolvedValue(service);
    vi.spyOn(api, "getModelInstances").mockResolvedValue({ items: [instance] });
    const { container } = renderApplication(api, `/services/${service.id}`);
    await screen.findByRole("heading", { name: service.name, level: 1 });
    const rows = endpointRows(container);
    const clipboard = vi.spyOn(navigator.clipboard, "writeText");
    for (const [protocol, address] of [
      ["OpenAI", `${window.location.origin}/v1`],
      ["Anthropic", window.location.origin],
    ] as const) {
      await user.click(
        within(rows).getByRole("button", {
          name: `复制 ${protocol} 兼容 Endpoint`,
        }),
      );
      expect(clipboard).toHaveBeenLastCalledWith(address);
    }
    expect((await axe.run(container)).violations).toEqual([]);
    if (role === "personal") {
      await user.click(screen.getByRole("tab", { name: "Claude Code" }));
      expect(screen.getByRole("tabpanel")).toHaveTextContent(
        `export ANTHROPIC_BASE_URL=${window.location.origin}`,
      );
    }
  },
);

it("UI-52 TC-UI-26 creation preview shows both Endpoint rows without copy buttons and passes axe", async () => {
  const user = userEvent.setup();
  const api = new FakeApiClient(ADMIN_ME);
  vi.spyOn(api, "getModelInstances").mockResolvedValue({ items: [instance] });
  renderApplication(api, "/services");
  await user.click(await screen.findByRole("button", { name: "创建服务" }));
  const dialog = await screen.findByRole("dialog", { name: "创建服务" });
  const preview = await within(dialog).findByRole("region", {
    name: "服务预览",
  });
  const rows = endpointRows(preview);
  expect(within(rows).queryByRole("button")).not.toBeInTheDocument();
  expect((await axe.run(dialog)).violations).toEqual([]);
});

it.each(["https://workspace.example/", "https://workspace.example/v1/"])(
  "UI-52 TC-UI-26 Claude Code shares the Anthropic address for %s",
  (baseUrl) => {
    const examples = buildAccessExamples(baseUrl, "qwen-mini");
    expect(examples.anthropicBaseUrl).toBe("https://workspace.example");
    expect(examples.baseUrl).toBe(`${examples.anthropicBaseUrl}/v1`);
    expect(examples.claudeCode.split("\n")[0]).toBe(
      `export ANTHROPIC_BASE_URL=${examples.anthropicBaseUrl}`,
    );
  },
);
