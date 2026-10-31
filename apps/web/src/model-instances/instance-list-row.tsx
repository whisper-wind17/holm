import { Link } from "react-router";

import type { GpuDevice } from "../api/system-contracts";
import { MissingValue } from "../components";
import { useInstanceTelemetry } from "../telemetry/queries";
import { InstanceActions, type InstanceActionsProps } from "./instance-actions";
import {
  FAILURE_PRESENTATIONS,
  instanceFailurePresentation,
  instanceStatusPresentation,
  runtimeLabel,
} from "./presentation";

export const INSTANCE_LIST_COLUMNS = [
  ["instance", "实例 · 模型"],
  ["status", "状态"],
  ["gpus", "GPU"],
  ["running", "运行请求"],
  ["ttft", "TTFT P95（近 1 小时）"],
  ["tps", "输出 TPS"],
  ["updated", "更新时间"],
  ["actions", "操作"],
] as const;

export function InstanceListRow(
  props: InstanceActionsProps & {
    artifactName: string | undefined;
    gpuByUuid: ReadonlyMap<string, GpuDevice>;
  },
) {
  const { instance, apiClient, artifactName, gpuByUuid } = props;
  // One subscription per row supplies both the desktop cells and narrow summary.
  const telemetry = useInstanceTelemetry(apiClient, instance.id);
  const status = instanceStatusPresentation(instance.status);
  const gpuIndices = instance.gpu_uuids
    .map((uuid) => `GPU ${String(gpuByUuid.get(uuid)?.display_index ?? "?")}`)
    .join("、");
  const gpuModels = [
    ...new Set(
      instance.gpu_uuids.map(
        (uuid) => gpuByUuid.get(uuid)?.model ?? "型号未识别",
      ),
    ),
  ].join("、");
  const reason =
    instance.status === "Unhealthy"
      ? (FAILURE_PRESENTATIONS[instance.error_code ?? ""]?.reason ??
        "健康检查未通过")
      : instance.status === "Restarting"
        ? `第 ${String(instance.restart_count)} 次自动重启（上限 3 次）`
        : instance.status === "Starting"
          ? "正在启动 Runtime"
          : instance.status === "WaitingForResource"
            ? `等待 ${gpuIndices}`
            : instance.status === "Failed"
              ? instanceFailurePresentation(instance.error_code).stage
              : undefined;
  function metric(value: number | null | undefined, label: string, unit = "") {
    const unavailable =
      instance.status === "Unhealthy"
        ? "实例运行异常"
        : instance.status !== "Running"
          ? "实例未运行"
          : value == null
            ? label === "TTFT P95" &&
              telemetry.data?.window_1h.ttft_p95_ms === null
              ? "近 1 小时无请求"
              : `${label}暂不可用`
            : undefined;
    return unavailable || value == null ? (
      <MissingValue
        kind="unavailable"
        reason={unavailable ?? `${label}暂不可用`}
      />
    ) : (
      `${value.toLocaleString("zh-CN", { maximumFractionDigits: 1 })}${unit}`
    );
  }
  const running = metric(telemetry.data?.current.running, "运行请求");
  const ttft = metric(telemetry.data?.window_1h.ttft_p95_ms, "TTFT P95", " ms");
  const tps = metric(telemetry.data?.current.output_tps, "输出 TPS", " tok/s");
  return (
    <tr>
      <td data-column="instance">
        <span className="pw-instance-identity">
          <Link to={`/models/instances/${instance.id}`} title={instance.name}>
            <strong>{instance.name}</strong>
          </Link>
          <span title={artifactName}>{artifactName ?? "模型信息暂不可用"}</span>
          <span>
            {runtimeLabel(instance.runtime_type)} · TP{" "}
            {instance.tensor_parallel_size} · Context{" "}
            {instance.context_length.toLocaleString("zh-CN")}
          </span>
          <span className="pw-instance-metrics-summary">
            运行请求 {running} · TTFT P95 {ttft} · 输出 TPS {tps}
          </span>
        </span>
      </td>
      <td data-column="status">
        <span className="pw-instance-state">
          <span className="pw-instance-status">
            <span
              aria-hidden="true"
              className={`pw-service-signal pw-service-signal--${status.tone}`}
            />
            <span>{status.label}</span>
          </span>
          {reason ? (
            <span className="pw-instance-state__reason">{reason}</span>
          ) : null}
        </span>
      </td>
      <td data-column="gpus">
        {instance.status === "WaitingForResource"
          ? `等待 ${gpuIndices}`
          : instance.status === "Stopped" || instance.status === "Failed"
            ? `已释放（原 ${gpuIndices}）`
            : `${String(instance.gpu_uuids.length)} × ${gpuModels} · ${gpuIndices}`}
      </td>
      <td data-column="running" className="pw-table__numeric">
        {running}
      </td>
      <td data-column="ttft" className="pw-table__numeric">
        {ttft}
      </td>
      <td data-column="tps" className="pw-table__numeric">
        {tps}
      </td>
      <td data-column="updated">
        <time dateTime={instance.updated_at}>
          {new Date(instance.updated_at).toLocaleString("zh-CN")}
        </time>
      </td>
      <td data-column="actions">
        <InstanceActions {...props} />
      </td>
    </tr>
  );
}
