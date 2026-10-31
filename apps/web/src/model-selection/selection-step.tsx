import {
  useInfiniteQuery,
  useMutation,
  useQueryClient,
  type InfiniteData,
  type UseInfiniteQueryResult,
} from "@tanstack/react-query";
import {
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { Link, useNavigate } from "react-router";

import { ApiError, type ApiClient } from "../api/client";
import {
  ModelSelectionContractError,
  type ModelCatalogItem,
  type ModelCatalogPage,
  type ResolveRequest,
  type ResolvedPreviewResponse,
} from "../api/model-selection-contracts";
import {
  Button,
  DataTable,
  EmptyState,
  InlineAlert,
  TextField,
  type TableColumn,
} from "../components";
import { originTypeLabel } from "../local-models/presentation";
import {
  DOWNLOAD_TASKS_QUERY_KEY,
  MODEL_ARTIFACTS_QUERY_KEY,
} from "../local-models/queries";
import { MODEL_DEPLOY_PATH } from "../model-instances/deploy-wizard";
import {
  modelSourceValidationActionLabel,
  modelSourceValidationReasonLabel,
} from "../model-sources/presentation";
import { formatBytes } from "../system/system-page";
import { ModelWriteIntentKeys, type ModelWriteAction } from "./idempotency";
import { localAvailabilityPresentation } from "./presentation";

export const MODEL_CATALOG_QUERY_KEY = ["models", "catalog"] as const;
type RemoteSource = "Hugging Face" | "ModelScope";
type Source = "catalog" | RemoteSource | "Local Path";
const LOCAL_PATH_REJECTED = "找不到这个目录，或它不在允许注册的范围内";
type Unresolved = Extract<
  Awaited<ReturnType<ApiClient["resolveModel"]>>,
  { outcome: "unresolved" }
>;

export function ModelSelectionStep({ apiClient }: { apiClient: ApiClient }) {
  const repositoryInputId = useId();
  const [source, setSource] = useState<Source>("Hugging Face");
  const [input, setInput] = useState("");
  const [revision, setRevision] = useState("");
  const [preview, setPreview] = useState<ResolvedPreviewResponse>();
  const [availableBytes, setAvailableBytes] = useState<number | null>(null);
  const [unresolved, setUnresolved] = useState<Unresolved>();
  const [resolveError, setResolveError] = useState<Error>();
  const [fieldError, setFieldError] = useState<string>();
  const [writeError, setWriteError] = useState<Error>();
  const [writeAction, setWriteAction] = useState<ModelWriteAction | "deploy">(
    "download",
  );
  const [done, setDone] = useState<"created" | "joined_existing" | "load">();
  const [localAccepted, setLocalAccepted] = useState(false);
  const [replayed, setReplayed] = useState(false);
  const [fatalError, setFatalError] = useState<Error>();
  const [slowResolve, setSlowResolve] = useState(false);
  const intentKeys = useRef(new ModelWriteIntentKeys());
  // Ignore responses for an input/source that the administrator has since changed.
  const generation = useRef(0);
  const [readingGeneration, setReadingGeneration] = useState<number>();
  const reading = readingGeneration !== undefined;
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const catalog = useInfiniteQuery<
    ModelCatalogPage,
    Error,
    InfiniteData<ModelCatalogPage>,
    typeof MODEL_CATALOG_QUERY_KEY,
    string | undefined
  >({
    enabled: source === "catalog",
    getNextPageParam: (lastPage) => lastPage.next_cursor ?? undefined,
    initialPageParam: undefined,
    queryFn: ({ pageParam }) => apiClient.getModelCatalog(pageParam),
    queryKey: MODEL_CATALOG_QUERY_KEY,
  });
  const catalogItems = useMemo(
    () => catalog.data?.pages.flatMap(({ items }) => items) ?? [],
    [catalog.data],
  );
  const resolveMutation = useMutation({
    mutationFn: async (body: ResolveRequest) => {
      const result = await apiClient.resolveModel(body);
      if (result.outcome !== "resolved") return { result, storage: null };
      const storage = await apiClient
        .getModelArtifacts()
        .then((page) => page.summary.storage_available_bytes)
        .catch(() => null);
      return { result, storage };
    },
  });
  const writeMutation = useMutation({
    mutationFn: async ({
      action,
      body,
      key,
    }: {
      action: ModelWriteAction;
      body: ResolveRequest;
      key: string;
    }) =>
      action === "download"
        ? apiClient.createDownload(body, key)
        : apiClient.registerModel(body, key),
  });
  const deployMutation = useMutation({
    mutationFn: async (value: ResolvedPreviewResponse) => {
      const model = value.resolved;
      const page = await apiClient.getModelArtifacts({
        search: model.repository_id,
        source: model.source,
        status: "Ready",
      });
      return page.items.find(
        (item) =>
          item.repository_id.toLowerCase() ===
            model.repository_id.toLowerCase() &&
          item.resolved_revision === model.resolved_revision,
      )?.id;
    },
  });

  useEffect(() => {
    if (!reading) {
      setSlowResolve(false);
      return;
    }
    const timer = window.setTimeout(() => {
      setSlowResolve(true);
    }, 10_000);
    return () => {
      window.clearTimeout(timer);
    };
  }, [readingGeneration, reading]);

  function clearResult() {
    generation.current += 1;
    setReadingGeneration(undefined);
    setSlowResolve(false);
    setLocalAccepted(false);
    setPreview(undefined);
    setUnresolved(undefined);
    setResolveError(undefined);
    setFieldError(undefined);
    setWriteError(undefined);
    setDone(undefined);
    setAvailableBytes(null);
    intentKeys.current.invalidate();
  }
  function changeSource(next: Source) {
    if (source === next) return;
    clearResult();
    setSource(next);
    setInput("");
    setRevision("");
  }
  function normalizeInput(value = input) {
    const normalized = value.trim();
    setInput(normalized);
    return normalized;
  }
  async function readModel(
    nextSource: RemoteSource,
    repositoryId: string,
    nextRevision: string,
  ) {
    const normalized = normalizeInput(repositoryId);
    if (normalized.length < 3) {
      setFieldError(
        `请填写 ${nextSource === "Hugging Face" ? "Repository ID" : "Model ID"}，格式为「组织/仓库名」，例如 Qwen/Qwen2.5-7B-Instruct。`,
      );
      document.getElementById(repositoryInputId)?.focus();
      return;
    }
    clearResult();
    const current = generation.current;
    setReadingGeneration(current);
    try {
      const { result, storage } = await resolveMutation.mutateAsync(
        remoteBody(nextSource, normalized, nextRevision),
      );
      if (current !== generation.current) return;
      if (result.outcome === "external_path_accepted")
        throw new ModelSelectionContractError();
      if (result.outcome === "unresolved") {
        setUnresolved(result);
        return;
      }
      setAvailableBytes(storage);
      setPreview(result);
    } catch (error) {
      if (current !== generation.current) return;
      if (error instanceof ModelSelectionContractError) {
        setFatalError(error);
        return;
      }
      setResolveError(
        error instanceof Error ? error : new Error("model resolution failed"),
      );
      if (error instanceof ApiError && error.status === 400)
        setFieldError(
          `${error.responseMessage ?? "模型标识不正确。"} Repository ID 需为「组织/仓库名」两段，例如 Qwen/Qwen3-32B。`,
        );
    } finally {
      if (current === generation.current) {
        setReadingGeneration(undefined);
        setSlowResolve(false);
      }
    }
  }
  async function checkLocalPath() {
    const normalized = normalizeInput();
    if (writeMutation.isPending || done || localAccepted) return;
    clearResult();
    if (!normalized.startsWith("/")) {
      setFieldError("必须是以 / 开头的绝对路径");
      return;
    }
    const current = generation.current;
    setReadingGeneration(current);
    try {
      const { result } = await resolveMutation.mutateAsync({
        source: "Local Path",
        external_path: normalized,
      });
      if (current !== generation.current) return;
      if (result.outcome === "external_path_accepted") setLocalAccepted(true);
      else if (result.outcome === "unresolved")
        setFieldError(LOCAL_PATH_REJECTED);
      else throw new ModelSelectionContractError();
    } catch (error) {
      if (current !== generation.current) return;
      if (error instanceof ModelSelectionContractError) setFatalError(error);
      else
        setResolveError(
          error instanceof Error ? error : new Error("path resolution failed"),
        );
    } finally {
      if (current === generation.current) setReadingGeneration(undefined);
    }
  }
  function retryRead() {
    if (source === "Local Path") void checkLocalPath();
    else if (source !== "catalog") void readModel(source, input, revision);
  }
  const insufficient =
    preview?.local_availability === "absent" &&
    preview.resolved.total_size_bytes.state === "identified" &&
    availableBytes !== null &&
    availableBytes < preview.resolved.total_size_bytes.value;
  async function writeModel(action: ModelWriteAction) {
    const normalized = normalizeInput();
    if (
      (source === "Local Path"
        ? !localAccepted || done !== undefined
        : !preview) ||
      source === "catalog" ||
      (action === "download" && insufficient)
    )
      return;
    const current = generation.current;
    const body: ResolveRequest =
      source === "Local Path"
        ? { source, external_path: normalized }
        : remoteBody(source, normalized, revision);
    const key = intentKeys.current.acquire({
      action,
      repositoryId: normalized,
      source,
    });
    setWriteAction(action);
    setWriteError(undefined);
    setUnresolved(undefined);
    try {
      const result = await writeMutation.mutateAsync({ action, body, key });
      intentKeys.current.settle(key, true);
      if (current !== generation.current) return;
      if ("outcome" in result) {
        if (source === "Local Path") {
          setLocalAccepted(false);
          setFieldError(LOCAL_PATH_REJECTED);
        } else setUnresolved(result);
        return;
      }
      setDone("disposition" in result ? result.disposition : "load");
      setReplayed(result.replayed);
      await Promise.all(
        [
          MODEL_CATALOG_QUERY_KEY,
          DOWNLOAD_TASKS_QUERY_KEY,
          MODEL_ARTIFACTS_QUERY_KEY,
        ].map((queryKey) => queryClient.invalidateQueries({ queryKey })),
      );
    } catch (error) {
      intentKeys.current.settle(
        key,
        error instanceof ApiError && error.status < 500,
      );
      if (current !== generation.current) return;
      if (error instanceof ModelSelectionContractError) {
        setFatalError(error);
        return;
      }
      setWriteError(
        error instanceof Error ? error : new Error("model write failed"),
      );
    }
  }
  async function deploy() {
    if (!preview) return;
    setWriteAction("deploy");
    setWriteError(undefined);
    const current = generation.current;
    try {
      const id = await deployMutation.mutateAsync(preview);
      if (current === generation.current)
        void navigate(
          id
            ? `${MODEL_DEPLOY_PATH}?artifact_id=${encodeURIComponent(id)}`
            : MODEL_DEPLOY_PATH,
        );
    } catch (error) {
      if (current === generation.current)
        setWriteError(
          error instanceof Error ? error : new Error("artifact lookup failed"),
        );
    }
  }
  if (fatalError) throw fatalError;
  if (catalog.error instanceof ModelSelectionContractError) throw catalog.error;
  const writing = writeMutation.isPending;
  return (
    <section className="pw-model-selection" aria-label="选择模型">
      <div
        className="pw-model-selection__sources"
        role="group"
        aria-label="模型来源"
      >
        {(["catalog", "Hugging Face", "ModelScope", "Local Path"] as const).map(
          (value) => (
            <Button
              key={value}
              aria-pressed={source === value}
              onClick={() => {
                changeSource(value);
              }}
              variant={source === value ? "primary" : "secondary"}
            >
              {value === "catalog"
                ? "模型目录"
                : value === "Local Path"
                  ? "服务器本地目录"
                  : value}
            </Button>
          ),
        )}
      </div>
      {source === "catalog" ? (
        <CatalogSource
          catalog={catalog}
          items={catalogItems}
          onSelect={(item) => {
            if (item.source === "Local Path" || item.repository_id === null)
              return;
            setSource(item.source);
            setInput(item.repository_id);
            setRevision(item.revision);
            void readModel(item.source, item.repository_id, item.revision);
          }}
        />
      ) : source === "Local Path" ? (
        <section
          className="pw-model-selection__remote"
          aria-labelledby="local-source-heading"
        >
          <h2 id="local-source-heading">注册服务器本地目录</h2>
          <TextField
            label="服务器绝对路径"
            placeholder="/srv/models/llama3-8b"
            autoComplete="off"
            value={input}
            errorText={fieldError}
            helpText={localAccepted && !done ? "目录可以注册" : undefined}
            disabled={writing}
            onChange={(event) => {
              clearResult();
              setInput(event.currentTarget.value);
            }}
            onBlur={() => void checkLocalPath()}
            onKeyDown={(event) => {
              if (event.key === "Enter") {
                event.preventDefault();
                void checkLocalPath();
              }
            }}
          />
          <p className="pw-model-selection__help">
            注册为「外部目录」类型的本地模型：平台只读取和校验，不复制、不修改，也不能通过产品删除目录里的文件。
          </p>
          <div>
            <Button
              variant="primary"
              disabled={!localAccepted || writing || done !== undefined}
              onClick={() => void writeModel("download")}
            >
              注册本地目录
            </Button>
          </div>
        </section>
      ) : (
        <section
          className="pw-model-selection__remote"
          aria-labelledby="model-source-heading"
        >
          <h2 id="model-source-heading">从 {source} 下载</h2>
          <form
            className="pw-model-selection__form"
            noValidate
            onSubmit={(event) => {
              event.preventDefault();
              if (!reading) retryRead();
            }}
          >
            <TextField
              id={repositoryInputId}
              label={source === "Hugging Face" ? "Repository ID" : "Model ID"}
              placeholder="例如 Qwen/Qwen2.5-7B-Instruct"
              autoComplete="off"
              required
              errorText={fieldError}
              value={input}
              onBlur={() => normalizeInput()}
              onChange={(event) => {
                clearResult();
                setInput(event.currentTarget.value);
              }}
            />
            <TextField
              label="Revision（可选）"
              placeholder={
                source === "Hugging Face" ? "例如 main" : "例如 master"
              }
              autoComplete="off"
              value={revision}
              onChange={(event) => {
                clearResult();
                setRevision(event.currentTarget.value);
              }}
            />
            <Button
              type="submit"
              variant="primary"
              disabled={writing}
              loading={reading}
              loadingText="正在读取模型信息…"
            >
              读取模型信息
            </Button>
          </form>
          <p className="pw-model-selection__help">
            从模型源读取：Revision
            解析出的具体版本、文件清单与每个文件的大小（用来算预计大小、检查磁盘空间）、config.json
            里的架构／精度／上下文长度，以及本地是否已有相同版本。只读几 KB
            的元数据和配置文件，不下载权重，也不会登记任何记录。
          </p>
        </section>
      )}
      {slowResolve && source !== "Local Path" ? (
        <p role="status">正在等待模型源返回仓库元数据…</p>
      ) : null}
      {resolveError ? (
        <RequestError
          error={resolveError}
          title="读取信息失败："
          onRetry={retryRead}
        />
      ) : null}
      {unresolved ? (
        <UnresolvedAlert
          value={unresolved}
          source={source}
          onRetry={retryRead}
        />
      ) : null}
      {preview && !done ? (
        <Preview
          value={preview}
          availableBytes={availableBytes}
          insufficient={insufficient}
        >
          {preview.local_availability === "artifact_ready" ? (
            <Button
              variant="primary"
              loading={deployMutation.isPending}
              onClick={() => void deploy()}
            >
              部署这个模型
            </Button>
          ) : preview.local_availability === "download_active" ? (
            <Link to="/models?tab=local-models">查看下载进度</Link>
          ) : preview.deployable_layout === "unsupported" ? null : (
            <>
              <Button
                disabled={writing}
                onClick={() => void writeModel("register")}
              >
                只加载到模型目录
              </Button>
              <Button
                variant="primary"
                disabled={insufficient || writing}
                onClick={() => void writeModel("download")}
              >
                确认下载
              </Button>
            </>
          )}
        </Preview>
      ) : null}
      {writeError ? (
        <RequestError
          error={writeError}
          title={
            source === "Local Path"
              ? "注册本地目录失败"
              : writeAction === "deploy"
                ? "无法读取本地模型"
                : writeAction === "download"
                  ? "确认下载失败"
                  : "加载模型失败"
          }
        />
      ) : null}
      {done ? (
        <InlineAlert
          title={
            source === "Local Path"
              ? done === "created"
                ? "已注册为外部目录。"
                : "这个目录已经注册过。"
              : done === "load"
                ? "已加载到模型目录。"
                : done === "created"
                  ? "已开始下载。"
                  : "已加入进行中的下载。"
          }
          tone="positive"
        >
          {source === "Local Path"
            ? done === "created"
              ? "平台正在校验目录中的文件，校验通过后即可部署。"
              : "没有重复创建，可在「本地模型」里查看它。"
            : done === "load"
              ? "只登记了元数据，没有下载文件；需要时在模型目录里选择它再下载。"
              : "任务在后台进行，可以离开本页；完成并校验通过后会出现在「本地模型」。"}
          {replayed && source !== "Local Path"
            ? "这是之前已提交的同一请求。"
            : null}
          <Link to="/models?tab=local-models">
            {done === "load" ? "返回本地模型" : "查看本地模型"}
          </Link>
        </InlineAlert>
      ) : null}
    </section>
  );
}

function catalogColumns(
  onSelect: (item: ModelCatalogItem) => void,
): TableColumn<ModelCatalogItem>[] {
  return [
    {
      header: "模型",
      id: "model",
      render: (item) => (
        <>
          <strong>{item.display_name}</strong>
          <span className="pw-model-preview__note">
            {knowledgeText(item.architecture)}
          </span>
        </>
      ),
    },
    {
      header: "来源 · Revision",
      id: "source",
      render: (item) => `${originTypeLabel(item.source)} · ${item.revision}`,
    },
    {
      header: "预计大小",
      id: "size",
      numeric: true,
      render: (item) => knowledgeBytes(item.estimated_size_bytes),
    },
    {
      header: "本地文件",
      id: "availability",
      render: (item) => {
        const presentation = localAvailabilityPresentation(
          item.local_availability,
        );
        return (
          <span className="pw-model-selection__availability">
            <span
              aria-hidden="true"
              className={`pw-model-selection__light pw-model-selection__light--${presentation.tone}`}
            />
            {presentation.label}
          </span>
        );
      },
    },
    {
      header: "操作",
      id: "select",
      render: (item) =>
        item.source === "Local Path" ? (
          <span className="pw-model-selection__unavailable">
            本地目录模型不需要下载
          </span>
        ) : (
          <Button
            onClick={() => {
              onSelect(item);
            }}
            size="s"
            variant="ghost"
          >
            选择
          </Button>
        ),
    },
  ];
}

function Preview({
  value,
  availableBytes,
  insufficient,
  children,
}: {
  value: ResolvedPreviewResponse;
  availableBytes: number | null;
  insufficient: boolean;
  children: ReactNode;
}) {
  const model = value.resolved;
  const size = model.total_size_bytes;
  const note =
    value.local_availability === "artifact_ready"
      ? "同一来源、同一版本只保存一份，可以直接部署，不需要再次下载。"
      : value.local_availability === "download_active"
        ? "同一版本已有进行中的下载任务，重复下载会并入那个任务。"
        : insufficient
          ? `磁盘空间不足：需要 ${knowledgeBytes(size)}，模型存储目录剩余 ${formatBytes(availableBytes ?? 0)}。清理未被引用的本地模型后再下载；空间不足时不会创建下载任务。`
          : size.state === "identified" && availableBytes !== null
            ? `下载需要 ${knowledgeBytes(size)}，模型存储目录剩余 ${formatBytes(availableBytes)}。`
            : "无法预先确认磁盘空间；空间不足时下载任务会暂停并提示清理。";
  return (
    <section className="pw-model-preview" aria-label="读取信息预览">
      {value.deployable_layout === "unsupported" &&
      value.local_availability === "absent" ? (
        <InlineAlert title="该模型类型暂不支持" tone="notice">
          平台目前只部署对话类大语言模型，这个模型的文件结构不符合，不能下载或加载。
        </InlineAlert>
      ) : null}
      <header>
        <h2>{model.repository_id}</h2>
        <span className="pw-model-preview__note">
          {model.source} · 读取于刚刚
        </span>
      </header>
      <dl>
        <dt>架构</dt>
        <dd>{knowledgeText(model.architecture)}</dd>
        <dt>模态</dt>
        <dd>{knowledgeList(model.modalities)}</dd>
        <dt>精度 / 量化</dt>
        <dd>
          {knowledgeText(model.precision)} / {knowledgeText(model.quantization)}
        </dd>
        <dt>上下文长度</dt>
        <dd>{knowledgeInteger(model.context_length)}</dd>
        <dt>支持的 Runtime</dt>
        <dd>{knowledgeList(model.candidate_runtimes)}</dd>
        <dt>预计大小</dt>
        <dd>
          {knowledgeBytes(size)}
          {model.files.state === "identified"
            ? `（共 ${String(model.files.value.length)} 个文件）`
            : null}
        </dd>
        <dt>Revision</dt>
        <dd>
          {model.resolved_revision}
          <span className="pw-model-preview__note">
            {model.revision_immutability === "immutable"
              ? `由 ${model.requested_revision_norm} 解析的不可变 Commit`
              : "ModelScope 没有不可变 Revision，下载时以文件清单与逐文件哈希固定版本"}
          </span>
        </dd>
        <dt>本地文件</dt>
        <dd>
          {localAvailabilityPresentation(value.local_availability).previewLabel}
        </dd>
      </dl>
      <div
        role={insufficient ? "alert" : "status"}
        className={`pw-model-selection__notice${insufficient ? " pw-model-selection__notice--negative" : ""}`}
      >
        {note}
      </div>
      <div className="pw-model-selection__actions">{children}</div>
    </section>
  );
}

function UnresolvedAlert({
  value,
  source,
  onRetry,
}: {
  value: Unresolved;
  source: Source;
  onRetry: () => void;
}) {
  const unreachable =
    value.reason_code === "validation_timeout" ||
    value.reason_code === "validation_failed";
  const credentialAction =
    value.next_action === "update_credential" ||
    value.next_action === "check_source_reference_and_configure_credential";
  return (
    <InlineAlert title="读取信息失败：" tone="negative">
      {unreachable ? (
        `连接 ${source} ${value.reason_code === "validation_timeout" ? "超时" : "失败"}，服务器可能无法访问该模型源。当前不能读取该模型。请稍后重试，或改用其他模型源。`
      ) : (
        <>
          {modelSourceValidationReasonLabel(value.reason_code)}{" "}
          当前不能读取或登记该模型。{" "}
          {modelSourceValidationActionLabel(value.next_action)}
        </>
      )}
      {credentialAction ? (
        <Link to="/system?tab=model-sources">前往「系统 › 模型源」</Link>
      ) : null}
      {unreachable || value.next_action === "retry_later" ? (
        <Button onClick={onRetry}>重试</Button>
      ) : null}
    </InlineAlert>
  );
}

function RequestError({
  error,
  title,
  onRetry,
}: {
  error: Error;
  title: string;
  onRetry?: () => void;
}) {
  const apiError = error instanceof ApiError ? error : undefined;
  return (
    <InlineAlert title={title} tone="negative">
      {apiError?.responseMessage ?? "请求没有完成，请检查连接后重试。"}
      {apiError?.requestId === undefined ? null : (
        <span className="pw-model-selection__request-id">
          Request ID：{apiError.requestId}
        </span>
      )}
      {onRetry ? <Button onClick={onRetry}>重试</Button> : null}
    </InlineAlert>
  );
}

function remoteBody(
  source: RemoteSource,
  repositoryId: string,
  revision: string,
): ResolveRequest {
  return {
    repository_id: repositoryId,
    source,
    ...(revision.trim() ? { revision: revision.trim() } : {}),
  };
}
function CatalogSource({
  catalog,
  items,
  onSelect,
}: {
  catalog: UseInfiniteQueryResult<InfiniteData<ModelCatalogPage>>;
  items: ModelCatalogItem[];
  onSelect: (item: ModelCatalogItem) => void;
}) {
  const error = catalog.error;
  if (
    catalog.data === undefined &&
    error instanceof ApiError &&
    error.status === 403
  ) {
    return (
      <InlineAlert title="无权限查看模型目录" tone="negative">
        当前账号不能读取模型目录，无法从目录选择模型。请联系管理员授予权限后重试。
      </InlineAlert>
    );
  }
  if (catalog.data === undefined && error !== null) {
    return (
      <InlineAlert title="无法读取模型目录" tone="negative">
        模型目录加载失败，不能据此判断目录为空。请检查连接后重试。
        <Button onClick={() => void catalog.refetch()}>重新加载</Button>
      </InlineAlert>
    );
  }
  return (
    <div className="pw-model-selection__catalog">
      <DataTable
        caption="模型目录"
        columns={catalogColumns(onSelect)}
        emptyContent={
          <EmptyState
            description="还没有加载任何模型。从 Hugging Face 或 ModelScope 读取并下载第一个模型。"
            icon="layers"
            title="模型目录为空"
          />
        }
        getRowKey={({ id }) => id}
        loading={catalog.isPending}
        rows={items}
      />
      {catalog.hasNextPage ? (
        <Button
          loading={catalog.isFetchingNextPage}
          loadingText="正在加载"
          onClick={() => void catalog.fetchNextPage()}
        >
          加载更多
        </Button>
      ) : null}
    </div>
  );
}

export function knowledgeText(value: { state: string; value?: string }) {
  return value.state === "identified" ? (value.value ?? "未识别") : "未识别";
}

export function knowledgeList(value: { state: string; value?: string[] }) {
  return value.state === "identified"
    ? (value.value?.join("、") ?? "未识别")
    : "未识别";
}

function knowledgeInteger(value: { state: string; value?: number }) {
  return value.state === "identified" && value.value !== undefined
    ? value.value.toLocaleString("zh-CN")
    : "未识别";
}

export function knowledgeBytes(value: { state: string; value?: number }) {
  return value.state === "identified" && value.value !== undefined
    ? formatBytes(value.value)
    : "未识别";
}
