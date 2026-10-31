import type {
  ModelSourceConfigurationState,
  ModelSourceType,
  ModelSourceValidationAction,
  ModelSourceValidationReason,
  ModelSourceValidationStatus,
} from "../api/model-source-contracts";
import {
  MODEL_SOURCE_CONFIGURATION_STATES,
  MODEL_SOURCE_TYPES,
  MODEL_SOURCE_VALIDATION_ACTIONS,
  MODEL_SOURCE_VALIDATION_REASONS,
  MODEL_SOURCE_VALIDATION_STATUSES,
  ModelSourceContractError,
} from "../api/model-source-contracts";
import type { IconName, StatusTone } from "../components";
import { originTypeLabel } from "../local-models/presentation";

interface StatusPresentation {
  icon: IconName;
  label: string;
  tone: StatusTone;
}

export const MODEL_SOURCE_TYPE_LABELS = Object.fromEntries(
  MODEL_SOURCE_TYPES.map((sourceType) => [
    sourceType,
    originTypeLabel(sourceType),
  ]),
) as Readonly<Record<ModelSourceType, string>>;

export const MODEL_SOURCE_CONFIGURATION_PRESENTATIONS = {
  configured: { icon: "success", label: "已配置", tone: "positive" },
  key_unavailable: {
    icon: "error",
    label: "凭证不可用",
    tone: "negative",
  },
  unconfigured: { icon: "info", label: "未配置", tone: "idle" },
} as const satisfies Record<ModelSourceConfigurationState, StatusPresentation>;

export const MODEL_SOURCE_VALIDATION_PRESENTATIONS = {
  accessible: { icon: "success", label: "可访问", tone: "positive" },
  invalid_credential: {
    icon: "error",
    label: "凭证错误或已过期",
    tone: "negative",
  },
  not_found: { icon: "search", label: "来源中未找到", tone: "negative" },
  permission_denied: {
    icon: "lock",
    label: "没有访问权限",
    tone: "negative",
  },
  rate_limited: { icon: "notice", label: "来源暂时限流", tone: "notice" },
} as const satisfies Record<ModelSourceValidationStatus, StatusPresentation>;

export const MODEL_SOURCE_VALIDATION_REASON_LABELS = {
  credential_invalid: "凭证错误或已过期。",
  credential_valid: "凭证可用于访问该来源。",
  source_not_found: "来源中未找到目标仓库。",
  source_not_found_or_inaccessible: "目标仓库不存在，或当前凭证无权访问。",
  source_permission_denied: "当前凭证无权访问目标仓库。",
  source_rate_limited: "模型源暂时限制了请求频率。",
  validation_failed: "验证过程失败，当前无法确认凭证状态。",
  validation_timeout: "验证请求超时。",
  validator_unavailable: "验证服务尚未安装，当前无法验证。",
} as const satisfies Record<ModelSourceValidationReason, string>;

export const MODEL_SOURCE_VALIDATION_ACTION_LABELS = {
  check_source_access: "请检查账号是否有权访问目标仓库。",
  check_source_reference: "请检查仓库标识是否正确。",
  check_source_reference_and_configure_credential:
    "请检查仓库标识；如为私有仓库，请使用“配置凭证”。",
  install_validator: "请联系平台部署管理员安装对应的模型源支持。",
  retry_later: "请稍后重试。",
  update_credential: "请使用“更换凭证”更新 Access Token。",
} as const satisfies Record<ModelSourceValidationAction, string>;

export function assertModelSourcePresentationMappings() {
  assertExactMappingKeys(
    Object.keys(MODEL_SOURCE_TYPE_LABELS),
    MODEL_SOURCE_TYPES,
  );
  assertExactMappingKeys(
    Object.keys(MODEL_SOURCE_CONFIGURATION_PRESENTATIONS),
    MODEL_SOURCE_CONFIGURATION_STATES,
  );
  assertExactMappingKeys(
    Object.keys(MODEL_SOURCE_VALIDATION_PRESENTATIONS),
    MODEL_SOURCE_VALIDATION_STATUSES,
  );
  assertExactMappingKeys(
    Object.keys(MODEL_SOURCE_VALIDATION_REASON_LABELS),
    MODEL_SOURCE_VALIDATION_REASONS,
  );
  assertExactMappingKeys(
    Object.keys(MODEL_SOURCE_VALIDATION_ACTION_LABELS),
    MODEL_SOURCE_VALIDATION_ACTIONS,
  );
  assertDistinctValidationPresentations(
    Object.values(MODEL_SOURCE_VALIDATION_PRESENTATIONS),
  );
}

export function assertDistinctValidationPresentations(
  presentations: readonly StatusPresentation[],
) {
  const icons = new Set(presentations.map(({ icon }) => icon));
  const labels = new Set(presentations.map(({ label }) => label));
  if (
    icons.size !== MODEL_SOURCE_VALIDATION_STATUSES.length ||
    labels.size !== MODEL_SOURCE_VALIDATION_STATUSES.length
  ) {
    throw new ModelSourceContractError();
  }
}

export function modelSourceTypeLabel(value: string) {
  assertModelSourcePresentationMappings();
  return mapped(MODEL_SOURCE_TYPE_LABELS, value);
}

export function modelSourceConfigurationPresentation(value: string) {
  assertModelSourcePresentationMappings();
  return mapped(MODEL_SOURCE_CONFIGURATION_PRESENTATIONS, value);
}

export function modelSourceValidationPresentation(value: string) {
  assertModelSourcePresentationMappings();
  return mapped(MODEL_SOURCE_VALIDATION_PRESENTATIONS, value);
}

export function modelSourceValidationReasonLabel(value: string) {
  assertModelSourcePresentationMappings();
  return mapped(MODEL_SOURCE_VALIDATION_REASON_LABELS, value);
}

export function modelSourceValidationActionLabel(value: string) {
  assertModelSourcePresentationMappings();
  return mapped(MODEL_SOURCE_VALIDATION_ACTION_LABELS, value);
}

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
    throw new ModelSourceContractError();
  }
}

function mapped<T>(mapping: Readonly<Record<string, T>>, value: string): T {
  if (!Object.hasOwn(mapping, value)) throw new ModelSourceContractError();
  return mapping[value] as T;
}
