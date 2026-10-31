import * as RadixTabs from "@radix-ui/react-tabs";
import { useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";
import { Link, useLocation, useParams } from "react-router";
import { ApiError, type ApiClient } from "../api/client";
import type {
  PermissionLimitsMap,
  SafeApiKey,
  UserDetail,
} from "../api/user-contracts";
import { useAiServices } from "../ai-services/queries";
import { useUnsavedChangesGuard } from "../app/navigation-guard";
import { useSession } from "../auth/session";
import { Button, InlineAlert, Modal, StatusBadge } from "../components";
import {
  changedPermissionLimits,
  DEFAULT_PERMISSION_LIMITS,
  PermissionLimitFields,
} from "./permission-limits";
import { USERS_QUERY_KEY, useUser } from "./queries";
import { UserActions, UserStatusBadge, userStatusLabel } from "./user-actions";
import { UserActivity } from "./user-detail-activity";
import { UsageNumber, UserUsage, useUserUsage } from "./user-detail-usage";

export function detailDate(value: string | null, empty = "—") {
  return value === null
    ? empty
    : new Date(value).toLocaleString("zh-CN", {
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
      });
}
export function UserDetailPage({ apiClient }: { apiClient: ApiClient }) {
  const { userId = "" } = useParams();
  const query = useUser(apiClient, userId);
  if (query.data === undefined) {
    if (query.isPending)
      return (
        <section
          className="pw-user-detail"
          aria-busy="true"
          aria-label="用户详情正在加载"
        >
          <p role="status">正在加载用户详情…</p>
        </section>
      );
    const missing =
      query.error instanceof ApiError &&
      query.error.status === 404 &&
      query.error.code === "user_not_found";
    return (
      <section className="pw-user-detail">
        <InlineAlert
          title={missing ? "找不到这个用户" : "无法获取用户详情"}
          tone="negative"
        >
          {missing
            ? "链接可能有误。已删除的用户仍可在用户列表勾选「包含已删除」后查看。"
            : "控制面暂时没有响应。该用户的登录与 API 调用不受影响，请稍后重试。"}
        </InlineAlert>
        <Link to="/users">返回用户列表</Link>
        {missing ? null : (
          <Button onClick={() => void query.refetch()}>重试</Button>
        )}
      </section>
    );
  }
  return (
    <UserDetailContent
      key={userId}
      apiClient={apiClient}
      item={query.data}
      stale={query.isError}
    />
  );
}
function UserDetailContent({
  apiClient,
  item,
  stale,
}: {
  apiClient: ApiClient;
  item: UserDetail;
  stale: boolean;
}) {
  const session = useSession();
  const cache = useQueryClient();
  const services = useAiServices(apiClient);
  const location = useLocation();
  // 个人资料页的「已授权服务」经路由 state 直达服务权限。
  const [tab, setTab] = useState(() =>
    (location.state as { tab?: unknown } | null)?.tab === "permissions"
      ? "permissions"
      : "overview",
  );
  const [pendingTab, setPendingTab] = useState<string>();
  const tabTriggers = useRef(new Map<string, HTMLButtonElement>());
  const returningTabFocus = useRef(false);
  const tabReturnFocus = useRef<HTMLButtonElement | null>(null);
  const [draft, setDraft] = useState<ReadonlyMap<string, boolean>>(new Map());
  const [limitDraft, setLimitDraft] = useState<PermissionLimitsMap>({});
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState(false);
  const [notice, setNotice] = useState<string>();
  const [commandError, setCommandError] = useState<string>();
  const saved = new Set(item.authorized_services.map((service) => service.id));
  const changes = [...draft].filter(([id, on]) => saved.has(id) !== on);
  const savedLimits = Object.fromEntries(
    item.authorized_services.map((service) => [
      service.id,
      {
        max_concurrent_requests: service.max_concurrent_requests,
        max_requests_per_minute: service.max_requests_per_minute,
      },
    ]),
  );
  const limitChanges = changedPermissionLimits(
    limitDraft,
    savedLimits,
    (id) => draft.get(id) ?? saved.has(id),
  );
  const limitCount = Object.keys(limitChanges).filter((id) =>
    saved.has(id),
  ).length;
  const dirty =
    item.status !== "deleted" && (changes.length > 0 || limitCount > 0);
  const added = changes.filter(([, on]) => on).length;
  const removed = changes.length - added;
  const self = item.id === session.me?.user.id;
  const today = useUserUsage(apiClient, item.id, "today", "user");
  const month = useUserUsage(apiClient, item.id, "month", "user");
  const oldN = item.api_keys.filter((key) => key.status !== "Active").length;
  function discard() {
    setDraft(new Map());
    setLimitDraft({});
    setSaveError(false);
  }
  useUnsavedChangesGuard(
    dirty
      ? {
          discard,
          title: "有未保存的服务权限更改",
          message: "离开后，本次勾选的更改不会生效。",
        }
      : null,
  );
  function goTab(value: string) {
    if (value === tab || returningTabFocus.current) return;
    if (dirty) {
      tabReturnFocus.current = tabTriggers.current.get(value) ?? null;
      setPendingTab(value);
    } else setTab(value);
  }
  async function save() {
    setSaving(true);
    setSaveError(false);
    setNotice(undefined);
    const selected = new Set(saved);
    for (const [id, on] of changes) {
      if (on) selected.add(id);
      else selected.delete(id);
    }
    try {
      await apiClient.replaceUserPermissions(
        item.id,
        [...selected],
        limitChanges,
      );
      await cache.invalidateQueries({ queryKey: ["user-activity", item.id] });
      await cache.invalidateQueries({ queryKey: ["service-permissions"] });
      await cache.invalidateQueries({ queryKey: USERS_QUERY_KEY });
      setDraft(new Map());
      setLimitDraft({});
      setNotice("服务权限已生效");
    } catch {
      setSaveError(true);
    } finally {
      setSaving(false);
    }
  }
  const actions = {
    apiClient,
    isSelf: self,
    onFailure: setCommandError,
    onStart: () => {
      setCommandError(undefined);
      setNotice(undefined);
    },
    onSuccess: (_kind: unknown, message: string) => {
      setNotice(message);
      void cache.invalidateQueries({ queryKey: ["user-activity", item.id] });
    },
    user: item,
  };
  const title = item.display_name || item.username;
  const tabs = [
    ["overview", "概览"],
    ["permissions", `服务权限 ${String(item.authorized_services.length)}`],
    ["keys", `API 密钥 ${String(item.api_key_count)}`],
    ["usage", "用量"],
    ["activity", "活动"],
  ];
  return (
    <article className="pw-user-detail">
      <nav aria-label="面包屑">
        <Link to="/users">用户</Link>
        <span aria-hidden="true"> › </span>
        <span aria-current="page">{title}</span>
      </nav>
      <header className="pw-user-detail__head">
        <div>
          <div className="pw-user-detail__identity">
            <h1>{title}</h1>
            <UserStatusBadge value={item.status} />
            <span className="pw-user-detail__role">
              {item.role === "admin" ? "管理员" : "普通用户"}
            </span>
          </div>
          <p>
            @{item.username}
            {self ? " · 当前登录的账号" : ""}
          </p>
        </div>
        <div className="pw-service-actions">
          <UserActions {...actions} />
          {self && item.status !== "deleted" ? (
            <Link to="/change-password">修改我的密码</Link>
          ) : null}
        </div>
      </header>
      {self && item.status !== "deleted" ? (
        <InlineAlert title="当前登录的账号" tone="info">
          这是你当前登录的账号。为避免把自己锁在门外，不能在这里禁用或删除自己；需要时请另一位管理员操作。修改自己的密码请用「修改我的密码」。
        </InlineAlert>
      ) : null}
      {item.status === "disabled" ? (
        <div className="pw-user-detail__row">
          <InlineAlert title="已禁用" tone="notice">
            不能登录；
            {item.api_key_count > 0
              ? `${String(item.api_key_count)} 个 API 密钥暂停使用；`
              : ""}
            服务授权与历史用量保留。启用后恢复。
          </InlineAlert>
          <UserActions {...actions} enableOnly />
        </div>
      ) : null}
      {item.status === "deleted" ? (
        <InlineAlert title="已删除" tone="info">
          删除时全部 API
          密钥已永久撤销、服务授权已移除；历史用量与审计记录保留。账号不能恢复，用户名也不能再次使用。本页只读。
        </InlineAlert>
      ) : null}
      {item.status === "active" && item.must_change_password ? (
        <InlineAlert title="尚未完成首次登录" tone="info">
          用户首次登录时必须设置新密码。临时密码丢失时，可以「重置密码」重新生成。
        </InlineAlert>
      ) : null}
      {stale ? (
        <InlineAlert title="用户详情未更新" tone="notice">
          当前显示上次获取的数据，请稍后刷新。
        </InlineAlert>
      ) : null}
      {commandError ? (
        <InlineAlert title="操作未完成" tone="negative">
          {commandError}
        </InlineAlert>
      ) : null}
      {notice ? <p role="status">{notice}</p> : null}
      <RadixTabs.Root className="pw-tabs" value={tab} onValueChange={goTab}>
        <RadixTabs.List className="pw-tabs__list" aria-label="用户详情">
          {tabs.map(([id, label]) => (
            <RadixTabs.Trigger
              className="pw-tabs__trigger"
              key={id}
              ref={(node) => {
                if (node) tabTriggers.current.set(id ?? "", node);
                else tabTriggers.current.delete(id ?? "");
              }}
              value={id ?? ""}
            >
              {label}
            </RadixTabs.Trigger>
          ))}
        </RadixTabs.List>
        <RadixTabs.Content className="pw-tabs__content" value="overview">
          <div className="pw-user-detail__grid">
            <section className="pw-user-detail__card">
              <h2>账号</h2>
              <dl className="pw-service-facts">
                {[
                  ["用户名", item.username],
                  ["显示名称", item.display_name || "（未填写，显示用户名）"],
                  ["角色", item.role === "admin" ? "管理员" : "普通用户"],
                  ["状态", userStatusLabel(item.status)],
                  ["创建时间", detailDate(item.created_at)],
                  ["最近登录", detailDate(item.last_login_at, "从未登录")],
                  [
                    "首次改密",
                    item.must_change_password
                      ? "未完成（下次登录时必须设置新密码）"
                      : "已完成",
                  ],
                ].map(([label, value]) => (
                  <div key={label}>
                    <dt>{label}</dt>
                    <dd>{value}</dd>
                  </div>
                ))}
              </dl>
            </section>
            <div className="pw-user-detail__stack">
              <section className="pw-user-detail__card">
                <div className="pw-user-detail__row">
                  <h2>用量</h2>
                  <Button
                    variant="ghost"
                    onClick={() => {
                      goTab("usage");
                    }}
                  >
                    查看用量明细
                  </Button>
                </div>
                <div className="pw-user-detail__grid">
                  {(
                    [
                      ["今日", today],
                      ["本月", month],
                    ] as const
                  ).map(([label, query]) => {
                    const summary = query.isError
                      ? undefined
                      : query.data?.summary;
                    return (
                      <div className="pw-user-detail__tile" key={label}>
                        <span>{label} Tokens</span>
                        <strong>
                          <UsageNumber value={summary?.total_tokens} />
                        </strong>
                        <span>
                          <UsageNumber value={summary?.requests} /> 次请求
                        </span>
                      </div>
                    );
                  })}
                </div>
              </section>
              <section className="pw-user-detail__card">
                <h2>访问</h2>
                <dl className="pw-service-facts">
                  <div>
                    <dt>服务权限</dt>
                    <dd>
                      {saved.size
                        ? `已授权 ${String(saved.size)} 个 AI 服务`
                        : "未授权任何 AI 服务"}{" "}
                      <Button
                        variant="ghost"
                        onClick={() => {
                          goTab("permissions");
                        }}
                      >
                        管理
                      </Button>
                    </dd>
                  </div>
                  <div>
                    <dt>API 密钥</dt>
                    <dd>
                      {item.status === "deleted"
                        ? `全部已撤销（${String(item.api_keys.length)} 个）`
                        : `${String(item.api_key_count)} 个生效中${item.status === "disabled" && item.api_key_count ? "（暂停使用）" : ""}${oldN ? `，${String(oldN)} 个已过期或已撤销` : ""}`}{" "}
                      <Button
                        variant="ghost"
                        onClick={() => {
                          goTab("keys");
                        }}
                      >
                        查看
                      </Button>
                    </dd>
                  </div>
                </dl>
              </section>
            </div>
          </div>
        </RadixTabs.Content>
        <RadixTabs.Content className="pw-tabs__content" value="permissions">
          <section className="pw-user-detail__stack" aria-label="服务权限">
            <p>
              勾选该用户可以调用的 AI 服务。这里与 AI
              服务详情的「访问权限」改的是同一份权限。
            </p>
            {item.status === "deleted" ? (
              <p>用户已删除，全部服务授权已在删除时移除。</p>
            ) : (
              <>
                <fieldset
                  disabled={saving || services.isError}
                  className="pw-user-detail__choices"
                >
                  <legend>允许的 AI 服务</legend>
                  {services.isPending ? (
                    <p role="status">正在加载 AI 服务…</p>
                  ) : services.isError ? (
                    <InlineAlert title="无法获取 AI 服务" tone="negative">
                      请稍后重试。
                    </InlineAlert>
                  ) : services.data.items.length === 0 ? (
                    <p>当前没有可授权的 AI 服务。</p>
                  ) : (
                    services.data.items.map((service) => {
                      const on = draft.get(service.id) ?? saved.has(service.id);
                      return (
                        <div
                          key={service.id}
                          style={{
                            display: "flex",
                            flexWrap: "wrap",
                            alignItems: "center",
                            gap: "var(--pw-space-12)",
                            borderBottom:
                              "var(--pw-width-hairline) solid var(--pw-border-hairline)",
                          }}
                        >
                          <label
                            className="pw-user-detail__choice"
                            style={{
                              flex: "1 1 auto",
                              minWidth: 0,
                              borderBottom: 0,
                            }}
                          >
                            <input
                              type="checkbox"
                              aria-label={`${service.name} · ${service.public_model_name}`}
                              checked={on}
                              onChange={() => {
                                setDraft(new Map(draft).set(service.id, !on));
                                setSaveError(false);
                                setNotice(undefined);
                              }}
                            />
                            <span>
                              <strong>{service.name}</strong>{" "}
                              <code>{service.public_model_name}</code>
                              {!service.enabled ? <span> 已停用</span> : null}
                              <small>
                                {!service.enabled
                                  ? "服务已停用；保留授权，服务重新启用后即可调用"
                                  : on
                                    ? "可以调用"
                                    : "未授权"}
                              </small>
                            </span>
                            {on !== saved.has(service.id) ? (
                              <span>{on ? "将授权" : "将撤销"}</span>
                            ) : null}
                          </label>
                          {on ? (
                            <PermissionLimitFields
                              name={service.name}
                              value={
                                limitDraft[service.id] ??
                                savedLimits[service.id] ??
                                DEFAULT_PERMISSION_LIMITS
                              }
                              disabled={saving}
                              onChange={(value) => {
                                setLimitDraft({
                                  ...limitDraft,
                                  [service.id]: value,
                                });
                                setSaveError(false);
                                setNotice(undefined);
                              }}
                            />
                          ) : null}
                        </div>
                      );
                    })
                  )}
                </fieldset>
                {dirty ? (
                  <div
                    className="pw-user-detail__save"
                    role="region"
                    aria-label="未保存的更改"
                  >
                    <span>
                      {`未保存：${[
                        added ? `新增授权 ${String(added)} 个` : "",
                        removed ? `撤销 ${String(removed)} 个` : "",
                        limitCount ? `修改上限 ${String(limitCount)} 个` : "",
                      ]
                        .filter(Boolean)
                        .join(
                          "、",
                        )}。${removed ? "撤销后，该用户用这些服务的调用立即被拒绝。" : "保存后立即生效。"}`}
                    </span>
                    <Button disabled={saving} onClick={discard}>
                      放弃更改
                    </Button>
                    <Button
                      disabled={services.isError}
                      loading={saving}
                      variant="primary"
                      onClick={() => void save()}
                    >
                      保存
                    </Button>
                  </div>
                ) : null}
                {saveError ? (
                  <InlineAlert title="服务权限未保存" tone="negative">
                    控制面暂时没有响应，权限没有改变。勾选保留，可以直接重试。
                  </InlineAlert>
                ) : null}
              </>
            )}
          </section>
        </RadixTabs.Content>
        <RadixTabs.Content className="pw-tabs__content" value="keys">
          <UserKeys apiClient={apiClient} item={item} />
        </RadixTabs.Content>
        <RadixTabs.Content className="pw-tabs__content" value="usage">
          <UserUsage apiClient={apiClient} userId={item.id} />
        </RadixTabs.Content>
        <RadixTabs.Content className="pw-tabs__content" value="activity">
          <UserActivity apiClient={apiClient} userId={item.id} />
        </RadixTabs.Content>
      </RadixTabs.Root>
      <Modal
        trigger={<span hidden />}
        open={pendingTab !== undefined}
        onCloseAutoFocus={(event) => {
          event.preventDefault();
          // Radix automatically activates focused tabs; restoring focus must not reopen the dialog.
          returningTabFocus.current = true;
          tabReturnFocus.current?.focus();
          returningTabFocus.current = false;
        }}
        onOpenChange={(open) => {
          if (!open) setPendingTab(undefined);
        }}
        title="有未保存的服务权限更改"
        description="离开后，本次勾选的更改不会生效。"
      >
        <div className="pw-service-form__actions">
          <Button
            onClick={() => {
              setPendingTab(undefined);
            }}
          >
            继续编辑
          </Button>
          <Button
            variant="danger"
            onClick={() => {
              discard();
              setTab(pendingTab ?? tab);
              setPendingTab(undefined);
            }}
          >
            放弃更改并离开
          </Button>
        </div>
      </Modal>
    </article>
  );
}
function UserKeys({
  apiClient,
  item,
}: {
  apiClient: ApiClient;
  item: UserDetail;
}) {
  const cache = useQueryClient();
  const [showOld, setShowOld] = useState(false);
  const [target, setTarget] = useState<SafeApiKey>();
  const revokeReturnFocus = useRef<HTMLButtonElement | null>(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string>();
  const [notice, setNotice] = useState<string>();
  const usage = useUserUsage(apiClient, item.id, "keys", "api_key");
  const oldN = item.api_keys.filter((key) => key.status !== "Active").length;
  const rows = item.api_keys.filter(
    (key) => showOld || key.status === "Active",
  );
  async function revoke() {
    if (!target) return;
    setPending(true);
    setError(undefined);
    setNotice(undefined);
    try {
      await apiClient.revokeUserApiKey(item.id, target.id);
      setNotice(`已撤销 ${target.name}`);
      await cache.invalidateQueries({ queryKey: USERS_QUERY_KEY });
      await cache.invalidateQueries({ queryKey: ["user-activity", item.id] });
    } catch (caught) {
      setError(
        caught instanceof ApiError
          ? (caught.responseMessage ?? "密钥未撤销，请稍后重试。")
          : "密钥未撤销，请稍后重试。",
      );
    } finally {
      setPending(false);
      setTarget(undefined);
    }
  }
  return (
    <section className="pw-user-detail__stack" aria-label="API 密钥">
      <div className="pw-user-detail__row">
        <p>
          密钥由用户在自己的「API
          密钥」页创建。管理员只能看到名称、前缀和后四位，看不到明文。
        </p>
        <label>
          <input
            type="checkbox"
            checked={showOld}
            onChange={(event) => {
              setShowOld(event.target.checked);
            }}
          />
          显示已过期和已撤销（{oldN}）
        </label>
      </div>
      {error ? (
        <InlineAlert title="操作未完成" tone="negative">
          {error}
        </InlineAlert>
      ) : null}
      {notice ? <p role="status">{notice}</p> : null}
      {rows.length === 0 ? (
        <p>
          {item.api_keys.length === 0
            ? "该用户还没有创建 API 密钥。用户登录后可以在自己的「API 密钥」页创建。"
            : "没有生效中的 API 密钥。勾选右上角可查看已过期和已撤销的密钥。"}
        </p>
      ) : (
        <div className="pw-user-detail__table">
          <table className="pw-table">
            <caption className="pw-visually-hidden">用户 API 密钥</caption>
            <thead>
              <tr>
                {[
                  "名称",
                  "密钥",
                  "状态",
                  "最近使用",
                  "到期时间",
                  "近 30 天 Tokens",
                  "操作",
                ].map((label) => (
                  <th key={label} scope="col">
                    {label}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((key) => {
                const tokens =
                  usage.isError || usage.data === undefined
                    ? undefined
                    : usage.data.details.find((row) => row.group_id === key.id)
                        ?.total_tokens;
                const hasRow = usage.data?.details.some(
                  (row) => row.group_id === key.id,
                );
                return (
                  <tr key={key.id}>
                    <th scope="row">{key.name}</th>
                    <td>
                      <code
                        aria-label={`${key.display_prefix}省略${key.last4}`}
                      >
                        {key.display_prefix}…{key.last4}
                      </code>
                    </td>
                    <td>
                      {key.status === "Active" && item.status === "disabled" ? (
                        <span title="用户已禁用时的显示状态，不是密钥本身的状态；启用用户后恢复为生效中">
                          暂停使用
                        </span>
                      ) : (
                        <StatusBadge
                          text={
                            key.status === "Active"
                              ? "生效中"
                              : key.status === "Expired"
                                ? "已过期"
                                : "已撤销"
                          }
                          tone={key.status === "Active" ? "positive" : "idle"}
                          icon={key.status === "Active" ? "success" : "pause"}
                        />
                      )}
                    </td>
                    <td>{detailDate(key.last_used_at, "从未使用")}</td>
                    <td>{detailDate(key.expires_at, "永不过期")}</td>
                    <td>
                      <UsageNumber
                        value={usage.isSuccess && !hasRow ? 0 : tokens}
                      />
                    </td>
                    <td>
                      {key.status === "Active" && item.status !== "deleted" ? (
                        <Button
                          variant="ghost"
                          onClick={(event) => {
                            revokeReturnFocus.current = event.currentTarget;
                            setTarget(key);
                          }}
                        >
                          撤销
                        </Button>
                      ) : null}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      {!showOld && oldN > 0 ? (
        <p className="pw-user-detail__muted">
          已隐藏 {oldN} 个已过期或已撤销的密钥；它们的历史用量仍计入用量统计。
        </p>
      ) : null}
      <Modal
        trigger={<span hidden />}
        open={target !== undefined}
        onCloseAutoFocus={(event) => {
          event.preventDefault();
          revokeReturnFocus.current?.focus();
        }}
        onOpenChange={(open) => {
          if (!open && !pending) setTarget(undefined);
        }}
        title={`撤销 API 密钥「${target?.name ?? ""}」？`}
        description="请确认下面的影响后再继续。"
      >
        <ul className="pw-user-confirm-lines">
          <li>
            立即生效且不可恢复：用这个密钥的调用会被拒绝（api_key_revoked）。
          </li>
          <li>用户需要在自己的「API 密钥」页新建密钥。</li>
          <li>
            历史用量保留；这次撤销记入该用户的「活动」与审计日志（操作者为你）。
          </li>
        </ul>
        <div className="pw-service-form__actions">
          <Button
            disabled={pending}
            onClick={() => {
              setTarget(undefined);
            }}
          >
            取消
          </Button>
          <Button
            loading={pending}
            variant="danger"
            onClick={() => void revoke()}
          >
            撤销密钥
          </Button>
        </div>
      </Modal>
    </section>
  );
}
