import { useEffect, useState, type CSSProperties } from "react";

import { Icon, type IconName } from "./icon";

export type ProgressStageStatus =
  "completed" | "current" | "waiting" | "skipped" | "not-run" | "failed";

export interface ProgressStage {
  detail?: string;
  id: string;
  label: string;
  percent?: number;
  status: ProgressStageStatus;
}

const STATUS_LABELS: Readonly<Record<ProgressStageStatus, string>> = {
  completed: "已完成",
  current: "进行中",
  failed: "失败",
  "not-run": "未执行",
  skipped: "已跳过",
  waiting: "等待",
};

const STATUS_ICONS: Readonly<Record<ProgressStageStatus, IconName>> = {
  completed: "success",
  current: "loading",
  failed: "error",
  "not-run": "close",
  skipped: "check",
  waiting: "notice",
};

function useReducedMotion() {
  const [reduced, setReduced] = useState(false);

  useEffect(() => {
    const query = window.matchMedia("(prefers-reduced-motion: reduce)");
    const update = () => {
      setReduced(query.matches);
    };
    update();
    query.addEventListener("change", update);
    return () => {
      query.removeEventListener("change", update);
    };
  }, []);

  return reduced;
}

export interface ProgressStagesProps {
  label: string;
  percent: number;
  stages: readonly ProgressStage[];
}

export function ProgressStages({
  label,
  percent,
  stages,
}: ProgressStagesProps) {
  const reducedMotion = useReducedMotion();
  const clampedPercent = Math.min(100, Math.max(0, percent));
  const progressStyle = {
    "--pw-progress-value": `${String(clampedPercent)}%`,
  } as CSSProperties;

  return (
    <section
      aria-label={label}
      className="pw-progress"
      data-reduced-motion={reducedMotion ? "true" : "false"}
    >
      <div className="pw-progress__summary">
        <strong>{label}</strong>
        <span>{clampedPercent}%</span>
      </div>
      <div
        aria-label={`${label} ${String(clampedPercent)}%`}
        aria-valuemax={100}
        aria-valuemin={0}
        aria-valuenow={clampedPercent}
        className="pw-progress__track"
        role="progressbar"
      >
        <span className="pw-progress__fill" style={progressStyle} />
      </div>
      <ol className="pw-progress__stages">
        {stages.map((stage) => (
          <li data-status={stage.status} key={stage.id}>
            <Icon
              className={
                stage.status === "current" ? "pw-icon--spin" : undefined
              }
              name={STATUS_ICONS[stage.status]}
              size="s"
            />
            <span>
              <strong>{stage.label}</strong>
              <span>
                {STATUS_LABELS[stage.status]}
                {stage.percent === undefined
                  ? ""
                  : ` · ${String(stage.percent)}%`}
                {stage.detail === undefined ? "" : ` · ${stage.detail}`}
              </span>
            </span>
          </li>
        ))}
      </ol>
    </section>
  );
}
