import { occupiedGpuInstances } from "./gpu-availability";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { Link, useNavigate } from "react-router";

import { ApiError, type ApiClient } from "../api/client";
import type { AiService } from "../api/ai-service-contracts";
import type { ModelInstance } from "../api/model-instance-contracts";
import type { GpuDevice } from "../api/system-contracts";
import {
  Button,
  InlineAlert,
  Modal,
  StatusBadge,
  type ButtonVariant,
} from "../components";
import { formatBytes } from "../system/system-page";
import {
  instanceActions,
  type InstanceAction,
  shortInstanceId,
} from "./presentation";
import { AI_SERVICES_QUERY_KEY } from "../ai-services/queries";
import { MODEL_INSTANCES_QUERY_KEY } from "./queries";
import { EditRetryDialog } from "./edit-retry-dialog";

type InstanceCommand =
  | "cancel"
  | "delete"
  | "restart"
  | "retry"
  | "select-gpus"
  | "start-selected"
  | "start"
  | "stop";

export interface InstanceActionsProps {
  apiClient: ApiClient;
  bindingState: "error" | "loading" | "ready";
  boundServices: readonly AiService[];
  gpuDevices: readonly GpuDevice[];
  instance: ModelInstance;
  /** shown in the "修改配置并重试" explanation (detail header only). */
  modelName?: string;
  onError: (error: ApiError | undefined) => void;
  onRemoved?: () => void;
  mode?: "list" | "detail";
}

export function InstanceActions({
  apiClient,
  bindingState,
  boundServices,
  gpuDevices,
  instance,
  modelName,
  onError,
  onRemoved,
  mode = "list",
}: InstanceActionsProps) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [notice, setNotice] = useState<string>();
  const [rowError, setRowError] = useState<ApiError>();
  const command = useMutation({
    mutationFn: async ({
      gpuUuids,
      kind,
    }: {
      gpuUuids?: readonly string[];
      kind: InstanceCommand;
    }) => {
      if (kind === "start-selected") {
        const started = await apiClient.startModelInstance(instance.id);
        if (started.status === "Starting") {
          setNotice("原 GPU 已空出，实例已在原 GPU 上启动");
          return started;
        }
        try {
          return await apiClient.selectModelInstanceGpus(
            instance.id,
            gpuUuids ?? [],
          );
        } catch (error) {
          if (
            error instanceof ApiError &&
            error.code === "invalid_instance_state"
          ) {
            setNotice("原 GPU 已空出，实例已在原 GPU 上启动");
            return started;
          }
          throw error;
        }
      }
      if (kind === "start") return apiClient.startModelInstance(instance.id);
      if (kind === "stop") return apiClient.stopModelInstance(instance.id);
      if (kind === "restart")
        return apiClient.restartModelInstance(instance.id);
      if (kind === "retry") return apiClient.retryModelInstance(instance.id);
      if (kind === "select-gpus")
        return apiClient.selectModelInstanceGpus(instance.id, gpuUuids ?? []);
      if (kind === "cancel")
        return apiClient.cancelModelInstanceDeployment(instance.id);
      return boundServices.length > 0
        ? apiClient.deleteModelInstance(instance.id, true)
        : apiClient.deleteModelInstance(instance.id);
    },
  });

  async function execute(
    kind: InstanceCommand,
    gpuUuids?: readonly string[],
  ): Promise<ApiError | undefined> {
    onError(undefined);
    setRowError(undefined);
    setNotice(undefined);
    try {
      await command.mutateAsync({ gpuUuids, kind });
      await queryClient.invalidateQueries({
        queryKey: MODEL_INSTANCES_QUERY_KEY,
      });
      if (kind === "delete") {
        await queryClient.invalidateQueries({
          queryKey: AI_SERVICES_QUERY_KEY,
        });
      }
      if (kind === "cancel" || kind === "delete") onRemoved?.();
      return undefined;
    } catch (error) {
      if (error instanceof ApiError) {
        onError(error);
        setRowError(error);
        if (kind === "start-selected")
          await queryClient.invalidateQueries({
            queryKey: MODEL_INSTANCES_QUERY_KEY,
          });
        return error;
      }
      throw error;
    }
  }

  const actions = instanceActions(instance.status).filter(
    (action) => mode === "list" || action !== "logs",
  );
  const bindingBlocked =
    bindingState !== "ready" &&
    actions.some(
      (action) =>
        action === "delete" || action === "stop" || action === "restart",
    );
  const controls = actions.map((action) => (
    <ActionControl
      action={action}
      apiClient={apiClient}
      bindingState={bindingState}
      boundServices={boundServices}
      execute={execute}
      gpuDevices={gpuDevices}
      instance={instance}
      key={action}
      navigateTo={(path) => void navigate(path)}
      pending={command.isPending}
      mode={mode}
    />
  ));
  if (mode === "list")
    return (
      <div className="pw-instance-action-result">
        <span className="pw-instance-actions">{controls}</span>
        {notice ? <span role="status">{notice}</span> : null}
        {rowError ? (
          <span role="alert">
            {rowError.status === 409
              ? "配置已更新，请刷新后重新提交；未提交的选择仍会保留。"
              : (rowError.responseMessage ?? "操作未完成，请刷新列表后重试。")}
          </span>
        ) : null}
      </div>
    );
  return (
    <span className="pw-instance-actions-detail">
      <span className="pw-instance-actions">
        {instance.status === "Failed" ? (
          <EditRetryDialog
            gpuText={describeSelectedGpuResources(
              instance.gpu_uuids,
              gpuDevices,
            )}
            instance={instance}
            modelName={modelName ?? instance.internal_model_name}
          />
        ) : null}
        {controls}
      </span>
      {notice ? <span role="status">{notice}</span> : null}
      {bindingBlocked ? (
        <span className="pw-instance-actions-detail__reason">
          服务绑定信息暂不可用，停止、重启和删除需等待绑定确认。
        </span>
      ) : null}
    </span>
  );
}

function ActionControl({
  action,
  apiClient,
  bindingState,
  boundServices,
  execute,
  gpuDevices,
  instance,
  navigateTo,
  pending,
  mode,
}: {
  action: InstanceAction;
  mode: "list" | "detail";
  apiClient: ApiClient;
  bindingState: InstanceActionsProps["bindingState"];
  boundServices: readonly AiService[];
  execute: (
    kind: InstanceCommand,
    gpuUuids?: readonly string[],
  ) => Promise<ApiError | undefined>;
  gpuDevices: readonly GpuDevice[];
  instance: ModelInstance;
  navigateTo: (path: string) => void;
  pending: boolean;
}) {
  const variant = mode === "detail" ? "secondary" : "ghost";
  const detailPath = `/models/instances/${instance.id}`;
  if (action === "logs") {
    return (
      <Button
        className="pw-instance-actions__logs"
        onClick={() => {
          navigateTo(`${detailPath}#logs`);
        }}
        size="s"
        variant={variant}
      >
        日志
      </Button>
    );
  }
  if (action === "publish") {
    return (
      <Button
        onClick={() => {
          // open the create dialog with this instance preselected.
          navigateTo(`/services?create=${encodeURIComponent(instance.id)}`);
        }}
        size="s"
        variant={variant}
      >
        发布服务
      </Button>
    );
  }
  if (action === "start") {
    return (
      <StartControl
        apiClient={apiClient}
        instance={instance}
        gpuDevices={gpuDevices}
        execute={execute}
        disabled={pending}
        triggerVariant={mode === "detail" ? "primary" : variant}
      />
    );
  }
  if (action === "retry") {
    return (
      <RetryModal
        triggerVariant={variant}
        apiClient={apiClient}
        disabled={pending}
        execute={execute}
        gpuDevices={gpuDevices}
        instance={instance}
      />
    );
  }
  if (action === "reselect-gpus") {
    return (
      <GpuSelectionModal
        triggerVariant={variant}
        disabled={pending}
        execute={execute}
        gpuDevices={gpuDevices}
        instance={instance}
      />
    );
  }
  if (action === "delete" && bindingState !== "ready") {
    return (
      <Button
        disabled
        size="s"
        title="服务绑定信息暂不可用，当前不能安全删除"
        variant={variant}
      >
        删除
      </Button>
    );
  }
  return (
    <DangerousActionModal
      triggerVariant={variant}
      action={action}
      boundServices={boundServices}
      disabled={
        pending ||
        ((action === "restart" || action === "stop") &&
          bindingState !== "ready")
      }
      execute={execute}
      instance={instance}
      navigateTo={navigateTo}
    />
  );
}

const RETRY_INVENTORY_UNAVAILABLE =
  "GPU 信息暂不可用，当前不能确认所选 GPU 是否可用。请稍后重新检查。";

function RetryModal({
  triggerVariant,
  apiClient,
  disabled,
  execute,
  gpuDevices,
  instance,
}: {
  triggerVariant: ButtonVariant;
  apiClient: ApiClient;
  disabled: boolean;
  execute: (kind: InstanceCommand) => Promise<ApiError | undefined>;
  gpuDevices: readonly GpuDevice[];
  instance: ModelInstance;
}) {
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [validationIssue, setValidationIssue] = useState<string>();
  const server = useQuery({
    enabled: open,
    queryFn: () => apiClient.getSystemServer(),
    queryKey: ["model-instance-retry", instance.id, "server"],
  });
  const instances = useQuery({
    enabled: open,
    queryFn: () => apiClient.getModelInstances(),
    queryKey: ["model-instance-retry", instance.id, "instances"],
  });
  const checking =
    open &&
    (server.isFetching ||
      instances.isFetching ||
      server.data === undefined ||
      instances.data === undefined);
  const detectedIssue =
    server.isError || instances.isError
      ? RETRY_INVENTORY_UNAVAILABLE
      : retryGpuIssue(instance, server.data, instances.data);
  const issue = validationIssue ?? detectedIssue;
  const refreshedDevices =
    server.data?.agent.status === "available"
      ? server.data.agent.gpus.devices
      : gpuDevices;
  const selectedResources = describeSelectedGpuResources(
    instance.gpu_uuids,
    refreshedDevices,
  );

  function changeOpen(next: boolean) {
    setOpen(next);
    if (next) setValidationIssue(undefined);
  }

  async function submit() {
    setSubmitting(true);
    setValidationIssue(undefined);
    try {
      const [latestServer, latestInstances] = await Promise.all([
        server.refetch(),
        instances.refetch(),
      ]);
      const latestIssue =
        latestServer.isError || latestInstances.isError
          ? RETRY_INVENTORY_UNAVAILABLE
          : retryGpuIssue(instance, latestServer.data, latestInstances.data);
      if (latestIssue !== undefined) {
        setValidationIssue(latestIssue);
        return;
      }
      const error = await execute("retry");
      if (error === undefined) setOpen(false);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Modal
      description="确认上次配置，并在提交前重新检查所选 GPU。"
      footer={
        <>
          <Button
            onClick={() => {
              setOpen(false);
            }}
          >
            返回
          </Button>
          <Button
            disabled={disabled || submitting || checking || issue !== undefined}
            onClick={() => {
              void submit();
            }}
            variant="primary"
          >
            {submitting ? "正在确认" : "确认重试"}
          </Button>
        </>
      }
      onOpenChange={changeOpen}
      open={open}
      title="重试部署模型实例？"
      trigger={
        <Button disabled={disabled} size="s" variant={triggerVariant}>
          重试
        </Button>
      }
    >
      <Button
        disabled={disabled || submitting}
        onClick={() => {
          setOpen(false);
          void navigate(
            `/models/deploy?retry_instance_id=${encodeURIComponent(instance.id)}`,
          );
        }}
      >
        修改配置并重试
      </Button>
      <dl className="pw-instance-detail__facts">
        <RetryFact label="GPU" value={selectedResources} />
        <RetryFact
          label="并行数量"
          value={instance.tensor_parallel_size.toLocaleString("zh-CN")}
        />
        <RetryFact
          label="Context Length"
          value={instance.context_length.toLocaleString("zh-CN")}
        />
      </dl>
      {checking ? (
        <InlineAlert title="正在确认 GPU" tone="info">
          正在获取最新 GPU 清单与占用状态，确认完成前不能提交。
        </InlineAlert>
      ) : issue === undefined ? (
        <InlineAlert title="GPU 当前可用" tone="positive">
          已按最新 GPU
          清单确认所选资源仍存在、健康且未被占用；提交时会再次检查。
        </InlineAlert>
      ) : (
        <InlineAlert title="当前不能重试" tone="negative">
          {issue}
          <Button
            disabled={server.isFetching || instances.isFetching}
            onClick={() => {
              setValidationIssue(undefined);
              void Promise.all([server.refetch(), instances.refetch()]);
            }}
            size="s"
          >
            重新检查 GPU
          </Button>
        </InlineAlert>
      )}
    </Modal>
  );
}

function RetryFact({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}

function describeSelectedGpuResources(
  gpuUuids: readonly string[],
  gpuDevices: readonly GpuDevice[],
) {
  const gpuByUuid = new Map(gpuDevices.map((gpu) => [gpu.uuid, gpu]));
  const selected = gpuUuids.map((uuid) => gpuByUuid.get(uuid));
  if (selected.some((gpu) => gpu === undefined) || selected.length === 0) {
    return "GPU 信息暂不可用";
  }
  const groups = new Map<string, number>();
  for (const gpu of selected) {
    if (gpu === undefined) continue;
    const label = `${gpu.model} ${formatBytes(gpu.memory_bytes)}`;
    groups.set(label, (groups.get(label) ?? 0) + 1);
  }
  return [...groups]
    .map(([label, count]) => `${label} × ${String(count)}`)
    .join("、");
}

function retryGpuIssue(
  instance: ModelInstance,
  server: Awaited<ReturnType<ApiClient["getSystemServer"]>> | undefined,
  instances: Awaited<ReturnType<ApiClient["getModelInstances"]>> | undefined,
) {
  if (server === undefined || instances === undefined) return undefined;
  if (
    server.agent.status !== "available" ||
    server.agent.gpus.discovery_state !== "ready"
  ) {
    return RETRY_INVENTORY_UNAVAILABLE;
  }
  const gpuByUuid = new Map(
    server.agent.gpus.devices.map((gpu) => [gpu.uuid, gpu]),
  );
  const missing = instance.gpu_uuids.some((uuid) => !gpuByUuid.has(uuid));
  if (missing) {
    return "所选 GPU 已不在当前清单中，不能沿用上次配置。请重新部署并选择可用 GPU。";
  }
  const unhealthy = instance.gpu_uuids.some(
    (uuid) => gpuByUuid.get(uuid)?.health !== "healthy",
  );
  if (unhealthy) {
    return "所选 GPU 当前健康异常，不能沿用上次配置。请恢复 GPU 后重新检查。";
  }
  const occupiedBy = occupiedGpuInstances(instances.items, instance.id);
  const occupied = instance.gpu_uuids.some((uuid) => occupiedBy.has(uuid));
  if (occupied) {
    return "所选 GPU 已被其他模型实例占用，不能沿用上次配置。请等待资源释放后重新检查。";
  }
  return undefined;
}

function DangerousActionModal({
  triggerVariant,
  action,
  boundServices,
  disabled,
  execute,
  instance,
  navigateTo,
}: {
  triggerVariant: ButtonVariant;
  action: "cancel" | "delete" | "restart" | "stop";
  boundServices: readonly AiService[];
  disabled: boolean;
  execute: (kind: InstanceCommand) => Promise<ApiError | undefined>;
  instance: ModelInstance;
  navigateTo: (path: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const cascade = action === "delete" && boundServices.length > 0;
  const copy = cascade
    ? {
        ...DANGEROUS_ACTION_COPY.delete,
        title: "删除模型实例和绑定的 AI 服务？",
        confirmLabel: "删除实例和服务",
        description:
          "这些 AI 服务将一并删除，使用这些 API 模型名的调用将返回 model_not_found。历史用量与调用记录保留，本地模型文件不受影响。",
      }
    : DANGEROUS_ACTION_COPY[action];
  return (
    <Modal
      description={copy.description}
      footer={
        <>
          <Button
            onClick={() => {
              setOpen(false);
            }}
          >
            返回
          </Button>
          <Button
            disabled={disabled}
            onClick={() =>
              void execute(action).then((error) => {
                if (error === undefined) setOpen(false);
              })
            }
            variant="danger"
          >
            {copy.confirmLabel}
          </Button>
        </>
      }
      onOpenChange={setOpen}
      open={open}
      title={copy.title}
      trigger={
        <Button disabled={disabled} size="s" variant={triggerVariant}>
          {copy.label}
        </Button>
      }
    >
      <p className="pw-models-dialog-note">
        将操作模型实例 {cascade ? instance.name : shortInstanceId(instance.id)}
        。
      </p>
      {action === "stop" &&
      (instance.status === "Unhealthy" || instance.status === "Restarting") ? (
        <p>停止后不再自动重启</p>
      ) : null}
      {cascade ? (
        <>
          <p>将删除以下 AI 服务：</p>
          <ul>
            {boundServices.map((service) => (
              <li key={service.id}>
                <Link to={`/services/${service.id}`}>{service.name}</Link>
                {" · "}
                {service.public_model_name}
              </li>
            ))}
          </ul>
          <p>如需保留服务，先在服务设置中切换到另一个运行中的实例。</p>
        </>
      ) : null}
      {(action === "stop" || action === "restart") &&
      boundServices.length > 0 ? (
        <>
          <p>受影响的 AI 服务：</p>
          <ul>
            {boundServices.map((service) => (
              <li key={service.id}>
                {service.name} · {service.public_model_name}
              </li>
            ))}
          </ul>
          <Button
            onClick={() => {
              navigateTo("/services");
            }}
            size="s"
            variant="ghost"
          >
            查看 AI 服务
          </Button>
        </>
      ) : null}
    </Modal>
  );
}

const DANGEROUS_ACTION_COPY = {
  cancel: {
    confirmLabel: "确认取消部署",
    description: "取消部署只删除模型实例配置，不删除本地模型与下载任务。",
    label: "取消部署",
    title: "取消这次部署？",
  },
  delete: {
    confirmLabel: "确认删除",
    description: "删除后无法恢复，但不会删除本地模型文件。",
    label: "删除",
    title: "删除这个模型实例？",
  },
  restart: {
    confirmLabel: "确认重启",
    description: "重启会保留原 GPU；已绑定的 AI 服务会短暂不可用。",
    label: "重启",
    title: "重启这个模型实例？",
  },
  stop: {
    confirmLabel: "确认停止",
    description: "停止会释放 GPU；已绑定的 AI 服务将停止提供模型响应。",
    label: "停止",
    title: "停止这个模型实例？",
  },
} as const;

function StartControl({
  apiClient,
  instance,
  gpuDevices,
  execute,
  disabled,
  triggerVariant,
}: {
  apiClient: ApiClient;
  instance: ModelInstance;
  gpuDevices: readonly GpuDevice[];
  execute: (
    kind: InstanceCommand,
    gpuUuids?: readonly string[],
  ) => Promise<ApiError | undefined>;
  disabled: boolean;
  triggerVariant: ButtonVariant;
}) {
  const [open, setOpen] = useState(false);
  const instances = useQuery({
    queryKey: MODEL_INSTANCES_QUERY_KEY,
    queryFn: () => apiClient.getModelInstances(),
    staleTime: Infinity,
  });
  const occupied = occupiedGpuInstances(
    instances.data?.items ?? [],
    instance.id,
  );
  const ownerIds = new Set(
    instance.gpu_uuids.flatMap((uuid) => occupied.get(uuid) ?? []),
  );
  const owners = (instances.data?.items ?? []).filter((item) =>
    ownerIds.has(item.id),
  );
  return (
    <>
      {instances.isError ? (
        <span role="alert">无法确认 GPU 占用，请刷新列表后重试。</span>
      ) : null}
      <Modal
        open={open}
        onOpenChange={(next) => {
          setOpen(next && owners.length > 0);
        }}
        trigger={
          <Button
            disabled={disabled || instances.isPending || instances.isError}
            size="s"
            variant={triggerVariant}
            onClick={() => {
              if (owners.length === 0) void execute("start");
            }}
          >
            启动
          </Button>
        }
        title="原 GPU 已被占用"
        description="可以等待原 GPU，或选择其他空闲且健康的 GPU。"
        footer={
          <Button
            onClick={() => {
              setOpen(false);
            }}
          >
            返回
          </Button>
        }
      >
        <p>占用模型实例：</p>
        <ul>
          {owners.map((owner) => (
            <li key={owner.id}>{owner.name}</li>
          ))}
        </ul>
        <Button
          disabled={disabled}
          onClick={() =>
            void execute("start").then((error) => {
              if (!error) setOpen(false);
            })
          }
        >
          等待原 GPU
        </Button>
        <GpuSelectionModal
          triggerVariant="primary"
          disabled={disabled}
          instance={instance}
          gpuDevices={gpuDevices}
          occupied={occupied}
          requiredCount={instance.gpu_uuids.length}
          execute={async (_kind, uuids) => {
            const error = await execute("start-selected", uuids);
            // A failed second command leaves a WaitingForResource row with its own retry action.
            setOpen(false);
            return error;
          }}
        />
      </Modal>
    </>
  );
}

function GpuSelectionModal({
  triggerVariant,
  disabled,
  execute,
  gpuDevices,
  instance,
  occupied,
  requiredCount,
}: {
  occupied?: ReadonlyMap<string, string>;
  requiredCount?: number;
  triggerVariant: ButtonVariant;
  disabled: boolean;
  execute: (
    kind: InstanceCommand,
    gpuUuids?: readonly string[],
  ) => Promise<ApiError | undefined>;
  gpuDevices: readonly GpuDevice[];
  instance: ModelInstance;
}) {
  const [open, setOpen] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(
    () => new Set(instance.gpu_uuids),
  );
  const [conflict, setConflict] = useState(false);
  const selectedDevices = useMemo(
    () => gpuDevices.filter(({ uuid }) => selected.has(uuid)),
    [gpuDevices, selected],
  );
  const selectedSpec = selectedDevices[0];

  function changeOpen(next: boolean) {
    setOpen(next);
    if (next) {
      setSelected(
        new Set(requiredCount === undefined ? instance.gpu_uuids : []),
      );
      setConflict(false);
    }
  }

  async function submit() {
    setConflict(false);
    const error = await execute("select-gpus", [...selected]);
    if (error === undefined) setOpen(false);
    else if (error.status === 409) setConflict(true);
  }

  return (
    <Modal
      description="选择健康且规格一致的 GPU；提交时会重新校验并原子预留。"
      footer={
        <>
          <Button
            onClick={() => {
              setOpen(false);
            }}
          >
            返回
          </Button>
          <Button
            disabled={
              disabled ||
              selected.size === 0 ||
              (requiredCount !== undefined &&
                (selected.size !== requiredCount ||
                  selectedDevices.some(
                    (gpu) =>
                      gpu.health !== "healthy" || occupied?.has(gpu.uuid),
                  )))
            }
            onClick={() => {
              void submit();
            }}
            variant="primary"
          >
            确认重新选择
          </Button>
        </>
      }
      onOpenChange={changeOpen}
      open={open}
      size="l"
      title="重新选择 GPU"
      trigger={
        <Button disabled={disabled} size="s" variant={triggerVariant}>
          重新选择 GPU
        </Button>
      }
    >
      {requiredCount === undefined ? null : (
        <p>请选择 {requiredCount} 张空闲且健康的 GPU。</p>
      )}
      {conflict ? (
        <InlineAlert title="配置已更新" tone="notice">
          请刷新后重新提交；当前 GPU 选择已保留，可直接重试。
        </InlineAlert>
      ) : null}
      {gpuDevices.length === 0 ? (
        <InlineAlert title="没有可用的 GPU 信息" tone="negative">
          当前不能重新选择资源，请稍后重试。
        </InlineAlert>
      ) : (
        <fieldset className="pw-deploy-fieldset">
          <legend>选择 GPU（可多选）</legend>
          <div className="pw-gpu-grid">
            {gpuDevices.map((gpu) => {
              const incompatible =
                selectedSpec !== undefined &&
                !selected.has(gpu.uuid) &&
                (gpu.model !== selectedSpec.model ||
                  gpu.memory_bytes !== selectedSpec.memory_bytes);
              const unavailable =
                gpu.health !== "healthy" ||
                incompatible ||
                Boolean(occupied?.has(gpu.uuid));
              return (
                <label className="pw-gpu-choice" key={gpu.uuid}>
                  <span className="pw-gpu-choice__head">
                    <input
                      checked={selected.has(gpu.uuid)}
                      disabled={unavailable}
                      onChange={() => {
                        setSelected((current) => {
                          const next = new Set(current);
                          if (next.has(gpu.uuid)) next.delete(gpu.uuid);
                          else next.add(gpu.uuid);
                          return next;
                        });
                      }}
                      type="checkbox"
                    />
                    <strong>GPU {gpu.display_index ?? "—"}</strong>
                    <StatusBadge
                      icon={unavailable ? "error" : "success"}
                      text={
                        gpu.health !== "healthy"
                          ? "健康异常"
                          : occupied?.has(gpu.uuid)
                            ? "已占用"
                            : incompatible
                              ? "规格不一致"
                              : "可选择"
                      }
                      tone={unavailable ? "negative" : "positive"}
                    />
                  </span>
                  <span>
                    {gpu.model} · {formatBytes(gpu.memory_bytes)}
                  </span>
                </label>
              );
            })}
          </div>
        </fieldset>
      )}
    </Modal>
  );
}
