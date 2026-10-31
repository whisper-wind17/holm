import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiClient, ApiError, readCsrfCookie } from "./client";

afterEach(() => {
  document.cookie = "__Host-pws_csrf=; Max-Age=0; Path=/";
});

describe("TC-SEC-14 Control API browser client", () => {
  it("reads the delivery Cookie and echoes it only in X-CSRF-Token", async () => {
    vi.spyOn(document, "cookie", "get").mockReturnValue(
      "__Host-pws_csrf=csrf%20sentinel",
    );
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(
        new Response(JSON.stringify({ status: "logged_out" })),
      );

    await new ApiClient().logout();

    const [, request] = fetchMock.mock.calls[0] ?? [];
    const headers = new Headers(request?.headers);
    expect(headers.get("X-CSRF-Token")).toBe("csrf sentinel");
    expect(headers.get("Cookie")).toBeNull();
    expect(request?.credentials).toBe("same-origin");
  });

  it("fails loudly before a write when the CSRF delivery Cookie is absent", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch");
    await expect(new ApiClient().logout()).rejects.toMatchObject({
      code: "csrf_token_unavailable",
    });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("decodes the named Cookie while ignoring every other Cookie", () => {
    expect(readCsrfCookie("a=1; __Host-pws_csrf=token%2Bvalue; z=3")).toBe(
      "token+value",
    );
  });
});

describe("M2_12B model-acquisition client paths", () => {
  it("encodes every local-model filter and the opaque cursor under server names", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          items: [],
          next_cursor: null,
          summary: {
            active_download_tasks: 0,
            managed_bytes: 0,
            storage_available_bytes: null,
          },
        }),
      ),
    );
    await new ApiClient().getModelArtifacts({
      cursor: "opaque+/cursor==",
      search: "Qwen 3",
      source: "ModelScope",
      status: "Ready",
      storageType: "Managed",
    });
    const [path] = fetchMock.mock.calls[0] ?? [];
    expect(path).toBe(
      "/api/v1/artifacts?cursor=opaque%2B%2Fcursor%3D%3D&search=Qwen+3&source=ModelScope&status=Ready&storage_type=Managed",
    );
  });

  it("uses the shipped slash command path and preserves the server message", async () => {
    vi.spyOn(document, "cookie", "get").mockReturnValue(
      "__Host-pws_csrf=model-command-csrf",
    );
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          attempt: 1,
          command: "pause",
          id: "0198f58a-2586-7e37-bbf5-fefe165e8082",
          staging_cleanup_requested: false,
          status: "Paused",
        }),
      ),
    );
    await new ApiClient().executeDownloadCommand(
      "0198f58a-2586-7e37-bbf5-fefe165e8082",
      "pause",
    );
    const [path, request] = fetchMock.mock.calls[0] ?? [];
    expect(path).toBe(
      "/api/v1/downloads/0198f58a-2586-7e37-bbf5-fefe165e8082/pause",
    );
    expect(request?.method).toBe("POST");
    expect(new Headers(request?.headers).get("X-CSRF-Token")).toBe(
      "model-command-csrf",
    );
  });

  it("retains an error response message and Request ID for the row", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          error: {
            code: "download_command_not_allowed",
            details: {},
            message:
              "当前状态不允许执行此操作，下载任务未改变，请刷新状态后重试",
            request_id: "request-m2-12b",
          },
        }),
        { status: 409 },
      ),
    );
    await expect(new ApiClient().getDownloadTasks()).rejects.toMatchObject({
      requestId: "request-m2-12b",
      responseMessage:
        "当前状态不允许执行此操作，下载任务未改变，请刷新状态后重试",
    });
  });
});

describe("PROTO_12 Request Record client", () => {
  it("PROTO_12 encodes every UI filter and preserves the opaque cursor", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(
        new Response(JSON.stringify({ items: [], next_cursor: null })),
      );
    await new ApiClient().getRequestRecords({
      cursor: "opaque+/cursor==",
      requestId: "0198f58a-2586-7e37-bbf5-fefe165e8080",
      serviceId: "0198f58a-2586-7e37-bbf5-fefe165e8081",
      status: "failed",
      userId: "0198f58a-2586-7e37-bbf5-fefe165e8082",
    });
    const [path] = fetchMock.mock.calls[0] ?? [];
    expect(path).toBe(
      "/api/v1/requests?cursor=opaque%2B%2Fcursor%3D%3D&request_id=0198f58a-2586-7e37-bbf5-fefe165e8080&service_id=0198f58a-2586-7e37-bbf5-fefe165e8081&status=failed&user_id=0198f58a-2586-7e37-bbf5-fefe165e8082",
    );
  });
});

describe("TC-UI-01 401 recovery boundary", () => {
  it("notifies the session for protected 401 responses", async () => {
    const client = new ApiClient();
    const onAuthenticationFailure = vi.fn();
    client.setAuthenticationFailureHandler(onAuthenticationFailure);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      errorResponse(401, "authentication_required"),
    );

    await expect(client.getMe()).rejects.toBeInstanceOf(ApiError);
    expect(onAuthenticationFailure).toHaveBeenCalledOnce();
  });

  it("keeps login's own 401 local so the credential message remains visible", async () => {
    const client = new ApiClient();
    const onAuthenticationFailure = vi.fn();
    client.setAuthenticationFailureHandler(onAuthenticationFailure);
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      errorResponse(401, "invalid_credentials"),
    );

    await expect(
      client.login({ password: "wrong", username: "someone" }),
    ).rejects.toMatchObject({ code: "invalid_credentials", status: 401 });
    expect(onAuthenticationFailure).not.toHaveBeenCalled();
  });

  it("takes the lock wait value verbatim from the server details", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      errorResponse(429, "login_locked", 173),
    );

    await expect(
      new ApiClient().login({ password: "wrong", username: "someone" }),
    ).rejects.toMatchObject({ retryAfterSeconds: 173 });
  });

  it("TC-UI-01 preserves structured password-policy reasons for the component", async () => {
    vi.spyOn(document, "cookie", "get").mockReturnValue(
      "__Host-pws_csrf=policy-csrf",
    );
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          error: {
            code: "invalid_new_password",
            details: {
              password_policy: [
                { code: "too_short", current_length: 10 },
                { code: "common_password" },
              ],
            },
          },
        }),
        { status: 400 },
      ),
    );
    const client = new ApiClient();
    await expect(
      client.changePassword({
        confirm_password: "Password1!",
        current_password: null,
        new_password: "Password1!",
      }),
    ).rejects.toMatchObject({
      code: "invalid_new_password",
      details: {
        password_policy: [
          { code: "too_short", current_length: 10 },
          { code: "common_password" },
        ],
      },
    });
  });
});

describe("M2_13A model-source authentication boundary", () => {
  const writeBody = {
    clear: false,
    credential: "source-token",
    current_password: "administrator-password",
    source_type: "Hugging Face" as const,
  };

  it("positive control keeps invalid_credentials local to the modal", async () => {
    const client = new ApiClient();
    const onAuthenticationFailure = vi.fn();
    client.setAuthenticationFailureHandler(onAuthenticationFailure);
    vi.spyOn(document, "cookie", "get").mockReturnValue(
      "__Host-pws_csrf=model-source-csrf",
    );
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      errorResponse(401, "invalid_credentials"),
    );

    await expect(client.updateModelSource(writeBody)).rejects.toMatchObject({
      code: "invalid_credentials",
      status: 401,
    });
    expect(onAuthenticationFailure).not.toHaveBeenCalled();
  });

  it("positive control sends authentication_required through session recovery", async () => {
    const client = new ApiClient();
    const onAuthenticationFailure = vi.fn();
    client.setAuthenticationFailureHandler(onAuthenticationFailure);
    vi.spyOn(document, "cookie", "get").mockReturnValue(
      "__Host-pws_csrf=model-source-csrf",
    );
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      errorResponse(401, "authentication_required"),
    );

    await expect(client.updateModelSource(writeBody)).rejects.toMatchObject({
      code: "authentication_required",
      status: 401,
    });
    expect(onAuthenticationFailure).toHaveBeenCalledOnce();
  });

  it("writes with PUT, CSRF and the closed server field names", async () => {
    vi.spyOn(document, "cookie", "get").mockReturnValue(
      "__Host-pws_csrf=model-source-csrf",
    );
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          configuration_state: "configured",
          credential_last4: "oken",
          next_action: null,
          source_type: "Hugging Face",
          verification_reason_code: "credential_valid",
          verification_status: "accessible",
          verified_at: "2026-09-11T05:00:00Z",
        }),
      ),
    );
    await new ApiClient().updateModelSource(writeBody);
    const [path, request] = fetchMock.mock.calls[0] ?? [];
    expect(path).toBe("/api/v1/system/model-sources");
    expect(request?.method).toBe("PUT");
    expect(new Headers(request?.headers).get("X-CSRF-Token")).toBe(
      "model-source-csrf",
    );
    expect(typeof request?.body).toBe("string");
    if (typeof request?.body !== "string") {
      throw new Error("model-source request body must be JSON text");
    }
    expect(JSON.parse(request.body)).toEqual({
      clear: false,
      credential: "source-token",
      current_password: "administrator-password",
      source_type: "Hugging Face",
    });
  });
});

describe("UI-33 administrator API key revoke client", () => {
  it("posts the colon-revoke path with CSRF and parses the extended key", async () => {
    vi.spyOn(document, "cookie", "get").mockReturnValue(
      "__Host-pws_csrf=ui33-csrf",
    );
    const keyId = "0198f58a-2586-7e37-bbf5-fefe165e8083";
    const userId = "0198f58a-2586-7e37-bbf5-fefe165e8084";
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({
          display_prefix: "pws_live_ab",
          expires_at: null,
          id: keyId,
          last4: "wxyz",
          last_used_at: "2026-10-14T01:02:03Z",
          name: "CI key",
          status: "Revoked",
        }),
      ),
    );
    const key = await new ApiClient().revokeUserApiKey(userId, keyId);
    const [path, request] = fetchMock.mock.calls[0] ?? [];
    expect(path).toBe(`/api/v1/users/${userId}/api-keys/${keyId}:revoke`);
    expect(request?.method).toBe("POST");
    expect(new Headers(request?.headers).get("X-CSRF-Token")).toBe("ui33-csrf");
    expect(key).toMatchObject({
      id: keyId,
      name: "CI key",
      status: "Revoked",
    });
  });
});

function errorResponse(
  status: number,
  code: string,
  retryAfterSeconds?: number,
) {
  return new Response(
    JSON.stringify({
      error: {
        code,
        details:
          retryAfterSeconds === undefined
            ? {}
            : { retry_after_seconds: retryAfterSeconds },
        message: "must never be rendered",
        request_id: "0198f58a-2586-7e37-bbf5-fefe165e8080",
      },
    }),
    {
      headers: { "Content-Type": "application/json" },
      status,
    },
  );
}
