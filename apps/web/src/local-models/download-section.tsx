import {
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { Link } from "react-router";

import { ApiError } from "../api/client";
import {
  ModelAcquisitionContractError,
  type DownloadCommand,
  type DownloadTaskItem,
  type DownloadTaskStatus,
} from "../api/model-acquisition-contracts";
import type { ApiClient } from "../api/client";
import {
  Button,
  DataTable,
  EmptyState,
  InlineAlert,
  MissingValue,
  Modal,
  StatusBadge,
  type TableColumn,
} from "../components";
import { formatBytes, formatObservedAt } from "../system/system-page";
import {
  downloadFailurePresentation,
  downloadStatusDisplay,
  DownloadRateTracker,
  failureStageLabel,
  formatDuration,
  originTypeLabel,
  shortRevision,
  type DownloadRate,
} from "./presentation";
import {
  ACTIVE_DOWNLOAD_STATUSES,
  DOWNLOAD_POLL_INTERVAL_MS,
  FINISHED_DOWNLOAD_STATUSES,
  LIVE_DOWNLOAD_STATUSES,
  useDownloadStatusCounts,
  useFinishedDownloads,
  type useModelQueries,
} from "./queries";
import { ReadError, SectionHeading, StaleNotice } from "./section-parts";

type LiveDownloadsQuery = ReturnType<typeof useModelQueries>["downloads"];

/** First reveal and each "显示更多" step of the collapsed 已结束 group. */
const FINISHED_FIRST_COUNT = 5;
const FINISHED_MORE_COUNT = 10;

const LIVE_STATUS_SET: ReadonlySet<string> = new Set(LIVE_DOWNLOAD_STATUSES);
const FINISHED_STATUS_SET: ReadonlySet<string> = new Set(
  FINISHED_DOWNLOAD_STATUSES,
);
const ACTIVE_STATUS_SET: ReadonlySet<string> = new Set(
  ACTIVE_DOWNLOAD_STATUSES,
);

export function DownloadTasksSection({
  apiClient,
  live,
  onViewArtifacts,
  refreshLists,
}: {
  apiClient: ApiClient;
  live: LiveDownloadsQuery;
  onViewArtifacts: (repositoryId: string) => void;
  refreshLists: () => Promise<void>;
}) {
  const tracker = useRef(new DownloadRateTracker());
  const previousStatuses = useRef<Map<string, string> | undefined>(undefined);
  const [rates, setRates] = useState<ReadonlyMap<string, DownloadRate>>(
    new Map(),
  );
  const [politeAnnouncement, setPoliteAnnouncement] = useState("");
  const [assertiveAnnouncement, setAssertiveAnnouncement] = useState("");
  const [commandErrors, setCommandErrors] = useState<Record<string, ApiError>>(
    {},
  );
  const [fatalError, setFatalError] = useState<Error>();
  const [pendingIds, setPendingIds] = useState<ReadonlySet<string>>(new Set());
  const [finishedOpen, setFinishedOpen] = useState(false);
  const [finishedShown, setFinishedShown] = useState(FINISHED_FIRST_COUNT);
  const counts = useDownloadStatusCounts(apiClient);
  const finished = useFinishedDownloads(apiClient, finishedOpen);

  // Active tasks first, then failed ones; the server order (newest first) holds inside a group.
  const liveItems = useMemo(() => {
    const items = (live.data?.pages.flatMap(({ items }) => items) ?? []).filter(
      ({ status }) => LIVE_STATUS_SET.has(status),
    );
    return [
      ...items.filter(({ status }) => ACTIVE_STATUS_SET.has(status)),
      ...items.filter(({ status }) => !ACTIVE_STATUS_SET.has(status)),
    ];
  }, [live.data]);
  const finishedItems = useMemo(() => {
    const liveIds = new Set(liveItems.map(({ id }) => id));
    return (finished.data?.pages.flatMap(({ items }) => items) ?? []).filter(
      ({ id, status }) => FINISHED_STATUS_SET.has(status) && !liveIds.has(id),
    );
  }, [finished.data, liveItems]);
  const visibleFinished = finishedOpen
    ? finishedItems.slice(0, finishedShown)
    : [];
  const rows = [...liveItems, ...visibleFinished];

  const countsByStatus = counts.data?.counts;
  const activeCount = ACTIVE_DOWNLOAD_STATUSES.reduce(
    (sum, status) => sum + countOf(countsByStatus, status),
    0,
  );
  const failedCount = countOf(countsByStatus, "Failed");
  const completedCount = countOf(countsByStatus, "Completed");
  const cancelledCount = countOf(countsByStatus, "Cancelled");
  const finishedTotal = completedCount + cancelledCount;
  const totalTasks = activeCount + failedCount + finishedTotal;
  const remainingFinished = Math.max(0, finishedTotal - finishedShown);

  useEffect(() => {
    setRates(tracker.current.observe(liveItems));
    const nextStatuses = new Map<string, string>(
      liveItems.map((item): [string, string] => [item.id, item.status]),
    );
    const previous = previousStatuses.current;
    previousStatuses.current = nextStatuses;
    if (previous === undefined) return;
    const ordinary: string[] = [];
    const failures: string[] = [];
    for (const item of liveItems) {
      const oldStatus = previous.get(item.id);
      if (oldStatus === undefined || oldStatus === item.status) continue;
      const label = downloadStatusDisplay(item.status).label;
      // Verifying -> Publishing both read 校验中; announce a label only once.
      if (label === downloadStatusDisplay(oldStatus).label) continue;
      const message = `${item.display_name}状态更新为${label}`;
      if (item.status === "Failed") failures.push(message);
      else ordinary.push(message);
    }
    if (ordinary.length > 0) setPoliteAnnouncement(ordinary.join("；"));
    if (failures.length > 0) setAssertiveAnnouncement(failures.join("；"));
  }, [liveItems, live.dataUpdatedAt]);

  // Reveal more: fetch the next server page while the shown window is not yet covered.
  const fetchNextFinished = finished.fetchNextPage;
  useEffect(() => {
    if (
      finishedOpen &&
      finishedItems.length < finishedShown &&
      finished.hasNextPage &&
      !finished.isFetching
    ) {
      void fetchNextFinished();
    }
  }, [
    fetchNextFinished,
    finished.hasNextPage,
    finished.isFetching,
    finishedItems.length,
    finishedOpen,
    finishedShown,
  ]);

  const contractError = [counts.error, finished.error].find(
    (error) => error instanceof ModelAcquisitionContractError,
  );
  if (contractError !== undefined) throw contractError;
  if (fatalError !== undefined) throw fatalError;

  async function perform(id: string, action: () => Promise<unknown>) {
    setPendingIds((current) => new Set(current).add(id));
    setCommandErrors((current) =>
      Object.fromEntries(
        Object.entries(current).filter(([taskId]) => taskId !== id),
      ),
    );
    try {
      await action();
      await refreshLists();
    } catch (error) {
      if (error instanceof ApiError) {
        setCommandErrors((current) => ({ ...current, [id]: error }));
        if (error.status === 409) await refreshLists();
        return;
      }
      setFatalError(
        error instanceof Error ? error : new Error("download command failed"),
      );
    } finally {
      setPendingIds((current) => {
        const next = new Set(current);
        next.delete(id);
        return next;
      });
    }
  }

  function toggleFinished() {
    setFinishedOpen((open) => !open);
    setFinishedShown(FINISHED_FIRST_COUNT);
  }

  const columns = downloadColumns({
    commandErrors,
    onCancel: (item, cleanupStaging) =>
      perform(item.id, () =>
        apiClient.executeDownloadCommand(item.id, "cancel", cleanupStaging),
      ),
    onCommand: (item, command) =>
      perform(item.id, () =>
        apiClient.executeDownloadCommand(item.id, command),
      ),
    onDelete: (item, cleanupStaging) =>
      perform(item.id, () =>
        apiClient.deleteDownloadTask(item.id, cleanupStaging),
      ),
    onViewArtifacts,
    pendingIds,
    rates,
  });

  const showCounts = counts.data !== undefined;
  return (
    <>
      <span aria-live="polite" className="pw-visually-hidden">
        {politeAnnouncement}
      </span>
      <span aria-live="assertive" className="pw-visually-hidden">
        {assertiveAnnouncement}
      </span>
      <StaleNotice
        dataUpdatedAt={live.dataUpdatedAt}
        interval={DOWNLOAD_POLL_INTERVAL_MS}
        label="下载任务"
        stale={live.data !== undefined && live.isError}
      />
      <section className="pw-models-section pw-models-section--downloads">
        <SectionHeading
          count={live.data === undefined ? undefined : liveItems.length}
          refreshing={live.isFetching && !live.isPending}
          summary={
            showCounts
              ? `进行中 ${String(activeCount)} · 失败 ${String(failedCount)} · 已结束 ${String(finishedTotal)}`
              : undefined
          }
          title="下载任务"
        />
        {live.data === undefined && live.isError ? (
          <ReadError
            onRetry={() => void live.refetch()}
            title="无法获取下载任务"
          />
        ) : (
          <DataTable
            caption="下载任务"
            columns={columns}
            emptyContent={
              showCounts && totalTasks > 0 ? (
                <p className="pw-models-muted">没有进行中或失败的下载任务。</p>
              ) : (
                <EmptyState
                  description="创建下载后，任务会在这里显示进度、阶段与可用操作。"
                  icon="layers"
                  title="还没有下载任务"
                />
              )
            }
            getRowKey={({ id }) => id}
            loading={live.isPending}
            rows={rows}
          />
        )}
        {live.hasNextPage ? (
          <Button
            loading={live.isFetchingNextPage}
            loadingText="正在加载"
            onClick={() => void live.fetchNextPage()}
          >
            加载更多下载任务
          </Button>
        ) : null}
        {showCounts && finishedTotal > 0 ? (
          <div className="pw-download-finished">
            <span>
              已结束 {finishedTotal} 条（下载完成 {completedCount}、已取消{" "}
              {cancelledCount}）
            </span>
            <Button
              aria-expanded={finishedOpen}
              onClick={toggleFinished}
              size="s"
              variant="ghost"
            >
              {finishedOpen ? "收起" : "展开"}
            </Button>
          </div>
        ) : null}
        {finishedOpen && finished.data === undefined && finished.isPending ? (
          <span className="pw-models-muted" role="status">
            正在加载已结束的下载任务
          </span>
        ) : null}
        {finishedOpen && finished.data === undefined && finished.isError ? (
          <ReadError
            onRetry={() => void finished.refetch()}
            title="无法获取已结束的下载任务"
          />
        ) : null}
        {finishedOpen && remainingFinished > 0 ? (
          <div className="pw-download-finished">
            <Button
              loading={finished.isFetchingNextPage}
              loadingText="正在加载"
              onClick={() => {
                setFinishedShown((shown) => shown + FINISHED_MORE_COUNT);
              }}
              size="s"
              variant="ghost"
            >
              {`显示更多（还有 ${String(remainingFinished)} 条）`}
            </Button>
          </div>
        ) : null}
      </section>
    </>
  );
}

function countOf(
  counts: Partial<Record<DownloadTaskStatus, number>> | undefined,
  status: DownloadTaskStatus,
) {
  return counts === undefined ? 0 : (counts[status] ?? 0);
}

interface DownloadColumnOptions {
  commandErrors: Record<string, ApiError>;
  onCancel: (item: DownloadTaskItem, cleanupStaging: boolean) => Promise<void>;
  onCommand: (
    item: DownloadTaskItem,
    command: DownloadCommand,
  ) => Promise<void>;
  onDelete: (item: DownloadTaskItem, cleanupStaging: boolean) => Promise<void>;
  onViewArtifacts: (repositoryId: string) => void;
  pendingIds: ReadonlySet<string>;
  rates: ReadonlyMap<string, DownloadRate>;
}

function downloadColumns(
  options: DownloadColumnOptions,
): TableColumn<DownloadTaskItem>[] {
  return [
    {
      header: "模型 · 来源 · Revision",
      id: "model",
      render: (item) => (
        <span className="pw-models-name">
          <strong title={item.repository_id}>{item.repository_id}</strong>
          <span>
            {originTypeLabel(item.source)} ·{" "}
            {item.source === "Local Path" ? (
              <span
                className="pw-models-technical"
                title="外部目录没有 Revision"
              >
                —
              </span>
            ) : item.revision === null ? (
              <span className="pw-models-technical">未解析</span>
            ) : (
              <span className="pw-models-technical" title={item.revision}>
                {shortRevision(item.revision)}
              </span>
            )}
          </span>
          {item.staging_path === null ? null : (
            <span>临时文件 {formatBytes(item.downloaded_bytes)}</span>
          )}
        </span>
      ),
    },
    {
      header: "状态 / 进度",
      id: "status",
      render: (item) => (
        <DownloadStatusCell item={item} rate={options.rates.get(item.id)} />
      ),
    },
    {
      header: "更新时间",
      id: "updated",
      render: (item) => (
        <time className="pw-models-technical" dateTime={item.updated_at}>
          {formatObservedAt(item.updated_at)}
        </time>
      ),
    },
    {
      header: "操作",
      id: "actions",
      render: (item) => {
        const commandError = options.commandErrors[item.id];
        return (
          <span className="pw-models-row-actions">
            <span>
              <DownloadActions
                disabled={options.pendingIds.has(item.id)}
                item={item}
                options={options}
              />
            </span>
            {commandError === undefined ? null : (
              <CommandError error={commandError} />
            )}
          </span>
        );
      },
    },
  ];
}

const COMMAND_ORDER = ["pause", "resume", "cancel"] as const;

function DownloadActions({
  disabled,
  item,
  options,
}: {
  disabled: boolean;
  item: DownloadTaskItem;
  options: DownloadColumnOptions;
}) {
  // "retry" (重新下载) stays a backend command but has no UI entry this round.
  const commands = COMMAND_ORDER.filter((command) =>
    item.available_commands.includes(command),
  );
  const pieces: ReactNode[] = [];
  if (item.status === "Completed") {
    pieces.push(
      <Button
        key="view"
        onClick={() => {
          options.onViewArtifacts(item.repository_id);
        }}
        size="s"
        variant="ghost"
      >
        查看本地模型
      </Button>,
    );
  }
  for (const command of commands) {
    if (command === "cancel") {
      pieces.push(
        <CancelTaskDialog
          disabled={disabled}
          item={item}
          key="cancel"
          onConfirm={(cleanup) => options.onCancel(item, cleanup)}
        />,
      );
    } else {
      pieces.push(
        <Button
          disabled={disabled}
          key={command}
          onClick={() => void options.onCommand(item, command)}
          size="s"
          variant="ghost"
        >
          {command === "pause" ? "暂停" : "继续"}
        </Button>,
      );
    }
  }
  if (item.status === "Publishing") {
    pieces.push(<PublishingCancel key="publishing-cancel" />);
  }
  if (["Completed", "Failed", "Cancelled"].includes(item.status)) {
    pieces.push(
      <DeleteTaskDialog
        disabled={disabled}
        item={item}
        key="delete"
        onConfirm={(cleanup) => options.onDelete(item, cleanup)}
      />,
    );
  }
  return <>{pieces}</>;
}

function PublishingCancel() {
  const hintId = useId();
  return (
    <>
      <Button
        aria-describedby={hintId}
        disabled
        size="s"
        title="即将完成，不能取消"
        variant="ghost"
      >
        取消
      </Button>
      <span className="pw-visually-hidden" id={hintId}>
        即将完成，不能取消
      </span>
    </>
  );
}

function CancelTaskDialog({
  disabled,
  item,
  onConfirm,
}: {
  disabled: boolean;
  item: DownloadTaskItem;
  onConfirm: (cleanupStaging: boolean) => Promise<void>;
}) {
  const hasStaging = item.staging_path !== null;
  return (
    <TaskConfirmDialog
      checkbox={
        hasStaging
          ? {
              defaultChecked: true,
              label: `同时清理临时文件（${formatBytes(item.downloaded_bytes)}）`,
            }
          : undefined
      }
      confirmLabel="确认取消"
      description="任务变为「已取消」，任务记录保留 30 天。已发布的本地模型不受影响。"
      disabled={disabled}
      note={(checked) =>
        !hasStaging ? null : checked ? (
          <p className="pw-models-dialog-note">
            将清理临时文件，之后不能从当前临时文件继续。
          </p>
        ) : (
          <p className="pw-models-dialog-note">
            临时文件会留在服务器上，需要时可在删除任务时一并清理。
          </p>
        )
      }
      // Without a temporary directory the box is hidden; keep the default (clean up).
      onConfirm={(checked) => onConfirm(hasStaging ? checked : true)}
      returnLabel="返回"
      title={`取消下载「${item.repository_id}」？`}
      triggerLabel="取消"
    />
  );
}

function DeleteTaskDialog({
  disabled,
  item,
  onConfirm,
}: {
  disabled: boolean;
  item: DownloadTaskItem;
  onConfirm: (cleanupStaging: boolean) => Promise<void>;
}) {
  return (
    <TaskConfirmDialog
      checkbox={
        item.staging_path === null
          ? undefined
          : {
              defaultChecked: false,
              label: `同时删除临时文件（${formatBytes(item.downloaded_bytes)}）`,
            }
      }
      confirmLabel="确认删除任务"
      description={
        item.status === "Completed"
          ? "只删除任务记录，已生成的本地模型不受影响。"
          : "只删除任务记录，不会删除任何已发布的本地模型。"
      }
      disabled={disabled}
      onConfirm={onConfirm}
      returnLabel="返回"
      title={`删除下载任务「${item.repository_id}」？`}
      triggerLabel="删除任务"
    />
  );
}

function TaskConfirmDialog({
  checkbox,
  confirmLabel,
  description,
  disabled,
  note,
  onConfirm,
  returnLabel,
  title,
  triggerLabel,
}: {
  checkbox?: { defaultChecked: boolean; label: string };
  confirmLabel: string;
  description: string;
  disabled: boolean;
  note?: (checked: boolean) => ReactNode;
  onConfirm: (checked: boolean) => Promise<void>;
  returnLabel: string;
  title: string;
  triggerLabel: string;
}) {
  const [open, setOpen] = useState(false);
  const [checked, setChecked] = useState(checkbox?.defaultChecked ?? false);
  return (
    <Modal
      description={description}
      footer={
        <>
          <Button
            disabled={disabled}
            onClick={() => {
              setOpen(false);
            }}
          >
            {returnLabel}
          </Button>
          <Button
            disabled={disabled}
            onClick={() => {
              void onConfirm(checked).then(() => {
                setOpen(false);
              });
            }}
            variant="danger"
          >
            {confirmLabel}
          </Button>
        </>
      }
      onOpenChange={(next) => {
        // Every opening starts from the operation default (cancel: clean up, delete: keep).
        if (next) setChecked(checkbox?.defaultChecked ?? false);
        setOpen(next);
      }}
      open={open}
      size="s"
      title={title}
      trigger={
        <Button disabled={disabled} size="s" variant="ghost">
          {triggerLabel}
        </Button>
      }
    >
      {checkbox === undefined ? null : (
        <label className="pw-models-dialog-check">
          <input
            checked={checked}
            onChange={(event) => {
              setChecked(event.currentTarget.checked);
            }}
            type="checkbox"
          />
          <span>{checkbox.label}</span>
        </label>
      )}
      {note?.(checked)}
    </Modal>
  );
}

function DownloadStatusCell({
  item,
  rate,
}: {
  item: DownloadTaskItem;
  rate: DownloadRate | undefined;
}) {
  const status = downloadStatusDisplay(item.status);
  const showProgress =
    item.status === "Downloading" || item.status === "Paused";
  const percentage =
    item.total_bytes === null
      ? null
      : item.total_bytes === 0
        ? 0
        : Math.min(100, (item.downloaded_bytes / item.total_bytes) * 100);
  const stageNote = stageDescription(item);
  return (
    <span className="pw-download-state">
      <StatusBadge icon={status.icon} text={status.label} tone={status.tone} />
      {stageNote === null ? null : (
        <span className="pw-download-note">{stageNote}</span>
      )}
      {showProgress ? (
        <span className="pw-download-progress">
          <span
            aria-label={
              percentage === null
                ? "不确定进度"
                : `下载进度 ${String(Math.round(percentage))}%`
            }
            aria-valuemax={percentage === null ? undefined : 100}
            aria-valuemin={percentage === null ? undefined : 0}
            aria-valuenow={
              percentage === null ? undefined : Math.round(percentage)
            }
            className="pw-download-progress__track"
            role="progressbar"
          >
            <span
              className={
                percentage === null
                  ? "pw-download-progress__fill is-indeterminate"
                  : "pw-download-progress__fill"
              }
              style={
                percentage === null
                  ? undefined
                  : { inlineSize: `${String(percentage)}%` }
              }
            />
          </span>
          <span>{formatDownloadProgress(item)}</span>
        </span>
      ) : null}
      {item.status === "Downloading" ? (
        <span className="pw-download-note">
          {rate === undefined ? (
            "速度与剩余时间计算中"
          ) : (
            <>
              <span>{`${formatBytes(rate.bytesPerSecond)}/s`}</span>
              {" · "}
              {rate.etaSeconds === null ? (
                "剩余时间未知"
              ) : (
                <>
                  剩余 <span>{formatDuration(rate.etaSeconds)}</span>
                </>
              )}
            </>
          )}
        </span>
      ) : null}
      {item.status === "Failed" ? <DownloadFailure item={item} /> : null}
    </span>
  );
}

function stageDescription(item: DownloadTaskItem) {
  switch (item.status) {
    case "Queued":
      return "等待开始下载";
    case "Resolving":
      return "正在读取文件清单与预计大小";
    case "Verifying":
      return item.source === "Local Path"
        ? `正在校验服务器目录中的${item.total_bytes === null ? "" : ` ${formatBytes(item.total_bytes)} `}文件（不经下载）`
        : "正在校验文件完整性";
    case "Publishing":
      return "校验通过，正在登记为本地模型，即将完成";
    case "Completed":
      return "已生成本地模型";
    case "Cancelled":
      return `取消时已下载 ${formatBytes(item.downloaded_bytes)}`;
    default:
      return null;
  }
}

function formatDownloadProgress(item: DownloadTaskItem) {
  if (item.total_bytes === null)
    return `不确定进度 · 已下载 ${formatBytes(item.downloaded_bytes)}`;
  const percentage =
    item.total_bytes === 0
      ? 0
      : Math.min(100, (item.downloaded_bytes / item.total_bytes) * 100);
  return `${String(Math.round(percentage))}% · ${formatBytes(item.downloaded_bytes)} / ${formatBytes(item.total_bytes)}`;
}

function DownloadFailure({ item }: { item: DownloadTaskItem }) {
  if (item.error_code === "external_path_unreadable") {
    return (
      <span className="pw-download-failure">
        校验失败：运行环境无法读取这个目录。请在服务器上调整目录权限后点「继续」重新校验。
      </span>
    );
  }
  if (item.error_code === null) {
    return <MissingValue kind="unavailable" reason="下载失败原因暂不可用" />;
  }
  const failure = downloadFailurePresentation(
    item.error_code,
    item.failure_stage,
  );
  return (
    <span className="pw-download-failure">
      <strong>{failure.title}</strong>
      <span>{failure.detail}</span>
      {item.error_code === "download_process_failed" ? (
        <Link to="/system?tab=model-sources">前往「系统 › 模型源」</Link>
      ) : null}
      {item.failure_stage === null ? null : (
        <span>失败阶段：{failureStageLabel(item.failure_stage)}</span>
      )}
      <span>已完成大小：{formatBytes(item.downloaded_bytes)}</span>
    </span>
  );
}

function CommandError({ error }: { error: ApiError }) {
  return (
    <InlineAlert title="操作未完成" tone="negative">
      {error.responseMessage ?? "下载任务未改变，请刷新状态后重试。"}
      {error.requestId === undefined ? null : (
        <span className="pw-models-request-id" title={error.requestId}>
          Request ID：{error.requestId}
        </span>
      )}
    </InlineAlert>
  );
}
