import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import axe from "axe-core";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useLocation } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, type ApiClient } from "../api/client";
import {
  DOWNLOAD_TASK_STATUSES,
  ModelAcquisitionContractError,
  parseDownloadCommandResponse,
  parseDownloadTaskPage,
  parseModelArtifactDetail,
  parseModelArtifactPage,
  type DownloadCommandResponse,
  type DownloadTaskPage,
  type DownloadTaskStatus,
  type ModelArtifactPage,
} from "../api/model-acquisition-contracts";
import {
  ADMIN_ME,
  FakeApiClient,
  renderApplication,
} from "../app/test-support";
import { AppErrorBoundary } from "../app/error-boundary";
import { ModelsPage } from "./model-page";
import {
  ARTIFACT_POLL_INTERVAL_MS,
  DOWNLOAD_POLL_INTERVAL_MS,
} from "./queries";

const APP_CSS_PATH = resolve(
  dirname(fileURLToPath(import.meta.url)),
  "../styles/app.css",
);

// FX-UI-39: synthetic long names derived from the local-model fixtures below.
it("UI-39 preserves full local-model and download names in text and hover titles", async () => {
  const name = "Organization/LongModelName".repeat(6);
  renderModels(
    clientWith(
      downloadPage([download({ repository_id: name })]),
      artifactPage([artifact({ display_name: name })]),
    ),
  );
  const artifacts = await findTable("本地模型");
  const link = await within(artifacts).findByRole("link", { name });
  expect(link).toHaveAttribute("title", name);
  expect(link).toHaveAccessibleName(name);
  const downloads = await findTable("下载任务");
  const label = await within(downloads).findByText(name);
  expect(label).toHaveAttribute("title", name);
  expect(label).toHaveTextContent(name);
});

afterEach(() => {
  vi.useRealTimers();
});

describe("M2-10C / TC-DL-09 manual verification", () => {
  it("offers 重新校验 for Ready, Corrupted and Missing rows in the actions column (UI-30 2a)", async () => {
    const client = clientWith(
      downloadPage([]),
      artifactPage([
        artifact(),
        artifact({
          id: "53000000-0000-4000-8000-000000000002",
          status: "Corrupted",
          storage_type: "External",
        }),
        artifact({
          id: "53000000-0000-4000-8000-000000000003",
          status: "Missing",
          storage_type: "External",
        }),
      ]),
    );
    const { container } = renderModels(client);
    const table = await findTable("本地模型");
    const actions = await within(table).findAllByRole("button", {
      name: "重新校验",
    });
    expect(actions).toHaveLength(3);
    expect(within(table).getByText("需在服务器上恢复路径")).toBeVisible();
    for (const action of actions)
      expect(action.closest("td")?.getAttribute("data-column")).toBe("actions");
    expect(
      (
        await axe.run(container, {
          rules: { "color-contrast": { enabled: false } },
        })
      ).violations,
    ).toEqual([]);
  });

  it("queues once, prevents duplicate clicks, and restores queued state on mount", async () => {
    const client = clientWith(downloadPage([]), artifactPage([artifact()]));
    const verification = vi.spyOn(client, "getArtifactVerificationStatus");
    verification
      .mockResolvedValueOnce({ job_id: null, status: null, error_code: null })
      .mockResolvedValue({
        job_id: ARTIFACT_ID,
        status: "Queued",
        error_code: null,
      });
    const submit = vi
      .spyOn(client, "verifyModelArtifact")
      .mockResolvedValue({ id: ARTIFACT_ID, queued: true });
    const user = userEvent.setup();
    renderModels(client);
    const button = await screen.findByRole("button", { name: "重新校验" });
    await waitFor(() => expect(button).toBeEnabled());
    await user.click(button);
    expect(await screen.findByText("校验已排队")).toBeVisible();
    expect(button).toBeDisabled();
    await user.click(button);
    expect(submit).toHaveBeenCalledOnce();
  });

  it("refreshes each of two completed Jobs even when the second finishes before polling", async () => {
    const client = clientWith(downloadPage([]), artifactPage([artifact()]));
    const list = vi
      .spyOn(client, "getModelArtifacts")
      .mockResolvedValue(artifactPage([artifact()]));
    const status = vi
      .spyOn(client, "getArtifactVerificationStatus")
      .mockResolvedValue({
        job_id: ARTIFACT_ID,
        status: "Succeeded",
        error_code: null,
      });
    vi.spyOn(client, "verifyModelArtifact").mockImplementation(() => {
      status.mockResolvedValue({
        job_id: "53000000-0000-4000-8000-000000000099",
        status: "Succeeded",
        error_code: null,
      });
      return Promise.resolve({ id: ARTIFACT_ID, queued: true as const });
    });
    const user = userEvent.setup();
    renderModels(client);
    await waitFor(() => {
      expect(list).toHaveBeenCalledTimes(2);
    });
    await user.click(await screen.findByRole("button", { name: "重新校验" }));
    await waitFor(() => {
      expect(list).toHaveBeenCalledTimes(3);
    });
  });

  it.each(["Queued", "Running"] as const)(
    "restores persisted %s on refresh",
    async (state) => {
      const client = clientWith(downloadPage([]), artifactPage([artifact()]));
      vi.spyOn(client, "getArtifactVerificationStatus").mockResolvedValue({
        job_id: ARTIFACT_ID,
        status: state,
        error_code: null,
      });
      renderModels(client);
      expect(
        await screen.findByText(state === "Queued" ? "校验已排队" : "正在校验"),
      ).toBeVisible();
      expect(screen.getByRole("button", { name: "重新校验" })).toBeDisabled();
    },
  );

  it.each(["Succeeded", "Failed"] as const)(
    "shows persisted %s without continuing active polling",
    async (state) => {
      const client = clientWith(downloadPage([]), artifactPage([artifact()]));
      const list = vi
        .spyOn(client, "getModelArtifacts")
        .mockResolvedValue(artifactPage([artifact()]));
      vi.spyOn(client, "getArtifactVerificationStatus").mockResolvedValue({
        job_id: ARTIFACT_ID,
        status: state,
        error_code: state === "Failed" ? "artifact_verify_failed" : null,
      });
      renderModels(client);
      expect(
        await screen.findByText(
          state === "Failed"
            ? /校验执行失败/
            : "校验已完成，请查看本地模型状态",
        ),
      ).toBeVisible();
      const calls = vi.spyOn(client, "getArtifactVerificationStatus").mock.calls
        .length;
      const listCalls = list.mock.calls.length;
      await new Promise((resolve) => setTimeout(resolve, 1200));
      expect(list).toHaveBeenCalledTimes(listCalls);
      expect(
        vi.spyOn(client, "getArtifactVerificationStatus"),
      ).toHaveBeenCalledTimes(calls);
    },
  );
});

describe("M7-10A / TC-DL-14 managed Artifact deletion", () => {
  it("confirms directly without a typed model name and shows a Worker move failure", async () => {
    const apiClient = clientWith(downloadPage([]), artifactPage([artifact()]));
    const deleteArtifact = vi
      .spyOn(apiClient, "deleteModelArtifact")
      .mockResolvedValue({ id: ARTIFACT_ID, queued: true });
    vi.spyOn(apiClient, "getArtifactDeletionStatus").mockResolvedValue({
      status: "Failed",
      error_code: "artifact_delete_move_failed",
    });
    const user = userEvent.setup();
    const { container } = renderModels(apiClient);
    const table = await findTable("本地模型");
    await user.click(
      await within(table).findByRole("button", { name: "删除文件" }),
    );
    const confirm = screen.getByRole("button", { name: "确认删除文件" });
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(confirm).toBeEnabled();
    await user.click(confirm);
    await waitFor(() => {
      expect(deleteArtifact).toHaveBeenCalledOnce();
    });
    expect(deleteArtifact).toHaveBeenCalledWith(ARTIFACT_ID);
    expect(
      await screen.findByText(/文件移动失败，本地模型仍可用/),
    ).toBeVisible();
    const result = await axe.run(container, {
      rules: { "color-contrast": { enabled: false } },
    });
    expect(result.violations).toEqual([]);
  });

  it("omits file deletion for External and lists referencing instances instead of deleting (UI-30)", async () => {
    const referenced = artifact({ id: uuid(52), referencing_instances: 1 });
    const client = clientWith(
      downloadPage([]),
      artifactPage([
        artifact({ id: uuid(51), storage_type: "External" }),
        referenced,
      ]),
    );
    const detail = vi.spyOn(client, "artifactDetail").mockResolvedValue(
      parseModelArtifactDetail({
        ...referenced,
        download_tasks: [],
        files: null,
        instances: [{ id: uuid(70), name: "qwen3-prod", status: "Running" }],
        model: {
          architecture: null,
          context_length: null,
          license: null,
          modalities: null,
          precision: null,
          quantization: null,
        },
      }),
    );
    const user = userEvent.setup();
    renderModels(client);
    const table = await findTable("本地模型");
    await within(table).findAllByRole("button", { name: "删除文件" });
    const rows = within(table).getAllByRole("row");
    const externalRow = rows[1];
    const referencedRow = rows[2];
    if (externalRow === undefined || referencedRow === undefined) {
      throw new Error("expected External and referenced Managed rows");
    }
    expect(
      within(externalRow).queryByRole("button", { name: "删除文件" }),
    ).not.toBeInTheDocument();
    await user.click(
      within(referencedRow).getByRole("button", { name: "删除文件" }),
    );
    const dialog = await screen.findByRole("dialog");
    expect(detail).toHaveBeenCalledWith(uuid(52));
    expect(within(dialog).getByText(/仍有 1 个模型实例引用/)).toBeVisible();
    expect(
      await within(dialog).findByRole("link", { name: "qwen3-prod" }),
    ).toHaveAttribute("href", `/models/instances/${uuid(70)}`);
    expect(within(dialog).getByText("运行中")).toBeVisible();
    expect(
      within(dialog).queryByRole("button", { name: "确认删除文件" }),
    ).not.toBeInTheDocument();
  });

  it("lets a Corrupted or Missing Managed Artifact be deleted and hints at re-verifying first", async () => {
    renderModels(
      clientWith(
        downloadPage([]),
        artifactPage([
          artifact({ id: uuid(81), status: "Corrupted" }),
          artifact({ id: uuid(82), status: "Missing" }),
        ]),
      ),
    );
    const table = await findTable("本地模型");
    expect(
      await within(table).findAllByRole("button", { name: "删除文件" }),
    ).toHaveLength(2);
    expect(
      within(table).getAllByText("先重新校验，仍未通过可删除后重新下载"),
    ).toHaveLength(2);
  });
});

describe("M2_12B keeps Download Tasks and local models separate", () => {
  it("renders no download state in the Ready local-model row with the same model_id", async () => {
    const apiClient = clientWith(
      downloadPage([download({ model_id: MODEL_ID, status: "Downloading" })]),
      artifactPage([artifact({ model_id: MODEL_ID, status: "Ready" })]),
    );
    const { container } = renderModels(apiClient);
    await screen.findByText("下载中");
    expect(() => {
      validateSeparatedLocalRows(container);
    }).not.toThrow();
  });

  it("positive control rejects download progress injected into a local-model row", async () => {
    const apiClient = clientWith(downloadPage([]), artifactPage([artifact()]));
    const { container } = renderModels(apiClient);
    await screen.findByText("Qwen3");
    const table = await findTable("本地模型");
    within(table)
      .getByText("Qwen3")
      .closest("tr")
      ?.insertAdjacentHTML("beforeend", "<td>下载中 42% 82 MB/s</td>");
    expect(() => {
      validateSeparatedLocalRows(container);
    }).toThrow(/download state leaked/);
  });
});

describe("M2_12B TC-UI-08 response-to-DOM missing semantics", () => {
  it("positive control rejects null storage availability collapsed to zero", async () => {
    const parsed = artifactPage([], { storage_available_bytes: null });
    const { container } = renderModels(clientWith(downloadPage([]), parsed));
    await screen.findByText("读不到模型存储目录可用空间");
    const card = await summaryCard("模型存储目录可用空间");
    expect(within(card).getByLabelText("暂无数据")).toHaveTextContent("—");
    within(card).getByLabelText("暂无数据").textContent = "0";
    expect(() => {
      validateUnavailableCard(container, "模型存储目录可用空间");
    }).toThrow(/collapsed to zero/);
  });

  it("positive control rejects null referencing_instances collapsed to zero", async () => {
    const parsed = artifactPage([artifact({ referencing_instances: null })]);
    const { container } = renderModels(clientWith(downloadPage([]), parsed));
    await screen.findByText("实例数据暂不可用");
    const table = await findTable("本地模型");
    const row = within(table).getByText("Qwen3").closest("tr");
    expect(row).not.toBeNull();
    const mark = within(row as HTMLElement).getByLabelText("暂无数据");
    expect(mark).toHaveTextContent("—");
    mark.textContent = "0";
    expect(() => {
      validateUnavailableReference(container);
    }).toThrow(/collapsed to zero/);
  });

  it("TC-DL-08 positive control rejects a percentage for null total_bytes", async () => {
    const parsed = downloadPage([
      download({
        downloaded_bytes: 4096,
        status: "Downloading",
        total_bytes: null,
      }),
    ]);
    const { container } = renderModels(clientWith(parsed, artifactPage([])));
    const progress = await screen.findByRole("progressbar", {
      name: "不确定进度",
    });
    expect(screen.getByText(/不确定进度 · 已下载/)).toBeVisible();
    progress.setAttribute("aria-label", "下载进度 0%");
    expect(() => {
      validateIndeterminateProgress(container);
    }).toThrow(/invented percentage/);
  });

  it("positive control rejects a true zero managed_bytes rendered as unavailable", async () => {
    const parsed = artifactPage([], {
      active_download_tasks: 0,
      managed_bytes: 0,
    });
    const { container } = renderModels(clientWith(downloadPage([]), parsed));
    await screen.findByText("0 B");
    const card = await summaryCard("本地模型占用");
    expect(within(card).getByText("0 B")).toBeVisible();
    expect(
      within(await summaryCard("进行中的下载任务数")).getByText("0"),
    ).toBeVisible();
    within(card).getByText("0 B").textContent = "—";
    expect(() => {
      validateTrueZero(container, "本地模型占用");
    }).toThrow(/true zero disappeared/);
  });

  it("renders unresolved Revision and writes no 临时文件 line when staging_path is null", async () => {
    renderModels(
      clientWith(
        downloadPage([download({ revision: null, staging_path: null })]),
        artifactPage([]),
      ),
    );
    expect(await screen.findByText("未解析")).toBeVisible();
    expect(screen.queryByText(/临时文件/)).not.toBeInTheDocument();
    expect(screen.queryByText("已清理")).not.toBeInTheDocument();
  });
});

describe("M2_12B component speed and ETA", () => {
  it("shows speed only after a second server-timestamped observation", async () => {
    const apiClient = clientWith(downloadPage([download()]), artifactPage([]));
    apiClient.downloadTaskResults.push(
      downloadPage([
        download({
          downloaded_bytes: 3_145_728,
          updated_at: "2026-09-11T01:00:02Z",
        }),
      ]),
    );
    const { queryClient } = renderModels(apiClient);
    await screen.findByText("下载中");
    expect(screen.queryByText("1 MB/s")).not.toBeInTheDocument();
    await queryClient.refetchQueries({ queryKey: ["models", "downloads"] });
    expect(await screen.findByText("1 MB/s")).toBeVisible();
    expect(screen.getByText("7 秒")).toBeVisible();
  });
});

describe("M2_12B server-provided row commands", () => {
  it("positive control renders only cancel from available_commands and confirms cleanup", async () => {
    const apiClient = clientWith(
      downloadPage([download({ available_commands: ["cancel"] })]),
      artifactPage([]),
    );
    renderModels(apiClient);
    await screen.findByRole("button", { name: "取消" });
    const table = await findTable("下载任务");
    const row = within(table)
      .getByRole("button", { name: "取消" })
      .closest("tr");
    expect(row).not.toBeNull();
    expect(() => {
      validateOnlyCommands(row as HTMLElement, ["取消"]);
    }).not.toThrow();
    const brokenRow = (row as HTMLElement).cloneNode(true) as HTMLElement;
    brokenRow.insertAdjacentHTML("beforeend", "<button>暂停</button>");
    expect(() => {
      validateOnlyCommands(brokenRow, ["取消"]);
    }).toThrow(/unexpected command/);
    await userEvent.click(within(table).getByRole("button", { name: "取消" }));
    expect(screen.getByText(/将清理临时文件/)).toBeVisible();
    expect(screen.queryByText(/保留临时文件/)).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "确认取消" }));
    await waitFor(() => {
      expect(apiClient.downloadCommandCalls).toEqual([
        { command: "cancel", id: DOWNLOAD_ID },
      ]);
      expect(apiClient.downloadTaskCursors.length).toBeGreaterThan(1);
      expect(apiClient.modelArtifactCalls.length).toBeGreaterThan(1);
    });
  });

  it("keeps each row disabled while concurrent commands for different tasks are pending", async () => {
    const firstId = uuid(61);
    const secondId = uuid(62);
    const items = [
      download({
        available_commands: ["pause"],
        id: firstId,
        repository_id: "Qwen/First",
      }),
      download({
        available_commands: ["pause"],
        id: secondId,
        repository_id: "Qwen/Second",
      }),
    ];
    const apiClient = clientWith(downloadPage(items), artifactPage([]));
    const first = deferred<DownloadCommandResponse>();
    const second = deferred<DownloadCommandResponse>();
    vi.spyOn(apiClient, "executeDownloadCommand").mockImplementation((id) =>
      id === firstId ? first.promise : second.promise,
    );
    apiClient.downloadTaskResults.push(
      downloadPage(items),
      downloadPage(items),
    );
    apiClient.modelArtifactResults.push(artifactPage([]), artifactPage([]));
    renderModels(apiClient);
    const firstRow = (await screen.findByText("Qwen/First")).closest("tr");
    const secondRow = screen.getByText("Qwen/Second").closest("tr");
    expect(firstRow).not.toBeNull();
    expect(secondRow).not.toBeNull();
    const firstButton = within(firstRow as HTMLElement).getByRole("button", {
      name: "暂停",
    });
    const secondButton = within(secondRow as HTMLElement).getByRole("button", {
      name: "暂停",
    });

    await userEvent.click(firstButton);
    await waitFor(() => expect(firstButton).toBeDisabled());
    expect(secondButton).toBeEnabled();
    await userEvent.click(secondButton);
    await waitFor(() => {
      expect(firstButton).toBeDisabled();
      expect(secondButton).toBeDisabled();
    });

    first.resolve(commandResponse(firstId));
    second.resolve(commandResponse(secondId));
    await waitFor(() => {
      expect(firstButton).toBeEnabled();
      expect(secondButton).toBeEnabled();
    });
  });

  it("shows the server message and Request ID after a command failure", async () => {
    const apiClient = clientWith(
      downloadPage([download({ available_commands: ["pause"] })]),
      artifactPage([]),
    );
    apiClient.downloadCommandErrors.push(
      new ApiError({
        code: "download_command_not_allowed",
        requestId: "request-m2-12b",
        responseMessage:
          "当前状态不允许执行此操作，下载任务未改变，请刷新状态后重试",
        status: 409,
      }),
    );
    apiClient.downloadTaskResults.push(
      downloadPage([download({ available_commands: ["pause"] })]),
    );
    apiClient.modelArtifactResults.push(artifactPage([]));
    renderModels(apiClient);
    await userEvent.click(await screen.findByRole("button", { name: "暂停" }));
    expect(await screen.findByText(/当前状态不允许执行此操作/)).toBeVisible();
    expect(screen.getByText("Request ID：request-m2-12b")).toBeVisible();
    await waitFor(() => {
      expect(apiClient.downloadTaskCursors.length).toBeGreaterThan(1);
      expect(apiClient.modelArtifactCalls.length).toBeGreaterThan(1);
    });
  });

  it("hands an invalid command response to the stable error boundary", async () => {
    const apiClient = clientWith(
      downloadPage([download({ available_commands: ["pause"] })]),
      artifactPage([]),
    );
    vi.spyOn(apiClient, "executeDownloadCommand").mockImplementation(() =>
      Promise.resolve().then(() =>
        parseDownloadCommandResponse({
          ...commandResponse(DOWNLOAD_ID),
          status: "FutureStatus",
        }),
      ),
    );
    const queryClient = new QueryClient({
      defaultOptions: {
        mutations: { retry: false },
        queries: { retry: false },
      },
    });
    render(
      <AppErrorBoundary>
        <QueryClientProvider client={queryClient}>
          <MemoryRouter>
            <ModelsPage apiClient={apiClient} defaultTab="local-models" />
          </MemoryRouter>
        </QueryClientProvider>
      </AppErrorBoundary>,
    );

    await userEvent.click(await screen.findByRole("button", { name: "暂停" }));
    expect(
      await screen.findByRole("heading", { name: "页面暂时无法显示" }),
    ).toBeVisible();
    expect(screen.getByText("model_acquisition_contract_error")).toBeVisible();
  });

  it("offers 继续 and 删除任务 but no 重新下载 for a failed task even when the server lists retry", async () => {
    const apiClient = clientWith(
      downloadPage([
        download({
          available_commands: ["resume", "retry"],
          error_code: "download_process_failed",
          failure_stage: "Downloading",
          status: "Failed",
        }),
      ]),
      artifactPage([]),
    );
    renderModels(apiClient);
    const table = await findTable("下载任务");
    expect(
      await within(table).findByRole("button", { name: "继续" }),
    ).toBeVisible();
    expect(
      within(table).getByRole("button", { name: "删除任务" }),
    ).toBeVisible();
    expect(
      within(table).queryByRole("button", { name: "重新下载" }),
    ).not.toBeInTheDocument();
    await userEvent.click(within(table).getByRole("button", { name: "继续" }));
    await waitFor(() => {
      expect(apiClient.downloadCommandCalls).toEqual([
        { command: "resume", id: DOWNLOAD_ID },
      ]);
    });
  });
});

describe("M2_12B failure display", () => {
  it("positive control keeps Cancelled duplicate resolution idle and hides internal cause", async () => {
    const { client } = groupedClient(
      [
        download({
          available_commands: [],
          error_code: "duplicate_resolved_revision",
          failure_stage: "Resolving",
          status: "Cancelled",
        }),
      ],
      artifactPage([]),
    );
    renderModels(client);
    await userEvent.click(await screen.findByRole("button", { name: "展开" }));
    const row = (await screen.findByText("取消时已下载 1 MB")).closest("tr");
    expect(row).not.toBeNull();
    expect(() => {
      validateCancelledRow(row as HTMLElement);
    }).not.toThrow();
    const brokenRow = (row as HTMLElement).cloneNode(true) as HTMLElement;
    brokenRow.insertAdjacentHTML(
      "beforeend",
      "<td>失败阶段：读取模型信息</td>",
    );
    expect(() => {
      validateCancelledRow(brokenRow);
    }).toThrow(/cancelled row rendered as failure/);
  });

  it("renders an uncertain process failure without deterministic attribution", async () => {
    renderModels(
      clientWith(
        downloadPage([
          download({
            error_code: "download_process_failed",
            failure_stage: "Downloading",
            status: "Failed",
          }),
        ]),
        artifactPage([]),
      ),
    );
    const failure = await screen.findByText("下载进程失败");
    const row = failure.closest("tr");
    expect(row).not.toBeNull();
    expect(row).toHaveTextContent("原因无法区分");
    expect(row).not.toHaveTextContent("重新下载");
    expect(
      within(row as HTMLElement).getByRole("link", {
        name: "前往「系统 › 模型源」",
      }),
    ).toHaveAttribute("href", "/system?tab=model-sources");
    expect(() => {
      validateUncertainProcessFailure(row as HTMLElement);
    }).not.toThrow();
    const brokenRow = (row as HTMLElement).cloneNode(true) as HTMLElement;
    brokenRow.insertAdjacentHTML("beforeend", "<td>网络错误</td>");
    expect(() => {
      validateUncertainProcessFailure(brokenRow);
    }).toThrow(/deterministic attribution/);
  });
});

describe("M2_12B polling, URL filters, and opaque cursors", () => {
  it("retains successful data after refresh failure and labels client receipt time", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.setSystemTime("2026-09-11T02:00:00Z");
    const apiClient = clientWith(
      downloadPage([download()]),
      artifactPage([artifact()]),
    );
    const { queryClient } = renderModels(apiClient);
    await screen.findByText("Qwen3");
    apiClient.downloadTaskResults.push(new Error("poll failed"));
    vi.setSystemTime("2026-09-11T02:00:05Z");
    await queryClient.refetchQueries({ queryKey: ["models", "downloads"] });
    expect(await screen.findByText("下载任务数据可能延迟")).toBeVisible();
    expect(screen.getAllByText("Qwen/Qwen3").length).toBeGreaterThan(0);
    const stale = screen.getByText(/最近成功刷新/).closest('[role="status"]');
    expect(stale?.querySelector("time")).toHaveAttribute(
      "datetime",
      "2026-09-11T02:00:00.000Z",
    );
    expect(DOWNLOAD_POLL_INTERVAL_MS).toBe(5_000);
    expect(ARTIFACT_POLL_INTERVAL_MS).toBe(10_000);
  });

  it("clears unsupported URL values visibly and never sends them", async () => {
    const apiClient = clientWith(downloadPage([]), artifactPage([]));
    renderModels(apiClient, "/models?source=FutureSource&unexpected=value");
    expect(await screen.findByText("筛选条件已修正")).toBeVisible();
    expect(screen.getByTestId("location-search")).toHaveTextContent("/models");
    expect(apiClient.modelArtifactCalls[0]).toEqual({});
  });

  it("M2_13B / TC-UI-11 stores filters in the URL and shows search no-results", async () => {
    const apiClient = clientWith(downloadPage([]), artifactPage([]));
    renderModels(apiClient);
    await screen.findByText("还没有本地模型");
    await userEvent.type(screen.getByLabelText("搜索模型名称"), "Qwen");
    await userEvent.selectOptions(screen.getByLabelText("来源"), "ModelScope");
    await userEvent.selectOptions(screen.getByLabelText("状态"), "Ready");
    expect(screen.queryByLabelText("存储类型")).not.toBeInTheDocument();
    expect(screen.getByTestId("location-search")).toHaveTextContent(
      "/models?search=Qwen&source=ModelScope&status=Ready",
    );
    await waitFor(() => {
      expect(apiClient.modelArtifactCalls).toContainEqual({
        search: "Qwen",
        source: "ModelScope",
        status: "Ready",
      });
    });
    expect(await screen.findByText("没有匹配结果")).toBeVisible();
    expect(screen.getByRole("button", { name: "清除筛选" })).toBeVisible();
    const noResults = screen.getByText("没有匹配结果").closest("section");
    expect(noResults).not.toBeNull();
    expect(
      within(noResults as HTMLElement).queryByRole("button", {
        name: "下载模型",
      }),
    ).not.toBeInTheDocument();
  });

  it("UI-30 ignores the retired storage_type URL parameter with the visible correction notice", async () => {
    const apiClient = clientWith(downloadPage([]), artifactPage([]));
    renderModels(apiClient, "/models?storage_type=Managed&status=Ready");
    expect(await screen.findByText("筛选条件已修正")).toBeVisible();
    expect(screen.getByTestId("location-search")).toHaveTextContent(
      "/models?status=Ready",
    );
    expect(apiClient.modelArtifactCalls[0]).toEqual({ status: "Ready" });
  });

  it("UI-30 filters 未被引用 on the loaded rows without sending it to the server", async () => {
    const apiClient = clientWith(
      downloadPage([]),
      artifactPage([
        artifact({ display_name: "Unused-A", id: uuid(91) }),
        artifact({
          display_name: "Used-B",
          id: uuid(92),
          referencing_instances: 2,
        }),
        artifact({
          display_name: "Unknown-C",
          id: uuid(93),
          referencing_instances: null,
        }),
      ]),
    );
    renderModels(apiClient);
    const table = await findTable("本地模型");
    expect(await within(table).findByText("Used-B")).toBeVisible();
    await userEvent.selectOptions(screen.getByLabelText("状态"), "未被引用");
    expect(screen.getByTestId("location-search")).toHaveTextContent(
      "/models?status=unreferenced",
    );
    await waitFor(() => {
      expect(within(table).queryByText("Used-B")).not.toBeInTheDocument();
    });
    expect(within(table).getByText("Unused-A")).toBeVisible();
    expect(within(table).queryByText("Unknown-C")).not.toBeInTheDocument();
    expect(
      apiClient.modelArtifactCalls.every(({ status }) => status === undefined),
    ).toBe(true);
  });

  it("returns the opaque cursor unchanged when loading more", async () => {
    const opaque = "opaque+/cursor==";
    const apiClient = clientWith(
      downloadPage([download()], opaque),
      artifactPage([]),
    );
    apiClient.downloadTaskResults.push(downloadPage([], null));
    renderModels(apiClient);
    await userEvent.click(
      await screen.findByRole("button", { name: "加载更多下载任务" }),
    );
    await waitFor(() => {
      expect(apiClient.downloadTaskCursors).toEqual([undefined, opaque]);
    });
  });

  it("returns the local-model opaque cursor unchanged when loading more", async () => {
    const opaque = "artifact+opaque/cursor==";
    const apiClient = clientWith(
      downloadPage([]),
      artifactPage([artifact()], {}, opaque),
    );
    apiClient.modelArtifactResults.push(artifactPage([]));
    renderModels(apiClient);
    await userEvent.click(
      await screen.findByRole("button", { name: "加载更多本地模型" }),
    );
    await waitFor(() => {
      expect(apiClient.modelArtifactCalls).toEqual([{}, { cursor: opaque }]);
    });
  });

  it("TC-UI-10 announces stages politely, failures assertively, and not progress-only polls", async () => {
    const apiClient = clientWith(downloadPage([download()]), artifactPage([]));
    apiClient.downloadTaskResults.push(
      downloadPage([download({ downloaded_bytes: 2_097_152 })]),
      downloadPage([
        download({
          available_commands: ["resume", "cancel"],
          downloaded_bytes: 2_097_152,
          status: "Paused",
          updated_at: "2026-09-11T01:00:02Z",
        }),
      ]),
      downloadPage([
        download({
          available_commands: ["retry"],
          error_code: "download_process_failed",
          failure_stage: "Downloading",
          status: "Failed",
          updated_at: "2026-09-11T01:00:03Z",
        }),
      ]),
    );
    const { container, queryClient } = renderModels(apiClient);
    await screen.findByText("下载中");
    const polite = container.querySelector('[aria-live="polite"]');
    const assertive = container.querySelector('[aria-live="assertive"]');
    expect(polite).not.toBeNull();
    expect(assertive).not.toBeNull();

    await queryClient.refetchQueries({ queryKey: ["models", "downloads"] });
    expect(polite).toHaveTextContent("");
    expect(assertive).toHaveTextContent("");

    await queryClient.refetchQueries({ queryKey: ["models", "downloads"] });
    await waitFor(() => {
      expect(polite).toHaveTextContent("状态更新为已暂停");
    });
    expect(assertive).toHaveTextContent("");

    await queryClient.refetchQueries({ queryKey: ["models", "downloads"] });
    await waitFor(() => {
      expect(assertive).toHaveTextContent("状态更新为下载失败");
    });
  });
});

describe("M2_13B unique local-model primary action", () => {
  it("renders exactly one primary 下载模型 in the fresh-system empty state", async () => {
    const { container } = renderModels(
      clientWith(downloadPage([]), artifactPage([])),
    );
    await screen.findByText("还没有本地模型");
    const primary = container.querySelectorAll(".pw-button--primary");
    expect(primary).toHaveLength(1);
    expect(primary[0]).toHaveTextContent("下载模型");
    expect(screen.getAllByRole("button", { name: "下载模型" })).toHaveLength(1);
  });

  it("keeps 下载模型 out of search-no-results and renders no unsupported artifact redownload", async () => {
    const { container } = renderModels(
      clientWith(
        downloadPage([]),
        artifactPage([
          artifact({ status: "Corrupted" }),
          artifact({ id: uuid(98), status: "Missing" }),
        ]),
      ),
    );
    await screen.findByText("文件异常");
    const localTable = await findTable("本地模型");
    expect(
      within(localTable).queryByRole("button", { name: "重新下载" }),
    ).not.toBeInTheDocument();
    expect(container.querySelectorAll(".pw-button--primary")).toHaveLength(1);
  });
});

describe("M2_12B state, responsive, and accessibility coverage", () => {
  it("covers every download and artifact status including paused and indeterminate progress", async () => {
    const downloads = DOWNLOAD_TASK_STATUSES.map((status, index) =>
      download({
        available_commands: COMMANDS_BY_STATUS[status],
        error_code: status === "Failed" ? "insufficient_storage" : null,
        failure_stage: status === "Failed" ? "Downloading" : null,
        id: uuid(index + 20),
        status,
        total_bytes: status === "Downloading" ? null : 10_485_760,
      }),
    );
    const { client } = groupedClient(
      downloads,
      artifactPage([
        artifact({ id: uuid(41), status: "Ready" }),
        artifact({ id: uuid(44), status: "Ready", storage_type: "External" }),
        artifact({
          id: uuid(42),
          status: "Missing",
          storage_type: "External",
        }),
        artifact({
          id: uuid(43),
          status: "Corrupted",
          storage_type: "External",
        }),
      ]),
    );
    renderModels(client);
    await userEvent.click(await screen.findByRole("button", { name: "展开" }));
    for (const label of [
      "等待下载",
      "读取模型信息",
      "下载中",
      "已暂停",
      "校验中",
      "下载完成",
      "下载失败",
      "已取消",
      "可用",
      "文件缺失",
      "文件异常",
    ]) {
      expect((await screen.findAllByText(label))[0]).toBeVisible();
    }
    // Publishing is folded into 校验中 (UI-30), so both stages read the same.
    expect(screen.getAllByText("校验中")).toHaveLength(2);
    expect(screen.queryByText("正在注册")).not.toBeInTheDocument();
    expect(screen.getByText("需在服务器上恢复路径")).toBeVisible();
    expect(screen.getByText("需在服务器上修复源目录")).toBeVisible();
    const downloadTable = await findTable("下载任务");
    for (const unsupportedAction of ["部署", "详情", "重新下载"]) {
      expect(
        within(downloadTable).queryByRole("button", {
          name: unsupportedAction,
        }),
      ).not.toBeInTheDocument();
    }
    expect(
      within(downloadTable).getAllByRole("button", { name: "删除任务" }),
    ).toHaveLength(3);
    expect(
      within(downloadTable).getAllByRole("button", { name: "查看本地模型" }),
    ).toHaveLength(1);
    const artifactTable = await findTable("本地模型");
    for (const unsupportedAction of ["详情", "重新下载"]) {
      expect(
        within(artifactTable).queryByRole("button", {
          name: unsupportedAction,
        }),
      ).not.toBeInTheDocument();
    }
    expect(
      within(artifactTable).getAllByRole("button", { name: "删除文件" }),
    ).toHaveLength(1);
    expect(
      screen.getByRole("progressbar", { name: "不确定进度" }),
    ).toBeVisible();
  });

  it("TC-UI-11 covers loading, empty, error, and permission states", async () => {
    const loadingClient = baseClient();
    vi.spyOn(loadingClient, "getDownloadTasks").mockReturnValue(
      new Promise(() => undefined),
    );
    vi.spyOn(loadingClient, "getModelArtifacts").mockReturnValue(
      new Promise(() => undefined),
    );
    const loading = renderModels(loadingClient);
    expect(await screen.findByLabelText("本地模型统计正在加载")).toBeVisible();
    loading.unmount();

    const empty = renderModels(clientWith(downloadPage([]), artifactPage([])));
    expect(await screen.findByText("还没有下载任务")).toBeVisible();
    expect(screen.getByText("还没有本地模型")).toBeVisible();
    empty.unmount();

    const failed = baseClient();
    failed.downloadTaskResults.push(new Error("failed"));
    failed.modelArtifactResults.push(new Error("failed"));
    const error = renderModels(failed);
    expect(await screen.findByText("无法获取下载任务")).toBeVisible();
    expect(screen.getByText("无法获取本地模型")).toBeVisible();
    error.unmount();

    const denied = baseClient();
    denied.downloadTaskResults.push(
      new ApiError({ code: "forbidden", status: 403 }),
    );
    denied.modelArtifactResults.push(
      new ApiError({ code: "forbidden", status: 403 }),
    );
    renderApplication(denied, "/models");
    expect(await screen.findByText("无权限查看本地模型")).toBeVisible();
  });

  it("hands model acquisition contract failures to the stable error boundary", async () => {
    const apiClient = baseClient();
    apiClient.downloadTaskResults.push(new ModelAcquisitionContractError());
    apiClient.modelArtifactResults.push(artifactPage([]));
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    render(
      <AppErrorBoundary>
        <QueryClientProvider client={queryClient}>
          <MemoryRouter>
            <ModelsPage apiClient={apiClient} defaultTab="local-models" />
          </MemoryRouter>
        </QueryClientProvider>
      </AppErrorBoundary>,
    );
    expect(
      await screen.findByRole("heading", { name: "页面暂时无法显示" }),
    ).toBeVisible();
    expect(screen.getByText("model_acquisition_contract_error")).toBeVisible();
  });

  it("TC-UI-10 / UI-30 defines the 1280 px fit, the 1179 px column hiding and reduced-motion progress", () => {
    const css = readFileSync(APP_CSS_PATH, "utf8");
    // Fixed layout replaces the old 1760 px minimum width that forced horizontal scrolling.
    expect(css).toMatch(
      /\.pw-models-stack \.pw-table \{\s*table-layout: fixed;/,
    );
    expect(css).not.toMatch(/\.pw-models-page \.pw-table \{[^}]*min-width/);
    const hiding = css.match(/@media \(max-width: 1179px\) \{([\s\S]*?)\n\}/);
    expect(hiding?.[1]).toContain('data-column="updated"');
    expect(hiding?.[1]).toContain('data-column="verified"');
    expect(css.match(/@media \(max-width: 1179px\)/g)).toHaveLength(1);
    expect(css.match(/@media \(max-width: 767px\)/g)).toHaveLength(1);
    expect(css).toMatch(
      /@media \(prefers-reduced-motion: reduce\)[\s\S]*pw-download-progress__fill\.is-indeterminate/,
    );
  });

  it.each(["normal", "failed"] as const)(
    "TC-UI-01 has non-empty axe passes and no violations: %s",
    async (state) => {
      const apiClient = clientWith(
        downloadPage([
          download(
            state === "failed"
              ? {
                  error_code: "insufficient_storage",
                  failure_stage: "Downloading",
                  status: "Failed",
                }
              : {},
          ),
        ]),
        artifactPage([
          artifact({ status: state === "failed" ? "Missing" : "Ready" }),
        ]),
      );
      const { container } = renderModels(apiClient);
      await screen.findByText(state === "failed" ? "下载失败" : "下载中");
      const result = await axe.run(container, {
        rules: { "color-contrast": { enabled: false } },
      });
      expect(result.passes.flatMap((rule) => rule.nodes)).not.toHaveLength(0);
      expect(result.violations).toEqual([]);
    },
  );
});

describe("UI-30 download task groups", () => {
  const HASH = "3f2a9c1d4e5b60718293a4b5c6d7e8f901234567";

  it("always lists in-progress and failed tasks and folds Completed and Cancelled into a counted group", async () => {
    const tasks = [
      download({ id: uuid(101), repository_id: "Org/Active-1" }),
      download({
        available_commands: ["resume"],
        error_code: "insufficient_storage",
        failure_stage: "Downloading",
        id: uuid(102),
        repository_id: "Org/Failed-1",
        status: "Failed",
      }),
      download({
        available_commands: ["resume", "cancel"],
        id: uuid(103),
        repository_id: "Org/Paused-1",
        status: "Paused",
      }),
      ...Array.from({ length: 9 }, (_, index) =>
        download({
          available_commands: [],
          id: uuid(110 + index),
          repository_id: `Org/Done-${String(index)}`,
          status: "Completed",
        }),
      ),
      ...Array.from({ length: 3 }, (_, index) =>
        download({
          available_commands: [],
          id: uuid(130 + index),
          repository_id: `Org/Cancelled-${String(index)}`,
          status: "Cancelled",
        }),
      ),
    ];
    const { client, getTasks } = groupedClient(tasks, artifactPage([]), 4);
    renderModels(client);
    const table = await findTable("下载任务");
    expect(await within(table).findByText("Org/Active-1")).toBeVisible();
    expect(within(table).getByText("Org/Failed-1")).toBeVisible();
    expect(within(table).getByText("Org/Paused-1")).toBeVisible();
    expect(screen.getByText("进行中 2 · 失败 1 · 已结束 12")).toBeVisible();
    expect(
      screen.getByText("已结束 12 条（下载完成 9、已取消 3）"),
    ).toBeVisible();
    // Collapsed: nothing finished is listed and its query has not been sent.
    expect(within(table).queryByText("Org/Done-0")).not.toBeInTheDocument();
    expect(getTasks.mock.calls.map(([, status]) => status)).toEqual([
      [
        "Queued",
        "Resolving",
        "Downloading",
        "Paused",
        "Verifying",
        "Publishing",
        "Failed",
      ],
    ]);

    const toggle = screen.getByRole("button", { name: "展开" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    await userEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    // First reveal shows the latest 5 even though the server pages by 4.
    await waitFor(() => {
      expect(within(table).getByText("Org/Done-4")).toBeVisible();
    });
    expect(within(table).queryByText("Org/Done-5")).not.toBeInTheDocument();
    expect(getTasks).toHaveBeenCalledWith(undefined, [
      "Completed",
      "Cancelled",
    ]);

    // Each "显示更多" reveals 10 more: 5 + 10 covers all 12.
    await userEvent.click(
      screen.getByRole("button", { name: "显示更多（还有 7 条）" }),
    );
    await waitFor(() => {
      expect(within(table).getByText("Org/Cancelled-2")).toBeVisible();
    });
    expect(
      screen.queryByRole("button", { name: /显示更多/ }),
    ).not.toBeInTheDocument();

    await userEvent.click(toggle);
    expect(within(table).queryByText("Org/Done-0")).not.toBeInTheDocument();
  });

  it("shows a hint instead of the empty state when only finished tasks exist", async () => {
    const { client } = groupedClient(
      [download({ available_commands: [], status: "Completed" })],
      artifactPage([]),
    );
    renderModels(client);
    expect(
      await screen.findByText("没有进行中或失败的下载任务。"),
    ).toBeVisible();
    expect(screen.queryByText("还没有下载任务")).not.toBeInTheDocument();
    expect(
      screen.getByText("已结束 1 条（下载完成 1、已取消 0）"),
    ).toBeVisible();
  });

  it("writes source, short revision and temporary-file size on the model cell", async () => {
    renderModels(
      clientWith(
        downloadPage([
          download({ downloaded_bytes: 3_145_728, revision: HASH }),
        ]),
        artifactPage([]),
      ),
    );
    const table = await findTable("下载任务");
    expect(await within(table).findByText("Qwen/Qwen3")).toBeVisible();
    const revision = within(table).getByText("3f2a9c1");
    expect(revision).toHaveAttribute("title", HASH);
    expect(within(table).getByText("临时文件 3 MB")).toBeVisible();
    for (const header of ["模型 · 来源 · Revision", "状态 / 进度", "更新时间"])
      expect(
        within(table).getByRole("columnheader", { name: header }),
      ).toBeVisible();
    for (const retired of [
      "来源",
      "Repository",
      "Revision",
      "速度",
      "ETA",
      "临时文件",
    ])
      expect(
        within(table).queryByRole("columnheader", { name: retired }),
      ).not.toBeInTheDocument();
  });

  it("folds Publishing into 校验中 with a disabled, explained 取消", async () => {
    const { client } = groupedClient(
      [
        download({
          available_commands: [],
          id: uuid(141),
          repository_id: "Org/Publishing",
          status: "Publishing",
        }),
        download({
          available_commands: ["cancel"],
          id: uuid(142),
          repository_id: "Org/Verifying",
          status: "Verifying",
        }),
      ],
      artifactPage([]),
    );
    renderModels(client);
    const table = await findTable("下载任务");
    const publishing = (
      await within(table).findByText("Org/Publishing")
    ).closest("tr") as HTMLElement;
    expect(within(publishing).getByText("校验中")).toBeVisible();
    expect(
      within(publishing).getByText("校验通过，正在登记为本地模型，即将完成"),
    ).toBeVisible();
    const cancel = within(publishing).getByRole("button", { name: "取消" });
    expect(cancel).toBeDisabled();
    expect(cancel).toHaveAccessibleDescription("即将完成，不能取消");
    expect(cancel).toHaveAttribute("title", "即将完成，不能取消");
    const verifying = within(table)
      .getByText("Org/Verifying")
      .closest("tr") as HTMLElement;
    expect(
      within(verifying).getByRole("button", { name: "取消" }),
    ).toBeEnabled();
  });

  it("offers exactly the PDD 9.3 actions for every status", async () => {
    const statuses = DOWNLOAD_TASK_STATUSES;
    const tasks = statuses.map((status, index) =>
      download({
        available_commands: COMMANDS_BY_STATUS[status],
        id: uuid(150 + index),
        repository_id: `Org/${status}`,
        status,
      }),
    );
    const { client } = groupedClient(tasks, artifactPage([]));
    renderModels(client);
    await userEvent.click(await screen.findByRole("button", { name: "展开" }));
    const table = await findTable("下载任务");
    const expected: Record<DownloadTaskStatus, string[]> = {
      Cancelled: ["删除任务"],
      Completed: ["查看本地模型", "删除任务"],
      Downloading: ["暂停", "取消"],
      Failed: ["继续", "删除任务"],
      Paused: ["继续", "取消"],
      Publishing: ["取消"],
      Queued: ["取消"],
      Resolving: ["取消"],
      Verifying: ["取消"],
    };
    for (const status of statuses) {
      const row = (await within(table).findByText(`Org/${status}`)).closest(
        "tr",
      ) as HTMLElement;
      const cell = row.querySelector('[data-column="actions"]') as HTMLElement;
      expect(
        within(cell)
          .getAllByRole("button")
          .map((button) => button.textContent.trim()),
      ).toEqual(expected[status]);
    }
  });

  it("cancels with the temporary-file cleanup checked by default and sends false when unchecked", async () => {
    const { client } = groupedClient([download()], artifactPage([]));
    const execute = vi
      .spyOn(client, "executeDownloadCommand")
      .mockResolvedValue(commandResponse(DOWNLOAD_ID));
    const user = userEvent.setup();
    renderModels(client);
    const table = await findTable("下载任务");
    await user.click(
      await within(table).findByRole("button", { name: "取消" }),
    );
    const dialog = await screen.findByRole("dialog");
    expect(
      within(dialog).getByText(
        "任务变为「已取消」，任务记录保留 30 天。已发布的本地模型不受影响。",
      ),
    ).toBeVisible();
    const cleanup = within(dialog).getByRole("checkbox", {
      name: "同时清理临时文件（1 MB）",
    });
    expect(cleanup).toBeChecked();
    await user.click(within(dialog).getByRole("button", { name: "确认取消" }));
    await waitFor(() => {
      expect(execute).toHaveBeenLastCalledWith(DOWNLOAD_ID, "cancel", true);
    });

    await user.click(within(table).getByRole("button", { name: "取消" }));
    const reopened = await screen.findByRole("dialog");
    const again = within(reopened).getByRole("checkbox", {
      name: "同时清理临时文件（1 MB）",
    });
    expect(again).toBeChecked();
    await user.click(again);
    expect(again).not.toBeChecked();
    await user.click(
      within(reopened).getByRole("button", { name: "确认取消" }),
    );
    await waitFor(() => {
      expect(execute).toHaveBeenLastCalledWith(DOWNLOAD_ID, "cancel", false);
    });
  });

  it.each([
    ["Completed", "只删除任务记录，已生成的本地模型不受影响。"],
    ["Failed", "只删除任务记录，不会删除任何已发布的本地模型。"],
    ["Cancelled", "只删除任务记录，不会删除任何已发布的本地模型。"],
  ] as const)(
    "deletes a %s task with the right wording, an unchecked temporary-file option and refreshed lists",
    async (status, wording) => {
      const { client, deleteTask, getTasks } = groupedClient(
        [
          download({
            available_commands: [],
            error_code: status === "Failed" ? "download_process_failed" : null,
            failure_stage: status === "Failed" ? "Downloading" : null,
            status,
          }),
        ],
        artifactPage([]),
      );
      const user = userEvent.setup();
      renderModels(client);
      if (status !== "Failed")
        await user.click(await screen.findByRole("button", { name: "展开" }));
      const table = await findTable("下载任务");
      await user.click(
        await within(table).findByRole("button", { name: "删除任务" }),
      );
      const dialog = await screen.findByRole("dialog");
      expect(within(dialog).getByText(wording)).toBeVisible();
      const keep = within(dialog).getByRole("checkbox", {
        name: "同时删除临时文件（1 MB）",
      });
      expect(keep).not.toBeChecked();
      const callsBefore = getTasks.mock.calls.length;
      await user.click(
        within(dialog).getByRole("button", { name: "确认删除任务" }),
      );
      await waitFor(() => {
        expect(deleteTask).toHaveBeenCalledWith(DOWNLOAD_ID, false);
      });
      await waitFor(() => {
        expect(getTasks.mock.calls.length).toBeGreaterThan(callsBefore);
      });
      await waitFor(() => {
        expect(
          within(table).queryByRole("button", { name: "删除任务" }),
        ).not.toBeInTheDocument();
      });
      expect(screen.getByText("还没有下载任务")).toBeVisible();
    },
  );

  it("sends cleanupStaging=true when the delete option is checked and shows a 409 error", async () => {
    const { client, deleteTask } = groupedClient(
      [
        download({
          available_commands: ["resume"],
          error_code: "download_process_failed",
          failure_stage: "Downloading",
          status: "Failed",
        }),
      ],
      artifactPage([]),
    );
    deleteTask.mockRejectedValueOnce(
      new ApiError({
        code: "download_command_not_allowed",
        requestId: "request-ui-30",
        responseMessage:
          "当前状态不允许删除下载任务，任务未改变，请刷新状态后重试",
        status: 409,
      }),
    );
    const user = userEvent.setup();
    renderModels(client);
    const table = await findTable("下载任务");
    await user.click(
      await within(table).findByRole("button", { name: "删除任务" }),
    );
    const dialog = await screen.findByRole("dialog");
    await user.click(
      within(dialog).getByRole("checkbox", {
        name: "同时删除临时文件（1 MB）",
      }),
    );
    await user.click(
      within(dialog).getByRole("button", { name: "确认删除任务" }),
    );
    await waitFor(() => {
      expect(deleteTask).toHaveBeenCalledWith(DOWNLOAD_ID, true);
    });
    expect(await within(table).findByText("操作未完成")).toBeVisible();
    expect(within(table).getByText(/当前状态不允许删除下载任务/)).toBeVisible();
    expect(within(table).getByText("Request ID：request-ui-30")).toBeVisible();
    // UI-39: CSS truncation keeps the full request ID available on hover and in text.
    expect(
      within(table).getByText("Request ID：request-ui-30"),
    ).toHaveAttribute("title", "request-ui-30");
  });

  it("filters the local models by repository_id when 查看本地模型 is used", async () => {
    const { client } = groupedClient(
      [download({ available_commands: [], status: "Completed" })],
      artifactPage([artifact()]),
    );
    const user = userEvent.setup();
    renderModels(client);
    await user.click(await screen.findByRole("button", { name: "展开" }));
    const table = await findTable("下载任务");
    await user.click(
      await within(table).findByRole("button", { name: "查看本地模型" }),
    );
    expect(screen.getByTestId("location-search")).toHaveTextContent(
      "/models?search=Qwen%2FQwen3",
    );
    await waitFor(() => {
      expect(client.modelArtifactCalls).toContainEqual({
        search: "Qwen/Qwen3",
      });
    });
    expect(screen.getByLabelText("搜索模型名称")).toHaveValue("Qwen/Qwen3");
  });

  it.each([
    ["insufficient_storage", /清理本地模型或临时文件释放空间后，点「继续」/],
    ["artifact_safetensors_invalid", /删除任务后在「下载模型」重新发起/],
    ["download_task_state_mismatch", /点「继续」/],
  ] as const)(
    "writes a failure disposal for %s without 重新下载",
    async (code, disposal) => {
      renderModels(
        clientWith(
          downloadPage([
            download({
              available_commands: ["resume"],
              error_code: code,
              failure_stage: "Downloading",
              status: "Failed",
            }),
          ]),
          artifactPage([]),
        ),
      );
      const table = await findTable("下载任务");
      const row = (await within(table).findByText("下载失败")).closest(
        "tr",
      ) as HTMLElement;
      expect(row).toHaveTextContent(disposal);
      expect(row).not.toHaveTextContent("重新下载");
      expect(within(row).queryByRole("link")).not.toBeInTheDocument();
    },
  );
});

describe("UI-30 local model rows", () => {
  it("writes name link, source and short revision, storage type with path and the merged columns", async () => {
    const hash = "a09a354bc0ffee1234567890abcdef1234567890";
    renderModels(
      clientWith(
        downloadPage([]),
        artifactPage([
          artifact({
            path: "/data/models/managed/Qwen/Qwen3/a09a354",
            resolved_revision: hash,
          }),
        ]),
      ),
    );
    const table = await findTable("本地模型");
    const link = await within(table).findByRole("link", { name: "Qwen3" });
    expect(link).toHaveAttribute("href", `/models/local/${ARTIFACT_ID}`);
    const row = link.closest("tr") as HTMLElement;
    expect(within(row).getByText("a09a354")).toHaveAttribute("title", hash);
    expect(row).toHaveTextContent("Hugging Face · a09a354");
    expect(within(row).getByText("平台托管")).toBeVisible();
    expect(
      within(row).getByLabelText("/data/models/managed/Qwen/Qwen3/a09a354"),
    ).toBeVisible();
    expect(within(row).getByRole("button", { name: "复制路径" })).toBeVisible();
    for (const header of [
      "模型 · 来源 · Revision",
      "状态",
      "文件大小",
      "引用实例",
      "校验时间",
      "操作",
    ])
      expect(
        within(table).getByRole("columnheader", { name: header }),
      ).toBeVisible();
    for (const retired of ["来源", "Revision", "存储类型", "本地路径"])
      expect(
        within(table).queryByRole("columnheader", { name: retired }),
      ).not.toBeInTheDocument();
    expect(within(row).getByRole("button", { name: "部署" })).toBeVisible();
    expect(within(row).queryByRole("button", { name: "详情" })).toBeNull();
  });
});

function renderModels(apiClient: FakeApiClient, initialEntry = "/models") {
  const queryClient = new QueryClient({
    defaultOptions: { mutations: { retry: false }, queries: { retry: false } },
  });
  const result = render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[initialEntry]}>
        <ModelsPage apiClient={apiClient} defaultTab="local-models" />
        <LocationProbe />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { ...result, queryClient };
}

function LocationProbe() {
  const location = useLocation();
  return (
    <output data-testid="location-search">
      {location.pathname}
      {location.search}
    </output>
  );
}

function zeroCounts(): Record<DownloadTaskStatus, number> {
  return Object.fromEntries(
    DOWNLOAD_TASK_STATUSES.map((status) => [status, 0]),
  ) as Record<DownloadTaskStatus, number>;
}

function tally(items: readonly { status: DownloadTaskStatus }[]) {
  const counts = zeroCounts();
  for (const { status } of items) counts[status] += 1;
  return counts;
}

/** FakeApiClient plus the UI-25 endpoints it does not know; counts default to zero. */
function baseClient() {
  const client = new FakeApiClient(ADMIN_ME);
  vi.spyOn(client, "getArtifactVerificationStatus").mockResolvedValue({
    job_id: null,
    status: null,
    error_code: null,
  });
  vi.spyOn(client, "downloadStatusCounts").mockResolvedValue({
    counts: zeroCounts(),
  });
  return client;
}

function clientWith(downloads: DownloadTaskPage, artifacts: ModelArtifactPage) {
  const client = baseClient();
  vi.spyOn(client, "downloadStatusCounts").mockResolvedValue({
    counts: tally(downloads.items),
  });
  client.downloadTaskResults.push(downloads);
  client.modelArtifactResults.push(artifacts);
  return client;
}

/**
 * Status-aware server stand-in: filters by the requested statuses, pages by an
 * opaque offset cursor and derives the counts from the current task list.
 */
function groupedClient(
  tasks: unknown[],
  artifacts: ModelArtifactPage,
  pageSize = 100,
) {
  const client = baseClient();
  let all = downloadPage(tasks).items;
  // FakeApiClient narrows getDownloadTasks to (cursor); spy on the full ApiClient signature.
  const api: ApiClient = client;
  const getTasks = vi
    .spyOn(api, "getDownloadTasks")
    .mockImplementation((cursor, status = []) => {
      const matching = all.filter(
        (task) => status.length === 0 || status.includes(task.status),
      );
      const start = cursor === undefined ? 0 : Number(cursor);
      const next = start + pageSize;
      return Promise.resolve(
        parseDownloadTaskPage({
          items: matching.slice(start, next),
          next_cursor: next < matching.length ? String(next) : null,
        }),
      );
    });
  vi.spyOn(client, "downloadStatusCounts").mockImplementation(() =>
    Promise.resolve({ counts: tally(all) }),
  );
  const deleteTask = vi
    .spyOn(client, "deleteDownloadTask")
    .mockImplementation((id) => {
      all = all.filter((task) => task.id !== id);
      return Promise.resolve(undefined);
    });
  client.modelArtifactResults.push(artifacts);
  return { client, deleteTask, getTasks };
}

const COMMANDS_BY_STATUS: Record<DownloadTaskStatus, string[]> = {
  Cancelled: [],
  Completed: [],
  Downloading: ["cancel", "pause"],
  Failed: ["resume", "retry"],
  Paused: ["cancel", "resume"],
  Publishing: [],
  Queued: ["cancel"],
  Resolving: ["cancel"],
  Verifying: ["cancel"],
};

function downloadPage(items: unknown[], nextCursor: string | null = null) {
  return parseDownloadTaskPage({ items, next_cursor: nextCursor });
}

function artifactPage(
  items: unknown[],
  summary: Partial<ModelArtifactPage["summary"]> = {},
  nextCursor: string | null = null,
) {
  return parseModelArtifactPage({
    items,
    next_cursor: nextCursor,
    summary: {
      active_download_tasks: 1,
      managed_bytes: 10_485_760,
      storage_available_bytes: 1_073_741_824,
      ...summary,
    },
  });
}

function download(overrides: Record<string, unknown> = {}) {
  return {
    attempt: 1,
    available_commands: ["pause", "cancel"],
    completed_at: null,
    created_at: "2026-09-11T00:00:00Z",
    display_name: "Qwen3 download",
    downloaded_bytes: 1_048_576,
    error_code: null,
    failure_stage: null,
    id: DOWNLOAD_ID,
    model_id: MODEL_ID,
    repository_id: "Qwen/Qwen3",
    revision: "abc123",
    source: "Hugging Face",
    staging_path: "/staging/qwen3",
    status: "Downloading",
    total_bytes: 10_485_760,
    updated_at: "2026-09-11T01:00:00Z",
    ...overrides,
  };
}

function artifact(overrides: Record<string, unknown> = {}) {
  return {
    created_at: "2026-09-11T00:00:00Z",
    display_name: "Qwen3",
    id: ARTIFACT_ID,
    last_verified_at: "2026-09-11T00:00:00Z",
    model_id: MODEL_ID,
    path: "/models/qwen3",
    referencing_instances: 0,
    repository_id: "Qwen/Qwen3",
    resolved_revision: "abc123",
    size_bytes: 10_485_760,
    source: "Hugging Face",
    status: "Ready",
    storage_type: "Managed",
    updated_at: "2026-09-11T01:00:00Z",
    ...overrides,
  };
}

async function findTable(name: string) {
  return await screen.findByRole("table", { name });
}

async function summaryCard(label: string) {
  return (await screen.findByText(label)).closest("div") as HTMLElement;
}

function validateSeparatedLocalRows(container: HTMLElement) {
  const table =
    container.querySelector('table[aria-label="本地模型"]') ??
    [...container.querySelectorAll("table")].find((candidate) => {
      const caption = candidate.querySelector("caption");
      return caption !== null && caption.textContent.includes("本地模型");
    });
  if (table !== undefined && /下载中|\d+%|MB\/s/.test(table.textContent)) {
    throw new Error("download state leaked into local model rows");
  }
}

function validateUnavailableCard(container: HTMLElement, label: string) {
  const card = [...container.querySelectorAll(".pw-models-summary__item")].find(
    (candidate) => candidate.textContent.includes(label),
  );
  const mark = card?.querySelector('[aria-label="暂无数据"]');
  if (mark === undefined || mark === null || !mark.textContent.includes("—")) {
    throw new Error("unavailable value collapsed to zero");
  }
}

function validateUnavailableReference(container: HTMLElement) {
  const cell = container.querySelector(
    '[data-column="references"] [aria-label="暂无数据"]',
  );
  if (cell === null || !cell.textContent.includes("—"))
    throw new Error("unavailable reference collapsed to zero");
}

function validateIndeterminateProgress(container: HTMLElement) {
  const progress = container.querySelector('[role="progressbar"]');
  if (
    progress !== null &&
    progress.getAttribute("aria-label")?.includes("%") === true
  ) {
    throw new Error("indeterminate progress invented percentage");
  }
}

function validateTrueZero(container: HTMLElement, label: string) {
  const card = [...container.querySelectorAll(".pw-models-summary__item")].find(
    (candidate) => candidate.textContent.includes(label),
  );
  if (card !== undefined && card.textContent.includes("—"))
    throw new Error("true zero disappeared");
}

function validateOnlyCommands(row: HTMLElement, expected: string[]) {
  const commandLabels = ["暂停", "继续", "取消", "重新下载"];
  const actual = [...row.querySelectorAll("button")]
    .map((button) => button.textContent.trim())
    .filter((label) => commandLabels.includes(label));
  if (actual.join("|") !== expected.join("|"))
    throw new Error("unexpected command rendered");
}

function validateCancelledRow(row: HTMLElement) {
  if (/下载失败|失败阶段|内部处理失败/.test(row.textContent))
    throw new Error("cancelled row rendered as failure");
}

function validateUncertainProcessFailure(row: HTMLElement) {
  if (/网络|凭证/.test(row.textContent))
    throw new Error("process failure received deterministic attribution");
}

function uuid(suffix: number) {
  return `0198f58a-2586-7e37-bbf5-fefe165e${suffix.toString().padStart(4, "0")}`;
}

function commandResponse(id: string): DownloadCommandResponse {
  return {
    attempt: 1,
    command: "pause",
    id,
    staging_cleanup_requested: false,
    status: "Paused",
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

const DOWNLOAD_ID = "0198f58a-2586-7e37-bbf5-fefe165e8082";
const MODEL_ID = "0198f58a-2586-7e37-bbf5-fefe165e8083";
const ARTIFACT_ID = "0198f58a-2586-7e37-bbf5-fefe165e8084";

// FX-M2-07D: UI-30 fixtures with the M2-07C Local Path task shape.
describe("M2_07D external directory rows", () => {
  it.each([null, 10485760])(
    "M2_07D describes verification without download for size %s",
    async (totalBytes) => {
      const client = clientWith(
        downloadPage([
          download({
            source: "Local Path",
            repository_id: "/srv/models/one",
            revision: null,
            staging_path: null,
            status: "Verifying",
            downloaded_bytes: 0,
            total_bytes: totalBytes,
            available_commands: ["cancel"],
          }),
        ]),
        artifactPage([]),
      );
      renderModels(client);
      expect(
        await screen.findByText(
          totalBytes === null
            ? "正在校验服务器目录中的文件（不经下载）"
            : "正在校验服务器目录中的 10 MB 文件（不经下载）",
        ),
      ).toBeVisible();
      expect(screen.getByTitle("外部目录没有 Revision")).toHaveTextContent("—");
      expect(screen.getByText(/本地目录 ·/)).toBeVisible();
    },
  );
  it("M2_07D explains unreadable failure and resumes verification", async () => {
    const client = clientWith(
      downloadPage([
        download({
          source: "Local Path",
          revision: null,
          staging_path: null,
          status: "Failed",
          error_code: "external_path_unreadable",
          failure_stage: "Verifying",
          downloaded_bytes: 0,
          total_bytes: null,
          available_commands: ["resume"],
        }),
      ]),
      artifactPage([]),
    );
    renderModels(client);
    expect(
      await screen.findByText(
        "校验失败：运行环境无法读取这个目录。请在服务器上调整目录权限后点「继续」重新校验。",
      ),
    ).toBeVisible();
    await userEvent.click(screen.getByRole("button", { name: "继续" }));
    await waitFor(() => {
      expect(client.downloadCommandCalls).toEqual([
        { command: "resume", id: DOWNLOAD_ID },
      ]);
    });
  });
  it("M2_07D removes External rows while Managed keeps file deletion", async () => {
    const client = clientWith(
      downloadPage([]),
      artifactPage([
        artifact({
          storage_type: "External",
          source: "Local Path",
          resolved_revision: null,
        }),
        artifact({ id: uuid(88), display_name: "Managed" }),
      ]),
    );
    const remove = vi
      .spyOn(client, "deleteModelArtifact")
      .mockImplementation((id) => {
        client.modelArtifactResults.push(
          artifactPage([artifact({ id: uuid(88), display_name: "Managed" })]),
        );
        return Promise.resolve({ id, queued: false });
      });
    renderModels(client);
    const table = await findTable("本地模型");
    await userEvent.click(
      await within(table).findByRole("button", { name: "移除登记" }),
    );
    await userEvent.click(screen.getByRole("button", { name: "确认移除登记" }));
    await waitFor(() => {
      expect(
        within(table).queryByRole("button", { name: "移除登记" }),
      ).toBeNull();
    });
    expect(remove).toHaveBeenCalledWith(ARTIFACT_ID);
    expect(
      within(table).getByRole("button", { name: "删除文件" }),
    ).toBeVisible();
  });
});
