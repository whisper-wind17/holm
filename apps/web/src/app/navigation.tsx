import { useEffect, useRef } from "react";

import type { NavigationResponse } from "../api/contracts";
import type { IconName } from "../components";

export interface NavigationRoute {
  icon: IconName;
  id: string;
  label: string;
  path: string;
}

export class NavigationContractError extends Error {
  readonly code = "navigation_contract_error";

  constructor() {
    super("server returned an unsupported navigation identifier");
    this.name = "NavigationContractError";
  }
}

function routePresentation(id: string): Pick<NavigationRoute, "icon" | "path"> {
  switch (id) {
    case "overview":
      return { icon: "home", path: "/overview" };
    case "ai_services":
      return { icon: "apiServices", path: "/services" };
    case "models":
      return { icon: "layers", path: "/models" };
    case "users":
      return { icon: "users", path: "/users" };
    case "usage":
      return { icon: "usage", path: "/usage" };
    case "system":
      return { icon: "system", path: "/system" };
    case "api_keys":
      return { icon: "key", path: "/api-keys" };
    default:
      throw new NavigationContractError();
  }
}

export function buildNavigationRoutes(
  navigation: readonly NavigationResponse[],
) {
  if (navigation.length === 0) {
    throw new NavigationContractError();
  }
  const seen = new Set<string>();
  return navigation.map((item) => {
    if (seen.has(item.id)) {
      throw new NavigationContractError();
    }
    seen.add(item.id);
    return { ...routePresentation(item.id), id: item.id, label: item.label };
  });
}

export function RoutePlaceholder({ route }: { route: NavigationRoute }) {
  const heading = useRef<HTMLHeadingElement>(null);

  useEffect(() => {
    heading.current?.focus();
  }, [route.path]);

  return (
    <div className="pw-route-content">
      <h1 className="pw-route-heading" ref={heading} tabIndex={-1}>
        {route.label}
      </h1>
      <div
        aria-label={`${route.label}内容区`}
        className="pw-route-placeholder"
        role="region"
      />
    </div>
  );
}
