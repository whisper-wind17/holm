import { describe, expect, it, vi } from "vitest";

import {
  GATEWAY_ERROR_CODES,
  gatewayErrorPresentation,
  runFirstTestCall,
  type FirstTestCallProgress,
} from "./first-test-call";

describe("UI_46 M4-13 first API key test call", () => {
  it("M4-13 sends max_tokens 8 and discards response text after token detection", async () => {
    const responseSentinel = "M4-13-RESPONSE-BODY-MUST-NOT-SURVIVE";
    const fetcher = vi
      .fn()
      .mockResolvedValueOnce(
        jsonResponse(
          { data: [{ id: "qwen-mini", object: "model" }] },
          200,
          "request-models",
        ),
      )
      .mockResolvedValueOnce(
        jsonResponse(
          {
            choices: [
              { message: { content: responseSentinel, role: "assistant" } },
            ],
            usage: { completion_tokens: 1 },
          },
          200,
          "request-success",
        ),
      );
    const updates: FirstTestCallProgress[] = [];

    const result = await runFirstTestCall({
      apiKey: "holm_sk_fixture_plaintext",
      targetModel: "qwen-mini",
      createRequestId: () => "request-browser",
      fetcher,
      onProgress: (progress) => updates.push(progress),
    });

    expect(result.status).toBe("succeeded");
    expect(result.stages.map(({ label }) => label)).toEqual([
      "Endpoint",
      "鉴权",
      "服务路由",
      "模型响应",
    ]);
    expect(result.stages.map(({ status }) => status)).toEqual([
      "completed",
      "completed",
      "completed",
      "completed",
    ]);
    expect(result.stages[3]?.detail).toBe("已返回 Token");
    expect(JSON.stringify({ result, updates })).not.toContain(responseSentinel);

    const completionInit = fetcher.mock.calls[1]?.[1] as
      RequestInit | undefined;
    expect(completionInit?.method).toBe("POST");
    expect(parseRequestBody(completionInit?.body)).toEqual({
      max_tokens: 8,
      messages: [{ content: "请简短确认服务可用。", role: "user" }],
      model: "qwen-mini",
    });
  });

  it("M4-13 maps an Endpoint transport failure to a stable local Request ID", async () => {
    const result = await runFirstTestCall({
      apiKey: "holm_sk_fixture_plaintext",
      targetModel: "qwen-mini",
      createRequestId: () => "request-endpoint",
      fetcher: vi.fn().mockRejectedValue(new TypeError("offline")),
    });

    expectFailure(result, "endpoint", undefined, "request-endpoint");
    expect(result.stages.map(({ status }) => status)).toEqual([
      "failed",
      "not-run",
      "not-run",
      "not-run",
    ]);
  });

  it("M4-13 maps invalid_api_key to the authentication stage", async () => {
    const fetcher = vi
      .fn()
      .mockResolvedValueOnce(
        jsonResponse({ data: [{ id: "qwen-mini" }] }, 200, "request-models"),
      )
      .mockResolvedValueOnce(
        errorResponse("invalid_api_key", 401, "request-authentication"),
      );

    const result = await runFirstTestCall({
      apiKey: "holm_sk_fixture_plaintext",
      targetModel: "qwen-mini",
      fetcher,
    });

    expectFailure(
      result,
      "authentication",
      "invalid_api_key",
      "request-authentication",
    );
  });

  it("M4-13 maps service_disabled to the service routing stage", async () => {
    const result = await runFirstTestCall({
      apiKey: "holm_sk_fixture_plaintext",
      targetModel: "qwen-mini",
      fetcher: vi
        .fn()
        .mockResolvedValue(
          errorResponse("service_disabled", 503, "request-routing"),
        ),
    });

    expectFailure(
      result,
      "serviceRouting",
      "service_disabled",
      "request-routing",
    );
  });

  it("M4-13 maps inference_timeout to the model response stage", async () => {
    const fetcher = vi
      .fn()
      .mockResolvedValueOnce(
        jsonResponse({ data: [{ id: "qwen-mini" }] }, 200, "request-models"),
      )
      .mockResolvedValueOnce(
        errorResponse("inference_timeout", 504, "request-model-response"),
      );

    const result = await runFirstTestCall({
      apiKey: "holm_sk_fixture_plaintext",
      targetModel: "qwen-mini",
      fetcher,
    });

    expectFailure(
      result,
      "modelResponse",
      "inference_timeout",
      "request-model-response",
    );
  });

  it("M4-13 bounds new-key propagation and reports its last Request ID as routing failure", async () => {
    const fetcher = vi
      .fn()
      .mockResolvedValue(
        errorResponse("invalid_api_key", 401, "request-propagation"),
      );

    const result = await runFirstTestCall({
      apiKey: "holm_sk_fixture_plaintext",
      targetModel: "qwen-mini",
      fetcher,
      now: () => 10,
      propagationTimeoutMs: 0,
    });

    expect(fetcher).toHaveBeenCalledTimes(1);
    expectFailure(result, "serviceRouting", undefined, "request-propagation");
    expect(result.failure?.conclusion).toContain("尚未完成服务路由同步");
  });

  it("M4-13 keeps every frozen Gateway message free of internal implementation terms", () => {
    for (const code of GATEWAY_ERROR_CODES) {
      const failure = gatewayErrorPresentation(code, `request-${code}`);
      expect(failure.requestId).toBe(`request-${code}`);
      expect(failure.conclusion).not.toMatch(/Runtime|后端实例|容器|内部地址/i);
    }
  });
});

function expectFailure(
  result: FirstTestCallProgress,
  stage: NonNullable<FirstTestCallProgress["failure"]>["stage"],
  code: NonNullable<FirstTestCallProgress["failure"]>["code"],
  requestId: string,
) {
  expect(result.status).toBe("failed");
  expect(result.failure).toMatchObject({ requestId, stage });
  expect(result.failure?.code).toBe(code);
  expect(result.stages.find((item) => item.id === stage)?.status).toBe(
    "failed",
  );
}

function errorResponse(code: string, status: number, requestId: string) {
  return jsonResponse(
    { error: { code, message: "must not be rendered", request_id: requestId } },
    status,
    requestId,
  );
}

function jsonResponse(payload: unknown, status: number, requestId: string) {
  return new Response(JSON.stringify(payload), {
    headers: {
      "Content-Type": "application/json",
      "x-request-id": requestId,
    },
    status,
  });
}

function parseRequestBody(body: BodyInit | null | undefined) {
  if (typeof body !== "string") throw new Error("expected a JSON request body");
  return JSON.parse(body) as unknown;
}
