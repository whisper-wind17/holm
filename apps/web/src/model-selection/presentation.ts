import type { IconName, StatusTone } from "../components";
import {
  LOCAL_AVAILABILITIES,
  ModelSelectionContractError,
  type LocalAvailability,
} from "../api/model-selection-contracts";

interface AvailabilityPresentation {
  icon: IconName;
  label: string;
  previewLabel: string;
  tone: StatusTone;
}

export const LOCAL_AVAILABILITY_PRESENTATIONS = {
  absent: {
    icon: "apiServices",
    label: "未下载",
    previewLabel: "本地还没有这个版本",
    tone: "idle",
  },
  artifact_ready: {
    icon: "success",
    label: "本地可用",
    previewLabel: "已有可用的本地模型",
    tone: "positive",
  },
  download_active: {
    icon: "loading",
    label: "下载中",
    previewLabel: "正在下载",
    tone: "info",
  },
} as const satisfies Record<LocalAvailability, AvailabilityPresentation>;

export function assertLocalAvailabilityPresentations() {
  const actual = Object.keys(LOCAL_AVAILABILITY_PRESENTATIONS).sort();
  const expected = [...LOCAL_AVAILABILITIES].sort();
  if (
    actual.length !== expected.length ||
    actual.some((value, index) => value !== expected[index])
  ) {
    throw new ModelSelectionContractError();
  }
}

export function localAvailabilityPresentation(value: string) {
  assertLocalAvailabilityPresentations();
  if (!Object.hasOwn(LOCAL_AVAILABILITY_PRESENTATIONS, value))
    throw new ModelSelectionContractError();
  return LOCAL_AVAILABILITY_PRESENTATIONS[
    value as LocalAvailability
  ] as AvailabilityPresentation;
}
