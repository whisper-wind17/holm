import { MINIMUM_PASSWORD_CHARACTERS } from "../components/password-policy";
import { zodResolver } from "@hookform/resolvers/zod";
import { Controller, useForm, useWatch } from "react-hook-form";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { useNavigate } from "react-router";
import { z } from "zod";

import { ApiError, type ApiClient } from "../api/client";
import {
  ACCOUNT_NAME_MAX_LENGTH,
  ACCOUNT_NAME_MIN_LENGTH,
  DISPLAY_NAME_MAX_LENGTH,
  parseBootstrapBlocking,
  parseBootstrapUsernameReason,
  type BootstrapCheck,
  type BootstrapCheckId,
  type BootstrapCheckReport,
} from "../api/bootstrap-contracts";
import type {
  AgentObservation,
  GpuDevice,
  UInt64Observation,
} from "../api/system-contracts";
import { toAppError, type AppError } from "../app/errors";
import { useSession } from "../auth/session";
import { AuthBrand, PasswordVisibilityButton } from "../auth/login-page";
import {
  Button,
  Icon,
  InlineAlert,
  MissingValue,
  PasswordPolicyFeedback,
  StatusBadge,
  TextField,
  parsePasswordPolicyViolations,
  type PasswordPolicyViolation,
} from "../components";
import {
  gpuDiscoveryFailureReason,
  gpuHealthPresentation,
} from "../system/presentation";
import { formatBytes, formatObservedAt } from "../system/system-page";
import {
  bootstrapErrorMessage,
  checkLabel,
  hostCheckReasonPresentation,
  outcomePresentation,
  usernameReasonMessage,
} from "./presentation";
import { useBootstrapChecks, useBootstrapGate } from "./queries";

/**
 * first-time initialization: 欢迎 → 系统检测 → 创建管理员.
 *
 * Three things about this page are structural rather than cosmetic:
 *
 * *   **It lives before the session gate.** `ApplicationRoutes` resolves this
 *     path before it can redirect an anonymous visitor to `/login`, and nothing
 *     here reads `session.status`: at this point in a platform's life there is
 *     no account to have a session with.
 * *   **The one-time Token exists only in this component's state.** No browser
 *     storage of any kind holds it, and neither does the URL: a Token that
 *     arrived in the link's fragment is stripped from the address on arrival.
 *     Losing it on reload is the intended behaviour, and the welcome step says
 *     so instead of degrading into a blank "unauthorized" page.
 * *   **Every verdict shown here was computed by the server.** The page maps
 *     identifiers to sentences (`./presentation`) and refuses unknown ones; it
 *     never decides that something passed, and its disabled "下一步" is a
 *     courtesy, not the enforcement point — `/bootstrap/admin` recomputes the
 *     detection and refuses on its own.
 *
 * Two of the six state coverages required by the UI state checklist do not apply
 * to this flow, and are deliberately absent rather than faked:
 *
 * *   **Permission** — the flow is anonymous by construction; there is no role
 *     that can be told "you may not". A bad Token is not a permission state; it
 *     is the Token state handled on the welcome step.
 * *   **Empty** — a detection report always carries exactly seven cards
 *     (`parseBootstrapCheckReport` rejects a partial one) and the administrator
 *     form is a create form, so neither surface has a list that can legitimately
 *     be empty. "Zero GPUs" is a *failure*, not an empty state, and initialization
 *     forbids rendering it as one.
 */
export const BOOTSTRAP_WIZARD_PATH = "/setup";

const OVERVIEW_PATH = "/overview";
const LOGIN_PATH = "/login";
const TOKEN_FRAGMENT_PREFIX = "#token=";

type WizardStep = "administrator" | "checks" | "ready" | "welcome";

const STEP_LABELS: readonly { id: WizardStep; label: string }[] = [
  { id: "welcome", label: "欢迎" },
  { id: "checks", label: "系统检测" },
  { id: "administrator", label: "创建管理员" },
];

const administratorSchema = z
  .object({
    confirmPassword: z.string().min(1, "请再次输入密码"),
    displayName: z
      .string()
      .max(
        DISPLAY_NAME_MAX_LENGTH,
        `显示名称最多 ${String(DISPLAY_NAME_MAX_LENGTH)} 个字符`,
      ),
    password: z
      .string()
      .min(
        MINIMUM_PASSWORD_CHARACTERS,
        `密码至少需要 ${String(MINIMUM_PASSWORD_CHARACTERS)} 个字符`,
      ),
    username: z.string().min(1, "请输入用户名"),
  })
  .refine((values) => values.password === values.confirmPassword, {
    message: "两次输入的密码不一致",
    path: ["confirmPassword"],
  });

type AdministratorFields = z.infer<typeof administratorSchema>;

/**
 * Take the Token out of the URL fragment, and out of the address bar.
 *
 * Installation delivers the one-time link with the Token in the fragment, which
 * browsers never send to a server. It would still sit in history and on screen,
 * so it is removed the moment it has been read: from here on the only copy is
 * React state.
 */
function consumeTokenFromFragment(): string {
  const hash = window.location.hash;
  if (hash.length <= 1) {
    return "";
  }
  const raw = hash.startsWith(TOKEN_FRAGMENT_PREFIX)
    ? hash.slice(TOKEN_FRAGMENT_PREFIX.length)
    : hash.slice(1);
  window.history.replaceState(
    window.history.state,
    "",
    `${window.location.pathname}${window.location.search}`,
  );
  try {
    return decodeURIComponent(raw);
  } catch {
    return raw;
  }
}

export function BootstrapWizardPage({ apiClient }: { apiClient: ApiClient }) {
  const navigate = useNavigate();
  const session = useSession();
  const gate = useBootstrapGate(apiClient);
  const [token, setToken] = useState("");
  const [attempt, setAttempt] = useState(0);
  const [step, setStep] = useState<WizardStep>("welcome");
  const [tokenError, setTokenError] = useState<string>();
  const [closed, setClosed] = useState(false);
  const [serverBlocking, setServerBlocking] = useState<
    readonly BootstrapCheckId[]
  >([]);
  const [unexpectedError, setUnexpectedError] = useState<AppError>();
  const fragmentRead = useRef(false);

  const checks = useBootstrapChecks(
    apiClient,
    token,
    attempt,
    step === "checks" || step === "administrator",
  );

  useEffect(() => {
    if (fragmentRead.current) {
      return;
    }
    fragmentRead.current = true;
    const fromLink = consumeTokenFromFragment();
    if (fromLink.length > 0) {
      setToken(fromLink);
    }
  }, []);

  if (unexpectedError !== undefined) {
    throw unexpectedError;
  }

  const checksError =
    checks.error instanceof ApiError ? checks.error : undefined;

  const closedForGood =
    step !== "ready" &&
    (closed || gate.data === "initialized" || checksError?.status === 404);

  if (closedForGood) {
    return (
      <WizardCanvas>
        <ClosedPanel
          onLogin={() => {
            void navigate(LOGIN_PATH, { replace: true });
          }}
        />
      </WizardCanvas>
    );
  }

  // A Token that stopped being usable mid-flow is answered on the step that can
  // fix it, not with a spinner and not with a generic error page.
  const tokenRejected = checksError?.status === 401;
  const visibleStep: WizardStep = tokenRejected ? "welcome" : step;

  return (
    <WizardCanvas>
      {visibleStep === "ready" ? null : <WizardSteps current={visibleStep} />}
      {visibleStep === "welcome" ? (
        <WelcomeStep
          error={
            tokenRejected
              ? bootstrapErrorMessage("bootstrap_token_invalid")
              : tokenError
          }
          onStart={startChecks}
          token={tokenRejected ? "" : token}
        />
      ) : null}
      {visibleStep === "checks" ? (
        <ChecksStep
          blockedByServer={serverBlocking}
          onContinue={() => {
            setStep("administrator");
          }}
          onRecheck={() => {
            setServerBlocking([]);
            void checks.refetch();
          }}
          query={checks}
        />
      ) : null}
      {visibleStep === "administrator" ? (
        <AdministratorStep
          onBack={backToChecks}
          onSubmit={createAdministrator}
        />
      ) : null}
    </WizardCanvas>
  );

  function backToChecks() {
    setStep("checks");
  }

  function startChecks(nextToken: string) {
    if (nextToken.length === 0) {
      setTokenError("请粘贴安装时生成的初始化口令后再开始检测。");
      return;
    }
    setTokenError(undefined);
    setToken(nextToken);
    setServerBlocking([]);
    setStep("checks");
    // A new attempt is a new query entry, so a previous refusal cannot linger
    // as a cached error over a Token that has since been corrected.
    setAttempt((previous) => previous + 1);
  }

  async function createAdministrator(
    values: AdministratorFields,
    form: AdministratorForm,
  ) {
    try {
      await apiClient.createBootstrapAdministrator(token, {
        confirm_password: values.confirmPassword,
        display_name: values.displayName,
        password: values.password,
        username: values.username,
      });
    } catch (error) {
      handleCreationFailure(error, form);
      return;
    }
    setStep("ready");
    try {
      // /bootstrap/admin already delivered the session cookies, so this only pulls the identity the shell needs. The password is
      // never replayed through /auth/login.
      await session.refresh();
      setToken("");
      void navigate(OVERVIEW_PATH, {
        replace: true,
        state: { initializedUsername: values.username },
      });
    } catch (error) {
      setUnexpectedError(toAppError(error));
    }
  }

  function handleCreationFailure(error: unknown, form: AdministratorForm) {
    if (!(error instanceof ApiError)) {
      setUnexpectedError(toAppError(error));
      return;
    }
    try {
      switch (error.code) {
        case "bootstrap_checks_failed":
          setServerBlocking(parseBootstrapBlocking(error.details?.blocking));
          setStep("checks");
          return;
        case "bootstrap_token_invalid":
          setToken("");
          setTokenError(bootstrapErrorMessage("bootstrap_token_invalid"));
          setStep("welcome");
          return;
        case "invalid_password":
          form.setPolicyViolations(
            parsePasswordPolicyViolations(error.details?.password_policy),
          );
          form.setAlert(bootstrapErrorMessage("invalid_password"));
          return;
        case "invalid_username":
          form.setFieldError(
            "username",
            usernameReasonMessage(
              parseBootstrapUsernameReason(error.details?.reason_code),
            ),
          );
          return;
        case "not_found":
          setClosed(true);
          return;
        case "password_confirmation_mismatch":
          form.setFieldError(
            "confirmPassword",
            bootstrapErrorMessage("password_confirmation_mismatch"),
          );
          return;
        case "username_taken":
          form.setFieldError(
            "username",
            bootstrapErrorMessage("username_taken"),
          );
          return;
        default:
          setUnexpectedError(toAppError(error));
          return;
      }
    } catch (contractError) {
      setUnexpectedError(toAppError(contractError));
    }
  }
}

function WizardCanvas({ children }: { children: ReactNode }) {
  return (
    <main className="pw-setup-canvas" id="main-content">
      <div className="pw-setup-wrap">{children}</div>
    </main>
  );
}

function WizardSteps({ current }: { current: WizardStep }) {
  const currentIndex = STEP_LABELS.findIndex((step) => step.id === current);
  return (
    <ol aria-label="初始化步骤" className="pw-setup-steps">
      {STEP_LABELS.map((step, index) => {
        const state =
          index < currentIndex
            ? "completed"
            : index === currentIndex
              ? "current"
              : "waiting";
        return (
          <li
            aria-current={state === "current" ? "step" : undefined}
            data-state={state}
            key={step.id}
          >
            <span className="pw-setup-steps__index">
              {state === "completed" ? (
                <Icon name="check" size="xs" />
              ) : (
                index + 1
              )}
            </span>
            <span>
              {step.label}
              <span className="pw-visually-hidden">
                {state === "completed"
                  ? "（已完成）"
                  : state === "current"
                    ? "（当前步骤）"
                    : "（未开始）"}
              </span>
            </span>
          </li>
        );
      })}
    </ol>
  );
}

function useFocusedHeading() {
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    heading.current?.focus();
  }, []);
  return heading;
}

function WelcomeStep({
  error,
  onStart,
  token,
}: {
  error: string | undefined;
  onStart: (token: string) => void;
  token: string;
}) {
  const [draft, setDraft] = useState(token);
  const heading = useFocusedHeading();

  useEffect(() => {
    setDraft(token);
  }, [token]);

  return (
    <section className="pw-setup-card pw-setup-card--narrow">
      <AuthBrand />
      <h1 ref={heading} tabIndex={-1}>
        首次初始化
      </h1>
      <p className="pw-setup-card__intro">
        将这台 GPU 服务器配置为团队 AI 服务平台。
      </p>
      {error === undefined ? null : (
        <InlineAlert title="无法开始初始化" tone="negative">
          {error}
        </InlineAlert>
      )}
      <form
        onSubmit={(event) => {
          event.preventDefault();
          onStart(draft.trim());
        }}
      >
        <TextField
          autoComplete="off"
          controlSize="l"
          helpText="见安装终端末尾的链接，或在服务器上执行 sudo cat /etc/holm/secrets/bootstrap-token"
          id="setup-token"
          label="初始化口令"
          onChange={(event) => {
            setDraft(event.target.value);
          }}
          placeholder="粘贴初始化口令"
          required
          type="password"
          value={draft}
        />
        <Button block size="l" type="submit" variant="primary">
          开始检测
        </Button>
      </form>
    </section>
  );
}

type ChecksQuery = ReturnType<typeof useBootstrapChecks>;

function ChecksStep({
  blockedByServer,
  onContinue,
  onRecheck,
  query,
}: {
  blockedByServer: readonly BootstrapCheckId[];
  onContinue: () => void;
  onRecheck: () => void;
  query: ChecksQuery;
}) {
  if (query.data === undefined && query.isError) {
    return <ChecksFailure onRecheck={onRecheck} />;
  }
  if (query.data === undefined) {
    return <ChecksSkeleton />;
  }
  return (
    <ChecksReport
      blockedByServer={blockedByServer}
      onContinue={onContinue}
      onRecheck={onRecheck}
      refreshing={query.isFetching}
      report={query.data}
      stale={query.isStale}
    />
  );
}

function ChecksFailure({ onRecheck }: { onRecheck: () => void }) {
  const heading = useFocusedHeading();
  return (
    <section className="pw-setup-card">
      <h2 ref={heading} tabIndex={-1}>
        系统检测
      </h2>
      <InlineAlert title="无法完成系统检测" tone="negative">
        <span>
          没有拿到这台服务器的检测结果，初始化无法继续。请确认平台服务正在运行后重新检测。
        </span>
        <Button onClick={onRecheck} size="s">
          重新检测
        </Button>
      </InlineAlert>
    </section>
  );
}

/**
 * "检测中使用骨架屏，不先显示 0 张 GPU，避免误导".
 *
 * The GPU placeholders are deliberately meaningless in number — how many cards
 * this machine has is itself one of the facts being measured — and the page says
 * so in words, so neither the skeleton nor its length can be read as a result.
 */
function ChecksSkeleton() {
  const heading = useFocusedHeading();
  return (
    <section aria-busy="true" className="pw-setup-card">
      <h2 ref={heading} tabIndex={-1}>
        正在检测这台服务器
      </h2>
      <p className="pw-setup-card__intro">
        检查 GPU、驱动、磁盘和运行环境。通常需要 10～30 秒。
      </p>
      <div className="pw-setup-skeletons">
        {Array.from({ length: 4 }, (_, slot) => (
          <span
            className="sk pw-setup-gpu-skeleton"
            key={`gpu-slot-${String(slot)}`}
          />
        ))}
      </div>
      <p className="pw-setup-hint">正在枚举设备，尚未确定数量。</p>
    </section>
  );
}

function ChecksReport({
  blockedByServer,
  onContinue,
  onRecheck,
  refreshing,
  report,
  stale,
}: {
  blockedByServer: readonly BootstrapCheckId[];
  onContinue: () => void;
  onRecheck: () => void;
  refreshing: boolean;
  report: BootstrapCheckReport;
  stale: boolean;
}) {
  const heading = useFocusedHeading();
  const blocking = [...new Set([...report.blocking, ...blockedByServer])];
  const warnings = report.checks.filter(
    (check) => check.outcome === "warning" && !blocking.includes(check.id),
  );
  const blocked = blocking.length > 0;

  return (
    <section className="pw-setup-card">
      <div className="pw-setup-card__head">
        <span>
          <h2 ref={heading} tabIndex={-1}>
            {blocked
              ? `${String(blocking.length)} 项未通过`
              : warnings.length > 0
                ? `${String(warnings.length)} 项有警告`
                : "全部通过"}
          </h2>
          <p className="pw-setup-observed">
            <span>检测时间</span>
            <time dateTime={report.observed_at}>
              {formatObservedAt(report.observed_at)}
            </time>
            {refreshing ? <span role="status">正在重新检测</span> : null}
            {stale && !refreshing ? (
              <StatusBadge
                icon="notice"
                text="检测结果可能已过期"
                tone="idle"
              />
            ) : null}
          </p>
        </span>
        <Button
          icon="loading"
          loading={refreshing}
          loadingText="正在检测"
          onClick={onRecheck}
        >
          重新检测
        </Button>
      </div>
      {blockedByServer.length > 0 ? (
        <InlineAlert title="初始化未完成" tone="negative">
          {bootstrapErrorMessage("bootstrap_checks_failed")}
          服务器在创建管理员时重新检测，仍有失败项：
          {blockedByServer.map((id) => checkLabel(id)).join("、")}。
        </InlineAlert>
      ) : null}
      <div className="pw-setup-checks">
        {report.checks.map((check) => (
          <CheckCard
            report={report}
            check={check}
            forcedFailure={blockedByServer.includes(check.id)}
            key={check.id}
            observedAt={report.observed_at}
          />
        ))}
      </div>
      <div className="pw-setup-foot">
        <Button
          disabled={blocked}
          onClick={onContinue}
          size="l"
          variant="primary"
        >
          {warnings.length > 0 && !blocked ? "了解影响，继续" : "下一步"}
        </Button>
      </div>
    </section>
  );
}

function CheckCard({
  report,
  check,
  forcedFailure,
  observedAt,
}: {
  report: BootstrapCheckReport;
  check: BootstrapCheck;
  forcedFailure: boolean;
  observedAt: string;
}) {
  const outcome = forcedFailure ? "failed" : check.outcome;
  const status = outcomePresentation(outcome);
  return (
    <section
      aria-label={`${checkLabel(check.id)} ${status.label}`}
      className="pw-setup-check"
      data-outcome={outcome}
    >
      <span className="pw-setup-check__head">
        <strong>{checkLabel(check.id)}</strong>
        <StatusBadge
          icon={status.icon}
          text={status.label}
          tone={status.tone}
        />
      </span>
      {check.id === "gpu" || check.id === "disk" || check.id === "memory" ? (
        <span className="pw-setup-check__value">
          <CheckValue
            agent={report.agent}
            check={check}
            observedAt={observedAt}
          />
        </span>
      ) : null}
      {check.reasons
        .filter(
          (reason) =>
            ![
              "gpu_devices_present",
              "disk_sufficient",
              "memory_sufficient",
            ].includes(reason),
        )
        .map((reason) => (
          <CheckReason key={reason} reason={reason} report={report} />
        ))}
      {forcedFailure ? (
        <span className="pw-setup-check__advice">
          服务器在创建管理员时重新检测，这一项仍未通过。请按上面的提示处理后重新检测。
        </span>
      ) : null}
    </section>
  );
}

function CheckReason({
  reason,
  report,
}: {
  reason: string;
  report: BootstrapCheckReport;
}) {
  const presentation = hostCheckReasonPresentation(reason, report);
  return (
    <span className="pw-setup-check__reason">
      <span>{presentation.detail}</span>
      {presentation.advice === null ? null : (
        <span className="pw-setup-check__advice">{presentation.advice}</span>
      )}
    </span>
  );
}

function CheckValue({
  agent,
  check,
  observedAt,
}: {
  agent: AgentObservation;
  check: BootstrapCheck;
  observedAt: string;
}) {
  if (agent.status === "unavailable") {
    return (
      <MissingValue
        kind="unavailable"
        lastUpdated={formatObservedAt(observedAt)}
        reason="服务器检测服务暂不可用"
      />
    );
  }
  switch (check.id) {
    case "gpu":
      return <GpuValue gpus={agent.gpus} check={check} />;
    case "disk":
      return (
        <DiskValue
          available={agent.host.available_disk_bytes}
          observedAt={observedAt}
        />
      );
    case "memory":
      return (
        <BytesValue
          observation={agent.host.total_memory_bytes}
          observedAt={observedAt}
        />
      );
    default:
      // Version cards render their evaluated host facts in CheckReason.
      return null;
  }
}

/**
 * "读不到" is not "读到了零".
 *
 * `unavailable` means discovery could not run at all, so the device count is
 * unknown; an empty device list means discovery finished and found none. Both
 * are failures, and they get different sentences because a reader has to act
 * differently on each.
 */
function GpuValue({
  gpus,
  check,
}: {
  check: BootstrapCheck;
  gpus: Extract<AgentObservation, { status: "available" }>["gpus"];
}) {
  if (gpus.discovery_state === "unavailable") {
    return (
      <span>
        GPU 信息暂不可用：{gpuDiscoveryFailureReason(gpus.failure_reason)}
        ，当前无法判断 GPU 数量与 Health。
      </span>
    );
  }
  if (gpus.devices.length === 0) {
    return <span>没有检测到 GPU 设备</span>;
  }
  return (
    <>
      <span>
        检测到 {gpus.devices.length} 张 GPU
        {check.reasons.includes("gpu_devices_present")
          ? "，Health 全部正常"
          : ""}
      </span>
      <ul className="pw-setup-gpus">
        {gpus.devices.map((device) => (
          <GpuTile device={device} key={device.uuid} />
        ))}
      </ul>
    </>
  );
}

function GpuTile({ device }: { device: GpuDevice }) {
  const health = gpuHealthPresentation(device.health);
  return (
    <li className="pw-setup-gpu">
      <span className="pw-setup-gpu__index">
        GPU {device.display_index ?? "编号未知"}
      </span>
      <span className="pw-setup-gpu__model">{device.model}</span>
      <span className="pw-setup-gpu__memory">
        {formatBytes(device.memory_bytes)}
      </span>
      <StatusBadge icon={health.icon} text={health.label} tone={health.tone} />
    </li>
  );
}

function BytesValue({
  observation,
  observedAt,
}: {
  observation: UInt64Observation;
  observedAt: string;
}) {
  if (observation.state === "known") {
    return (
      <span className="pw-setup-technical">
        {formatBytes(observation.value)}
      </span>
    );
  }
  return (
    <MissingValue
      kind="unavailable"
      lastUpdated={formatObservedAt(observedAt)}
      reason="监控数据暂不可用"
    />
  );
}

function DiskValue({
  available,
  observedAt,
}: {
  available: UInt64Observation;
  observedAt: string;
}) {
  if (available.state === "known") {
    return (
      <span className="pw-setup-technical">
        模型目录可用 {formatBytes(available.value)}
      </span>
    );
  }
  return (
    <MissingValue
      kind="unavailable"
      lastUpdated={formatObservedAt(observedAt)}
      reason="监控数据暂不可用"
    />
  );
}

interface AdministratorForm {
  setAlert: (message: string) => void;
  setFieldError: (
    field: "confirmPassword" | "username",
    message: string,
  ) => void;
  setPolicyViolations: (violations: PasswordPolicyViolation[]) => void;
}

function AdministratorStep({
  onBack,
  onSubmit,
}: {
  onBack: () => void;
  onSubmit: (
    values: AdministratorFields,
    form: AdministratorForm,
  ) => Promise<void>;
}) {
  const heading = useFocusedHeading();
  const [visibleField, setVisibleField] = useState<string>();
  const [alert, setAlert] = useState<string>();
  const [policyViolations, setPolicyViolations] = useState<
    PasswordPolicyViolation[]
  >([]);
  const {
    control,
    formState: { errors, isSubmitting },
    handleSubmit,
    setError,
  } = useForm<AdministratorFields>({
    defaultValues: {
      confirmPassword: "",
      displayName: "",
      password: "",
      username: "",
    },
    resolver: zodResolver(administratorSchema),
  });
  const password = useWatch({ control, name: "password" });

  useEffect(() => {
    setPolicyViolations([]);
    setAlert(undefined);
  }, [password]);

  return (
    <section className="pw-setup-card pw-setup-card--narrow">
      <h2 ref={heading} tabIndex={-1}>
        创建第一个管理员
      </h2>
      <p className="pw-setup-card__intro">
        这个账号可以部署模型、发布服务和管理其他用户。初始化完成后，这个流程会永久关闭。
      </p>
      {alert === undefined ? null : (
        <InlineAlert title="管理员未创建" tone="negative">
          {alert}
        </InlineAlert>
      )}
      <form
        onSubmit={(event) =>
          void handleSubmit((values) =>
            onSubmit(values, {
              setAlert,
              setFieldError: (field, message) => {
                setError(field, { message });
              },
              setPolicyViolations,
            }),
          )(event)
        }
      >
        <Controller
          control={control}
          name="username"
          render={({ field }) => (
            <TextField
              {...field}
              autoComplete="username"
              controlSize="l"
              errorText={errors.username?.message}
              helpText={`${String(ACCOUNT_NAME_MIN_LENGTH)}～${String(ACCOUNT_NAME_MAX_LENGTH)} 位，可用字母、数字、点、下划线和短横线，系统内唯一。用于登录。`}
              id="setup-username"
              label="用户名"
              placeholder="例如 chen.pei"
              required
            />
          )}
        />
        <Controller
          control={control}
          name="displayName"
          render={({ field }) => (
            <TextField
              {...field}
              autoComplete="name"
              controlSize="l"
              errorText={errors.displayName?.message}
              helpText={`0～${String(DISPLAY_NAME_MAX_LENGTH)} 个字符，留空则界面显示用户名。`}
              id="setup-display-name"
              label="显示名称"
            />
          )}
        />
        <Controller
          control={control}
          name="password"
          render={({ field }) => (
            <TextField
              {...field}
              autoComplete="new-password"
              controlSize="l"
              errorText={errors.password?.message}
              helpText={`至少 ${String(MINIMUM_PASSWORD_CHARACTERS)} 个字符；允许空格与 Unicode 字符。`}
              id="setup-password"
              label="密码"
              required
              trailingAction={
                <PasswordVisibilityButton
                  onToggle={() => {
                    toggleVisible("password");
                  }}
                  visible={visibleField === "password"}
                />
              }
              type={visibleField === "password" ? "text" : "password"}
            />
          )}
        />
        <PasswordPolicyFeedback
          password={password}
          violations={policyViolations}
        />
        <Controller
          control={control}
          name="confirmPassword"
          render={({ field }) => (
            <TextField
              {...field}
              autoComplete="new-password"
              controlSize="l"
              errorText={errors.confirmPassword?.message}
              id="setup-confirm-password"
              label="确认密码"
              required
              trailingAction={
                <PasswordVisibilityButton
                  onToggle={() => {
                    toggleVisible("confirmPassword");
                  }}
                  visible={visibleField === "confirmPassword"}
                />
              }
              type={visibleField === "confirmPassword" ? "text" : "password"}
            />
          )}
        />
        <div className="pw-setup-foot">
          <Button onClick={onBack} type="button">
            返回检测结果
          </Button>
          <Button
            loading={isSubmitting}
            loadingText="正在创建"
            size="l"
            type="submit"
            variant="primary"
          >
            创建并完成初始化
          </Button>
        </div>
      </form>
    </section>
  );

  function toggleVisible(name: string) {
    setVisibleField((current) => (current === name ? undefined : name));
  }
}

/**
 * The ordinary answer for every machine that has already been set up.
 *
 * `/bootstrap/status` answering 404 is a fact, not a failure, so this is a plain
 * explanation with the way forward — never an error page, never a retry loop.
 */
function ClosedPanel({ onLogin }: { onLogin: () => void }) {
  const heading = useFocusedHeading();
  return (
    <section className="pw-setup-card pw-setup-card--narrow">
      <AuthBrand />
      <h1 ref={heading} tabIndex={-1}>
        已完成初始化
      </h1>
      <p className="pw-setup-card__intro">
        这台服务器已经创建过管理员，初始化流程已永久关闭，不能再次创建管理员。请使用平台账号登录。
      </p>
      <Button block onClick={onLogin} size="l" variant="primary">
        前往登录
      </Button>
    </section>
  );
}
