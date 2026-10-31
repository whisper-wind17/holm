import { zodResolver } from "@hookform/resolvers/zod";
import { Controller, useForm } from "react-hook-form";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useEffect, useId, useRef, useState } from "react";

import {
  renameApiKeyRequestSchema,
  type ApiKey,
  type CreatedApiKey,
  type ListedApiKey,
  type RenameApiKeyRequest,
} from "../api/api-key-contracts";
import { HELP_TOPICS } from "../help/content";
import { HelpButton } from "../help/help-center";
import { buildAccessExamples } from "../api/access-examples";
import { ApiError, type ApiClient } from "../api/client";
import type { UsageGroup } from "../api/usage-contracts";
import {
  Button,
  CodeExample,
  CopyControl,
  DataTable,
  EmptyState,
  InlineAlert,
  Modal,
  MissingValue,
  ProgressStages,
  StatusBadge,
  TextField,
  type TableColumn,
} from "../components";
import { useSession } from "../auth/session";
import {
  MY_AI_SERVICES_QUERY_KEY,
  useMyAiServices,
  useUser,
} from "../users/queries";
import {
  createApiKeyFormSchema,
  type CreateApiKeyFormInput,
  type CreateApiKeyFormOutput,
} from "./api-key-expiration";
import { API_KEYS_QUERY_KEY, useApiKeys, useApiKeyTodayUsage } from "./queries";
import {
  runFirstTestCall,
  type FirstTestCallProgress,
} from "./first-test-call";

export function ApiKeyPage({ apiClient }: { apiClient: ApiClient }) {
  const me = useSession().me;
  return me?.role === "user" ? (
    <UserApiKeyPage apiClient={apiClient} />
  ) : (
    <AdminApiKeyPage apiClient={apiClient} />
  );
}

function AdminApiKeyPage({ apiClient }: { apiClient: ApiClient }) {
  const me = useSession().me;
  const services = useUser(apiClient, me?.user.id ?? "");
  return (
    <ApiKeyPageContent
      apiClient={apiClient}
      services={services.data?.authorized_services ?? []}
      servicesUnavailable={services.isError}
    />
  );
}

function UserApiKeyPage({ apiClient }: { apiClient: ApiClient }) {
  const services = useMyAiServices(apiClient);
  return (
    <ApiKeyPageContent
      apiClient={apiClient}
      personalScope
      servicesLoading={services.isPending}
      services={services.data?.items ?? []}
      servicesUnavailable={services.isError}
    />
  );
}

function ApiKeyPageContent({
  apiClient,
  personalScope = false,
  servicesLoading = false,
  services,
  servicesUnavailable,
}: {
  apiClient: ApiClient;
  personalScope?: boolean;
  servicesLoading?: boolean;
  services: readonly { id: string; name: string; public_model_name: string }[];
  servicesUnavailable: boolean;
}) {
  const heading = useRef<HTMLHeadingElement>(null);
  const queryClient = useQueryClient();
  const keys = useApiKeys(apiClient);
  const me = useSession().me;
  const canViewUsage = me?.capabilities.some(
    ({ capability }) => capability === "view_usage",
  );
  const todayUsage = useApiKeyTodayUsage(
    apiClient,
    canViewUsage ? me?.user.id : undefined,
  );
  const usageByKey =
    canViewUsage && todayUsage.isSuccess
      ? new Map(todayUsage.data.details.map((item) => [item.group_id, item]))
      : undefined;
  const [commandError, setCommandError] = useState<ApiError>();

  useEffect(() => heading.current?.focus(), []);

  const revoke = useMutation({
    mutationFn: (id: string) => apiClient.revokeApiKey(id),
  });

  async function revokeKey(id: string) {
    setCommandError(undefined);
    try {
      await revoke.mutateAsync(id);
      await queryClient.invalidateQueries({ queryKey: API_KEYS_QUERY_KEY });
    } catch (error) {
      if (error instanceof ApiError) setCommandError(error);
      else throw error;
    }
  }

  return (
    <section className="pw-api-keys-page">
      <header className="pw-api-keys-page__head">
        <span>
          <h1 ref={heading} tabIndex={-1}>
            API 密钥
          </h1>
          {personalScope ? null : (
            <p>创建用于调用 AI 服务的个人密钥，并在不再使用时立即撤销。</p>
          )}
        </span>
        <CreateApiKeyModal
          apiClient={apiClient}
          personalScope={personalScope}
          services={services}
          servicesUnavailable={servicesUnavailable}
        />
      </header>
      <HelpButton topic={HELP_TOPICS.access.id}>查看 API 接入帮助</HelpButton>
      {personalScope &&
      !servicesLoading &&
      (servicesUnavailable || services.length === 0) ? (
        <InlineAlert
          title={
            servicesUnavailable
              ? "无法确认服务授权"
              : "管理员尚未为你分配 AI 服务"
          }
          tone="notice"
        >
          {servicesUnavailable
            ? "服务授权无法显示，请刷新页面后重试。"
            : "暂时不能创建密钥。获得授权后即可创建。"}
        </InlineAlert>
      ) : null}
      {commandError === undefined ? null : (
        <InlineAlert title="撤销未完成" tone="negative">
          {commandError.responseMessage ??
            "API 密钥仍然有效。请刷新列表后重试。"}
        </InlineAlert>
      )}
      {keys.data !== undefined && keys.isError ? (
        <InlineAlert title="API 密钥数据可能过期" tone="notice">
          刷新失败，允许服务可能已变更。请重试以确认当前范围。
          <Button onClick={() => void keys.refetch()} size="s">
            重试
          </Button>
        </InlineAlert>
      ) : null}
      {keys.data === undefined && keys.isError ? (
        <InlineAlert title="无法获取 API 密钥" tone="negative">
          API 密钥列表无法显示。请检查平台连接后重试。
          <Button onClick={() => void keys.refetch()} size="s">
            重试
          </Button>
        </InlineAlert>
      ) : (
        <DataTable
          caption="API 密钥"
          columns={apiKeyColumns({
            apiClient,
            onDeleted: () => heading.current?.focus(),
            onRevoke: revokeKey,
            pendingId: revoke.isPending ? revoke.variables : undefined,
            usageByKey,
          })}
          emptyContent={
            <EmptyState
              description="创建后，完整密钥只会显示一次。"
              icon="key"
              title="还没有 API 密钥"
            />
          }
          getRowKey={({ id }) => id}
          loading={keys.isPending}
          rows={keys.data?.items ?? []}
        />
      )}
    </section>
  );
}

function apiKeyColumns(options: {
  apiClient: ApiClient;
  onDeleted: () => void;
  onRevoke: (id: string) => Promise<void>;
  pendingId?: string;
  usageByKey?: ReadonlyMap<string | null, UsageGroup>;
}): TableColumn<ListedApiKey>[] {
  return [
    { header: "名称", id: "name", render: (key) => key.name },
    {
      accessibleValue: (key) => `${key.display_prefix}省略${key.last4}`,
      header: "密钥标识",
      id: "identifier",
      render: (key) => (
        <code
          className="pw-api-key-identifier"
          title="完整密钥只在创建时显示一次，这里只显示前缀与后四位"
        >
          {key.display_prefix}…{key.last4}
        </code>
      ),
    },
    {
      header: "允许服务",
      id: "services",
      render: (key) => {
        if (key.effective_services.length === 0)
          return <StatusBadge icon="alert" text="无可用服务" tone="notice" />;
        return key.effective_services
          .map((service) => `${service.name} · ${service.public_model_name}`)
          .join("、");
      },
    },
    {
      header: "状态",
      id: "status",
      render: (key) => <ApiKeyStatus value={key.status} />,
    },
    {
      header: "创建时间",
      id: "created",
      render: (key) => (
        <time dateTime={key.created_at}>{formatDate(key.created_at)}</time>
      ),
    },
    {
      header: "到期",
      id: "expires",
      render: (key) =>
        key.expires_at === null ? (
          "永不过期"
        ) : (
          <time dateTime={key.expires_at}>{formatDate(key.expires_at)}</time>
        ),
    },
    {
      header: "最近使用",
      id: "last-used",
      render: (key) =>
        key.last_used_at === null ? (
          "从未使用"
        ) : (
          <time dateTime={key.last_used_at}>
            {formatDate(key.last_used_at)}
          </time>
        ),
    },
    {
      header: "今日 Requests",
      id: "today-requests",
      numeric: true,
      render: (key) =>
        options.usageByKey === undefined ? (
          <MissingValue kind="unavailable" reason="暂不可用" />
        ) : (
          (options.usageByKey.get(key.id)?.requests ?? 0).toLocaleString(
            "zh-CN",
          )
        ),
    },
    {
      header: "今日 Tokens",
      id: "today-tokens",
      numeric: true,
      render: (key) => {
        if (options.usageByKey === undefined)
          return <MissingValue kind="unavailable" reason="暂不可用" />;
        const usage = options.usageByKey.get(key.id);
        if (usage?.total_tokens === null)
          return <MissingValue kind="unavailable" reason="未知" />;
        return (usage?.total_tokens ?? 0).toLocaleString("zh-CN");
      },
    },
    {
      header: "操作",
      id: "actions",
      render: (key) => (
        <div className="pw-service-form__actions">
          <ManageKeyModal
            apiClient={options.apiClient}
            keyItem={key}
            mode="rename"
          />
          {key.status === "Active" ? (
            <RevokeKeyModal
              disabled={options.pendingId === key.id}
              keyItem={key}
              onRevoke={() => options.onRevoke(key.id)}
            />
          ) : (
            <ManageKeyModal
              apiClient={options.apiClient}
              keyItem={key}
              mode="delete"
              onDeleted={options.onDeleted}
            />
          )}
        </div>
      ),
    },
  ];
}

function ManageKeyModal({
  apiClient,
  keyItem,
  mode,
  onDeleted,
}: {
  apiClient: ApiClient;
  keyItem: ApiKey;
  mode: "rename" | "delete";
  onDeleted?: () => void;
}) {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [pending, setPending] = useState(false);
  const inFlight = useRef(false);
  const [errorMessage, setErrorMessage] = useState<string>();
  const {
    control,
    handleSubmit,
    reset,
    formState: { errors },
  } = useForm<RenameApiKeyRequest>({
    defaultValues: { name: keyItem.name },
    resolver: zodResolver(renameApiKeyRequestSchema),
  });
  const rename = mode === "rename";
  const nameInputId = useId();
  function changeOpen(next: boolean) {
    if (inFlight.current) return;
    if (next) {
      reset({ name: keyItem.name });
      setErrorMessage(undefined);
    }
    setOpen(next);
  }
  async function submit(values?: RenameApiKeyRequest) {
    if (inFlight.current) return;
    inFlight.current = true;
    setPending(true);
    setErrorMessage(undefined);
    try {
      if (rename) {
        if (values === undefined) throw new Error("rename values required");
        await apiClient.renameApiKey(keyItem.id, values);
      } else await apiClient.deleteApiKey(keyItem.id);
      setOpen(false);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: API_KEYS_QUERY_KEY }),
        queryClient.invalidateQueries({ queryKey: ["usage"] }),
      ]);
      if (!rename) onDeleted?.();
    } catch (error) {
      setErrorMessage(
        error instanceof ApiError
          ? (error.responseMessage ?? "操作未完成，请检查连接后重试。")
          : "操作未完成，请刷新列表后重试。",
      );
    } finally {
      inFlight.current = false;
      setPending(false);
    }
  }
  return (
    <Modal
      description={
        rename
          ? "仅修改名称，密钥和调用权限保持不变。"
          : "仅从列表移除，历史用量保留。密钥不会恢复有效。"
      }
      onOpenChange={changeOpen}
      open={open}
      title={rename ? "重命名密钥" : `删除“${keyItem.name}”的显示记录？`}
      trigger={
        <Button size="s" variant="ghost">
          {rename ? "重命名" : "删除记录"}
        </Button>
      }
    >
      <form
        className="pw-service-form"
        noValidate
        onSubmit={
          rename
            ? (event) =>
                void handleSubmit(
                  (values) => submit(values),
                  () => document.getElementById(nameInputId)?.focus(),
                )(event)
            : (event) => {
                event.preventDefault();
                void submit();
              }
        }
      >
        {rename ? (
          <Controller
            control={control}
            name="name"
            render={({ field }) => (
              <TextField
                id={nameInputId}
                label="名称"
                required
                maxLength={40}
                minLength={2}
                name={field.name}
                onBlur={field.onBlur}
                onChange={field.onChange}
                value={field.value}
                disabled={pending}
                errorText={errors.name?.message}
                helpText="2～40 个字符，不能含首尾空白。"
              />
            )}
          />
        ) : (
          <p className="pw-dialog-note">此操作不会删除历史调用记录或用量。</p>
        )}
        {errorMessage === undefined ? null : (
          <InlineAlert
            title={rename ? "重命名未完成" : "删除未完成"}
            tone="negative"
          >
            {errorMessage}
          </InlineAlert>
        )}
        <footer className="pw-service-form__actions">
          <Button
            disabled={pending}
            onClick={() => {
              changeOpen(false);
            }}
          >
            取消
          </Button>
          <Button
            type="submit"
            loading={pending}
            loadingText="正在保存"
            variant={rename ? "primary" : "danger"}
          >
            {rename ? "保存名称" : "删除记录"}
          </Button>
        </footer>
      </form>
    </Modal>
  );
}

function ApiKeyStatus({ value }: { value: ApiKey["status"] }) {
  const presentation = {
    Active: {
      icon: "success" as const,
      label: "有效",
      tone: "positive" as const,
    },
    Expired: {
      icon: "notice" as const,
      label: "已过期",
      tone: "notice" as const,
    },
    Revoked: { icon: "pause" as const, label: "已撤销", tone: "idle" as const },
  }[value];
  return <StatusBadge {...presentation} text={presentation.label} />;
}

function RevokeKeyModal({
  disabled,
  keyItem,
  onRevoke,
}: {
  disabled: boolean;
  keyItem: ApiKey;
  onRevoke: () => Promise<void>;
}) {
  const [open, setOpen] = useState(false);
  async function confirmRevoke() {
    await onRevoke();
    setOpen(false);
  }

  return (
    <Modal
      description="撤销立即生效且不可恢复。使用这份密钥的调用将被拒绝。"
      footer={
        <>
          <Button
            onClick={() => {
              setOpen(false);
            }}
          >
            取消
          </Button>
          <Button
            disabled={disabled}
            onClick={() => void confirmRevoke()}
            variant="danger"
          >
            撤销密钥
          </Button>
        </>
      }
      onOpenChange={setOpen}
      open={open}
      title={`撤销“${keyItem.name}”？`}
      trigger={
        <Button size="s" variant="ghost">
          撤销密钥
        </Button>
      }
    >
      <p className="pw-dialog-note">请确认这份 API 密钥已不再使用。</p>
    </Modal>
  );
}

export function CreateApiKeyModal({
  apiClient,
  personalScope,
  services,
  servicesUnavailable,
  initialServiceId,
  triggerHidden = false,
  onTestSucceeded,
}: {
  apiClient: ApiClient;
  initialServiceId?: string;
  triggerHidden?: boolean;
  onTestSucceeded?: () => void;
  personalScope: boolean;
  services: readonly { id: string; name: string; public_model_name: string }[];
  servicesUnavailable: boolean;
}) {
  const queryClient = useQueryClient();
  const creationAttempt = useRef(0);
  const secretRef = useRef<HTMLTextAreaElement>(null);
  const testAbortController = useRef<AbortController | undefined>(undefined);
  const testAttempt = useRef(0);
  const [open, setOpen] = useState(false);
  const nameInputId = useId();
  const expirationInputId = useId();
  const errorSummary = useRef<HTMLDivElement>(null);
  const {
    control,
    register,
    handleSubmit,
    reset,
    watch,
    formState: { errors },
  } = useForm<CreateApiKeyFormInput, unknown, CreateApiKeyFormOutput>({
    resolver: zodResolver(createApiKeyFormSchema),
    defaultValues: { name: "", expiration: "never", customDate: "" },
  });
  const expiration = watch("expiration");
  const [serviceIds, setServiceIds] = useState<Set<string>>(new Set());
  const [scope, setScope] = useState<"all" | "pick">("all");
  const [scopeError, setScopeError] = useState(false);
  const [testServiceId, setTestServiceId] = useState("");
  const [created, setCreated] = useState<CreatedApiKey>();
  const [pending, setPending] = useState(false);
  const [submitError, setSubmitError] = useState<ApiError>();
  const [confirmClose, setConfirmClose] = useState(false);
  const [copyFailed, setCopyFailed] = useState(false);
  const [testProgress, setTestProgress] = useState<FirstTestCallProgress>();
  const [testPending, setTestPending] = useState(false);

  useEffect(
    () => () => {
      creationAttempt.current += 1;
      testAttempt.current += 1;
      testAbortController.current?.abort();
    },
    [],
  );

  function clearSensitiveState() {
    testAttempt.current += 1;
    testAbortController.current?.abort();
    testAbortController.current = undefined;
    setCreated(undefined);
    setCopyFailed(false);
    setConfirmClose(false);
    setTestPending(false);
    setTestProgress(undefined);
  }

  function close() {
    creationAttempt.current += 1;
    setOpen(false);
    setPending(false);
    clearSensitiveState();
    reset();
    setServiceIds(new Set());
    setScopeError(false);
    setTestServiceId("");
    setSubmitError(undefined);
  }

  function changeOpen(nextOpen: boolean) {
    if (nextOpen) {
      setScope(initialServiceId === undefined ? "all" : "pick");
      setServiceIds(
        new Set(initialServiceId === undefined ? [] : [initialServiceId]),
      );
      setOpen(true);
      return;
    }
    if (created !== undefined) {
      setConfirmClose(true);
      return;
    }
    close();
  }

  async function submit(values: CreateApiKeyFormOutput) {
    if (pending) return;
    if (personalScope && (servicesUnavailable || services.length === 0)) return;
    if (personalScope && scope === "pick" && serviceIds.size === 0) {
      setScopeError(true);
      return;
    }
    const attempt = creationAttempt.current + 1;
    creationAttempt.current = attempt;
    setSubmitError(undefined);
    setPending(true);
    try {
      // Deliberately call the client directly: a mutation hook would retain the
      // one-time plaintext response in TanStack's MutationCache.
      const response = await apiClient.createApiKey({
        expires_at: values.expires_at,
        name: values.name,
        service_ids: personalScope && scope === "all" ? [] : [...serviceIds],
      });
      if (creationAttempt.current !== attempt) return;
      setCreated(response);
      setTestServiceId(
        initialServiceId ??
          services.find(
            (service) =>
              response.service_ids.length === 0 ||
              response.service_ids.includes(service.id),
          )?.id ??
          "",
      );
      void queryClient.invalidateQueries({ queryKey: API_KEYS_QUERY_KEY });
    } catch (error) {
      if (creationAttempt.current !== attempt) return;
      if (error instanceof ApiError) setSubmitError(error);
      else throw error;
    } finally {
      if (creationAttempt.current === attempt) setPending(false);
    }
  }

  async function testCall() {
    if (created === undefined || testService === undefined) return;
    const attempt = testAttempt.current + 1;
    testAttempt.current = attempt;
    testAbortController.current?.abort();
    const controller = new AbortController();
    testAbortController.current = controller;
    setTestPending(true);
    setTestProgress(undefined);
    try {
      const result = await runFirstTestCall({
        apiKey: created.api_key,
        targetModel: testService.public_model_name,
        onProgress: (progress) => {
          if (testAttempt.current === attempt) setTestProgress(progress);
        },
        signal: controller.signal,
      });
      if (testAttempt.current === attempt && result.status === "succeeded") {
        onTestSucceeded?.();
        void queryClient.invalidateQueries({
          queryKey: MY_AI_SERVICES_QUERY_KEY,
        });
      }
    } finally {
      if (testAttempt.current === attempt) {
        testAbortController.current = undefined;
        setTestPending(false);
      }
    }
  }

  const testServices = services.filter(
    (service) =>
      created !== undefined &&
      (created.service_ids.length === 0 ||
        created.service_ids.includes(service.id)),
  );
  const testService = (
    initialServiceId === undefined ? testServices : services
  ).find((service) => service.id === testServiceId);
  const examples = buildAccessExamples(
    window.location.origin,
    testService?.public_model_name ?? "<API 模型名>",
  );
  return (
    <Modal
      description={
        created === undefined
          ? "完整密钥创建后只显示一次。"
          : "请立即保存完整 API 密钥。"
      }
      onOpenChange={changeOpen}
      open={open}
      size="xl"
      title={created === undefined ? "创建密钥" : "保存 API 密钥"}
      trigger={
        <Button
          hidden={triggerHidden}
          style={triggerHidden ? { display: "none" } : undefined}
          disabled={
            personalScope && (servicesUnavailable || services.length === 0)
          }
          variant="primary"
        >
          {initialServiceId === undefined ? "创建密钥" : "创建 API 密钥"}
        </Button>
      }
    >
      {confirmClose ? (
        <div className="pw-secret-confirm">
          <InlineAlert title="关闭后无法再次查看" tone="notice">
            返回复制，或确认仍然关闭。
          </InlineAlert>
          <div className="pw-secret-confirm-actions">
            <Button
              onClick={() => {
                setConfirmClose(false);
              }}
              variant="primary"
            >
              返回复制
            </Button>
            <Button onClick={close} variant="danger">
              仍然关闭
            </Button>
          </div>
        </div>
      ) : null}
      {created === undefined ? (
        <form
          className="pw-service-form"
          noValidate
          onSubmit={(event) =>
            void handleSubmit(submit, () => {
              requestAnimationFrame(() => errorSummary.current?.focus());
            })(event)
          }
        >
          {Object.keys(errors).length > 0 ? (
            <div ref={errorSummary} role="alert" tabIndex={-1}>
              <p>API 密钥未创建，请修正以下内容。</p>
              {errors.name ? (
                <p>
                  <a href={`#${nameInputId}`}>{errors.name.message}</a>
                </p>
              ) : null}
              {errors.customDate ? (
                <p>
                  <a href={`#${expirationInputId}`}>
                    {errors.customDate.message}
                  </a>
                </p>
              ) : null}
            </div>
          ) : null}
          <Controller
            control={control}
            name="name"
            render={({ field }) => (
              <TextField
                id={nameInputId}
                label="名称"
                errorText={errors.name?.message}
                maxLength={40}
                minLength={2}
                onBlur={field.onBlur}
                onChange={field.onChange}
                name={field.name}
                placeholder="例如：开发环境"
                required
                value={field.value}
                disabled={pending}
              />
            )}
          />
          <label className="pw-service-field">
            <span>有效期</span>
            <select {...register("expiration")} disabled={pending}>
              <option value="never">永不过期</option>
              <option value="30">30 天</option>
              <option value="90">90 天</option>
              <option value="180">180 天</option>
              <option value="custom">自定义日期</option>
            </select>
          </label>
          {expiration === "custom" ? (
            <Controller
              control={control}
              name="customDate"
              render={({ field }) => (
                <TextField
                  id={expirationInputId}
                  helpText="按浏览器本地时间填写，须晚于当前时间。"
                  label="到期时间"
                  errorText={errors.customDate?.message}
                  onBlur={field.onBlur}
                  onChange={field.onChange}
                  name={field.name}
                  type="datetime-local"
                  required
                  value={field.value}
                  disabled={pending}
                />
              )}
            />
          ) : null}
          <fieldset className="pw-service-scope">
            <legend>{personalScope ? "允许服务" : "允许服务（可选）"}</legend>
            {personalScope ? (
              <div className="pw-service-scope__options">
                <label>
                  <input
                    type="radio"
                    checked={scope === "all"}
                    disabled={pending}
                    onChange={() => {
                      setScope("all");
                      setScopeError(false);
                    }}
                    name={`scope-${nameInputId}`}
                  />
                  全部已授权服务（含以后新授权的）
                </label>
                <label>
                  <input
                    type="radio"
                    checked={scope === "pick"}
                    disabled={pending}
                    onChange={() => {
                      setScope("pick");
                    }}
                    name={`scope-${nameInputId}`}
                  />
                  指定服务
                </label>
                {scope === "pick"
                  ? services.map((service) => (
                      <label key={service.id}>
                        <input
                          type="checkbox"
                          checked={serviceIds.has(service.id)}
                          disabled={pending}
                          onChange={() => {
                            setServiceIds((current) => {
                              const next = new Set(current);
                              if (next.has(service.id)) next.delete(service.id);
                              else next.add(service.id);
                              return next;
                            });
                            setScopeError(false);
                          }}
                        />
                        <span>
                          {service.name} · {service.public_model_name}
                        </span>
                      </label>
                    ))
                  : null}
                {scopeError ? (
                  <span role="alert">至少勾选一个服务。</span>
                ) : null}
              </div>
            ) : servicesUnavailable ? (
              <InlineAlert title="暂时不能限定服务" tone="notice">
                服务候选项不可用；本次可创建适用于全部服务的密钥。
              </InlineAlert>
            ) : services.length === 0 ? (
              <span className="pw-service-scope__empty">
                当前没有可选的 AI 服务。
              </span>
            ) : (
              <div className="pw-service-scope__options">
                {services.map((service) => (
                  <label key={service.id}>
                    <input
                      checked={serviceIds.has(service.id)}
                      onChange={() => {
                        setServiceIds((current) => {
                          const next = new Set(current);
                          if (next.has(service.id)) next.delete(service.id);
                          else next.add(service.id);
                          return next;
                        });
                      }}
                      type="checkbox"
                    />
                    <span>
                      {service.name} · {service.public_model_name}
                    </span>
                  </label>
                ))}
              </div>
            )}
          </fieldset>
          {submitError === undefined ? null : (
            <InlineAlert title="API 密钥未创建" tone="negative">
              {submitError.code === "service_not_authorized"
                ? "当前账号没有相应服务权限，API 密钥未创建，请联系管理员授权后重试。"
                : (submitError.responseMessage ?? "请检查填写内容后重试。")}
            </InlineAlert>
          )}
          <footer className="pw-service-form__actions">
            <Button onClick={close}>取消</Button>
            <Button
              loading={pending}
              loadingText="正在创建"
              type="submit"
              variant="primary"
            >
              创建密钥
            </Button>
          </footer>
        </form>
      ) : (
        <div className="pw-secret-result" hidden={confirmClose}>
          <InlineAlert title="API 密钥已创建" tone="positive">
            完整密钥仅在此弹层显示。关闭后无法再次查看。
          </InlineAlert>
          <section className="pw-secret-block" aria-label="完整 API 密钥">
            <label htmlFor={`api-key-${created.id}`}>完整 API 密钥</label>
            <textarea
              id={`api-key-${created.id}`}
              onFocus={(event) => {
                event.currentTarget.select();
              }}
              readOnly
              ref={secretRef}
              rows={2}
              value={created.api_key}
            />
            <CopyControl
              label="复制 API 密钥"
              persistCopied={personalScope}
              onCopyError={() => {
                setCopyFailed(true);
              }}
              value={created.api_key}
              variant="secondary"
            />
            {copyFailed ? (
              <span className="pw-secret-copy-fallback" role="status">
                自动复制失败。请
                <button
                  className="pw-inline-link"
                  onClick={() => {
                    secretRef.current?.focus();
                    secretRef.current?.select();
                  }}
                  type="button"
                >
                  全选密钥
                </button>
                后手动复制。
              </span>
            ) : null}
          </section>
          <section
            aria-labelledby={`api-key-test-${created.id}`}
            className="pw-code-example"
          >
            <header>
              <span>
                <h3 id={`api-key-test-${created.id}`}>测试调用</h3>
                <p>
                  使用当前 API
                  密钥发起最小调用；调用正常计入用量，不显示响应正文。
                </p>
              </span>
              <Button
                disabled={
                  testProgress?.status === "succeeded" ||
                  testService === undefined
                }
                loading={testPending}
                loadingText="正在测试"
                onClick={() => void testCall()}
                variant="secondary"
              >
                {testProgress?.status === "failed" ? "重新测试" : "测试调用"}
              </Button>
            </header>
            {personalScope ? (
              initialServiceId === undefined && testServices.length > 1 ? (
                <label className="pw-service-field">
                  <span>测试的服务</span>
                  <select
                    aria-label="测试的服务"
                    disabled={testPending}
                    value={testServiceId}
                    onChange={(event) => {
                      setTestServiceId(event.target.value);
                      setTestProgress(undefined);
                    }}
                  >
                    {testServices.map((service) => (
                      <option key={service.id} value={service.id}>
                        {service.name} · {service.public_model_name}
                      </option>
                    ))}
                  </select>
                </label>
              ) : (
                <span>
                  服务：
                  {testService === undefined
                    ? "—"
                    : `${testService.name} · ${testService.public_model_name}`}
                </span>
              )
            ) : null}
            {testProgress === undefined ? null : (
              <div
                aria-live={
                  testProgress.status === "failed" ? "assertive" : "polite"
                }
                className="pw-secret-result"
              >
                <ProgressStages
                  label="测试调用结果"
                  percent={testProgress.percent}
                  stages={testProgress.stages}
                />
                {testProgress.status === "succeeded" ? (
                  <InlineAlert title="测试调用成功" tone="positive">
                    四段检查均已通过，模型响应已返回
                    Token。响应正文未显示，也不会保留在页面中。
                    {testProgress.requestId === undefined ? null : (
                      <p>
                        Request ID：<code>{testProgress.requestId}</code>
                      </p>
                    )}
                  </InlineAlert>
                ) : testProgress.failure === undefined ? null : (
                  <InlineAlert
                    title={`${
                      testProgress.stages.find(
                        ({ id }) => id === testProgress.failure?.stage,
                      )?.label ?? "测试调用"
                    }未通过`}
                    tone="negative"
                  >
                    {testProgress.failure.conclusion}
                    {testProgress.failure.code === undefined ? null : (
                      <p>
                        错误码：<code>{testProgress.failure.code}</code>
                      </p>
                    )}
                    <p>
                      Request ID：<code>{testProgress.failure.requestId}</code>
                    </p>
                  </InlineAlert>
                )}
              </div>
            )}
          </section>
          <CodeExample label="curl" value={examples.curl} />
          <CodeExample label="OpenAI Python SDK" value={examples.python} />
          <footer className="pw-service-form__actions">
            <Button onClick={close} variant="primary">
              我已保存此密钥
            </Button>
          </footer>
        </div>
      )}
    </Modal>
  );
}

function formatDate(value: string) {
  return new Intl.DateTimeFormat("zh-CN", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}
