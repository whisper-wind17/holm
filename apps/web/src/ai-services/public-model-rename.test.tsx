import axe from "axe-core";
import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import {
  type AiService,
  renameAiServiceRequestSchema,
} from "../api/ai-service-contracts";
import { ApiClient, ApiError } from "../api/client";
import {
  ADMIN_ME,
  USER_ME,
  FakeApiClient,
  renderApplication,
} from "../app/test-support";

// FX-M4-11A: same metadata shape as PROTO-07; no real model/runtime or credentials.
const service: AiService = {
  id: "0198f58a-2586-7e37-bbf5-000000000001",
  primary_instance_id: "0198f58a-2586-7e37-bbf5-000000000002",
  primary_instance_status: "Stopped",
  name: "研发助手",
  public_model_name: "old-model",
  description: "已有描述",
  enabled: false,
  runtime_status: "Disabled",
  route_version: 1,
};
const changed: AiService = {
  ...service,
  name: "研发助手",
  public_model_name: "new-model",
  route_version: 2,
};

async function setup() {
  const api = new FakeApiClient(ADMIN_ME);
  api.aiServiceDetailResults.push(service);
  const rename = vi.spyOn(api, "renameAiService").mockResolvedValue(changed);
  const rendered = renderApplication(api, `/services/${service.id}`);
  const user = userEvent.setup();
  await screen.findByRole("heading", { name: "研发助手" });
  const trigger = screen.getByRole("button", { name: "修改 API 模型名" });
  await waitFor(() => expect(trigger).toBeEnabled());
  return { api, rename, user, trigger, ...rendered };
}

async function fill(
  user: ReturnType<typeof userEvent.setup>,
  name = "new-model",
) {
  const dialog = screen.getByRole("dialog");
  await user.type(within(dialog).getByLabelText(/^新 API 模型名/), name);
  await user.type(
    within(dialog).getByLabelText(/^再次输入新 API 模型名/),
    name,
  );
  return dialog;
}

describe("M4-11A public model rename", () => {
  it("M4_11A_TC_UI_01_03 confirms explicitly, keeps settings drafts and updates detail and list cache", async () => {
    const { user, rename, queryClient } = await setup();
    await user.click(screen.getByRole("tab", { name: "设置" }));
    await user.clear(screen.getByLabelText(/^描述/));
    await user.type(screen.getByLabelText(/^描述/), "尚未提交的描述");
    await user.click(screen.getByRole("tab", { name: "概览" }));
    queryClient.setQueryData(["ai-services"], { items: [service] });
    await user.click(screen.getByRole("button", { name: "修改 API 模型名" }));
    const dialog = screen.getByRole("dialog");
    expect(
      within(dialog).getByRole("heading", { name: "修改 API 模型名" }),
    ).toHaveFocus();
    expect(
      within(dialog).getByRole("button", { name: "确认修改" }),
    ).toBeDisabled();
    await user.type(
      within(dialog).getByLabelText(/^新 API 模型名/),
      "new-model",
    );
    expect(
      within(dialog).getByRole("button", { name: "确认修改" }),
    ).toBeDisabled();
    await user.type(
      within(dialog).getByLabelText(/^再次输入新 API 模型名/),
      "new-model",
    );
    const results = await axe.run(dialog);
    expect(
      results.violations.filter(
        ({ impact }) => impact === "critical" || impact === "serious",
      ),
    ).toEqual([]);
    await user.click(within(dialog).getByRole("button", { name: "确认修改" }));
    await waitFor(() => {
      expect(screen.getAllByText("new-model").length).toBeGreaterThan(0);
    });
    expect(screen.getByRole("heading", { name: "研发助手" })).toBeVisible();
    expect(rename).toHaveBeenCalledExactlyOnceWith(service.id, {
      public_model_name: "new-model",
      confirm_public_model_name: "new-model",
      expected_route_version: 1,
    });

    expect(queryClient.getQueryData(["ai-services", service.id])).toEqual(
      changed,
    );
    expect(queryClient.getQueryState(["ai-services"])?.isInvalidated).toBe(
      true,
    );
    expect(
      screen.getByRole("button", { name: "修改 API 模型名" }),
    ).toHaveFocus();
    await user.click(screen.getByRole("tab", { name: "设置" }));
    expect(screen.getByLabelText(/^描述/)).toHaveValue("尚未提交的描述");
  });

  it("M4_11A_TC_UI_03 cancels with Escape, restores focus and reopens with current version", async () => {
    const { user, trigger, rename, queryClient } = await setup();
    await user.click(trigger);
    await fill(user);
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
    expect(rename).not.toHaveBeenCalled();
    act(() => {
      queryClient.setQueryData(["ai-services", service.id], changed);
    });
    await user.click(trigger);
    expect(
      within(screen.getByRole("dialog")).getByText("new-model"),
    ).toBeVisible();
    expect(screen.getByLabelText(/^新 API 模型名/)).toHaveValue("");
    await fill(user, "third-model");
    await user.click(screen.getByRole("button", { name: "确认修改" }));
    expect(rename).toHaveBeenCalledWith(
      service.id,
      expect.objectContaining({ expected_route_version: 2 }),
    );
  });

  it("M4_11A retains failed inputs and Request ID, and prevents duplicate submissions", async () => {
    const { user, trigger, rename } = await setup();
    let reject: (error: Error) => void = () => undefined;
    rename.mockImplementationOnce(
      () =>
        new Promise((_resolve, rejectPromise) => {
          reject = rejectPromise;
        }),
    );
    await user.click(trigger);
    await fill(user);
    await user.dblClick(screen.getByRole("button", { name: "确认修改" }));
    expect(rename).toHaveBeenCalledTimes(1);
    expect(screen.getByLabelText(/^新 API 模型名/)).toBeDisabled();
    act(() => {
      reject(
        new ApiError({
          status: 409,
          code: "service_name_conflict",
          requestId: "rename-request-id",
          responseMessage: "API 模型名已被使用，请更换名称",
        }),
      );
    });
    expect(await screen.findByText(/rename-request-id/)).toBeVisible();
    expect(screen.getByLabelText(/^新 API 模型名/)).toHaveValue("new-model");
    expect(screen.getByLabelText(/^再次输入新 API 模型名/)).toHaveValue(
      "new-model",
    );
    await user.click(screen.getByRole("button", { name: "取消" }));
    expect(rename).toHaveBeenCalledTimes(1);
  });

  it("M4_11A_TC_UI_13 requires explicit refresh and fresh confirmation after version conflict", async () => {
    const { user, trigger, rename, api } = await setup();
    rename.mockRejectedValueOnce(
      new ApiError({ status: 409, code: "version_conflict" }),
    );
    await user.click(trigger);
    await fill(user);
    await user.click(screen.getByRole("button", { name: "确认修改" }));
    await screen.findByText("配置已更新");
    expect(screen.getByRole("button", { name: "确认修改" })).toBeDisabled();
    api.aiServiceDetailResults.push(
      new ApiError({
        status: 503,
        code: "request_failed",
        requestId: "refresh-request-id",
      }),
    );
    await user.click(screen.getByRole("button", { name: "刷新当前配置" }));
    expect(await screen.findByText(/refresh-request-id/)).toBeVisible();
    expect(screen.getByLabelText(/^新 API 模型名/)).toHaveValue("new-model");
    expect(screen.getByRole("button", { name: "确认修改" })).toBeDisabled();
    expect(rename).toHaveBeenCalledTimes(1);
    api.aiServiceDetailResults.push({
      ...service,
      public_model_name: "other-admin-name",
      route_version: 5,
    });
    await user.click(screen.getByRole("button", { name: "刷新当前配置" }));
    expect(
      await screen.findByText("other-admin-name", { selector: "bdi" }),
    ).toBeVisible();
    expect(screen.getByLabelText(/^新 API 模型名/)).toHaveValue("new-model");
    expect(screen.getByLabelText(/^再次输入新 API 模型名/)).toHaveValue("");
    expect(rename).toHaveBeenCalledTimes(1);
    await user.type(
      screen.getByLabelText(/^再次输入新 API 模型名/),
      "new-model",
    );
    await user.click(screen.getByRole("button", { name: "确认修改" }));
    expect(rename).toHaveBeenLastCalledWith(
      service.id,
      expect.objectContaining({ expected_route_version: 5 }),
    );
  });

  it("M4_11A blocks background route changes without replacing drafts", async () => {
    const { user, trigger, rename, queryClient } = await setup();
    await user.click(trigger);
    await fill(user);
    act(() => {
      queryClient.setQueryData(["ai-services", service.id], changed);
    });
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "确认修改" })).toBeDisabled(),
    );
    expect(screen.getByLabelText(/^新 API 模型名/)).toHaveValue("new-model");
    expect(rename).not.toHaveBeenCalled();
  });

  it("M4_11A validates invalid names and never invokes the API", async () => {
    const { user, trigger, rename } = await setup();
    await user.click(trigger);
    await fill(user, "UPPER");
    await user.click(screen.getByRole("button", { name: "确认修改" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("2～64");
    await waitFor(() =>
      expect(screen.getByLabelText(/^新 API 模型名/)).toHaveFocus(),
    );
    expect(rename).not.toHaveBeenCalled();
    for (const name of ["x", "a/b", "a".repeat(65), "name ", "name\n"]) {
      expect(
        renameAiServiceRequestSchema.safeParse({
          public_model_name: name,
          confirm_public_model_name: name,
          expected_route_version: 1,
        }).success,
      ).toBe(false);
    }
  });

  it("M4_11A_TC_SEC_11 exposes no rename control to ordinary users", async () => {
    const api = new FakeApiClient(USER_ME);
    renderApplication(api, "/services");
    await screen.findByRole("heading", { name: "AI 服务" });
    expect(
      screen.queryByRole("button", { name: "修改 API 模型名" }),
    ).not.toBeInTheDocument();
  });

  it("M4_11A keeps the rename entry unavailable when service loading fails", async () => {
    const api = new FakeApiClient(ADMIN_ME);
    let reject: (error: Error) => void = () => undefined;
    vi.spyOn(api, "getAiService").mockImplementation(
      () =>
        new Promise((_resolve, rejectPromise) => {
          reject = rejectPromise;
        }),
    );
    renderApplication(api, `/services/${service.id}`);
    await screen.findByLabelText("AI 服务详情正在加载");
    expect(
      screen.queryByRole("button", { name: "修改 API 模型名" }),
    ).not.toBeInTheDocument();
    act(() => {
      reject(new ApiError({ status: 503, code: "request_failed" }));
    });
    await screen.findByText("无法获取 AI 服务详情");
    expect(
      screen.queryByRole("button", { name: "修改 API 模型名" }),
    ).not.toBeInTheDocument();
  });

  it("M4_11A sends the isolated rename action with CSRF and parses the existing response", async () => {
    const fetchCall = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify(changed), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    const api = new ApiClient();
    vi.spyOn(document, "cookie", "get").mockReturnValue(
      "__Host-pws_csrf=csrf-fixture",
    );
    await expect(
      api.renameAiService(service.id, {
        public_model_name: "new-model",
        confirm_public_model_name: "new-model",
        expected_route_version: 1,
      }),
    ).resolves.toEqual(changed);
    expect(fetchCall).toHaveBeenCalledWith(
      expect.stringContaining(`${service.id}:rename`),
      expect.objectContaining({ method: "POST" }),
    );
    const headers = fetchCall.mock.calls[0]?.[1]?.headers as Headers;
    expect(headers).toBeInstanceOf(Headers);
    expect(headers.get("X-CSRF-Token")).toBe("csrf-fixture");
  });
});
