import axe from "axe-core";
import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { ApiError } from "../api/client";
import type { ListedApiKey } from "../api/api-key-contracts";
import {
  FakeApiClient,
  renderApplication,
  USER_ME,
  UI_46_PERSONAL_SERVICE,
} from "../app/test-support";
import { MY_AI_SERVICES_QUERY_KEY } from "../users/queries";

// FX-UI-46: authorized, unavailable and disabled service metadata; no real secrets.
const first = UI_46_PERSONAL_SERVICE;
const second = {
  ...first,
  id: "45000000-0000-4000-8000-000000000002",
  name: "翻译",
  public_model_name: "translate",
  available: false,
  enabled: false,
};
const key: ListedApiKey = {
  id: "46000000-0000-4000-8000-000000000001",
  name: "开发环境",
  status: "Active",
  display_prefix: "holm_sk_",
  last4: "1234",
  created_at: "2026-10-01T00:00:00Z",
  expires_at: null,
  revoked_at: null,
  last_used_at: null,
  service_ids: [],
  effective_services: [],
};
function setup(
  items = [first, second],
  keys: ListedApiKey[] = [],
  path = `/services/${first.id}`,
) {
  const api = new FakeApiClient(USER_ME);
  vi.spyOn(api, "getMyAiServices").mockResolvedValue({ items });
  vi.spyOn(api, "getApiKeys").mockResolvedValue({ items: keys });
  const view = renderApplication(api, path);
  return { api, ...view, user: userEvent.setup() };
}
afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
});

it("UI_46 TC-UI-26 keeps API order, two states, usage and access links on compact cards", async () => {
  setup([second, first], [], "/services");
  await screen.findByRole("heading", { name: "翻译" });
  const cards = screen.getAllByRole("article");
  const [disabledCard, availableCard] = cards;
  if (disabledCard === undefined || availableCard === undefined)
    throw new Error("FX-UI-46 cards missing");
  expect(
    cards.map((card) => within(card).getByRole("heading").textContent),
  ).toEqual(["翻译", "研发助手"]);
  expect(within(disabledCard).getByText("不可用")).toBeVisible();
  expect(within(availableCard).getByText("可用")).toBeVisible();
  expect(within(disabledCard).getByText("尚未使用")).toBeVisible();
  expect(
    within(disabledCard).getByRole("link", { name: "查看接入方式" }),
  ).toHaveAttribute("href", `/services/${second.id}`);
  expect(document.body).not.toHaveTextContent(
    /base_url|HOLM_API_KEY|当前繁忙|Runtime|GPU/,
  );
});

it("UI_46 TC-UI-26 empty grants disable creation and explain why", async () => {
  const view = setup([], [], "/services");
  expect(await screen.findByText("管理员尚未为你分配 AI 服务")).toBeVisible();
  view.unmount();
  const { api } = setup([], [], "/api-keys");
  expect(
    await screen.findByRole("button", { name: "创建密钥" }),
  ).toBeDisabled();
  expect(await screen.findByText("管理员尚未为你分配 AI 服务")).toBeVisible();
  expect(api.apiKeyCreateCalls).toEqual([]);
});

it.each([0, 1, 2])(
  "UI_46 TC-UI-26 detail supports %i applicable keys and excludes expired, revoked and other scopes",
  async (count) => {
    const applicable = Array.from({ length: count }, (_, index) => ({
      ...key,
      id: `46000000-0000-4000-8000-00000000000${String(index + 1)}`,
      name: `密钥${String(index + 1)}`,
      service_ids: index === 0 ? [] : [first.id],
    }));
    const { user, api } = setup(
      [first],
      [
        ...applicable,
        {
          ...key,
          id: "46000000-0000-4000-8000-000000000003",
          name: "已撤销",
          status: "Revoked",
        },
        {
          ...key,
          id: "46000000-0000-4000-8000-000000000004",
          name: "已过期",
          expires_at: "2020-01-01T00:00:00Z",
        },
        {
          ...key,
          id: "46000000-0000-4000-8000-000000000005",
          name: "其他范围",
          service_ids: [second.id],
        },
      ],
    );
    await screen.findByRole("heading", { name: first.name });
    for (const label of [
      "Endpoint",
      "Model",
      "API Key",
      "Status",
      "最近调用成功",
    ])
      expect(screen.getByText(label)).toBeVisible();
    expect(screen.getByText("还没有成功的调用")).toBeVisible();
    if (count === 0) {
      expect(await screen.findByText("尚未创建")).toBeVisible();
      expect(
        screen.getByRole("button", { name: "创建 API 密钥" }),
      ).toBeVisible();
    } else {
      expect(await screen.findByText("密钥1 · holm_sk_…1234")).toBeVisible();
      expect(
        screen.queryByRole("button", { name: "创建 API 密钥" }),
      ).not.toBeInTheDocument();
    }
    if (count === 2) {
      await user.selectOptions(
        screen.getByRole("combobox", { name: "切换 API 密钥" }),
        "46000000-0000-4000-8000-000000000002",
      );
      expect(screen.getByText("密钥2 · holm_sk_…1234")).toBeVisible();
    } else
      expect(
        screen.queryByRole("combobox", { name: "切换 API 密钥" }),
      ).not.toBeInTheDocument();
    expect(api.aiServiceCalls).toBe(0);
    expect(api.modelInstanceCalls).toBe(0);
    expect(document.body).not.toHaveTextContent(/已撤销 ·|已过期 ·|其他范围 ·/);
  },
);

it.each([true, false])(
  "UI_46 TC-UI-26 unavailable enabled=%s keeps information and the appropriate banner",
  async (enabled) => {
    setup([{ ...first, available: false, enabled }]);
    expect(
      await screen.findByText(
        enabled
          ? /该服务暂时不可用。配置无需修改/
          : /该服务已被管理员停用，暂时无法调用/,
      ),
    ).toBeVisible();
    expect(screen.getByText(`${window.location.origin}/v1`)).toBeVisible();
    expect(screen.getByText("不可用")).toBeVisible();
  },
);

it("UI_48 UI_46 TC-UI-26 seven example tabs, real fields, four FAQs, copy feedback and axe", async () => {
  const { user } = setup();
  await screen.findByRole("heading", { name: first.name });
  expect(screen.getAllByRole("tab").map((tab) => tab.textContent)).toEqual([
    "OpenAI SDK",
    "Python",
    "curl",
    "Claude Code",
    "Cursor",
    "Cline",
    "Continue",
  ]);
  const clipboard = vi
    .spyOn(navigator.clipboard, "writeText")
    .mockResolvedValue();
  for (const label of [
    "OpenAI SDK",
    "Python",
    "curl",
    "Claude Code",
    "Cursor",
    "Cline",
    "Continue",
  ]) {
    await user.click(screen.getByRole("tab", { name: label }));
    const panel = screen.getByRole("tabpanel");
    expect(panel).toHaveTextContent(first.public_model_name);
    if (label === "Claude Code") {
      expect(panel.querySelector("pre")).toHaveTextContent(
        `ANTHROPIC_BASE_URL=${window.location.origin}`,
      );
      expect(panel.querySelector("pre")).not.toHaveTextContent("/v1");
      expect(panel).toHaveTextContent("$HOLM_API_KEY");
      expect(panel).toHaveTextContent("以工具当前版本为准");
    } else {
      expect(panel).toHaveTextContent(`${window.location.origin}/v1`);
    }
    expect(panel).toHaveTextContent(
      label === "Cursor" || label === "Cline" || label === "Continue"
        ? "以工具当前版本为准"
        : "HOLM_API_KEY",
    );
    await user.click(within(panel).getByRole("button", { name: "复制代码" }));
    expect(clipboard).toHaveBeenLastCalledWith(
      panel.querySelector("pre")?.textContent,
    );
  }
  expect(
    document.querySelectorAll(".pw-personal-access-faq details"),
  ).toHaveLength(4);
  const result = await axe.run(document, {
    rules: { "color-contrast": { enabled: false } },
  });
  expect(result.violations).toEqual([]);
});

it("UI_46 TC-UI-26 detail creation selects the current disabled service and tests only it", async () => {
  const { api, user } = setup([first, second], [], `/services/${second.id}`);
  api.apiKeyCreateResults.push({
    ...key,
    service_ids: [second.id],
    api_key: "holm_sk_UI46_fixture_only",
  });
  const fetcher = vi.spyOn(globalThis, "fetch").mockResolvedValue(
    new Response(JSON.stringify({ data: [{ id: first.public_model_name }] }), {
      status: 200,
      headers: {
        "Content-Type": "application/json",
        "x-request-id": "UI46-route",
      },
    }),
  );
  await user.click(
    await screen.findByRole("button", { name: "创建 API 密钥" }),
  );
  const form = screen.getByRole("dialog");
  expect(within(form).getByRole("radio", { name: "指定服务" })).toBeChecked();
  expect(within(form).getByRole("checkbox", { name: /翻译/ })).toBeChecked();
  await user.type(within(form).getByLabelText(/^名称/), "翻译调用");
  await user.click(within(form).getByRole("button", { name: "创建密钥" }));
  await screen.findByRole("dialog", { name: "保存 API 密钥" });
  expect(api.apiKeyCreateCalls[0]?.service_ids).toEqual([second.id]);
  expect(
    screen.queryByRole("combobox", { name: "测试的服务" }),
  ).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "测试调用" }));
  expect(await screen.findByText("服务路由未通过")).toBeVisible();
  expect(fetcher).toHaveBeenCalledTimes(1);
});

it("UI_46 TC-UI-19 TC-UI-26 key-page creation selects all, validates empty scope, switches test target and preserves copy until close", async () => {
  const { api, user, queryClient } = setup([first, second], [], "/api-keys");
  const secret = "holm_sk_UI46_one_time_fixture";
  api.apiKeyCreateResults.push({ ...key, api_key: secret });
  await user.click(await screen.findByRole("button", { name: "创建密钥" }));
  const dialog = screen.getByRole("dialog");
  expect(
    within(dialog).getByRole("radio", {
      name: "全部已授权服务（含以后新授权的）",
    }),
  ).toBeChecked();
  await user.type(within(dialog).getByLabelText(/^名称/), "全部服务");
  await user.click(within(dialog).getByRole("radio", { name: "指定服务" }));
  await user.click(within(dialog).getByRole("button", { name: "创建密钥" }));
  expect(await screen.findByText("至少勾选一个服务。")).toBeVisible();
  expect(api.apiKeyCreateCalls).toHaveLength(0);
  await user.click(
    within(dialog).getByRole("radio", {
      name: "全部已授权服务（含以后新授权的）",
    }),
  );
  await user.click(within(dialog).getByRole("button", { name: "创建密钥" }));
  await screen.findByRole("dialog", { name: "保存 API 密钥" });
  expect(api.apiKeyCreateCalls[0]?.service_ids).toEqual([]);
  const select = screen.getByRole("combobox", { name: "测试的服务" });
  expect(select).toHaveValue(first.id);
  await user.selectOptions(select, second.id);
  const response = (data: unknown) =>
    new Response(JSON.stringify(data), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  const fetcher = vi
    .spyOn(globalThis, "fetch")
    .mockResolvedValueOnce(
      response({
        data: [
          { id: first.public_model_name },
          { id: second.public_model_name },
        ],
      }),
    )
    .mockResolvedValueOnce(response({ usage: { completion_tokens: 1 } }));
  await user.click(screen.getByRole("button", { name: "测试调用" }));
  expect(await screen.findByText("测试调用成功")).toBeVisible();
  expect(JSON.parse(fetcher.mock.calls[1]?.[1]?.body as string)).toMatchObject({
    model: second.public_model_name,
    max_tokens: 8,
  });
  expect(
    JSON.stringify(
      queryClient
        .getQueryCache()
        .getAll()
        .map(({ state }) => state.data),
    ),
  ).not.toContain(secret);
  await user.click(screen.getByRole("button", { name: "复制 API 密钥" }));
  vi.useFakeTimers();
  await act(() => vi.advanceTimersByTimeAsync(2200));
  expect(screen.getByRole("button", { name: "已复制" })).toBeVisible();
  vi.useRealTimers();
  await user.click(screen.getByRole("button", { name: "关闭" }));
  await user.click(screen.getByRole("button", { name: "返回复制" }));
  expect(screen.getByRole("button", { name: "已复制" })).toBeVisible();
  await user.click(screen.getByRole("button", { name: "我已保存此密钥" }));
  expect(screen.queryByDisplayValue(secret)).not.toBeInTheDocument();
  expect(
    screen.queryByRole("button", { name: "测试调用" }),
  ).not.toBeInTheDocument();
});

it("UI_46 TC-UI-26 handles missing permission, load failure and stale data with retry", async () => {
  const view = setup([], [], `/services/${first.id}`);
  expect(await screen.findByText("无权限")).toBeVisible();
  view.unmount();
  const { api, queryClient, user } = setup();
  await screen.findByRole("heading", { name: first.name });
  vi.spyOn(api, "getMyAiServices").mockRejectedValueOnce(
    new ApiError({ code: "forbidden", status: 403 }),
  );
  await act(() =>
    queryClient.invalidateQueries({ queryKey: MY_AI_SERVICES_QUERY_KEY }),
  );
  expect(await screen.findByText("AI 服务数据可能过期")).toBeVisible();
  await user.click(screen.getByRole("button", { name: "重试" }));
  await waitFor(() =>
    expect(screen.queryByText("AI 服务数据可能过期")).not.toBeInTheDocument(),
  );
});

it("UI_46 TC-UI-26 shows the current user's successful test immediately while activity refresh is pending", async () => {
  const { api, user } = setup([first]);
  api.apiKeyCreateResults.push({
    ...key,
    service_ids: [first.id],
    api_key: "holm_sk_UI46_success_fixture",
  });
  const response = (data: unknown) =>
    new Response(JSON.stringify(data), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  vi.spyOn(globalThis, "fetch")
    .mockResolvedValueOnce(
      response({ data: [{ id: first.public_model_name }] }),
    )
    .mockResolvedValueOnce(response({ usage: { completion_tokens: 1 } }));
  await user.click(
    await screen.findByRole("button", { name: "创建 API 密钥" }),
  );
  const form = screen.getByRole("dialog");
  await user.type(within(form).getByLabelText(/^名称/), "首次成功");
  await user.click(within(form).getByRole("button", { name: "创建密钥" }));
  await screen.findByRole("dialog", { name: "保存 API 密钥" });
  await user.click(screen.getByRole("button", { name: "测试调用" }));
  await screen.findByText("测试调用成功");
  await user.click(screen.getByRole("button", { name: "我已保存此密钥" }));
  expect(screen.getByText("刚刚（测试调用）")).toBeVisible();
});
