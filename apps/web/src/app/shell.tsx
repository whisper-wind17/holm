import { PRODUCT_NAME } from "./brand";
import { useUser } from "../users/queries";
import { HelpButton, HelpProvider } from "../help/help-center";
import * as Dialog from "@radix-ui/react-dialog";
import { useEffect, useState, type ReactNode } from "react";
import { NavLink, useLocation, useNavigate } from "react-router";

import type { ApiClient } from "../api/client";
import type { MeResponse } from "../api/contracts";
import { Button, Icon } from "../components";
import { useSession } from "../auth/session";
import { toAppError, type AppError } from "./errors";
import { NavigationGuardProvider } from "./navigation-guard";
import type { NavigationRoute } from "./navigation";
import {
  buildPlatformStatus,
  unavailablePlatformStatus,
} from "../system/presentation";
import { useSystemQueries } from "../system/queries";

interface AppShellProps {
  apiClient: ApiClient;
  children: ReactNode;
  me: MeResponse;
  routes: readonly NavigationRoute[];
}

export function AppShell({ apiClient, children, me, routes }: AppShellProps) {
  const location = useLocation();
  const [drawerOpen, setDrawerOpen] = useState(false);
  const currentRoute = routes.find(
    (route) =>
      route.path === location.pathname ||
      (route.path !== "/" && location.pathname.startsWith(`${route.path}/`)),
  );
  const canViewSystem = me.capabilities.some(
    (item) => item.capability === "view_system",
  );

  // The navigation guard can finish a navigation after the drawer link's own
  // click handler was skipped, so close the drawer on every route change.
  useEffect(() => {
    setDrawerOpen(false);
  }, [location.pathname]);

  return (
    <HelpProvider admin={me.role === "admin"}>
      <NavigationGuardProvider>
        <div className="pw-app-shell">
          <a className="pw-skip-link" href="#main-content">
            跳到主内容
          </a>
          <aside className="pw-sidebar">
            <Brand />
            <NavigationList routes={routes} />
            {canViewSystem ? <PlatformStatus apiClient={apiClient} /> : null}
          </aside>
          <div className="pw-shell-body">
            <header className="pw-topbar">
              <NavigationDrawer
                apiClient={apiClient}
                canViewSystem={canViewSystem}
                onOpenChange={setDrawerOpen}
                open={drawerOpen}
                routes={routes}
              />
              <span className="pw-topbar__title">
                {currentRoute?.label ?? PRODUCT_NAME}
              </span>
              <HelpButton />
              <UserMenu apiClient={apiClient} me={me} />
            </header>
            <main className="pw-main" id="main-content" tabIndex={-1}>
              <div className="pw-main__inner">{children}</div>
            </main>
          </div>
        </div>
      </NavigationGuardProvider>
    </HelpProvider>
  );
}

function Brand() {
  return (
    <div className="pw-brand">
      <span aria-hidden="true" className="pw-brand__mark" />
      <span className="pw-brand__name">{PRODUCT_NAME}</span>
    </div>
  );
}

function NavigationList({
  onNavigate,
  routes,
}: {
  onNavigate?: () => void;
  routes: readonly NavigationRoute[];
}) {
  return (
    <nav aria-label="主导航" className="pw-navigation">
      {routes.map((route) => (
        <NavLink
          aria-label={route.label}
          className={({ isActive }) =>
            ["pw-navigation__item", isActive ? "is-active" : undefined]
              .filter(Boolean)
              .join(" ")
          }
          end
          key={route.id}
          onClick={onNavigate}
          title={route.label}
          to={route.path}
        >
          <Icon name={route.icon} />
          <span className="pw-navigation__label">{route.label}</span>
        </NavLink>
      ))}
    </nav>
  );
}

function PlatformStatus({ apiClient }: { apiClient: ApiClient }) {
  const queries = useSystemQueries(apiClient);
  const failed = queries.server.isError || queries.components.isError;
  const loading = queries.server.isPending || queries.components.isPending;

  if (!failed && loading) {
    return (
      <NavLink
        aria-label="平台状态正在加载"
        className="pw-platform-status is-loading"
        to="/system"
      >
        <span aria-hidden="true" className="pw-platform-status__skeleton">
          <span className="sk" />
          <span className="sk" />
        </span>
        <Icon name="chevronRight" size="xs" />
      </NavLink>
    );
  }

  const status =
    failed ||
    queries.server.data === undefined ||
    queries.components.data === undefined
      ? unavailablePlatformStatus()
      : buildPlatformStatus(queries.server.data, queries.components.data);

  return (
    <NavLink
      aria-label={`平台状态：${status.label}。${status.detail}`}
      className={`pw-platform-status pw-platform-status--${status.tone}`}
      title={`平台状态：${status.label}。${status.detail}`}
      to="/system"
    >
      <Icon name={status.icon} size="s" />
      <span className="pw-platform-status__copy">
        <strong>平台状态：{status.label}</strong>
        <span>{status.detail}</span>
      </span>
      <Icon name="chevronRight" size="xs" />
    </NavLink>
  );
}

function NavigationDrawer({
  apiClient,
  canViewSystem,
  onOpenChange,
  open,
  routes,
}: {
  apiClient: ApiClient;
  canViewSystem: boolean;
  onOpenChange: (open: boolean) => void;
  open: boolean;
  routes: readonly NavigationRoute[];
}) {
  return (
    <Dialog.Root onOpenChange={onOpenChange} open={open}>
      <Dialog.Trigger asChild>
        <Button
          aria-label="打开主导航"
          className="pw-navigation-trigger"
          icon="menu"
          size="s"
          variant="ghost"
        >
          <span className="pw-visually-hidden">打开主导航</span>
        </Button>
      </Dialog.Trigger>
      <Dialog.Portal>
        <Dialog.Overlay className="pw-navigation-scrim" />
        <Dialog.Content className="pw-navigation-drawer">
          <Dialog.Title className="pw-visually-hidden">主导航</Dialog.Title>
          <div className="pw-navigation-drawer__head">
            <Brand />
            <Dialog.Close asChild>
              <Button
                aria-label="关闭主导航"
                icon="close"
                size="s"
                variant="ghost"
              >
                <span className="pw-visually-hidden">关闭主导航</span>
              </Button>
            </Dialog.Close>
          </div>
          <NavigationList
            onNavigate={() => {
              onOpenChange(false);
            }}
            routes={routes}
          />
          {canViewSystem ? <PlatformStatus apiClient={apiClient} /> : null}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

function UserMenu({ apiClient, me }: { apiClient: ApiClient; me: MeResponse }) {
  const navigate = useNavigate();
  const session = useSession();
  const [logoutError, setLogoutError] = useState<AppError>();
  const [open, setOpen] = useState(false);
  const grants = useUser(apiClient, me.user.id, open && me.role === "admin");

  if (logoutError !== undefined) {
    throw logoutError;
  }

  async function logout() {
    try {
      await apiClient.logout();
      session.clear();
      void navigate("/login", { replace: true });
    } catch (error) {
      setLogoutError(toAppError(error));
    }
  }

  return (
    <details
      className="pw-user-menu"
      onToggle={(event) => {
        setOpen(event.currentTarget.open);
      }}
    >
      <summary aria-label="打开用户菜单">
        <span className="pw-user-menu__avatar">
          <Icon name="avatar" size="s" />
        </span>
        <span className="pw-user-menu__name">{me.user.display_name}</span>
        <Icon name="chevronDown" size="xs" />
      </summary>
      <div className="pw-user-menu__popover">
        <span className="pw-user-menu__identity">
          <strong>{me.user.display_name}</strong>
          <span>{me.role === "admin" ? "管理员" : "普通用户"}</span>
        </span>
        <NavLink className="pw-user-menu__item" to="/profile">
          <Icon name="avatar" size="s" />
          个人资料
        </NavLink>
        {me.role === "admin" &&
        !grants.isError &&
        (grants.data?.authorized_service_count ?? 0) > 0 ? (
          <NavLink className="pw-user-menu__item" to="/api-keys">
            <Icon name="key" size="s" />
            个人 API 密钥
          </NavLink>
        ) : null}
        <NavLink className="pw-user-menu__item" to="/change-password">
          <Icon name="lock" size="s" />
          修改密码
        </NavLink>
        <button
          className="pw-user-menu__item"
          onClick={() => void logout()}
          type="button"
        >
          <Icon name="close" size="s" />
          退出登录
        </button>
      </div>
    </details>
  );
}
