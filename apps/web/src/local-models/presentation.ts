import type {
  ArtifactStorageType,
  DownloadCommand,
  DownloadErrorCode,
  DownloadFailureStage,
  DownloadTaskItem,
  DownloadTaskStatus,
  ModelArtifactStatus,
  ModelOriginType,
} from "../api/model-acquisition-contracts";
import {
  ARTIFACT_STORAGE_TYPES,
  DOWNLOAD_COMMANDS,
  DOWNLOAD_ERROR_CODES,
  DOWNLOAD_FAILURE_STAGES,
  DOWNLOAD_TASK_STATUSES,
  MODEL_ARTIFACT_STATUSES,
  MODEL_ORIGIN_TYPES,
  ModelAcquisitionContractError,
} from "../api/model-acquisition-contracts";
import type { IconName, StatusTone } from "../components";

interface StatusPresentation {
  icon: IconName;
  label: string;
  tone: StatusTone;
}

export const DOWNLOAD_STATUS_PRESENTATIONS = {
  Cancelled: { icon: "close", label: "已取消", tone: "idle" },
  Completed: { icon: "success", label: "下载完成", tone: "positive" },
  Downloading: { icon: "loading", label: "下载中", tone: "info" },
  Failed: { icon: "error", label: "下载失败", tone: "negative" },
  Paused: { icon: "pause", label: "已暂停", tone: "notice" },
  Publishing: { icon: "layers", label: "正在注册", tone: "info" },
  Queued: { icon: "notice", label: "等待下载", tone: "info" },
  Resolving: { icon: "search", label: "读取模型信息", tone: "info" },
  Verifying: { icon: "visibility", label: "校验中", tone: "info" },
} as const satisfies Record<DownloadTaskStatus, StatusPresentation>;

export const ARTIFACT_STATUS_PRESENTATIONS = {
  Corrupted: { icon: "alert", label: "文件异常", tone: "negative" },
  Missing: { icon: "error", label: "文件缺失", tone: "negative" },
  Ready: { icon: "success", label: "可用", tone: "positive" },
} as const satisfies Record<ModelArtifactStatus, StatusPresentation>;

export const STORAGE_TYPE_LABELS = {
  External: "外部目录",
  Managed: "平台托管",
} as const satisfies Record<ArtifactStorageType, string>;

export const ORIGIN_TYPE_LABELS = {
  "Hugging Face": "Hugging Face",
  "Local Path": "本地目录",
  ModelScope: "ModelScope",
} as const satisfies Record<ModelOriginType, string>;

export const FAILURE_STAGE_LABELS = {
  Downloading: "下载中",
  Publishing: "正在注册",
  Resolving: "读取模型信息",
  Verifying: "校验中",
} as const satisfies Record<DownloadFailureStage, string>;

export const DOWNLOAD_COMMAND_LABELS = {
  cancel: "取消",
  pause: "暂停",
  resume: "继续",
  retry: "重新下载",
} as const satisfies Record<DownloadCommand, string>;

export type DownloadFailureCategory =
  "capacity" | "verification" | "process" | "internal" | "cancellation";

export const DOWNLOAD_FAILURE_CATEGORIES = {
  artifact_file_list_invalid: "verification",
  artifact_manifest_hash_failed: "verification",
  artifact_publish_failed: "internal",
  artifact_required_files_invalid: "verification",
  model_layout_unsupported: "verification",
  artifact_runtime_incompatible: "verification",
  artifact_safetensors_invalid: "verification",
  artifact_unsafe_file: "verification",
  download_process_failed: "process",
  download_task_identity_mismatch: "internal",
  download_task_state_mismatch: "internal",
  duplicate_resolved_revision: "cancellation",
  external_path_unreadable: "verification",
  insufficient_storage: "capacity",
  remote_code_not_allowed: "verification",
  staging_cleanup_failed: "internal",
  staging_filesystem_mismatch: "internal",
} as const satisfies Record<DownloadErrorCode, DownloadFailureCategory>;

export function assertExactMappingKeys(
  keys: readonly string[],
  expected: readonly string[],
) {
  const actualValues = [...keys].sort();
  const expectedValues = [...expected].sort();
  if (
    actualValues.length !== expectedValues.length ||
    actualValues.some((key, index) => key !== expectedValues[index])
  ) {
    throw new ModelAcquisitionContractError();
  }
}

function mapped<T>(mapping: Readonly<Record<string, T>>, value: string): T {
  if (!Object.hasOwn(mapping, value)) throw new ModelAcquisitionContractError();
  return mapping[value] as T;
}

export function assertPresentationMappings() {
  assertExactMappingKeys(
    Object.keys(DOWNLOAD_STATUS_PRESENTATIONS),
    DOWNLOAD_TASK_STATUSES,
  );
  assertExactMappingKeys(
    Object.keys(ARTIFACT_STATUS_PRESENTATIONS),
    MODEL_ARTIFACT_STATUSES,
  );
  assertExactMappingKeys(
    Object.keys(STORAGE_TYPE_LABELS),
    ARTIFACT_STORAGE_TYPES,
  );
  assertExactMappingKeys(Object.keys(ORIGIN_TYPE_LABELS), MODEL_ORIGIN_TYPES);
  assertExactMappingKeys(
    Object.keys(FAILURE_STAGE_LABELS),
    DOWNLOAD_FAILURE_STAGES,
  );
  assertExactMappingKeys(
    Object.keys(DOWNLOAD_COMMAND_LABELS),
    DOWNLOAD_COMMANDS,
  );
  assertExactMappingKeys(
    Object.keys(DOWNLOAD_FAILURE_CATEGORIES),
    DOWNLOAD_ERROR_CODES,
  );
}

export function downloadStatusPresentation(status: string) {
  assertPresentationMappings();
  return mapped<StatusPresentation>(DOWNLOAD_STATUS_PRESENTATIONS, status);
}

/**
 * Publishing is shown as "校验中" (the user cannot tell the two
 * stages apart and cannot cancel during registration). The exact per-status
 * table above keeps the contract-level distinction.
 */
export function downloadStatusDisplay(status: string) {
  return downloadStatusPresentation(
    status === "Publishing" ? "Verifying" : status,
  );
}

export function artifactStatusPresentation(status: string) {
  assertPresentationMappings();
  return mapped<StatusPresentation>(ARTIFACT_STATUS_PRESENTATIONS, status);
}

export function storageTypeLabel(value: string) {
  assertPresentationMappings();
  return mapped<string>(STORAGE_TYPE_LABELS, value);
}

export function originTypeLabel(value: string) {
  assertPresentationMappings();
  return mapped<string>(ORIGIN_TYPE_LABELS, value);
}

export function failureStageLabel(value: string) {
  assertPresentationMappings();
  return mapped<string>(FAILURE_STAGE_LABELS, value);
}

/** Resolved commit hashes are shortened to 7 characters; branch names stay whole. */
export function shortRevision(revision: string | null) {
  if (revision === null) return "—";
  return /^[0-9a-f]{8,}$/i.test(revision) ? revision.slice(0, 7) : revision;
}

export interface FailurePresentation {
  detail: string;
  title: string;
}

export function downloadFailurePresentation(
  code: DownloadErrorCode,
  stage: DownloadFailureStage | null,
): FailurePresentation {
  assertPresentationMappings();
  const category = mapped<DownloadFailureCategory>(
    DOWNLOAD_FAILURE_CATEGORIES,
    code,
  );
  if (category === "cancellation") throw new ModelAcquisitionContractError();
  if (category === "capacity") {
    return {
      detail:
        "可用空间不足，下载任务未能完成。清理本地模型或临时文件释放空间后，点「继续」。",
      title: "磁盘空间不足",
    };
  }
  if (code === "model_layout_unsupported") {
    return {
      title: "模型类型暂不支持",
      detail:
        "这个模型的文件结构不是平台可部署的对话模型，没有下载模型文件。可以删除这条任务。",
    };
  }
  if (code === "artifact_required_files_invalid") {
    return {
      title: "模型类型暂不支持或文件不完整",
      detail:
        "下载的文件缺少部署所需的配置（根目录的 config.json、Tokenizer 配置或权重），不会生成本地模型，再次校验结果相同。删除任务时勾选清理临时文件可释放空间。",
    };
  }
  if (category === "verification") {
    return {
      detail:
        "文件未通过安全或完整性校验，不会生成本地模型。可删除任务后在「下载模型」重新发起，或检查模型文件来源。",
      title: "文件校验失败",
    };
  }
  if (category === "process") {
    return {
      detail:
        "下载进程失败，原因无法区分，下载任务未能完成。可检查模型源设置与连接条件后点「继续」。",
      title: "下载进程失败",
    };
  }
  if (stage === null) throw new ModelAcquisitionContractError();
  return {
    detail: `${failureStageLabel(stage)}阶段发生内部处理失败，下载任务未能完成。可尝试刷新状态后点「继续」。`,
    title: "内部处理失败",
  };
}

export interface DownloadRate {
  bytesPerSecond: number;
  etaSeconds: number | null;
}

interface DownloadSample {
  attempt: number;
  downloadedBytes: number;
  updatedAtMs: number;
}

export class DownloadRateTracker {
  private readonly rates = new Map<string, DownloadRate>();
  private readonly samples = new Map<string, DownloadSample>();

  observe(items: readonly DownloadTaskItem[]) {
    const currentIds = new Set(items.map(({ id }) => id));
    for (const id of this.samples.keys()) {
      if (!currentIds.has(id)) {
        this.samples.delete(id);
        this.rates.delete(id);
      }
    }
    for (const item of items) {
      if (item.status !== "Downloading") {
        this.samples.delete(item.id);
        this.rates.delete(item.id);
        continue;
      }
      const updatedAtMs = Date.parse(item.updated_at);
      const previous = this.samples.get(item.id);
      if (previous === undefined || previous.attempt !== item.attempt) {
        this.samples.set(item.id, {
          attempt: item.attempt,
          downloadedBytes: item.downloaded_bytes,
          updatedAtMs,
        });
        this.rates.delete(item.id);
        continue;
      }
      if (updatedAtMs <= previous.updatedAtMs) continue;
      if (item.downloaded_bytes < previous.downloadedBytes) {
        this.samples.set(item.id, {
          attempt: item.attempt,
          downloadedBytes: item.downloaded_bytes,
          updatedAtMs,
        });
        this.rates.delete(item.id);
        continue;
      }
      const bytesPerSecond =
        ((item.downloaded_bytes - previous.downloadedBytes) * 1000) /
        (updatedAtMs - previous.updatedAtMs);
      this.samples.set(item.id, {
        attempt: item.attempt,
        downloadedBytes: item.downloaded_bytes,
        updatedAtMs,
      });
      this.rates.set(item.id, {
        bytesPerSecond,
        etaSeconds:
          bytesPerSecond > 0 && item.total_bytes !== null
            ? Math.max(0, item.total_bytes - item.downloaded_bytes) /
              bytesPerSecond
            : null,
      });
    }
    return new Map(this.rates);
  }
}

export function formatDuration(seconds: number) {
  const rounded = Math.max(0, Math.ceil(seconds));
  const hours = Math.floor(rounded / 3600);
  const minutes = Math.floor((rounded % 3600) / 60);
  const remainder = rounded % 60;
  if (hours > 0) return `${String(hours)} 小时 ${String(minutes)} 分`;
  if (minutes > 0) return `${String(minutes)} 分 ${String(remainder)} 秒`;
  return `${String(remainder)} 秒`;
}
