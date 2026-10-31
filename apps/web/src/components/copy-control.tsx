import { useEffect, useRef, useState } from "react";

import { Button, type ButtonProps } from "./button";

export interface CopyControlProps extends Omit<
  ButtonProps,
  "children" | "icon" | "loading" | "onClick"
> {
  label?: string;
  persistCopied?: boolean;
  onCopied?: () => void;
  onCopyError?: () => void;
  value: string;
}

export function CopyControl({
  label = "复制",
  persistCopied = false,
  onCopyError,
  onCopied,
  value,
  ...buttonProps
}: CopyControlProps) {
  const [state, setState] = useState<"idle" | "success" | "error">("idle");
  const resetTimer = useRef<number | undefined>(undefined);

  useEffect(
    () => () => {
      if (resetTimer.current !== undefined) {
        window.clearTimeout(resetTimer.current);
      }
    },
    [],
  );

  async function copy() {
    try {
      await navigator.clipboard.writeText(value);
      setState("success");
      onCopied?.();
      if (!persistCopied) {
        resetTimer.current = window.setTimeout(() => {
          setState("idle");
        }, 2000);
      }
    } catch {
      setState("error");
      onCopyError?.();
    }
  }

  return (
    <Button
      {...buttonProps}
      icon={state === "success" ? "check" : "copy"}
      onClick={() => void copy()}
      size={buttonProps.size ?? "s"}
      variant={buttonProps.variant ?? "ghost"}
    >
      {state === "success" ? "已复制" : state === "error" ? "复制失败" : label}
    </Button>
  );
}
