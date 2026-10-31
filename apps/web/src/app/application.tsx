import { PersonalAiServiceDetailPage } from "../ai-services/personal-service-detail-page";
import { AiServiceDetailPage } from "../ai-services/service-detail-page";
import { PRODUCT_NAME } from "./brand";
import { useUser } from "../users/queries";
import { useEffect, useRef } from "react";
import { Navigate, Route, Routes, useLocation } from "react-router";

import { browserApiClient, type ApiClient } from "../api/client";
import { LoginPage } from "../auth/login-page";
import { intendedRoute } from "../auth/login-page";
import { ProfilePage } from "../auth/profile-page";
import { PasswordPage } from "../auth/password-page";
import { useSession } from "../auth/session";
import { AppProviders } from "./providers";
import {
  buildNavigationRoutes,
  RoutePlaceholder,
  type NavigationRoute,
} from "./navigation";
import { AppShell } from "./shell";
import { SystemPage } from "../system/system-page";
import { LocalModelDetailPage } from "../local-models/artifact-detail-page";
import { ModelsPage } from "../local-models/model-page";
import {
  MODEL_DEPLOY_PATH,
  ModelDeployWizard,
} from "../model-instances/deploy-wizard";
import { ModelInstanceDetailPage } from "../model-instances/instance-detail";
import { AiServicesPage } from "../ai-services/service-pages";
import { ApiKeyPage } from "../api-keys/api-key-page";
import { OverviewPage } from "../telemetry/overview-page";
import { UserDetailPage, UsersPage } from "../users/user-pages";
import { UsagePage } from "../usage/usage-page";
import {
  MODEL_DOWNLOAD_PATH,
  ModelDownloadPage,
} from "../model-selection/download-page";
import { useBootstrapGate } from "../setup/queries";
import {
  BOOTSTRAP_WIZARD_PATH,
  BootstrapWizardPage,
} from "../setup/wizard-page";

export function HolmApplication({
  apiClient = browserApiClient,
}: {
  apiClient?: ApiClient;
}) {
  return (
    <AppProviders apiClient={apiClient}>
      <ApplicationRoutes apiClient={apiClient} />
    </AppProviders>
  );
}

export function ApplicationRoutes({ apiClient }: { apiClient: ApiClient }) {
  const location = useLocation();
  const session = useSession();
  // Only an anonymous visitor can be told which of the two first-run worlds
  // this machine is in; an authenticated one is proof enough that it is
  // initialized, so the probe stays off for them.
  const bootstrapGate = useBootstrapGate(
    apiClient,
    session.status === "anonymous",
  );

  useEffect(() => {
    document.documentElement.lang = "zh-CN";
    document.title = PRODUCT_NAME;
  }, []);

  // Initialization runs before any account exists, so the wizard is resolved ahead of
  // the session gate below and reads no session state of its own.
  if (location.pathname === BOOTSTRAP_WIZARD_PATH) {
    return <BootstrapWizardPage apiClient={apiClient} />;
  }

  if (session.status === "loading") {
    return (
      <main
        aria-busy="true"
        aria-label="正在确认登录状态"
        className="pw-session-loading"
      >
        <span className="pw-session-loading__mark" />
      </main>
    );
  }

  if (session.status === "anonymous" || session.me === undefined) {
    if (bootstrapGate.isPending) {
      return (
        <main
          aria-busy="true"
          aria-label="正在确认平台初始化状态"
          className="pw-session-loading"
        >
          <span className="pw-session-loading__mark" />
        </main>
      );
    }
    if (bootstrapGate.data === "uninitialized") {
      // There is no account to log in with yet, so every anonymous path leads
      // to the initialization wizard instead of the login form.
      return (
        <Routes>
          <Route
            element={<Navigate replace to={BOOTSTRAP_WIZARD_PATH} />}
            path="*"
          />
        </Routes>
      );
    }
    return (
      <Routes>
        <Route element={<LoginPage apiClient={apiClient} />} path="/login" />
        <Route
          element={
            <Navigate
              replace
              state={{
                from: `${location.pathname}${location.search}${location.hash}`,
              }}
              to="/login"
            />
          }
          path="*"
        />
      </Routes>
    );
  }

  const me = session.me;
  const routes = buildNavigationRoutes(me.navigation);
  const modelsRoute = routes.find(({ id }) => id === "models");
  const servicesRoute = routes.find(({ id }) => id === "ai_services");
  const usersRoute = routes.find(({ id }) => id === "users");
  const homePath = routes[0]?.path;
  if (homePath === undefined) {
    throw new Error("authenticated session has no route");
  }

  if (me.must_change_password) {
    return (
      <Routes>
        <Route
          element={<PasswordPage apiClient={apiClient} />}
          path="/change-password"
        />
        <Route
          element={
            <Navigate
              replace
              state={{
                from: `${location.pathname}${location.search}${location.hash}`,
              }}
              to="/change-password"
            />
          }
          path="*"
        />
      </Routes>
    );
  }

  return (
    <Routes>
      <Route element={<Navigate replace to={homePath} />} path="/" />
      <Route
        element={
          <Navigate
            replace
            to={
              intendedRoute(location.state) === "/"
                ? homePath
                : intendedRoute(location.state)
            }
          />
        }
        path="/login"
      />
      <Route
        element={<PasswordPage apiClient={apiClient} />}
        path="/change-password"
      />
      <Route
        path="/profile"
        element={
          <AppShell apiClient={apiClient} me={me} routes={routes}>
            <ProfilePage apiClient={apiClient} />
          </AppShell>
        }
      />
      {modelsRoute === undefined ? null : (
        <>
          <Route
            element={
              <AppShell apiClient={apiClient} me={me} routes={routes}>
                <ModelDownloadPage apiClient={apiClient} />
              </AppShell>
            }
            path={MODEL_DOWNLOAD_PATH}
          />
          <Route
            element={
              <AppShell apiClient={apiClient} me={me} routes={routes}>
                <ModelDeployWizard apiClient={apiClient} />
              </AppShell>
            }
            path={MODEL_DEPLOY_PATH}
          />
          <Route
            element={
              <AppShell apiClient={apiClient} me={me} routes={routes}>
                <ModelInstanceDetailPage apiClient={apiClient} />
              </AppShell>
            }
            path="/models/instances/:instanceId"
          />
          <Route
            element={
              <AppShell apiClient={apiClient} me={me} routes={routes}>
                <LocalModelDetailPage apiClient={apiClient} />
              </AppShell>
            }
            path="/models/local/:artifactId"
          />
        </>
      )}
      {servicesRoute === undefined || me.role !== "admin" ? null : (
        <Route
          element={
            <AppShell apiClient={apiClient} me={me} routes={routes}>
              <AiServiceDetailPage apiClient={apiClient} />
            </AppShell>
          }
          path="/services/:serviceId"
        />
      )}
      {servicesRoute === undefined || me.role !== "user" ? null : (
        <Route
          element={
            <AppShell apiClient={apiClient} me={me} routes={routes}>
              <PersonalAiServiceDetailPage apiClient={apiClient} />
            </AppShell>
          }
          path="/services/:serviceId"
        />
      )}
      {usersRoute === undefined ? null : (
        <Route
          element={
            <AppShell apiClient={apiClient} me={me} routes={routes}>
              <UserDetailPage apiClient={apiClient} />
            </AppShell>
          }
          path="/users/:userId"
        />
      )}
      {me.role === "admin" ? (
        <Route
          path="/api-keys"
          element={
            <AppShell apiClient={apiClient} me={me} routes={routes}>
              <AdminPersonalApiKeys apiClient={apiClient} userId={me.user.id} />
            </AppShell>
          }
        />
      ) : null}
      {routes.map((route) => (
        <Route
          element={
            <ShellRoute apiClient={apiClient} route={route} routes={routes} />
          }
          key={route.id}
          path={route.path}
        />
      ))}
      <Route
        element={
          <AppShell apiClient={apiClient} me={me} routes={routes}>
            <PermissionPage />
          </AppShell>
        }
        path="*"
      />
    </Routes>
  );
}

function AdminPersonalApiKeys({
  apiClient,
  userId,
}: {
  apiClient: ApiClient;
  userId: string;
}) {
  const grants = useUser(apiClient, userId);
  if (grants.isPending) {
    return (
      <div role="status" aria-busy="true">
        正在加载服务授权
      </div>
    );
  }
  if (grants.isError || grants.data.authorized_service_count === 0) {
    return <PermissionPage />;
  }
  return <ApiKeyPage apiClient={apiClient} />;
}

function ShellRoute({
  apiClient,
  route,
  routes,
}: {
  apiClient: ApiClient;
  route: NavigationRoute;
  routes: readonly NavigationRoute[];
}) {
  const me = useSession().me;
  if (me === undefined) {
    throw new Error("shell route requires an authenticated session");
  }
  return (
    <AppShell apiClient={apiClient} me={me} routes={routes}>
      {route.id === "system" ? (
        <SystemPage apiClient={apiClient} />
      ) : route.id === "overview" ? (
        <OverviewPage apiClient={apiClient} />
      ) : route.id === "models" ? (
        <ModelsPage apiClient={apiClient} />
      ) : route.id === "ai_services" ? (
        <AiServicesPage apiClient={apiClient} />
      ) : route.id === "api_keys" ? (
        <ApiKeyPage apiClient={apiClient} />
      ) : route.id === "users" ? (
        <UsersPage apiClient={apiClient} />
      ) : route.id === "usage" ? (
        <UsagePage apiClient={apiClient} />
      ) : (
        <RoutePlaceholder route={route} />
      )}
    </AppShell>
  );
}

function PermissionPage() {
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    heading.current?.focus();
  }, []);
  return (
    <section className="pw-permission-page" role="alert">
      <h1 ref={heading} tabIndex={-1}>
        无权限
      </h1>
      <p>当前账号不能访问此页面。请从主导航选择可用功能。</p>
    </section>
  );
}
