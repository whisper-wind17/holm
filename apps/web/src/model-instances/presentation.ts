import type {
  ModelInstanceStatus,
  RuntimeType,
} from "../api/model-instance-contracts";
import type { IconName, StatusTone } from "../components";

interface StatusPresentation {
  icon: IconName;
  label: string;
  tone: StatusTone;
}

export type InstanceAction =
  | "cancel"
  | "delete"
  | "logs"
  | "publish"
  | "reselect-gpus"
  | "restart"
  | "retry"
  | "start"
  | "stop";

export const INSTANCE_ACTIONS = {
  Failed: ["retry", "delete", "logs"],
  Restarting: ["stop", "logs"],
  Running: ["publish", "restart", "stop", "logs"],
  Starting: ["logs"],
  Stopped: ["start", "delete", "logs"],
  Stopping: ["logs"],
  Unhealthy: ["stop", "logs"],
  WaitingForResource: ["reselect-gpus", "cancel"],
} as const satisfies Record<ModelInstanceStatus, readonly InstanceAction[]>;

export const INSTANCE_STATUS_PRESENTATIONS = {
  Failed: { icon: "error", label: "运行失败", tone: "negative" },
  Restarting: { icon: "loading", label: "重启中", tone: "info" },
  Running: { icon: "success", label: "运行中", tone: "positive" },
  Starting: { icon: "loading", label: "启动中", tone: "info" },
  Stopped: { icon: "pause", label: "已停止", tone: "idle" },
  Stopping: { icon: "loading", label: "停止中", tone: "notice" },
  Unhealthy: { icon: "error", label: "运行异常", tone: "negative" },
  WaitingForResource: {
    icon: "notice",
    label: "等待 GPU",
    tone: "notice",
  },
} as const satisfies Record<ModelInstanceStatus, StatusPresentation>;

export function instanceStatusPresentation(status: ModelInstanceStatus) {
  return INSTANCE_STATUS_PRESENTATIONS[status];
}

export function instanceActions(status: ModelInstanceStatus) {
  return INSTANCE_ACTIONS[status];
}

export function runtimeLabel(runtime: RuntimeType) {
  return runtime === "vllm" ? "vLLM" : "SGLang";
}

export interface InstanceFailurePresentation {
  advice: string;
  reason: string;
  stage: string;
}

const UNKNOWN_FAILURE: InstanceFailurePresentation = {
  advice: "可尝试刷新系统状态后重试；问题持续时请查看详细日志。",
  reason: "部署未能完成，模型实例当前不可用。请检查系统状态后重试。",
  stage: "未知",
};

export const FAILURE_PRESENTATIONS: Readonly<
  Record<string, InstanceFailurePresentation>
> = {
  gpu_inventory_unavailable: {
    advice: "可尝试稍后刷新 GPU 信息，确认资源可用后重试。",
    reason: "GPU 信息暂不可用，模型实例未能启动。",
    stage: "检查 GPU",
  },
  invalid_request: {
    advice: "可尝试修改为受支持的配置后重新部署。",
    reason: "运行配置不受支持，模型实例未能启动。",
    stage: "校验配置",
  },
  runtime_exited: {
    advice: "可尝试重新部署；问题持续时请查看详细日志。",
    reason: "模型运行意外停止，模型实例当前不可用。",
    stage: "运行模型",
  },
  runtime_gpu_mismatch: {
    advice: "可尝试确认所选 GPU 未被占用后重新部署。",
    reason: "实际使用的 GPU 与所选资源不一致，部署已停止。",
    stage: "校验 GPU",
  },
  runtime_health_failed: {
    advice: "可尝试重新部署；问题持续时请检查 GPU 状态并查看详细日志。",
    reason: "模型实例未通过健康检查，当前无法提供推理。",
    stage: "健康检查",
  },
  runtime_not_certified: {
    advice: "可尝试改用已认证的运行配置后重新部署。",
    reason: "当前运行配置未通过兼容性认证，部署已停止。",
    stage: "兼容性检查",
  },
  runtime_out_of_memory: {
    advice: "可尝试使用 2 张同型号 GPU，或降低 Context Length。",
    reason: "GPU 显存不足，模型实例未能完成部署。",
    stage: "加载权重",
  },
  runtime_restart_failed: {
    advice: "可尝试重新部署；问题持续时请查看详细日志。",
    reason: "模型实例多次自动恢复仍未成功，当前不可用。",
    stage: "自动恢复",
  },
  artifact_verification_failed: {
    advice: "可尝试在本地模型页重新校验；文件缺失或异常时恢复文件后再重试。",
    reason: "本地模型完整性校验未通过，模型实例未启动。",
    stage: "校验模型文件",
  },
  artifact_verification_timeout: {
    advice: "可尝试检查后台组件状态，恢复后重新启动模型实例。",
    reason: "本地模型完整性校验未在规定时间内完成，模型实例未启动。",
    stage: "校验模型文件",
  },
  runtime_start_failed: {
    advice: "可尝试确认本地模型完整且 GPU 可用后重试。",
    reason: "模型实例启动失败，当前无法提供推理。",
    stage: "启动模型",
  },
  runtime_start_timeout: {
    advice: "可尝试稍后重试；问题持续时请检查 GPU 状态和本地模型。",
    reason: "模型实例未能在规定时间内启动，当前不可用。",
    stage: "启动模型",
  },
};

export function instanceFailurePresentation(errorCode: string | null) {
  return errorCode === null
    ? UNKNOWN_FAILURE
    : (FAILURE_PRESENTATIONS[errorCode] ?? UNKNOWN_FAILURE);
}

export function shortInstanceId(id: string) {
  return id.slice(0, 8);
}

/** the unhealthy banner states the health-check conclusion only. */
export function instanceHealthConclusion(errorCode: string | null) {
  return (
    (errorCode === null
      ? undefined
      : FAILURE_PRESENTATIONS[errorCode]?.reason) ?? "健康检查未通过"
  );
}

export type LogLevel = "ERROR" | "INFO" | "WARN";

/**
 * Host Agent log lines carry no structured level, so the level is read
 * from the text. Lines without a recognised keyword return null.
 */
export function detectLogLevel(text: string): LogLevel | null {
  if (/\bERROR\b/.test(text)) return "ERROR";
  if (/\b(?:WARNING|WARN)\b/.test(text)) return "WARN";
  if (/\bINFO\b/.test(text)) return "INFO";
  return null;
}
