import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiClient } from "../api/client";
import { ApplicationRoutes } from "../app/application";
import { ADMIN_ME } from "../app/test-support";
import { SessionProvider } from "./session";

afterEach(() => {
  vi.restoreAllMocks();
});

function jsonResponse(status: number, body: unknown) {
  return new Response(JSON.stringify(body), {
    headers: { "content-type": "application/json" },
    status,
  });
}

function mountPasswordPage(passwordErrorCode: string) {
  const calls: string[] = [];
  vi.spyOn(document, "cookie", "get").mockReturnValue(
    "__Host-pws_csrf=password-csrf",
  );
  vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
    const url =
      typeof input === "string"
        ? input
        : input instanceof URL
          ? input.href
          : input.url;
    const method = init?.method ?? "GET";
    calls.push(`${method} ${url}`);
    if (method === "GET" && url === "/api/v1/me") {
      return Promise.resolve(jsonResponse(200, ADMIN_ME));
    }
    if (method === "POST" && url === "/api/v1/auth/password") {
      return Promise.resolve(
        jsonResponse(401, {
          error: {
            code: passwordErrorCode,
            details: {},
            message: "password change failed",
            request_id: "request-m1-06a-password",
          },
        }),
      );
    }
    return Promise.resolve(
      jsonResponse(404, {
        error: {
          code: "not_found",
          details: {},
          message: "not found",
          request_id: "request-m1-06a-not-found",
        },
      }),
    );
  });

  const apiClient = new ApiClient();
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={["/change-password"]}>
        <SessionProvider apiClient={apiClient}>
          <ApplicationRoutes apiClient={apiClient} />
        </SessionProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return calls;
}

function passwordField(id: string) {
  const element = document.getElementById(id);
  if (!(element instanceof HTMLInputElement)) {
    throw new Error(`missing password field ${id}`);
  }
  return element;
}

async function submitPasswordChange(calls: string[]) {
  const user = userEvent.setup();
  await screen.findByRole("heading", { name: "修改密码" });
  await user.type(
    passwordField("password-currentPassword"),
    "wrong current passphrase",
  );
  await user.type(
    passwordField("password-newPassword"),
    "Brand-new secure passphrase 4821",
  );
  await user.type(
    passwordField("password-confirmPassword"),
    "Brand-new secure passphrase 4821",
  );
  await user.click(screen.getByRole("button", { name: "修改密码" }));
  await waitFor(() => {
    expect(calls).toContain("POST /api/v1/auth/password");
  });
}

describe("M1-06A password-change 401 recovery boundary", () => {
  it("positive control keeps an incorrect current password on the page with its field error", async () => {
    const calls = mountPasswordPage("current_password_incorrect");
    await submitPasswordChange(calls);

    expect(await screen.findByText("当前密码不正确")).toBeVisible();
    expect(screen.queryByRole("heading", { name: "登录" })).toBeNull();
  });

  it("positive control sends authentication_required back to login", async () => {
    const calls = mountPasswordPage("authentication_required");
    await submitPasswordChange(calls);

    expect(await screen.findByRole("heading", { name: "登录" })).toBeVisible();
    expect(screen.queryByText("当前密码不正确")).toBeNull();
  });
});
