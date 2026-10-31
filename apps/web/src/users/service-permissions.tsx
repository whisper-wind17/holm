import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import type { ApiClient } from "../api/client";
import type { PermissionLimitsMap, UserOverview } from "../api/user-contracts";
import { useUnsavedChangesGuard } from "../app/navigation-guard";
import { Button, InlineAlert, TextField } from "../components";
import {
  changedPermissionLimits,
  DEFAULT_PERMISSION_LIMITS,
  PermissionLimitFields,
} from "./permission-limits";
import { USERS_QUERY_KEY, useUsers } from "./queries";

export function servicePermissionsQueryKey(serviceId: string) {
  return ["service-permissions", serviceId] as const;
}

function userLabel(user: UserOverview) {
  return `${user.display_name || user.username} (@${user.username})`;
}

export function ServicePermissions({
  apiClient,
  serviceId,
}: {
  apiClient: ApiClient;
  serviceId: string;
}) {
  const queryClient = useQueryClient();
  const users = useUsers(apiClient);
  const permissions = useQuery({
    queryFn: () => apiClient.getServicePermissions(serviceId),
    queryKey: servicePermissionsQueryKey(serviceId),
  });
  // The draft holds only the users whose target state the administrator has
  // set; it is applied on top of the saved grants and sent as one PUT.
  const [draft, setDraft] = useState<ReadonlyMap<string, boolean>>(new Map());
  const [limitDraft, setLimitDraft] = useState<PermissionLimitsMap>({});
  const [selected, setSelected] = useState<ReadonlySet<string>>(new Set());
  const [search, setSearch] = useState("");
  const [notice, setNotice] = useState(false);
  const [error, setError] = useState(false);
  const save = useMutation({
    mutationFn: ({
      userIds,
      limits,
    }: {
      userIds: readonly string[];
      limits: PermissionLimitsMap;
    }) => apiClient.replaceServicePermissions(serviceId, userIds, limits),
  });

  const saved = new Set(permissions.data?.user_ids ?? []);
  // Entries equal to the saved state are not changes (e.g. grant, then remove).
  const changes = [...draft].filter(([id, to]) => saved.has(id) !== to);
  const granted = (id: string) => draft.get(id) ?? saved.has(id);

  const savedLimits = permissions.data?.limits ?? {};
  const limitChanges = changedPermissionLimits(
    limitDraft,
    savedLimits,
    granted,
  );
  const changeCount = changes.length + Object.keys(limitChanges).length;

  useUnsavedChangesGuard(
    changeCount === 0
      ? null
      : {
          discard: discardDraft,
          message: `「访问权限」有 ${String(changeCount)} 项更改尚未保存。离开后这些更改会丢失，原授权不变。`,
          title: "放弃未保存的访问权限更改？",
        },
  );

  function discardDraft() {
    setDraft(new Map());
    setLimitDraft({});
    setSelected(new Set());
    setNotice(false);
    setError(false);
  }

  function toggle(id: string) {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  function move(rows: readonly UserOverview[], to: boolean) {
    const ids = rows.filter(({ id }) => selected.has(id)).map(({ id }) => id);
    if (ids.length === 0) return;
    setNotice(false);
    setError(false);
    setDraft((current) => {
      const next = new Map(current);
      for (const id of ids) {
        if (saved.has(id) === to) next.delete(id);
        else next.set(id, to);
      }
      return next;
    });
    setSelected((current) => {
      const next = new Set(current);
      for (const id of ids) next.delete(id);
      return next;
    });
  }

  async function submit() {
    setNotice(false);
    setError(false);
    // Full replacement: saved grants (including users not in the list, such as
    // deleted ones) with the draft applied.
    const next = new Set(saved);
    for (const [id, to] of draft) {
      if (to) next.add(id);
      else next.delete(id);
    }
    try {
      const result = await save.mutateAsync({
        userIds: [...next],
        limits: limitChanges,
      });
      queryClient.setQueryData(servicePermissionsQueryKey(serviceId), result);
      setDraft(new Map());
      setLimitDraft({});
      setSelected(new Set());
      setNotice(true);
      await queryClient.invalidateQueries({
        queryKey: servicePermissionsQueryKey(serviceId),
      });
      await queryClient.invalidateQueries({ queryKey: USERS_QUERY_KEY });
      await queryClient.invalidateQueries({ queryKey: ["user-activity"] });
    } catch {
      // The draft stays so the administrator can retry.
      setError(true);
    }
  }

  const loading = users.isPending || permissions.isPending;
  const unavailable = users.isError || permissions.isError;
  const term = search.trim().toLowerCase();
  const matches = (user: UserOverview) =>
    term === "" ||
    user.username.toLowerCase().includes(term) ||
    user.display_name.toLowerCase().includes(term);
  const allUsers = users.data?.items ?? [];
  const authorized = allUsers.filter((user) => granted(user.id));
  // Only active users can be newly granted; a disabled user being removed
  // stays visible here so the removal can still be undone.
  const unauthorized = allUsers.filter(
    (user) =>
      !granted(user.id) && (user.status === "active" || saved.has(user.id)),
  );
  const authorizedRows = authorized.filter(matches);
  const unauthorizedRows = unauthorized.filter(matches);
  const selectedIn = authorizedRows.filter(({ id }) => selected.has(id)).length;
  const selectedOut = unauthorizedRows.filter(({ id }) =>
    selected.has(id),
  ).length;

  return (
    <section className="pw-service-detail__section">
      <h2>访问权限</h2>
      {unavailable ? (
        <InlineAlert title="无法获取用户权限" tone="negative">
          当前授权状态无法显示。请刷新页面后重试。
        </InlineAlert>
      ) : loading ? (
        <div aria-busy="true" aria-label="访问权限正在加载" role="status">
          <span className="sk pw-service-detail__skeleton" />
        </div>
      ) : (
        <div className="pw-service-scope-access">
          <p className="pw-service-scope-access__hint">
            已授权的用户可以用自己的 API 密钥调用这个服务；授权不会替用户创建
            API 密钥。
          </p>
          <TextField
            label="搜索用户名或显示名"
            onChange={(event) => {
              setSearch(event.currentTarget.value);
            }}
            type="search"
            value={search}
          />
          <div className="pw-service-scope-access__columns">
            <section aria-labelledby="pw-scope-in">
              <h3 id="pw-scope-in">
                已授权 <small>{authorized.length} 人</small>
              </h3>
              <Button
                disabled={selectedIn === 0}
                onClick={() => {
                  move(authorizedRows, false);
                }}
                size="s"
              >
                移除选中（{selectedIn}）
              </Button>
              {authorizedRows.length === 0 ? (
                <p className="pw-service-scope__empty">
                  {term === ""
                    ? "还没有授权任何用户。在右侧选择用户后授权。"
                    : "没有匹配的已授权用户。"}
                </p>
              ) : (
                <ul className="pw-service-scope-access__list">
                  {authorizedRows.map((user) => (
                    <li
                      key={user.id}
                      style={{
                        flexWrap: "wrap",
                        paddingBlock: "var(--pw-space-8)",
                      }}
                    >
                      <label>
                        <input
                          checked={selected.has(user.id)}
                          onChange={() => {
                            toggle(user.id);
                          }}
                          type="checkbox"
                        />
                        <span>{userLabel(user)}</span>
                      </label>
                      <span className="pw-service-scope-access__tags">
                        {user.status === "disabled" ? (
                          <span>用户已禁用</span>
                        ) : null}
                        {!saved.has(user.id) ? <span>待授权</span> : null}
                        {saved.has(user.id) && limitChanges[user.id] ? (
                          <span>待改上限</span>
                        ) : null}
                      </span>
                      <PermissionLimitFields
                        name={user.username}
                        serviceSide
                        value={
                          limitDraft[user.id] ??
                          savedLimits[user.id] ??
                          DEFAULT_PERMISSION_LIMITS
                        }
                        disabled={save.isPending}
                        onChange={(value) => {
                          setLimitDraft({ ...limitDraft, [user.id]: value });
                          setNotice(false);
                          setError(false);
                        }}
                      />
                    </li>
                  ))}
                </ul>
              )}
            </section>
            <section aria-labelledby="pw-scope-out">
              <h3 id="pw-scope-out">
                未授权 <small>{unauthorized.length} 人</small>
              </h3>
              <Button
                disabled={selectedOut === 0}
                onClick={() => {
                  move(unauthorizedRows, true);
                }}
                size="s"
              >
                授权选中（{selectedOut}）
              </Button>
              {unauthorizedRows.length === 0 ? (
                <p className="pw-service-scope__empty">
                  {term === ""
                    ? "全部活跃用户都已授权。"
                    : "没有匹配的未授权用户。"}
                </p>
              ) : (
                <ul className="pw-service-scope-access__list">
                  {unauthorizedRows.map((user) => (
                    <li key={user.id}>
                      <label>
                        <input
                          checked={selected.has(user.id)}
                          onChange={() => {
                            toggle(user.id);
                          }}
                          type="checkbox"
                        />
                        <span>{userLabel(user)}</span>
                      </label>
                      <span className="pw-service-scope-access__tags">
                        {saved.has(user.id) ? <span>待移除</span> : null}
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </section>
          </div>
          <p className="pw-service-scope-access__hint">
            已禁用的用户只在「已授权」中保留显示，便于理解历史配置；他们的密钥在启用前不能调用。未授权区只列出活跃用户。
          </p>
          {notice ? (
            <InlineAlert title="访问权限已生效" tone="positive">
              该 AI 服务的授权用户已更新。
            </InlineAlert>
          ) : null}
          {error ? (
            <InlineAlert title="访问权限未保存" tone="negative">
              原授权仍然有效，未保存的更改仍保留在页面上。请重试。
            </InlineAlert>
          ) : null}
          {changeCount > 0 ? (
            <div
              aria-label="未保存的更改"
              className="pw-service-scope-access__bar"
              role="region"
            >
              <span>
                <strong>{changeCount} 项未保存的更改</strong>
                保存后立即生效；被移除的用户再调用会收到{" "}
                <code>service_not_authorized</code>。
              </span>
              <span className="pw-service-scope-access__bar-actions">
                <Button disabled={save.isPending} onClick={discardDraft}>
                  放弃更改
                </Button>
                <Button
                  loading={save.isPending}
                  onClick={() => void submit()}
                  variant="primary"
                >
                  保存访问权限
                </Button>
              </span>
            </div>
          ) : null}
        </div>
      )}
    </section>
  );
}
