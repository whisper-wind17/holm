import type { ReactNode } from "react";

import { Icon, type IconName } from "./icon";

export type StatusTone = "positive" | "notice" | "negative" | "info" | "idle";

export interface StatusBadgeProps {
  icon: IconName;
  note?: ReactNode;
  size?: "default" | "large";
  text: string;
  tone: StatusTone;
}

export function StatusBadge({
  icon,
  note,
  size = "default",
  text,
  tone,
}: StatusBadgeProps) {
  return (
    <span className="pw-status-group">
      <span
        className={[
          "pw-status",
          `pw-status--${tone}`,
          size === "large" ? "pw-status--large" : undefined,
        ]
          .filter(Boolean)
          .join(" ")}
      >
        <Icon name={icon} size="xs" />
        <span>{text}</span>
      </span>
      {note !== undefined ? (
        <span className="pw-status__note">{note}</span>
      ) : null}
    </span>
  );
}
