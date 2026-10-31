import * as Dialog from "@radix-ui/react-dialog";
import { useEffect, useRef, type ReactElement, type ReactNode } from "react";

import { Button } from "./button";
import { Icon } from "./icon";
import type { StatusTone } from "./status-badge";

export type ModalSize = "s" | "m" | "l" | "xl";

export interface ModalProps {
  children: ReactNode;
  defaultOpen?: boolean;
  description?: string;
  footer?: ReactNode;
  onCloseAutoFocus?: (event: Event) => void;
  onOpenChange?: (open: boolean) => void;
  open?: boolean;
  size?: ModalSize;
  title: string;
  trigger: ReactElement;
}

export function Modal({
  children,
  defaultOpen,
  description,
  footer,
  onCloseAutoFocus,
  onOpenChange,
  open,
  size = "m",
  title,
  trigger,
}: ModalProps) {
  const titleRef = useRef<HTMLHeadingElement>(null);

  return (
    <Dialog.Root
      defaultOpen={defaultOpen}
      onOpenChange={onOpenChange}
      open={open}
    >
      <Dialog.Trigger asChild>{trigger}</Dialog.Trigger>
      <Dialog.Portal>
        <Dialog.Overlay className="pw-dialog-overlay" />
        <Dialog.Content
          className={`pw-dialog pw-dialog--${size}`}
          onCloseAutoFocus={onCloseAutoFocus}
          onOpenAutoFocus={(event) => {
            event.preventDefault();
            titleRef.current?.focus();
          }}
        >
          <header className="pw-dialog__header">
            <div>
              <Dialog.Title asChild>
                <h2 ref={titleRef} tabIndex={-1}>
                  {title}
                </h2>
              </Dialog.Title>
              {description ? (
                <Dialog.Description>{description}</Dialog.Description>
              ) : null}
            </div>
            <Dialog.Close asChild>
              <Button aria-label="关闭" icon="close" size="s" variant="ghost">
                关闭
              </Button>
            </Dialog.Close>
          </header>
          <div className="pw-dialog__body">{children}</div>
          {footer !== undefined ? (
            <footer className="pw-dialog__footer">{footer}</footer>
          ) : null}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

export interface DrawerProps {
  children: ReactNode;
  defaultOpen?: boolean;
  description?: string;
  onOpenChange?: (open: boolean) => void;
  open?: boolean;
  title: string;
  trigger: ReactElement;
  wide?: boolean;
}

export function Drawer({
  children,
  defaultOpen,
  description,
  onOpenChange,
  open,
  title,
  trigger,
  wide = false,
}: DrawerProps) {
  const titleRef = useRef<HTMLHeadingElement>(null);

  return (
    <Dialog.Root
      defaultOpen={defaultOpen}
      onOpenChange={onOpenChange}
      open={open}
    >
      <Dialog.Trigger asChild>{trigger}</Dialog.Trigger>
      <Dialog.Portal>
        <Dialog.Overlay className="pw-drawer-overlay" />
        <Dialog.Content
          className={["pw-drawer", wide ? "pw-drawer--wide" : undefined]
            .filter(Boolean)
            .join(" ")}
          onOpenAutoFocus={(event) => {
            event.preventDefault();
            titleRef.current?.focus();
          }}
        >
          <header className="pw-dialog__header">
            <div>
              <Dialog.Title asChild>
                <h2 ref={titleRef} tabIndex={-1}>
                  {title}
                </h2>
              </Dialog.Title>
              {description ? (
                <Dialog.Description>{description}</Dialog.Description>
              ) : null}
            </div>
            <Dialog.Close asChild>
              <Button
                aria-label="关闭详情"
                icon="close"
                size="s"
                variant="ghost"
              >
                关闭
              </Button>
            </Dialog.Close>
          </header>
          <div className="pw-dialog__body">{children}</div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

export interface ToastItem {
  id: string;
  message: string;
  tone?: Extract<StatusTone, "positive" | "negative" | "info">;
}

export interface ToastRegionProps {
  duration?: number;
  items: readonly ToastItem[];
  onDismiss: (id: string) => void;
}

export const DEFAULT_TOAST_DURATION = 4000;

export function ToastRegion({
  duration = DEFAULT_TOAST_DURATION,
  items,
  onDismiss,
}: ToastRegionProps) {
  const visibleItems = items.slice(0, 3);

  useEffect(() => {
    const timers = visibleItems.map((item) =>
      window.setTimeout(() => {
        onDismiss(item.id);
      }, duration),
    );
    return () => {
      timers.forEach((timer) => {
        window.clearTimeout(timer);
      });
    };
  }, [duration, onDismiss, visibleItems]);

  return (
    <div aria-label="通知" className="pw-toast-region">
      {visibleItems.map((item) => (
        <div
          className="pw-toast"
          key={item.id}
          role={item.tone === "negative" ? "alert" : "status"}
        >
          <Icon
            name={
              item.tone === "negative"
                ? "error"
                : item.tone === "info"
                  ? "info"
                  : "success"
            }
            size="s"
          />
          <span>{item.message}</span>
          <Button
            aria-label={`关闭通知：${item.message}`}
            icon="close"
            onClick={() => {
              onDismiss(item.id);
            }}
            size="s"
            variant="ghost"
          >
            关闭
          </Button>
        </div>
      ))}
    </div>
  );
}
