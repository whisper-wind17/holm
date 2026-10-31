import type { HTMLAttributes } from "react";

export const ICON_CLASS_NAMES = {
  add: "i-add",
  alert: "i-warn",
  apiServices: "i-cloud",
  avatar: "i-avatar",
  chevronDown: "i-cdown",
  chevronRight: "i-cright",
  check: "i-check",
  close: "i-close",
  collapse: "i-collapse",
  copy: "i-copy",
  data: "i-gpu",
  error: "i-fail",
  expand: "i-expand",
  help: "i-help",
  home: "i-home",
  info: "i-info",
  key: "i-key",
  layers: "i-layers",
  lock: "i-lock",
  loading: "i-refresh",
  menu: "i-menu",
  notice: "i-clock",
  pause: "i-pause",
  search: "i-search",
  success: "i-ok",
  system: "i-tools",
  usage: "i-chart",
  users: "i-users",
  visibility: "i-eye",
  visibilityOff: "i-eye-off",
} as const;

export type IconName = keyof typeof ICON_CLASS_NAMES;
export type IconSize = "xs" | "s" | "m" | "l" | "xl" | "empty";

export interface IconProps extends Omit<
  HTMLAttributes<HTMLSpanElement>,
  "children"
> {
  label?: string;
  name: IconName;
  size?: IconSize;
}

const SIZE_CLASS_NAMES: Readonly<Record<IconSize, string>> = {
  empty: "pwi-96",
  l: "pwi-l",
  m: "",
  s: "pwi-s",
  xl: "pwi-xl",
  xs: "pwi-xs",
};

export function Icon({
  className,
  label,
  name,
  size = "m",
  ...props
}: IconProps) {
  const classes = [
    "pwi",
    ICON_CLASS_NAMES[name],
    SIZE_CLASS_NAMES[size],
    className,
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <span
      {...props}
      aria-hidden={label === undefined ? true : undefined}
      aria-label={label}
      className={classes}
      role={label === undefined ? undefined : "img"}
    />
  );
}
