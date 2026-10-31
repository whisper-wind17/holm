import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { useNavigate } from "react-router";

import { ApiError, type ApiClient } from "../api/client";
import type { GpuDevice } from "../api/system-contracts";
import { Button, EmptyState, InlineAlert } from "../components";
import { formatBytes } from "../system/system-page";
import { InstanceListRow, INSTANCE_LIST_COLUMNS } from "./instance-list-row";
import { INSTANCE_STATUS_PRESENTATIONS, runtimeLabel } from "./presentation";
import { useModelInstances } from "./queries";

export function ModelInstancesTab({ apiClient }: { apiClient: ApiClient }) {
  const navigate = useNavigate();
  const instances = useModelInstances(apiClient);
  const artifacts = useQuery({
    queryFn: () => apiClient.getModelArtifacts(),
    queryKey: ["model-instance-artifacts"],
  });
  const server = useQuery({
    queryFn: () => apiClient.getSystemServer(),
    queryKey: ["model-instance-server"],
  });
  const services = useQuery({
    queryFn: () => apiClient.getAiServices(),
    queryKey: ["ai-services"],
  });
  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState("");
  const [runtimeFilter, setRuntimeFilter] = useState("");
  const [gpuFilter, setGpuFilter] = useState("");
  function clearFilters() {
    setSearch("");
    setStatusFilter("");
    setRuntimeFilter("");
    setGpuFilter("");
  }
  const [commandError, setCommandError] = useState<ApiError>();

  const artifactById = useMemo(
    () =>
      new Map(
        (artifacts.data?.items ?? []).map((artifact) => [
          artifact.id,
          artifact,
        ]),
      ),
    [artifacts.data],
  );
  const gpuByUuid = useMemo(() => {
    const devices =
      server.data?.agent.status === "available"
        ? server.data.agent.gpus.devices
        : [];
    return new Map(devices.map((gpu) => [gpu.uuid, gpu]));
  }, [server.data]);
  const gpuDevices =
    server.data?.agent.status === "available"
      ? server.data.agent.gpus.devices
      : [];

  if (instances.data === undefined && instances.isError) {
    return (
      <InlineAlert title="无法获取模型实例" tone="negative">
        模型实例列表无法显示。请检查平台连接后重试。
        <Button onClick={() => void instances.refetch()} size="s">
          重试
        </Button>
      </InlineAlert>
    );
  }

  const allRows = instances.data?.items ?? [];
  const filtered = Boolean(
    search || statusFilter || runtimeFilter || gpuFilter,
  );
  const rows = allRows
    .filter((item) => {
      const query = search.trim().toLocaleLowerCase();
      return (
        (!query ||
          `${item.name} ${artifactById.get(item.artifact_id)?.display_name ?? ""}`
            .toLocaleLowerCase()
            .includes(query)) &&
        (!statusFilter || item.status === statusFilter) &&
        (!runtimeFilter || item.runtime_type === runtimeFilter) &&
        (!gpuFilter || item.gpu_uuids.includes(gpuFilter))
      );
    })
    .sort((a, b) => {
      const priority = (status: string) =>
        status === "Unhealthy" || status === "Failed" ? 0 : 1;
      return (
        priority(a.status) - priority(b.status) ||
        Date.parse(b.updated_at) - Date.parse(a.updated_at)
      );
    });
  return (
    <section className="pw-instances-section">
      <header className="pw-instances-section__head">
        <span>
          <h2>模型实例</h2>
          <p>部署并管理提供推理能力的运行实例。</p>
        </span>
        <Button
          onClick={() => void navigate("/models/deploy")}
          variant="primary"
        >
          部署模型
        </Button>
      </header>
      {commandError === undefined ? null : (
        <InlineAlert title="操作未完成" tone="negative">
          {commandError.status === 409
            ? "配置已更新，请刷新后重新提交；未提交的选择仍会保留。"
            : (commandError.responseMessage ??
              "模型实例状态未改变。请刷新列表后重试。")}
        </InlineAlert>
      )}
      {instances.realtimeStatus === "polling" ? (
        <InlineAlert title="实时连接已中断" tone="notice">
          已降级为每 5 秒轮询，数据可能延迟；连接恢复后会自动回到实时更新。
        </InlineAlert>
      ) : instances.realtimeStatus === "connecting" &&
        instances.data !== undefined ? (
        <InlineAlert title="正在连接实时更新" tone="info">
          当前数据可能延迟，连接建立后会自动更新。
        </InlineAlert>
      ) : null}
      {services.isError ? (
        <InlineAlert title="无法确认服务绑定" tone="negative">
          停止、重启和删除暂不可用，以免影响未知的 AI 服务。请恢复连接后重试。
        </InlineAlert>
      ) : services.isFetching && services.data !== undefined ? (
        <InlineAlert title="服务绑定正在更新" tone="info">
          当前数据仍可查看，危险操作会在更新完成后恢复。
        </InlineAlert>
      ) : null}
      <div className="pw-instances-section__filters">
        <label>
          搜索实例或模型
          <input
            type="search"
            value={search}
            onChange={(event) => {
              setSearch(event.target.value);
            }}
            placeholder="搜索实例名或模型名"
          />
        </label>
        <label>
          状态
          <select
            value={statusFilter}
            onChange={(event) => {
              setStatusFilter(event.target.value);
            }}
          >
            <option value="">全部状态</option>
            {Object.entries(INSTANCE_STATUS_PRESENTATIONS).map(
              ([value, presentation]) => (
                <option key={value} value={value}>
                  {presentation.label}
                </option>
              ),
            )}
          </select>
        </label>
        <label>
          Runtime
          <select
            value={runtimeFilter}
            onChange={(event) => {
              setRuntimeFilter(event.target.value);
            }}
          >
            <option value="">全部 Runtime</option>
            {(["vllm", "sglang"] as const).map((value) => (
              <option key={value} value={value}>
                {runtimeLabel(value)}
              </option>
            ))}
          </select>
        </label>
        <label>
          GPU
          <select
            value={gpuFilter}
            onChange={(event) => {
              setGpuFilter(event.target.value);
            }}
          >
            <option value="">全部 GPU</option>
            {[
              ...new Set([
                ...gpuDevices.map((gpu) => gpu.uuid),
                ...allRows.flatMap((item) => item.gpu_uuids),
              ]),
            ].map((uuid) => (
              <option key={uuid} value={uuid}>
                GPU {gpuByUuid.get(uuid)?.display_index ?? "?"}
              </option>
            ))}
          </select>
        </label>
      </div>
      <div
        className="pw-table-wrap"
        aria-busy={instances.isPending}
        aria-label={instances.isPending ? "模型实例，正在加载" : "模型实例"}
      >
        <table className="pw-table">
          <caption className="pw-visually-hidden">模型实例</caption>
          <thead>
            <tr>
              {INSTANCE_LIST_COLUMNS.map(([id, label]) => (
                <th
                  key={id}
                  data-column={id}
                  scope="col"
                  aria-label={id === "ttft" ? label : undefined}
                >
                  {id === "ttft" ? (
                    <>
                      TTFT P95
                      <span className="pw-instance-column-window">
                        近 1 小时
                      </span>
                    </>
                  ) : (
                    label
                  )}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {instances.isPending
              ? Array.from({ length: 6 }, (_, index) => (
                  <tr key={index}>
                    {INSTANCE_LIST_COLUMNS.map(([id]) => (
                      <td key={id} data-column={id}>
                        <span className="sk pw-table__skeleton" />
                      </td>
                    ))}
                  </tr>
                ))
              : rows.map((item) => (
                  <InstanceListRow
                    key={item.id}
                    apiClient={apiClient}
                    instance={item}
                    artifactName={
                      artifactById.get(item.artifact_id)?.display_name
                    }
                    gpuByUuid={gpuByUuid}
                    gpuDevices={gpuDevices}
                    bindingState={
                      services.isError
                        ? "error"
                        : services.data === undefined
                          ? "loading"
                          : "ready"
                    }
                    boundServices={(services.data?.items ?? []).filter(
                      ({ primary_instance_id }) =>
                        primary_instance_id === item.id,
                    )}
                    onError={setCommandError}
                  />
                ))}
            {!instances.isPending && rows.length === 0 ? (
              <tr>
                <td className="pw-table__empty" colSpan={8}>
                  <EmptyState
                    icon="layers"
                    title={
                      filtered ? "没有符合筛选条件的模型实例" : "还没有模型实例"
                    }
                    description={
                      filtered
                        ? "请调整搜索或筛选条件。"
                        : "从一个可用的本地模型开始部署。"
                    }
                    action={
                      filtered ? (
                        <Button onClick={clearFilters}>清除筛选</Button>
                      ) : (
                        <Button
                          onClick={() => void navigate("/models/deploy")}
                          variant="primary"
                        >
                          部署模型
                        </Button>
                      )
                    }
                  />
                </td>
              </tr>
            ) : null}
          </tbody>
        </table>
      </div>
    </section>
  );
}

export function describeGpu(gpu: GpuDevice) {
  return `${gpu.model} · ${formatBytes(gpu.memory_bytes)}`;
}
