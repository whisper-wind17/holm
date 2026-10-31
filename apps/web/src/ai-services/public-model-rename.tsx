import { PUBLIC_MODEL_NAME_HELP } from "./public-model-name-copy";
import { zodResolver } from "@hookform/resolvers/zod";
import { useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";
import { Controller, useForm } from "react-hook-form";

import {
  renameAiServiceRequestSchema,
  type AiService,
  type RenameAiServiceRequest,
} from "../api/ai-service-contracts";
import { ApiError, type ApiClient } from "../api/client";
import { Button, InlineAlert, Modal, TextField } from "../components";
import { AI_SERVICES_QUERY_KEY, aiServiceQueryKey } from "./queries";

export function PublicModelRename({
  apiClient,
  service,
  unavailable,
  onRenamed,
}: {
  apiClient: ApiClient;
  service: AiService;
  unavailable: boolean;
  onRenamed: () => void;
}) {
  const cache = useQueryClient();
  const [open, setOpen] = useState(false);
  const [observed, setObserved] = useState(service);
  const [pending, setPending] = useState(false);
  const busy = useRef(false);
  const [error, setError] = useState<unknown>();
  const [conflict, setConflict] = useState(false);
  const formRef = useRef<HTMLFormElement>(null);
  const {
    control,
    handleSubmit,
    reset,
    setValue,
    watch,
    formState: { errors },
  } = useForm<RenameAiServiceRequest>({
    resolver: zodResolver(renameAiServiceRequestSchema),
    defaultValues: {
      public_model_name: "",
      confirm_public_model_name: "",
      expected_route_version: service.route_version,
    },
  });
  const stale = conflict || service.route_version !== observed.route_version;
  const name = watch("public_model_name");
  const confirmation = watch("confirm_public_model_name");

  function changeOpen(next: boolean) {
    if (busy.current) return;
    if (next) {
      setObserved(service);
      setError(undefined);
      setConflict(false);
      reset({
        public_model_name: "",
        confirm_public_model_name: "",
        expected_route_version: service.route_version,
      });
    }
    setOpen(next);
  }

  async function refresh() {
    if (busy.current) return;
    busy.current = true;
    setPending(true);
    setError(undefined);
    setValue("confirm_public_model_name", "");
    try {
      const current = await apiClient.getAiService(service.id);
      cache.setQueryData(aiServiceQueryKey(service.id), current);
      setObserved(current);
      setValue("expected_route_version", current.route_version);
      setConflict(false);
    } catch (reason) {
      setError(reason);
    } finally {
      busy.current = false;
      setPending(false);
    }
  }

  async function submit(value: RenameAiServiceRequest) {
    if (busy.current || stale || unavailable) return;
    busy.current = true;
    setPending(true);
    setError(undefined);
    try {
      const changed = await apiClient.renameAiService(service.id, value);
      cache.setQueryData(aiServiceQueryKey(service.id), changed);
      void cache.invalidateQueries({
        queryKey: AI_SERVICES_QUERY_KEY,
        exact: true,
      });
      onRenamed();
      setOpen(false);
    } catch (reason) {
      setError(reason);
      if (reason instanceof ApiError && reason.code === "version_conflict")
        setConflict(true);
    } finally {
      busy.current = false;
      setPending(false);
    }
  }

  return (
    <Modal
      open={open}
      onOpenChange={changeOpen}
      title="修改 API 模型名"
      description="路由刷新后，使用旧名称的客户端将无法调用。请同步修改客户端配置；已开始的调用不会因此中断。"
      trigger={
        <Button
          aria-label="修改 API 模型名"
          size="s"
          variant="ghost"
          disabled={unavailable}
        >
          修改
        </Button>
      }
    >
      <form
        ref={formRef}
        className="pw-service-form"
        noValidate
        onSubmit={(event) =>
          void handleSubmit(submit, () => {
            requestAnimationFrame(() =>
              formRef.current
                ?.querySelector<HTMLInputElement>('[aria-invalid="true"]')
                ?.focus(),
            );
          })(event)
        }
      >
        <p>
          修改只更新路由，不重启模型；仍使用旧名称的客户端会收到
          404，需要同步修改配置。
        </p>
        <p className="pw-public-model-rename__current">
          当前 API 模型名：<bdi>{observed.public_model_name}</bdi>
        </p>
        {stale ? (
          <InlineAlert title="配置已更新" tone="notice">
            API
            模型名未修改。请刷新当前配置，再重新输入确认名称。未提交的新名称会保留。
            <Button loading={pending} onClick={() => void refresh()}>
              刷新当前配置
            </Button>
          </InlineAlert>
        ) : null}
        {unavailable && !stale ? (
          <InlineAlert title="当前配置尚未确认" tone="notice">
            请等待详情刷新完成后再提交；刷新失败时请关闭弹层并重新加载详情。
          </InlineAlert>
        ) : null}
        {error === undefined ? null : (
          <InlineAlert title="API 模型名未修改" tone="negative">
            {error instanceof ApiError
              ? (error.responseMessage ?? "修改失败，请重试。")
              : "无法保存或获取配置，请检查连接后重试。"}
            {error instanceof ApiError && error.requestId ? (
              <p>Request ID：{error.requestId}</p>
            ) : null}
          </InlineAlert>
        )}
        <Controller
          control={control}
          name="public_model_name"
          render={({ field }) => (
            <TextField
              label="新 API 模型名"
              required
              value={field.value}
              onChange={field.onChange}
              onBlur={field.onBlur}
              disabled={pending}
              maxLength={64}
              errorText={errors.public_model_name?.message}
              helpText={`${PUBLIC_MODEL_NAME_HELP}2～64 位小写字母、数字、点、下划线或短横线，例如 qwen3-32b。`}
            />
          )}
        />
        <Controller
          control={control}
          name="confirm_public_model_name"
          render={({ field }) => (
            <TextField
              label="再次输入新 API 模型名"
              required
              value={field.value}
              onChange={field.onChange}
              onBlur={field.onBlur}
              disabled={pending || stale}
              maxLength={64}
              errorText={errors.confirm_public_model_name?.message}
            />
          )}
        />
        {pending ? <p role="status">正在等待服务器确认，请稍候。</p> : null}
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
            variant="primary"
            loading={pending}
            loadingText="正在保存"
            disabled={
              stale || unavailable || name.length === 0 || confirmation !== name
            }
          >
            确认修改
          </Button>
        </footer>
      </form>
    </Modal>
  );
}
