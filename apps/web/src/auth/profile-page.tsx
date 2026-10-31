import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Controller, useForm } from "react-hook-form";
import { useEffect, useRef, useState } from "react";
import { Link } from "react-router";
import { z } from "zod";
import { ApiError, type ApiClient } from "../api/client";
import { Button, InlineAlert, TextField } from "../components";
import { useSession } from "./session";

const profileSchema = z.object({
  display_name: z
    .string()
    .refine((value) => Array.from(value).length <= 80, "最多 80 个字符。"),
});

function loginDate(value: string, today = true) {
  const date = new Date(value);
  const pad = (part: number) => String(part).padStart(2, "0");
  const day =
    today && date.toDateString() === new Date().toDateString()
      ? "今天"
      : `${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
  return `${day} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

export function ProfilePage({ apiClient }: { apiClient: ApiClient }) {
  const session = useSession();
  const cache = useQueryClient();
  const heading = useRef<HTMLHeadingElement>(null);
  const [editing, setEditing] = useState(false);
  const [saved, setSaved] = useState(false);
  const queryKey = ["my-profile", session.me?.user.id];
  const profile = useQuery({
    queryKey,
    queryFn: () => apiClient.getMyProfile(),
  });
  const form = useForm<z.infer<typeof profileSchema>>({
    defaultValues: { display_name: "" },
    resolver: zodResolver(profileSchema),
  });
  const mutation = useMutation({
    mutationFn: async (values: z.infer<typeof profileSchema>) => {
      const result = await apiClient.updateMyProfile(values);
      cache.setQueryData(queryKey, result);
      await session.refresh();
      return result;
    },
    onSuccess: () => {
      setEditing(false);
      setSaved(true);
    },
  });
  useEffect(() => {
    heading.current?.focus();
  }, []);
  const item = profile.data;
  return (
    <section className="pw-account-page" aria-busy={profile.isPending}>
      <h1 ref={heading} tabIndex={-1}>
        个人资料
      </h1>
      {profile.isPending ? <p role="status">正在加载个人资料…</p> : null}
      {profile.isError ? (
        <InlineAlert
          title={
            profile.error instanceof ApiError && profile.error.status === 403
              ? "无权查看个人资料"
              : item
                ? "个人资料可能已过期"
                : "无法获取个人资料"
          }
          tone="negative"
        >
          请稍后重试。
          <Button onClick={() => void profile.refetch()}>重试</Button>
        </InlineAlert>
      ) : null}
      {saved ? <span role="status">已保存</span> : null}
      {item ? (
        <dl className="pw-profile-facts">
          <dt>用户名</dt>
          <dd>
            <code>{item.username}</code>
          </dd>
          <dt>显示名称</dt>
          <dd>
            {editing ? (
              <form
                className="pw-profile-edit"
                onSubmit={(event) =>
                  void form.handleSubmit((values) => {
                    mutation.mutate(values);
                  })(event)
                }
              >
                <Controller
                  control={form.control}
                  name="display_name"
                  render={({ field }) => (
                    <TextField
                      {...field}
                      autoFocus
                      id="profile-display-name"
                      label="显示名称"
                      errorText={form.formState.errors.display_name?.message}
                    />
                  )}
                />
                <Button
                  type="submit"
                  variant="primary"
                  loading={mutation.isPending}
                >
                  保存
                </Button>
                <Button
                  disabled={mutation.isPending}
                  onClick={() => {
                    setEditing(false);
                    mutation.reset();
                  }}
                >
                  取消
                </Button>
                {mutation.isError ? (
                  <InlineAlert title="显示名称未保存" tone="negative">
                    请稍后重试。
                  </InlineAlert>
                ) : null}
              </form>
            ) : (
              <>
                <span>{item.display_name || item.username}</span>
                <Button
                  size="s"
                  variant="ghost"
                  onClick={() => {
                    form.reset({ display_name: item.display_name });
                    setSaved(false);
                    setEditing(true);
                  }}
                >
                  修改
                </Button>
              </>
            )}
          </dd>
          <dt>角色</dt>
          <dd>{item.role === "admin" ? "管理员" : "普通用户"}</dd>
          <dt>最近登录</dt>
          <dd>
            {loginDate(item.current_login_at)}
            {item.previous_login_at
              ? `（上次 ${loginDate(item.previous_login_at, false)}）`
              : ""}
          </dd>
          <dt>账号创建时间</dt>
          <dd>
            {new Date(item.created_at).toLocaleString("zh-CN", {
              year: "numeric",
              month: "2-digit",
              day: "2-digit",
              hour: "2-digit",
              minute: "2-digit",
            })}
          </dd>
          <dt>已授权服务</dt>
          <dd>
            <Link
              to={
                item.role === "admin"
                  ? `/users/${session.me?.user.id ?? ""}`
                  : "/services"
              }
              state={item.role === "admin" ? { tab: "permissions" } : undefined}
            >
              {item.authorized_service_count} 个服务
            </Link>
          </dd>
        </dl>
      ) : null}
    </section>
  );
}
