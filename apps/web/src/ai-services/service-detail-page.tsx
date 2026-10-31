import * as RadixTabs from "@radix-ui/react-tabs";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useParams, useSearchParams } from "react-router";
import { buildAccessExamples } from "../api/access-examples";
import type {
  AiService,
  UpdateAiServiceRequest,
} from "../api/ai-service-contracts";
import { ApiError, type ApiClient } from "../api/client";
import type {
  RequestRecordFilters,
  RequestRecordErrorGroup,
} from "../api/request-record-contracts";
import { useConfirmLeave } from "../app/navigation-guard";
import { useSession } from "../auth/session";
import {
  Button,
  CopyControl,
  InlineAlert,
  MissingValue,
  Modal,
} from "../components";
import { useModelInstances } from "../model-instances/queries";
import { RequestRecordsPanel } from "../request-records/request-records-panel";
import { formatObservedAt } from "../system/formatting";
import { formatSummaryCount } from "../telemetry/format-summary-count";
import { useInstanceTelemetry } from "../telemetry/queries";
import { UsagePanel } from "../usage/usage-page";
import {
  ServicePermissions,
  servicePermissionsQueryKey,
} from "../users/service-permissions";
import { PublicModelRename } from "./public-model-rename";
import { AI_SERVICES_QUERY_KEY, useAiService } from "./queries";
import { ServiceDisplayName } from "./service-display-name";
import { ServiceMonitor } from "./service-monitor";
import {
  AiServiceStatusBadge,
  DeleteServiceAction,
  primaryInstanceStatusLabel,
} from "./service-pages";

type InitialFilters = Pick<
  RequestRecordFilters,
  "errorCode" | "status" | "from" | "to"
>;

export function AiServiceDetailPage({ apiClient }: { apiClient: ApiClient }) {
  const { serviceId = "" } = useParams();
  // Reset drafts and tab state when navigating directly between service URLs.
  return (
    <ServiceDetail
      key={serviceId}
      apiClient={apiClient}
      serviceId={serviceId}
    />
  );
}

function ServiceDetail({
  apiClient,
  serviceId,
}: {
  apiClient: ApiClient;
  serviceId: string;
}) {
  const queryClient = useQueryClient();
  const session = useSession();
  const [params] = useSearchParams();
  const [tab, setCurrentTab] = useState(
    params.get("tab") === "access" ? "access" : "overview",
  );
  const confirmLeave = useConfirmLeave();
  // Leaving the access tab with unsaved grants asks first; every tab
  // change goes through here, including the buttons that jump to a tab.
  function setTab(next: string) {
    if (next === tab) return;
    confirmLeave(() => {
      setCurrentTab(next);
    });
  }
  const service = useAiService(apiClient, serviceId);
  const instances = useModelInstances(apiClient);
  const [description, setDescription] = useState<string>();
  const [instanceId, setInstanceId] = useState<string>();
  const [notice, setNotice] = useState<string>();
  const [submitError, setSubmitError] = useState<string>();
  const [confirmBinding, setConfirmBinding] = useState(false);
  const [deleted, setDeleted] = useState<AiService>();
  const [initialFilters, setInitialFilters] = useState<InitialFilters>();
  const canRename = session.me?.role === "admin";
  const canViewUsage =
    session.me?.capabilities.some(
      ({ capability }) => capability === "view_usage",
    ) === true;
  const canViewRecords =
    session.me?.capabilities.some(
      ({ capability }) => capability === "view_request_records",
    ) === true;
  const permissions = useQuery({
    queryKey: servicePermissionsQueryKey(serviceId),
    queryFn: () => apiClient.getServicePermissions(serviceId),
    enabled: service.data !== undefined && deleted === undefined,
  });
  const update = useMutation({
    mutationFn: (body: UpdateAiServiceRequest) =>
      apiClient.updateAiService(serviceId, body),
  });

  async function save(body: UpdateAiServiceRequest) {
    setSubmitError(undefined);
    setNotice(undefined);
    try {
      const changed = await update.mutateAsync(body);
      queryClient.setQueryData(["ai-services", serviceId], changed);
      if (body.description !== undefined) {
        setDescription(undefined);
        setInstanceId(undefined);
        setConfirmBinding(false);
        setNotice(
          body.primary_instance_id === undefined ? "描述已保存" : "换绑已生效",
        );
      }
      await queryClient.invalidateQueries({ queryKey: AI_SERVICES_QUERY_KEY });
    } catch (error) {
      setSubmitError(
        error instanceof ApiError
          ? (error.responseMessage ?? "AI 服务设置未改变，请重试。")
          : "无法保存设置，请检查平台连接后重试。",
      );
    }
  }

  if (deleted !== undefined)
    return (
      <section className="pw-service-detail" role="status">
        <h1>已删除 AI 服务「{deleted.name}」</h1>
        <p>
          模型实例已保留，可在模型实例中删除或重新发布。历史用量与调用记录保留。
        </p>
        <div className="pw-service-detail__actions">
          <Link to="/services">返回 AI 服务</Link>
          <Link to="/models/instances">前往模型实例</Link>
        </div>
      </section>
    );
  if (service.isPending)
    return (
      <section
        aria-busy="true"
        aria-label="AI 服务详情正在加载"
        className="pw-service-detail"
      >
        <span className="sk pw-service-detail__skeleton" />
        <span className="sk pw-service-detail__skeleton" />
        <div className="pw-service-detail__grid">
          <span className="sk pw-service-detail__skeleton" />
          <span className="sk pw-service-detail__skeleton" />
        </div>
      </section>
    );
  if (service.data === undefined)
    return (
      <section className="pw-service-detail">
        <Link to="/services">返回 AI 服务</Link>
        <InlineAlert title="无法获取 AI 服务详情" tone="negative">
          详情无法显示，不影响这个服务的调用。请确认服务仍然存在，然后重试或从列表重新进入。
          <Button onClick={() => void service.refetch()}>重试</Button>
        </InlineAlert>
      </section>
    );
  const item = service.data;
  const examples = buildAccessExamples(
    window.location.origin,
    item.public_model_name,
  );
  const bound = instances.data?.items.find(
    ({ id }) => id === item.primary_instance_id,
  );
  const alternatives = (instances.data?.items ?? []).filter(
    ({ id, status }) => id !== item.primary_instance_id && status === "Running",
  );
  const currentDescription = description ?? item.description;
  const currentInstanceId = instanceId ?? item.primary_instance_id;
  const switching = currentInstanceId !== item.primary_instance_id;
  const instancePath = `/models/instances/${item.primary_instance_id}`;
  const canDelete =
    !item.enabled ||
    item.primary_instance_status === "Stopped" ||
    item.primary_instance_status === "Failed";
  const tabs = [
    { value: "overview", label: "概览" },
    { value: "monitor", label: "监控" },
    ...(canViewRecords ? [{ value: "records", label: "调用记录" }] : []),
    { value: "access", label: "访问权限" },
    ...(canViewUsage ? [{ value: "usage", label: "用量" }] : []),
    { value: "settings", label: "设置" },
  ];
  return (
    <article className="pw-service-detail">
      <nav aria-label="面包屑">
        <Link to="/services">AI 服务</Link>
        <span aria-hidden="true"> › </span>
        <span>{item.name}</span>
      </nav>
      <header className="pw-service-detail__head">
        <div>
          <div className="pw-service-detail__title">
            <span
              aria-hidden="true"
              className={`pw-service-detail__light pw-service-detail__light--${item.runtime_status}`}
            />
            <h1>{item.name}</h1>
            {canRename ? (
              <ServiceDisplayName
                apiClient={apiClient}
                service={item}
                unavailable={service.isError || service.isFetching}
                onRenamed={() => {
                  setNotice("服务名称已修改");
                }}
              />
            ) : null}
            <AiServiceStatusBadge size="large" status={item.runtime_status} />
          </div>
          {item.description ? <p>{item.description}</p> : null}
        </div>
        <div className="pw-service-detail__actions">
          {canDelete ? (
            <DeleteServiceAction
              apiClient={apiClient}
              service={item}
              instanceName={bound?.name ?? "模型实例"}
              permissionCount={permissions.data?.user_ids.length}
              onDeleted={() => {
                setDeleted(item);
              }}
            />
          ) : null}
          <Button
            loading={update.isPending}
            disabled={service.isError}
            onClick={() => void save({ enabled: !item.enabled })}
          >
            {item.enabled ? "停用" : "启用"}
          </Button>
        </div>
      </header>
      {service.isError ? (
        <InlineAlert title="详情更新失败" tone="notice">
          当前显示上次获取的数据，请重试。
          <Button onClick={() => void service.refetch()}>重试</Button>
        </InlineAlert>
      ) : null}
      {item.runtime_status === "Disabled" ? (
        <div className="pw-service-detail__banner" role="status">
          <div>
            <strong>服务已停用</strong>
            <p>
              调用将返回 503 <code>service_disabled</code>。授权与 API
              密钥保留，启用后即可恢复对外服务。
            </p>
          </div>
          <Button
            loading={update.isPending}
            onClick={() => void save({ enabled: true })}
          >
            启用
          </Button>
        </div>
      ) : null}
      {item.runtime_status === "Unavailable" ? (
        <InlineAlert title="服务不可用" tone="negative">
          后端实例{primaryInstanceStatusLabel(item.primary_instance_status)}
          ，调用将返回 503 <code>backend_unavailable</code>。
          <div className="pw-service-detail__actions">
            <Link to={instancePath}>启动实例</Link>
            {alternatives.length > 0 ? (
              <Button
                size="s"
                onClick={() => {
                  setTab("settings");
                }}
              >
                换绑实例
              </Button>
            ) : (
              <Link to="/models/deploy">部署模型</Link>
            )}
          </div>
        </InlineAlert>
      ) : null}
      {item.runtime_status === "Degraded" ? (
        <InlineAlert title="服务启动中" tone="notice">
          后端实例启动完成后自动可用。当前调用返回 503{" "}
          <code>backend_unavailable</code>。
          <Link to={instancePath}>前往实例</Link>
        </InlineAlert>
      ) : null}
      {notice ? (
        <InlineAlert title={notice} tone="positive">
          AI 服务已使用最新设置。
        </InlineAlert>
      ) : null}
      {submitError && !confirmBinding ? (
        <InlineAlert title="设置未保存" tone="negative">
          {submitError}
        </InlineAlert>
      ) : null}
      <RadixTabs.Root
        className="pw-tabs"
        value={tabs.some(({ value }) => value === tab) ? tab : "overview"}
        onValueChange={setTab}
      >
        <RadixTabs.List className="pw-tabs__list" aria-label="AI 服务详情">
          {tabs.map(({ value, label }) => (
            <RadixTabs.Trigger
              className="pw-tabs__trigger"
              key={value}
              value={value}
            >
              {label}
            </RadixTabs.Trigger>
          ))}
        </RadixTabs.List>
        <RadixTabs.Content className="pw-tabs__content" value="overview">
          <div className="pw-service-detail__overview">
            <div className="pw-service-detail__grid">
              <section
                className="pw-service-detail__panel"
                aria-label="接入信息"
              >
                <h2>接入信息</h2>
                <dl className="pw-service-facts">
                  <div>
                    <dt>Endpoint</dt>
                    <dd className="pw-endpoint-rows">
                      <div className="pw-endpoint-row">
                        <span>OpenAI 兼容</span>
                        <code>{examples.baseUrl}</code>
                        <CopyControl
                          aria-label="复制 OpenAI 兼容 Endpoint"
                          value={examples.baseUrl}
                        />
                      </div>
                      <div className="pw-endpoint-row">
                        <span>Anthropic 兼容</span>
                        <code>{examples.anthropicBaseUrl}</code>
                        <CopyControl
                          aria-label="复制 Anthropic 兼容 Endpoint"
                          value={examples.anthropicBaseUrl}
                        />
                      </div>
                    </dd>
                  </div>
                  <div>
                    <dt>API 模型名</dt>
                    <dd className="pw-service-detail__access">
                      <span dir="auto">{item.public_model_name}</span>
                      <span className="pw-service-detail__access-actions">
                        <CopyControl
                          aria-label="复制 API 模型名"
                          value={item.public_model_name}
                        />
                        {canRename ? (
                          <PublicModelRename
                            apiClient={apiClient}
                            service={item}
                            unavailable={service.isError || service.isFetching}
                            onRenamed={() => {
                              setNotice("API 模型名已修改");
                            }}
                          />
                        ) : null}
                      </span>
                    </dd>
                  </div>
                  <div>
                    <dt>服务类型</dt>
                    <dd>Chat Completions</dd>
                  </div>
                  <div>
                    <dt>授权用户</dt>
                    <dd>
                      {permissions.data === undefined
                        ? "授权用户数暂不可用"
                        : `${String(permissions.data.user_ids.length)} 人`}
                      <Button
                        size="s"
                        variant="ghost"
                        onClick={() => {
                          setTab("access");
                        }}
                      >
                        管理
                      </Button>
                    </dd>
                  </div>
                </dl>
              </section>
              <BackendHealth
                apiClient={apiClient}
                service={item}
                name={bound?.name}
                runtime={bound?.runtime_type}
              />
            </div>
            <TodayUsage
              apiClient={apiClient}
              serviceId={serviceId}
              allowed={canViewUsage}
            />
            <RecentErrors
              apiClient={apiClient}
              serviceId={serviceId}
              allowed={canViewRecords}
              onSelect={(filters) => {
                setInitialFilters(filters);
                setTab("records");
              }}
            />
          </div>
        </RadixTabs.Content>
        <RadixTabs.Content className="pw-tabs__content" value="monitor">
          <ServiceMonitor
            apiClient={apiClient}
            service={item}
            instanceName={bound?.name ?? "模型实例"}
          />
        </RadixTabs.Content>
        {canViewRecords ? (
          <RadixTabs.Content className="pw-tabs__content" value="records">
            <RequestRecordsPanel
              key={JSON.stringify(initialFilters)}
              apiClient={apiClient}
              lockedService={{ id: serviceId, label: item.name }}
              initialFilters={initialFilters}
            />
          </RadixTabs.Content>
        ) : null}
        <RadixTabs.Content
          className="pw-tabs__content"
          value="access"
          forceMount
          hidden={tab !== "access"}
        >
          <ServicePermissions apiClient={apiClient} serviceId={serviceId} />
        </RadixTabs.Content>
        {canViewUsage ? (
          <RadixTabs.Content className="pw-tabs__content" value="usage">
            <UsagePanel
              apiClient={apiClient}
              lockedService={{ id: serviceId, label: item.name }}
            />
          </RadixTabs.Content>
        ) : null}
        <RadixTabs.Content className="pw-tabs__content" value="settings">
          <section className="pw-service-detail__section">
            <h2>服务设置</h2>
            <form
              className="pw-service-form"
              onSubmit={(event) => {
                event.preventDefault();
                setSubmitError(undefined);
                if (switching) setConfirmBinding(true);
                else void save({ description: currentDescription });
              }}
            >
              <label className="pw-service-field">
                <span>后端实例</span>
                <select
                  value={currentInstanceId}
                  onChange={(event) => {
                    setInstanceId(event.currentTarget.value);
                  }}
                >
                  <option value={item.primary_instance_id}>
                    {bound?.name ?? "当前模型实例"} ·{" "}
                    {primaryInstanceStatusLabel(item.primary_instance_status)}
                    （当前）
                  </option>
                  {alternatives.map((instance) => (
                    <option key={instance.id} value={instance.id}>
                      {instance.name} · 运行中
                    </option>
                  ))}
                </select>
              </label>
              {instances.isError ? (
                <InlineAlert title="无法获取模型实例" tone="negative">
                  当前不能换绑实例，请重试。
                  <Button onClick={() => void instances.refetch()}>重试</Button>
                </InlineAlert>
              ) : alternatives.length === 0 && !instances.isPending ? (
                <p>
                  暂无其他运行中的实例可切换。请先部署模型或启动已有实例。
                  <Link to="/models/deploy">部署模型</Link> ·{" "}
                  <Link to="/models/instances">模型实例</Link>
                </p>
              ) : null}
              <label className="pw-service-field">
                <span>描述</span>
                <textarea
                  maxLength={200}
                  rows={4}
                  value={currentDescription}
                  onChange={(event) => {
                    setDescription(event.currentTarget.value);
                  }}
                />
                <span>{currentDescription.length}/200</span>
              </label>
              <div className="pw-service-form__actions">
                <Modal
                  trigger={
                    <Button
                      type="submit"
                      variant="primary"
                      loading={update.isPending}
                      disabled={
                        service.isError ||
                        (switching &&
                          (instances.isError || instances.isPending))
                      }
                    >
                      保存设置
                    </Button>
                  }
                  open={confirmBinding}
                  onOpenChange={(open) => {
                    if (!update.isPending && (!open || switching))
                      setConfirmBinding(open);
                  }}
                  title="切换后端实例？"
                  description="请确认当前实例与目标实例。"
                  footer={
                    <>
                      <Button
                        disabled={update.isPending}
                        onClick={() => {
                          setConfirmBinding(false);
                        }}
                      >
                        取消
                      </Button>
                      <Button
                        variant="primary"
                        loading={update.isPending}
                        onClick={() =>
                          void save({
                            description: currentDescription,
                            primary_instance_id: currentInstanceId,
                          })
                        }
                      >
                        切换并保存
                      </Button>
                    </>
                  }
                >
                  <p>
                    {bound?.name ?? "当前模型实例"} →{" "}
                    {instances.data?.items.find(
                      ({ id }) => id === currentInstanceId,
                    )?.name ?? "目标模型实例"}
                  </p>
                  <p>
                    保存时校验目标实例，校验不通过则保留原实例。进行中的调用在原实例完成；API
                    模型名与授权不变。
                  </p>
                  {submitError ? (
                    <InlineAlert title="设置未保存" tone="negative">
                      {submitError}
                    </InlineAlert>
                  ) : null}
                </Modal>
              </div>
            </form>
          </section>
        </RadixTabs.Content>
      </RadixTabs.Root>
    </article>
  );
}

function BackendHealth({
  apiClient,
  service,
  name,
  runtime,
}: {
  apiClient: ApiClient;
  service: AiService;
  name: string | undefined;
  runtime: string | undefined;
}) {
  const telemetry = useInstanceTelemetry(
    apiClient,
    service.primary_instance_id,
  );
  const running = service.primary_instance_status === "Running";
  const metric = (key: "running" | "waiting") => {
    const unsupported = telemetry.data?.current.unsupported.includes(
      key === "running"
        ? "runtime_requests_running"
        : "runtime_requests_waiting",
    );
    const value =
      running &&
      !unsupported &&
      telemetry.data?.telemetry_status === "available"
        ? telemetry.data.current[key]
        : null;
    return value == null ? (
      <MissingValue
        kind="unavailable"
        reason={
          !running
            ? "实例不在运行中，无实时负载"
            : unsupported
              ? "当前 Runtime 不支持该指标"
              : "后端遥测不可用"
        }
      />
    ) : (
      value.toLocaleString("zh-CN")
    );
  };
  return (
    <section className="pw-service-detail__panel" aria-label="后端健康">
      <h2>后端健康</h2>
      <dl className="pw-service-facts">
        <div>
          <dt>模型实例</dt>
          <dd>
            <Link to={`/models/instances/${service.primary_instance_id}`}>
              {name ?? "查看模型实例"}
            </Link>
          </dd>
        </div>
        <div>
          <dt>状态</dt>
          <dd>{primaryInstanceStatusLabel(service.primary_instance_status)}</dd>
        </div>
        <div>
          <dt>Runtime</dt>
          <dd>
            {runtime === "vllm"
              ? "vLLM"
              : runtime === "sglang"
                ? "SGLang"
                : "—"}
          </dd>
        </div>
        <div>
          <dt>当前请求</dt>
          <dd>{metric("running")}</dd>
        </div>
        <div>
          <dt>等待请求</dt>
          <dd>{metric("waiting")}</dd>
        </div>
      </dl>
      {!running ? (
        <p>实例不在运行中，无实时负载。</p>
      ) : (
        <p>以上为后端实例总负载。</p>
      )}
    </section>
  );
}

function TodayUsage({
  apiClient,
  serviceId,
  allowed,
}: {
  apiClient: ApiClient;
  serviceId: string;
  allowed: boolean;
}) {
  const now = new Date();
  const from = new Date(
    now.getFullYear(),
    now.getMonth(),
    now.getDate(),
  ).toISOString();
  const usage = useQuery({
    enabled: allowed,
    queryKey: ["service-detail-usage", serviceId, from],
    queryFn: () =>
      apiClient.getUsage({
        serviceId,
        from,
        to: new Date().toISOString(),
        timeZone: Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC",
        groupBy: "service",
      }),
    refetchInterval: 30_000,
  });
  const summary = usage.data?.summary;
  return (
    <section className="pw-service-detail__panel" aria-label="今日用量">
      <h2>今日用量</h2>
      {!allowed ? (
        <p>你没有查看用量的权限。</p>
      ) : usage.isPending ? (
        <p role="status">正在加载今日用量…</p>
      ) : usage.isError ? (
        <InlineAlert title="无法获取今日用量" tone="notice">
          请重试。<Button onClick={() => void usage.refetch()}>重试</Button>
        </InlineAlert>
      ) : (
        <dl className="pw-service-detail__kpis">
          {(
            [
              ["Requests", summary?.requests],
              ["Input Tokens", summary?.input_tokens],
              ["Output Tokens", summary?.output_tokens],
              ["Total Tokens", summary?.total_tokens],
            ] as const
          ).map(([label, value]) => (
            <div key={label}>
              <dt>{label}</dt>
              <dd
                title={
                  value == null ? undefined : value.toLocaleString("zh-CN")
                }
              >
                {value == null ? (
                  <MissingValue
                    kind="unavailable"
                    reason="Token 用量尚不完整"
                  />
                ) : (
                  formatSummaryCount(value)
                )}
              </dd>
            </div>
          ))}
          <div>
            <dt>错误率</dt>
            <dd>
              {summary?.error_rate == null ? (
                <MissingValue
                  kind="unavailable"
                  reason="今日暂无可计算错误率的调用"
                />
              ) : (
                `${(summary.error_rate * 100).toFixed(1)}%`
              )}
            </dd>
          </div>
        </dl>
      )}
    </section>
  );
}

function RecentErrors({
  apiClient,
  serviceId,
  allowed,
  onSelect,
}: {
  apiClient: ApiClient;
  serviceId: string;
  allowed: boolean;
  onSelect: (filters: InitialFilters) => void;
}) {
  const [window] = useState(() => ({
    from: new Date(Date.now() - 7 * 86_400_000).toISOString(),
    to: new Date().toISOString(),
  }));
  const errors = useQuery({
    enabled: allowed,
    queryKey: ["service-detail-errors", serviceId, window],
    queryFn: () =>
      apiClient.getRequestRecordErrorGroups({ serviceId, ...window }),
  });
  function select(row: RequestRecordErrorGroup) {
    onSelect({
      ...window,
      ...(row.status === "unknown" || row.error_code === null
        ? { status: row.status }
        : { errorCode: row.error_code }),
    });
  }
  return (
    <section className="pw-service-detail__panel" aria-label="最近异常">
      <h2>
        最近异常 <small>近 7 天</small>
      </h2>
      {!allowed ? (
        <p>你没有查看调用记录的权限。</p>
      ) : errors.isError ? (
        <InlineAlert title="无法获取最近异常" tone="notice">
          其他区块仍可查看，请重试。
          <Button onClick={() => void errors.refetch()}>重试</Button>
        </InlineAlert>
      ) : (
        <div className="pw-table-wrap" aria-busy={errors.isPending}>
          {errors.isPending ? (
            <p role="status">正在加载最近异常…</p>
          ) : errors.data.items.length === 0 ? (
            <p>暂无失败或状态未知的调用</p>
          ) : (
            <table className="pw-table pw-service-detail__errors">
              <caption className="pw-visually-hidden">最近异常</caption>
              <thead>
                <tr>
                  {["错误", "次数", "首次", "最近", "最新 Request ID"].map(
                    (label) => (
                      <th scope="col" key={label}>
                        {label}
                      </th>
                    ),
                  )}
                </tr>
              </thead>
              <tbody>
                {errors.data.items.slice(0, 5).map((row) => (
                  <tr
                    key={`${row.status}:${row.error_code ?? "unknown"}`}
                    onClick={() => {
                      select(row);
                    }}
                  >
                    <td>
                      <Button
                        variant="ghost"
                        size="s"
                        onClick={(event) => {
                          event.stopPropagation();
                          select(row);
                        }}
                      >
                        {row.status === "unknown"
                          ? "状态未知"
                          : (row.error_code ??
                            (row.status === "failed" ? "失败" : "已拒绝"))}
                      </Button>
                    </td>
                    <td>{row.count.toLocaleString("zh-CN")}</td>
                    <td>{formatObservedAt(row.first_at)}</td>
                    <td>{formatObservedAt(row.last_at)}</td>
                    <td>{row.latest_request_id}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}
    </section>
  );
}
