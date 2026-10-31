import { zodResolver } from "@hookform/resolvers/zod";
import { useQueryClient } from "@tanstack/react-query";
import { Controller, useForm } from "react-hook-form";
import { useRef, useState } from "react";
import { z } from "zod";

import { ApiError, type ApiClient } from "../api/client";
import {
  ModelSourceContractError,
  type ModelSourceResponse,
  type ModelSourceType,
} from "../api/model-source-contracts";
import {
  Button,
  InlineAlert,
  Modal,
  StatusBadge,
  TextField,
} from "../components";
import { formatObservedAt } from "../system/formatting";
import {
  modelSourceConfigurationPresentation,
  modelSourceTypeLabel,
  modelSourceValidationActionLabel,
  modelSourceValidationPresentation,
  modelSourceValidationReasonLabel,
} from "./presentation";
import { MODEL_SOURCES_QUERY_KEY, useModelSourcesQuery } from "./queries";

const credentialFormSchema = z.strictObject({
  credential: z
    .string()
    .min(1, "请输入新的 Access Token")
    .max(4096, "Access Token 最多 4096 个字符"),
  currentPassword: z
    .string()
    .min(1, "请输入管理员密码")
    .max(4096, "管理员密码最多 4096 个字符"),
});

const removalFormSchema = z.strictObject({
  currentPassword: z
    .string()
    .min(1, "请输入管理员密码")
    .max(4096, "管理员密码最多 4096 个字符"),
});

type CredentialFields = z.infer<typeof credentialFormSchema>;
type RemovalFields = z.infer<typeof removalFormSchema>;
type CredentialAction = "configure" | "replace" | "remove";

export function ModelSourcesTab({ apiClient }: { apiClient: ApiClient }) {
  const query = useModelSourcesQuery(apiClient);
  const queryClient = useQueryClient();
  const [announcement, setAnnouncement] = useState("");

  if (query.error instanceof ModelSourceContractError) throw query.error;

  if (
    query.data === undefined &&
    query.error instanceof ApiError &&
    query.error.status === 403
  ) {
    return (
      <section className="pw-model-sources" role="alert">
        <InlineAlert title="无权限查看模型源" tone="negative">
          当前账号无权读取或配置模型源，页面数据无法显示。请联系管理员授予权限后重试。
        </InlineAlert>
      </section>
    );
  }

  if (query.data === undefined && query.isError) {
    return (
      <section className="pw-model-sources">
        <InlineAlert title="无法获取模型源" tone="negative">
          <span>模型源配置无法显示。请检查平台连接后重试。</span>
          <Button onClick={() => void query.refetch()} size="s">
            重新加载
          </Button>
        </InlineAlert>
      </section>
    );
  }

  if (query.data === undefined) return <ModelSourcesLoading />;

  function updateCachedSource(updated: ModelSourceResponse) {
    queryClient.setQueryData<ModelSourceResponse[]>(
      MODEL_SOURCES_QUERY_KEY,
      (current) => {
        if (current === undefined) throw new ModelSourceContractError();
        let matches = 0;
        const next = current.map((item) => {
          if (item.source_type !== updated.source_type) return item;
          matches += 1;
          return updated;
        });
        if (matches !== 1) throw new ModelSourceContractError();
        return next;
      },
    );
    setAnnouncement(
      `${modelSourceTypeLabel(updated.source_type)}配置已更新，${validationAnnouncement(updated)}`,
    );
  }

  return (
    <section className="pw-model-sources">
      <span aria-live="polite" className="pw-visually-hidden">
        {announcement}
      </span>
      <InlineAlert title="凭证只在模型源中配置" tone="info">
        部署与下载流程不接收明文凭证。保存前先验证；暂时无法验证时，可确认以未验证状态保存。
      </InlineAlert>
      <div className="pw-model-sources__list">
        {query.data.map((source) => (
          <ModelSourceCard
            apiClient={apiClient}
            key={source.source_type}
            onConflict={() => query.refetch()}
            onUpdated={updateCachedSource}
            source={source}
          />
        ))}
      </div>
    </section>
  );
}

function ModelSourcesLoading() {
  return (
    <section
      aria-busy="true"
      aria-label="模型源正在加载"
      className="pw-model-sources"
    >
      <span className="sk pw-model-sources__notice-skeleton" />
      <div className="pw-model-sources__list">
        <SourceCardSkeleton />
        <SourceCardSkeleton />
      </div>
    </section>
  );
}

function SourceCardSkeleton() {
  return (
    <section className="pw-model-source-card">
      <span className="sk pw-model-source-card__title-skeleton" />
      <span className="sk pw-model-source-card__line-skeleton" />
      <span className="sk pw-model-source-card__line-skeleton" />
    </section>
  );
}

function ModelSourceCard({
  apiClient,
  onConflict,
  onUpdated,
  source,
}: {
  apiClient: ApiClient;
  onConflict: () => Promise<unknown>;
  onUpdated: (updated: ModelSourceResponse) => void;
  source: ModelSourceResponse;
}) {
  const configuration = modelSourceConfigurationPresentation(
    source.configuration_state,
  );
  const configured = source.configuration_state !== "unconfigured";

  return (
    <article
      aria-label={`${modelSourceTypeLabel(source.source_type)} 模型源`}
      className="pw-model-source-card"
    >
      <header className="pw-model-source-card__head">
        <span className="pw-model-source-card__identity">
          <h2>{modelSourceTypeLabel(source.source_type)}</h2>
          <StatusBadge
            icon={configuration.icon}
            text={configuration.label}
            tone={configuration.tone}
          />
        </span>
        <span className="pw-model-source-card__actions">
          {configured ? (
            <>
              <CredentialActionModal
                action="replace"
                apiClient={apiClient}
                onConflict={onConflict}
                onUpdated={onUpdated}
                sourceType={source.source_type}
              />
              <CredentialActionModal
                action="remove"
                apiClient={apiClient}
                onConflict={onConflict}
                onUpdated={onUpdated}
                sourceType={source.source_type}
              />
            </>
          ) : (
            <CredentialActionModal
              action="configure"
              apiClient={apiClient}
              onConflict={onConflict}
              onUpdated={onUpdated}
              sourceType={source.source_type}
            />
          )}
        </span>
      </header>
      {source.configuration_state === "unconfigured" ? (
        <p className="pw-model-source-card__unconfigured">
          公开仓库不需要凭证；访问私有仓库时再配置即可。
        </p>
      ) : (
        <ModelSourceDetails source={source} />
      )}
    </article>
  );
}

function ModelSourceDetails({ source }: { source: ModelSourceResponse }) {
  return (
    <div className="pw-model-source-card__details">
      {source.configuration_state === "key_unavailable" ? (
        <InlineAlert title="平台无法解开已保存的凭证" tone="negative">
          私有仓库的读取与下载会失败。请联系负责平台部署的管理员检查加密密钥。
        </InlineAlert>
      ) : null}
      <dl>
        {source.configuration_state === "configured" ? (
          <div>
            <dt>凭证</dt>
            <dd>
              {source.credential_last4 === null ? (
                "凭证较短，不显示尾部标识"
              ) : (
                <span className="pw-system-technical">
                  尾部 {source.credential_last4}
                </span>
              )}
            </dd>
          </div>
        ) : null}
        <div>
          <dt>验证结论</dt>
          <dd>
            <ValidationResult source={source} />
          </dd>
        </div>
        {source.verified_at === null ? null : (
          <div>
            <dt>验证时间</dt>
            <dd>
              <time dateTime={source.verified_at}>
                {formatObservedAt(source.verified_at)}
              </time>
            </dd>
          </div>
        )}
        {source.verification_reason_code === null ? null : (
          <div>
            <dt>原因</dt>
            <dd>
              {modelSourceValidationReasonLabel(
                source.verification_reason_code,
              )}
            </dd>
          </div>
        )}
        {source.next_action === null ? null : (
          <div>
            <dt>处置</dt>
            <dd>{modelSourceValidationActionLabel(source.next_action)}</dd>
          </div>
        )}
      </dl>
    </div>
  );
}

function ValidationResult({ source }: { source: ModelSourceResponse }) {
  if (source.verification_status === null) {
    return <StatusBadge icon="info" text="未验证" tone="idle" />;
  }
  const validation = modelSourceValidationPresentation(
    source.verification_status,
  );
  return (
    <StatusBadge
      icon={validation.icon}
      text={validation.label}
      tone={validation.tone}
    />
  );
}

function CredentialActionModal({
  action,
  apiClient,
  onConflict,
  onUpdated,
  sourceType,
}: {
  action: CredentialAction;
  apiClient: ApiClient;
  onConflict: () => Promise<unknown>;
  onUpdated: (updated: ModelSourceResponse) => void;
  sourceType: ModelSourceType;
}) {
  const [open, setOpen] = useState(false);
  const sourceLabel = modelSourceTypeLabel(sourceType);
  const label = actionLabel(action);

  return (
    <Modal
      description={modalDescription(action)}
      onOpenChange={setOpen}
      open={open}
      title={`${label}${sourceLabel}凭证`}
      trigger={
        <Button
          variant={action === "configure" ? "primary" : "secondary"}
          size="s"
        >
          {label}凭证
        </Button>
      }
    >
      {open ? (
        <CredentialForm
          action={action}
          apiClient={apiClient}
          onClose={() => {
            setOpen(false);
          }}
          onConflict={onConflict}
          onUpdated={onUpdated}
          sourceType={sourceType}
        />
      ) : null}
    </Modal>
  );
}

function CredentialForm({
  action,
  apiClient,
  onClose,
  onConflict,
  onUpdated,
  sourceType,
}: {
  action: CredentialAction;
  apiClient: ApiClient;
  onClose: () => void;
  onConflict: () => Promise<unknown>;
  onUpdated: (updated: ModelSourceResponse) => void;
  sourceType: ModelSourceType;
}) {
  return action === "remove" ? (
    <RemovalForm
      apiClient={apiClient}
      onClose={onClose}
      onConflict={onConflict}
      onUpdated={onUpdated}
      sourceType={sourceType}
    />
  ) : (
    <CredentialWriteForm
      apiClient={apiClient}
      onClose={onClose}
      onConflict={onConflict}
      onUpdated={onUpdated}
      sourceType={sourceType}
    />
  );
}

function CredentialWriteForm({
  apiClient,
  onClose,
  onConflict,
  onUpdated,
  sourceType,
}: Omit<CredentialFormProps, "action">) {
  const formRef = useRef<HTMLFormElement>(null);
  const [confirmUnverified, setConfirmUnverified] = useState(false);
  const [requestError, setRequestError] = useState<ApiError>();
  const [fatalError, setFatalError] = useState<Error>();
  const {
    control,
    formState: { errors, isSubmitting },
    handleSubmit,
    reset,
    resetField,
    setError,
  } = useForm<CredentialFields>({
    defaultValues: { credential: "", currentPassword: "" },
    mode: "onBlur",
    resolver: zodResolver(credentialFormSchema),
  });

  function focusPassword() {
    // TextField owns its input ref; focus through this form rather than RHF's ref.
    const input = formRef.current?.elements.namedItem("currentPassword");
    if (input instanceof HTMLInputElement) input.focus();
  }

  function close() {
    reset();
    setRequestError(undefined);
    onClose();
  }

  async function submit(values: CredentialFields) {
    setRequestError(undefined);
    try {
      const updated = await apiClient.updateModelSource({
        clear: false,
        ...(confirmUnverified ? { confirm_unverified: true } : {}),
        credential: values.credential,
        current_password: values.currentPassword,
        source_type: sourceType,
      });
      reset();
      onUpdated(updated);
      onClose();
    } catch (error) {
      if (!(error instanceof ApiError)) {
        setFatalError(
          error instanceof Error
            ? error
            : new Error("model-source credential update failed"),
        );
        return;
      }
      if (error.code === "model_source_validation_unavailable") {
        setConfirmUnverified(true);
        resetField("currentPassword", { defaultValue: "" });
        focusPassword();
      } else if (error.code === "model_source_credential_rejected") {
        setConfirmUnverified(false);
      }
      await handleWriteError({
        error,
        onConflict,
        onInvalidPassword: () => {
          resetField("currentPassword", { defaultValue: "" });
          setError("currentPassword", { message: "管理员密码不正确" });
          focusPassword();
        },
        setRequestError,
      });
    }
  }

  if (fatalError !== undefined) throw fatalError;

  return (
    <form
      ref={formRef}
      className="pw-model-source-form"
      onSubmit={(event) => void handleSubmit(submit)(event)}
    >
      <WriteFailure error={requestError} />
      <Controller
        control={control}
        name="currentPassword"
        render={({ field }) => (
          <TextField
            {...field}
            autoComplete="current-password"
            errorText={errors.currentPassword?.message}
            label="你的管理员密码"
            required
            type="password"
          />
        )}
      />
      <Controller
        control={control}
        name="credential"
        render={({ field }) => (
          <TextField
            {...field}
            onChange={(event) => {
              field.onChange(event);
              setConfirmUnverified(false);
            }}
            autoComplete="off"
            errorText={errors.credential?.message}
            helpText="保存前先验证，结果显示在该来源上。"
            label="新的 Access Token"
            required
            type="password"
          />
        )}
      />
      <div className="pw-model-source-form__actions">
        <Button onClick={close}>取消</Button>
        <Button
          loading={isSubmitting}
          loadingText="正在保存"
          type="submit"
          variant="primary"
        >
          {confirmUnverified ? "仍然保存（未验证）" : "验证并保存"}
        </Button>
      </div>
    </form>
  );
}

interface CredentialFormProps {
  action: CredentialAction;
  apiClient: ApiClient;
  onClose: () => void;
  onConflict: () => Promise<unknown>;
  onUpdated: (updated: ModelSourceResponse) => void;
  sourceType: ModelSourceType;
}

function RemovalForm({
  apiClient,
  onClose,
  onConflict,
  onUpdated,
  sourceType,
}: Omit<CredentialFormProps, "action">) {
  const [requestError, setRequestError] = useState<ApiError>();
  const [fatalError, setFatalError] = useState<Error>();
  const {
    control,
    formState: { errors, isSubmitting },
    handleSubmit,
    reset,
    resetField,
    setError,
    setFocus,
  } = useForm<RemovalFields>({
    defaultValues: { currentPassword: "" },
    mode: "onBlur",
    resolver: zodResolver(removalFormSchema),
  });

  function close() {
    reset();
    setRequestError(undefined);
    onClose();
  }

  async function submit(values: RemovalFields) {
    setRequestError(undefined);
    try {
      const updated = await apiClient.updateModelSource({
        clear: true,
        credential: null,
        current_password: values.currentPassword,
        source_type: sourceType,
      });
      reset();
      onUpdated(updated);
      onClose();
    } catch (error) {
      if (!(error instanceof ApiError)) {
        setFatalError(
          error instanceof Error
            ? error
            : new Error("model-source credential removal failed"),
        );
        return;
      }
      await handleWriteError({
        error,
        onConflict,
        onInvalidPassword: () => {
          resetField("currentPassword", { defaultValue: "" });
          setError("currentPassword", { message: "管理员密码不正确" });
          setFocus("currentPassword");
        },
        setRequestError,
      });
    }
  }

  if (fatalError !== undefined) throw fatalError;

  return (
    <form
      className="pw-model-source-form"
      onSubmit={(event) => void handleSubmit(submit)(event)}
    >
      <InlineAlert title="移除后私有仓库不可用" tone="negative">
        依赖该凭证的私有仓库读取与下载会失败；公开仓库不受影响。
      </InlineAlert>
      <WriteFailure error={requestError} />
      <Controller
        control={control}
        name="currentPassword"
        render={({ field }) => (
          <TextField
            {...field}
            autoComplete="current-password"
            errorText={errors.currentPassword?.message}
            label="你的管理员密码"
            required
            type="password"
          />
        )}
      />
      <div className="pw-model-source-form__actions">
        <Button onClick={close}>取消</Button>
        <Button
          loading={isSubmitting}
          loadingText="正在移除"
          type="submit"
          variant="danger"
        >
          移除凭证
        </Button>
      </div>
    </form>
  );
}

async function handleWriteError({
  error,
  onConflict,
  onInvalidPassword,
  setRequestError,
}: {
  error: ApiError;
  onConflict: () => Promise<unknown>;
  onInvalidPassword: () => void;
  setRequestError: (error: ApiError | undefined) => void;
}) {
  if (error.status === 401 && error.code === "invalid_credentials") {
    onInvalidPassword();
    return;
  }
  if (error.status === 401 && error.code === "authentication_required") {
    return;
  }
  setRequestError(error);
  if (error.code === "credential_update_conflict") await onConflict();
}

function WriteFailure({ error }: { error: ApiError | undefined }) {
  if (error === undefined) return null;
  return (
    <InlineAlert
      title={error.code === "login_locked" ? "暂时无法验证密码" : "操作未完成"}
      tone={error.code === "login_locked" ? "notice" : "negative"}
    >
      {error.code === "login_locked" && error.retryAfterSeconds !== undefined
        ? `登录失败次数过多，请在 ${String(error.retryAfterSeconds)} 秒后重试。`
        : (error.responseMessage ?? "模型源凭证未更新，请稍后重试。")}
      {error.requestId === undefined ? null : (
        <span className="pw-model-source-form__request-id">
          Request ID：{error.requestId}
        </span>
      )}
    </InlineAlert>
  );
}

function actionLabel(action: CredentialAction) {
  if (action === "configure") return "配置";
  if (action === "replace") return "更换";
  return "移除";
}

function modalDescription(action: CredentialAction) {
  if (action === "remove") return "移除凭证需要重新验证你的管理员密码。";
  return `${action === "configure" ? "配置" : "更换"}凭证需要重新验证你的管理员密码。`;
}

function validationAnnouncement(source: ModelSourceResponse) {
  return source.verification_status === null
    ? "未验证"
    : modelSourceValidationPresentation(source.verification_status).label;
}
