import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import axe from "axe-core";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";
import { describe, expect, it, vi } from "vitest";

import { ApiError } from "../api/client";
import {
  parseModelArtifactDetail,
  type ModelArtifactDetail,
} from "../api/model-acquisition-contracts";
import { ADMIN_ME, FakeApiClient } from "../app/test-support";
import { LocalModelDetailPage } from "./artifact-detail-page";
import { ModelsPage } from "./model-page";

const ARTIFACT_ID = "0198f58a-2586-7e37-bbf5-fefe165e8084";
const MODEL_ID = "0198f58a-2586-7e37-bbf5-fefe165e8083";
const INSTANCE_ID = "0198f58a-2586-7e37-bbf5-fefe165e8090";
const SERVICE_ID = "0198f58a-2586-7e37-bbf5-fefe165e8091";
const SHA = "0123456789abcdef".repeat(4);

describe("UI-31 本地模型详情页", () => {
  it("shows header, information, source, references and file list for a Ready Managed model", async () => {
    const client = clientWith(detail());
    const user = userEvent.setup();
    const { container } = renderDetail(client);

    expect(
      await screen.findByRole("heading", { level: 1, name: "Qwen3" }),
    ).toBeVisible();
    const crumbs = screen.getByRole("navigation", { name: "面包屑" });
    expect(within(crumbs).getByRole("link", { name: "模型" })).toHaveAttribute(
      "href",
      "/models",
    );
    expect(
      within(crumbs).getByRole("link", { name: "本地模型" }),
    ).toBeVisible();
    expect(within(crumbs).getByText("Qwen3")).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(screen.getAllByText("可用")).toHaveLength(2);
    expect(screen.getByText("平台托管")).toBeVisible();
    expect(
      screen.getByText(/Hugging Face · 10 MB · 被 1 个模型实例引用/),
    ).toBeVisible();
    expect(screen.queryByRole("button", { name: /重新下载/ })).toBeNull();
    expect(screen.getByRole("button", { name: "重新校验" })).toBeVisible();
    expect(screen.getByRole("button", { name: "删除文件" })).toBeVisible();

    // 模型信息
    expect(screen.getByText("Qwen3ForCausalLM")).toBeVisible();
    expect(screen.getByText("文本")).toBeVisible();
    expect(screen.getByText("bf16")).toBeVisible();
    expect(screen.getByText("32,768")).toBeVisible();
    expect(await screen.findByText("vLLM 0.29.0")).toBeVisible();

    // 来源与位置
    expect(
      screen.getByRole("link", { name: "https://huggingface.co/Qwen/Qwen3" }),
    ).toBeVisible();
    expect(screen.getByText("abc123def456abc123def456")).toBeVisible();
    expect(screen.getByText("/models/qwen3")).toBeVisible();
    expect(screen.getByText(/下载完成/)).toBeVisible();

    // 引用实例：GPU 张数取实例列表的 tensor_parallel_size，服务按 primary_instance_id 匹配
    const instances = await screen.findByRole("table", { name: "引用实例" });
    expect(
      await within(instances).findByRole("link", { name: "qwen3-prod" }),
    ).toHaveAttribute("href", `/models/instances/${INSTANCE_ID}`);
    expect(within(instances).getByText("运行中")).toBeVisible();
    expect(await within(instances).findByText("2")).toBeVisible();
    expect(
      await within(instances).findByRole("link", { name: "对话服务" }),
    ).toHaveAttribute("href", `/services/${SERVICE_ID}`);

    // 文件清单：SHA-256 前 12 位，完整值在 title
    const files = screen.getByRole("table", { name: "文件清单" });
    expect(within(files).getByText("config.json")).toBeVisible();
    expect(within(files).getByText(SHA.slice(0, 12))).toHaveAttribute(
      "title",
      SHA,
    );
    expect(screen.getByText("1 个文件")).toBeVisible();

    await user.click(screen.getByRole("button", { name: "部署" }));
    expect(screen.getByTestId("location")).toHaveTextContent(
      `/models/deploy?artifact_id=${ARTIFACT_ID}`,
    );
    expect(
      (
        await axe.run(container, {
          rules: { "color-contrast": { enabled: false } },
        })
      ).violations,
    ).toEqual([]);
  });

  it("explains the three source kinds and the ModelScope revision pin", async () => {
    const scope = renderDetail(
      clientWith(detail({ source: "ModelScope", repository_id: "qwen/Qwen3" })),
    );
    expect(
      await screen.findByRole("link", {
        name: "https://modelscope.cn/models/qwen/Qwen3",
      }),
    ).toBeVisible();
    expect(
      screen.getByText("以下载时的文件清单与逐文件哈希固定版本"),
    ).toBeVisible();
    scope.unmount();

    renderDetail(
      clientWith(detail({ source: "Local Path", storage_type: "External" })),
    );
    expect(
      await screen.findByText(
        "服务器本地目录（外部目录，平台只读、不复制、不删除）",
      ),
    ).toBeVisible();
    expect(screen.getByRole("button", { name: "部署" })).toBeVisible();
    expect(screen.getByRole("button", { name: "重新校验" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "删除文件" })).toBeNull();
  });

  it("shows a pending note with a 重新校验 entry when files is null", async () => {
    renderDetail(clientWith(detail({ files: null })));
    expect(
      await screen.findByText("文件清单将在下一次完整校验后显示"),
    ).toBeVisible();
    expect(screen.queryByRole("table", { name: "文件清单" })).toBeNull();
    expect(screen.getAllByRole("button", { name: "重新校验" })).toHaveLength(2);
  });

  it("writes 未识别 for null model fields and degrades the Runtime row", async () => {
    const nulls = {
      architecture: null,
      context_length: null,
      license: null,
      modalities: null,
      precision: null,
      quantization: null,
    };
    const empty = clientWith(detail({ model: nulls }));
    vi.spyOn(empty, "getModelDeploymentOptions").mockResolvedValue({
      model_context_limit: { state: "unrecognized" },
      runtimes: [],
    });
    const first = renderDetail(empty);
    expect(await screen.findByText("无可用 Runtime")).toBeVisible();
    expect(screen.getAllByText("未识别")).toHaveLength(4);
    first.unmount();

    const failing = clientWith(detail());
    vi.spyOn(failing, "getModelDeploymentOptions").mockRejectedValue(
      new Error("down"),
    );
    renderDetail(failing);
    expect(await screen.findByText("暂不可用")).toBeVisible();
  });

  it("explains Corrupted Managed, Corrupted External and Missing External models", async () => {
    const managed = renderDetail(clientWith(detail({ status: "Corrupted" })));
    expect(
      await screen.findByText("文件异常", { selector: "strong" }),
    ).toBeVisible();
    expect(
      screen.getByText(
        "先重新校验，仍未通过可删除文件后到「下载模型」重新下载。",
      ),
    ).toBeVisible();
    expect(screen.queryByRole("button", { name: "部署" })).toBeNull();
    expect(screen.getByRole("button", { name: "删除文件" })).toBeVisible();
    managed.unmount();

    const external = renderDetail(
      clientWith(detail({ status: "Corrupted", storage_type: "External" })),
    );
    expect(await screen.findByText(/请在服务器上修复源目录/)).toBeVisible();
    expect(screen.queryByRole("button", { name: "删除文件" })).toBeNull();
    expect(screen.getByRole("button", { name: "重新校验" })).toBeVisible();
    external.unmount();

    renderDetail(
      clientWith(detail({ status: "Missing", storage_type: "External" })),
    );
    expect(await screen.findByText(/请在服务器上恢复路径/)).toBeVisible();
    expect(screen.getByText("文件缺失", { selector: "strong" })).toBeVisible();
  });

  it("shows a progress notice while a verification job runs", async () => {
    const client = clientWith(detail());
    vi.spyOn(client, "getArtifactVerificationStatus").mockResolvedValue({
      job_id: ARTIFACT_ID,
      status: "Running",
      error_code: null,
    });
    renderDetail(client);
    expect(await screen.findByText("正在重新校验")).toBeVisible();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "重新校验" })).toBeDisabled(),
    );
  });

  it("deletes without asking for the model name and then shows the deleted state", async () => {
    const client = clientWith(
      detail({ referencing_instances: 0, instances: [] }),
    );
    const remove = vi
      .spyOn(client, "deleteModelArtifact")
      .mockResolvedValue({ id: ARTIFACT_ID, queued: false });
    const user = userEvent.setup();
    renderDetail(client);
    await user.click(await screen.findByRole("button", { name: "删除文件" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).queryByRole("textbox")).toBeNull();
    expect(
      within(dialog).getByText(/下载任务记录与历史用量保留/),
    ).toBeVisible();
    await user.click(
      within(dialog).getByRole("button", { name: "确认删除文件" }),
    );
    expect(remove).toHaveBeenCalledWith(ARTIFACT_ID);
    expect(await screen.findByText("本地模型已删除")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "返回本地模型" }));
    expect(screen.getByTestId("location")).toHaveTextContent(
      "/models?tab=local-models",
    );
  });

  it("refuses deletion while instances reference the model and lists them", async () => {
    const client = clientWith(detail());
    const user = userEvent.setup();
    renderDetail(client);
    await user.click(await screen.findByRole("button", { name: "删除文件" }));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText(/仍有 1 个模型实例引用/)).toBeVisible();
    expect(
      await within(dialog).findByRole("link", { name: "qwen3-prod" }),
    ).toBeVisible();
    expect(
      within(dialog).queryByRole("button", { name: "确认删除文件" }),
    ).toBeNull();
  });

  it("treats a 404 as already deleted", async () => {
    const client = clientWith(detail());
    vi.spyOn(client, "artifactDetail").mockRejectedValue(
      new ApiError({ code: "artifact_not_found", status: 404 }),
    );
    renderDetail(client);
    expect(await screen.findByText("本地模型已删除")).toBeVisible();
    expect(screen.getByRole("button", { name: "下载模型" })).toBeVisible();
  });

  it("offers retry and a way back when the detail cannot be read", async () => {
    const client = clientWith(detail());
    const read = vi
      .spyOn(client, "artifactDetail")
      .mockRejectedValueOnce(new Error("down"))
      .mockResolvedValue(detail());
    const user = userEvent.setup();
    renderDetail(client);
    expect(await screen.findByText("无法获取本地模型详情")).toBeVisible();
    expect(screen.getByRole("button", { name: "返回本地模型" })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "重试" }));
    expect(
      await screen.findByRole("heading", { level: 1, name: "Qwen3" }),
    ).toBeVisible();
    expect(read).toHaveBeenCalledTimes(2);
  });
});

describe("UI-31 /models?tab= selects the 模型 page tab", () => {
  function renderModelsAt(entry: string) {
    const client = new FakeApiClient(ADMIN_ME);
    vi.spyOn(client, "downloadStatusCounts").mockResolvedValue({
      counts: {},
    } as never);
    vi.spyOn(client, "getArtifactVerificationStatus").mockResolvedValue({
      job_id: null,
      status: null,
      error_code: null,
    });
    return render(
      <QueryClientProvider
        client={
          new QueryClient({ defaultOptions: { queries: { retry: false } } })
        }
      >
        <MemoryRouter initialEntries={[entry]}>
          <ModelsPage apiClient={client} />
        </MemoryRouter>
      </QueryClientProvider>,
    );
  }

  it("opens the 本地模型 tab for ?tab=local-models and keeps the param", async () => {
    renderModelsAt("/models?tab=local-models");
    expect(
      await screen.findByRole("tab", { name: "本地模型", selected: true }),
    ).toBeVisible();
    expect(screen.queryByText("筛选条件已修正")).toBeNull();
  });

  it("still defaults to 模型实例 without or with an unknown tab", async () => {
    const first = renderModelsAt("/models");
    expect(
      await screen.findByRole("tab", { name: "模型实例", selected: true }),
    ).toBeVisible();
    first.unmount();
    renderModelsAt("/models?tab=other");
    expect(
      await screen.findByRole("tab", { name: "模型实例", selected: true }),
    ).toBeVisible();
  });
});

function Location() {
  const location = useLocation();
  return (
    <output data-testid="location">
      {location.pathname}
      {location.search}
    </output>
  );
}

function renderDetail(client: FakeApiClient) {
  const queryClient = new QueryClient({
    defaultOptions: { mutations: { retry: false }, queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[`/models/local/${ARTIFACT_ID}`]}>
        <Routes>
          <Route
            element={<LocalModelDetailPage apiClient={client} />}
            path="/models/local/:artifactId"
          />
          <Route element={null} path="*" />
        </Routes>
        <Location />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function clientWith(item: ModelArtifactDetail) {
  const client = new FakeApiClient(ADMIN_ME);
  vi.spyOn(client, "artifactDetail").mockResolvedValue(item);
  vi.spyOn(client, "getArtifactVerificationStatus").mockResolvedValue({
    job_id: null,
    status: null,
    error_code: null,
  });
  client.modelInstanceResults.push({
    items: [{ id: INSTANCE_ID, tensor_parallel_size: 2 }] as never,
  });
  client.aiServiceResults.push({
    items: [
      { id: SERVICE_ID, name: "对话服务", primary_instance_id: INSTANCE_ID },
    ] as never,
  });
  return client;
}

function detail(overrides: Record<string, unknown> = {}) {
  return parseModelArtifactDetail({
    created_at: "2026-09-11T00:00:00Z",
    display_name: "Qwen3",
    download_tasks: [
      {
        completed_at: "2026-09-11T00:10:00Z",
        created_at: "2026-09-11T00:00:00Z",
        id: "0198f58a-2586-7e37-bbf5-fefe165e8099",
        status: "Completed",
      },
    ],
    files: [{ path: "config.json", sha256: SHA, size_bytes: 1024 }],
    id: ARTIFACT_ID,
    instances: [{ id: INSTANCE_ID, name: "qwen3-prod", status: "Running" }],
    last_verified_at: "2026-09-11T00:00:00Z",
    model: {
      architecture: "Qwen3ForCausalLM",
      context_length: 32768,
      license: null,
      modalities: ["文本"],
      precision: "bf16",
      quantization: null,
    },
    model_id: MODEL_ID,
    path: "/models/qwen3",
    referencing_instances: 1,
    repository_id: "Qwen/Qwen3",
    resolved_revision: "abc123def456abc123def456",
    size_bytes: 10_485_760,
    source: "Hugging Face",
    status: "Ready",
    storage_type: "Managed",
    updated_at: "2026-09-11T01:00:00Z",
    ...overrides,
  });
}

// FX-M2-07D: existing UI-31 artifact fixture with M2-07C External fields.
describe("M2_07D external artifact detail", () => {
  const external = (extra: Record<string, unknown> = {}) =>
    detail({
      source: "Local Path",
      storage_type: "External",
      resolved_revision: null,
      ...extra,
    });
  it("M2_07D removes only registration and keeps the external completion page", async () => {
    const client = clientWith(
      external({ referencing_instances: 0, instances: [], download_tasks: [] }),
    );
    const remove = vi
      .spyOn(client, "deleteModelArtifact")
      .mockResolvedValue({ id: ARTIFACT_ID, queued: false });
    const { container } = renderDetail(client);
    expect(
      await screen.findByText("无（外部目录注册，不经下载）"),
    ).toBeVisible();
    expect(screen.getByText("外部目录没有 Revision")).toBeVisible();
    const revision = screen.getByText("Revision").nextElementSibling;
    expect(revision).toHaveTextContent("—");
    await userEvent.click(screen.getByRole("button", { name: "移除登记" }));
    const dialog = await screen.findByRole("dialog", {
      name: "移除本地模型「Qwen3」的登记？",
    });
    expect(
      within(dialog).getByText(
        "只移除平台里的这条登记，不删除服务器上的文件（/models/qwen3）。",
      ),
    ).toBeVisible();
    expect(within(dialog).getByText("历史用量保留。")).toBeVisible();
    expect(within(dialog).queryByRole("textbox")).toBeNull();
    expect(
      (
        await axe.run(dialog, {
          rules: { "color-contrast": { enabled: false } },
        })
      ).violations,
    ).toEqual([]);
    await userEvent.click(
      within(dialog).getByRole("button", { name: "确认移除登记" }),
    );
    expect(remove).toHaveBeenCalledWith(ARTIFACT_ID);
    expect(await screen.findByText("本地模型已移除登记")).toBeVisible();
    expect(
      screen.getByText(
        "「Qwen3」已从平台移除登记，服务器上的文件没有改动。要再用，可在「下载模型」重新注册这个目录。",
      ),
    ).toBeVisible();
    expect(container).not.toHaveTextContent("可读");
  });
  it.each([false, true])(
    "M2_07D shows referencing instances (409 conflict: %s)",
    async (conflict) => {
      const client = clientWith(
        external(conflict ? { referencing_instances: 0, instances: [] } : {}),
      );
      const remove = vi
        .spyOn(client, "deleteModelArtifact")
        .mockImplementation(() => {
          vi.spyOn(client, "artifactDetail").mockResolvedValue(external());
          return Promise.reject(
            new ApiError({ code: "artifact_referenced", status: 409 }),
          );
        });
      renderDetail(client);
      expect(
        await screen.findByRole("link", { name: "下载完成" }),
      ).toHaveAttribute("href", "/models?tab=local-models");
      await userEvent.click(screen.getByRole("button", { name: "移除登记" }));
      if (conflict)
        await userEvent.click(
          screen.getByRole("button", { name: "确认移除登记" }),
        );
      const dialog = await screen.findByRole("dialog", {
        name: "还不能移除「Qwen3」的登记",
      });
      expect(
        within(dialog).getByText("仍有 1 个模型实例在用这个目录。"),
      ).toBeVisible();
      expect(
        within(dialog).getByText("先删除下面这些实例，再回来移除登记。"),
      ).toBeVisible();
      expect(
        await within(dialog).findByRole("link", { name: "qwen3-prod" }),
      ).toBeVisible();
      expect(
        within(dialog).queryByRole("button", { name: "确认移除登记" }),
      ).toBeNull();
      expect(remove).toHaveBeenCalledTimes(conflict ? 1 : 0);
    },
  );
});
