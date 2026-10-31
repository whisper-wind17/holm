import { useQuery } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { Link } from "react-router";

import { ApiError, type ApiClient } from "../api/client";
import type { ModelArtifactItem } from "../api/model-acquisition-contracts";
import { Button, InlineAlert, Modal } from "../components";
import { instanceStatusPresentation } from "../model-instances/presentation";
import { useArtifactVerification } from "./queries";

/*
 * the 重新校验 / 删除文件 actions are shared by the 本地模型 list rows
 * and the 本地模型 detail page. Extracted from model-page.tsx unchanged.
 */

export function ArtifactVerifyAction({
  apiClient,
  item,
  refreshLists,
}: {
  apiClient: ApiClient;
  item: ModelArtifactItem;
  refreshLists: () => Promise<void>;
}) {
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string>();
  const status = useArtifactVerification(apiClient, item.id);
  const state = status.data?.status;
  const refreshed = useRef<string | null>(null);
  const active = state === "Queued" || state === "Running";
  useEffect(() => {
    if (
      state === "Succeeded" &&
      status.data?.job_id &&
      refreshed.current !== status.data.job_id
    ) {
      refreshed.current = status.data.job_id;
      void refreshLists();
    }
  }, [state, status.data?.job_id, refreshLists]);
  return (
    <>
      <Button
        size="s"
        variant="ghost"
        disabled={submitting || active || status.isPending || status.isError}
        onClick={() => {
          setSubmitting(true);
          setError(undefined);
          void apiClient
            .verifyModelArtifact(item.id)
            .then(async () => {
              await status.refetch();
            })
            .catch(() => {
              setError("校验未提交，请刷新列表并等待当前操作结束后重试。");
            })
            .finally(() => {
              setSubmitting(false);
            });
        }}
      >
        重新校验
      </Button>
      <span role="status">
        {submitting
          ? "正在提交校验"
          : state === "Queued"
            ? "校验已排队"
            : state === "Running"
              ? "正在校验"
              : state === "Succeeded"
                ? "校验已完成，请查看本地模型状态"
                : null}
      </span>
      {error || status.isError || state === "Failed" ? (
        <InlineAlert tone="negative" title="校验未完成">
          {error ??
            (status.isError
              ? "无法查询校验状态，请刷新列表后重试。"
              : "校验执行失败，请检查模型目录与访问权限后重新校验。")}
        </InlineAlert>
      ) : null}
    </>
  );
}

export function ArtifactDeleteAction({
  apiClient,
  item,
  refreshLists,
}: {
  apiClient: ApiClient;
  item: ModelArtifactItem;
  refreshLists: () => Promise<void>;
}) {
  const external = item.storage_type === "External";
  const references = item.referencing_instances;
  if (references === null) {
    return (
      <span className="pw-models-muted">
        <Button disabled size="s" variant="ghost">
          {external ? "移除登记" : "删除文件"}
        </Button>
        {external
          ? "无法核实引用关系，暂不可移除登记"
          : "无法核实引用关系，暂不可删除"}
      </span>
    );
  }
  if (references > 0) {
    return (
      <ReferencedArtifactDialog
        apiClient={apiClient}
        item={item}
        references={references}
      />
    );
  }
  return (
    <ArtifactDeleteConfirm
      apiClient={apiClient}
      item={item}
      refreshLists={refreshLists}
    />
  );
}

/** deletion is refused while instances reference the files; list them instead. */
function ReferencedArtifactDialog({
  apiClient,
  item,
  references,
  initiallyOpen = false,
}: {
  apiClient: ApiClient;
  item: ModelArtifactItem;
  references: number;
  initiallyOpen?: boolean;
}) {
  const external = item.storage_type === "External";
  const [open, setOpen] = useState(initiallyOpen);
  const detail = useQuery({
    enabled: open,
    queryFn: () => apiClient.artifactDetail(item.id),
    queryKey: ["artifact-referencing-instances", item.id],
  });
  return (
    <Modal
      description={
        external
          ? `仍有 ${String(references)} 个模型实例在用这个目录。`
          : `仍有 ${String(references)} 个模型实例引用这份模型文件，删除会让它们无法启动。`
      }
      footer={
        <Button
          onClick={() => {
            setOpen(false);
          }}
        >
          知道了
        </Button>
      }
      onOpenChange={setOpen}
      open={open}
      size="s"
      title={
        external
          ? `还不能移除「${item.display_name}」的登记`
          : `还不能删除「${item.display_name}」`
      }
      trigger={
        <Button size="s" variant="ghost">
          {external ? "移除登记" : "删除文件"}
        </Button>
      }
    >
      <p className="pw-models-dialog-note">
        {external
          ? "先删除下面这些实例，再回来移除登记。"
          : "先删除下面这些实例，再回来删除文件。删除实例不会自动删除本地模型。"}
      </p>
      {detail.isPending ? (
        <span className="pw-models-muted" role="status">
          正在读取引用实例
        </span>
      ) : detail.isError ? (
        <InlineAlert title="无法读取引用实例" tone="negative">
          引用实例暂时无法显示，请稍后重试。
          <Button onClick={() => void detail.refetch()} size="s">
            重试
          </Button>
        </InlineAlert>
      ) : (
        <ul className="pw-models-referencing">
          {detail.data.instances.map((instance) => (
            <li key={instance.id}>
              <Link to={`/models/instances/${encodeURIComponent(instance.id)}`}>
                {instance.name}
              </Link>
              <span>{instanceStatusPresentation(instance.status).label}</span>
            </li>
          ))}
        </ul>
      )}
    </Modal>
  );
}

function ArtifactDeleteConfirm({
  apiClient,
  item,
  refreshLists,
}: {
  apiClient: ApiClient;
  item: ModelArtifactItem;
  refreshLists: () => Promise<void>;
}) {
  const external = item.storage_type === "External";
  const [conflictReferences, setConflictReferences] = useState<number>();
  const [open, setOpen] = useState(false);
  const [pending, setPending] = useState(false);
  const [tracking, setTracking] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [error, setError] = useState<string>();
  const status = useQuery({
    queryKey: ["artifact-deletion", item.id, attempt],
    queryFn: () => apiClient.getArtifactDeletionStatus(item.id),
    enabled: tracking,
    refetchInterval: (query) =>
      query.state.data?.status === "Succeeded" ||
      query.state.data?.status === "Failed"
        ? false
        : 1000,
  });

  useEffect(() => {
    if (!tracking) return;
    if (status.data?.status === "Succeeded") {
      setTracking(false);
      setOpen(false);
      void refreshLists();
    } else if (status.data?.status === "Failed") {
      setTracking(false);
      setPending(false);
      setError(
        status.data.error_code === "artifact_delete_move_failed"
          ? "文件移动失败，本地模型仍可用。请检查存储权限后重试。"
          : "文件删除失败，本地模型仍可用。请刷新列表后重试。",
      );
    } else if (status.isError) {
      setTracking(false);
      setPending(false);
      setError("无法查询删除结果，请刷新列表后重试。");
    }
  }, [tracking, status.data, status.isError, refreshLists]);

  if (conflictReferences !== undefined) {
    return (
      <ReferencedArtifactDialog
        apiClient={apiClient}
        item={item}
        references={conflictReferences}
        initiallyOpen
      />
    );
  }
  return (
    <Modal
      description={
        external
          ? `只移除平台里的这条登记，不删除服务器上的文件（${item.path}）。`
          : item.status === "Missing"
            ? "文件已缺失，删除会移除这条本地模型记录。"
            : `删除服务器上的模型文件${item.status === "Corrupted" ? "，包括校验未通过的文件" : ""}。之后要用，可在「下载模型」重新下载。`
      }
      footer={
        <>
          <Button
            disabled={pending}
            onClick={() => {
              setOpen(false);
            }}
          >
            {external ? "取消" : "返回"}
          </Button>
          <Button
            disabled={pending}
            onClick={() => {
              setPending(true);
              setError(undefined);
              void apiClient
                .deleteModelArtifact(item.id)
                .then(async (result) => {
                  if (result.queued) {
                    setAttempt((current) => current + 1);
                    setTracking(true);
                  } else {
                    setOpen(false);
                    await refreshLists();
                  }
                })
                .catch(async (cause: unknown) => {
                  if (cause instanceof ApiError && cause.status === 409) {
                    try {
                      const current = await apiClient.artifactDetail(item.id);
                      if (current.instances.length > 0) {
                        setConflictReferences(current.instances.length);
                        return;
                      }
                    } catch {
                      // Keep the confirmation open when references cannot be refreshed.
                    }
                  }
                  setError(
                    external
                      ? "登记未移除，请刷新列表后重试。"
                      : cause instanceof ApiError
                        ? (cause.responseMessage ??
                          "本地模型未删除，请刷新列表后重试。")
                        : "无法删除文件，请稍后重试。",
                  );
                  setPending(false);
                });
            }}
            variant="danger"
          >
            {external
              ? pending
                ? "正在移除登记"
                : "确认移除登记"
              : pending
                ? "正在删除"
                : "确认删除文件"}
          </Button>
        </>
      }
      onOpenChange={(next) => {
        if (!pending) setOpen(next);
      }}
      open={open}
      size="s"
      title={
        external
          ? `移除本地模型「${item.display_name}」的登记？`
          : `删除本地模型「${item.display_name}」？`
      }
      trigger={
        <Button size="s" variant="ghost">
          {external ? "移除登记" : "删除文件"}
        </Button>
      }
    >
      <p className="pw-models-dialog-note">
        {external ? "历史用量保留。" : "下载任务记录与历史用量保留。"}
      </p>
      {error === undefined ? null : (
        <InlineAlert
          title={external ? "移除登记失败" : "删除失败"}
          tone="negative"
        >
          {error}
        </InlineAlert>
      )}
    </Modal>
  );
}
