import { HelpButton } from "../help/help-center";
import { HELP_TOPICS } from "../help/content";
import { z } from "zod";
import { Controller, useForm } from "react-hook-form";
import { instanceNameSchema } from "../api/model-instance-contracts";
import { useMutation, useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router";

import { ApiError, type ApiClient } from "../api/client";
import type { DeploymentRuntimeOption } from "../api/deployment-options-contracts";
import type { ModelArtifactItem } from "../api/model-acquisition-contracts";
import type { RuntimeType } from "../api/model-instance-contracts";
import type { ModelCatalogItem } from "../api/model-selection-contracts";
import type { GpuDevice } from "../api/system-contracts";
import { Button, InlineAlert, StatusBadge, TextField } from "../components";
import { originTypeLabel } from "../local-models/presentation";
import { MODEL_DOWNLOAD_PATH } from "../model-selection/download-page";
import { formatBytes } from "../system/system-page";
import {
  minimumGpuCount,
  modelInfoText,
  useArtifactModelInfo,
} from "./artifact-model-info";
import { describeGpu } from "./instances-tab";

import { defaultInstanceName } from "./instance-names";

import { occupiedGpuInstances } from "./gpu-availability";

const GPU_HEALTH_REASONS: Record<GpuDevice["health_reasons"][number], string> =
  {
    ecc_errors_detected: "检测到显存纠错错误",
    query_unsupported: "不支持健康信息采集",
    query_failed: "健康信息采集失败",
    protocol_invalid: "健康信息格式异常",
  };

export const MODEL_DEPLOY_PATH = "/models/deploy";

// Deployment narrows the accepted GPU memory ratio; 0.90 stays recommended.
const MEMORY_UTILIZATION_MIN = 0.8;
const MEMORY_UTILIZATION_MAX = 0.95;
const MEMORY_UTILIZATION_ERROR = "显存比例需在 0.80～0.95 之间，推荐 0.90";

function memoryUtilizationValid(value: number) {
  return (
    Number.isFinite(value) &&
    value >= MEMORY_UTILIZATION_MIN &&
    value <= MEMORY_UTILIZATION_MAX
  );
}

export function ModelDeployWizard({ apiClient }: { apiClient: ApiClient }) {
  const [searchParams] = useSearchParams();
  const retryInstanceId = searchParams.get("retry_instance_id");
  return (
    <DeployWizardSession
      key={retryInstanceId ?? "deploy"}
      apiClient={apiClient}
      retryInstanceId={retryInstanceId}
    />
  );
}

function DeployWizardSession({
  apiClient,
  retryInstanceId,
}: {
  apiClient: ApiClient;
  retryInstanceId: string | null;
}) {
  const heading = useRef<HTMLHeadingElement>(null);
  const retryMode = retryInstanceId !== null;
  const validSourceId = z.uuid().safeParse(retryInstanceId).success;
  const initializedSource = useRef(false);
  const submittingRef = useRef(false);
  const [submitting, setSubmitting] = useState(false);
  const [verificationIssue, setVerificationIssue] = useState<string>();
  const source = useQuery({
    enabled: retryMode && validSourceId,
    queryKey: ["model-deploy", "retry-source", retryInstanceId],
    queryFn: () => apiClient.getModelInstance(retryInstanceId ?? ""),
  });
  useEffect(() => {
    if (
      !retryMode ||
      initializedSource.current ||
      source.data?.status !== "Failed" ||
      source.isError
    )
      return;
    initializedSource.current = true;
    setArtifactId(source.data.artifact_id);
    setSelectedGpuUuids(new Set(source.data.gpu_uuids));
    setRuntimeName(source.data.runtime_type);
    setContextLength(source.data.context_length);
    setGpuMemoryUtilization(source.data.gpu_memory_utilization);
  }, [retryMode, source.data, source.isError]);
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  // retry mode locks the model, so it starts at the GPU step.
  const [step, setStep] = useState(retryMode ? 2 : 1);
  const [reselectNotice, setReselectNotice] = useState<string>();
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [artifactId, setArtifactId] = useState(
    retryMode ? "" : (searchParams.get("artifact_id") ?? ""),
  );
  const [selectedGpuUuids, setSelectedGpuUuids] = useState<Set<string>>(
    new Set(),
  );
  const [runtimeName, setRuntimeName] = useState<RuntimeType | "">("");
  const [contextLength, setContextLength] = useState<number | null>(null);
  const [gpuMemoryUtilization, setGpuMemoryUtilization] = useState(0.9);
  const [submitError, setSubmitError] = useState<ApiError>();
  const nameForm = useForm<{ name: string }>({ defaultValues: { name: "" } });
  const instanceName = nameForm.watch("name");
  const nameValidation = instanceNameSchema.safeParse(instanceName);
  const nameEdited = useRef(false);

  useEffect(() => heading.current?.focus(), []);

  const artifacts = useQuery({
    enabled: !retryMode || source.data?.status === "Failed",
    queryFn: async () => {
      let page = await apiClient.getModelArtifacts({ status: "Ready" });
      if (!retryMode) return page;
      const cursors = new Set<string>();
      while (
        !page.items.some(({ id }) => id === source.data?.artifact_id) &&
        page.next_cursor !== null
      ) {
        if (cursors.has(page.next_cursor) || cursors.size >= 100)
          throw new Error("artifact pagination did not advance");
        cursors.add(page.next_cursor);
        page = await apiClient.getModelArtifacts({
          status: "Ready",
          cursor: page.next_cursor,
        });
      }
      return page;
    },
    queryKey: [
      "model-deploy",
      "artifacts",
      ...(retryMode ? [source.data?.artifact_id] : []),
    ],
  });
  const catalog = useQuery({
    queryFn: () => apiClient.getModelCatalog(),
    queryKey: ["model-deploy", "catalog"],
  });
  const server = useQuery({
    queryFn: () => apiClient.getSystemServer(),
    queryKey: ["model-deploy", "server"],
  });
  const instances = useQuery({
    queryFn: () => apiClient.getModelInstances(),
    queryKey: ["model-deploy", "instances"],
  });
  const readyArtifacts = (artifacts.data?.items ?? []).filter(
    (item) => item.status === "Ready",
  );
  const selectedArtifact = readyArtifacts.find(({ id }) => id === artifactId);
  const selectedModel = catalog.data?.items.find(
    ({ id }) => id === selectedArtifact?.model_id,
  );
  const selectedModelId = selectedArtifact?.model_id;
  const modelDisplayName =
    selectedModel?.display_name ?? selectedArtifact?.display_name;
  const { setValue: setName } = nameForm;
  useEffect(() => {
    if (modelDisplayName !== undefined && !nameEdited.current) {
      setName(
        "name",
        defaultInstanceName(
          modelDisplayName,
          instances.data?.items.map(({ name }) => name) ?? [],
        ),
      );
    }
  }, [modelDisplayName, instances.data, setName]);
  const deploymentOptions = useQuery({
    enabled: selectedModelId !== undefined,
    queryFn: () => {
      if (selectedModelId === undefined) {
        throw new Error("deployment options query enabled without a model");
      }
      return apiClient.getModelDeploymentOptions(selectedModelId);
    },
    queryKey: ["model-deploy", "deployment-options", selectedModelId],
  });
  const selectedRuntime = deploymentOptions.data?.runtimes.find(
    ({ name }) => name === runtimeName,
  );

  useEffect(() => {
    if (retryMode) return;
    setRuntimeName("");
    setContextLength(null);
  }, [selectedModelId, retryMode]);

  useEffect(() => {
    if (retryMode) return;
    const first = deploymentOptions.data?.runtimes[0];
    if (first === undefined) return;
    setRuntimeName(first.name);
    setContextLength(first.default_context_length);
  }, [deploymentOptions.data, retryMode]);

  const create = useMutation({
    mutationFn: () => {
      if (runtimeName === "" || contextLength === null) {
        throw new Error("deployment policy was not selected");
      }
      return apiClient.createModelInstance({
        name: instanceNameSchema.parse(instanceName),
        artifact_id: artifactId,
        context_length: contextLength,
        gpu_memory_utilization: gpuMemoryUtilization,
        gpu_uuids: [...selectedGpuUuids],
        runtime_type: runtimeName,
        tensor_parallel_size: selectedGpuUuids.size,
      });
    },
  });

  const devices =
    server.data?.agent.status === "available"
      ? server.data.agent.gpus.devices
      : [];
  const occupiedBy = useMemo(
    () => occupiedGpuInstances(instances.data?.items ?? []),
    [instances.data],
  );
  const resourcesRefreshing = server.isFetching || instances.isFetching;
  const resourcesFailed = server.isError || instances.isError;
  const inventoryReady =
    server.data?.agent.status === "available" &&
    server.data.agent.gpus.discovery_state === "ready";
  const inventoryUnavailable =
    server.data?.agent.status !== "available" ||
    server.data.agent.gpus.discovery_state === "unavailable";
  const resourcesReady =
    inventoryReady && !resourcesFailed && !resourcesRefreshing;
  const invalidGpuUuids = inventoryUnavailable
    ? []
    : [...selectedGpuUuids].filter(
        (uuid) =>
          occupiedBy.has(uuid) ||
          !devices.some((gpu) => gpu.uuid === uuid && gpu.health === "healthy"),
      );
  const selectionAvailable = resourcesReady && invalidGpuUuids.length === 0;
  // weight-only estimate; it only blocks selections that obviously cannot fit.
  const selectedDevices = devices.filter(({ uuid }) =>
    selectedGpuUuids.has(uuid),
  );
  const referenceGpuMemory =
    selectedDevices.length > 0
      ? Math.min(...selectedDevices.map(({ memory_bytes }) => memory_bytes))
      : Math.max(
          0,
          ...devices
            .filter(({ health }) => health === "healthy")
            .map(({ memory_bytes }) => memory_bytes),
        );
  const requiredGpuCount =
    selectedArtifact === undefined
      ? undefined
      : minimumGpuCount(selectedArtifact.size_bytes, referenceGpuMemory);
  const weightFitError =
    requiredGpuCount !== undefined &&
    selectedGpuUuids.size > 0 &&
    selectedGpuUuids.size < requiredGpuCount
      ? `已选 ${String(selectedGpuUuids.size)} 张，权重放不下，至少需要 ${String(requiredGpuCount)} 张`
      : undefined;
  const nameDuplicate =
    nameValidation.success &&
    (instances.data?.items ?? []).some(
      ({ name }) => name === nameValidation.data,
    );

  function refreshResources() {
    void server.refetch();
    void instances.refetch();
  }
  const sourceIssue = retryMode
    ? !validSourceId
      ? "来源实例标识无效，请返回模型列表重新进入。"
      : source.isError
        ? "来源配置读取失败，无法继续部署。请确认访问权限和平台连接后重新加载。"
        : source.data !== undefined && source.data.status !== "Failed"
          ? "来源实例已不是运行失败状态，无法继续此操作。请返回实例详情。"
          : artifacts.isError
            ? "本地模型信息未能完整读取，无法继续部署。请恢复平台连接后重新加载来源。"
            : source.data !== undefined &&
                initializedSource.current &&
                !artifacts.isPending &&
                (selectedArtifact === undefined ||
                  source.data.artifact_id !== artifactId)
              ? "上次使用的本地模型尚未就绪或已不可用，无法继续部署。请恢复本地模型后重新加载。"
              : undefined
    : undefined;
  const retryFactsReady =
    !retryMode ||
    (sourceIssue === undefined &&
      source.data !== undefined &&
      !source.isFetching &&
      !artifacts.isError &&
      !artifacts.isFetching &&
      !deploymentOptions.isError &&
      !deploymentOptions.isFetching);
  const loading =
    (retryMode &&
      validSourceId &&
      (source.isPending || !initializedSource.current)) ||
    [artifacts, catalog, server, instances].some((query) => query.isPending) ||
    (selectedModelId !== undefined && deploymentOptions.isPending);
  const failed =
    [artifacts, catalog, server, instances].some(
      (query) => query.isError && query.data === undefined,
    ) ||
    (selectedModelId !== undefined &&
      deploymentOptions.isError &&
      deploymentOptions.data === undefined);
  const gpuPolicyError = gpuSelectionError(
    selectedGpuUuids.size,
    selectedRuntime,
  );
  const contextPolicyError = contextSelectionError(
    contextLength,
    selectedRuntime,
  );
  const configurationValid =
    retryFactsReady &&
    selectedArtifact !== undefined &&
    memoryUtilizationValid(gpuMemoryUtilization) &&
    nameValidation.success &&
    !nameDuplicate &&
    selectionAvailable &&
    weightFitError === undefined &&
    gpuPolicyError === undefined &&
    contextPolicyError === undefined &&
    runtimeName !== "";

  function toggleGpu(gpu: GpuDevice) {
    if (!resourcesReady || occupiedBy.has(gpu.uuid) || gpu.health !== "healthy")
      return;
    setReselectNotice(undefined);
    setSelectedGpuUuids((current) => {
      const next = new Set(current);
      if (next.has(gpu.uuid)) next.delete(gpu.uuid);
      else next.add(gpu.uuid);
      return next;
    });
  }

  async function submit() {
    if (!configurationValid || submittingRef.current || create.isPending)
      return;
    submittingRef.current = true;
    setSubmitting(true);
    setSubmitError(undefined);
    setVerificationIssue(undefined);
    setReselectNotice(undefined);
    try {
      // Recheck right before creating; the backend would otherwise accept an
      // occupied GPU and park the instance in WaitingForResource.
      const retryChecks = retryMode
        ? ([
            source.refetch(),
            artifacts.refetch(),
            deploymentOptions.refetch(),
          ] as const)
        : undefined;
      const [latestServer, latestInstances] = await Promise.all([
        server.refetch(),
        instances.refetch(),
      ]);
      const agent = latestServer.data?.agent;
      const occupied = occupiedGpuInstances(latestInstances.data?.items ?? []);
      if (retryChecks !== undefined) {
        const [latestSource, latestArtifacts, latestOptions] =
          await Promise.all(retryChecks);
        const runtime = latestOptions.data?.runtimes.find(
          ({ name }) => name === runtimeName,
        );
        if (
          [
            latestServer,
            latestInstances,
            latestSource,
            latestArtifacts,
            latestOptions,
          ].some((query) => query.isError) ||
          agent?.status !== "available" ||
          agent.gpus.discovery_state !== "ready" ||
          latestInstances.data === undefined ||
          latestSource.data?.status !== "Failed" ||
          latestSource.data.artifact_id !== artifactId ||
          !latestArtifacts.data?.items.some(
            (item) => item.id === artifactId && item.status === "Ready",
          ) ||
          [...selectedGpuUuids].some(
            (uuid) =>
              !agent.gpus.devices.some(
                (gpu) => gpu.uuid === uuid && gpu.health === "healthy",
              ),
          ) ||
          gpuSelectionError(selectedGpuUuids.size, runtime) !== undefined ||
          contextSelectionError(contextLength, runtime) !== undefined
        ) {
          setVerificationIssue(
            "最新配置或资源无法确认可用，模型实例未创建。请检查来源状态、本地模型和所选配置，刷新后重试；已编辑的配置仍会保留。",
          );
          return;
        }
      }
      if (
        latestInstances.data !== undefined &&
        [...selectedGpuUuids].some((uuid) => occupied.has(uuid))
      ) {
        // back to the GPU step, keeping every other input.
        setSelectedGpuUuids(
          new Set([...selectedGpuUuids].filter((uuid) => !occupied.has(uuid))),
        );
        setReselectNotice("所选 GPU 已被其他模型实例占用，请重新选择");
        setStep(2);
        return;
      }
      const created = await create.mutateAsync();
      void navigate(`/models/instances/${created.id}`, { replace: true });
    } catch (error) {
      if (error instanceof ApiError) setSubmitError(error);
      else throw error;
    } finally {
      submittingRef.current = false;
      setSubmitting(false);
    }
  }

  return (
    <div className="pw-deploy-page">
      <header className="pw-deploy-page__head">
        <span>
          <Button
            disabled={submitting}
            onClick={() => void navigate("/models")}
            size="s"
            variant="ghost"
          >
            返回模型
          </Button>
          <h1 ref={heading} tabIndex={-1}>
            {retryMode ? "修改配置并重试" : "部署模型"}
          </h1>
          <p>选择本地模型和 GPU，确认运行配置后创建模型实例。</p>
          <HelpButton topic={HELP_TOPICS.deployment.id}>
            查看首次部署帮助
          </HelpButton>
        </span>
      </header>
      {retryMode ? (
        <InlineAlert title="重新部署为新模型实例" tone="info">
          以原实例的模型和配置为初始值，确认后新建一个模型实例；原实例保留，确认新实例正常后可自行删除。
          <Button
            disabled={submitting}
            onClick={() =>
              void navigate(
                validSourceId
                  ? `/models/instances/${retryInstanceId}`
                  : "/models",
              )
            }
          >
            返回来源实例
          </Button>
        </InlineAlert>
      ) : null}
      <ol aria-label="部署步骤" className="pw-deploy-steps">
        {["选择模型", "选择 GPU", "运行配置", "确认并部署"].map(
          (label, index) => (
            <li
              aria-current={step === index + 1 ? "step" : undefined}
              data-complete={step > index + 1 || undefined}
              key={label}
            >
              <strong>{index + 1}</strong>
              <span>{label}</span>
            </li>
          ),
        )}
      </ol>
      {sourceIssue !== undefined ? (
        <InlineAlert title="无法修改配置并重试" tone="negative">
          {sourceIssue}
          <Button
            disabled={
              !validSourceId || source.isFetching || artifacts.isFetching
            }
            onClick={() => {
              void source.refetch();
              void artifacts.refetch();
            }}
          >
            重新加载来源
          </Button>
        </InlineAlert>
      ) : loading ? (
        <section
          aria-busy="true"
          aria-label="部署信息正在加载"
          className="pw-deploy-panel"
        >
          <span className="sk pw-deploy-skeleton" />
          <span className="sk pw-deploy-skeleton" />
          <span className="sk pw-deploy-skeleton" />
        </section>
      ) : failed ? (
        <InlineAlert title="无法准备部署信息" tone="negative">
          本地模型、GPU 或模型实例信息未能加载。请检查平台连接后重试。
          <Button
            onClick={() => {
              if (artifacts.isError) void artifacts.refetch();
              if (catalog.isError) void catalog.refetch();
              if (selectedModelId !== undefined && deploymentOptions.isError)
                void deploymentOptions.refetch();
              refreshResources();
            }}
          >
            重新加载
          </Button>
        </InlineAlert>
      ) : (
        <section className="pw-deploy-panel">
          <fieldset className="pw-deploy-fieldset" disabled={submitting}>
            {step >= 2 ? (
              <div className="pw-deploy-stack">
                <Button
                  disabled={create.isPending}
                  loading={resourcesRefreshing}
                  loadingText="正在刷新资源"
                  onClick={refreshResources}
                >
                  刷新资源
                </Button>
                {resourcesFailed ? (
                  <InlineAlert title="资源信息已过期" tone="negative">
                    资源刷新失败，当前不能确认可用设备，暂时无法继续部署。请刷新资源后重试。
                    <span>
                      最近成功读取：
                      {new Date(
                        Math.min(server.dataUpdatedAt, instances.dataUpdatedAt),
                      ).toLocaleString("zh-CN")}
                    </span>
                  </InlineAlert>
                ) : inventoryUnavailable ? (
                  <InlineAlert title="资源信息暂不可用" tone="negative">
                    无法确认设备状态，暂时无法继续部署。请检查系统状态后刷新资源。
                  </InlineAlert>
                ) : null}
                {invalidGpuUuids.length > 0 ? (
                  <InlineAlert title="所选设备已不可用" tone="negative">
                    所选设备已被占用、健康异常或不在当前清单中，暂时无法继续部署。请清除失效选择并重新选择，或等待资源恢复后刷新。
                    <Button
                      onClick={() => {
                        setSelectedGpuUuids(
                          new Set(
                            [...selectedGpuUuids].filter(
                              (uuid) => !invalidGpuUuids.includes(uuid),
                            ),
                          ),
                        );
                        setStep(2);
                      }}
                    >
                      清除失效选择
                    </Button>
                  </InlineAlert>
                ) : null}
              </div>
            ) : null}
            {retryMode && (artifacts.isError || deploymentOptions.isError) ? (
              <InlineAlert title="部署配置已过期" tone="negative">
                本地模型或运行选项刷新失败，暂时无法继续部署。请重新加载配置。
                <Button
                  onClick={() => {
                    void artifacts.refetch();
                    void deploymentOptions.refetch();
                  }}
                >
                  重新加载配置
                </Button>
              </InlineAlert>
            ) : null}
            {verificationIssue === undefined ? null : (
              <InlineAlert title="部署前检查未通过" tone="negative">
                {verificationIssue}
              </InlineAlert>
            )}
            {step === 1 && !retryMode ? (
              <ModelStep
                apiClient={apiClient}
                artifacts={readyArtifacts}
                catalog={catalog.data?.items ?? []}
                onDownload={() => void navigate(MODEL_DOWNLOAD_PATH)}
                onSelect={setArtifactId}
                selectedId={artifactId}
              />
            ) : null}
            {step === 2 &&
            server.data?.agent.status === "available" &&
            server.data.agent.gpus.discovery_state !== "unavailable" ? (
              <GpuStep
                devices={devices}
                resourcesReady={resourcesReady}
                occupiedBy={occupiedBy}
                instanceNames={
                  new Map(
                    (instances.data?.items ?? []).map((item) => [
                      item.id,
                      item.name,
                    ]),
                  )
                }
                onToggle={toggleGpu}
                reselectNotice={reselectNotice}
                requiredGpuCount={requiredGpuCount}
                selected={selectedGpuUuids}
                selectedArtifact={selectedArtifact}
                validationError={gpuPolicyError}
                weightFitError={weightFitError}
              />
            ) : null}
            {step === 3 ? (
              <>
                <Controller
                  control={nameForm.control}
                  name="name"
                  render={({ field }) => (
                    <TextField
                      label="实例名称"
                      required
                      name={field.name}
                      value={field.value}
                      onBlur={field.onBlur}
                      onChange={(event) => {
                        nameEdited.current = true;
                        field.onChange(event);
                      }}
                      helpText="2～64 个字符，系统内唯一。"
                      errorText={
                        nameValidation.success
                          ? nameDuplicate
                            ? "已有同名实例，换一个名称"
                            : undefined
                          : nameValidation.error.issues[0]?.message
                      }
                    />
                  )}
                />
                {retryMode && selectedRuntime === undefined ? (
                  <InlineAlert title="上次运行环境已不可用" tone="negative">
                    当前模型不再支持上次选择的运行环境，无法继续部署。请选择可用运行环境。
                  </InlineAlert>
                ) : null}
                {gpuPolicyError !== undefined ? (
                  <InlineAlert title="当前设备数量不可用" tone="negative">
                    {gpuPolicyError}{" "}
                    请返回上一步调整设备，或选择支持当前数量的运行环境。
                  </InlineAlert>
                ) : null}
                <ConfigurationStep
                  advancedOpen={advancedOpen}
                  apiClient={apiClient}
                  artifactId={artifactId}
                  contextLength={contextLength}
                  contextLimit={deploymentOptions.data?.model_context_limit}
                  gpuMemoryUtilization={gpuMemoryUtilization}
                  onAdvancedOpen={setAdvancedOpen}
                  onContextLength={setContextLength}
                  onGpuMemoryUtilization={setGpuMemoryUtilization}
                  onRuntime={(value) => {
                    setRuntimeName(value);
                    const option = deploymentOptions.data?.runtimes.find(
                      ({ name }) => name === value,
                    );
                    setContextLength(option?.default_context_length ?? null);
                  }}
                  runtimeName={runtimeName}
                  runtimes={deploymentOptions.data?.runtimes ?? []}
                  tensorParallelSize={selectedGpuUuids.size}
                  validationError={contextPolicyError}
                />
              </>
            ) : null}
            {step === 4 && selectedArtifact !== undefined ? (
              <ConfirmationStep
                instanceName={instanceName.trim()}
                apiClient={apiClient}
                artifact={selectedArtifact}
                contextLength={contextLength}
                devices={devices.filter(({ uuid }) =>
                  selectedGpuUuids.has(uuid),
                )}
                gpuMemoryUtilization={gpuMemoryUtilization}
                model={selectedModel}
                runtime={selectedRuntime}
              />
            ) : null}
            {submitError === undefined ? null : (
              <InlineAlert title="部署未开始" tone="negative">
                {submitError.responseMessage ??
                  "模型实例没有创建。请检查所选 GPU 与运行配置后重试。"}
              </InlineAlert>
            )}
            <footer className="pw-deploy-actions">
              <Button
                disabled={
                  step === 1 || (retryMode && step === 2) || create.isPending
                }
                onClick={() => {
                  setReselectNotice(undefined);
                  setStep((current) => current - 1);
                }}
              >
                上一步
              </Button>
              {step < 4 ? (
                <Button
                  disabled={
                    !retryFactsReady ||
                    (step === 3 &&
                      (!nameValidation.success ||
                        nameDuplicate ||
                        gpuPolicyError !== undefined)) ||
                    (step >= 2 && !selectionAvailable) ||
                    (step >= 2 && weightFitError !== undefined) ||
                    !(retryMode && step === 2
                      ? selectedGpuUuids.size > 0
                      : canContinue(
                          step,
                          artifactId,
                          selectedGpuUuids,
                          contextLength,
                          gpuMemoryUtilization,
                          selectedRuntime,
                        ))
                  }
                  onClick={() => {
                    setReselectNotice(undefined);
                    setStep((current) => current + 1);
                  }}
                  variant="primary"
                >
                  下一步
                </Button>
              ) : (
                <Button
                  disabled={!configurationValid}
                  loading={submitting || create.isPending}
                  loadingText="正在部署"
                  onClick={() => void submit()}
                  variant="primary"
                >
                  开始部署
                </Button>
              )}
            </footer>
          </fieldset>
        </section>
      )}
    </div>
  );
}

function ModelStep({
  apiClient,
  artifacts,
  catalog,
  onDownload,
  onSelect,
  selectedId,
}: {
  apiClient: ApiClient;
  artifacts: readonly ModelArtifactItem[];
  catalog: readonly ModelCatalogItem[];
  onDownload: () => void;
  onSelect: (id: string) => void;
  selectedId: string;
}) {
  if (artifacts.length === 0) {
    return (
      <InlineAlert title="没有可用的本地模型" tone="notice">
        Hugging Face／ModelScope
        的模型先在本地模型页下载，下载完成后从本地模型行「部署」
        <Button onClick={onDownload}>下载模型</Button>
      </InlineAlert>
    );
  }
  return (
    <fieldset className="pw-deploy-fieldset">
      <legend>选择一个可用的本地模型</legend>
      <div className="pw-deploy-models">
        {artifacts.map((artifact) => (
          <LocalModelChoice
            apiClient={apiClient}
            artifact={artifact}
            catalog={catalog}
            key={artifact.id}
            onSelect={onSelect}
            selected={artifact.id === selectedId}
          />
        ))}
      </div>
      <div className="pw-deploy-model-entry">
        <Button onClick={onDownload} size="s" variant="ghost">
          下载模型
        </Button>
      </div>
    </fieldset>
  );
}

function LocalModelChoice({
  apiClient,
  artifact,
  catalog,
  onSelect,
  selected,
}: {
  apiClient: ApiClient;
  artifact: ModelArtifactItem;
  catalog: readonly ModelCatalogItem[];
  onSelect: (id: string) => void;
  selected: boolean;
}) {
  const model = catalog.find(({ id }) => id === artifact.model_id);
  const info = useArtifactModelInfo(apiClient, artifact.id);
  const architecture =
    model?.architecture.state === "identified"
      ? model.architecture.value
      : "未识别";
  return (
    <label className="pw-deploy-choice">
      <input
        checked={selected}
        name="artifact"
        onChange={() => {
          onSelect(artifact.id);
        }}
        type="radio"
      />
      <span>
        <strong>{artifact.display_name}</strong>
        <span>
          {originTypeLabel(artifact.source)} ·{" "}
          <span title={artifact.resolved_revision ?? undefined}>
            {artifact.resolved_revision?.slice(0, 7) ?? "—"}
          </span>
        </span>
        <span>
          {formatBytes(artifact.size_bytes)} · {architecture} · 最长上下文{" "}
          {modelInfoText(info.contextLength, info.pending)}
        </span>
      </span>
    </label>
  );
}

function GpuStep({
  instanceNames,
  devices,
  occupiedBy,
  resourcesReady,
  onToggle,
  reselectNotice,
  requiredGpuCount,
  selected,
  selectedArtifact,
  validationError,
  weightFitError,
}: {
  devices: readonly GpuDevice[];
  occupiedBy: ReadonlyMap<string, string>;
  instanceNames: ReadonlyMap<string, string>;
  resourcesReady: boolean;
  onToggle: (gpu: GpuDevice) => void;
  reselectNotice?: string;
  requiredGpuCount?: number;
  selected: ReadonlySet<string>;
  selectedArtifact?: ModelArtifactItem;
  validationError?: string;
  weightFitError?: string;
}) {
  if (devices.length === 0) {
    return (
      <InlineAlert title="没有可选设备" tone="notice">
        当前设备清单为空。请检查系统状态后刷新资源。
      </InlineAlert>
    );
  }
  return (
    <div className="pw-deploy-stack">
      {reselectNotice === undefined ? null : (
        <InlineAlert title="需要重新选择 GPU" tone="negative">
          {reselectNotice}
        </InlineAlert>
      )}
      <div className="pw-deploy-summary-line">
        <span>模型：{selectedArtifact?.display_name ?? "未选择"}</span>
        <strong>
          已选总显存：
          {formatBytes(
            devices
              .filter(({ uuid }) => selected.has(uuid))
              .reduce((sum, { memory_bytes }) => sum + memory_bytes, 0),
          )}
        </strong>
      </div>
      {!devices.some(
        (gpu) => gpu.health === "healthy" && !occupiedBy.has(gpu.uuid),
      ) ? (
        <InlineAlert title="当前没有可用设备" tone="notice">
          设备已被占用或健康状态不可用。请等待资源释放或恢复后刷新资源。
        </InlineAlert>
      ) : null}
      <fieldset className="pw-deploy-fieldset">
        <legend>选择 GPU（可多选）</legend>
        <div className="pw-gpu-grid">
          {devices.map((gpu) => {
            const occupied = occupiedBy.get(gpu.uuid);
            return (
              <label className="pw-gpu-choice" key={gpu.uuid}>
                <span className="pw-gpu-choice__head">
                  <input
                    checked={selected.has(gpu.uuid)}
                    disabled={
                      !resourcesReady ||
                      gpu.health !== "healthy" ||
                      occupied !== undefined
                    }
                    onChange={() => {
                      onToggle(gpu);
                    }}
                    type="checkbox"
                  />
                  <strong>GPU {gpu.display_index ?? gpu.uuid.slice(-6)}</strong>
                  <StatusBadge
                    icon={
                      gpu.health !== "healthy"
                        ? "error"
                        : occupied === undefined
                          ? "success"
                          : "notice"
                    }
                    text={
                      gpu.health !== "healthy"
                        ? gpu.health === "unknown"
                          ? "健康状态未知"
                          : "健康异常"
                        : occupied === undefined
                          ? "可用"
                          : "已占用"
                    }
                    tone={
                      gpu.health !== "healthy"
                        ? "negative"
                        : occupied === undefined
                          ? "positive"
                          : "notice"
                    }
                  />
                </span>
                <span>{describeGpu(gpu)}</span>
                {gpu.health !== "healthy" ? (
                  <span>
                    {gpu.health_reasons.length === 0
                      ? "健康检查结果不可用，请检查系统状态。"
                      : gpu.health_reasons
                          .map((reason) => GPU_HEALTH_REASONS[reason])
                          .join("；")}
                  </span>
                ) : null}
                {occupied === undefined ? null : (
                  <span>
                    占用实例：{instanceNames.get(occupied)} ·{" "}
                    {occupied.slice(0, 8)}
                  </span>
                )}
              </label>
            );
          })}
        </div>
      </fieldset>
      {validationError === undefined ? null : (
        <InlineAlert title="当前设备数量不可用" tone="negative">
          {validationError}
        </InlineAlert>
      )}
      {weightFitError === undefined ? null : (
        <InlineAlert title="所选 GPU 数量不足" tone="negative">
          {weightFitError}
        </InlineAlert>
      )}
      {requiredGpuCount === undefined ? null : (
        <p className="pw-deploy-note">
          按权重大小估算，至少需要 {requiredGpuCount.toLocaleString("zh-CN")} 张
        </p>
      )}
      <p className="pw-deploy-note">
        资源信息仅供参考，不代表部署一定成功；提交时会重新校验并原子预留。
      </p>
    </div>
  );
}

function ConfigurationStep({
  advancedOpen,
  apiClient,
  artifactId,
  contextLength,
  contextLimit,
  gpuMemoryUtilization,
  onAdvancedOpen,
  onContextLength,
  onGpuMemoryUtilization,
  onRuntime,
  runtimeName,
  runtimes,
  tensorParallelSize,
  validationError,
}: {
  advancedOpen: boolean;
  apiClient: ApiClient;
  artifactId: string;
  contextLength: number | null;
  contextLimit?:
    { state: "identified"; value: number } | { state: "unrecognized" };
  gpuMemoryUtilization: number;
  onAdvancedOpen: (open: boolean) => void;
  onContextLength: (value: number | null) => void;
  onGpuMemoryUtilization: (value: number) => void;
  onRuntime: (value: RuntimeType) => void;
  runtimeName: RuntimeType | "";
  runtimes: readonly DeploymentRuntimeOption[];
  tensorParallelSize: number;
  validationError?: string;
}) {
  const info = useArtifactModelInfo(apiClient, artifactId);
  const memoryInvalid = !memoryUtilizationValid(gpuMemoryUtilization);
  return (
    <div className="pw-deploy-form">
      <label className="pw-field">
        <span className="pw-field__label">运行环境</span>
        <select
          className="pw-input"
          onChange={(event) => {
            onRuntime(event.currentTarget.value as RuntimeType);
          }}
          value={runtimeName}
        >
          {runtimeName !== "" &&
          !runtimes.some(({ name }) => name === runtimeName) ? (
            <option value={runtimeName}>
              {runtimeName}（上次配置，当前不可用）
            </option>
          ) : null}
          {runtimes.length === 0 ? (
            <option value="">暂无可用环境</option>
          ) : null}
          {runtimes.map((runtime) => (
            <option key={runtime.name} value={runtime.name}>
              {runtime.name} {runtime.version}
            </option>
          ))}
        </select>
      </label>
      <label className="pw-field">
        <span className="pw-field__label">并行数量（只读）</span>
        <input
          aria-label="并行数量"
          className="pw-input"
          disabled
          value={tensorParallelSize}
        />
        <span className="pw-field__help">自动等于所选 GPU 数量。</span>
      </label>
      <label className="pw-field">
        <span className="pw-field__label">上下文长度</span>
        <select
          aria-describedby="context-length-help"
          className="pw-input"
          onChange={(event) => {
            const value = Number(event.currentTarget.value);
            onContextLength(
              Number.isInteger(value) && value > 0 ? value : null,
            );
          }}
          required
          value={contextLength ?? ""}
        >
          {contextLength !== null &&
          !runtimes
            .find(({ name }) => name === runtimeName)
            ?.context_lengths.includes(contextLength) ? (
            <option value={contextLength}>
              {contextLength.toLocaleString("zh-CN")}（上次配置，当前不可用）
            </option>
          ) : null}
          {contextLength === null ? (
            <option value="">请选择可用档位</option>
          ) : null}
          {runtimes
            .find(({ name }) => name === runtimeName)
            ?.context_lengths.map((value) => (
              <option key={value} value={value}>
                {value.toLocaleString("zh-CN")}
              </option>
            ))}
        </select>
        <span className="pw-field__help" id="context-length-help">
          {contextLimit?.state === "identified"
            ? `可用档位已按模型上限 ${contextLimit.value.toLocaleString("zh-CN")} 筛选。`
            : "模型上限未识别，当前显示兼容清单中的全部可用档位。"}
        </span>
      </label>
      {validationError === undefined ? null : (
        <InlineAlert title="上下文长度不可用" tone="negative">
          {validationError}
        </InlineAlert>
      )}
      {memoryInvalid ? (
        <InlineAlert title="显存比例不可用" tone="negative">
          {MEMORY_UTILIZATION_ERROR}
          {advancedOpen ? null : "；请展开高级设置调整"}
        </InlineAlert>
      ) : null}
      <div className="pw-deploy-advanced">
        <Button
          aria-controls={
            advancedOpen ? "pw-deploy-advanced-settings" : undefined
          }
          aria-expanded={advancedOpen}
          onClick={() => {
            onAdvancedOpen(!advancedOpen);
          }}
          size="s"
          variant="ghost"
        >
          {advancedOpen ? "收起高级设置" : "高级设置"}
        </Button>
        {advancedOpen ? (
          <div
            id="pw-deploy-advanced-settings"
            className="pw-deploy-form pw-deploy-advanced__body"
          >
            <label className="pw-field">
              <span className="pw-field__label">显存比例</span>
              <input
                aria-invalid={memoryInvalid || undefined}
                className="pw-input"
                max={MEMORY_UTILIZATION_MAX}
                min={MEMORY_UTILIZATION_MIN}
                onChange={(event) => {
                  onGpuMemoryUtilization(event.currentTarget.valueAsNumber);
                }}
                required
                step={0.01}
                type="number"
                value={gpuMemoryUtilization}
              />
              <span className="pw-field__help">
                推荐值 0.90，可填写 0.80 到 0.95。
              </span>
            </label>
            <label className="pw-field">
              <span className="pw-field__label">精度 / 量化（只读）</span>
              <input
                aria-label="精度 / 量化"
                className="pw-input"
                readOnly
                value={`${modelInfoText(info.precision, info.pending)} / ${modelInfoText(info.quantization, info.pending)}`}
              />
              <span className="pw-field__help">按模型自动选择。</span>
            </label>
          </div>
        ) : null}
      </div>
    </div>
  );
}

function ConfirmationStep({
  apiClient,
  instanceName,
  artifact,
  contextLength,
  devices,
  gpuMemoryUtilization,
  model,
  runtime,
}: {
  apiClient: ApiClient;
  instanceName: string;
  artifact: ModelArtifactItem;
  contextLength: number | null;
  devices: readonly GpuDevice[];
  gpuMemoryUtilization: number;
  model?: ModelCatalogItem;
  runtime?: DeploymentRuntimeOption;
}) {
  const info = useArtifactModelInfo(apiClient, artifact.id);
  const architecture =
    model?.architecture.state === "identified"
      ? model.architecture.value
      : "未识别";
  const groups: Array<{ title: string; rows: Array<[string, string]> }> = [
    {
      title: "模型",
      rows: [
        ["模型", artifact.display_name],
        [
          "来源",
          `${originTypeLabel(artifact.source)} · ${artifact.resolved_revision?.slice(0, 7) ?? "—"}`,
        ],
        ["架构", architecture],
        [
          "精度 / 量化",
          `${modelInfoText(info.precision, info.pending)} / ${modelInfoText(info.quantization, info.pending)}`,
        ],
      ],
    },
    {
      title: "模型文件",
      rows: [
        ["本地模型大小", formatBytes(artifact.size_bytes)],
        ["文件状态", "可用，启动前重新校验"],
      ],
    },
    {
      title: "资源",
      rows: [
        ["GPU", devices.map(describeGpu).join("；")],
        ["并行数量", String(devices.length)],
        [
          "总显存",
          formatBytes(
            devices.reduce((sum, { memory_bytes }) => sum + memory_bytes, 0),
          ),
        ],
      ],
    },
    {
      title: "运行配置",
      rows: [
        ["实例名称", instanceName],
        [
          "运行环境",
          runtime === undefined
            ? "未选择"
            : `${runtime.name} ${runtime.version}`,
        ],
        ["上下文长度", contextLength?.toLocaleString("zh-CN") ?? "未选择"],
        ["显存比例", gpuMemoryUtilization.toFixed(2)],
      ],
    },
  ];
  return (
    <div className="pw-deploy-confirmation">
      <h2>确认部署配置</h2>
      {groups.map((group) => (
        <section className="pw-deploy-group" key={group.title}>
          <h3>{group.title}</h3>
          <dl>
            {group.rows.map(([label, value]) => (
              <div key={label}>
                <dt>{label}</dt>
                <dd>{value}</dd>
              </div>
            ))}
          </dl>
        </section>
      ))}
      <InlineAlert title="部署前请确认" tone="info">
        所选 GPU 在启动前重新校验本地模型并原子预留，期间被占用时需要重新选择
        GPU。
      </InlineAlert>
    </div>
  );
}

function canContinue(
  step: number,
  artifactId: string,
  selectedGpuUuids: ReadonlySet<string>,
  contextLength: number | null,
  gpuMemoryUtilization: number,
  runtime: DeploymentRuntimeOption | undefined,
) {
  if (step === 1) return artifactId.length > 0;
  if (step === 2)
    return gpuSelectionError(selectedGpuUuids.size, runtime) === undefined;
  if (step === 3)
    return (
      contextSelectionError(contextLength, runtime) === undefined &&
      memoryUtilizationValid(gpuMemoryUtilization)
    );
  return true;
}

function gpuSelectionError(
  selectedGpuCount: number,
  runtime: DeploymentRuntimeOption | undefined,
) {
  if (selectedGpuCount === 0) return "请至少选择一台可用设备。";
  if (runtime === undefined) return "部署选项尚未就绪，请稍后重试。";
  if (selectedGpuCount > runtime.max_gpus_per_instance) {
    return `单个模型实例最多可选择 ${runtime.max_gpus_per_instance.toLocaleString("zh-CN")} 台设备，请减少所选设备。`;
  }
  return undefined;
}

function contextSelectionError(
  contextLength: number | null,
  runtime: DeploymentRuntimeOption | undefined,
) {
  if (runtime === undefined) return "部署选项尚未就绪，请稍后重试。";
  if (
    contextLength === null ||
    !runtime.context_lengths.includes(contextLength)
  ) {
    return "请选择当前模型可用的上下文长度档位。";
  }
  return undefined;
}
