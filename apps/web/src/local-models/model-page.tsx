import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router";

import { ApiError, type ApiClient } from "../api/client";
import {
  MODEL_ARTIFACT_STATUSES,
  MODEL_ORIGIN_TYPES,
  ModelAcquisitionContractError,
  type ModelArtifactItem,
  type ModelArtifactStatus,
  type ModelOriginType,
} from "../api/model-acquisition-contracts";
import {
  Button,
  DataTable,
  EmptyState,
  InlineAlert,
  MissingValue,
  StatusBadge,
  Tabs,
  TruncatedValue,
  type TableColumn,
} from "../components";
import { formatBytes, formatObservedAt } from "../system/system-page";
import { ArtifactDeleteAction, ArtifactVerifyAction } from "./artifact-actions";
import { DownloadTasksSection } from "./download-section";
import {
  artifactStatusPresentation,
  originTypeLabel,
  shortRevision,
  storageTypeLabel,
} from "./presentation";
import {
  ARTIFACT_POLL_INTERVAL_MS,
  DOWNLOAD_COUNTS_QUERY_KEY,
  DOWNLOAD_TASKS_QUERY_KEY,
  FINISHED_DOWNLOADS_QUERY_KEY,
  MODEL_ARTIFACTS_QUERY_KEY,
  type ArtifactFilters,
  useModelQueries,
} from "./queries";
import { ReadError, SectionHeading, StaleNotice } from "./section-parts";
import { ModelInstancesTab } from "../model-instances/instances-tab";

const FILTER_KEYS = ["search", "source", "status"] as const;
/** "未被引用" lives in the status filter but is applied on the loaded rows. */
const UNREFERENCED = "unreferenced";
const ARTIFACTS_SECTION_ID = "pw-local-models-section";

export function ModelsPage({
  apiClient,
  defaultTab = "model-instances",
}: {
  apiClient: ApiClient;
  defaultTab?: "local-models" | "model-instances";
}) {
  const heading = useRef<HTMLHeadingElement>(null);
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const [filterNotice, setFilterNotice] = useState<string>();
  const parsedFilters = useMemo(
    () => readFilters(searchParams),
    [searchParams],
  );
  const queries = useModelQueries(apiClient, parsedFilters.filters);
  // detail-page breadcrumbs link to /models?tab=local-models.
  const initialTab =
    searchParams.get("tab") === "local-models" ? "local-models" : defaultTab;

  useEffect(() => {
    heading.current?.focus();
  }, []);

  useEffect(() => {
    if (parsedFilters.invalidKeys.length === 0) return;
    const next = new URLSearchParams(searchParams);
    for (const key of parsedFilters.invalidKeys) next.delete(key);
    setSearchParams(next, { replace: true });
    setFilterNotice(
      "已清除无法识别的筛选条件，列表仅使用受支持的筛选重新加载。",
    );
  }, [parsedFilters.invalidKeys, searchParams, setSearchParams]);

  const permissionDenied = [queries.downloads, queries.artifacts].some(
    (query) =>
      query.data === undefined &&
      query.error instanceof ApiError &&
      query.error.status === 403,
  );
  const contractError = [queries.downloads.error, queries.artifacts.error].find(
    (error) => error instanceof ModelAcquisitionContractError,
  );
  if (contractError !== undefined) throw contractError;
  const artifactCount =
    queries.artifacts.data?.pages.reduce(
      (count, page) => count + page.items.length,
      0,
    ) ?? 0;
  const showDownloadInEmpty =
    queries.artifacts.data !== undefined &&
    artifactCount === 0 &&
    Object.values(parsedFilters.filters).every(
      (value) => value === undefined,
    ) &&
    !parsedFilters.unreferenced;

  function updateFilter(key: (typeof FILTER_KEYS)[number], value: string) {
    const next = new URLSearchParams(searchParams);
    if (value.length === 0) next.delete(key);
    else next.set(key, value);
    setFilterNotice(undefined);
    setSearchParams(next);
  }

  function clearFilters() {
    setFilterNotice(undefined);
    setSearchParams(new URLSearchParams());
  }

  // 下载完成 -> 查看本地模型: the task item carries no artifact id, so narrow by name only.
  function showArtifactsByName(repositoryId: string) {
    setFilterNotice(undefined);
    setSearchParams(new URLSearchParams({ search: repositoryId }));
    window.setTimeout(() => {
      const section = document.getElementById(ARTIFACTS_SECTION_ID);
      // jsdom has no scrollIntoView; browsers always do.
      if (section !== null && typeof section.scrollIntoView === "function") {
        section.scrollIntoView({ behavior: "smooth", block: "start" });
      }
    }, 0);
  }

  if (permissionDenied) {
    return (
      <section className="pw-models-page" role="alert">
        <h1 ref={heading} tabIndex={-1}>
          模型
        </h1>
        <InlineAlert title="无权限查看本地模型" tone="negative">
          当前账号无权管理本地模型，页面数据无法显示。请联系管理员授予权限后重试。
        </InlineAlert>
      </section>
    );
  }

  return (
    <div className="pw-models-page">
      <header className="pw-models-page__head">
        <span>
          <h1 ref={heading} tabIndex={-1}>
            模型
          </h1>
          <p>查看下载任务与已经落盘的本地模型。</p>
        </span>
        {showDownloadInEmpty ? null : (
          <Button
            onClick={() => {
              void navigate("/models/download");
            }}
            variant="primary"
          >
            下载模型
          </Button>
        )}
      </header>
      {filterNotice === undefined ? null : (
        <InlineAlert title="筛选条件已修正" tone="notice">
          {filterNotice}
        </InlineAlert>
      )}
      <Tabs
        aria-label="模型信息"
        defaultValue={initialTab}
        items={[
          {
            content: <ModelInstancesTab apiClient={apiClient} />,
            label: "模型实例",
            value: "model-instances",
          },
          {
            content: (
              <LocalModelsTab
                apiClient={apiClient}
                clearFilters={clearFilters}
                filters={parsedFilters.filters}
                queries={queries}
                showArtifactsByName={showArtifactsByName}
                showDownloadInEmpty={showDownloadInEmpty}
                unreferenced={parsedFilters.unreferenced}
                updateFilter={updateFilter}
              />
            ),
            label: "本地模型",
            value: "local-models",
          },
        ]}
      />
    </div>
  );
}

type ModelQueries = ReturnType<typeof useModelQueries>;

function LocalModelsTab({
  apiClient,
  clearFilters,
  filters,
  queries,
  showArtifactsByName,
  showDownloadInEmpty,
  unreferenced,
  updateFilter,
}: {
  apiClient: ApiClient;
  clearFilters: () => void;
  filters: ArtifactFilters;
  queries: ModelQueries;
  showArtifactsByName: (repositoryId: string) => void;
  showDownloadInEmpty: boolean;
  unreferenced: boolean;
  updateFilter: (key: (typeof FILTER_KEYS)[number], value: string) => void;
}) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const artifacts = useMemo(() => {
    const items =
      queries.artifacts.data?.pages.flatMap(({ items }) => items) ?? [];
    return unreferenced
      ? items.filter(({ referencing_instances }) => referencing_instances === 0)
      : items;
  }, [queries.artifacts.data, unreferenced]);
  const summary = queries.artifacts.data?.pages[0]?.summary;

  async function refreshLists() {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: DOWNLOAD_TASKS_QUERY_KEY }),
      queryClient.invalidateQueries({ queryKey: FINISHED_DOWNLOADS_QUERY_KEY }),
      queryClient.invalidateQueries({ queryKey: DOWNLOAD_COUNTS_QUERY_KEY }),
      queryClient.invalidateQueries({ queryKey: MODEL_ARTIFACTS_QUERY_KEY }),
    ]);
  }

  const activeFilters =
    Object.values(filters).filter((value) => value !== undefined).length +
    (unreferenced ? 1 : 0);
  return (
    <div className="pw-models-stack">
      <Summary loading={queries.artifacts.isPending} summary={summary} />
      <DownloadTasksSection
        apiClient={apiClient}
        live={queries.downloads}
        onViewArtifacts={showArtifactsByName}
        refreshLists={refreshLists}
      />
      <StaleNotice
        dataUpdatedAt={queries.artifacts.dataUpdatedAt}
        interval={ARTIFACT_POLL_INTERVAL_MS}
        label="本地模型"
        stale={
          queries.artifacts.data !== undefined && queries.artifacts.isError
        }
      />
      <section
        className="pw-models-section pw-models-section--artifacts"
        id={ARTIFACTS_SECTION_ID}
      >
        <SectionHeading
          count={
            queries.artifacts.data === undefined ? undefined : artifacts.length
          }
          refreshing={
            queries.artifacts.isFetching && !queries.artifacts.isPending
          }
          title="本地模型"
        />
        <ArtifactFiltersBar
          clearFilters={clearFilters}
          filters={filters}
          unreferenced={unreferenced}
          updateFilter={updateFilter}
        />
        {queries.artifacts.data === undefined && queries.artifacts.isError ? (
          <ReadError
            onRetry={() => void queries.artifacts.refetch()}
            title="无法获取本地模型"
          />
        ) : (
          <DataTable
            caption="本地模型"
            columns={artifactColumns(navigate, apiClient, refreshLists)}
            emptyContent={
              activeFilters > 0 ? (
                <EmptyState
                  action={<Button onClick={clearFilters}>清除筛选</Button>}
                  description="当前名称或筛选组合没有匹配的本地模型。"
                  icon="search"
                  title="没有匹配结果"
                  variant="search-no-results"
                />
              ) : (
                <EmptyState
                  action={
                    showDownloadInEmpty ? (
                      <Button
                        onClick={() => {
                          void navigate("/models/download");
                        }}
                        variant="primary"
                      >
                        下载模型
                      </Button>
                    ) : undefined
                  }
                  description="模型文件成功发布或外部目录完成注册后，会出现在这里供模型实例使用。"
                  icon="layers"
                  title="还没有本地模型"
                />
              )
            }
            getRowKey={({ id }) => id}
            loading={queries.artifacts.isPending}
            rows={artifacts}
          />
        )}
        {queries.artifacts.hasNextPage ? (
          <Button
            loading={queries.artifacts.isFetchingNextPage}
            loadingText="正在加载"
            onClick={() => void queries.artifacts.fetchNextPage()}
          >
            加载更多本地模型
          </Button>
        ) : null}
      </section>
    </div>
  );
}

function Summary({
  loading,
  summary,
}: {
  loading: boolean;
  summary:
    | {
        active_download_tasks: number;
        managed_bytes: number;
        storage_available_bytes: number | null;
      }
    | undefined;
}) {
  const cards = [
    {
      label: "本地模型占用",
      value:
        summary === undefined ? undefined : formatBytes(summary.managed_bytes),
    },
    {
      label: "模型存储目录可用空间",
      value:
        summary === undefined
          ? undefined
          : summary.storage_available_bytes === null
            ? null
            : formatBytes(summary.storage_available_bytes),
    },
    {
      label: "进行中的下载任务数",
      value:
        summary === undefined
          ? undefined
          : String(summary.active_download_tasks),
    },
  ] as const;
  return (
    <section
      aria-busy={loading}
      aria-label={loading ? "本地模型统计正在加载" : "本地模型统计"}
      className="pw-models-summary"
    >
      {cards.map((card) => (
        <div className="pw-models-summary__item" key={card.label}>
          <span>{card.label}</span>
          {card.value === undefined && loading ? (
            <span
              aria-hidden="true"
              className="sk pw-models-summary__skeleton"
            />
          ) : card.value === undefined || card.value === null ? (
            <MissingValue
              kind="unavailable"
              reason={
                card.label === "模型存储目录可用空间"
                  ? "读不到模型存储目录可用空间"
                  : "本地模型统计暂不可用"
              }
            />
          ) : (
            <strong>{card.value}</strong>
          )}
        </div>
      ))}
    </section>
  );
}

function artifactColumns(
  navigate: ReturnType<typeof useNavigate>,
  apiClient: ApiClient,
  refreshLists: () => Promise<void>,
): TableColumn<ModelArtifactItem>[] {
  return [
    {
      header: "模型 · 来源 · Revision",
      id: "model",
      render: (item) => (
        <span className="pw-models-name">
          {/* Open the local-model detail page. */}
          <Link
            className="pw-models-name__link"
            title={item.display_name}
            to={`/models/local/${encodeURIComponent(item.id)}`}
          >
            {item.display_name}
          </Link>
          <span>
            {originTypeLabel(item.source)} ·{" "}
            <span
              className="pw-models-technical"
              title={item.resolved_revision ?? undefined}
            >
              {shortRevision(item.resolved_revision)}
            </span>
          </span>
          <span className="pw-models-path">
            <span className="pw-models-storage">
              {storageTypeLabel(item.storage_type)}
            </span>
            <TruncatedValue copyLabel="复制路径" value={item.path} />
          </span>
        </span>
      ),
    },
    {
      header: "状态",
      id: "status",
      render: (item) => {
        const status = artifactStatusPresentation(item.status);
        return (
          <StatusBadge
            icon={status.icon}
            text={status.label}
            tone={status.tone}
          />
        );
      },
    },
    {
      header: "文件大小",
      id: "size",
      numeric: true,
      render: (item) => formatBytes(item.size_bytes),
    },
    {
      header: "引用实例",
      id: "references",
      numeric: true,
      render: (item) =>
        item.referencing_instances === null ? (
          <MissingValue kind="unavailable" reason="实例数据暂不可用" />
        ) : (
          item.referencing_instances
        ),
    },
    {
      header: "校验时间",
      id: "verified",
      render: (item) =>
        item.last_verified_at === null ? (
          <MissingValue kind="unavailable" reason="校验时间暂不可用" />
        ) : (
          <time
            className="pw-models-technical"
            dateTime={item.last_verified_at}
          >
            {formatObservedAt(item.last_verified_at)}
          </time>
        ),
    },
    {
      header: "操作",
      id: "actions",
      render: (item) => (
        <span className="pw-models-name">
          <span className="pw-models-row-actions__buttons">
            {item.status === "Ready" ? (
              <Button
                onClick={() =>
                  void navigate(
                    `/models/deploy?artifact_id=${encodeURIComponent(item.id)}`,
                  )
                }
                size="s"
                variant="ghost"
              >
                部署
              </Button>
            ) : null}
            <ArtifactVerifyAction
              apiClient={apiClient}
              item={item}
              refreshLists={refreshLists}
            />
            <ArtifactDeleteAction
              apiClient={apiClient}
              item={item}
              refreshLists={refreshLists}
            />
          </span>
          {item.status === "Ready" ? null : item.storage_type === "Managed" ? (
            <span className="pw-models-external-hint">
              先重新校验，仍未通过可删除后重新下载
            </span>
          ) : (
            <span className="pw-models-external-hint">
              {item.status === "Corrupted"
                ? "需在服务器上修复源目录"
                : "需在服务器上恢复路径"}
            </span>
          )}
        </span>
      ),
    },
  ];
}

function ArtifactFiltersBar({
  clearFilters,
  filters,
  unreferenced,
  updateFilter,
}: {
  clearFilters: () => void;
  filters: ArtifactFilters;
  unreferenced: boolean;
  updateFilter: (key: (typeof FILTER_KEYS)[number], value: string) => void;
}) {
  const chips = [
    filters.search === undefined
      ? undefined
      : ["search", `名称：${filters.search}`],
    filters.source === undefined
      ? undefined
      : ["source", `来源：${originTypeLabel(filters.source)}`],
    filters.status === undefined
      ? undefined
      : ["status", `状态：${artifactStatusPresentation(filters.status).label}`],
    unreferenced ? ["status", "状态：未被引用"] : undefined,
  ].filter((chip): chip is [string, string] => chip !== undefined);
  return (
    <div className="pw-models-filters">
      <label className="pw-models-search">
        <span className="pw-visually-hidden">搜索模型名称</span>
        <input
          onChange={(event) => {
            updateFilter("search", event.target.value);
          }}
          placeholder="搜索模型名称"
          type="search"
          value={filters.search ?? ""}
        />
      </label>
      <FilterSelect
        label="来源"
        onChange={(value) => {
          updateFilter("source", value);
        }}
        options={MODEL_ORIGIN_TYPES.map((value) => [
          value,
          originTypeLabel(value),
        ])}
        value={filters.source ?? ""}
      />
      <FilterSelect
        label="状态"
        onChange={(value) => {
          updateFilter("status", value);
        }}
        options={[
          ...MODEL_ARTIFACT_STATUSES.map(
            (value) =>
              [value, artifactStatusPresentation(value).label] as const,
          ),
          [UNREFERENCED, "未被引用"] as const,
        ]}
        value={unreferenced ? UNREFERENCED : (filters.status ?? "")}
      />
      {chips.length === 0 ? null : (
        <span className="pw-models-filter-chips" aria-label="当前筛选">
          {chips.map(([key, label]) => (
            <Button
              aria-label={`移除筛选：${label}`}
              icon="close"
              key={key}
              onClick={() => {
                updateFilter(key as (typeof FILTER_KEYS)[number], "");
              }}
              size="s"
              variant="ghost"
            >
              {label}
            </Button>
          ))}
          <Button onClick={clearFilters} size="s" variant="ghost">
            清除全部
          </Button>
        </span>
      )}
    </div>
  );
}

function FilterSelect({
  label,
  onChange,
  options,
  value,
}: {
  label: string;
  onChange: (value: string) => void;
  options: readonly (readonly [string, string])[];
  value: string;
}) {
  return (
    <label className="pw-models-select">
      <span className="pw-visually-hidden">{label}</span>
      <select
        aria-label={label}
        onChange={(event) => {
          onChange(event.target.value);
        }}
        value={value}
      >
        <option value="">{label}：全部</option>
        {options.map(([optionValue, optionLabel]) => (
          <option key={optionValue} value={optionValue}>
            {optionLabel}
          </option>
        ))}
      </select>
    </label>
  );
}

function readFilters(searchParams: URLSearchParams): {
  filters: ArtifactFilters;
  invalidKeys: string[];
  unreferenced: boolean;
} {
  // Anything outside FILTER_KEYS is dropped, including the retired storage_type.
  const invalidKeys = [...searchParams.keys()].filter(
    (key) => key !== "tab" && !(FILTER_KEYS as readonly string[]).includes(key),
  );
  const search = searchParams.get("search");
  const source = searchParams.get("source");
  const status = searchParams.get("status");
  if (search !== null && (search.length === 0 || search.length > 200))
    invalidKeys.push("search");
  if (source !== null && !isMember(MODEL_ORIGIN_TYPES, source))
    invalidKeys.push("source");
  if (
    status !== null &&
    status !== UNREFERENCED &&
    !isMember(MODEL_ARTIFACT_STATUSES, status)
  )
    invalidKeys.push("status");
  return {
    filters: {
      search:
        search !== null && !invalidKeys.includes("search") ? search : undefined,
      source:
        source !== null && isMember(MODEL_ORIGIN_TYPES, source)
          ? (source as ModelOriginType)
          : undefined,
      status:
        status !== null && isMember(MODEL_ARTIFACT_STATUSES, status)
          ? (status as ModelArtifactStatus)
          : undefined,
    },
    invalidKeys: [...new Set(invalidKeys)].sort(),
    unreferenced: status === UNREFERENCED,
  };
}

function isMember(values: readonly string[], value: string) {
  return values.includes(value);
}
