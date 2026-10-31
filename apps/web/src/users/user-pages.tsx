import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Fragment,
  useEffect,
  useMemo,
  useRef,
  useState,
  type SyntheticEvent,
} from "react";
import { Link } from "react-router";

import { ApiError, type ApiClient } from "../api/client";
import type {
  CreatedUser,
  UserOverview,
  UserRole,
  UserStatus,
} from "../api/user-contracts";
import { useAiServices } from "../ai-services/queries";
import { useSession } from "../auth/session";
import {
  Button,
  EmptyState,
  InlineAlert,
  Modal,
  TextField,
} from "../components";
import { USERS_QUERY_KEY, useUsers } from "./queries";
import { SecretResult, UserActions, userStatusLabel } from "./user-actions";

const USERNAME_PATTERN = "[A-Za-z0-9._-]{3,32}";

const TOKEN_UNAVAILABLE = "—";
const STATUS_RANK: Record<UserStatus, number> = {
  active: 0,
  disabled: 1,
  deleted: 2,
};

export function UsersPage({ apiClient }: { apiClient: ApiClient }) {
  const heading = useRef<HTMLHeadingElement>(null);
  const session = useSession();
  const [search, setSearch] = useState("");
  const [role, setRole] = useState<UserRole>();
  const [serviceId, setServiceId] = useState("");
  const [statusFilter, setStatusFilter] = useState<UserStatus>();
  const [includeDeleted, setIncludeDeleted] = useState(false);
  const [rowErrors, setRowErrors] = useState<Readonly<Record<string, string>>>(
    {},
  );
  const [notice, setNotice] = useState<string>();
  const filters = useMemo(
    () => ({ includeDeleted, role, search: search.trim() }),
    [includeDeleted, role, search],
  );
  const users = useUsers(apiClient, filters);
  const services = useAiServices(apiClient);
  const grants = useQuery({
    enabled: serviceId !== "",
    queryFn: () => apiClient.getServicePermissions(serviceId),
    queryKey: [...USERS_QUERY_KEY, "service-permissions", serviceId],
  });
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
        groupBy: "user",
        timeZone: Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC",
        to: new Date().toISOString(),
      }),
    queryKey: ["user-list-today-usage", todayStart],
    refetchInterval: 30_000,
  });
  const currentUserId = session.me?.user.id;

  useEffect(() => heading.current?.focus(), []);
  useEffect(() => {
    if (notice === undefined) return undefined;
    const timer = window.setTimeout(() => {
      setNotice(undefined);
    }, 6000);
    return () => {
      window.clearTimeout(timer);
    };
  }, [notice]);

  const todayTokens = useMemo(
    () =>
      new Map(
        (todayUsage.data?.details ?? []).flatMap((detail) =>
          detail.group_id === null
            ? []
            : [[detail.group_id, detail.total_tokens] as const],
        ),
      ),
    [todayUsage.data],
  );
  const granted = useMemo(
    () => (serviceId === "" ? undefined : new Set(grants.data?.user_ids ?? [])),
    [grants.data, serviceId],
  );
  // Search and role are applied by the server; the status counts below are
  // computed from what remains after the AI service filter.
  const scoped = (users.data?.items ?? []).filter(
    (item) =>
      (includeDeleted || item.status !== "deleted") &&
      (granted === undefined || granted.has(item.id)),
  );
  const count = (status: UserStatus) =>
    scoped.filter((item) => item.status === status).length;
  const visible = scoped
    .filter(
      (item) => statusFilter === undefined || item.status === statusFilter,
    )
    .sort(
      (a, b) =>
        STATUS_RANK[a.status] - STATUS_RANK[b.status] ||
        a.username.localeCompare(b.username),
    );
  const statusChips: Array<[UserStatus | undefined, string, number]> = [
    [undefined, "全部", scoped.length],
    ["active", "正常", count("active")],
    ["disabled", "已禁用", count("disabled")],
    ...(includeDeleted
      ? [
          ["deleted", "已删除", count("deleted")] as [
            UserStatus,
            string,
            number,
          ],
        ]
      : []),
  ];
  const filtered =
    search.trim() !== "" ||
    role !== undefined ||
    serviceId !== "" ||
    statusFilter !== undefined;

  function clearRowError(id: string) {
    setRowErrors((current) => {
      if (!(id in current)) return current;
      return Object.fromEntries(
        Object.entries(current).filter(([key]) => key !== id),
      );
    });
  }

  let body;
  if (users.data === undefined && users.isError) {
    body = (
      <InlineAlert title="无法获取用户" tone="negative">
        用户列表无法显示。请检查平台连接后重试。
        <Button onClick={() => void users.refetch()} size="s">
          重试
        </Button>
      </InlineAlert>
    );
  } else if (serviceId !== "" && grants.isError) {
    body = (
      <InlineAlert title="无法按 AI 服务筛选" tone="negative">
        该服务的授权用户无法读取。请重试，或改为查看全部 AI 服务。
        <Button onClick={() => void grants.refetch()} size="s">
          重试
        </Button>
      </InlineAlert>
    );
  } else if (
    users.data !== undefined &&
    !(serviceId !== "" && grants.isPending) &&
    visible.length === 0
  ) {
    body = filtered ? (
      <div className="pw-user-no-match">
        <span>没有符合筛选条件的用户。</span>
        <Button
          onClick={() => {
            setSearch("");
            setRole(undefined);
            setServiceId("");
            setStatusFilter(undefined);
          }}
          size="s"
        >
          清除筛选
        </Button>
      </div>
    ) : (
      <EmptyState
        action={<CreateUserModal apiClient={apiClient} />}
        description="创建账号后，可为普通用户授权 AI 服务。"
        icon="users"
        title="还没有其他用户"
      />
    );
  } else {
    body = (
      <UserListTable
        apiClient={apiClient}
        currentUserId={currentUserId}
        loading={users.isPending || (serviceId !== "" && grants.isPending)}
        onFailure={(id, message) => {
          setRowErrors((current) => ({ ...current, [id]: message }));
        }}
        onStart={clearRowError}
        onSuccess={(id, message) => {
          clearRowError(id);
          setNotice(message);
        }}
        rowErrors={rowErrors}
        rows={visible}
        tokenFor={(id) => {
          if (
            !canViewUsage ||
            todayUsage.isError ||
            todayUsage.data === undefined
          )
            return { text: TOKEN_UNAVAILABLE };
          if (!todayTokens.has(id))
            return { muted: true, text: "0", title: "0 Tokens" };
          const total = todayTokens.get(id);
          if (total === null || total === undefined)
            return { text: TOKEN_UNAVAILABLE };
          return {
            muted: total === 0,
            text: formatTokens(total),
            title: `${total.toLocaleString("en-US")} Tokens`,
          };
        }}
      />
    );
  }

  return (
    <section className="pw-users-page">
      <header className="pw-users-page__head">
        <span>
          <h1 ref={heading} tabIndex={-1}>
            用户
          </h1>
          <p>管理登录账号、服务权限和账号状态。</p>
        </span>
        <CreateUserModal apiClient={apiClient} />
      </header>
      <div aria-label="筛选用户" className="pw-user-filters" role="search">
        <TextField
          label="搜索"
          onChange={(event) => {
            setSearch(event.currentTarget.value);
          }}
          placeholder="用户名或显示名称"
          type="search"
          value={search}
        />
        <label className="pw-service-field">
          <span>角色</span>
          <select
            onChange={(event) => {
              setRole(
                event.currentTarget.value === ""
                  ? undefined
                  : (event.currentTarget.value as UserRole),
              );
            }}
            value={role ?? ""}
          >
            <option value="">全部角色</option>
            <option value="user">普通用户</option>
            <option value="admin">管理员</option>
          </select>
        </label>
        <label className="pw-service-field">
          <span>AI 服务</span>
          <select
            onChange={(event) => {
              setServiceId(event.currentTarget.value);
            }}
            value={serviceId}
          >
            <option value="">全部 AI 服务</option>
            {(services.data?.items ?? []).map((service) => (
              <option key={service.id} value={service.id}>
                {service.name}
                {service.enabled ? "" : "（已停用）"}
              </option>
            ))}
          </select>
        </label>
        <label className="pw-user-filter-check">
          <input
            checked={includeDeleted}
            onChange={(event) => {
              setIncludeDeleted(event.currentTarget.checked);
              if (!event.currentTarget.checked && statusFilter === "deleted")
                setStatusFilter(undefined);
            }}
            type="checkbox"
          />
          <span>包含已删除</span>
        </label>
      </div>
      <div className="pw-service-filters pw-user-status-filters">
        <div aria-label="按状态筛选" role="group">
          {statusChips.map(([value, label, total]) => (
            <Button
              aria-pressed={statusFilter === value}
              key={value ?? "all"}
              onClick={() => {
                setStatusFilter(value);
              }}
              size="s"
              variant="ghost"
            >
              {label} <span>{total}</span>
            </Button>
          ))}
        </div>
      </div>
      {notice === undefined ? null : (
        <p className="pw-user-notice" role="status">
          {notice}
        </p>
      )}
      {users.isError && users.data !== undefined ? (
        <InlineAlert title="用户列表尚未更新" tone="notice">
          当前显示上次读取的结果。请检查平台连接后重试。
          <Button onClick={() => void users.refetch()} size="s">
            重试
          </Button>
        </InlineAlert>
      ) : null}
      {services.isError ? (
        <InlineAlert title="无法获取 AI 服务" tone="negative">
          服务筛选选项无法更新，请稍后重试。
          <Button onClick={() => void services.refetch()} size="s">
            重试
          </Button>
        </InlineAlert>
      ) : null}
      {body}
    </section>
  );
}

function formatTokens(value: number) {
  if (value >= 1e9) return `${(value / 1e9).toFixed(1)}B`;
  if (value >= 1e6) return `${(value / 1e6).toFixed(1)}M`;
  return value.toLocaleString("en-US");
}

interface TokenCell {
  muted?: boolean;
  text: string;
  title?: string;
}

function UserListTable({
  apiClient,
  currentUserId,
  loading,
  onFailure,
  onStart,
  onSuccess,
  rowErrors,
  rows,
  tokenFor,
}: {
  apiClient: ApiClient;
  currentUserId: string | undefined;
  loading: boolean;
  onFailure: (id: string, message: string) => void;
  onStart: (id: string) => void;
  onSuccess: (id: string, message: string) => void;
  rowErrors: Readonly<Record<string, string>>;
  rows: readonly UserOverview[];
  tokenFor: (id: string) => TokenCell;
}) {
  return (
    <div
      aria-busy={loading}
      aria-label={loading ? "用户，正在加载" : "用户"}
      className="pw-table-wrap"
    >
      <table className="pw-table">
        <caption className="pw-visually-hidden">用户</caption>
        <thead>
          <tr>
            <th data-column="user">用户</th>
            <th data-column="role">角色</th>
            <th className="pw-table__numeric" data-column="services">
              服务数
            </th>
            <th
              className="pw-table__numeric"
              data-column="keys"
              title="当前能调用的 API Key 数：生效中且用户未被禁用"
            >
              API Keys 数
            </th>
            <th className="pw-table__numeric" data-column="tokens">
              今日 Tokens
            </th>
            <th data-column="last-login">最近登录</th>
            <th data-column="status">状态</th>
            <th data-column="actions">操作</th>
          </tr>
        </thead>
        <tbody>
          {loading
            ? Array.from({ length: 6 }, (_, index) => (
                <tr aria-hidden="true" key={`skeleton-${String(index)}`}>
                  {Array.from({ length: 8 }, (_cell, cell) => (
                    <td key={cell}>
                      <span className="sk pw-table__skeleton" />
                    </td>
                  ))}
                </tr>
              ))
            : rows.map((item) => {
                const isSelf = item.id === currentUserId;
                const keys =
                  item.status === "disabled" ? 0 : item.api_key_count;
                const token = tokenFor(item.id);
                return (
                  <Fragment key={item.id}>
                    <tr
                      className={
                        item.status === "deleted"
                          ? "pw-user-row--deleted"
                          : undefined
                      }
                    >
                      <td data-column="user">
                        <span className="pw-service-identity">
                          <Link
                            title={item.display_name || item.username}
                            to={`/users/${item.id}`}
                          >
                            {item.display_name || item.username}
                          </Link>
                          <span>
                            @{item.username}
                            {isSelf ? " · 当前登录" : ""}
                          </span>
                        </span>
                      </td>
                      <td data-column="role">
                        {item.role === "admin" ? "管理员" : "普通用户"}
                      </td>
                      <td className="pw-table__numeric" data-column="services">
                        {item.authorized_service_count}
                      </td>
                      <td
                        className={[
                          "pw-table__numeric",
                          keys === 0 ? "pw-user-muted" : undefined,
                        ]
                          .filter(Boolean)
                          .join(" ")}
                        data-column="keys"
                        title={keysTitle(item)}
                      >
                        {keys}
                      </td>
                      <td
                        className={[
                          "pw-table__numeric",
                          token.muted ? "pw-user-muted" : undefined,
                        ]
                          .filter(Boolean)
                          .join(" ")}
                        data-column="tokens"
                        title={token.title}
                      >
                        {token.text}
                      </td>
                      <td data-column="last-login">
                        {formatDate(item.last_login_at)}
                      </td>
                      <td data-column="status">
                        <span className="pw-user-status">
                          <span
                            aria-hidden="true"
                            className={`pw-service-signal pw-service-signal--${item.status === "active" ? "positive" : item.status === "disabled" ? "notice" : "idle"}`}
                          />
                          <span>{userStatusLabel(item.status)}</span>
                        </span>
                      </td>
                      <td data-column="actions">
                        <div className="pw-user-actions">
                          {item.status === "deleted" ? (
                            <span className="pw-user-muted">
                              已删除，不可恢复
                            </span>
                          ) : (
                            <UserActions
                              apiClient={apiClient}
                              compact
                              isSelf={isSelf}
                              onFailure={(message) => {
                                onFailure(item.id, message);
                              }}
                              onStart={() => {
                                onStart(item.id);
                              }}
                              onSuccess={(_kind, message) => {
                                onSuccess(item.id, message);
                              }}
                              user={item}
                            />
                          )}
                        </div>
                      </td>
                    </tr>
                    {rowErrors[item.id] === undefined ? null : (
                      <tr className="pw-user-row-error">
                        <td colSpan={8}>
                          <InlineAlert title="操作未完成" tone="negative">
                            {rowErrors[item.id]}
                          </InlineAlert>
                        </td>
                      </tr>
                    )}
                  </Fragment>
                );
              })}
        </tbody>
      </table>
    </div>
  );
}

function keysTitle(item: UserOverview) {
  if (item.status === "disabled" && item.api_key_count > 0)
    return `用户已禁用：${String(item.api_key_count)} 个 API Key 暂停使用，不计入；启用后恢复`;
  if (item.status === "deleted") return "删除时已全部撤销";
  return "已过期、已撤销的不计入";
}

function CreateUserModal({ apiClient }: { apiClient: ApiClient }) {
  const queryClient = useQueryClient();
  const attempt = useRef(0);
  const services = useAiServices(apiClient);
  const [open, setOpen] = useState(false);
  const [username, setUsername] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [role, setRole] = useState<UserRole>("user");
  const [confirmAdmin, setConfirmAdmin] = useState(false);
  const [serviceIds, setServiceIds] = useState<Set<string>>(new Set());
  const [created, setCreated] = useState<{
    permissionWarning: boolean;
    temporaryPassword: string;
    username: string;
  }>();
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<ApiError>();
  const [conflict, setConflict] = useState<{
    deleted: boolean;
    username: string;
  }>();

  function close() {
    attempt.current += 1;
    setOpen(false);
    setConflict(undefined);
    setUsername("");
    setDisplayName("");
    setRole("user");
    setConfirmAdmin(false);
    setServiceIds(new Set());
    setCreated(undefined);
    setPending(false);
    setError(undefined);
  }

  async function submit(event: SyntheticEvent<HTMLFormElement>) {
    event.preventDefault();
    const currentAttempt = attempt.current + 1;
    attempt.current = currentAttempt;
    setPending(true);
    setError(undefined);
    setConflict(undefined);
    try {
      // Direct calls keep the one-time plaintext out of TanStack MutationCache.
      const response: CreatedUser = await apiClient.createUser({
        display_name: displayName,
        role,
        username,
      });
      if (attempt.current !== currentAttempt) return;
      setCreated({
        permissionWarning: false,
        temporaryPassword: response.temporary_password,
        username: response.username,
      });
      const initialServiceIds = role === "user" ? [...serviceIds] : [];
      if (initialServiceIds.length > 0) {
        void applyInitialPermissions(
          response.id,
          initialServiceIds,
          currentAttempt,
        );
      } else {
        void queryClient.invalidateQueries({ queryKey: USERS_QUERY_KEY });
      }
    } catch (caught) {
      if (attempt.current !== currentAttempt) return;
      if (caught instanceof ApiError) {
        if (caught.code === "username_conflict") {
          // The API cannot tell whether the name belongs to a deleted user.
          const deleted = await belongsToDeletedUser(apiClient, username);
          if (attempt.current !== currentAttempt) return;
          setConflict({ deleted, username });
        }
        setError(caught);
      } else {
        throw caught;
      }
    } finally {
      if (attempt.current === currentAttempt) setPending(false);
    }
  }

  async function applyInitialPermissions(
    userId: string,
    initialServiceIds: readonly string[],
    currentAttempt: number,
  ) {
    try {
      await apiClient.replaceUserPermissions(userId, initialServiceIds);
    } catch {
      if (attempt.current === currentAttempt) {
        setCreated((current) =>
          current === undefined
            ? undefined
            : { ...current, permissionWarning: true },
        );
      }
    } finally {
      void queryClient.invalidateQueries({ queryKey: USERS_QUERY_KEY });
    }
  }

  const usernameConflict = error?.code === "username_conflict";
  return (
    <Modal
      description={
        created === undefined
          ? "创建可登录平台的本地账号。"
          : "首次登录须修改密码，关闭后无法再次查看。"
      }
      onOpenChange={(next) => {
        if (next) {
          setOpen(true);
        } else {
          close();
        }
      }}
      open={open}
      size="l"
      title={created === undefined ? "创建用户" : "保存临时密码"}
      trigger={<Button variant="primary">创建用户</Button>}
    >
      {created === undefined ? (
        <form
          className="pw-service-form"
          onSubmit={(event) => void submit(event)}
        >
          <TextField
            errorText={
              usernameConflict && conflict !== undefined
                ? conflict.deleted
                  ? `用户名「${conflict.username}」属于一个已删除的用户，不能复用（不区分大小写）。勾选列表上方「包含已删除」可以查看该用户。`
                  : `用户名「${conflict.username}」已被使用（不区分大小写）。`
                : undefined
            }
            helpText="3～32 位字母、数字、点、下划线或连字符。"
            label="用户名"
            maxLength={32}
            minLength={3}
            onChange={(event) => {
              setUsername(event.currentTarget.value);
            }}
            pattern={USERNAME_PATTERN}
            required
            value={username}
          />
          <TextField
            helpText="留空时显示用户名。"
            label="显示名称"
            maxLength={80}
            onChange={(event) => {
              setDisplayName(event.currentTarget.value);
            }}
            value={displayName}
          />
          <label className="pw-service-field">
            <span>角色</span>
            <select
              onChange={(event) => {
                const next = event.currentTarget.value as UserRole;
                setRole(next);
                setConfirmAdmin(false);
                if (next === "admin") {
                  setServiceIds(new Set());
                }
              }}
              value={role}
            >
              <option value="user">普通用户</option>
              <option value="admin">管理员</option>
            </select>
          </label>
          {role === "admin" ? (
            <InlineAlert title="确认授予管理权限" tone="notice">
              管理员可以管理用户、AI 服务和平台设置。
              <label className="pw-user-confirm-check">
                <input
                  checked={confirmAdmin}
                  onChange={(event) => {
                    setConfirmAdmin(event.currentTarget.checked);
                  }}
                  type="checkbox"
                />
                <span>我确认授予该账号管理员权限</span>
              </label>
            </InlineAlert>
          ) : (
            <ServiceChoices
              emptyText="当前没有可授权的 AI 服务。"
              items={services.data?.items ?? []}
              legend="允许的 AI 服务（可选）"
              onChange={setServiceIds}
              selected={serviceIds}
              unavailable={services.isError}
            />
          )}
          {error !== undefined && !usernameConflict ? (
            <InlineAlert title="用户未创建" tone="negative">
              {error.responseMessage ?? "用户信息未保存，请检查后重试。"}
            </InlineAlert>
          ) : null}
          <footer className="pw-service-form__actions">
            <Button onClick={close}>取消</Button>
            <Button
              disabled={role === "admin" && !confirmAdmin}
              loading={pending}
              loadingText="正在创建"
              type="submit"
              variant="primary"
            >
              创建用户
            </Button>
          </footer>
        </form>
      ) : (
        <SecretResult
          onClose={close}
          secret={created.temporaryPassword}
          title={`用户 ${created.username} 已创建`}
          warning={
            created.permissionWarning
              ? "用户已创建，但服务权限未保存。关闭后请进入用户详情重新授权。"
              : undefined
          }
        />
      )}
    </Modal>
  );
}

async function belongsToDeletedUser(apiClient: ApiClient, username: string) {
  try {
    const list = await apiClient.getUsers({
      includeDeleted: true,
      search: username,
    });
    const wanted = username.toLowerCase();
    return list.items.some(
      (item) =>
        item.status === "deleted" && item.username.toLowerCase() === wanted,
    );
  } catch {
    return false;
  }
}

export { UserDetailPage } from "./user-detail-page";

export function ServiceChoices({
  emptyText,
  items,
  legend,
  onChange,
  selected,
  unavailable,
}: {
  emptyText: string;
  items: readonly { id: string; name: string; public_model_name: string }[];
  legend: string;
  onChange: (value: Set<string>) => void;
  selected: ReadonlySet<string>;
  unavailable: boolean;
}) {
  return (
    <fieldset className="pw-service-scope">
      <legend>{legend}</legend>
      {unavailable ? (
        <InlineAlert title="无法获取 AI 服务" tone="negative">
          当前不能修改服务权限，请稍后重试。
        </InlineAlert>
      ) : items.length === 0 ? (
        <span className="pw-service-scope__empty">{emptyText}</span>
      ) : (
        <div className="pw-service-scope__options">
          {items.map((service) => (
            <label key={service.id}>
              <input
                checked={selected.has(service.id)}
                onChange={() => {
                  const next = new Set(selected);
                  if (next.has(service.id)) {
                    next.delete(service.id);
                  } else {
                    next.add(service.id);
                  }
                  onChange(next);
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
  );
}

function formatDate(value: string | null) {
  if (value === null) return "从未登录";
  return new Intl.DateTimeFormat("zh-CN", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}
