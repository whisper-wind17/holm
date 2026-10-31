import { useEffect, useState } from "react";

import type { ApiClient } from "../api/client";
import type {
  ModelInstance,
  ModelInstanceEvent,
} from "../api/model-instance-contracts";
import { Button, InlineAlert } from "../components";
import { safeLogText } from "./instance-logs";
import { instanceHealthConclusion } from "./presentation";
import { useInstanceLogs } from "./queries";

const AUTO_RESTART_NOTE =
  "平台会结束推理进程，按 5、15、45 秒间隔自动重启；10 分钟内累计 3 次失败转为运行失败并释放 GPU。期间关联的 AI 服务不可用；不想等待可以停止，立即释放 GPU。";

/** status banners for the states that have no dedicated block. */
export function InstanceStatusBanner({
  gpuLabels,
  instance,
  onViewErrorLogs,
}: {
  /** "GPU 2、GPU 3" for the selected cards, or undefined when unknown. */
  gpuLabels: string | undefined;
  instance: ModelInstance;
  onViewErrorLogs: () => void;
}) {
  if (instance.status === "Unhealthy") {
    return (
      <InlineAlert title="实例运行异常" tone="negative">
        {instanceHealthConclusion(instance.error_code)}
        {AUTO_RESTART_NOTE}
        <Button onClick={onViewErrorLogs} size="s" variant="ghost">
          查看错误日志
        </Button>
      </InlineAlert>
    );
  }
  if (instance.status === "Restarting") {
    return (
      <InlineAlert title="重启中" tone="notice">
        {instance.restart_count > 0
          ? `第 ${String(instance.restart_count)} 次自动重启（上限 3 次）。`
          : "正在重启，原 GPU 保留。"}
        {AUTO_RESTART_NOTE}
      </InlineAlert>
    );
  }
  if (instance.status === "WaitingForResource") {
    return (
      <InlineAlert title="等待 GPU 资源" tone="notice">
        {gpuLabels === undefined
          ? "正在等待所选 GPU 释放。"
          : `正在等待 ${gpuLabels} 释放。`}
        GPU 释放后自动启动，平台不会自动换卡；也可以重新选择 GPU 或取消部署。
      </InlineAlert>
    );
  }
  if (instance.status === "Starting") {
    return (
      <InlineAlert title="启动中" tone="info">
        正在启动 Runtime。启动完成后关联的 AI 服务自动可用。
      </InlineAlert>
    );
  }
  return null;
}

/**
 * deployment progress. Shown only for WaitingForResource and for
 * a first start; an instance that has already been Running or Stopped is a
 * restart and shows nothing. Events are newest-window only, which is enough to
 * find the earlier Running/Stopped transition for the common cases.
 */
export function isFreshDeployment(
  instance: ModelInstance,
  events: readonly ModelInstanceEvent[] | undefined,
) {
  if (instance.status === "WaitingForResource") return true;
  if (instance.status !== "Starting" || events === undefined) return false;
  return !events.some(
    ({ to_status }) => to_status === "Running" || to_status === "Stopped",
  );
}

type StepState = "done" | "current" | "todo";

const STEP_MARKS: Readonly<Record<StepState, string>> = {
  current: "●",
  done: "✓",
  todo: "○",
};
const STEP_STATE_LABELS: Readonly<Record<StepState, string>> = {
  current: "进行中",
  done: "已完成",
  todo: "未开始",
};

export function DeploymentProgress({
  apiClient,
  events,
  gpuLabels,
  instance,
  onViewLogs,
}: {
  apiClient: ApiClient;
  events: readonly ModelInstanceEvent[];
  gpuLabels: string | undefined;
  instance: ModelInstance;
  onViewLogs: () => void;
}) {
  const waiting = instance.status === "WaitingForResource";
  const logs = useInstanceLogs(apiClient, instance.id, {
    enabled: !waiting,
    refetchInterval: 5_000,
  });
  const startedAt = events
    .filter(({ to_status }) => to_status === "Starting")
    .map(({ occurred_at }) => Date.parse(occurred_at))
    .sort((a, b) => b - a)[0];
  const now = useNow(!waiting);
  const lastLog = logs.data?.entries.at(-1);
  const steps: Array<{
    detail?: string;
    label: string;
    state: StepState;
  }> = [
    { label: "校验本地模型", state: "done" },
    {
      detail: waiting
        ? gpuLabels === undefined
          ? "等待所选 GPU 释放"
          : `等待 ${gpuLabels} 释放`
        : undefined,
      label: "预留 GPU",
      state: waiting ? "current" : "done",
    },
    {
      detail: waiting
        ? undefined
        : [
            startedAt === undefined
              ? undefined
              : `已用 ${formatElapsed(now - startedAt)}`,
            lastLog === undefined ? undefined : safeLogText(lastLog.text),
          ]
            .filter((part) => part !== undefined)
            .join(" · ") || undefined,
      label: "启动 Runtime",
      state: waiting ? "todo" : "current",
    },
    { label: "健康检查通过", state: "todo" },
  ];
  return (
    <section
      aria-labelledby="instance-deploy-progress"
      className="pw-instance-overview__panel"
    >
      <div className="pw-instance-overview__panel-head">
        <h2 id="instance-deploy-progress">部署进度</h2>
        <Button onClick={onViewLogs} size="s" variant="ghost">
          查看详细日志
        </Button>
      </div>
      <ol className="pw-instance-progress">
        {steps.map((step) => (
          <li data-state={step.state} key={step.label}>
            <span aria-hidden="true" className="pw-instance-progress__mark">
              {STEP_MARKS[step.state]}
            </span>
            <span>
              <strong>{step.label}</strong>
              <span className="pw-visually-hidden">
                {STEP_STATE_LABELS[step.state]}
              </span>
              {step.detail === undefined ? null : (
                <small className="pw-instance-progress__detail">
                  {step.detail}
                </small>
              )}
            </span>
          </li>
        ))}
      </ol>
      <p className="pw-instance-overview__note">
        {waiting
          ? "运行配置保留；平台不会静默换卡。"
          : "离开页面不会终止部署，回到实例详情可继续查看。"}
      </p>
    </section>
  );
}

function useNow(active: boolean) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    setNow(Date.now());
    const timer = setInterval(() => {
      setNow(Date.now());
    }, 1_000);
    return () => {
      clearInterval(timer);
    };
  }, [active]);
  return now;
}

function formatElapsed(milliseconds: number) {
  const seconds = Math.max(0, Math.floor(milliseconds / 1_000));
  return seconds < 60
    ? `${String(seconds)} 秒`
    : `${String(Math.floor(seconds / 60))} 分 ${String(seconds % 60)} 秒`;
}
