import { zodResolver } from "@hookform/resolvers/zod";
import { useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";
import { Controller, useForm } from "react-hook-form";

import {
  renameServiceDisplayNameSchema,
  type AiService,
  type RenameServiceDisplayNameRequest,
} from "../api/ai-service-contracts";
import { ApiError, type ApiClient } from "../api/client";
import { Button, InlineAlert, Modal, TextField } from "../components";
import { API_KEYS_QUERY_KEY } from "../api-keys/queries";
import { MY_AI_SERVICES_QUERY_KEY, USERS_QUERY_KEY } from "../users/queries";
import { AI_SERVICES_QUERY_KEY, aiServiceQueryKey } from "./queries";

export function ServiceDisplayName({
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
  } = useForm<RenameServiceDisplayNameRequest>({
    resolver: zodResolver(renameServiceDisplayNameSchema),
    defaultValues: {
      name: service.name,
      expected_route_version: service.route_version,
    },
  });
  const stale = conflict || service.route_version !== observed.route_version;
  const name = watch("name");

  function changeOpen(next: boolean) {
    if (busy.current) return;
    if (next) {
      setObserved(service);
      setError(undefined);
      setConflict(false);
      reset({
        name: service.name,
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

  async function submit(value: RenameServiceDisplayNameRequest) {
    if (busy.current || stale || unavailable) return;
    busy.current = true;
    setPending(true);
    setError(undefined);
    try {
      const changed = await apiClient.renameServiceDisplayName(
        service.id,
        value,
      );
      cache.setQueryData(aiServiceQueryKey(service.id), changed);
      void cache.invalidateQueries({
        queryKey: AI_SERVICES_QUERY_KEY,
        exact: true,
      });
      for (const queryKey of [
        API_KEYS_QUERY_KEY,
        USERS_QUERY_KEY,
        MY_AI_SERVICES_QUERY_KEY,
      ])
        void cache.invalidateQueries({ queryKey });
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
      title="修改服务名称"
      description="服务名称用于页面展示。API 模型名仍用于客户端调用。"
      trigger={
        <Button
          aria-label="修改服务名称"
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
        <p className="pw-public-model-rename__current">
          当前服务名称：<bdi>{observed.name}</bdi>
        </p>
        {stale ? (
          <InlineAlert title="配置已更新" tone="notice">
            服务名称未修改。请刷新当前配置后重新提交。未提交的新名称会保留。
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
          <InlineAlert title="服务名称未修改" tone="negative">
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
          name="name"
          render={({ field }) => (
            <TextField
              label="新服务名称"
              required
              value={field.value}
              onChange={field.onChange}
              onBlur={field.onBlur}
              disabled={pending}
              errorText={errors.name?.message}
              helpText="2～40 个字符，系统内唯一；首尾空白会自动去除。"
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
            disabled={stale || unavailable || name.trim().length === 0}
          >
            保存名称
          </Button>
        </footer>
      </form>
    </Modal>
  );
}
