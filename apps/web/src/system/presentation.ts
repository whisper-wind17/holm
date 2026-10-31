import type {
  GpuDevice,
  HostEnvironmentItem,
  InferenceEngine,
  SystemComponent,
  SystemComponentsResponse,
  SystemServerResponse,
} from "../api/system-contracts";
import {
  SYSTEM_COMPONENT_IDS,
  SystemContractError,
} from "../api/system-contracts";
import type { IconName, StatusTone } from "../components";

interface ComponentPresentation {
  impact: string;
  label: string;
}

interface StatusPresentation {
  icon: IconName;
  label: string;
  tone: StatusTone;
}

export const COMPONENT_PRESENTATIONS = {
  Edge: { impact: "管理页面与 API 入口不可用。", label: "接入层" },
  Maintenance: { impact: "备份与过期数据清理可能延迟。", label: "维护任务" },
  "Host Agent": {
    impact: "模型实例无法启动或停止；已在运行的实例不受影响。",
    label: "宿主机服务",
  },
  "Control Plane": {
    impact: "管理页面与配置操作可能不可用。",
    label: "控制平面",
  },
  Database: {
    impact: "平台配置与授权数据可能无法读取。",
    label: "数据库",
  },
  "Download Manager": {
    impact: "模型下载任务可能暂停。",
    label: "下载管理器",
  },
  Gateway: {
    impact: "新的 API 调用可能失败。",
    label: "推理网关",
  },
  "Request Ledger Writer": {
    impact: "用量入账可能延迟，调用是否继续取决于可靠队列状态。",
    label: "调用账本写入器",
  },
  Scheduler: {
    impact: "后台任务调度可能延迟。",
    label: "调度器",
  },
  "Telemetry Collector": {
    impact: "监控指标停止更新，AI 服务可用性判断不受影响。",
    label: "遥测采集器",
  },
} as const satisfies Record<
  (typeof SYSTEM_COMPONENT_IDS)[number],
  ComponentPresentation
>;

const COMPONENT_STATUS_PRESENTATIONS = {
  degraded: { icon: "alert", label: "降级", tone: "notice" },
  not_deployed: { icon: "pause", label: "尚未部署", tone: "idle" },
  ready: { icon: "success", label: "就绪", tone: "positive" },
  unavailable: { icon: "error", label: "不可用", tone: "negative" },
} as const satisfies Record<SystemComponent["status"], StatusPresentation>;

const GPU_HEALTH_PRESENTATIONS = {
  healthy: { icon: "success", label: "正常", tone: "positive" },
  unhealthy: { icon: "error", label: "异常", tone: "negative" },
  unknown: { icon: "notice", label: "未知", tone: "notice" },
} as const satisfies Record<GpuDevice["health"], StatusPresentation>;

const AGENT_UNAVAILABLE_REASONS = {
  access_denied: "服务器检测服务拒绝访问",
  connection_failed: "无法连接服务器检测服务",
  rpc_failed: "服务器检测服务返回失败",
  timeout: "服务器检测服务响应超时",
} as const;

const GPU_DISCOVERY_FAILURE_REASONS = {
  driver_unavailable: "NVIDIA Driver 暂不可用",
  enumeration_failed: "GPU 枚举失败",
  initialization_failed: "GPU 检测初始化失败",
  library_unavailable: "GPU 检测组件暂不可用",
  no_devices: "没有检测到 GPU",
  none: "GPU 检测没有报告失败",
  permission_denied: "GPU 检测权限不足",
  protocol_invalid: "GPU 检测响应不兼容",
  snapshot_changed: "GPU 列表在检测期间发生变化",
} as const;

export function assertExactComponentPresentationKeys(
  presentationKeys: readonly string[],
  expectedIds: readonly string[] = SYSTEM_COMPONENT_IDS,
) {
  const actual = [...presentationKeys].sort();
  const expected = [...expectedIds].sort();
  if (
    actual.length !== expected.length ||
    actual.some((key, index) => key !== expected[index])
  ) {
    throw new SystemContractError();
  }
}

export function componentPresentation(componentId: string) {
  assertExactComponentPresentationKeys(Object.keys(COMPONENT_PRESENTATIONS));
  if (!Object.hasOwn(COMPONENT_PRESENTATIONS, componentId)) {
    throw new SystemContractError();
  }
  return COMPONENT_PRESENTATIONS[
    componentId as keyof typeof COMPONENT_PRESENTATIONS
  ];
}

export function componentStatusPresentation(status: string) {
  if (!Object.hasOwn(COMPONENT_STATUS_PRESENTATIONS, status)) {
    throw new SystemContractError();
  }
  return COMPONENT_STATUS_PRESENTATIONS[
    status as keyof typeof COMPONENT_STATUS_PRESENTATIONS
  ];
}

export function gpuHealthPresentation(health: string) {
  if (!Object.hasOwn(GPU_HEALTH_PRESENTATIONS, health)) {
    throw new SystemContractError();
  }
  return GPU_HEALTH_PRESENTATIONS[
    health as keyof typeof GPU_HEALTH_PRESENTATIONS
  ];
}

export function agentUnavailableReason(reason: string) {
  if (!Object.hasOwn(AGENT_UNAVAILABLE_REASONS, reason)) {
    throw new SystemContractError();
  }
  return AGENT_UNAVAILABLE_REASONS[
    reason as keyof typeof AGENT_UNAVAILABLE_REASONS
  ];
}

export function gpuDiscoveryFailureReason(reason: string) {
  if (!Object.hasOwn(GPU_DISCOVERY_FAILURE_REASONS, reason)) {
    throw new SystemContractError();
  }
  return GPU_DISCOVERY_FAILURE_REASONS[
    reason as keyof typeof GPU_DISCOVERY_FAILURE_REASONS
  ];
}

export interface PlatformStatusPresentation {
  mostSevereIssue?: { detail: string; tone: StatusTone };
  detail: string;
  icon: IconName;
  label: "不可用" | "部分异常" | "正常";
  tone: StatusTone;
}

export function unavailablePlatformStatus(
  detail = "无法获取平台状态，请稍后重试",
): PlatformStatusPresentation {
  return { detail, icon: "error", label: "不可用", tone: "negative" };
}

export function buildPlatformStatus(
  server: SystemServerResponse,
  components: SystemComponentsResponse,
): PlatformStatusPresentation {
  const issues: string[] = [];
  let mostSevereIssue: PlatformStatusPresentation["mostSevereIssue"];
  const addIssue = (detail: string, tone: StatusTone, impact?: string) => {
    issues.push(detail);
    if (
      !mostSevereIssue ||
      (tone === "negative" && mostSevereIssue.tone !== "negative")
    ) {
      mostSevereIssue = {
        detail: impact ? `${detail}。${impact}` : detail,
        tone,
      };
    }
  };

  for (const component of components.components) {
    if (component.status === "ready" || component.status === "not_deployed") {
      continue;
    }
    const presentation = componentPresentation(component.id);
    const status = componentStatusPresentation(component.status);
    addIssue(
      `${presentation.label}${status.label}：${presentation.impact}`,
      status.tone,
    );
  }

  if (server.agent.status === "unavailable") {
    addIssue(
      `GPU 信息不可用：${agentUnavailableReason(server.agent.reason)}`,
      "negative",
      "设备状态无法确认，部署前请查看系统详情。",
    );
  } else if (server.agent.gpus.discovery_state === "unavailable") {
    addIssue(
      `GPU 信息不可用：${gpuDiscoveryFailureReason(server.agent.gpus.failure_reason)}`,
      "negative",
      "设备状态无法确认，部署前请查看系统详情。",
    );
  } else {
    for (const gpu of server.agent.gpus.devices) {
      if (gpu.health !== "healthy") {
        const gpuLabel = String(gpu.display_index ?? gpu.uuid);
        addIssue(
          `GPU ${gpuLabel} Health ${gpuHealthPresentation(gpu.health).label}`,
          gpuHealthPresentation(gpu.health).tone,
          gpu.health === "unhealthy"
            ? "相关模型实例可能无法正常运行。"
            : "设备状态无法确认，部署前请查看系统详情。",
        );
      }
    }
  }

  if (issues.length === 0) {
    return {
      detail: "服务器与已部署平台组件运行正常",
      icon: "success",
      label: "正常",
      tone: "positive",
    };
  }

  return {
    mostSevereIssue,
    detail: issues.join(" · "),
    icon: "alert",
    label: "部分异常",
    tone: "notice",
  };
}

const PROBE_REASONS = {
  probe_ready: "健康检查通过",
  probe_unreachable: "健康检查无响应",
  gateway_route_snapshot_unavailable: "路由数据未同步，暂不能接收调用",
  gateway_ledger_unavailable: "调用记录暂不可写，暂不能接收调用",
} as const satisfies Partial<Record<SystemComponent["reason"], string>>;

export function componentImpact(component: SystemComponent) {
  if (Object.hasOwn(PROBE_REASONS, component.reason)) {
    return PROBE_REASONS[component.reason as keyof typeof PROBE_REASONS];
  }
  if (component.status === "not_deployed") {
    return "该组件在本版本尚未部署，不计入平台异常。";
  }
  if (component.status === "ready") {
    return "已部署组件运行正常。";
  }
  return componentPresentation(component.id).impact;
}

export const HOST_ENVIRONMENT_NAMES = {
  nvidia_driver: "GPU 驱动",
  docker: "Docker",
  compose: "Compose",
  nvidia_container_toolkit: "NVIDIA Container Toolkit",
} as const satisfies Record<HostEnvironmentItem["id"], string>;

export const INFERENCE_ENGINE_NAMES = {
  vllm: "vLLM",
  sglang: "SGLang",
} as const;

export const HOST_ENVIRONMENT_STATUS_PRESENTATIONS = {
  satisfied: { icon: "success", label: "满足", tone: "positive" },
  unsatisfied: { icon: "error", label: "不满足", tone: "negative" },
  unknown: { icon: "notice", label: "无法判断", tone: "idle" },
} as const satisfies Record<HostEnvironmentItem["status"], StatusPresentation>;

export const DRIVER_COMPATIBILITY_PRESENTATIONS = {
  compatible: { icon: "success", label: "兼容", tone: "positive" },
  incompatible: { icon: "error", label: "不兼容", tone: "negative" },
  unknown: { icon: "notice", label: "无法判断", tone: "idle" },
} as const satisfies Record<
  InferenceEngine["driver_compatibility"],
  StatusPresentation
>;

export const HOST_ENVIRONMENT_REASON_PRESENTATIONS = {
  meets_minimum: "",
  below_minimum: "低于最低要求，请先在宿主机升级。",
  agent_unavailable: "宿主机服务不可用，无法读取宿主环境。",
  current_unavailable: "未检测到当前版本。",
  minimum_not_declared: "发行清单尚未声明最低版本。",
  version_unparseable: "版本号无法识别，无法比较。",
} as const satisfies Record<HostEnvironmentItem["reason"], string>;

export const DRIVER_COMPATIBILITY_REASON_PRESENTATIONS = {
  meets_minimum: "要求驱动 ≥ {minimum}",
  below_minimum: "需先升级驱动：要求 ≥ {minimum}，当前 {driver}。",
  agent_unavailable: "宿主机服务不可用，无法读取当前驱动版本。",
  driver_unavailable: "无法读取当前驱动版本。",
  minimum_not_declared: "兼容清单尚未声明最低驱动版本。",
  version_unparseable: "版本号无法识别，无法比较。",
} as const satisfies Record<InferenceEngine["reason"], string>;

function exactPresentation<T>(mapping: Record<string, T>, key: string): T {
  if (!Object.hasOwn(mapping, key)) throw new SystemContractError();
  const value = mapping[key];
  if (value === undefined) throw new SystemContractError();
  return value;
}

export function hostEnvironmentName(id: string) {
  return exactPresentation(HOST_ENVIRONMENT_NAMES, id);
}
export function inferenceEngineName(name: string) {
  return exactPresentation(INFERENCE_ENGINE_NAMES, name);
}
export function hostEnvironmentStatusPresentation(status: string) {
  return exactPresentation(HOST_ENVIRONMENT_STATUS_PRESENTATIONS, status);
}
export function driverCompatibilityPresentation(status: string) {
  return exactPresentation(DRIVER_COMPATIBILITY_PRESENTATIONS, status);
}
export function hostEnvironmentReason(reason: string) {
  return exactPresentation(HOST_ENVIRONMENT_REASON_PRESENTATIONS, reason);
}
export function driverCompatibilityReason(
  reason: string,
  minimum: string | null,
  driver: string | null,
) {
  return exactPresentation(DRIVER_COMPATIBILITY_REASON_PRESENTATIONS, reason)
    .replace("{minimum}", minimum ?? "—")
    .replace("{driver}", driver ?? "—");
}
