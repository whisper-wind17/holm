import type { ReactNode } from "react";

import type { StatusTone } from "./status-badge";
import { Icon, type IconName } from "./icon";

const TONE_ICONS: Readonly<Record<StatusTone, IconName>> = {
  idle: "info",
  info: "info",
  negative: "error",
  notice: "alert",
  positive: "success",
};

export interface FeedbackProps {
  children: ReactNode;
  className?: string;
  title: string;
  tone: StatusTone;
}

export function InlineAlert({
  children,
  className,
  title,
  tone,
}: FeedbackProps) {
  return (
    <div
      className={["pw-alert", `pw-alert--${tone}`, className]
        .filter(Boolean)
        .join(" ")}
      role={tone === "negative" ? "alert" : "status"}
    >
      <Icon name={TONE_ICONS[tone]} size="s" />
      <span>
        <strong>{title}</strong>
        <span>{children}</span>
      </span>
    </div>
  );
}

export function Banner({ children, className, title, tone }: FeedbackProps) {
  return (
    <div
      className={["pw-banner", `pw-alert--${tone}`, className]
        .filter(Boolean)
        .join(" ")}
      role={tone === "negative" ? "alert" : "status"}
    >
      <Icon name={TONE_ICONS[tone]} size="s" />
      <strong>{title}</strong>
      <span>{children}</span>
    </div>
  );
}

export interface EmptyStateProps {
  action?: ReactNode;
  description: string;
  icon: IconName;
  title: string;
  variant?: "empty" | "search-no-results";
}

export function EmptyState({
  action,
  description,
  icon,
  title,
  variant = "empty",
}: EmptyStateProps) {
  return (
    <section className="pw-empty-state" data-variant={variant}>
      <Icon name={icon} size="empty" />
      <h2>{title}</h2>
      <p>{description}</p>
      {action === undefined ? null : (
        <div className="pw-empty-state__action">{action}</div>
      )}
    </section>
  );
}
