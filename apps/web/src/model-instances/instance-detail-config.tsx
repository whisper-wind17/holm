import { useQuery } from "@tanstack/react-query";
import type { ReactNode } from "react";

import type { ApiClient } from "../api/client";
import type { ModelInstance } from "../api/model-instance-contracts";
import type { GpuDevice } from "../api/system-contracts";
import { InlineAlert, MissingValue } from "../components";
import { formatBytes } from "../system/system-page";
import { runtimeLabel } from "./presentation";

const SOURCE_LABELS: Readonly<Record<string, string>> = {
  "Local Path": "本地目录",
};

/**
 * configuration tab. Read-only. The model-detail and runtime
 * queries live here (not in the page) so they only run while the tab is open.
 */
export function InstanceConfiguration({
  apiClient,
  gpuByUuid,
  instance,
  modelId,
}: {
  apiClient: ApiClient;
  gpuByUuid: ReadonlyMap<string, GpuDevice>;
  instance: ModelInstance;
  /** From the artifact list, so the Runtime version does not need the detail call. */
  modelId: string | undefined;
}) {
  const detail = useQuery({
    queryFn: () => apiClient.artifactDetail(instance.artifact_id),
    queryKey: ["model-instance-config", "artifact", instance.artifact_id],
  });
  const resolvedModelId = detail.data?.model_id ?? modelId;
  const options = useQuery({
    enabled: resolvedModelId !== undefined,
    queryFn: () => apiClient.getModelDeploymentOptions(resolvedModelId ?? ""),
    queryKey: ["model-instance-config", "runtimes", resolvedModelId],
  });
  const released =
    instance.status === "Stopped" || instance.status === "Failed";
  const version = options.data?.runtimes.find(
    ({ name }) => name === instance.runtime_type,
  )?.version;
  const model = detail.data;
  const modelUnavailable = (
    <MissingValue kind="unavailable" reason="模型信息暂不可用" />
  );
  const modelValue = (value: ReactNode) =>
    detail.isPending
      ? "正在加载"
      : model === undefined
        ? modelUnavailable
        : value;
  return (
    <section className="pw-instance-detail__section pw-instance-config">
      <h2>配置</h2>
      {detail.isError ? (
        <InlineAlert title="模型信息暂不可用" tone="notice">
          模型源、Revision、精度和量化暂时无法获取，其余配置照常显示。
        </InlineAlert>
      ) : null}
      <dl className="pw-instance-detail__facts">
        <Fact
          label="模型源"
          value={modelValue(
            model === undefined
              ? null
              : `${SOURCE_LABELS[model.source] ?? model.source} · ${model.repository_id}`,
          )}
        />
        <Fact label="Revision" value={modelValue(model?.resolved_revision)} />
        <Fact
          label="精度"
          value={modelValue(model?.model.precision ?? "未识别")}
        />
        <Fact
          label="量化"
          value={modelValue(model?.model.quantization ?? "未识别")}
        />
        <Fact
          label="Runtime / 版本"
          value={
            options.isPending && resolvedModelId !== undefined
              ? `${runtimeLabel(instance.runtime_type)} · 版本正在加载`
              : version === undefined
                ? `${runtimeLabel(instance.runtime_type)} · 版本未知`
                : `${runtimeLabel(instance.runtime_type)} ${version}`
          }
        />
        <Fact
          label="GPU 分配"
          value={gpuAllocation(instance, gpuByUuid, released)}
        />
        <Fact
          label="Tensor Parallel"
          value={String(instance.tensor_parallel_size)}
        />
        <Fact
          label="上下文长度"
          value={instance.context_length.toLocaleString("zh-CN")}
        />
        <Fact
          label="显存比例"
          value={instance.gpu_memory_utilization.toFixed(2)}
        />
        <Fact label="配置版本" value={String(instance.config_version)} />
      </dl>
      <section
        aria-labelledby="instance-internal-endpoint"
        className="pw-instance-config__endpoint"
      >
        <h3 id="instance-internal-endpoint">
          内部 Endpoint
          <span className="pw-instance-config__admin-tag">仅管理员可见</span>
        </h3>
        <dl className="pw-instance-detail__facts">
          <Fact label="地址" value={instance.internal_endpoint ?? "未运行"} />
          <Fact label="内部模型名" value={instance.internal_model_name} />
        </dl>
        <p className="pw-instance-config__hint">
          平台网关内部使用。用户调用一律走 AI 服务的 API 模型名。
        </p>
      </section>
    </section>
  );
}

function gpuAllocation(
  instance: ModelInstance,
  gpuByUuid: ReadonlyMap<string, GpuDevice>,
  released: boolean,
) {
  if (instance.gpu_uuids.length === 0) return "未分配";
  const labels = instance.gpu_uuids.map((uuid) => {
    const gpu = gpuByUuid.get(uuid);
    if (gpu === undefined) return "GPU 信息暂不可用";
    const index = gpu.display_index === null ? "—" : String(gpu.display_index);
    return `GPU ${index} · ${gpu.model} ${formatBytes(gpu.memory_bytes)}`;
  });
  return `${labels.join("；")}${released ? "（已释放，启动时优先使用）" : ""}`;
}

function Fact({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}
