import { MINIMUM_PASSWORD_CHARACTERS } from "../components/password-policy";
import { PRODUCT_NAME } from "../app/brand";
import { zodResolver } from "@hookform/resolvers/zod";
import { Controller, useForm, useWatch } from "react-hook-form";
import { useEffect, useState } from "react";
import { useLocation, useNavigate } from "react-router";
import { z } from "zod";

import { ApiError, type ApiClient } from "../api/client";
import {
  Button,
  InlineAlert,
  PasswordPolicyFeedback,
  TextField,
  parsePasswordPolicyViolations,
  type PasswordPolicyViolation,
} from "../components";
import { buildNavigationRoutes } from "../app/navigation";
import { toAppError, type AppError } from "../app/errors";
import { intendedRoute, PasswordVisibilityButton } from "./login-page";
import { AppShell } from "../app/shell";
import { useSession } from "./session";

const passwordSchema = z
  .object({
    confirmPassword: z.string().min(1, "请再次输入新密码"),
    currentPassword: z.string(),
    newPassword: z
      .string()
      .min(
        MINIMUM_PASSWORD_CHARACTERS,
        `新密码至少需要 ${String(MINIMUM_PASSWORD_CHARACTERS)} 个字符`,
      )
      .max(128, "新密码最多 128 个字符"),
  })
  .refine((values) => values.newPassword === values.confirmPassword, {
    message: "两次输入的新密码不一致",
    path: ["confirmPassword"],
  });

type PasswordFields = z.infer<typeof passwordSchema>;

export function PasswordPage({ apiClient }: { apiClient: ApiClient }) {
  const location = useLocation();
  const navigate = useNavigate();
  const session = useSession();
  const me = session.me;
  const [saved, setSaved] = useState(false);
  const [visibleField, setVisibleField] = useState<string>();
  const [serverError, setServerError] = useState<string>();
  const [policyViolations, setPolicyViolations] = useState<
    PasswordPolicyViolation[]
  >([]);
  const [unexpectedError, setUnexpectedError] = useState<AppError>();
  const {
    control,
    formState: { errors, isSubmitting },
    handleSubmit,
    setError,
    reset,
  } = useForm<PasswordFields>({
    defaultValues: {
      confirmPassword: "",
      currentPassword: "",
      newPassword: "",
    },
    resolver: zodResolver(passwordSchema),
  });
  const newPassword = useWatch({ control, name: "newPassword" });

  useEffect(() => {
    setPolicyViolations([]);
    setServerError(undefined);
  }, [newPassword]);

  if (me === undefined) {
    throw new Error("password route requires an authenticated session");
  }
  if (unexpectedError !== undefined) {
    throw unexpectedError;
  }

  const temporaryPassword = me.must_change_password;

  async function submit(values: PasswordFields) {
    setServerError(undefined);
    setPolicyViolations([]);
    if (!temporaryPassword && values.currentPassword.length === 0) {
      setError("currentPassword", { message: "请输入当前密码" });
      return;
    }
    try {
      await apiClient.changePassword({
        confirm_password: values.confirmPassword,
        current_password: temporaryPassword ? null : values.currentPassword,
        new_password: values.newPassword,
      });
      const refreshed = await session.refresh();
      if (!temporaryPassword) {
        reset();
        setSaved(true);
        return;
      }
      const firstRoute = buildNavigationRoutes(refreshed.navigation)[0];
      const requested = intendedRoute(location.state);
      void navigate(
        requested === "/" || requested === "/change-password"
          ? (firstRoute?.path ?? "/")
          : requested,
        { replace: true },
      );
    } catch (error) {
      if (
        error instanceof ApiError &&
        error.code === "current_password_incorrect"
      ) {
        setError("currentPassword", { message: "当前密码不正确" });
        return;
      }
      if (error instanceof ApiError && error.code === "invalid_new_password") {
        try {
          setPolicyViolations(
            parsePasswordPolicyViolations(error.details?.password_policy),
          );
        } catch (contractError) {
          setUnexpectedError(toAppError(contractError));
          return;
        }
        setServerError("新密码不符合安全要求，请更换后重试。");
        return;
      }
      setUnexpectedError(toAppError(error));
    }
  }

  const content = (
    <section
      aria-labelledby="password-title"
      className={temporaryPassword ? "pw-auth-card" : "pw-account-page"}
    >
      <AuthPasswordHeading temporary={temporaryPassword} />
      {serverError === undefined ? null : (
        <InlineAlert title="密码未修改" tone="negative">
          {serverError}
        </InlineAlert>
      )}
      {saved ? (
        <InlineAlert title="密码已修改。" tone="positive">
          其他设备上的登录已退出，当前设备保持登录；API
          密钥不受影响，程序调用照常进行。
        </InlineAlert>
      ) : (
        <form
          className={temporaryPassword ? undefined : "pw-account-password"}
          onSubmit={(event) => void handleSubmit(submit)(event)}
        >
          {temporaryPassword ? null : (
            <PasswordController
              autoComplete="current-password"
              control={control}
              errorText={errors.currentPassword?.message}
              label="当前密码"
              name="currentPassword"
              onToggle={() => {
                toggleVisible("currentPassword");
              }}
              visible={visibleField === "currentPassword"}
            />
          )}
          <PasswordController
            autoComplete="new-password"
            control={control}
            errorText={errors.newPassword?.message}
            helpText={
              temporaryPassword
                ? `至少 ${String(MINIMUM_PASSWORD_CHARACTERS)} 个字符；允许空格与 Unicode 字符。`
                : undefined
            }
            label="新密码"
            name="newPassword"
            onToggle={() => {
              toggleVisible("newPassword");
            }}
            visible={visibleField === "newPassword"}
          />
          <PasswordPolicyFeedback
            password={newPassword}
            violations={policyViolations}
          />
          <PasswordController
            autoComplete="new-password"
            control={control}
            errorText={errors.confirmPassword?.message}
            label="确认新密码"
            name="confirmPassword"
            onToggle={() => {
              toggleVisible("confirmPassword");
            }}
            visible={visibleField === "confirmPassword"}
          />
          <InlineAlert
            title="修改后，其他设备需要重新登录。API 密钥不会被撤销。"
            tone="info"
          >
            {null}
          </InlineAlert>
          <Button
            block={temporaryPassword}
            loading={isSubmitting}
            loadingText="正在保存"
            size="l"
            type="submit"
            variant="primary"
          >
            {temporaryPassword ? "保存并进入平台" : "修改密码"}
          </Button>
        </form>
      )}
    </section>
  );
  return temporaryPassword ? (
    <main className="pw-auth-canvas" id="main-content">
      {content}
    </main>
  ) : (
    <AppShell
      apiClient={apiClient}
      me={me}
      routes={buildNavigationRoutes(me.navigation)}
    >
      {content}
    </AppShell>
  );

  function toggleVisible(name: string) {
    setVisibleField((current) => (current === name ? undefined : name));
  }
}

function AuthPasswordHeading({ temporary }: { temporary: boolean }) {
  if (!temporary) return <h1 id="password-title">修改密码</h1>;
  return (
    <>
      <div className="pw-auth-brand">
        <span aria-hidden="true" className="pw-brand__mark" />
        <span>
          <strong>{PRODUCT_NAME}</strong>
          <span>完成后才能进入平台</span>
        </span>
      </div>
      <h1 id="password-title">设置新密码</h1>
      <p className="pw-auth-card__intro">当前使用的是管理员发放的临时密码。</p>
    </>
  );
}

interface PasswordControllerProps {
  autoComplete: string;
  control: ReturnType<typeof useForm<PasswordFields>>["control"];
  errorText: string | undefined;
  helpText?: string;
  label: string;
  name: "confirmPassword" | "currentPassword" | "newPassword";
  onToggle: () => void;
  visible: boolean;
}

function PasswordController({
  autoComplete,
  control,
  errorText,
  helpText,
  label,
  name,
  onToggle,
  visible,
}: PasswordControllerProps) {
  return (
    <Controller
      control={control}
      name={name}
      render={({ field }) => (
        <TextField
          {...field}
          autoComplete={autoComplete}
          controlSize="l"
          errorText={errorText}
          helpText={helpText}
          id={`password-${name}`}
          label={label}
          required
          trailingAction={
            <PasswordVisibilityButton onToggle={onToggle} visible={visible} />
          }
          type={visible ? "text" : "password"}
        />
      )}
    />
  );
}
