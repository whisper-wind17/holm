import { useInfiniteQuery } from "@tanstack/react-query";
import { useState } from "react";
import type { ApiClient } from "../api/client";
import type {
  UserActivityAction,
  UserActivityItem,
} from "../api/user-contracts";
import { useAiServices } from "../ai-services/queries";
import { Button, InlineAlert, StatusBadge } from "../components";

export function activityText(
  item: UserActivityItem,
  names: ReadonlyMap<string, string>,
) {
  const op = item.operation;
  if (item.action === "auth.login")
    return item.result === "succeeded" ? "登录成功" : "登录未成功";
  if (op === null)
    return {
      "account.change": "修改账号",
      "account.password_change": "修改密码",
      "permission.change": "修改服务权限",
      "api_key.change": "变更 API 密钥",
    }[item.action];
  if (item.action === "account.password_change")
    return op === "reset" ? "重置密码，撤销全部登录会话" : "修改密码";
  if (item.action === "account.change")
    return (
      (
        {
          create: "创建账号",
          bootstrap: "初始化时创建管理员账号",
          disable: "禁用账号",
          enable: "启用账号",
          delete: "删除账号；撤销全部 API 密钥、移除全部服务授权",
        } as Record<string, string>
      )[op] ?? "修改账号"
    );
  if (item.action === "api_key.change")
    return `${({ create: "创建", rename: "改名", revoke: "撤销", delete: "删除记录" } as Record<string, string>)[op] ?? "变更"} API 密钥${item.api_key === null ? "" : `「${item.api_key.name}」…${item.api_key.last4}`}`;
  const list = (ids: string[]) =>
    ids.map((id) => `「${names.get(id) ?? "已删除的 AI 服务"}」`).join("、");
  const parts = [
    item.limit_changed_service_ids.length
      ? `调整 ${String(item.limit_changed_service_ids.length)} 个 AI 服务的调用上限`
      : "",
    item.added_service_ids.length
      ? `授权 AI 服务${list(item.added_service_ids)}`
      : "",
    item.removed_service_ids.length
      ? `撤销 AI 服务${list(item.removed_service_ids)}的授权`
      : "",
  ].filter(Boolean);
  return (
    (parts.join("；") || "服务权限未变化") +
    (op === "replace_by_service" ? "（在 AI 服务页操作）" : "")
  );
}
const filters: { label: string; actions: UserActivityAction[] }[] = [
  { label: "全部", actions: [] },
  { label: "登录", actions: ["auth.login"] },
  {
    label: "账号与密码",
    actions: ["account.change", "account.password_change"],
  },
  { label: "API 密钥", actions: ["api_key.change"] },
  { label: "服务权限", actions: ["permission.change"] },
];
export function UserActivity({
  apiClient,
  userId,
}: {
  apiClient: ApiClient;
  userId: string;
}) {
  const [filter, setFilter] = useState(0);
  const services = useAiServices(apiClient);
  const names = new Map(
    services.data?.items.map((item) => [item.id, item.name]),
  );
  const events = useInfiniteQuery({
    queryKey: ["user-activity", userId, filter],
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam }) =>
      apiClient.getUserActivity(userId, {
        actions: filters[filter]?.actions ?? [],
        cursor: pageParam,
        limit: 50,
      }),
    getNextPageParam: (page) => page.next_cursor ?? undefined,
  });
  const rows = events.data?.pages.flatMap((page) => page.items) ?? [];
  return (
    <section className="pw-user-detail__stack" aria-label="活动">
      <div role="group" aria-label="活动类型">
        {filters.map((item, i) => (
          <Button
            key={item.label}
            aria-pressed={filter === i}
            onClick={() => {
              setFilter(i);
            }}
          >
            {item.label}
          </Button>
        ))}
      </div>
      {events.isError ? (
        <InlineAlert title="无法获取活动" tone="negative">
          活动暂时无法显示。
          <Button onClick={() => void events.refetch()}>重试</Button>
        </InlineAlert>
      ) : null}
      {events.isPending ? (
        <p role="status">正在加载活动…</p>
      ) : rows.length === 0 && !events.isError ? (
        <p>没有这一类活动记录。</p>
      ) : (
        <div className="pw-user-detail__table">
          <table className="pw-table">
            <caption className="pw-visually-hidden">用户活动</caption>
            <thead>
              <tr>
                {["时间", "事件", "操作者", "结果"].map((label) => (
                  <th key={label} scope="col">
                    {label}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((item) => {
                const date = new Date(item.created_at);
                const at = `${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")} ${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
                return (
                  <tr key={item.id}>
                    <td title={date.toLocaleString()}>{at}</td>
                    <td>
                      {item.action === "permission.change" &&
                      item.limit_changed_service_ids.length > 0 ? (
                        <>
                          <div>
                            调整 {item.limit_changed_service_ids.length} 个 AI
                            服务的调用上限
                          </div>
                          {item.added_service_ids.length > 0 ? (
                            <div>
                              授权 {item.added_service_ids.length} 个 AI 服务
                            </div>
                          ) : null}
                          {item.removed_service_ids.length > 0 ? (
                            <div>
                              撤销 {item.removed_service_ids.length} 个 AI
                              服务的授权
                            </div>
                          ) : null}
                        </>
                      ) : (
                        activityText(item, names)
                      )}
                    </td>
                    <td>
                      {item.actor.type === "system"
                        ? "系统"
                        : item.actor.type === "user"
                          ? `${item.actor.display_name || item.actor.username || "—"}${item.actor.id === userId ? "（本人）" : ""}`
                          : "—"}
                    </td>
                    <td>
                      <StatusBadge
                        icon={item.result === "succeeded" ? "success" : "error"}
                        tone={
                          item.result === "succeeded" ? "positive" : "negative"
                        }
                        text={item.result === "succeeded" ? "成功" : "失败"}
                      />
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      {events.hasNextPage ? (
        <Button
          loading={events.isFetchingNextPage}
          onClick={() => void events.fetchNextPage()}
        >
          加载更多
        </Button>
      ) : null}
      <p className="pw-user-detail__muted">
        记录登录、密码、账号状态、API
        密钥与服务权限的变化，最新的在前，不含任何请求或回复内容。
      </p>
    </section>
  );
}
