import axe from "axe-core";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { AiService } from "../api/ai-service-contracts";
import type { ModelInstance } from "../api/model-instance-contracts";
import type { RequestRecordErrorGroup } from "../api/request-record-contracts";
import {
  ADMIN_ME,
  FakeApiClient,
  renderApplication,
} from "../app/test-support";

// FX-UI-22: metadata-only fixtures from the UI-04 instance and UI-19 error group contracts.
const service: AiService = {
  id: "0198f58a-2586-7e37-bbf5-000000000021",
  name: "研发助手",
  description: "测试服务说明",
  public_model_name: "code-assist",
  primary_instance_id: "0198f58a-2586-7e37-bbf5-000000000022",
  primary_instance_status: "Running",
  enabled: true,
  runtime_status: "Available",
  route_version: 1,
};
const instance: ModelInstance = {
  id: service.primary_instance_id,
  name: "研发实例",
  artifact_id: "0198f58a-2586-7e37-bbf5-000000000023",
  config_version: 1,
  context_length: 8192,
  error_code: null,
  gpu_memory_utilization: 0.9,
  restart_count: 0,
  gpu_uuids: ["FX-UI-22-GPU"],
  internal_endpoint: null,
  internal_model_name: "fx-ui-22",
  runtime_type: "vllm",
  status: "Running",
  tensor_parallel_size: 1,
  updated_at: "2026-10-13T00:00:00Z",
};
const errorGroup: RequestRecordErrorGroup = {
  status: "failed",
  error_code: "backend_unavailable",
  count: 120,
  first_at: "2026-10-10T01:00:00Z",
  last_at: "2026-10-12T01:00:00Z",
  latest_request_id: "0198f58a-2586-7e37-bbf5-000000000024",
};
function setup(
  options: {
    item?: AiService;
    alternative?: boolean;
    allowed?: boolean;
    mount?: boolean;
  } = {},
) {
  const item = options.item ?? service;
  const api = new FakeApiClient({
    ...ADMIN_ME,
    capabilities:
      options.allowed === false
        ? ADMIN_ME.capabilities.filter(
            ({ capability }) =>
              capability !== "view_request_records" &&
              capability !== "view_usage",
          )
        : [
            ...ADMIN_ME.capabilities,
            { capability: "view_usage", scope: "all" },
          ],
  });
  const getService = vi.spyOn(api, "getAiService").mockResolvedValue(item);
  vi.spyOn(api, "getModelInstances").mockResolvedValue({
    items: [
      { ...instance, status: item.primary_instance_status ?? "Stopped" },
      ...(options.alternative
        ? [
            {
              ...instance,
              id: "0198f58a-2586-7e37-bbf5-000000000025",
              name: "备用实例",
            },
          ]
        : []),
    ],
  });
  const errors = vi
    .spyOn(api, "getRequestRecordErrorGroups")
    .mockResolvedValue({ items: [] });
  const usage = vi.spyOn(api, "getUsage");
  const records = vi.spyOn(api, "getRequestRecords");
  const permissions = vi.spyOn(api, "getServicePermissions").mockResolvedValue({
    user_ids: ["user-1", "user-2"],
    limits: Object.fromEntries(
      ["user-1", "user-2"].map((id) => [
        id,
        { max_concurrent_requests: null, max_requests_per_minute: null },
      ]),
    ),
  });
  const user = userEvent.setup();
  const mount = () => renderApplication(api, `/services/${service.id}`);
  const view = options.mount === false ? undefined : mount();
  return {
    api,
    getService,
    errors,
    usage,
    records,
    permissions,
    user,
    view,
    mount,
  };
}

describe("UI-22 service detail", () => {
  it("UI-22 shows four overview blocks with scoped today counts and live backend load, passes axe", async () => {
    const { api, mount, usage, errors } = setup({ mount: false });
    const empty = await api.getInstanceTelemetry(instance.id);
    vi.spyOn(api, "getInstanceTelemetry").mockResolvedValue({
      ...empty,
      telemetry_status: "available",
      current: { ...empty.current, running: 3, waiting: 2 },
    });
    const report = await api.getUsage({
      from: "2026-10-13T00:00:00Z",
      to: "2026-10-13T01:00:00Z",
      groupBy: "service",
      timeZone: "UTC",
    });
    usage.mockResolvedValue({
      ...report,
      summary: {
        ...report.summary,
        requests: 1_200_000,
        input_tokens: 2_000_000,
        output_tokens: 1_000_000,
        total_tokens: 3_000_000,
        error_rate: 0.01,
      },
    });
    const { container } = mount();
    await screen.findByRole("heading", { name: service.name });
    for (const name of ["接入信息", "后端健康", "今日用量", "最近异常"])
      expect(screen.getByRole("region", { name })).toBeVisible();
    expect(await screen.findByText("1.2M")).toHaveAttribute(
      "title",
      "1,200,000",
    );
    const health = screen.getByRole("region", { name: "后端健康" });
    expect(within(health).getByText("3")).toBeVisible();
    expect(within(health).getByText("2")).toBeVisible();
    expect(within(health).getByText("vLLM")).toBeVisible();
    expect(screen.getByText("Chat Completions")).toBeVisible();
    expect(screen.getByText("2 人")).toBeVisible();
    expect(usage).toHaveBeenLastCalledWith(
      expect.objectContaining({ serviceId: service.id, groupBy: "service" }),
    );
    const call = errors.mock.calls[0]?.[0];
    expect(call?.serviceId).toBe(service.id);
    expect(
      Date.parse(call?.to ?? "") - Date.parse(call?.from ?? ""),
    ).toBeCloseTo(7 * 86_400_000, -2);
    expect(
      (await axe.run(container)).violations.filter(
        ({ impact }) => impact === "critical" || impact === "serious",
      ),
    ).toEqual([]);
  });

  it("UI-22 hides unauthorized tabs and never queries usage or error groups", async () => {
    const { usage, errors } = setup({ allowed: false });
    await screen.findByRole("heading", { name: service.name });
    expect(
      screen.queryByRole("tab", { name: "调用记录" }),
    ).not.toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: "用量" })).not.toBeInTheDocument();
    expect(screen.getByText("你没有查看用量的权限。")).toBeVisible();
    expect(screen.getByText("你没有查看调用记录的权限。")).toBeVisible();
    expect(usage).not.toHaveBeenCalled();
    expect(errors).not.toHaveBeenCalled();
  });

  it.each([true, false])(
    "UI-22 unavailable banner offers alternatives=%s, no stopped backend load",
    async (alternative) => {
      const { user } = setup({
        alternative,
        item: {
          ...service,
          runtime_status: "Unavailable",
          primary_instance_status: "Stopped",
        },
      });
      await screen.findByRole("heading", { name: service.name });
      expect(screen.getByText("backend_unavailable")).toBeVisible();
      expect(screen.getByRole("link", { name: "启动实例" })).toHaveAttribute(
        "href",
        `/models/instances/${instance.id}`,
      );
      expect(screen.getByText("实例不在运行中，无实时负载。")).toBeVisible();
      if (alternative) {
        await user.click(screen.getByRole("button", { name: "换绑实例" }));
        expect(screen.getByRole("tab", { name: "设置" })).toHaveAttribute(
          "aria-selected",
          "true",
        );
      } else {
        expect(
          screen.queryByRole("button", { name: "换绑实例" }),
        ).not.toBeInTheDocument();
        expect(screen.getByRole("link", { name: "部署模型" })).toHaveAttribute(
          "href",
          "/models/deploy",
        );
        await user.click(screen.getByRole("tab", { name: "设置" }));
        expect(screen.getByText(/暂无其他运行中的实例可切换/)).toBeVisible();
        expect(
          screen.getByLabelText("后端实例").querySelectorAll("option"),
        ).toHaveLength(1);
      }
    },
  );

  it("UI-22 disabled banner enables immediately and starting banner links to instance", async () => {
    const item = {
      ...service,
      enabled: false,
      runtime_status: "Disabled" as const,
    };
    const { api, getService, user } = setup({ item });
    await screen.findByText("service_disabled");
    const changed = {
      ...service,
      runtime_status: "Degraded" as const,
      primary_instance_status: "Starting" as const,
    };
    api.aiServiceUpdateResults.push(changed);
    getService.mockResolvedValue(changed);
    await user.click(
      within(
        screen.getByText("服务已停用").closest("[role=status]") as HTMLElement,
      ).getByRole("button", { name: "启用" }),
    );
    expect(await screen.findByText("服务启动中")).toBeVisible();
    expect(screen.getByRole("link", { name: "前往实例" })).toHaveAttribute(
      "href",
      `/models/instances/${instance.id}`,
    );
    expect(api.aiServiceUpdateCalls).toEqual([
      { id: service.id, body: { enabled: true } },
    ]);
  });

  it.each([
    [true, "Running", false],
    [true, "Starting", false],
    [true, "Stopped", true],
    [true, "Failed", true],
    [false, "Running", true],
  ] as const)(
    "UI-22 header deletion enabled=%s status=%s visible=%s",
    async (enabled, status, visible) => {
      setup({ item: { ...service, enabled, primary_instance_status: status } });
      await screen.findByRole("heading", { name: service.name });
      expect(
        screen.queryByRole("button", { name: `删除 ${service.name}` }) !== null,
      ).toBe(visible);
    },
  );

  it("UI-22 shared confirmation deletes service into retained-instance result", async () => {
    const { api, user } = setup({
      item: { ...service, enabled: false, runtime_status: "Disabled" },
    });
    const deletion = vi
      .spyOn(api, "deleteAiService")
      .mockResolvedValue(undefined);
    await user.click(
      await screen.findByRole("button", { name: `删除 ${service.name}` }),
    );
    expect(
      screen.getByRole("dialog", { name: `删除 AI 服务「${service.name}」？` }),
    ).toBeVisible();
    await user.click(screen.getByRole("button", { name: "删除服务" }));
    expect(
      await screen.findByRole("heading", {
        name: `已删除 AI 服务「${service.name}」`,
      }),
    ).toBeVisible();
    expect(deletion).toHaveBeenCalledWith(service.id);
    expect(screen.getByRole("link", { name: "返回 AI 服务" })).toHaveAttribute(
      "href",
      "/services",
    );
    expect(screen.getByRole("link", { name: "前往模型实例" })).toHaveAttribute(
      "href",
      "/models/instances",
    );
  });

  it.each([false, true])(
    "UI-22 recent error keyboard navigation passes locked service and unknown=%s filter",
    async (unknown) => {
      const { errors, mount, user, records } = setup({ mount: false });
      errors.mockResolvedValue({
        items: [
          {
            ...errorGroup,
            ...(unknown
              ? ({ status: "unknown", error_code: null } as const)
              : {}),
          },
        ],
      });
      mount();
      const button = await screen.findByRole("button", {
        name: unknown ? "状态未知" : "backend_unavailable",
      });
      button.focus();
      await user.keyboard("{Enter}");
      expect(screen.getByRole("tab", { name: "调用记录" })).toHaveAttribute(
        "aria-selected",
        "true",
      );
      expect(
        screen.queryByRole("combobox", { name: "服务" }),
      ).not.toBeInTheDocument();
      await waitFor(() => {
        expect(records).toHaveBeenCalledWith(
          expect.objectContaining({
            serviceId: service.id,
            ...(unknown
              ? { status: "unknown" }
              : { errorCode: "backend_unavailable" }),
          }),
        );
      });
    },
  );

  it("UI-22 error groups retry stays within overview and empty state", async () => {
    const { errors, mount, user } = setup({ mount: false });
    errors.mockRejectedValueOnce(new Error("unavailable"));
    mount();
    await screen.findByText("无法获取最近异常");
    expect(screen.getByRole("region", { name: "接入信息" })).toBeVisible();
    await user.click(
      within(screen.getByRole("region", { name: "最近异常" })).getByRole(
        "button",
        { name: "重试" },
      ),
    );
    expect(await screen.findByText("暂无失败或状态未知的调用")).toBeVisible();
    expect(errors).toHaveBeenCalledTimes(2);
  });

  it("UI-22 loading and failure offer retry without a fake service", async () => {
    const { getService, mount, user } = setup({ mount: false });
    let reject!: (error: Error) => void;
    getService.mockImplementationOnce(
      () =>
        new Promise((_, no) => {
          reject = no;
        }),
    );
    mount();
    expect(await screen.findByLabelText("AI 服务详情正在加载")).toHaveAttribute(
      "aria-busy",
      "true",
    );
    reject(new Error("unavailable"));
    await screen.findByText("无法获取 AI 服务详情");
    await user.click(screen.getByRole("button", { name: "重试" }));
    expect(
      await screen.findByRole("heading", { name: service.name }),
    ).toBeVisible();
  });
});
