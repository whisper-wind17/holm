import * as Dialog from "@radix-ui/react-dialog";
import { useEffect, useRef, useState, type ReactElement } from "react";

import { Button } from "./button";
import { CopyControl } from "./copy-control";
import { Icon } from "./icon";

export interface OneTimeSecretModalProps {
  kind: "API 密钥" | "临时密码";
  onOpenChange: (open: boolean) => void;
  open: boolean;
  secret: string;
  trigger: ReactElement;
}

export function OneTimeSecretModal({
  kind,
  onOpenChange,
  open,
  secret,
  trigger,
}: OneTimeSecretModalProps) {
  const [confirmClose, setConfirmClose] = useState(false);
  const [saved, setSaved] = useState(false);
  const titleRef = useRef<HTMLHeadingElement>(null);

  useEffect(() => {
    if (!open) {
      setConfirmClose(false);
      setSaved(false);
    }
  }, [open]);

  function requestOpenChange(nextOpen: boolean) {
    if (nextOpen || saved) {
      onOpenChange(nextOpen);
      return;
    }
    setConfirmClose(true);
  }

  function closeAfterConfirmation() {
    setConfirmClose(false);
    onOpenChange(false);
  }

  function confirmSaved() {
    setSaved(true);
    onOpenChange(false);
  }

  return (
    <Dialog.Root onOpenChange={requestOpenChange} open={open}>
      <Dialog.Trigger asChild>{trigger}</Dialog.Trigger>
      <Dialog.Portal>
        <Dialog.Overlay className="pw-dialog-overlay" />
        <Dialog.Content
          className="pw-dialog pw-dialog--l"
          onEscapeKeyDown={(event) => {
            if (confirmClose) {
              event.preventDefault();
              setConfirmClose(false);
            }
          }}
          onOpenAutoFocus={(event) => {
            event.preventDefault();
            titleRef.current?.focus();
          }}
        >
          {confirmClose ? (
            <>
              <header className="pw-dialog__header">
                <div>
                  <Dialog.Title asChild>
                    <h2 ref={titleRef} tabIndex={-1}>
                      关闭后无法再次查看
                    </h2>
                  </Dialog.Title>
                  <Dialog.Description>
                    这份{kind}只显示一次。返回复制，或确认仍然关闭。
                  </Dialog.Description>
                </div>
              </header>
              <div className="pw-dialog__body">
                <div className="pw-secret-confirm-actions">
                  <Button
                    block
                    onClick={() => {
                      setConfirmClose(false);
                    }}
                    variant="primary"
                  >
                    返回复制
                  </Button>
                  <Button
                    block
                    onClick={closeAfterConfirmation}
                    variant="danger"
                  >
                    仍然关闭
                  </Button>
                </div>
              </div>
            </>
          ) : (
            <>
              <header className="pw-dialog__header">
                <div>
                  <Dialog.Title asChild>
                    <h2 ref={titleRef} tabIndex={-1}>
                      保存{kind}
                    </h2>
                  </Dialog.Title>
                  <Dialog.Description>
                    完整{kind}仅显示这一次。请立即复制到密码管理器。
                  </Dialog.Description>
                </div>
                <Button
                  aria-label={`关闭${kind}弹层`}
                  icon="close"
                  onClick={() => {
                    setConfirmClose(true);
                  }}
                  size="s"
                  variant="ghost"
                >
                  关闭
                </Button>
              </header>
              <div className="pw-dialog__body">
                <div className="pw-secret-value">
                  <Icon name="key" />
                  <code>{secret}</code>
                  <CopyControl
                    label={`复制${kind}`}
                    value={secret}
                    variant="secondary"
                  />
                </div>
                <p className="pw-secret-note">
                  关闭后无法再次查看，页面不会提供下载入口。
                </p>
              </div>
              <footer className="pw-dialog__footer">
                <Button onClick={confirmSaved} variant="primary">
                  我已保存{kind}
                </Button>
              </footer>
            </>
          )}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
