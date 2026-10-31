import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { Link, useNavigate, useParams } from "react-router";

import { ApiError, type ApiClient } from "../api/client";
import type { ModelArtifactDetail } from "../api/model-acquisition-contracts";
import {
  Button,
  CopyControl,
  DataTable,
  EmptyState,
  InlineAlert,
  MissingValue,
  StatusBadge,
  TruncatedValue,
  type TableColumn,
} from "../components";
import {
  instanceStatusPresentation,
  runtimeLabel,
} from "../model-instances/presentation";
import { MODEL_INSTANCES_QUERY_KEY } from "../model-instances/queries";
import { formatBytes, formatObservedAt } from "../system/system-page";
import { ArtifactDeleteAction, ArtifactVerifyAction } from "./artifact-actions";
import {
  artifactStatusPresentation,
  downloadStatusDisplay,
  originTypeLabel,
  storageTypeLabel,
} from "./presentation";
import {
  ARTIFACT_POLL_INTERVAL_MS,
  DOWNLOAD_COUNTS_QUERY_KEY,
  DOWNLOAD_TASKS_QUERY_KEY,
  FINISHED_DOWNLOADS_QUERY_KEY,
  MODEL_ARTIFACTS_QUERY_KEY,
  useArtifactVerification,
} from "./queries";

const DETAIL_QUERY_KEY = "model-artifact-detail";
const LOCAL_MODELS_PATH = "/models?tab=local-models";
const UNRECOGNIZED = "未识别";

type ReferencingInstance = ModelArtifactDetail["instances"][number];
type ArtifactFile = NonNullable<ModelArtifactDetail["files"]>[number];

/** 「本地模型详情」. */
export function LocalModelDetailPage({ apiClient }: { apiClient: ApiClient }) {
  const heading = useRef<HTMLHeadingElement>(null);
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { artifactId = "" } = useParams();
  const [deleted, setDeleted] = useState(false);
  const [removedExternalName, setRemovedExternalName] = useState<string>();
  const detail = useQuery({
    queryFn: () => apiClient.artifactDetail(artifactId),
    queryKey: [DETAIL_QUERY_KEY, artifactId],
    refetchInterval: ARTIFACT_POLL_INTERVAL_MS,
    // A 404 means "already deleted"; any other failure shows the retry state.
    retry: false,
  });
  const verification = useArtifactVerification(apiClient, artifactId);
  const modelId = detail.data?.model_id;
  const runtimes = useQuery({
    enabled: modelId !== undefined,
    queryFn: () => apiClient.getModelDeploymentOptions(modelId ?? ""),
    queryKey: [DETAIL_QUERY_KEY, "runtimes", modelId],
  });
  const instances = useQuery({
    enabled: detail.data !== undefined,
    queryFn: () => apiClient.getModelInstances(),
    queryKey: MODEL_INSTANCES_QUERY_KEY,
  });
  const services = useQuery({
    enabled: detail.data !== undefined,
    queryFn: () => apiClient.getAiServices(),
    queryKey: ["ai-services"],
  });

  const refreshLists = useCallback(
    () =>
      Promise.all([
        queryClient.invalidateQueries({ queryKey: DOWNLOAD_TASKS_QUERY_KEY }),
        queryClient.invalidateQueries({
          queryKey: FINISHED_DOWNLOADS_QUERY_KEY,
        }),
        queryClient.invalidateQueries({ queryKey: DOWNLOAD_COUNTS_QUERY_KEY }),
        queryClient.invalidateQueries({ queryKey: MODEL_ARTIFACTS_QUERY_KEY }),
      ]).then(() => undefined),
    [queryClient],
  );
  const refreshAfterVerify = useCallback(async () => {
    await Promise.all([
      refreshLists(),
      queryClient.invalidateQueries({
        queryKey: [DETAIL_QUERY_KEY, artifactId],
      }),
    ]);
  }, [artifactId, queryClient, refreshLists]);
  const afterDelete = useCallback(async () => {
    if (detail.data?.storage_type === "External")
      setRemovedExternalName(detail.data.display_name);
    setDeleted(true);
    queryClient.removeQueries({ queryKey: [DETAIL_QUERY_KEY, artifactId] });
    await refreshLists();
  }, [artifactId, queryClient, refreshLists, detail.data]);

  const notFound =
    detail.error instanceof ApiError && detail.error.status === 404;
  const item = detail.data;
  useEffect(() => {
    heading.current?.focus();
  }, [item?.id, deleted, notFound]);

  if (deleted || notFound) {
    return (
      <section className="pw-models-detail">
        <EmptyState
          action={
            <>
              <Button
                onClick={() => {
                  void navigate(LOCAL_MODELS_PATH);
                }}
              >
                返回本地模型
              </Button>
              <Button
                onClick={() => {
                  void navigate("/models/download");
                }}
                variant="primary"
              >
                下载模型
              </Button>
            </>
          }
          description={
            removedExternalName !== undefined
              ? `「${removedExternalName}」已从平台移除登记，服务器上的文件没有改动。要再用，可在「下载模型」重新注册这个目录。`
              : "这份本地模型的文件已从服务器移除。下载任务记录与历史用量仍然保留；再次部署需要重新下载。"
          }
          icon="layers"
          title={
            removedExternalName !== undefined
              ? "本地模型已移除登记"
              : "本地模型已删除"
          }
        />
      </section>
    );
  }
  if (item === undefined && detail.isPending) {
    return (
      <section
        aria-busy="true"
        aria-label="本地模型详情正在加载"
        className="pw-models-detail"
      >
        <span aria-hidden="true" className="sk pw-models-detail__skeleton" />
        <span aria-hidden="true" className="sk pw-models-detail__skeleton" />
      </section>
    );
  }
  if (item === undefined) {
    return (
      <section className="pw-models-detail">
        <InlineAlert title="无法获取本地模型详情" tone="negative">
          详情暂时无法显示，请检查平台连接后重试，或返回本地模型列表。
          <span className="pw-models-detail__actions">
            <Button onClick={() => void detail.refetch()} size="s">
              重试
            </Button>
            <Button
              onClick={() => {
                void navigate(LOCAL_MODELS_PATH);
              }}
              size="s"
              variant="ghost"
            >
              返回本地模型
            </Button>
          </span>
        </InlineAlert>
      </section>
    );
  }

  const status = artifactStatusPresentation(item.status);
  const managed = item.storage_type === "Managed";
  const verifying =
    verification.data?.status === "Queued" ||
    verification.data?.status === "Running";
  const gpuCounts = new Map(
    (instances.data?.items ?? []).map((instance) => [
      instance.id,
      instance.tensor_parallel_size,
    ]),
  );
  const servicesByInstance = new Map(
    (services.data?.items ?? []).map((service) => [
      service.primary_instance_id,
      service,
    ]),
  );
  const latestTask = [...item.download_tasks].sort(
    (left, right) => Date.parse(right.created_at) - Date.parse(left.created_at),
  )[0];
  const instanceColumns: TableColumn<ReferencingInstance>[] = [
    {
      header: "实例",
      id: "instance",
      render: ({ id, name }) => (
        <Link to={`/models/instances/${encodeURIComponent(id)}`}>{name}</Link>
      ),
    },
    {
      header: "状态",
      id: "status",
      render: ({ status: instanceStatus }) => {
        const presentation = instanceStatusPresentation(instanceStatus);
        return (
          <StatusBadge
            icon={presentation.icon}
            text={presentation.label}
            tone={presentation.tone}
          />
        );
      },
    },
    {
      header: "GPU",
      id: "gpu",
      numeric: true,
      render: ({ id }) =>
        instances.isError ? (
          <MissingValue kind="unavailable" reason="实例数据暂不可用" />
        ) : (
          (gpuCounts.get(id) ?? "—")
        ),
    },
    {
      header: "承载的 AI 服务",
      id: "service",
      render: ({ id }) => {
        if (services.isError) {
          return (
            <MissingValue kind="unavailable" reason="AI 服务数据暂不可用" />
          );
        }
        const service = servicesByInstance.get(id);
        return service === undefined ? (
          "—"
        ) : (
          <Link to={`/services/${encodeURIComponent(service.id)}`}>
            {service.name}
          </Link>
        );
      },
    },
  ];
  const fileColumns: TableColumn<ArtifactFile>[] = [
    {
      header: "文件路径",
      id: "path",
      render: ({ path }) => (
        <span className="pw-models-technical pw-models-detail__file-path">
          {path}
        </span>
      ),
    },
    {
      header: "大小",
      id: "size",
      numeric: true,
      render: ({ size_bytes }) => formatBytes(size_bytes),
    },
    {
      header: "SHA-256",
      id: "sha256",
      render: ({ path, sha256 }) => (
        <span className="pw-models-detail__hash">
          <span className="pw-models-technical" title={sha256}>
            {sha256.slice(0, 12)}
          </span>
          <CopyControl
            aria-label={`复制 ${path} 的 SHA-256`}
            label="复制"
            value={sha256}
          />
        </span>
      ),
    },
  ];

  return (
    <article className="pw-models-detail">
      <nav aria-label="面包屑" className="pw-models-detail__breadcrumb">
        <Link to="/models">模型</Link>
        <span aria-hidden="true">›</span>
        <Link to={LOCAL_MODELS_PATH}>本地模型</Link>
        <span aria-hidden="true">›</span>
        <span aria-current="page">{item.display_name}</span>
      </nav>
      <header className="pw-models-detail__head">
        <div className="pw-models-detail__identity">
          <div className="pw-models-detail__title">
            <h1 ref={heading} tabIndex={-1}>
              {item.display_name}
            </h1>
            <StatusBadge
              icon={status.icon}
              text={status.label}
              tone={status.tone}
            />
            <span className="pw-models-storage">
              {storageTypeLabel(item.storage_type)}
            </span>
          </div>
          <p>
            {originTypeLabel(item.source)} · {formatBytes(item.size_bytes)} ·{" "}
            {item.referencing_instances === null
              ? "引用关系暂不可用"
              : `被 ${String(item.referencing_instances)} 个模型实例引用`}
          </p>
        </div>
        <div className="pw-models-row-actions__buttons">
          {item.status === "Ready" ? (
            <Button
              onClick={() => {
                void navigate(
                  `/models/deploy?artifact_id=${encodeURIComponent(item.id)}`,
                );
              }}
              variant="primary"
            >
              部署
            </Button>
          ) : null}
          <ArtifactVerifyAction
            apiClient={apiClient}
            item={item}
            refreshLists={refreshAfterVerify}
          />
          <ArtifactDeleteAction
            apiClient={apiClient}
            item={item}
            refreshLists={afterDelete}
          />
        </div>
      </header>
      {detail.isError ? (
        <InlineAlert title="本地模型详情数据可能延迟" tone="notice">
          自动刷新失败，仍显示最近一次成功取得的数据。
        </InlineAlert>
      ) : null}
      {verifying ? (
        <InlineAlert title="正在重新校验" tone="info">
          系统正在重新校验这份本地模型的文件，完成后状态与校验结果会自动更新。
        </InlineAlert>
      ) : null}
      {item.status === "Ready" ? null : (
        <InlineAlert
          title={item.status === "Corrupted" ? "文件异常" : "文件缺失"}
          tone="negative"
        >
          {managed
            ? "先重新校验，仍未通过可删除文件后到「下载模型」重新下载。"
            : item.status === "Corrupted"
              ? "外部目录中的文件未通过校验。平台只读，不会修改外部目录，请在服务器上修复源目录后重新校验。"
              : "在外部目录登记的路径下找不到模型文件。平台只读，不会修改外部目录，请在服务器上恢复路径后重新校验。"}
        </InlineAlert>
      )}

      <Section title="模型信息">
        <dl className="pw-models-detail__facts">
          <Fact label="架构">{item.model.architecture ?? UNRECOGNIZED}</Fact>
          <Fact label="模态">
            {item.model.modalities === null
              ? UNRECOGNIZED
              : item.model.modalities.join("、")}
          </Fact>
          <Fact label="精度 / 量化">
            {item.model.precision === null && item.model.quantization === null
              ? UNRECOGNIZED
              : [item.model.precision, item.model.quantization]
                  .filter((value) => value !== null)
                  .join(" / ")}
          </Fact>
          <Fact label="上下文长度">
            {item.model.context_length === null
              ? UNRECOGNIZED
              : item.model.context_length.toLocaleString("en-US")}
          </Fact>
          <Fact label="支持的 Runtime">
            {runtimes.isPending ? (
              <span role="status">正在读取</span>
            ) : runtimes.isError ? (
              "暂不可用"
            ) : runtimes.data.runtimes.length === 0 ? (
              "无可用 Runtime"
            ) : (
              runtimes.data.runtimes
                .map(({ name, version }) => `${runtimeLabel(name)} ${version}`)
                .join("、")
            )}
          </Fact>
          <Fact label="文件大小">{formatBytes(item.size_bytes)}</Fact>
        </dl>
      </Section>

      <Section title="来源与位置">
        <dl className="pw-models-detail__facts">
          <Fact label="来源">
            <SourceValue item={item} />
          </Fact>
          <Fact label="Revision">
            <span className="pw-models-technical">
              {item.resolved_revision ?? "—"}
            </span>
            {item.storage_type === "External" ? (
              <span className="pw-models-muted">外部目录没有 Revision</span>
            ) : null}
            {item.source === "ModelScope" ? (
              <span className="pw-models-muted">
                以下载时的文件清单与逐文件哈希固定版本
              </span>
            ) : null}
          </Fact>
          <Fact label="本地路径">
            <TruncatedValue copyLabel="复制路径" value={item.path} />
          </Fact>
          <Fact label="下载任务">
            {latestTask === undefined ? (
              managed ? (
                "无"
              ) : (
                "无（外部目录注册，不经下载）"
              )
            ) : (
              <>
                <Link to="/models?tab=local-models">
                  {downloadStatusDisplay(latestTask.status).label}
                </Link>
                {latestTask.completed_at === null ? null : (
                  <>
                    {" · "}
                    <time dateTime={latestTask.completed_at}>
                      {formatObservedAt(latestTask.completed_at)}
                    </time>
                  </>
                )}
              </>
            )}
          </Fact>
          <Fact label="登记时间">
            <time dateTime={item.created_at}>
              {formatObservedAt(item.created_at)}
            </time>
          </Fact>
        </dl>
      </Section>

      <Section title="校验结果">
        <dl className="pw-models-detail__facts">
          <Fact label="最近校验时间">
            {item.last_verified_at === null ? (
              "尚未校验"
            ) : (
              <time dateTime={item.last_verified_at}>
                {formatObservedAt(item.last_verified_at)}
              </time>
            )}
          </Fact>
          <Fact label="整体结论">
            <StatusBadge
              icon={status.icon}
              text={status.label}
              tone={status.tone}
            />
          </Fact>
        </dl>
      </Section>

      <Section title="引用实例">
        <DataTable
          caption="引用实例"
          columns={instanceColumns}
          emptyContent="没有模型实例引用这份本地模型"
          getRowKey={({ id }) => id}
          rows={item.instances}
        />
      </Section>

      <Section
        count={
          item.files === null
            ? undefined
            : `${String(item.files.length)} 个文件`
        }
        title="文件清单"
      >
        {item.files === null ? (
          <div className="pw-models-detail__pending">
            <span>文件清单将在下一次完整校验后显示</span>
            <ArtifactVerifyAction
              apiClient={apiClient}
              item={item}
              refreshLists={refreshAfterVerify}
            />
          </div>
        ) : (
          <DataTable
            caption="文件清单"
            columns={fileColumns}
            getRowKey={({ path }) => path}
            rows={item.files}
          />
        )}
      </Section>
    </article>
  );
}

function Section({
  children,
  count,
  title,
}: {
  children: ReactNode;
  count?: string;
  title: string;
}) {
  return (
    <section aria-label={title} className="pw-models-detail__section">
      <header className="pw-models-section__head">
        <span>
          <h2>{title}</h2>
          {count === undefined ? null : <span>{count}</span>}
        </span>
      </header>
      {children}
    </section>
  );
}

function Fact({ children, label }: { children: ReactNode; label: string }) {
  return (
    <div className="pw-models-detail__fact">
      <dt>{label}</dt>
      <dd>{children}</dd>
    </div>
  );
}

function SourceValue({ item }: { item: ModelArtifactDetail }) {
  if (item.source === "Local Path") {
    return <>服务器本地目录（外部目录，平台只读、不复制、不删除）</>;
  }
  const url =
    item.source === "Hugging Face"
      ? `https://huggingface.co/${item.repository_id}`
      : `https://modelscope.cn/models/${item.repository_id}`;
  return (
    <a href={url} rel="noreferrer noopener" target="_blank">
      {url}
    </a>
  );
}
