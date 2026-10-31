import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useEffect, useId, useState, type ReactNode } from "react";
import {
  Controller,
  useForm,
  type UseFormRegisterReturn,
} from "react-hook-form";
import { useNavigate } from "react-router";
import { z } from "zod";

import { buildAccessExamples } from "../api/access-examples";
import {
  PUBLIC_MODEL_NAME_PATTERN,
  serviceDisplayNameSchema,
  type AiService,
  type CreateAiServiceRequest,
} from "../api/ai-service-contracts";
import { ApiError, type ApiClient } from "../api/client";
import type { ModelInstance } from "../api/model-instance-contracts";
import {
  Button,
  CodeExample,
  Icon,
  InlineAlert,
  Modal,
  StatusBadge,
  TextField,
  type IconName,
} from "../components";
import { HELP_TOPICS } from "../help/content";
import { HelpButton } from "../help/help-center";
import { USERS_QUERY_KEY, useUsers } from "../users/queries";
import { servicePermissionsQueryKey } from "../users/service-permissions";
import { PUBLIC_MODEL_NAME_HELP } from "./public-model-name-copy";

// JavaScript $ also matches before a final newline; require the actual end.
const PUBLIC_MODEL_NAME = new RegExp(
  `^${PUBLIC_MODEL_NAME_PATTERN}$(?![\\s\\S])`,
);

const createServiceFormSchema = z
  .object({
    instanceId: z.string().min(1, "请选择一个运行中的模型实例"),
    publicModelName: z
      .string()
      .regex(
        PUBLIC_MODEL_NAME,
        "API 模型名需为 2～64 位小写字母、数字、点、下划线或连字符，例如 qwen-chat",
      ),
    name: z
      .string()
      .refine(
        (value) =>
          value.trim() === "" ||
          serviceDisplayNameSchema.safeParse(value).success,
        "服务名称需为 2～40 个字符；留空则使用 API 模型名",
      ),
    description: z.string().max(200),
    access: z.enum(["none", "some"]),
    userIds: z.array(z.string()),
    initialState: z.enum(["enabled", "disabled"]),
  })
  .refine((value) => value.access === "none" || value.userIds.length > 0, {
    message: "请至少选择一位用户，或选择「暂不授权」",
    path: ["userIds"],
  });

type CreateServiceForm = z.infer<typeof createServiceFormSchema>;

const EMPTY_FORM: CreateServiceForm = {
  access: "none",
  description: "",
  initialState: "enabled",
  instanceId: "",
  name: "",
  publicModelName: "",
  userIds: [],
};

interface CreateResult {
  instanceName: string;
  permissionSaved: boolean;
  service: AiService;
  userIds: readonly string[];
}

type Mode =
  | { kind: "form"; fromInstance?: ModelInstance }
  | {
      kind: "prerequisite";
      requested?: { id: string; instance?: ModelInstance };
    }
  | { kind: "result"; result: CreateResult };

const INSTANCE_ERROR_CODES = new Set([
  "invalid_instance_state",
  "instance_not_found",
]);

/** create a service from a Running instance, then optionally grant users. */
export function CreateServiceModal({
  apiClient,
  instances,
  instancesLoaded,
  services,
  preselectInstanceId,
  onPreselectHandled,
  onCreated,
}: {
  apiClient: ApiClient;
  instances: readonly ModelInstance[];
  instancesLoaded: boolean;
  services: readonly AiService[];
  preselectInstanceId?: string;
  onPreselectHandled?: () => void;
  onCreated: (service: AiService) => Promise<void> | void;
}) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [mode, setMode] = useState<Mode>({ kind: "form" });
  const [submitError, setSubmitError] = useState<ApiError>();
  const [retrying, setRetrying] = useState(false);
  const {
    control,
    formState: { errors },
    handleSubmit,
    register,
    reset,
    watch,
  } = useForm<CreateServiceForm>({
    defaultValues: EMPTY_FORM,
    resolver: zodResolver(createServiceFormSchema),
  });
  const create = useMutation({
    mutationFn: (body: CreateAiServiceRequest) =>
      apiClient.createAiService(body),
  });

  const running = instances.filter(({ status }) => status === "Running");

  function openWith(requestedId?: string) {
    setSubmitError(undefined);
    if (requestedId !== undefined) {
      const instance = instances.find(({ id }) => id === requestedId);
      if (instance?.status === "Running") {
        reset({ ...EMPTY_FORM, instanceId: instance.id });
        setMode({ fromInstance: instance, kind: "form" });
      } else {
        setMode({
          kind: "prerequisite",
          requested: { id: requestedId, instance },
        });
      }
    } else if (instancesLoaded && running.length === 0) {
      setMode({ kind: "prerequisite" });
    } else {
      reset(EMPTY_FORM);
      setMode({ kind: "form" });
    }
    setOpen(true);
  }

  function close() {
    setOpen(false);
    setSubmitError(undefined);
    reset(EMPTY_FORM);
    create.reset();
  }

  useEffect(() => {
    if (preselectInstanceId === undefined) return;
    openWith(preselectInstanceId);
    onPreselectHandled?.();
    // Only a new instance request should reopen the dialog.
  }, [preselectInstanceId]);

  async function savePermissions(
    serviceId: string,
    userIds: readonly string[],
  ) {
    try {
      await apiClient.replaceServicePermissions(serviceId, userIds);
    } catch {
      return false;
    }
    await Promise.all([
      queryClient.invalidateQueries({
        queryKey: servicePermissionsQueryKey(serviceId),
      }),
      queryClient.invalidateQueries({ queryKey: USERS_QUERY_KEY }),
    ]);
    return true;
  }

  async function submit(values: CreateServiceForm) {
    setSubmitError(undefined);
    const name = values.name.trim();
    let service: AiService;
    try {
      // an empty name is omitted so the server uses the API model name.
      service = await create.mutateAsync({
        ...(name === "" ? {} : { name }),
        description: values.description,
        enabled: values.initialState === "enabled",
        primary_instance_id: values.instanceId,
        public_model_name: values.publicModelName,
      });
    } catch (error) {
      if (error instanceof ApiError) {
        setSubmitError(error);
        return;
      }
      throw error;
    }
    await onCreated(service);
    // Authorization is a second request; its failure must not undo or repeat creation.
    const userIds = values.access === "some" ? values.userIds : [];
    const permissionSaved =
      userIds.length === 0 || (await savePermissions(service.id, userIds));
    setMode({
      kind: "result",
      result: {
        instanceName:
          instances.find(({ id }) => id === values.instanceId)?.name ??
          "模型实例",
        permissionSaved,
        service,
        userIds,
      },
    });
  }

  async function retryPermissions(result: CreateResult) {
    setRetrying(true);
    const permissionSaved = await savePermissions(
      result.service.id,
      result.userIds,
    );
    setRetrying(false);
    setMode({ kind: "result", result: { ...result, permissionSaved } });
  }

  function go(path: string) {
    close();
    void navigate(path);
  }

  const values = watch();
  const errorCode = submitError?.code;
  const publicModelNameError =
    errors.publicModelName?.message ??
    (errorCode === "service_name_conflict" ? "API 模型名已被使用" : undefined);
  const nameError =
    errors.name?.message ??
    (errorCode === "service_display_name_conflict"
      ? "服务名称已被使用"
      : undefined);
  const instanceError =
    errors.instanceId?.message ??
    (errorCode !== undefined && INSTANCE_ERROR_CODES.has(errorCode)
      ? (submitError?.responseMessage ??
        "模型实例未在运行，AI 服务未创建，请选择运行中的实例")
      : undefined);
  const fieldLevelError =
    publicModelNameError !== undefined ||
    nameError !== undefined ||
    instanceError !== undefined;

  const title =
    mode.kind === "prerequisite"
      ? "还不能创建 AI 服务"
      : mode.kind === "result"
        ? "AI 服务已创建"
        : "创建服务";

  return (
    <Modal
      description={
        mode.kind === "form"
          ? "把一个运行中的模型实例发布为用户可调用的 AI 服务。"
          : undefined
      }
      onOpenChange={(next) => {
        if (next) openWith();
        else close();
      }}
      open={open}
      size={mode.kind === "form" ? "xl" : mode.kind === "result" ? "l" : "m"}
      title={title}
      trigger={<Button variant="primary">创建服务</Button>}
    >
      {mode.kind === "prerequisite" ? (
        <Prerequisite
          instances={instances}
          onCancel={close}
          onGo={go}
          requested={mode.requested}
        />
      ) : mode.kind === "result" ? (
        <CreatedResult
          onDone={close}
          onGo={go}
          onRetry={(result) => void retryPermissions(result)}
          result={mode.result}
          retrying={retrying}
        />
      ) : (
        <form
          className="pw-create-service"
          noValidate
          onSubmit={(event) => void handleSubmit(submit)(event)}
        >
          <div className="pw-create-service__fields">
            <HelpButton topic={HELP_TOPICS.publishing.id}>
              查看服务发布帮助
            </HelpButton>
            <InstanceField
              error={instanceError}
              fromInstance={mode.fromInstance}
              loading={!instancesLoaded}
              register={register("instanceId")}
              running={running}
              shared={
                values.instanceId !== "" &&
                services.some(
                  (service) =>
                    service.primary_instance_id === values.instanceId,
                )
              }
            />
            <Controller
              control={control}
              name="publicModelName"
              render={({ field }) => (
                <TextField
                  errorText={publicModelNameError}
                  helpText={`${PUBLIC_MODEL_NAME_HELP}更换后端实例时保持不变。2～64 位小写字母、数字、点、下划线或连字符，系统内唯一。`}
                  label="API 模型名"
                  maxLength={64}
                  onBlur={field.onBlur}
                  onChange={(event) => {
                    if (errorCode === "service_name_conflict")
                      setSubmitError(undefined);
                    field.onChange(event);
                  }}
                  placeholder="例如 qwen-chat"
                  required
                  spellCheck={false}
                  value={field.value}
                />
              )}
            />
            <Controller
              control={control}
              name="name"
              render={({ field }) => (
                <TextField
                  errorText={nameError}
                  helpText="在管理页面与用户的服务列表中显示，2～40 个字符，系统内唯一。留空时使用 API 模型名。"
                  label="服务名称（选填）"
                  onBlur={field.onBlur}
                  onChange={(event) => {
                    if (errorCode === "service_display_name_conflict")
                      setSubmitError(undefined);
                    field.onChange(event);
                  }}
                  placeholder={
                    values.publicModelName.trim() === ""
                      ? "留空则使用 API 模型名"
                      : `留空则为 ${values.publicModelName.trim()}`
                  }
                  value={field.value}
                />
              )}
            />
            <label className="pw-service-field">
              <span>描述（选填）</span>
              <textarea
                maxLength={200}
                placeholder="说明适用场景，例如「通用中文对话与摘要」"
                rows={3}
                {...register("description")}
              />
              <span>{Array.from(values.description).length}/200</span>
            </label>
            <div className="pw-create-service__readonly">
              <span className="pw-create-service__label">服务类型</span>
              <span>
                Chat Completions{" "}
                <span className="pw-create-service__hint">
                  · 当前版本只提供这一种
                </span>
              </span>
            </div>
            <fieldset
              aria-describedby={
                errors.userIds ? "pw-create-service-users-error" : undefined
              }
              className="pw-create-service__choices"
            >
              <legend>访问权限</legend>
              <label>
                <input type="radio" value="none" {...register("access")} />
                暂不授权（之后在服务详情的「访问权限」中授权）
              </label>
              <label>
                <input type="radio" value="some" {...register("access")} />
                现在授权给指定用户
              </label>
              {values.access === "some" ? (
                <Controller
                  control={control}
                  name="userIds"
                  render={({ field }) => (
                    <UserChoices
                      apiClient={apiClient}
                      onChange={field.onChange}
                      value={field.value}
                    />
                  )}
                />
              ) : null}
              {errors.userIds?.message ? (
                <span
                  className="pw-create-service__error"
                  id="pw-create-service-users-error"
                  role="alert"
                >
                  {errors.userIds.message}
                </span>
              ) : null}
            </fieldset>
            <fieldset className="pw-create-service__choices">
              <legend>初始状态</legend>
              <label>
                <input
                  type="radio"
                  value="enabled"
                  {...register("initialState")}
                />
                启用：创建后立即可调用
              </label>
              <label>
                <input
                  type="radio"
                  value="disabled"
                  {...register("initialState")}
                />
                停用：先创建，稍后再启用
              </label>
            </fieldset>
            {submitError !== undefined && !fieldLevelError ? (
              <InlineAlert title="AI 服务未创建" tone="negative">
                {submitError.responseMessage ?? "请检查填写内容后重试。"}
              </InlineAlert>
            ) : null}
          </div>
          <ServicePreview
            description={values.description}
            enabled={values.initialState === "enabled"}
            name={values.name}
            publicModelName={values.publicModelName}
          />
          <footer className="pw-service-form__actions pw-create-service__actions">
            <Button onClick={close}>取消</Button>
            <Button
              disabled={values.instanceId === ""}
              loading={create.isPending}
              loadingText="正在创建"
              type="submit"
              variant="primary"
            >
              创建服务
            </Button>
          </footer>
        </form>
      )}
    </Modal>
  );
}

function InstanceField({
  error,
  fromInstance,
  loading,
  register,
  running,
  shared,
}: {
  error?: string;
  fromInstance?: ModelInstance;
  loading: boolean;
  register: UseFormRegisterReturn<"instanceId">;
  running: readonly ModelInstance[];
  shared: boolean;
}) {
  const id = useId();
  const helpId = `${id}-help`;
  const errorId = `${id}-error`;
  return (
    <div className="pw-service-field">
      <label htmlFor={id}>
        后端实例 <span aria-hidden="true">必填</span>
      </label>
      <select
        aria-describedby={[error ? errorId : undefined, helpId]
          .filter(Boolean)
          .join(" ")}
        aria-invalid={error !== undefined || undefined}
        id={id}
        required
        {...register}
      >
        <option value="">
          {loading ? "正在加载模型实例" : "请选择运行中的模型实例"}
        </option>
        {running.map((instance) => (
          <option key={instance.id} value={instance.id}>
            {instance.name}
          </option>
        ))}
      </select>
      {error ? (
        <span className="pw-create-service__error" id={errorId}>
          {error}
        </span>
      ) : null}
      <span className="pw-create-service__hint" id={helpId}>
        {fromInstance
          ? `从模型实例「${fromInstance.name}」进入，已预选这个实例。`
          : ""}
        只列出运行中的实例。
        {shared
          ? "这个实例已在为其他服务提供推理，多个服务会共享它的算力。"
          : ""}
      </span>
    </div>
  );
}

function UserChoices({
  apiClient,
  onChange,
  value,
}: {
  apiClient: ApiClient;
  onChange: (value: string[]) => void;
  value: readonly string[];
}) {
  const users = useUsers(apiClient, { status: "active" });
  if (users.isError)
    return (
      <InlineAlert title="无法获取用户列表" tone="negative">
        现在无法选择用户。可以先选择「暂不授权」创建服务，之后在服务详情中授权。
      </InlineAlert>
    );
  if (users.isPending)
    return (
      <span className="pw-create-service__hint" role="status">
        正在加载活跃用户
      </span>
    );
  return (
    <div className="pw-create-service__users">
      {users.data.items.length === 0 ? (
        <span className="pw-create-service__hint">当前没有活跃用户。</span>
      ) : (
        users.data.items.map((user) => (
          <label key={user.id}>
            <input
              checked={value.includes(user.id)}
              onChange={() => {
                onChange(
                  value.includes(user.id)
                    ? value.filter((item) => item !== user.id)
                    : [...value, user.id],
                );
              }}
              type="checkbox"
            />
            <span>
              {user.display_name || user.username} (@{user.username})
            </span>
          </label>
        ))
      )}
      <span className="pw-create-service__hint">
        只列出活跃用户。用户仍需自行创建 API 密钥。
      </span>
    </div>
  );
}

function ServicePreview({
  description,
  enabled,
  name,
  publicModelName,
}: {
  description: string;
  enabled: boolean;
  name: string;
  publicModelName: string;
}) {
  const model = publicModelName.trim() || "your-model-name";
  const examples = buildAccessExamples(window.location.origin, model);
  return (
    <div className="pw-create-service__preview">
      <span className="pw-create-service__label">预览 · 已授权用户将看到</span>
      <section aria-label="服务预览" className="pw-create-service__card">
        <header>
          <strong>{name.trim() || publicModelName.trim() || "服务名称"}</strong>
          <StatusBadge
            icon={enabled ? "success" : "pause"}
            text={enabled ? "可用" : "已停用"}
            tone={enabled ? "positive" : "idle"}
          />
        </header>
        {description ? <p>{description}</p> : null}
        <dl>
          <dt>API 模型名</dt>
          <dd>
            <code>{model}</code>
          </dd>
          <dt>Endpoint</dt>
          <dd className="pw-endpoint-rows">
            <div className="pw-endpoint-row">
              <span>OpenAI 兼容</span>
              <code>{examples.baseUrl}</code>
            </div>
            <div className="pw-endpoint-row">
              <span>Anthropic 兼容</span>
              <code>{examples.anthropicBaseUrl}</code>
            </div>
          </dd>
          <dt>服务类型</dt>
          <dd>Chat Completions</dd>
        </dl>
        <CodeExample label="curl" value={examples.curl} />
      </section>
      <span className="pw-create-service__hint">
        用户看不到后端实例、GPU 与 Runtime。
      </span>
    </div>
  );
}

function Prerequisite({
  instances,
  onCancel,
  onGo,
  requested,
}: {
  instances: readonly ModelInstance[];
  onCancel: () => void;
  onGo: (path: string) => void;
  requested?: { id: string; instance?: ModelInstance };
}) {
  const notRunning = instances.filter(({ status }) => status !== "Running");
  const reason =
    requested === undefined
      ? notRunning.length > 0
        ? `当前 ${String(notRunning.length)} 个模型实例都没有运行，可以在模型实例中启动，或部署新的模型。`
        : "当前还没有模型实例，请先部署模型。"
      : requested.instance === undefined
        ? "要发布的模型实例已不存在，请刷新模型实例列表，或部署新的模型。"
        : `模型实例「${requested.instance.name}」当前没有运行，可以在模型实例中启动它，或部署新的模型。`;
  const instancesPath =
    requested?.instance === undefined
      ? "/models"
      : `/models/instances/${requested.instance.id}`;
  return (
    <div className="pw-create-service__prerequisite">
      <p>
        AI 服务需要绑定一个<strong>运行中</strong>的模型实例。{reason}
      </p>
      <footer className="pw-service-form__actions pw-create-service__actions">
        <Button onClick={onCancel}>取消</Button>
        {notRunning.length > 0 ? (
          <Button
            onClick={() => {
              onGo(instancesPath);
            }}
          >
            前往模型实例
          </Button>
        ) : null}
        <Button
          onClick={() => {
            onGo("/models/deploy");
          }}
          variant="primary"
        >
          部署模型
        </Button>
      </footer>
    </div>
  );
}

function CreatedResult({
  onDone,
  onGo,
  onRetry,
  result,
  retrying,
}: {
  onDone: () => void;
  onGo: (path: string) => void;
  onRetry: (result: CreateResult) => void;
  result: CreateResult;
  retrying: boolean;
}) {
  const { instanceName, permissionSaved, service, userIds } = result;
  const authorized = permissionSaved && userIds.length > 0;
  const servicePath = `/services/${service.id}`;
  const keyNotice =
    "用户需要自行创建 API 密钥，管理员不能代为创建，也无法查看密钥明文。";
  return (
    <div className="pw-create-service__result">
      <dl>
        <dt>服务名称</dt>
        <dd>{service.name}</dd>
        <dt>API 模型名</dt>
        <dd>
          <code>{service.public_model_name}</code>
        </dd>
        <dt>后端实例</dt>
        <dd>{instanceName}</dd>
        <dt>状态</dt>
        <dd>{service.enabled ? "已启用" : "已停用"}</dd>
      </dl>
      <ul aria-label="后续结果" className="pw-create-service__outcomes">
        <Outcome
          icon={service.enabled ? "success" : "pause"}
          tone={service.enabled ? "positive" : "idle"}
        >
          {service.enabled ? "服务已可用。" : "服务已停用，启用后才可调用。"}
        </Outcome>
        {!permissionSaved ? (
          <Outcome icon="error" tone="negative">
            服务已创建，但用户授权未保存。当前尚无用户可以访问，请重试授权，或在服务详情的「访问权限」中授权。
          </Outcome>
        ) : authorized ? (
          <Outcome icon="success" tone="positive">
            已授权 {String(userIds.length)} 位用户，他们可以用 API 模型名「
            {service.public_model_name}」调用。<strong>{keyNotice}</strong>
          </Outcome>
        ) : (
          <Outcome icon="notice" tone="notice">
            服务已发布，但尚无用户可以访问。在「访问权限」中授权后，用户即可调用。
            <strong>{keyNotice}</strong>
          </Outcome>
        )}
      </ul>
      <HelpButton topic={HELP_TOPICS.publishing.id}>
        查看服务发布帮助
      </HelpButton>
      <footer className="pw-service-form__actions pw-create-service__actions">
        <Button onClick={onDone}>完成</Button>
        {!permissionSaved ? (
          <Button
            loading={retrying}
            loadingText="正在授权"
            onClick={() => {
              onRetry(result);
            }}
          >
            重试授权
          </Button>
        ) : null}
        <Button
          onClick={() => {
            onGo(servicePath);
          }}
          variant="primary"
        >
          {authorized ? "查看服务" : "前往授权"}
        </Button>
      </footer>
    </div>
  );
}

function Outcome({
  children,
  icon,
  tone,
}: {
  children: ReactNode;
  icon: IconName;
  tone: "positive" | "notice" | "negative" | "idle";
}) {
  return (
    <li
      className={`pw-create-service__outcome pw-create-service__outcome--${tone}`}
    >
      <Icon name={icon} size="s" />
      <span>{children}</span>
    </li>
  );
}
