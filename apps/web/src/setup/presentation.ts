import {
  BOOTSTRAP_CHECK_IDS,
  BOOTSTRAP_CHECK_OUTCOMES,
  BOOTSTRAP_CHECK_REASONS,
  BOOTSTRAP_ERROR_CODES,
  BOOTSTRAP_USERNAME_REASONS,
  BootstrapContractError,
  type BootstrapCheckId,
  type BootstrapCheckOutcome,
  type BootstrapCheckReason,
  type BootstrapCheckReport,
} from "../api/bootstrap-contracts";
import { SYSTEM_COMPONENT_IDS } from "../api/system-contracts";
import type { IconName, StatusTone } from "../components";

/**
 * Identifier → Chinese sentence, and nothing else.
 *
 * The server already decided every verdict (`bootstrap_checks.py` holds the one
 * decision table). This module does not re-derive "passed", does not infer a
 * reason from a value, and refuses an identifier it has no entry for rather
 * than rendering a blank row — the same fail-loud shape as
 * `NavigationContractError` and `SystemContractError`.
 *
 * Error copy follows this order: what happened → what it affects →
 * what to do about it.
 */

interface CheckReasonPresentation {
  /** Impact plus remedy. `null` only where the verdict is a pass. */
  advice: string | null;
  /** What was observed. */
  detail: string;
}

interface OutcomePresentation {
  icon: IconName;
  label: string;
  tone: StatusTone;
}

export const BOOTSTRAP_CHECK_LABELS = {
  docker_compose: "Docker／Compose",
  nvidia_container_toolkit: "NVIDIA Container Toolkit",
  disk: "磁盘",
  gpu: "GPU",
  memory: "内存",
  nvidia_driver: "NVIDIA 驱动",
  holm_components: "Holm 组件",
} as const satisfies Record<BootstrapCheckId, string>;

const BOOTSTRAP_OUTCOME_PRESENTATIONS = {
  failed: { icon: "error", label: "失败", tone: "negative" },
  passed: { icon: "success", label: "通过", tone: "positive" },
  warning: { icon: "alert", label: "警告", tone: "notice" },
} as const satisfies Record<BootstrapCheckOutcome, OutcomePresentation>;

const BOOTSTRAP_CHECK_REASON_PRESENTATIONS = {
  agent_unavailable: {
    advice:
      "影响：读不到这台服务器的硬件与运行环境，初始化无法完成。请确认服务器检测服务已启动后重新检测。",
    detail: "无法连接服务器检测服务",
  },
  components_abnormal: {
    advice:
      "影响：管理页面与配置操作可能不可用，初始化无法完成。请在服务器上确认各 Holm 组件已启动后重新检测。",
    detail: "部分 Holm 组件降级或不可用",
  },
  components_not_deployed: {
    advice:
      "影响：可以继续初始化。尚未部署的组件不计入平台异常，相关功能在后续版本提供，现在无需处理。",
    detail: "部分 Holm 组件在本版本尚未部署",
  },
  components_ready: {
    advice: null,
    detail: `${String(SYSTEM_COMPONENT_IDS.length)} 个子模块全部就绪`,
  },
  docker_compose_meets_minimum: {
    advice: null,
    detail: "Docker／Compose 满足最低要求",
  },
  docker_below_minimum: {
    detail: "Docker 低于最低要求",
    advice: "模型实例无法正常启动。请在宿主机升级 Docker 后点「重新检测」。",
  },
  compose_below_minimum: {
    detail: "Compose 低于最低要求",
    advice: "平台组件无法正常运行。请在宿主机升级 Compose 后点「重新检测」。",
  },
  docker_version_undetermined: {
    detail: "无法判断 Docker 是否满足最低要求",
    advice:
      "无法确认模型实例的运行环境。这项警告不阻止初始化，请在宿主机检查 Docker 与发行清单。",
  },
  compose_version_undetermined: {
    detail: "无法判断 Compose 是否满足最低要求",
    advice:
      "无法确认平台组件的运行环境。这项警告不阻止初始化，请在宿主机检查 Compose 与发行清单。",
  },
  toolkit_meets_minimum: {
    advice: null,
    detail: "NVIDIA Container Toolkit 满足最低要求",
  },
  toolkit_below_minimum: {
    detail: "NVIDIA Container Toolkit 低于最低要求",
    advice:
      "模型实例无法拿到 GPU。请升级 NVIDIA Container Toolkit 后点「重新检测」。",
  },
  toolkit_version_undetermined: {
    detail: "无法判断 NVIDIA Container Toolkit 是否满足最低要求",
    advice:
      "无法确认模型实例能否使用 GPU。这项警告不阻止初始化，请在宿主机检查 NVIDIA Container Toolkit 与发行清单。",
  },
  cuda_version_unknown: {
    advice:
      "读不到 CUDA 版本，无法确认驱动兼容性，初始化无法完成。请安装或修复驱动后点「重新检测」。",
    detail: "读不到 CUDA 版本",
  },
  disk_below_minimum: {
    advice:
      "影响：当前空间连一个常见规模的模型都放不下，初始化无法完成。请扩容或更换模型存储目录后重新检测。",
    detail: "可用磁盘空间低于最小要求",
  },
  disk_below_recommended: {
    advice: "可以继续初始化，但能同时保存的模型有限，较大的模型可能下载失败。",
    detail: "可用磁盘空间低于建议值",
  },
  disk_capacity_unknown: {
    advice:
      "影响：读不到不等于足够，平台无法确认能否保存模型，初始化无法完成。请检查服务器检测服务的磁盘读取权限后重新检测。",
    detail: "读不到磁盘容量",
  },
  disk_sufficient: {
    advice: null,
    detail: "可用磁盘空间充足",
  },
  driver_meets_minimum: { advice: null, detail: "NVIDIA 驱动满足最低要求" },
  driver_below_minimum: {
    detail: "NVIDIA 驱动低于最低要求",
    advice: "模型实例无法正常使用 GPU。请升级 NVIDIA 驱动后点「重新检测」。",
  },
  driver_minimum_undetermined: {
    detail: "无法判断 NVIDIA 驱动是否满足最低要求",
    advice:
      "无法确认驱动兼容性。这项警告不阻止初始化，请检查驱动版本与发行清单。",
  },
  driver_version_unknown: {
    advice:
      "没有驱动就无法把 GPU 交给模型实例使用。请安装或修复驱动后点「重新检测」。",
    detail: "读不到 NVIDIA 驱动版本",
  },
  gpu_devices_present: {
    advice: null,
    detail: "已检测到可用 GPU，且全部 Health 正常",
  },
  gpu_discovery_unavailable: {
    advice: "读不到不等于没有 GPU。请检查驱动与检测权限后点「重新检测」。",
    detail: "GPU 检测没有完成，当前读不到 GPU 信息",
  },
  gpu_health_unknown: {
    advice:
      "影响：未知不等于异常，但这些设备此刻无法确认可用性。可以继续初始化，完成后到「系统」页重新查看。",
    detail: "部分 GPU 的 Health 读不到",
  },
  gpu_inventory_inconsistent: {
    advice:
      "影响：设备清单互相矛盾，平台无法确定可用卡数，初始化无法完成。请重新检测；若持续出现，请联系硬件维护。",
    detail: "GPU 检测报告了互相矛盾的结果",
  },
  gpu_no_devices: {
    advice:
      "影响：模型实例必须运行在 GPU 上，没有 GPU 则无法部署任何模型，初始化无法完成。请为这台服务器安装 GPU 后重新检测。",
    detail: "检测已完成，这台服务器没有可用的 GPU 设备",
  },
  gpu_unhealthy_devices: {
    advice:
      "异常的卡不会进入可部署列表，可用卡数相应减少。可以继续初始化，完成后到「系统」页查看。",
    detail: "部分 GPU 报告 Health 异常",
  },
  memory_below_minimum: {
    advice:
      "影响：内存低于最小要求时模型加载会失败，初始化无法完成。请扩容内存后重新检测。",
    detail: "内存容量低于最小要求",
  },
  memory_below_recommended: {
    advice:
      "影响：可以继续初始化，但同时加载多个模型或处理长上下文时可能变慢甚至失败。建议后续扩容内存。",
    detail: "内存容量低于建议值",
  },
  memory_capacity_unknown: {
    advice:
      "影响：读不到不等于足够，平台无法确认能否加载模型，初始化无法完成。请检查服务器检测服务的内存读取权限后重新检测。",
    detail: "读不到内存容量",
  },
  memory_sufficient: {
    advice: null,
    detail: "内存容量充足",
  },
} as const satisfies Record<BootstrapCheckReason, CheckReasonPresentation>;

const BOOTSTRAP_USERNAME_REASON_MESSAGES = {
  empty: "用户名不能为空，管理员未创建，请输入用户名。",
  invisible_character:
    "用户名包含不可见字符，管理员未创建，请只使用字母、数字、点、下划线和短横线。",
  not_text: "用户名格式不正确，管理员未创建，请重新输入。",
  too_long: "用户名超过 32 位，管理员未创建，请缩短后重试。",
  too_short: "用户名不足 3 位，管理员未创建，请补足后重试。",
  unsupported_characters:
    "用户名只能使用小写字母、数字、点、下划线和短横线，管理员未创建。例如：chen.pei。",
  whitespace: "用户名不能包含空格，管理员未创建，请改用点或短横线连接。",
} as const satisfies Record<
  (typeof BOOTSTRAP_USERNAME_REASONS)[number],
  string
>;

const BOOTSTRAP_ERROR_MESSAGES = {
  bootstrap_checks_failed:
    "系统检测存在失败项，管理员未创建。请按下方提示处理后重新检测。",
  bootstrap_token_invalid:
    "初始化口令无效或已失效，本次操作未执行。请重新粘贴安装时生成的初始化口令。",
  invalid_password: "密码不符合要求，管理员未创建，请按下方条件修改后重试。",
  invalid_username: "用户名不符合要求，管理员未创建，请修改后重试。",
  not_found: "这台服务器已经完成初始化，初始化流程已永久关闭。",
  password_confirmation_mismatch:
    "两次输入的密码不一致，管理员未创建，请重新输入。",
  username_taken: "该用户名已被占用，管理员未创建，请换一个用户名。",
} as const satisfies Record<(typeof BOOTSTRAP_ERROR_CODES)[number], string>;

function requireKnown(table: object, key: string) {
  if (!Object.hasOwn(table, key)) {
    // An identifier this client has no sentence for is a contract break, not a
    // blank row: rendering "" would hide a server change until a user reports it.
    throw new BootstrapContractError();
  }
}

export function checkLabel(id: string) {
  requireKnown(BOOTSTRAP_CHECK_LABELS, id);
  return BOOTSTRAP_CHECK_LABELS[id as keyof typeof BOOTSTRAP_CHECK_LABELS];
}

export function outcomePresentation(outcome: string): OutcomePresentation {
  requireKnown(BOOTSTRAP_OUTCOME_PRESENTATIONS, outcome);
  return BOOTSTRAP_OUTCOME_PRESENTATIONS[
    outcome as keyof typeof BOOTSTRAP_OUTCOME_PRESENTATIONS
  ];
}

export function checkReasonPresentation(
  reason: string,
): CheckReasonPresentation {
  requireKnown(BOOTSTRAP_CHECK_REASON_PRESENTATIONS, reason);
  return BOOTSTRAP_CHECK_REASON_PRESENTATIONS[
    reason as keyof typeof BOOTSTRAP_CHECK_REASON_PRESENTATIONS
  ];
}

export function usernameReasonMessage(reason: string) {
  requireKnown(BOOTSTRAP_USERNAME_REASON_MESSAGES, reason);
  return BOOTSTRAP_USERNAME_REASON_MESSAGES[
    reason as keyof typeof BOOTSTRAP_USERNAME_REASON_MESSAGES
  ];
}

export function bootstrapErrorMessage(code: string) {
  requireKnown(BOOTSTRAP_ERROR_MESSAGES, code);
  return BOOTSTRAP_ERROR_MESSAGES[
    code as keyof typeof BOOTSTRAP_ERROR_MESSAGES
  ];
}

/** Keys of each localized table, for the bidirectional guard against the server. */
export const BOOTSTRAP_PRESENTATION_KEYS = {
  checkIds: Object.keys(BOOTSTRAP_CHECK_LABELS),
  errorCodes: Object.keys(BOOTSTRAP_ERROR_MESSAGES),
  outcomes: Object.keys(BOOTSTRAP_OUTCOME_PRESENTATIONS),
  reasons: Object.keys(BOOTSTRAP_CHECK_REASON_PRESENTATIONS),
  usernameReasons: Object.keys(BOOTSTRAP_USERNAME_REASON_MESSAGES),
} as const;

/** The identifier lists this client believes the server publishes. */
export const BOOTSTRAP_CLIENT_ENUMS = {
  checkIds: BOOTSTRAP_CHECK_IDS,
  errorCodes: BOOTSTRAP_ERROR_CODES,
  outcomes: BOOTSTRAP_CHECK_OUTCOMES,
  reasons: BOOTSTRAP_CHECK_REASONS,
  usernameReasons: BOOTSTRAP_USERNAME_REASONS,
} as const;

/** format the evaluated host facts without comparing versions again. */
export function hostCheckReasonPresentation(
  reason: string,
  report: BootstrapCheckReport,
): CheckReasonPresentation {
  const base = checkReasonPresentation(reason);
  const host = report.host_environment;
  const item = (id: string) => {
    const found = host.items.filter((entry) => entry.id === id);
    const [entry] = found;
    if (found.length !== 1 || entry === undefined)
      throw new BootstrapContractError();
    return entry;
  };
  const minimum = (id: string) => {
    const value = item(id).minimum;
    if (value === null) throw new BootstrapContractError();
    return value;
  };
  const version = (id: string, label: string) => {
    const current = item(id).current;
    return current.state === "known" ? current.value : `读不到${label}版本`;
  };
  const driver = () => {
    const cuda = host.cuda_driver_version;
    return `${version("nvidia_driver", " NVIDIA 驱动")}（${cuda.state === "known" ? `CUDA ${cuda.value}` : "读不到 CUDA 版本"}）`;
  };
  if (reason === "driver_meets_minimum")
    return {
      ...base,
      detail: `${driver()}，满足最低要求 ${minimum("nvidia_driver")}`,
    };
  if (reason === "driver_below_minimum")
    return {
      ...base,
      detail: `${driver()}，低于最低要求 ${minimum("nvidia_driver")}`,
    };
  if (reason === "docker_compose_meets_minimum")
    return {
      ...base,
      detail: `Docker ${version("docker", " Docker ")}、Compose ${version("compose", " Compose ")}，满足最低要求`,
    };
  const mapping: Record<string, [string, string]> = {
    driver_minimum_undetermined: ["nvidia_driver", "NVIDIA 驱动"],
    docker_below_minimum: ["docker", "Docker"],
    compose_below_minimum: ["compose", "Compose"],
    docker_version_undetermined: ["docker", "Docker"],
    compose_version_undetermined: ["compose", "Compose"],
    toolkit_meets_minimum: [
      "nvidia_container_toolkit",
      "NVIDIA Container Toolkit",
    ],
    toolkit_below_minimum: [
      "nvidia_container_toolkit",
      "NVIDIA Container Toolkit",
    ],
    toolkit_version_undetermined: [
      "nvidia_container_toolkit",
      "NVIDIA Container Toolkit",
    ],
  };
  const target = mapping[reason];
  if (!target) return base;
  const [id, label] = target;
  const entry = item(id);
  if (entry.current.state !== "known")
    return { ...base, detail: `读不到 ${label} 版本` };
  const current =
    id === "nvidia_driver"
      ? driver()
      : id === "nvidia_container_toolkit"
        ? entry.current.value
        : `${label} ${entry.current.value}`;
  const detail = reason.endsWith("below_minimum")
    ? `${current}，低于最低要求 ${minimum(id)}`
    : reason === "toolkit_meets_minimum"
      ? `${current}，满足最低要求 ${minimum(id)}`
      : entry.minimum === null
        ? `${current}，发行清单未声明最低要求`
        : `${current}，版本无法比较，无法判断是否满足最低要求 ${entry.minimum}`;
  return { ...base, detail };
}
