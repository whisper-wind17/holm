import type { ButtonHTMLAttributes, ReactNode } from "react";

import { Icon, type IconName } from "./icon";

export type ButtonVariant = "primary" | "secondary" | "ghost" | "danger";
export type ButtonSize = "s" | "m" | "l";

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  block?: boolean;
  icon?: IconName;
  loading?: boolean;
  loadingText?: ReactNode;
  selected?: boolean;
  size?: ButtonSize;
  variant?: ButtonVariant;
}

export function Button({
  block = false,
  children,
  className,
  disabled = false,
  icon,
  loading = false,
  loadingText,
  selected = false,
  size = "m",
  type = "button",
  variant = "secondary",
  ...props
}: ButtonProps) {
  const classes = [
    "pw-button",
    `pw-button--${variant}`,
    `pw-button--${size}`,
    block ? "pw-button--block" : undefined,
    className,
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <button
      {...props}
      aria-busy={loading || undefined}
      aria-pressed={selected ? true : props["aria-pressed"]}
      className={classes}
      data-loading={loading || undefined}
      data-selected={selected || undefined}
      disabled={disabled || loading}
      type={type}
    >
      {loading ? (
        <Icon className="pw-icon--spin" name="loading" size="s" />
      ) : null}
      {!loading && icon !== undefined ? <Icon name={icon} size="s" /> : null}
      <span>
        {loading && loadingText !== undefined ? loadingText : children}
      </span>
    </button>
  );
}
