import {
  ServiceMetric,
  ServiceWindowMetric,
  useServiceListMetrics,
} from "./service-metrics";
import {
  useMutation,
  useQueries,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router";

import type {
  AiService,
  AiServicePrimaryInstanceStatus,
  AiServiceRuntimeStatus,
} from "../api/ai-service-contracts";
import { ApiError, type ApiClient } from "../api/client";
import type { ModelInstance } from "../api/model-instance-contracts";
import type {
  InstanceTelemetry,
  ServiceTelemetry,
} from "../api/telemetry-contracts";
import {
  Button,
  DataTable,
  CopyControl,
  EmptyState,
  InlineAlert,
  Modal,
  MissingValue,
  StatusBadge,
  Tabs,
  TextField,
  type TableColumn,
} from "../components";
import { useSession } from "../auth/session";
import { RequestRecordsPanel } from "../request-records/request-records-panel";
import { AI_SERVICES_QUERY_KEY, useAiServices } from "./queries";
import {
  MODEL_INSTANCES_QUERY_KEY,
  useModelInstances,
} from "../model-instances/queries";
import { PersonalAiServicesPage } from "./personal-service-page";
import { servicePermissionsQueryKey } from "../users/service-permissions";
import { CreateServiceModal } from "./create-service-modal";

function runtimeStatusPresentation(status: AiServiceRuntimeStatus) {
  switch (status) {
    case "Available":
      return { icon: "success", text: "可用", tone: "positive" } as const;
    case "Degraded":
      return { icon: "notice", text: "启动中", tone: "notice" } as const;
    case "Unavailable":
      return { icon: "error", text: "不可用", tone: "negative" } as const;
    case "Disabled":
      return { icon: "pause", text: "已停用", tone: "idle" } as const;
  }
}

export function primaryInstanceStatusLabel(
  status: AiServicePrimaryInstanceStatus | null,
) {
  switch (status) {
    case "WaitingForResource":
      return "等待资源";
    case "Starting":
      return "启动中";
    case "Running":
      return "运行中";
    case "Restarting":
      return "重启中";
    case "Unhealthy":
      return "运行异常";
    case "Stopping":
      return "停止中";
    case "Stopped":
      return "已停止";
    case "Failed":
      return "运行失败";
    case null:
      return "实例缺失";
  }
}

export function AiServiceStatusBadge({
  size,
  status,
}: {
  size?: "default" | "large";
  status: AiServiceRuntimeStatus;
}) {
  return <StatusBadge {...runtimeStatusPresentation(status)} size={size} />;
}

export function AiServicesPage({ apiClient }: { apiClient: ApiClient }) {
  const me = useSession().me;
  if (me?.role === "user") {
    return <PersonalAiServicesPage apiClient={apiClient} />;
  }
  return <AdminAiServicesPage apiClient={apiClient} />;
}

function AdminAiServicesPage({ apiClient }: { apiClient: ApiClient }) {
  const [searchParams, setSearchParams] = useSearchParams();
  const heading = useRef<HTMLHeadingElement>(null);
  const queryClient = useQueryClient();
  const session = useSession();
  const services = useAiServices(apiClient);
  const instances = useModelInstances(apiClient);
  const [commandError, setCommandError] = useState<ApiError>();
  const [statusFilter, setStatusFilter] = useState<
    "all" | "attention" | "available" | "disabled"
  >("all");
  const [search, setSearch] = useState("");
  const [deleted, setDeleted] = useState<AiService>();
  const now = new Date();
  const todayStart = new Date(
    now.getFullYear(),
    now.getMonth(),
    now.getDate(),
  ).toISOString();
  const canViewUsage =
    session.me?.role === "admin" &&
    session.me.capabilities.some(
      ({ capability }) => capability === "view_usage",
    );
  const todayUsage = useQuery({
    enabled: canViewUsage,
    queryFn: () =>
      apiClient.getUsage({
        from: todayStart,
        to: new Date().toISOString(),
        groupBy: "service",
        timeZone: Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC",
      }),
    queryKey: ["service-list-today-usage", todayStart],
    refetchInterval: 30_000,
  });

  useEffect(() => heading.current?.focus(), []);

  const instanceById = useMemo(
    () => new Map((instances.data?.items ?? []).map((item) => [item.id, item])),
    [instances.data],
  );
  const toggle = useMutation({
    mutationFn: ({ enabled, id }: { enabled: boolean; id: string }) =>
      apiClient.updateAiService(id, { enabled }),
  });

  async function toggleService(service: AiService) {
    setCommandError(undefined);
    try {
      await toggle.mutateAsync({ enabled: !service.enabled, id: service.id });
      await queryClient.invalidateQueries({ queryKey: AI_SERVICES_QUERY_KEY });
    } catch (error) {
      if (error instanceof ApiError) setCommandError(error);
      else throw error;
    }
  }

  const rows = services.data?.items ?? [];
  const permissionQueries = useQueries({
    queries: rows.map(({ id }) => ({
      queryFn: () => apiClient.getServicePermissions(id),
      queryKey: servicePermissionsQueryKey(id),
    })),
  });
  const permissionCounts = new Map<string, number>();
  rows.forEach((row, index) => {
    const result = permissionQueries[index]?.data;
    if (result !== undefined)
      permissionCounts.set(row.id, result.user_ids.length);
  });
  const {
    telemetryByInstanceId,
    telemetryByServiceId,
    telemetryQueries,
    serviceTelemetryQueries,
  } = useServiceListMetrics(apiClient, rows);
  const monitorView = searchParams.get("view") === "monitor";
  const matchesStatus = (service: AiService, filter = statusFilter) =>
    filter === "all" ||
    (filter === "attention"
      ? ["Unavailable", "Degraded"].includes(service.runtime_status)
      : service.runtime_status ===
        (filter === "available" ? "Available" : "Disabled"));
  const matchingRows = rows.filter(
    (service) =>
      matchesStatus(service) &&
      [service.name, service.public_model_name].some((name) =>
        name.toLocaleLowerCase().includes(search.trim().toLocaleLowerCase()),
      ),
  );
  const statusOrder = {
    Unavailable: 0,
    Degraded: 1,
    Available: 2,
    Disabled: 3,
  };
  const displayedRows = monitorView
    ? [...matchingRows].sort((a, b) => {
        const errorDifference =
          (telemetryByServiceId.get(b.id)?.window_1h.error_rate ?? -1) -
          (telemetryByServiceId.get(a.id)?.window_1h.error_rate ?? -1);
        return (
          errorDifference ||
          (telemetryByInstanceId.get(b.primary_instance_id)?.current.waiting ??
            -1) -
            (telemetryByInstanceId.get(a.primary_instance_id)?.current
              .waiting ?? -1)
        );
      })
    : [...matchingRows].sort(
        (a, b) =>
          statusOrder[a.runtime_status] - statusOrder[b.runtime_status] ||
          a.name.localeCompare(b.name, "zh-CN"),
      );
  const backendCounts = new Map<string, number>();
  for (const row of rows)
    backendCounts.set(
      row.primary_instance_id,
      (backendCounts.get(row.primary_instance_id) ?? 0) + 1,
    );
  const canViewRequestRecords =
    session.me?.capabilities.some(
      ({ capability }) => capability === "view_request_records",
    ) === true;

  const columns = serviceColumns({
    instanceById,
    onToggle: toggleService,
    deleteAction: (service) => (
      <DeleteServiceAction
        apiClient={apiClient}
        service={service}
        instanceName={
          instanceById.get(service.primary_instance_id)?.name ?? "模型实例"
        }
        permissionCount={permissionCounts.get(service.id)}
        onDeleted={() => {
          setDeleted(service);
        }}
      />
    ),
    pendingId: toggle.isPending ? toggle.variables.id : undefined,
    telemetryByInstanceId,
    telemetryByServiceId,
    backendCounts,
    permissionCounts,
    todayRequests:
      todayUsage.data === undefined
        ? undefined
        : new Map(
            todayUsage.data.details.flatMap((item) =>
              item.group_id === null
                ? []
                : [[item.group_id, item.requests] as const],
            ),
          ),
    todayReason: canViewUsage ? "今日用量暂不可用" : "无权查看今日用量",
    canViewUsage,
  });
  const narrow = useServiceTableNarrow();
  const visibleColumns = columns.filter(
    ({ id }) =>
      !narrow ||
      ![
        "authorized-users",
        "today-requests",
        "service-tps",
        "concurrency",
      ].includes(id),
  );
  const telemetry = [
    ...telemetryByServiceId.values(),
    ...telemetryByInstanceId.values(),
  ];
  const monitorDelayed =
    telemetry.some((item) => item.telemetry_status !== "available") ||
    telemetryQueries.some((query) => query.isError) ||
    serviceTelemetryQueries.some((query) => query.isError);
  const sampledAt = telemetry
    .map((item) => item.sampled_at)
    .filter((value): value is string => value !== null)
    .sort()
    .at(-1);

  const serviceList = (
    <>
      {commandError === undefined ? null : (
        <InlineAlert title="操作未完成" tone="negative">
          {commandError.responseMessage ??
            "AI 服务状态未改变。请刷新列表后重试。"}
        </InlineAlert>
      )}
      <div className="pw-service-filters">
        <div role="group" aria-label="按状态筛选">
          {(
            [
              ["all", "全部"],
              ["attention", "需处理"],
              ["available", "可用"],
              ["disabled", "已停用"],
            ] as const
          ).map(([value, label]) => (
            <Button
              key={value}
              size="s"
              variant="ghost"
              aria-pressed={statusFilter === value}
              onClick={() => {
                setStatusFilter(value);
              }}
            >
              {label}{" "}
              <span>
                {rows.filter((row) => matchesStatus(row, value)).length}
              </span>
            </Button>
          ))}
        </div>
        <div className="pw-service-filters__search">
          {rows.length > 0 && monitorDelayed ? (
            <span className="pw-service-monitor-note" role="status">
              {sampledAt
                ? `指标更新于 ${new Date(sampledAt).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false })} · `
                : ""}
              监控数据延迟或不可用
            </span>
          ) : null}
          <TextField
            label="搜索服务或 API 模型名"
            type="search"
            value={search}
            onChange={(event) => {
              setSearch(event.target.value);
            }}
            placeholder="搜索服务或 API 模型名"
          />
        </div>
      </div>
      {monitorView ? (
        <p>监控视图：按 Error Rate、后端 Waiting 降序排列。</p>
      ) : null}
      {services.data === undefined && services.isError ? (
        <InlineAlert title="无法获取 AI 服务" tone="negative">
          AI 服务列表无法显示。请检查平台连接后重试。
          <Button onClick={() => void services.refetch()} size="s">
            重试
          </Button>
        </InlineAlert>
      ) : (
        <DataTable
          caption="AI 服务"
          columns={visibleColumns}
          headerGroups={[
            { id: "today", header: "今日", columnIds: ["today-requests"] },
            {
              id: "hour",
              header: "近 1 小时",
              columnIds: ["ttft", "error-rate"],
            },
            {
              id: "current",
              header: "当前",
              columnIds: ["service-tps", "concurrency"],
            },
          ]}
          emptyContent={
            <EmptyState
              description={
                rows.length
                  ? "请调整状态筛选或搜索内容。"
                  : "选择一个运行中的模型实例，发布为用户可调用的 AI 服务。"
              }
              icon="apiServices"
              title={rows.length ? "没有匹配的 AI 服务" : "还没有 AI 服务"}
            />
          }
          getRowKey={({ id }) => id}
          loading={services.isPending || instances.isPending}
          rows={displayedRows}
        />
      )}
      <p className="pw-service-monitor-note">
        只有已停用、或后端实例已停止／运行失败的服务才能删除；正在提供服务的服务需先停用。
      </p>
    </>
  );

  return (
    <section className="pw-services-page">
      <header className="pw-services-page__head">
        <span>
          <h1 ref={heading} tabIndex={-1}>
            AI 服务
          </h1>
          <p>
            把模型实例发布为用户可调用的 AI 服务，并管理每个服务绑定的模型实例。
          </p>
        </span>
        <CreateServiceModal
          apiClient={apiClient}
          instances={instances.data?.items ?? []}
          instancesLoaded={instances.data !== undefined}
          services={rows}
          // instance actions link here as /services?create=<instanceId>.
          preselectInstanceId={
            instances.data === undefined
              ? undefined
              : (searchParams.get("create") ?? undefined)
          }
          onPreselectHandled={() => {
            setSearchParams(
              (current) => {
                const next = new URLSearchParams(current);
                next.delete("create");
                return next;
              },
              { replace: true },
            );
          }}
          onCreated={(service) => {
            queryClient.setQueryData(
              AI_SERVICES_QUERY_KEY,
              (current: { items: AiService[] } | undefined) => ({
                items: [...(current?.items ?? []), service],
              }),
            );
          }}
        />
      </header>
      {deleted ? (
        <InlineAlert
          title={`已删除 AI 服务「${deleted.name}」`}
          tone="positive"
        >
          模型实例「
          {instanceById.get(deleted.primary_instance_id)?.name ?? "模型实例"}
          」已保留（
          {primaryInstanceStatusLabel(deleted.primary_instance_status)}
          ），可在模型实例中删除或重新发布。
          <Link to="/models">前往模型实例</Link>
        </InlineAlert>
      ) : null}
      {canViewRequestRecords ? (
        <Tabs
          aria-label="AI 服务内容"
          defaultValue="services"
          items={[
            { content: serviceList, label: "服务列表", value: "services" },
            {
              content: <RequestRecordsPanel apiClient={apiClient} />,
              label: "调用记录",
              value: "request-records",
            },
          ]}
        />
      ) : (
        serviceList
      )}
    </section>
  );
}

// remove four comparison columns only below 1024px.
function useServiceTableNarrow() {
  const query = useMemo(() => window.matchMedia("(max-width: 1023px)"), []);
  const [narrow, setNarrow] = useState(query.matches);
  useEffect(() => {
    const update = () => {
      setNarrow(query.matches);
    };
    query.addEventListener("change", update);
    update();
    return () => {
      query.removeEventListener("change", update);
    };
  }, [query]);
  return narrow;
}

function serviceColumns(options: {
  instanceById: ReadonlyMap<string, ModelInstance>;
  onToggle: (service: AiService) => Promise<void>;
  deleteAction: (service: AiService) => React.ReactNode;
  pendingId?: string;
  telemetryByInstanceId: ReadonlyMap<string, InstanceTelemetry>;
  telemetryByServiceId: ReadonlyMap<string, ServiceTelemetry>;
  backendCounts: ReadonlyMap<string, number>;
  permissionCounts: ReadonlyMap<string, number>;
  todayRequests?: ReadonlyMap<string, number>;
  todayReason: string;
  canViewUsage: boolean;
}): TableColumn<AiService>[] {
  return [
    {
      header: "服务",
      id: "name",
      render: (service) => {
        const { text, tone } = runtimeStatusPresentation(
          service.runtime_status,
        );
        const secondary =
          service.runtime_status === "Available"
            ? [
                service.description,
                service.public_model_name !== service.name
                  ? `API 模型名 ${service.public_model_name}`
                  : "",
              ]
                .filter(Boolean)
                .join(" · ")
            : service.runtime_status === "Disabled"
              ? text
              : `${text} · 后端${service.primary_instance_status === null ? "实例缺失" : `实例${primaryInstanceStatusLabel(service.primary_instance_status)}`}`;
        return (
          <span className={`pw-service-name pw-service-name--${tone}`}>
            <span
              className={`pw-service-signal pw-service-signal--${tone}`}
              role="img"
              aria-label={text}
              title={text}
            />
            <span className="pw-service-identity">
              <Link to={`/services/${service.id}`} title={service.name}>
                {service.name}
              </Link>
              {secondary ? (
                <span className="pw-service-secondary" title={secondary}>
                  {service.runtime_status === "Available" ? (
                    <>
                      {service.description ? (
                        <span title={service.description}>
                          {service.description}
                        </span>
                      ) : null}
                      {service.description &&
                      service.public_model_name !== service.name
                        ? " · "
                        : ""}
                      {service.public_model_name !== service.name ? (
                        <>
                          API 模型名{" "}
                          <span aria-label={service.public_model_name}>
                            {service.public_model_name}
                          </span>
                        </>
                      ) : null}
                    </>
                  ) : (
                    secondary
                  )}
                </span>
              ) : null}
            </span>
          </span>
        );
      },
    },
    {
      header: "后端实例",
      id: "instance",
      render: (service) => {
        const instance = options.instanceById.get(service.primary_instance_id);
        const name = instance?.name ?? "模型实例";
        const suffix = instance?.id.replaceAll("-", "");
        const defaultName =
          suffix &&
          /^[a-f0-9]{32}$/i.test(suffix) &&
          name.endsWith(`-${suffix}`)
            ? name.slice(0, -(suffix.length + 1))
            : undefined;
        const displayName = defaultName
          ? defaultName.slice(defaultName.lastIndexOf("/") + 1)
          : name;
        return (
          <span className="pw-service-identity">
            <span className="pw-service-instance-name">
              <strong title={name} aria-label={name}>
                {displayName}
              </strong>
              {instance ? (
                <CopyControl
                  aria-label={`复制实例名 ${name}`}
                  label=""
                  value={name}
                />
              ) : null}
            </span>
            {service.primary_instance_status !== "Running" ? (
              <span className="pw-service-instance-status">
                {primaryInstanceStatusLabel(service.primary_instance_status)} ·{" "}
                <Link to={`/models/instances/${service.primary_instance_id}`}>
                  前往处理
                </Link>
              </span>
            ) : null}
          </span>
        );
      },
    },
    {
      header: "授权用户",
      id: "authorized-users",
      numeric: true,
      render: (service) =>
        options.permissionCounts.has(service.id) ? (
          options.permissionCounts.get(service.id)
        ) : (
          <MissingValue kind="unavailable" reason="授权用户数暂不可用" />
        ),
    },
    {
      header: "请求（今日）",
      headerContent: "请求",
      id: "today-requests",
      numeric: true,
      render: (service) =>
        !options.canViewUsage ? (
          // no usage capability shows a plain dash named by its reason.
          <span
            className="pw-service-inactive-metric"
            title={options.todayReason}
          >
            <span aria-hidden="true">—</span>
            <span className="pw-visually-hidden">{options.todayReason}</span>
          </span>
        ) : options.todayRequests === undefined ? (
          <MissingValue kind="unavailable" reason={options.todayReason} />
        ) : (
          (options.todayRequests.get(service.id) ?? 0).toLocaleString("zh-CN")
        ),
    },
    {
      header: "TTFT P95（近 1 小时）",
      headerContent: "TTFT P95",
      id: "ttft",
      numeric: true,
      render: (service) =>
        service.primary_instance_status !== "Running" ? (
          <InactiveMetric />
        ) : (
          <span className="pw-service-window-metric">
            <ServiceWindowMetric
              telemetry={options.telemetryByServiceId.get(service.id)}
              metric="ttft_p95_ms"
            />
            <ServiceWindowStart
              telemetry={options.telemetryByServiceId.get(service.id)}
            />
          </span>
        ),
    },
    {
      header: "错误率（近 1 小时）",
      headerContent: "错误率",
      id: "error-rate",
      numeric: true,
      render: (service) =>
        service.primary_instance_status !== "Running" ? (
          <InactiveMetric />
        ) : (
          <ServiceWindowMetric
            telemetry={options.telemetryByServiceId.get(service.id)}
            metric="error_rate"
          />
        ),
    },
    {
      header: "输出 TPS（当前）",
      headerContent: "输出 TPS",
      id: "service-tps",
      numeric: true,
      render: (service) =>
        service.primary_instance_status !== "Running" ? (
          <InactiveMetric />
        ) : (
          <ServiceMetric
            value={
              options.telemetryByServiceId.get(service.id)?.current
                .output_tps_avg
            }
            digits={1}
          />
        ),
    },
    {
      header: "并发 / 排队（当前）",
      headerContent: "并发 / 排队",
      id: "concurrency",
      numeric: true,
      render: (service) =>
        service.primary_instance_status !== "Running" ? (
          <InactiveMetric />
        ) : (
          <span className="pw-service-window-metric">
            <span>
              <ServiceMetric
                value={
                  options.telemetryByServiceId.get(service.id)?.current
                    .inflight_last
                }
              />{" "}
              /{" "}
              <ServiceMetric
                value={
                  options.telemetryByInstanceId.get(service.primary_instance_id)
                    ?.current.waiting
                }
                reason="后端遥测未采集"
              />
            </span>
            {(options.backendCounts.get(service.primary_instance_id) ?? 0) >
            1 ? (
              <span>共享后端</span>
            ) : null}
          </span>
        ),
    },
    {
      header: "操作",
      id: "actions",
      render: (service) => (
        <span className="pw-service-actions">
          <Button
            disabled={options.pendingId === service.id}
            onClick={() => void options.onToggle(service)}
            size="s"
            variant="ghost"
          >
            {service.enabled ? "停用" : "启用"}
          </Button>
          {!service.enabled ||
          ["Stopped", "Failed"].includes(service.primary_instance_status ?? "")
            ? options.deleteAction(service)
            : null}
        </span>
      ),
    },
  ];
}

export function DeleteServiceAction({
  apiClient,
  service,
  instanceName,
  permissionCount,
  onDeleted,
}: {
  apiClient: ApiClient;
  service: AiService;
  instanceName: string;
  permissionCount: number | undefined;
  onDeleted: () => void;
}) {
  const [open, setOpen] = useState(false);
  const queryClient = useQueryClient();
  const deletion = useMutation({
    mutationFn: () => apiClient.deleteAiService(service.id),
  });
  async function confirm() {
    try {
      await deletion.mutateAsync();
    } catch {
      return;
    }
    setOpen(false);
    onDeleted();
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: AI_SERVICES_QUERY_KEY }),
      queryClient.invalidateQueries({ queryKey: MODEL_INSTANCES_QUERY_KEY }),
    ]);
  }
  return (
    <Modal
      open={open}
      onOpenChange={(value) => {
        if (!deletion.isPending) {
          setOpen(value);
          deletion.reset();
        }
      }}
      title={`删除 AI 服务「${service.name}」？`}
      description="请确认以下信息与删除后果。"
      trigger={
        <Button
          variant="ghost"
          size="s"
          className="pw-service-delete"
          aria-label={`删除 ${service.name}`}
        >
          删除
        </Button>
      }
      footer={
        <>
          <Button
            variant="secondary"
            disabled={deletion.isPending}
            onClick={() => {
              setOpen(false);
            }}
          >
            取消
          </Button>
          <Button
            variant="danger"
            disabled={deletion.isPending}
            onClick={() => void confirm()}
          >
            删除服务
          </Button>
        </>
      }
    >
      <dl className="pw-service-delete-facts">
        <dt>API 模型名</dt>
        <dd>{service.public_model_name}</dd>
        <dt>后端实例</dt>
        <dd>
          {instanceName}（
          {primaryInstanceStatusLabel(service.primary_instance_status)}）
        </dd>
        <dt>授权用户</dt>
        <dd>
          {permissionCount === undefined
            ? "授权用户数暂不可用"
            : `${String(permissionCount)} 人`}
        </dd>
      </dl>
      <p>删除后：</p>
      <ul>
        <li>
          使用 API 模型名 {service.public_model_name} 的调用将返回{" "}
          <code>model_not_found</code>，已授权用户不再看到这个服务。
        </li>
        <li>
          只限这个服务的 API 密钥将不能再调用任何服务；密钥本身不会被撤销。
        </li>
        <li>
          模型实例「{instanceName}」保留，
          {service.primary_instance_status === "Running"
            ? "仍在运行并占用计算资源，可在模型实例中停止或删除。"
            : "可在模型实例中删除，或重新发布为新服务。"}
        </li>
        <li>
          历史用量、调用记录与审计保留；服务名称与 API
          模型名释放，可用于新服务。
        </li>
        <li>删除后无法恢复。</li>
      </ul>
      {deletion.isError ? (
        <InlineAlert title="删除失败" tone="negative">
          服务没有被删除，请关闭对话框，刷新列表后再操作。
        </InlineAlert>
      ) : null}
    </Modal>
  );
}

function InactiveMetric() {
  return <span className="pw-service-inactive-metric">—</span>;
}

function ServiceWindowStart({
  telemetry,
}: {
  telemetry: ServiceTelemetry | undefined;
}) {
  const start = telemetry?.window_1h.started_at;
  const end = telemetry?.sampled_at;
  if (
    !start ||
    !end ||
    new Date(end).getTime() - new Date(start).getTime() >= 3_595_000
  )
    return null;
  return (
    <span>
      自{" "}
      <time dateTime={start}>
        {new Date(start).toLocaleString("zh-CN", { hour12: false })}
      </time>{" "}
      起
    </span>
  );
}
