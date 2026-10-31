import { useQueryClient } from "@tanstack/react-query";
import { useRef, useState, type ReactElement } from "react";

import { ApiError, type ApiClient } from "../api/client";
import type { UserStatus } from "../api/user-contracts";
import {
  Button,
  CopyControl,
  InlineAlert,
  Modal,
  StatusBadge,
} from "../components";
import { USERS_QUERY_KEY } from "./queries";

/**
 * the four confirm dialogs (disable, enable, reset password, delete)
 * and the one-time password result, shared by the user list rows and the
 * user detail header. Command results are awaited directly so a temporary
 * password never enters the TanStack mutation cache.
 */
export type UserActionKind = "disable" | "enable" | "reset" | "delete";

export interface UserActionTarget {
  api_key_count: number;
  id: string;
  status: UserStatus;
  username: string;
}

export const SELF_ACTION_TIP =
  "不能禁用或删除当前登录的自己；需要时请另一位管理员操作";

export function userStatusLabel(value: UserStatus) {
  return value === "active"
    ? "正常"
    : value === "disabled"
      ? "已禁用"
      : "已删除";
}

export function UserStatusBadge({ value }: { value: UserStatus }) {
  const item =
    value === "active"
      ? { icon: "success" as const, text: "正常", tone: "positive" as const }
      : value === "disabled"
        ? { icon: "pause" as const, text: "已禁用", tone: "notice" as const }
        : { icon: "pause" as const, text: "已删除", tone: "idle" as const };
  return <StatusBadge {...item} />;
}

export function userActionErrorMessage(
  error: unknown,
  kind: UserActionKind = "disable",
) {
  if (error instanceof ApiError) {
    if (error.code === "self_protection")
      return "不能禁用或删除当前账号，本次操作未执行，请使用其他管理员账号。";
    if (error.code === "last_active_administrator")
      return "系统必须保留一个正常管理员，本次操作未执行，请先创建或启用其他管理员。";
    if (error.responseMessage !== undefined) return error.responseMessage;
  }
  return kind === "reset"
    ? "控制面暂时没有响应，密码没有重置。请稍后重试。"
    : "控制面暂时没有响应，账号状态没有改变。请稍后重试。";
}

function confirmLines(kind: UserActionKind, user: UserActionTarget) {
  const keys = user.api_key_count;
  if (kind === "disable")
    return [
      "立即阻止登录，已登录的会话随之失效。",
      keys > 0
        ? `该用户的 ${String(keys)} 个生效中 API 密钥暂停使用（不改变密钥本身的状态，也不删除）。`
        : "该用户没有生效中的 API 密钥。",
      "服务授权与历史用量保留；启用后恢复登录和仍未过期的密钥。",
    ];
  if (kind === "enable")
    return [
      "恢复登录。",
      `恢复仍未过期、未撤销的 API 密钥（${String(keys)} 个）和原有服务授权。`,
      "禁用期间已过期的密钥不会恢复，需要用户自己新建。",
    ];
  if (kind === "reset")
    return [
      "生成一次性临时密码，只显示一次。",
      "立即撤销该用户所有登录会话；下次登录必须设置新密码。",
      "默认不撤销 API 密钥：已经在用的程序调用不受影响。",
      ...(user.status === "disabled"
        ? ["该用户目前已禁用：启用之前仍不能登录。"]
        : []),
    ];
  return [
    "逻辑删除，不能恢复账号；同名用户名也不能再次使用。",
    "立即永久撤销尚未撤销的 API 密钥，移除全部服务授权。",
    "历史审计与用量数据保留，在用量页仍可按该用户查询。",
  ];
}

function successMessage(kind: UserActionKind, username: string) {
  if (kind === "disable") return `已禁用 ${username}`;
  if (kind === "enable") return `已启用 ${username}`;
  if (kind === "reset") return `已重置 ${username} 的密码`;
  return `已删除 ${username}；勾选「包含已删除」可查看`;
}

export function UserActions({
  apiClient,
  compact = false,
  enableOnly = false,
  isSelf,
  onFailure,
  onStart,
  onSuccess,
  user,
}: {
  apiClient: ApiClient;
  compact?: boolean;
  enableOnly?: boolean;
  isSelf: boolean;
  onFailure: (message: string) => void;
  onStart?: () => void;
  onSuccess: (kind: UserActionKind, message: string) => void;
  user: UserActionTarget;
}) {
  const queryClient = useQueryClient();
  const attempt = useRef(0);
  const [openKind, setOpenKind] = useState<UserActionKind>();
  const [pending, setPending] = useState(false);
  const [secret, setSecret] = useState<string>();

  function close() {
    attempt.current += 1;
    if (secret !== undefined) {
      onSuccess("reset", successMessage("reset", user.username));
    }
    setOpenKind(undefined);
    setSecret(undefined);
    setPending(false);
  }

  async function run(kind: UserActionKind) {
    const current = ++attempt.current;
    setPending(true);
    onStart?.();
    try {
      if (kind === "reset") {
        const result = await apiClient.resetUserPassword(user.id);
        if (attempt.current !== current) return;
        setSecret(result.temporary_password);
        void queryClient.invalidateQueries({ queryKey: USERS_QUERY_KEY });
        return;
      }
      await apiClient.commandUser(user.id, kind);
      void queryClient.invalidateQueries({ queryKey: USERS_QUERY_KEY });
      if (attempt.current !== current) return;
      setOpenKind(undefined);
      onSuccess(kind, successMessage(kind, user.username));
    } catch (caught) {
      if (attempt.current !== current) return;
      setOpenKind(undefined);
      onFailure(userActionErrorMessage(caught, kind));
    } finally {
      if (attempt.current === current) setPending(false);
    }
  }

  const triggerProps = compact
    ? { size: "s" as const, variant: "ghost" as const }
    : {};
  const selfProps = (disabled: boolean) =>
    disabled ? { disabled: true, title: SELF_ACTION_TIP } : { disabled: false };

  function confirmModal(
    kind: UserActionKind,
    trigger: ReactElement,
    title: string,
    ok: string,
  ) {
    const showSecret = kind === "reset" && secret !== undefined;
    return (
      <Modal
        description={
          showSecret
            ? "首次登录须修改密码，关闭后无法再次查看。"
            : "请确认下面的影响后再继续。"
        }
        onOpenChange={(next) => {
          if (next) {
            setOpenKind(kind);
          } else {
            close();
          }
        }}
        open={openKind === kind}
        size="l"
        title={showSecret ? "保存临时密码" : title}
        trigger={trigger}
      >
        {showSecret ? (
          <SecretResult
            onClose={close}
            secret={secret}
            title={`用户 ${user.username} 的密码已重置`}
          />
        ) : (
          <div className="pw-service-form">
            <ul className="pw-user-confirm-lines">
              {confirmLines(kind, user).map((line) => (
                <li key={line}>{line}</li>
              ))}
            </ul>
            <footer className="pw-service-form__actions">
              <Button onClick={close}>取消</Button>
              <Button
                loading={pending}
                onClick={() => void run(kind)}
                variant={kind === "enable" ? "primary" : "danger"}
              >
                {ok}
              </Button>
            </footer>
          </div>
        )}
      </Modal>
    );
  }

  if (user.status === "deleted") return null;
  return (
    <>
      {user.status === "active"
        ? confirmModal(
            "disable",
            <Button {...triggerProps} {...selfProps(isSelf)}>
              禁用
            </Button>,
            `禁用用户「${user.username}」？`,
            "禁用用户",
          )
        : confirmModal(
            "enable",
            <Button {...triggerProps}>启用</Button>,
            `启用用户「${user.username}」？`,
            "启用",
          )}
      {isSelf || enableOnly
        ? null
        : confirmModal(
            "reset",
            <Button {...triggerProps}>重置密码</Button>,
            `重置「${user.username}」的密码？`,
            "重置密码",
          )}
      {enableOnly
        ? null
        : confirmModal(
            "delete",
            <Button
              {...triggerProps}
              {...selfProps(isSelf)}
              className="pw-user-action--danger"
            >
              删除
            </Button>,
            `删除用户「${user.username}」？`,
            "删除用户",
          )}
    </>
  );
}

export function SecretResult({
  onClose,
  secret,
  title,
  warning,
}: {
  onClose: () => void;
  secret: string;
  title: string;
  warning?: string;
}) {
  const secretRef = useRef<HTMLTextAreaElement>(null);
  const [copyFailed, setCopyFailed] = useState(false);
  return (
    <div className="pw-secret-result">
      <InlineAlert title={title} tone="positive">
        首次登录须修改密码，关闭后无法再次查看。
      </InlineAlert>
      {warning === undefined ? null : (
        <InlineAlert title="服务权限未保存" tone="notice">
          {warning}
        </InlineAlert>
      )}
      <section aria-label="一次性临时密码" className="pw-secret-block">
        <label htmlFor="temporary-password">临时密码</label>
        <textarea
          id="temporary-password"
          onFocus={(event) => {
            event.currentTarget.select();
          }}
          readOnly
          ref={secretRef}
          rows={2}
          value={secret}
        />
        <CopyControl
          label="复制临时密码"
          onCopyError={() => {
            setCopyFailed(true);
          }}
          value={secret}
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
              全选密码
            </button>
            后手动复制。
          </span>
        ) : null}
      </section>
      <footer className="pw-service-form__actions">
        <Button onClick={onClose} variant="primary">
          我已保存临时密码
        </Button>
      </footer>
    </div>
  );
}
