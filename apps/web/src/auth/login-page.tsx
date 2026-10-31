import { PRODUCT_NAME } from "../app/brand";
import { zodResolver } from "@hookform/resolvers/zod";
import { Controller, useForm } from "react-hook-form";
import { useEffect, useState } from "react";
import { useLocation, useNavigate } from "react-router";
import { z } from "zod";

import { ApiError, type ApiClient } from "../api/client";
import { Button, InlineAlert, TextField } from "../components";
import { toAppError, type AppError } from "../app/errors";
import { useSession } from "./session";

const loginSchema = z.object({
  password: z.string().min(1, "请输入密码").max(4096),
  username: z.string().min(1, "请输入用户名").max(640),
});

type LoginFields = z.infer<typeof loginSchema>;

export function LoginPage({ apiClient }: { apiClient: ApiClient }) {
  const location = useLocation();
  const navigate = useNavigate();
  const session = useSession();
  const [showPassword, setShowPassword] = useState(false);
  const [loginFailure, setLoginFailure] = useState<
    { retryAfterSeconds?: number } | undefined
  >();
  const [lockedUntil, setLockedUntil] = useState(0);
  const [remaining, setRemaining] = useState(0);
  useEffect(() => {
    if (lockedUntil === 0) return;
    const tick = () => {
      setRemaining(Math.max(0, Math.ceil((lockedUntil - Date.now()) / 1000)));
    };
    tick();
    const timer = window.setInterval(tick, 1000);
    return () => {
      window.clearInterval(timer);
    };
  }, [lockedUntil]);
  const [unexpectedError, setUnexpectedError] = useState<AppError>();
  const {
    control,
    formState: { errors, isSubmitting },
    handleSubmit,
  } = useForm<LoginFields>({
    defaultValues: { password: "", username: "" },
    resolver: zodResolver(loginSchema),
  });

  if (unexpectedError !== undefined) {
    throw unexpectedError;
  }

  const intendedPath = intendedRoute(location.state);

  async function submit(values: LoginFields) {
    if (remaining > 0) return;
    setLoginFailure(undefined);
    try {
      await apiClient.login(values);
      const me = await session.refresh();
      void navigate(
        me.must_change_password ? "/change-password" : intendedPath,
        {
          replace: true,
          state: me.must_change_password ? { from: intendedPath } : undefined,
        },
      );
    } catch (error) {
      if (
        error instanceof ApiError &&
        (error.status === 400 || error.status === 401 || error.status === 429)
      ) {
        setLoginFailure({ retryAfterSeconds: error.retryAfterSeconds });
        const seconds = Math.max(0, error.retryAfterSeconds ?? 0);
        setRemaining(seconds);
        setLockedUntil(seconds > 0 ? Date.now() + seconds * 1000 : 0);
        document.querySelector<HTMLInputElement>("#login-password")?.focus();
        return;
      }
      setUnexpectedError(toAppError(error));
    }
  }

  return (
    <main className="pw-auth-canvas" id="main-content">
      <section aria-labelledby="login-title" className="pw-auth-card">
        <AuthBrand />
        <h1 id="login-title">登录</h1>
        {loginFailure === undefined ? null : loginFailure.retryAfterSeconds ===
          undefined ? (
          <InlineAlert title="用户名或密码不正确" tone="negative">
            请检查后重新输入。
          </InlineAlert>
        ) : remaining > 0 ? (
          <InlineAlert title="尝试次数过多" tone="notice">
            请在{" "}
            {remaining >= 60
              ? `${String(Math.floor(remaining / 60))} 分 ${String(remaining % 60).padStart(2, "0")} 秒`
              : `${String(remaining)} 秒`}
            后重试。
          </InlineAlert>
        ) : null}
        <form onSubmit={(event) => void handleSubmit(submit)(event)}>
          <Controller
            control={control}
            name="username"
            render={({ field }) => (
              <TextField
                {...field}
                autoComplete="username"
                controlSize="l"
                errorText={errors.username?.message}
                id="login-username"
                label="用户名"
                placeholder="请输入用户名"
                required
              />
            )}
          />
          <Controller
            control={control}
            name="password"
            render={({ field }) => (
              <TextField
                {...field}
                autoComplete="current-password"
                controlSize="l"
                errorText={errors.password?.message}
                id="login-password"
                label="密码"
                placeholder="请输入密码"
                required
                trailingAction={
                  <PasswordVisibilityButton
                    onToggle={() => {
                      setShowPassword((visible) => !visible);
                    }}
                    visible={showPassword}
                  />
                }
                type={showPassword ? "text" : "password"}
              />
            )}
          />
          <Button
            block
            disabled={remaining > 0}
            loading={isSubmitting}
            loadingText="正在登录"
            size="l"
            type="submit"
            variant="primary"
          >
            {remaining > 0 ? "请稍后再试" : "登录"}
          </Button>
        </form>
        <p className="pw-auth-card__footer">忘记密码请联系管理员重置。</p>
      </section>
    </main>
  );
}

export function AuthBrand() {
  return (
    <div className="pw-auth-brand">
      <span aria-hidden="true" className="pw-brand__mark" />
      <span>
        <strong>{PRODUCT_NAME}</strong>
        <span>企业私有 AI 服务平台</span>
      </span>
    </div>
  );
}

export function PasswordVisibilityButton({
  onToggle,
  visible,
}: {
  onToggle: () => void;
  visible: boolean;
}) {
  const label = visible ? "隐藏密码" : "显示密码";
  return (
    <Button
      aria-label={label}
      aria-pressed={visible}
      className="pw-password-visibility"
      icon={visible ? "visibilityOff" : "visibility"}
      onClick={onToggle}
      size="s"
      title={label}
      variant="ghost"
    >
      <span className="pw-visually-hidden">{label}</span>
    </Button>
  );
}

function intendedRoute(state: unknown) {
  if (
    typeof state === "object" &&
    state !== null &&
    "from" in state &&
    typeof state.from === "string" &&
    state.from.startsWith("/") &&
    !state.from.startsWith("//") &&
    state.from !== "/login"
  ) {
    return state.from;
  }
  return "/";
}

export { intendedRoute };
