import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import axe from "axe-core";
import { act, fireEvent, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiClient, ApiError } from "../api/client";
import {
  diagnosticManifestSchema,
  type DiagnosticPreview,
} from "../api/diagnostic-contracts";
import {
  ADMIN_ME,
  USER_ME,
  FakeApiClient,
  renderApplication,
} from "../app/test-support";

// FX-M7-11B: generated from the backend's safe M7-11A sources; Python asserts equality.
const manifest = diagnosticManifestSchema.parse(
  JSON.parse(
    readFileSync(
      resolve(
        import.meta.dirname,
        "../../../../tests/contract/snapshots/m7_11b_manifest.json",
      ),
      "utf8",
    ),
  ),
);
const receipt = "a".repeat(43);
function preview(): DiagnosticPreview {
  return {
    manifest: structuredClone(manifest),
    receipt,
    expiresAt: Date.now() + 300000,
  };
}
function setup(value = preview()) {
  const api = new FakeApiClient(ADMIN_ME);
  const get = vi.spyOn(api, "previewDiagnostics").mockResolvedValue(value);
  const download = vi
    .spyOn(api, "downloadDiagnostics")
    .mockResolvedValue(new Blob(["safe archive"], { type: "application/zip" }));
  const view = renderApplication(api, "/system?tab=diagnostics");
  return { ...view, api, get, download };
}
async function generate() {
  await userEvent.click(
    await screen.findByRole("button", { name: "预览内容" }),
  );
  await screen.findByRole("heading", { name: "诊断包内容清单" });
}
const createUrl = vi.fn(() => "blob:diagnostic-test");
const revokeUrl = vi.fn();
beforeEach(() => {
  createUrl.mockClear();
  revokeUrl.mockClear();
  vi.stubGlobal(
    "URL",
    Object.assign(URL, {
      createObjectURL: createUrl,
      revokeObjectURL: revokeUrl,
    }),
  );
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(
    () => undefined,
  );
});
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("M7-11B diagnostic preview and confirmation", () => {
  it("uses backend contract, requires explicit confirmation, releases URL, keeps storage clear, and passes axe", async () => {
    const value = preview();
    const health = value.manifest.items.find((item) => item.key === "health");
    if (!health) throw new Error("FX-M7-11B health item missing");
    health.status = "not_included";
    health.count = 0;
    health.bytes = 0;
    health.reason = "sanitized_source_timeout";
    const { container, get, download } = setup(value);
    const storage = vi.spyOn(Storage.prototype, "setItem");
    await generate();
    expect(get).toHaveBeenCalledWith({
      window_minutes: 60,
      end_at: undefined,
      request_ids: [],
      instance_id: null,
    });
    expect(download).not.toHaveBeenCalled();
    expect(screen.getByText("组件健康状态").closest("tr")).toHaveTextContent(
      "未包含",
    );
    expect(screen.getByText("组件健康状态").closest("tr")).toHaveTextContent(
      "来源响应超时，请稍后重新预览",
    );
    expect(screen.getAllByLabelText("暂无数据").length).toBeGreaterThan(0);
    expect((await axe.run(container)).violations).toEqual([]);
    await userEvent.click(screen.getByRole("button", { name: "下载诊断包" }));
    await screen.findByText("诊断包已交给浏览器下载。");
    expect(download).toHaveBeenCalledWith(
      {
        window_minutes: 60,
        end_at: manifest.window.to,
        request_ids: [],
        instance_id: null,
      },
      receipt,
    );
    expect(revokeUrl).toHaveBeenCalledWith("blob:diagnostic-test");
    expect(storage).not.toHaveBeenCalled();
    expect(window.location.href).not.toContain(receipt);
  });
  it("invalidates edited inputs and rejects illegal input without a request", async () => {
    const { get } = setup();
    await generate();
    await userEvent.clear(screen.getByLabelText("时间范围（分钟）"));
    await userEvent.type(screen.getByLabelText("时间范围（分钟）"), "1441");
    expect(screen.getByRole("button", { name: "下载诊断包" })).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "重新预览" }));
    expect(await screen.findByText("请输入 1–1440 分钟")).toBeVisible();
    expect(get).toHaveBeenCalledTimes(1);
  });
  it("ignores a late preview after edits and protects repeated clicks", async () => {
    const { get } = setup();
    let finish!: (value: DiagnosticPreview) => void;
    get.mockReturnValue(
      new Promise((resolve) => {
        finish = resolve;
      }),
    );
    await userEvent.click(
      await screen.findByRole("button", { name: "预览内容" }),
    );
    await userEvent.type(screen.getByLabelText("Request ID（可选）"), "bad");
    expect(screen.getByRole("button", { name: "正在预览" })).toBeDisabled();
    await act(async () => {
      finish(preview());
      await Promise.resolve();
    });
    expect(
      screen.queryByRole("heading", { name: "诊断包内容清单" }),
    ).not.toBeInTheDocument();
    expect(get).toHaveBeenCalledTimes(1);
  });
  it("never downloads after the user switches tabs", async () => {
    const { download } = setup();
    let finish!: (blob: Blob) => void;
    download.mockReturnValue(
      new Promise((resolve) => {
        finish = resolve;
      }),
    );
    await generate();
    await userEvent.click(screen.getByRole("button", { name: "下载诊断包" }));
    await userEvent.click(screen.getByRole("tab", { name: "平台组件" }));
    await act(async () => {
      finish(new Blob(["safe"]));
      await Promise.resolve();
    });
    expect(createUrl).not.toHaveBeenCalled();
  });
  it("preserves inputs and permits retry after failure, expired receipts demand repreview", async () => {
    const { get, download } = setup();
    get.mockRejectedValueOnce(
      new ApiError({
        code: "diagnostics_busy",
        status: 503,
        requestId: "FX-M7-11B-error",
      }),
    );
    await userEvent.click(
      await screen.findByRole("button", { name: "预览内容" }),
    );
    expect(await screen.findByText(/诊断生成容量已满/)).toBeVisible();
    expect(screen.getByLabelText("时间范围（分钟）")).toHaveValue(60);
    await generate();
    download.mockRejectedValue(
      new ApiError({ code: "diagnostic_preview_expired", status: 409 }),
    );
    await userEvent.click(screen.getByRole("button", { name: "下载诊断包" }));
    await screen.findByText("预览已失效，请重新预览后确认下载。");
    expect(screen.getByRole("button", { name: "下载诊断包" })).toBeDisabled();
  });
  it("displays truncation and disables an empty bundle", async () => {
    const value = preview();
    value.manifest.items.forEach((item) => {
      item.count = 0;
      item.truncated = true;
      item.reason = "byte_or_count_limit_reached";
    });
    setup(value);
    await generate();
    expect(
      screen.getByText("当前没有可包含的数据，请调整条件后重新预览。"),
    ).toBeVisible();
    expect(screen.getAllByText(/已截断/).length).toBeGreaterThan(0);
    expect(screen.getByRole("button", { name: "下载诊断包" })).toBeDisabled();
  });
  it("requires repreview when the clock passes expiry before confirmation", async () => {
    const value = preview();
    const { download } = setup(value);
    await generate();
    vi.spyOn(Date, "now").mockReturnValue(value.expiresAt + 1);
    await userEvent.click(screen.getByRole("button", { name: "下载诊断包" }));
    await screen.findByText("预览已过期，请重新预览后确认下载。");
    expect(download).not.toHaveBeenCalled();
  });
  it("does not expose the system route to ordinary users", async () => {
    renderApplication(new FakeApiClient(USER_ME), "/system?tab=diagnostics");
    await waitFor(() =>
      expect(
        screen.queryByRole("tab", { name: "诊断包" }),
      ).not.toBeInTheDocument(),
    );
    expect(
      screen.queryByRole("button", { name: "预览内容" }),
    ).not.toBeInTheDocument();
  });
  it("shows permission failure without rendering raw server text", async () => {
    const { get } = setup();
    get.mockRejectedValue(
      new ApiError({
        code: "permission_denied",
        status: 403,
        responseMessage: "/sensitive/path",
      }),
    );
    await userEvent.click(
      await screen.findByRole("button", { name: "预览内容" }),
    );
    await screen.findByText("没有诊断包权限");
    expect(screen.queryByText(/sensitive/)).not.toBeInTheDocument();
  });
});

describe("M7-11B restricted Blob transport", () => {
  beforeEach(() => {
    vi.spyOn(document, "cookie", "get").mockReturnValue(
      "__Host-pws_csrf=FX-M7-11B-csrf",
    );
  });
  const input = { window_minutes: 60, request_ids: [], instance_id: null };
  it("shares CSRF and authentication recovery and never treats JSON errors as ZIP", async () => {
    const api = new ApiClient();
    const recovered = vi.fn();
    api.setAuthenticationFailureHandler(recovered);
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(
        JSON.stringify({
          error: {
            code: "authentication_required",
            message: "请登录",
            details: {},
          },
        }),
        { status: 401, headers: { "content-type": "application/json" } },
      ),
    );
    vi.stubGlobal("fetch", fetcher);
    await expect(api.downloadDiagnostics(input, receipt)).rejects.toMatchObject(
      { status: 401 },
    );
    expect(recovered).toHaveBeenCalledOnce();
    expect(fetcher.mock.calls[0]?.[0]).toBe("/api/v1/diagnostics");
    expect(
      new Headers(fetcher.mock.calls[0]?.[1]?.headers).get("X-CSRF-Token"),
    ).toBe("FX-M7-11B-csrf");
  });
  it.each([
    ["application/json", "{}"],
    ["application/zip", "not a ZIP"],
  ])("rejects invalid successful response %s", async (mime, body) => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValue(
          new Response(body, { headers: { "content-type": mime } }),
        ),
    );
    await expect(
      new ApiClient().downloadDiagnostics(input, receipt),
    ).rejects.toMatchObject({ code: "invalid_diagnostic_response" });
  });
  it("accepts ZIP bytes and rejects malformed preview metadata without leaking text", async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(
        new Response(new Uint8Array([80, 75, 3, 4, 1]), {
          headers: { "content-type": "application/zip" },
        }),
      )
      .mockResolvedValueOnce(
        new Response("{}", {
          headers: {
            "x-diagnostic-preview": receipt,
            "x-diagnostic-preview-ttl": "300",
            "x-request-id": "FX-M7-11B-contract",
          },
        }),
      );
    vi.stubGlobal("fetch", fetcher);
    const api = new ApiClient();
    expect((await api.downloadDiagnostics(input, receipt)).type).toBe(
      "application/zip",
    );
    await expect(api.previewDiagnostics(input)).rejects.toMatchObject({
      code: "invalid_diagnostic_response",
      requestId: "FX-M7-11B-contract",
    });
    expect(fetcher.mock.calls[1]?.[0]).toBe(
      "/api/v1/diagnostics?window_minutes=60",
    );
  });
  it("rejects oversized responses before consuming them", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response("PK", {
          headers: {
            "content-type": "application/zip",
            "content-length": "999999999",
          },
        }),
      ),
    );
    await expect(
      new ApiClient().downloadDiagnostics(input, receipt),
    ).rejects.toMatchObject({ code: "invalid_diagnostic_response" });
  });
});

// FX-SYS-05-DIAGNOSTICS: the existing backend manifest, varied only by Request ID selection.
describe("SYS_05 TC_HEALTH_31 diagnostic conditions and seven-item preview", () => {
  it.each(["时间范围（分钟）", "结束时间", "Request ID（可选）"])(
    "keeps the preview but disables download after changing %s",
    async (label) => {
      setup();
      await generate();
      expect(screen.queryByLabelText(/模型实例/)).not.toBeInTheDocument();
      expect(
        screen
          .getByRole("table", { name: "诊断包内容清单" })
          .querySelectorAll("tbody tr"),
      ).toHaveLength(7);
      expect(screen.getByText("相关日志行")).toBeVisible();
      const field = screen.getByLabelText(label);
      if (label === "结束时间") {
        fireEvent.change(field, { target: { value: "2026-10-17T12:00" } });
      } else {
        await userEvent.type(
          field,
          label === "时间范围（分钟）"
            ? "1"
            : "00000000-0000-4000-8000-000000000001",
        );
      }
      expect(screen.getByRole("button", { name: "下载诊断包" })).toBeDisabled();
      expect(screen.getByText("条件已修改，请重新预览。")).toBeVisible();
      expect(
        screen
          .getByRole("table", { name: "诊断包内容清单" })
          .querySelectorAll("tbody tr"),
      ).toHaveLength(7);
    },
  );
  it.each([false, true])(
    "shows backend inclusion with request IDs=%s and always sends instance_id null",
    async (selected) => {
      const ids = [
        "00000000-0000-4000-8000-000000000001",
        "00000000-0000-4000-8000-000000000002",
      ];
      const value = preview();
      value.manifest.request_ids = selected ? ids : [];
      for (const item of value.manifest.items) {
        if (item.key === "request_records" || item.key === "sanitized_logs") {
          item.status = selected ? "included" : "not_included";
          item.count = selected ? 2 : 0;
          item.bytes = selected ? 512 : 0;
          item.reason = null;
        }
      }
      const { get } = setup(value);
      if (selected)
        await userEvent.type(
          await screen.findByLabelText("Request ID（可选）"),
          ids.join(", "),
        );
      await generate();
      expect(get).toHaveBeenCalledWith({
        window_minutes: 60,
        end_at: undefined,
        request_ids: selected ? ids : [],
        instance_id: null,
      });
      for (const label of ["调用记录元数据", "相关日志行"]) {
        const row = screen.getByText(label).closest("tr");
        if (!row) throw new Error("FX-SYS-05 missing manifest row");
        expect(row).toHaveTextContent(selected ? "已包含" : "未包含");
        expect(row.querySelector('[data-column="count"]')).toHaveTextContent(
          selected ? /^2$/ : /—/,
        );
        expect(row.querySelector('[data-column="bytes"]')).toHaveTextContent(
          selected ? "512 B" : /—/,
        );
        if (!selected) expect(row).toHaveTextContent("未填写 Request ID");
      }
    },
  );
  it("disables the visible download after five minutes even without another interaction", async () => {
    setup();
    // Switch before preview creation so the expiry timer belongs to the fake clock.
    await screen.findByRole("button", { name: "预览内容" });
    vi.useFakeTimers();
    try {
      fireEvent.click(screen.getByRole("button", { name: "预览内容" }));
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1);
      });
      expect(screen.getByRole("button", { name: "下载诊断包" })).toBeEnabled();
      await act(async () => {
        await vi.advanceTimersByTimeAsync(300001);
      });
      expect(screen.getByRole("button", { name: "下载诊断包" })).toBeDisabled();
      expect(
        screen.getByText("预览已过期，请重新预览后确认下载。"),
      ).toBeVisible();
    } finally {
      vi.useRealTimers();
    }
  });
});
