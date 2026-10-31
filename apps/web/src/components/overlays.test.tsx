import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

import { Button } from "./button";
import { OneTimeSecretModal } from "./one-time-secret";
import { Drawer, Modal, ToastRegion } from "./overlays";

describe("TC-UI-03 Radix overlay focus management", () => {
  it("moves focus into a modal, traps Tab, closes with Escape, and restores focus", async () => {
    const user = userEvent.setup();
    render(
      <Modal
        footer={<Button variant="primary">确认</Button>}
        title="确认操作"
        trigger={<Button>打开确认</Button>}
      >
        <Button>取消</Button>
      </Modal>,
    );
    const trigger = screen.getByRole("button", { name: "打开确认" });
    await user.click(trigger);
    expect(screen.getByRole("heading", { name: "确认操作" })).toHaveFocus();

    await user.tab();
    expect(screen.getByRole("button", { name: "关闭" })).toHaveFocus();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
  });

  it("uses the same keyboard contract for the right-side drawer", async () => {
    const user = userEvent.setup();
    render(
      <Drawer title="实例详情" trigger={<Button>查看详情</Button>}>
        <p>只读内容</p>
      </Drawer>,
    );
    const trigger = screen.getByRole("button", { name: "查看详情" });
    await user.click(trigger);
    expect(screen.getByRole("heading", { name: "实例详情" })).toHaveFocus();
    await user.keyboard("{Escape}");
    expect(trigger).toHaveFocus();
  });
});

describe("TC-UI-19 one-time plaintext flow", () => {
  function Harness() {
    const [open, setOpen] = useState(false);
    return (
      <OneTimeSecretModal
        kind="API 密钥"
        onOpenChange={setOpen}
        open={open}
        secret="pwk_once_only"
        trigger={<Button>显示密钥</Button>}
      />
    );
  }

  it("offers copy without download and keeps the close confirmation escapable", async () => {
    const user = userEvent.setup();
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    render(<Harness />);
    await user.click(screen.getByRole("button", { name: "显示密钥" }));
    expect(screen.getByText("pwk_once_only")).toBeInTheDocument();
    expect(
      screen.queryByRole("link", { name: /下载/ }),
    ).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "复制API 密钥" }));
    expect(writeText).toHaveBeenCalledWith("pwk_once_only");

    await user.click(screen.getByRole("button", { name: "关闭API 密钥弹层" }));
    expect(
      screen.getByRole("heading", { name: "关闭后无法再次查看" }),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "仍然关闭" })).toBeEnabled();
    await user.keyboard("{Escape}");
    expect(
      screen.getByRole("heading", { name: "保存API 密钥" }),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "关闭API 密钥弹层" }));
    await user.click(screen.getByRole("button", { name: "仍然关闭" }));
    expect(screen.queryByText("pwk_once_only")).not.toBeInTheDocument();
  });
});

describe("TC-UI-08 toast boundaries", () => {
  it("caps visible toasts at three and dismisses them after four seconds", () => {
    vi.useFakeTimers();
    const onDismiss = vi.fn();
    render(
      <ToastRegion
        items={[
          { id: "1", message: "服务已启用" },
          { id: "2", message: "API 密钥已撤销" },
          { id: "3", message: "Endpoint 已复制" },
          { id: "4", message: "不应显示" },
        ]}
        onDismiss={onDismiss}
      />,
    );
    expect(screen.getAllByRole("status")).toHaveLength(3);
    expect(screen.queryByText("不应显示")).not.toBeInTheDocument();
    vi.advanceTimersByTime(4000);
    expect(onDismiss).toHaveBeenCalledTimes(3);
  });
});
