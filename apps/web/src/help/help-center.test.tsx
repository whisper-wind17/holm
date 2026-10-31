import { render, screen, within, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import axe from "axe-core";
import { describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import {
  ADMIN_ME,
  USER_ME,
  FakeApiClient,
  renderApplication,
} from "../app/test-support";
import { HelpButton, HelpProvider } from "./help-center";
import {
  HELP_TOPICS,
  HelpBody,
  helpBody,
  readHelpSections,
  type HelpTopicId,
} from "./content";
import source from "../../../../docs/content/help-center.md?raw";
import type { ModelInstance } from "../api/model-instance-contracts";

// FX-M7-15A: existing FakeApiClient session fixtures plus a running/failed instance;
// no live account, key, GPU or external service is involved.
const instanceId = "0198f58a-2586-7e37-bbf5-000000000012";
function instance(status: ModelInstance["status"] = "Running"): ModelInstance {
  return {
    artifact_id: "0198f58a-2586-7e37-bbf5-000000000010",
    config_version: 1,
    updated_at: "2026-10-08T09:00:00Z",
    context_length: 4096,
    error_code: status === "Failed" ? "runtime_out_of_memory" : null,
    gpu_memory_utilization: 0.9,
    restart_count: 0,
    gpu_uuids: ["GPU-fixture-0"],
    id: instanceId,
    internal_endpoint: null,
    name: "FX-M7-15A",
    internal_model_name: "FX-M7-15A",
    runtime_type: "vllm",
    status,
    tensor_parallel_size: 1,
  };
}
async function openHelp(name = "帮助") {
  await userEvent.click(await screen.findByRole("button", { name }));
  return screen.findByRole("dialog", { name: "帮助中心" });
}

describe("M7-15A TC-UI-01/02/03 task help", () => {
  it("opens the real topbar with keyboard, renders all four admin topics and restores focus", async () => {
    renderApplication(new FakeApiClient(ADMIN_ME), "/overview");
    const trigger = await screen.findByRole("button", { name: "帮助" });
    const user = userEvent.setup();
    trigger.focus();
    await user.keyboard("{Enter}");
    const dialog = await screen.findByRole("dialog", { name: "帮助中心" });
    expect(
      within(dialog).getByRole("heading", { name: "帮助中心" }),
    ).toHaveFocus();
    for (const topic of Object.values(HELP_TOPICS)) {
      await user.click(
        within(dialog).getByRole("button", { name: topic.title }),
      );
      expect(within(dialog).getByRole("article")).toHaveAccessibleName(
        topic.title,
      );
      expect(within(dialog).getAllByRole("heading").length).toBeGreaterThan(3);
      const scan = await axe.run(dialog, {
        rules: { "color-contrast": { enabled: false } },
      });
      expect(scan.passes.length).toBeGreaterThan(0);
      expect(
        scan.violations.filter(
          (item) => item.impact === "critical" || item.impact === "serious",
        ),
      ).toEqual([]);
    }
    expect(
      within(dialog).getByRole("heading", { name: "部署失败" }),
    ).toBeVisible();
    expect(
      within(dialog).getByRole("heading", { name: "用量对不上" }),
    ).toBeVisible();
    await user.keyboard("{Escape}");
    await waitFor(() => expect(trigger).toHaveFocus());
  });

  it("shows only user topics and never renders administrator troubleshooting", async () => {
    renderApplication(new FakeApiClient(USER_ME), "/services");
    const dialog = await openHelp();
    expect(
      within(dialog).queryByRole("button", { name: "首次部署" }),
    ).toBeNull();
    expect(
      within(dialog).queryByRole("button", { name: "服务发布" }),
    ).toBeNull();
    await userEvent.click(
      within(dialog).getByRole("button", { name: "故障排查" }),
    );
    expect(within(dialog).getAllByRole("row")).toHaveLength(6);
    expect(dialog.textContent).not.toMatch(
      /GPU|Runtime|模型实例|部署失败|下载失败/,
    );
    const scan = await axe.run(dialog, {
      rules: { "color-contrast": { enabled: false } },
    });
    expect(scan.violations).toEqual([]);
  });

  it.each([
    HELP_TOPICS.deployment.id,
    HELP_TOPICS.publishing.id,
    "unknown-topic",
  ])(
    "recovers from restricted or unknown topic %s without showing its contents",
    async (id) => {
      render(
        <HelpProvider admin={false}>
          <HelpButton topic={id as HelpTopicId}>构造入口</HelpButton>
        </HelpProvider>,
      );
      const dialog = await openHelp("构造入口");
      expect(
        within(dialog).getByRole("heading", { name: "无法打开此帮助主题" }),
      ).toBeVisible();
      expect(dialog.textContent).not.toMatch(/GPU|Runtime|模型实例/);
      await userEvent.click(
        within(dialog).getByRole("button", { name: "返回帮助目录" }),
      );
      expect(
        within(dialog).getByRole("heading", { name: "选择你正在处理的任务" }),
      ).toBeVisible();
      await userEvent.click(
        within(dialog).getByRole("button", { name: "API 接入" }),
      );
      expect(
        within(dialog).getByRole("heading", { name: "创建 API 密钥" }),
      ).toBeVisible();
    },
  );

  it("keeps help local and never persists dynamic page content or submits the underlying form", async () => {
    const fetcher = vi.spyOn(window, "fetch");
    const storage = vi.spyOn(Storage.prototype, "setItem");
    const push = vi.spyOn(window.history, "pushState");
    const replace = vi.spyOn(window.history, "replaceState");
    const submit = vi.fn();
    render(
      <HelpProvider admin>
        <form onSubmit={submit}>
          <label>
            草稿
            <input defaultValue="FX-M7-15A-private-draft" />
          </label>
          <HelpButton topic={HELP_TOPICS.access.id} />
        </form>
      </HelpProvider>,
    );
    const dialog = await openHelp();
    expect(dialog.textContent).not.toContain("FX-M7-15A-private-draft");
    await userEvent.click(
      within(dialog).getByRole("button", { name: "故障排查" }),
    );
    await userEvent.click(
      within(dialog).getByRole("button", { name: "关闭帮助" }),
    );
    expect(screen.getByLabelText("草稿")).toHaveValue(
      "FX-M7-15A-private-draft",
    );
    expect(submit).not.toHaveBeenCalled();
    expect(fetcher).not.toHaveBeenCalled();
    expect(storage).not.toHaveBeenCalled();
    expect(push).not.toHaveBeenCalled();
    expect(replace).not.toHaveBeenCalled();
  });

  it("UI_46 opens instance help and links personal cards to the access guide", async () => {
    const client = new FakeApiClient(ADMIN_ME);
    client.modelInstanceDetailResults.push(instance("Failed"));
    const view = renderApplication(client, `/models/instances/${instanceId}`);
    const dialog = await openHelp("查看故障排查帮助");
    expect(
      within(dialog).getByRole("heading", { name: "部署失败" }),
    ).toBeVisible();
    view.unmount();
    const consumer = new FakeApiClient(USER_ME);
    consumer.myAiServiceResults.push({
      items: [
        {
          name: "测试助手",
          public_model_name: "FX-M7-15A-service",
          description: "测试服务",
          available: true,
          enabled: true,
          id: "45000000-0000-4000-8000-000000000001",
          last_used_at: null,
          last_succeeded_at: null,
        },
      ],
    });
    renderApplication(consumer, "/services");
    expect(
      await screen.findByRole("link", { name: "查看接入方式" }),
    ).toHaveAttribute("href", "/services/45000000-0000-4000-8000-000000000001");
    expect(
      screen.queryByRole("button", { name: "查看 API 接入帮助" }),
    ).not.toBeInTheDocument();
  });

  it("preserves a publishing draft across help and displays help on the actual success result", async () => {
    const client = new FakeApiClient(ADMIN_ME);
    client.modelInstanceResults.push({ items: [instance()] });
    client.aiServiceCreateResults.push({
      id: "0198f58a-2586-7e37-bbf5-000000000014",
      name: "帮助草稿",
      public_model_name: "help-draft",
      description: "",
      enabled: true,
      primary_instance_id: instanceId,
      primary_instance_status: "Running",
      route_version: 1,
      runtime_status: "Available",
    });
    renderApplication(client, "/services");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "创建服务" }));
    const form = await screen.findByRole("dialog", { name: "创建服务" });
    await user.type(within(form).getByLabelText(/^服务名称/), "帮助草稿");
    await user.type(within(form).getByLabelText(/^API 模型名/), "help-draft");
    await user.selectOptions(
      within(form).getByLabelText(/^后端实例/),
      instanceId,
    );
    const trigger = within(form).getByRole("button", {
      name: "查看服务发布帮助",
    });
    await user.click(trigger);
    const help = await screen.findByRole("dialog", { name: "帮助中心" });
    expect(
      within(help).getByRole("heading", { name: "发布一个服务" }),
    ).toBeVisible();
    await user.click(within(help).getByRole("button", { name: "API 接入" }));
    await user.keyboard("{Escape}");
    await waitFor(() => expect(trigger).toHaveFocus());
    expect(within(form).getByLabelText(/^API 模型名/)).toHaveValue(
      "help-draft",
    );
    expect(within(form).getByLabelText(/^后端实例/)).toHaveValue(instanceId);
    expect(client.aiServiceCreateCalls).toEqual([]);
    await user.click(within(form).getByRole("button", { name: "创建服务" }));
    expect(await screen.findByText("AI 服务已创建")).toBeVisible();
    const resultHelp = await openHelp("查看服务发布帮助");
    expect(
      within(resultHelp).getByRole("heading", { name: "发布之后用户还差一步" }),
    ).toBeVisible();
    expect(client.aiServiceCreateCalls).toHaveLength(1);
  });

  it("consumes the approved source, rejects broken structure and escapes raw markup", () => {
    expect(readHelpSections(source).size).toBe(5);
    expect(() =>
      readHelpSections(source.replace("# Topic 1 · 首次部署", "# Topic X")),
    ).toThrow();
    expect(() =>
      readHelpSections(source + "\n# Topic 1 · 首次部署\n"),
    ).toThrow();
    expect(() =>
      renderToStaticMarkup(<HelpBody text="| a | b |\n| --- |\n| c | d |" />),
    ).toThrow("Invalid help table");
    expect(() => renderToStaticMarkup(<HelpBody text="```shell" />)).toThrow(
      "Unsupported help block",
    );
    expect(
      renderToStaticMarkup(<HelpBody text='<img src=x onerror="unsafe()">' />),
    ).toContain("&lt;img");
    for (const topic of Object.values(HELP_TOPICS)) {
      const body = helpBody(topic.id, true);
      expect(body).toBeTruthy();
      if (body === undefined) throw new Error("Missing fixture topic");
      expect(body).not.toMatch(/https?:\/\/|```|<script|curl /);
      expect(renderToStaticMarkup(<HelpBody text={body} />)).not.toContain(
        "<a ",
      );
    }
  });
});

// FX-UI-48 reuses the authorized USER_ME service fixture; no live service.
it.each(["/services/45000000-0000-4000-8000-000000000001", "/api-keys"])(
  "UI_48 opens API access help from %s without administrator topics",
  async (path) => {
    const client = new FakeApiClient(USER_ME);
    client.myAiServiceResults.push({
      items: [
        {
          id: "45000000-0000-4000-8000-000000000001",
          name: "编程助手",
          public_model_name: "coding",
          description: "测试服务",
          available: true,
          enabled: true,
          last_used_at: null,
          last_succeeded_at: null,
        },
      ],
    });
    renderApplication(client, path);
    const dialog = await openHelp("查看 API 接入帮助");
    expect(
      within(dialog).getByRole("article", { name: "API 接入" }),
    ).toBeVisible();
    expect(
      within(dialog).getByRole("heading", { name: "创建 API 密钥" }),
    ).toBeVisible();
    expect(
      within(dialog).queryByRole("button", { name: "首次部署" }),
    ).toBeNull();
    expect(
      within(dialog).queryByRole("button", { name: "服务发布" }),
    ).toBeNull();
    expect(dialog).toHaveTextContent("Claude Code");
    expect(dialog).toHaveTextContent("全部已授权服务（含以后新授权的）");
  },
);
