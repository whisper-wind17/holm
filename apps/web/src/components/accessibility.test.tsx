import axe from "axe-core";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { Button } from "./button";
import { Chart } from "./chart";
import { DataTable } from "./data-table";
import { EmptyState, InlineAlert } from "./feedback";
import { TextField } from "./form";
import { Modal } from "./overlays";
import { PasswordPolicyFeedback } from "./password-policy";
import { ProgressStages } from "./progress-stages";
import { StatusBadge } from "./status-badge";
import { Tabs } from "./tabs";

async function expectNoCriticalOrSeriousViolations(
  context: Element | Document,
) {
  const result = await axe.run(context, {
    rules: {
      // jsdom has no canvas-backed computed-color implementation; M1-11A owns contrast.
      "color-contrast": { enabled: false },
    },
  });
  const blocking = result.violations.filter(
    (violation) =>
      violation.impact === "critical" || violation.impact === "serious",
  );
  expect(
    result.passes.flatMap((rule) => rule.nodes),
    "axe must scan at least one node and report a passing rule",
  ).not.toHaveLength(0);
  expect(blocking).toEqual([]);
}

describe("TC-UI-01 component-level axe scans", () => {
  it("has no Critical or Serious violations across the component surface", async () => {
    const { container } = render(
      <main>
        <h1>组件检查</h1>
        <Button variant="primary">部署模型</Button>
        <TextField
          helpText="用于 API 请求。"
          label="API 模型名"
          name="public-name"
        />
        <StatusBadge icon="success" text="运行中" tone="positive" />
        <InlineAlert title="监控数据暂不可用" tone="notice">
          最后更新于 10:30，请稍后重试。
        </InlineAlert>
        <DataTable
          caption="AI 服务"
          columns={[
            { header: "名称", id: "name", render: (row: string) => row },
          ]}
          getRowKey={(row) => row}
          rows={["qwen-service"]}
        />
        <EmptyState
          action={<Button variant="primary">部署模型</Button>}
          description="部署模型后，才能发布供团队使用的 AI 服务。"
          icon="data"
          title="还没有模型实例"
        />
        <ProgressStages
          label="部署进度"
          percent={40}
          stages={[
            { id: "load", label: "加载权重", percent: 40, status: "current" },
          ]}
        />
        <PasswordPolicyFeedback password="orchid maple zephyr quartz" />
        <Chart
          ariaLabel="请求数"
          categories={["10:00"]}
          kind="ordinal"
          series={[{ id: "p50", label: "P50", values: [1] }]}
        />
        <Tabs
          aria-label="系统信息"
          defaultValue="server"
          items={[
            { content: <p>服务器内容</p>, label: "服务器", value: "server" },
            {
              content: <p>平台组件内容</p>,
              label: "平台组件",
              value: "components",
            },
          ]}
        />
      </main>,
    );
    await expectNoCriticalOrSeriousViolations(container);
  });

  it("has no Critical or Serious violations when a Radix modal is open", async () => {
    render(
      <Modal title="创建服务" trigger={<Button>打开弹层</Button>}>
        <TextField label="服务名称" name="service-name" />
      </Modal>,
    );
    await userEvent.click(screen.getByRole("button", { name: "打开弹层" }));
    await expectNoCriticalOrSeriousViolations(screen.getByRole("dialog"));
  });
});
