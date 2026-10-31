import { describe, expect, it, vi } from "vitest";

import {
  DOWNLOAD_ERROR_CODES,
  DOWNLOAD_TASK_STATUSES,
  type DownloadTaskItem,
} from "../api/model-acquisition-contracts";
import {
  assertExactMappingKeys,
  assertPresentationMappings,
  DOWNLOAD_FAILURE_CATEGORIES,
  DOWNLOAD_STATUS_PRESENTATIONS,
  ARTIFACT_STATUS_PRESENTATIONS,
  downloadFailurePresentation,
  downloadStatusDisplay,
  DownloadRateTracker,
  shortRevision,
} from "./presentation";

describe("M2_12B server-observation download speed", () => {
  it("positive control has no speed from only one observation", () => {
    const rates = new DownloadRateTracker().observe([task()]);
    expect(() => {
      validateNoRate(rates);
    }).not.toThrow();
    rates.set(TASK_ID, { bytesPerSecond: 1, etaSeconds: 1 });
    expect(() => {
      validateNoRate(rates);
    }).toThrow(/unexpected rate/);
  });

  it("differences consecutive server updated_at observations", () => {
    const tracker = new DownloadRateTracker();
    tracker.observe([task()]);
    const rates = tracker.observe([
      task({ downloaded_bytes: 3_145_728, updated_at: "2026-09-11T01:00:02Z" }),
    ]);
    expect(rates.get(TASK_ID)).toEqual({
      bytesPerSecond: 1_048_576,
      etaSeconds: 7,
    });
  });

  it("rejects equal timestamps and byte regressions as new samples", () => {
    const equalTimestamp = new DownloadRateTracker();
    equalTimestamp.observe([task()]);
    expect(
      equalTimestamp
        .observe([task({ downloaded_bytes: 3_145_728 })])
        .get(TASK_ID),
    ).toBeUndefined();

    const regressed = new DownloadRateTracker();
    regressed.observe([task()]);
    regressed.observe([
      task({ downloaded_bytes: 3_145_728, updated_at: "2026-09-11T01:00:02Z" }),
    ]);
    expect(
      regressed
        .observe([
          task({
            downloaded_bytes: 2_097_152,
            updated_at: "2026-09-11T01:00:03Z",
          }),
        ])
        .get(TASK_ID),
    ).toBeUndefined();
  });

  it("withholds ETA for zero speed, unknown totals, and non-downloading states", () => {
    const zeroSpeed = new DownloadRateTracker();
    zeroSpeed.observe([task()]);
    expect(
      zeroSpeed
        .observe([task({ updated_at: "2026-09-11T01:00:02Z" })])
        .get(TASK_ID)?.etaSeconds,
    ).toBeNull();

    const unknownTotal = new DownloadRateTracker();
    unknownTotal.observe([task({ total_bytes: null })]);
    expect(
      unknownTotal
        .observe([
          task({
            downloaded_bytes: 3_145_728,
            total_bytes: null,
            updated_at: "2026-09-11T01:00:02Z",
          }),
        ])
        .get(TASK_ID)?.etaSeconds,
    ).toBeNull();

    expect(
      unknownTotal.observe([task({ status: "Paused" })]).get(TASK_ID),
    ).toBeUndefined();
  });

  it("positive control clears old samples when attempt changes without byte regression", () => {
    const tracker = new DownloadRateTracker();
    tracker.observe([task()]);
    tracker.observe([
      task({ downloaded_bytes: 3_145_728, updated_at: "2026-09-11T01:00:02Z" }),
    ]);
    expect(
      tracker
        .observe([
          task({
            attempt: 2,
            downloaded_bytes: 4_194_304,
            updated_at: "2026-09-11T01:00:03Z",
          }),
        ])
        .get(TASK_ID),
    ).toBeUndefined();
  });

  it("positive control ignores browser clock jumps", () => {
    const tracker = new DownloadRateTracker();
    const now = vi.spyOn(Date, "now");
    now.mockReturnValue(1);
    tracker.observe([task()]);
    now.mockReturnValue(9_999_999_999_999);
    const rate = tracker
      .observe([
        task({
          downloaded_bytes: 3_145_728,
          updated_at: "2026-09-11T01:00:02Z",
        }),
      ])
      .get(TASK_ID)?.bytesPerSecond;
    expect(() => {
      validateServerTimedRate(rate);
    }).not.toThrow();
    expect(() => {
      validateServerTimedRate(2_097_152 / (9_999_999_999_999 - 1));
    }).toThrow(/browser clock affected rate/);
    now.mockRestore();
  });
});

describe("M2_12B exact presentation mappings", () => {
  it("keeps every mapping bidirectionally exact", () => {
    expect(() => {
      assertPresentationMappings();
    }).not.toThrow();
    expect(Object.keys(DOWNLOAD_FAILURE_CATEGORIES).sort()).toEqual(
      [...DOWNLOAD_ERROR_CODES].sort(),
    );
  });

  it("keeps same-tone states distinguishable by both icon and Chinese label", () => {
    const active = [
      "Queued",
      "Resolving",
      "Downloading",
      "Verifying",
      "Publishing",
    ] as const;
    expect(
      new Set(
        active.map((status) => DOWNLOAD_STATUS_PRESENTATIONS[status].icon),
      ).size,
    ).toBe(5);
    expect(
      new Set(
        active.map((status) => DOWNLOAD_STATUS_PRESENTATIONS[status].label),
      ).size,
    ).toBe(5);
    expect(ARTIFACT_STATUS_PRESENTATIONS.Corrupted.icon).not.toBe(
      ARTIFACT_STATUS_PRESENTATIONS.Missing.icon,
    );
    expect(ARTIFACT_STATUS_PRESENTATIONS.Corrupted.label).not.toBe(
      ARTIFACT_STATUS_PRESENTATIONS.Missing.label,
    );
  });

  it("positive control fails when one mapping is deleted", () => {
    expect(() => {
      assertExactMappingKeys(
        Object.keys(DOWNLOAD_STATUS_PRESENTATIONS).filter(
          (key) => key !== "Queued",
        ),
        DOWNLOAD_TASK_STATUSES,
      );
    }).toThrow();
  });

  it("positive control fails when a server-absent mapping is added", () => {
    expect(() => {
      assertExactMappingKeys(
        [...Object.keys(DOWNLOAD_STATUS_PRESENTATIONS), "FutureStatus"],
        DOWNLOAD_TASK_STATUSES,
      );
    }).toThrow();
  });
});

describe("UI-30 display folding and wording", () => {
  it("shows Publishing as 校验中 while the contract table keeps the stages apart", () => {
    expect(downloadStatusDisplay("Publishing").label).toBe("校验中");
    expect(downloadStatusDisplay("Publishing")).toEqual(
      downloadStatusDisplay("Verifying"),
    );
    expect(downloadStatusDisplay("Downloading").label).toBe("下载中");
    expect(DOWNLOAD_STATUS_PRESENTATIONS.Publishing.label).not.toBe(
      DOWNLOAD_STATUS_PRESENTATIONS.Verifying.label,
    );
  });

  it("shortens resolved commit hashes to 7 characters and keeps branch names whole", () => {
    expect(shortRevision("3f2a9c1d4e5b60718293a4b5c6d7e8f901234567")).toBe(
      "3f2a9c1",
    );
    expect(shortRevision("master")).toBe("master");
    expect(shortRevision("abc123")).toBe("abc123");
  });

  it("never offers 重新下载 as a disposal in any failure copy", () => {
    for (const code of DOWNLOAD_ERROR_CODES) {
      if (DOWNLOAD_FAILURE_CATEGORIES[code] === "cancellation") continue;
      const copy = downloadFailurePresentation(code, "Downloading");
      expect(`${copy.title}${copy.detail}`).not.toContain("重新下载");
    }
  });
});

describe("M2_12B failure classification", () => {
  it("maps all 17 codes to exactly one category", () => {
    expect(Object.keys(DOWNLOAD_FAILURE_CATEGORIES)).toHaveLength(17);
    expect(DOWNLOAD_FAILURE_CATEGORIES.external_path_unreadable).toBe(
      "verification",
    );
    expect(new Set(Object.values(DOWNLOAD_FAILURE_CATEGORIES)).size).toBe(5);
  });

  it("positive control never deterministically attributes download_process_failed", () => {
    const copy = downloadFailurePresentation(
      "download_process_failed",
      "Downloading",
    );
    expect(() => {
      validateUncertainCopy(`${copy.title}${copy.detail}`);
    }).not.toThrow();
    expect(() => {
      validateUncertainCopy(`${copy.title}${copy.detail}网络错误`);
    }).toThrow(/deterministic attribution/);
  });
});

function validateNoRate(rates: ReadonlyMap<string, unknown>) {
  if (rates.has(TASK_ID))
    throw new Error("unexpected rate without valid samples");
}

function validateServerTimedRate(rate: number | undefined) {
  if (rate !== 1_048_576) throw new Error("browser clock affected rate");
}

function validateUncertainCopy(copy: string) {
  if (/网络|凭证/.test(copy))
    throw new Error("process failure received deterministic attribution");
}

const TASK_ID = "0198f58a-2586-7e37-bbf5-fefe165e8082";

function task(overrides: Partial<DownloadTaskItem> = {}): DownloadTaskItem {
  return {
    attempt: 1,
    available_commands: ["pause", "cancel"],
    completed_at: null,
    created_at: "2026-09-11T00:00:00Z",
    display_name: "Qwen3",
    downloaded_bytes: 1_048_576,
    error_code: null,
    failure_stage: null,
    id: TASK_ID,
    model_id: "0198f58a-2586-7e37-bbf5-fefe165e8083",
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

it("FIX_23 TC_DL_33 explains both deterministic failures precisely", () => {
  expect(
    downloadFailurePresentation("model_layout_unsupported", "Resolving"),
  ).toEqual({
    title: "模型类型暂不支持",
    detail:
      "这个模型的文件结构不是平台可部署的对话模型，没有下载模型文件。可以删除这条任务。",
  });
  expect(
    downloadFailurePresentation("artifact_required_files_invalid", "Verifying"),
  ).toEqual({
    title: "模型类型暂不支持或文件不完整",
    detail:
      "下载的文件缺少部署所需的配置（根目录的 config.json、Tokenizer 配置或权重），不会生成本地模型，再次校验结果相同。删除任务时勾选清理临时文件可释放空间。",
  });
  expect(
    downloadFailurePresentation("artifact_safetensors_invalid", "Verifying")
      .title,
  ).toBe("文件校验失败");
});
