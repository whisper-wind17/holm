import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { Button } from "./button";
import { DataTable, MissingValue, TruncatedValue } from "./data-table";
import { Banner, EmptyState, InlineAlert } from "./feedback";
import { TextField } from "./form";
import { ProgressStages } from "./progress-stages";
import { StatusBadge } from "./status-badge";
import { Tabs } from "./tabs";

describe("TC-UI-08 component state semantics", () => {
  it("covers default, hover, pressed, selected, disabled, and loading button states", async () => {
    const user = userEvent.setup();
    const onClick = vi.fn();
    const onPointerDown = vi.fn();
    const onPointerEnter = vi.fn();
    const { rerender } = render(
      <Button
        onClick={onClick}
        onPointerDown={onPointerDown}
        onPointerEnter={onPointerEnter}
      >
        部署模型
      </Button>,
    );
    const button = screen.getByRole("button", { name: "部署模型" });

    expect(button).toHaveClass("pw-button--secondary");
    await user.hover(button);
    expect(onPointerEnter).toHaveBeenCalledOnce();
    await user.pointer([{ keys: "[MouseLeft>]", target: button }]);
    expect(onPointerDown).toHaveBeenCalledOnce();
    await user.pointer([{ keys: "[/MouseLeft]", target: button }]);
    expect(onClick).toHaveBeenCalledOnce();

    rerender(<Button selected>部署模型</Button>);
    expect(button).toHaveAttribute("aria-pressed", "true");
    rerender(<Button disabled>部署模型</Button>);
    expect(button).toBeDisabled();
    rerender(
      <Button loading loadingText="正在部署">
        部署模型
      </Button>,
    );
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute("aria-busy", "true");
    expect(button).toHaveTextContent("正在部署");
  });

  it("renders an actionable form error without disabling unrelated primary actions", () => {
    render(
      <>
        <TextField
          errorText="API 模型名格式不正确。试试 qwen3-32b。"
          helpText="用于 API 请求。"
          label="API 模型名"
          name="public-name"
          required
        />
        <Button variant="primary">创建服务</Button>
      </>,
    );

    expect(screen.getByRole("textbox", { name: /API 模型名/ })).toHaveAttribute(
      "aria-invalid",
      "true",
    );
    const target = document.querySelector(".pw-input-target");
    expect(target).not.toBeNull();
    target?.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
    expect(screen.getByRole("textbox", { name: /API 模型名/ })).toHaveFocus();
    expect(screen.getByRole("alert")).toHaveTextContent("试试 qwen3-32b");
    expect(screen.getByRole("button", { name: "创建服务" })).toBeEnabled();
  });

  it("distinguishes a busy skeleton table from an empty result", () => {
    const columns = [
      {
        header: "调用数",
        id: "count",
        numeric: true,
        render: (row: number) => row,
      },
    ];
    const { rerender } = render(
      <DataTable
        caption="用量"
        columns={columns}
        getRowKey={String}
        loading
        rows={[]}
      />,
    );
    const busy = screen.getByLabelText("用量，正在加载");
    expect(busy).toHaveAttribute("aria-busy", "true");
    expect(within(busy).queryByText(/[0-9—]/)).not.toBeInTheDocument();
    expect(busy.querySelectorAll("tbody tr")).toHaveLength(6);

    rerender(
      <DataTable
        caption="用量"
        columns={columns}
        emptyContent="没有调用记录"
        getRowKey={String}
        rows={[]}
      />,
    );
    expect(screen.getByText("没有调用记录")).toBeInTheDocument();
    expect(screen.getByLabelText("用量")).toHaveAttribute("aria-busy", "false");
  });

  it("keeps zero, unavailable, and not-applicable values distinct", () => {
    render(
      <div>
        <span>0</span>
        <MissingValue
          kind="unavailable"
          lastUpdated="10:30"
          reason="监控数据暂不可用"
        />
        <MissingValue kind="not-applicable" reason="用户已删除，不再适用" />
      </div>,
    );

    expect(screen.getByText("0")).toBeInTheDocument();
    const unavailable = screen.getAllByLabelText("暂无数据")[0];
    const notApplicable = screen.getAllByLabelText("暂无数据")[1];
    expect(
      unavailable?.parentElement?.querySelector(".i-clock"),
    ).not.toBeNull();
    expect(notApplicable?.parentElement?.querySelector(".i-clock")).toBeNull();
    expect(unavailable).toHaveAccessibleDescription(/最后更新：10:30/);
  });

  it("renders status icon, Chinese text, and an optional note as separate structure", () => {
    render(
      <StatusBadge
        icon="alert"
        note="自动重启 1/3"
        text="运行异常"
        tone="negative"
      />,
    );
    expect(screen.getByText("运行异常")).toBeInTheDocument();
    expect(screen.getByText("自动重启 1/3")).toHaveClass("pw-status__note");
    expect(document.querySelector(".pw-status .i-warn")).not.toBeNull();
  });
});

describe("TC-UI-11 empty states and TC-UI-12 text boundaries", () => {
  it("gives an empty state exactly one primary action and a search result only clear-filter", () => {
    const { rerender } = render(
      <EmptyState
        action={<Button variant="primary">部署模型</Button>}
        description="部署模型后，才能发布供团队使用的 AI 服务。"
        icon="data"
        title="还没有模型实例"
      />,
    );
    expect(screen.getAllByRole("button")).toHaveLength(1);
    expect(
      screen.getByRole("button", { name: "部署模型" }),
    ).toBeInTheDocument();

    rerender(
      <EmptyState
        action={<Button>清除筛选</Button>}
        description="调整筛选条件后再试。"
        icon="search"
        title="没有匹配结果"
        variant="search-no-results"
      />,
    );
    expect(
      screen.getByRole("button", { name: "清除筛选" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /创建|部署/ }),
    ).not.toBeInTheDocument();
  });

  it("preserves the accessible full value and offers copy for truncated technical text", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    const value = "模型/路径/very-long-名字-🙂-مرحبا";
    render(<TruncatedValue value={value} />);
    expect(screen.getByLabelText(value)).toHaveAttribute("title", value);
    await userEvent.click(screen.getByRole("button", { name: "复制完整值" }));
    expect(writeText).toHaveBeenCalledWith(value);
    expect(
      screen.getByRole("button", { name: "复制完整值" }),
    ).toHaveTextContent("已复制");
  });

  it("renders inline alerts with icon, title, and remediation text", () => {
    render(
      <InlineAlert title="无法保存" tone="negative">
        请修正标记字段后重试。
      </InlineAlert>,
    );
    expect(screen.getByRole("alert")).toHaveTextContent(
      "无法保存请修正标记字段后重试",
    );
    expect(document.querySelector(".pw-alert .i-fail")).not.toBeNull();
  });

  it("renders a system-level banner with text and a non-color status signal", () => {
    render(
      <Banner title="页面数据可能过期" tone="notice">
        已切换为轮询，请稍后刷新。
      </Banner>,
    );
    expect(screen.getByRole("status")).toHaveTextContent(
      "页面数据可能过期已切换为轮询，请稍后刷新",
    );
    expect(document.querySelector(".pw-banner .i-warn")).not.toBeNull();
  });
});

describe("TC-UI-10 reduced motion progress", () => {
  it("sets reduced-motion state while preserving stage text and percentages", async () => {
    vi.spyOn(window, "matchMedia").mockReturnValue({
      addEventListener: vi.fn(),
      addListener: vi.fn(),
      dispatchEvent: vi.fn(),
      matches: true,
      media: "(prefers-reduced-motion: reduce)",
      onchange: null,
      removeEventListener: vi.fn(),
      removeListener: vi.fn(),
    });
    render(
      <ProgressStages
        label="部署进度"
        percent={62}
        stages={[
          { id: "done", label: "准备文件", status: "completed" },
          { id: "current", label: "加载权重", percent: 62, status: "current" },
          { id: "skip", label: "兼容检查", status: "skipped" },
          { id: "not-run", label: "健康检查", status: "not-run" },
          { id: "wait", label: "发布服务", status: "waiting" },
        ]}
      />,
    );

    expect(await screen.findByLabelText("部署进度")).toHaveAttribute(
      "data-reduced-motion",
      "true",
    );
    expect(screen.getAllByText("62%").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("已跳过")).toBeInTheDocument();
    expect(screen.getByText("未执行")).toBeInTheDocument();
    expect(screen.getByText("等待")).toBeInTheDocument();
  });

  it("does not set reduced-motion state when the preference does not match", async () => {
    vi.spyOn(window, "matchMedia").mockReturnValue({
      addEventListener: vi.fn(),
      addListener: vi.fn(),
      dispatchEvent: vi.fn(),
      matches: false,
      media: "(prefers-reduced-motion: reduce)",
      onchange: null,
      removeEventListener: vi.fn(),
      removeListener: vi.fn(),
    });
    render(
      <ProgressStages
        label="下载进度"
        percent={37}
        stages={[
          { id: "current", label: "下载文件", percent: 37, status: "current" },
        ]}
      />,
    );

    expect(await screen.findByLabelText("下载进度")).not.toHaveAttribute(
      "data-reduced-motion",
      "true",
    );
    expect(screen.getByText("下载文件")).toBeInTheDocument();
    expect(screen.getAllByText("37%").length).toBeGreaterThanOrEqual(1);
  });
});

describe("TC-UI-01 accessible tab states", () => {
  it("moves with arrow keys and lets Tab enter and leave the tab stop", async () => {
    const user = userEvent.setup();
    render(
      <>
        <Button>标签页之前</Button>
        <Tabs
          aria-label="系统信息"
          defaultValue="server"
          items={[
            { content: <p>服务器内容</p>, label: "服务器", value: "server" },
            {
              content: <p>组件内容</p>,
              label: "平台组件",
              value: "components",
            },
            {
              content: <p>未来内容</p>,
              disabled: true,
              label: "未来标签",
              value: "future",
            },
          ]}
        />
        <Button>标签页之后</Button>
      </>,
    );

    await user.click(screen.getByRole("tab", { name: "服务器" }));
    await user.keyboard("{ArrowRight}");
    expect(screen.getByRole("tab", { name: "平台组件" })).toHaveFocus();
    expect(screen.getByRole("tab", { name: "平台组件" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.getByText("组件内容")).toBeVisible();
    expect(screen.getByRole("tab", { name: "未来标签" })).toBeDisabled();
    await user.tab();
    expect(screen.getByRole("tabpanel")).toHaveFocus();
    await user.tab();
    expect(screen.getByRole("button", { name: "标签页之后" })).toHaveFocus();
  });
});
