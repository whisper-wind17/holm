import * as Dialog from "@radix-ui/react-dialog";
import { useMemo, useRef, useState, type MouseEvent } from "react";

import type { ApiClient } from "../api/client";
import type {
  RequestRecord,
  RequestRecordFilters,
  RequestRecordStatus,
} from "../api/request-record-contracts";
import {
  Button,
  DataTable,
  EmptyState,
  InlineAlert,
  StatusBadge,
  TextField,
  TruncatedValue,
  type IconName,
  type StatusTone,
  type TableColumn,
} from "../components";
import { formatObservedAt } from "../system/formatting";
import { useAiServices } from "../ai-services/queries";
import { useModelInstances } from "../model-instances/queries";
import { useUsers } from "../users/queries";
import { useRequestRecordKeyOptions, useRequestRecords } from "./queries";

const STATUS_OPTIONS: ReadonlyArray<{
  label: string;
  value: RequestRecordStatus;
}> = [
  { label: "已接收", value: "received" },
  { label: "进行中", value: "in_progress" },
  { label: "已完成", value: "completed" },
  { label: "失败", value: "failed" },
  { label: "已中止", value: "aborted" },
  { label: "已拒绝", value: "rejected" },
  { label: "状态未知", value: "unknown" },
];

type ErrorCode = NonNullable<RequestRecordFilters["errorCode"]>;
// frozen Gateway error codes. The contract enum is the source of
// truth; the check below stops type-checking when the two drift apart.
const ERROR_CODES = [
  "invalid_request",
  "invalid_api_key",
  "api_key_expired",
  "service_not_authorized",
  "model_not_found",
  "rate_limit_exceeded",
  "service_disabled",
  "backend_unavailable",
  "gateway_overloaded",
  "usage_storage_unavailable",
  "control_plane_unavailable",
  "inference_timeout",
] as const satisfies readonly ErrorCode[];
const ERROR_CODES_COMPLETE: Exclude<
  ErrorCode,
  (typeof ERROR_CODES)[number]
> extends never
  ? true
  : never = true;
void ERROR_CODES_COMPLETE;

type TimeRange = "1h" | "today" | "7d" | "30d";
const TIME_OPTIONS: ReadonlyArray<{ label: string; value: TimeRange }> = [
  { label: "近 1 小时", value: "1h" },
  { label: "今日", value: "today" },
  { label: "近 7 天", value: "7d" },
  { label: "近 30 天", value: "30d" },
];
const HOUR_MS = 3_600_000;
const DAY_MS = 24 * HOUR_MS;
// Default detail retention; the API key options cover the same span.
const RETENTION_DAYS = 30;

const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

interface Period {
  from: string;
  to?: string;
}

// Presets are anchored when picked (never per render): a moving `from` would
// change the query key on every render.
function periodFor(range: TimeRange, now = new Date()): Period {
  switch (range) {
    case "1h":
      return { from: new Date(now.getTime() - HOUR_MS).toISOString() };
    case "today":
      return {
        from: new Date(
          now.getFullYear(),
          now.getMonth(),
          now.getDate(),
        ).toISOString(),
      };
    case "7d":
      return { from: new Date(now.getTime() - 7 * DAY_MS).toISOString() };
    case "30d":
      return { from: new Date(now.getTime() - 30 * DAY_MS).toISOString() };
  }
}

export function RequestRecordsPanel({
  apiClient,
  lockedService,
  initialFilters,
}: {
  apiClient: ApiClient;
  lockedService?: { id: string; label: string };
  initialFilters?: Pick<
    RequestRecordFilters,
    "errorCode" | "status" | "from" | "to"
  >;
}) {
  const [serviceId, setServiceId] = useState("");
  const [userId, setUserId] = useState("");
  const [apiKeyId, setApiKeyId] = useState("");
  const [errorCode, setErrorCode] = useState<ErrorCode | "">(
    initialFilters?.errorCode ?? "",
  );
  const [status, setStatus] = useState<RequestRecordStatus | "">(
    initialFilters?.status ?? "",
  );
  // An explicit window from the caller (recent errors: the last 7 days)
  // wins over the default "today"; the select then reads "近 7 天".
  const [range, setRange] = useState<TimeRange>(
    initialFilters?.from === undefined ? "today" : "7d",
  );
  const [period, setPeriod] = useState<Period>(() =>
    initialFilters?.from === undefined
      ? periodFor("today")
      : {
          from: initialFilters.from,
          ...(initialFilters.to === undefined ? {} : { to: initialFilters.to }),
        },
  );
  const [requestId, setRequestId] = useState("");
  const [requestIdInput, setRequestIdInput] = useState("");
  const [cursor, setCursor] = useState<string>();
  const [cursorHistory, setCursorHistory] = useState<Array<string | undefined>>(
    [],
  );
  const [selected, setSelected] = useState<RequestRecord>();
  const opener = useRef<HTMLElement | null>(null);
  const [keyWindow] = useState(() => {
    const now = new Date();
    return {
      from: new Date(now.getTime() - RETENTION_DAYS * DAY_MS).toISOString(),
      timeZone: Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC",
      to: now.toISOString(),
    };
  });
  const effectiveServiceId = lockedService?.id ?? (serviceId || undefined);
  const services = useAiServices(apiClient);
  const instances = useModelInstances(apiClient);
  const users = useUsers(apiClient);
  const keyOptions = useRequestRecordKeyOptions(
    apiClient,
    effectiveServiceId,
    keyWindow,
  );
  const filters: RequestRecordFilters = {
    ...(apiKeyId === "" ? {} : { apiKeyId }),
    ...(cursor === undefined ? {} : { cursor }),
    ...(errorCode === "" ? {} : { errorCode }),
    ...period,
    ...(requestId === "" ? {} : { requestId }),
    ...(effectiveServiceId === undefined
      ? {}
      : { serviceId: effectiveServiceId }),
    ...(status === "" ? {} : { status }),
    ...(userId === "" ? {} : { userId }),
  };
  const records = useRequestRecords(apiClient, filters);
  const rows = useMemo(() => records.data?.items ?? [], [records.data]);
  const instanceNames = useMemo(
    () =>
      new Map(
        (instances.data?.items ?? []).map((item) => [item.id, item.name]),
      ),
    [instances.data],
  );
  const showService = lockedService === undefined;
  const columns = useMemo(
    () =>
      requestRecordColumns({
        instanceNames,
        onOpen: (record, element) => {
          opener.current = element;
          setSelected(record);
        },
        showService,
      }),
    [instanceNames, showService],
  );

  function resetPage() {
    setCursor(undefined);
    setCursorHistory([]);
  }

  // DataTable has no row hook, so clicks are delegated: the whole row opens the
  // detail while buttons inside a row (copy, the time button) keep their own
  // behavior. The time button is the keyboard and screen-reader path.
  function openFromRow(event: MouseEvent<HTMLDivElement>) {
    if (!(event.target instanceof Element)) return;
    if (event.target.closest("button, a") !== null) return;
    const row = event.target.closest("tbody > tr");
    const body = row?.parentElement;
    if (row == null || body == null) return;
    const record = rows[Array.prototype.indexOf.call(body.children, row)];
    if (record === undefined) return;
    opener.current = row.querySelector("button");
    setSelected(record);
  }

  return (
    <section className="pw-request-records">
      <div className="pw-request-records__filters" aria-label="调用记录筛选">
        <label className="pw-request-records__select">
          <span>时间</span>
          <select
            onChange={(event) => {
              const next = event.currentTarget.value as TimeRange;
              setRange(next);
              setPeriod(periodFor(next));
              resetPage();
            }}
            value={range}
          >
            {TIME_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </label>
        {lockedService === undefined ? (
          <label className="pw-request-records__select">
            <span>服务</span>
            <select
              onChange={(event) => {
                setServiceId(event.currentTarget.value);
                // Key options are scoped to the service; a stale key would
                // silently filter every row out.
                setApiKeyId("");
                resetPage();
              }}
              value={serviceId}
            >
              <option value="">全部服务</option>
              {(services.data?.items ?? []).map((service) => (
                <option key={service.id} value={service.id}>
                  {service.name} · {service.public_model_name}
                </option>
              ))}
            </select>
          </label>
        ) : null}
        <label className="pw-request-records__select">
          <span>状态</span>
          <select
            onChange={(event) => {
              setStatus(event.currentTarget.value as RequestRecordStatus | "");
              resetPage();
            }}
            value={status}
          >
            <option value="">全部状态</option>
            {STATUS_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </label>
        <label className="pw-request-records__select">
          <span>错误码</span>
          <select
            onChange={(event) => {
              setErrorCode(event.currentTarget.value as ErrorCode | "");
              resetPage();
            }}
            value={errorCode}
          >
            <option value="">全部错误码</option>
            {ERROR_CODES.map((code) => (
              <option key={code} value={code}>
                {code}
              </option>
            ))}
          </select>
        </label>
        <label className="pw-request-records__select">
          <span>用户</span>
          <select
            onChange={(event) => {
              setUserId(event.currentTarget.value);
              resetPage();
            }}
            value={userId}
          >
            <option value="">全部用户</option>
            {(users.data?.items ?? []).map((user) => (
              <option key={user.id} value={user.id}>
                {user.username}
                {user.status === "disabled" ? "（已禁用）" : ""}
              </option>
            ))}
          </select>
        </label>
        <label className="pw-request-records__select">
          <span>API 密钥</span>
          <select
            onChange={(event) => {
              setApiKeyId(event.currentTarget.value);
              resetPage();
            }}
            value={apiKeyId}
          >
            <option value="">全部密钥</option>
            {(keyOptions.data ?? []).map((option) => (
              <option key={option.id} value={option.id}>
                {option.label}
              </option>
            ))}
          </select>
        </label>
        <TextField
          label="Request ID"
          onChange={(event) => {
            const value = event.currentTarget.value;
            setRequestIdInput(value);
            const normalized = value.trim();
            if (normalized === "" || UUID_PATTERN.test(normalized)) {
              setRequestId(normalized);
              resetPage();
            }
          }}
          placeholder="输入完整 Request ID"
          value={requestIdInput}
        />
      </div>
      {records.data === undefined && records.isError ? (
        <InlineAlert title="无法获取调用记录" tone="negative">
          调用记录列表无法显示。请检查平台连接后重试。
          <Button onClick={() => void records.refetch()} size="s">
            重试
          </Button>
        </InlineAlert>
      ) : (
        <>
          <div className="pw-request-records__table" onClick={openFromRow}>
            <DataTable
              caption="调用记录"
              columns={columns}
              emptyContent={
                <EmptyState
                  description="调整时间或筛选条件，或在调用 AI 服务后刷新。"
                  icon="apiServices"
                  title="还没有调用记录"
                />
              }
              getRowKey={(row) => row.request_id}
              loading={records.isPending}
              rows={rows}
            />
          </div>
          <div className="pw-request-records__pagination">
            <Button
              disabled={cursorHistory.length === 0}
              onClick={() => {
                const history = [...cursorHistory];
                setCursor(history.pop());
                setCursorHistory(history);
              }}
              size="s"
            >
              上一页
            </Button>
            <Button
              disabled={records.data?.next_cursor == null}
              onClick={() => {
                if (records.data?.next_cursor == null) return;
                setCursorHistory((history) => [...history, cursor]);
                setCursor(records.data.next_cursor);
              }}
              size="s"
            >
              下一页
            </Button>
          </div>
        </>
      )}
      <p className="pw-request-records__note">
        调用明细保留 30
        天（管理员可调长），更早的调用只保留在用量统计里。只记录调用元数据，不保存
        Prompt、上传内容或模型生成的内容。
      </p>
      <RequestRecordDetail
        instanceName={
          selected?.instance_id == null
            ? undefined
            : instanceNames.get(selected.instance_id)
        }
        onClose={() => {
          setSelected(undefined);
        }}
        onCloseAutoFocus={() => {
          if (opener.current?.isConnected === true) opener.current.focus();
        }}
        record={selected}
      />
    </section>
  );
}

function requestRecordColumns({
  instanceNames,
  onOpen,
  showService,
}: {
  instanceNames: ReadonlyMap<string, string>;
  onOpen: (record: RequestRecord, element: HTMLElement) => void;
  showService: boolean;
}): TableColumn<RequestRecord>[] {
  return [
    {
      header: "请求时间",
      id: "time",
      render: (record) => (
        <Button
          className="pw-request-records__open"
          onClick={(event) => {
            event.stopPropagation();
            onOpen(record, event.currentTarget);
          }}
          size="s"
          variant="ghost"
        >
          {formatObservedAt(record.received_at)}
          <span className="pw-visually-hidden">，查看详情</span>
        </Button>
      ),
    },
    {
      header: "用户",
      id: "user",
      render: (record) => record.username ?? "—",
    },
    ...(showService
      ? [
          {
            header: "服务",
            id: "service",
            render: (record: RequestRecord) => record.public_model_name ?? "—",
          },
        ]
      : []),
    {
      header: "API 密钥",
      id: "api-key",
      render: (record) => apiKeyLabel(record),
    },
    {
      header: "状态",
      id: "status",
      render: (record) => (
        <span className="pw-request-records__status">
          <RequestStatus status={record.status} />
          {record.error_code === null ? null : <code>{record.error_code}</code>}
        </span>
      ),
    },
    {
      header: "Input Tokens",
      id: "input-tokens",
      numeric: true,
      render: (record) => record.input_tokens ?? "—",
    },
    {
      header: "Output Tokens",
      id: "output-tokens",
      numeric: true,
      render: (record) => record.output_tokens ?? "—",
    },
    {
      header: "TTFT",
      id: "ttft",
      numeric: true,
      render: (record) => formatMs(record.ttft_ms),
    },
    {
      header: "端到端耗时",
      id: "duration",
      numeric: true,
      render: (record) => formatMs(record.duration_ms),
    },
    {
      header: "后端实例",
      id: "instance",
      render: (record) =>
        record.instance_id === null
          ? "—"
          : (instanceNames.get(record.instance_id) ?? "—"),
    },
    {
      accessibleValue: (record) => record.request_id,
      header: "Request ID",
      id: "request-id",
      render: (record) => (
        <TruncatedValue copyLabel="复制 Request ID" value={record.request_id} />
      ),
      truncate: true,
    },
  ];
}

function formatMs(value: number | null) {
  return value === null ? "—" : `${String(value)} ms`;
}

// The name first; a key without a name falls back to prefix + last 4.
function apiKeyLabel(record: RequestRecord) {
  if (record.api_key_name !== null) return record.api_key_name;
  if (record.api_key_display_prefix !== null && record.api_key_last4 !== null)
    return `${record.api_key_display_prefix}…${record.api_key_last4}`;
  return "—";
}

const STATUS_PRESENTATIONS: Readonly<
  Record<
    RequestRecordStatus,
    { icon: IconName; label: string; tone: StatusTone }
  >
> = {
  aborted: { icon: "pause", label: "已中止", tone: "idle" },
  completed: { icon: "success", label: "已完成", tone: "positive" },
  failed: { icon: "error", label: "失败", tone: "negative" },
  in_progress: { icon: "loading", label: "进行中", tone: "info" },
  received: { icon: "info", label: "已接收", tone: "info" },
  rejected: { icon: "notice", label: "已拒绝", tone: "notice" },
  unknown: { icon: "notice", label: "状态未知", tone: "notice" },
};

function RequestStatus({ status }: { status: RequestRecordStatus }) {
  const presentation = STATUS_PRESENTATIONS[status];
  return (
    <StatusBadge
      icon={presentation.icon}
      text={presentation.label}
      tone={presentation.tone}
    />
  );
}

function ledgerIntegrity(status: RequestRecordStatus) {
  if (status === "unknown") return "未知：平台重启前未收到最终结果";
  if (status === "received" || status === "in_progress") return "进行中";
  return "完整";
}

// Metadata only (AGENTS 5): no field here carries request or response text.
function RequestRecordDetail({
  instanceName,
  onClose,
  onCloseAutoFocus,
  record,
}: {
  instanceName: string | undefined;
  onClose: () => void;
  onCloseAutoFocus: () => void;
  record: RequestRecord | undefined;
}) {
  const titleRef = useRef<HTMLHeadingElement>(null);
  return (
    <Dialog.Root
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
      open={record !== undefined}
    >
      <Dialog.Portal>
        <Dialog.Overlay className="pw-drawer-overlay" />
        <Dialog.Content
          aria-describedby={undefined}
          className="pw-drawer pw-request-detail"
          onCloseAutoFocus={(event) => {
            event.preventDefault();
            onCloseAutoFocus();
          }}
          onOpenAutoFocus={(event) => {
            event.preventDefault();
            titleRef.current?.focus();
          }}
        >
          <header className="pw-dialog__header">
            <div>
              <Dialog.Title asChild>
                <h2 ref={titleRef} tabIndex={-1}>
                  调用详情
                </h2>
              </Dialog.Title>
            </div>
            <Dialog.Close asChild>
              <Button
                aria-label="关闭详情"
                icon="close"
                size="s"
                variant="ghost"
              >
                关闭
              </Button>
            </Dialog.Close>
          </header>
          {record === undefined ? null : (
            <div className="pw-dialog__body">
              {record.status === "unknown" ? (
                <InlineAlert title="状态未知" tone="notice">
                  平台重启前未收到最终结果。这次调用的用量可能不完整，不计为成功或失败。
                </InlineAlert>
              ) : null}
              <dl className="pw-request-detail__facts">
                <div>
                  <dt>Request ID</dt>
                  <dd>
                    <code>{record.request_id}</code>
                  </dd>
                </div>
                <div>
                  <dt>状态</dt>
                  <dd>
                    <RequestStatus status={record.status} />
                  </dd>
                </div>
                <div>
                  <dt>接收时间</dt>
                  <dd>{formatObservedAt(record.received_at)}</dd>
                </div>
                <div>
                  <dt>完成时间</dt>
                  <dd>
                    {record.completed_at === null
                      ? "—"
                      : formatObservedAt(record.completed_at)}
                  </dd>
                </div>
                <div>
                  <dt>HTTP 状态</dt>
                  <dd>{record.http_status ?? "—"}</dd>
                </div>
                <div>
                  <dt>错误码</dt>
                  <dd>
                    {record.error_code === null ? (
                      "—"
                    ) : (
                      <code>{record.error_code}</code>
                    )}
                  </dd>
                </div>
                <div>
                  <dt>用户</dt>
                  <dd>{record.username ?? "—"}</dd>
                </div>
                <div>
                  <dt>API 密钥</dt>
                  <dd>{apiKeyLabel(record)}</dd>
                </div>
                <div>
                  <dt>服务</dt>
                  <dd>{record.public_model_name ?? "—"}</dd>
                </div>
                <div>
                  <dt>后端实例</dt>
                  <dd>{instanceName ?? "—"}</dd>
                </div>
                <div>
                  <dt>流式</dt>
                  <dd>
                    {record.stream === null ? "—" : record.stream ? "是" : "否"}
                  </dd>
                </div>
                <div>
                  <dt>Finish Reason</dt>
                  <dd>{record.finish_reason ?? "—"}</dd>
                </div>
                <div>
                  <dt>Tokens</dt>
                  <dd>
                    Input {record.input_tokens ?? "—"} · Output{" "}
                    {record.output_tokens ?? "—"}
                  </dd>
                </div>
                <div>
                  <dt>TTFT</dt>
                  <dd>{formatMs(record.ttft_ms)}</dd>
                </div>
                <div>
                  <dt>Queue Time</dt>
                  <dd>当前 Runtime 不支持</dd>
                </div>
                <div>
                  <dt>端到端耗时</dt>
                  <dd>{formatMs(record.duration_ms)}</dd>
                </div>
                <div>
                  <dt>客户端中断</dt>
                  <dd>
                    {record.status === "aborted"
                      ? "是（客户端断开连接）"
                      : "否"}
                  </dd>
                </div>
                <div>
                  <dt>账本完整性</dt>
                  <dd>{ledgerIntegrity(record.status)}</dd>
                </div>
              </dl>
            </div>
          )}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
