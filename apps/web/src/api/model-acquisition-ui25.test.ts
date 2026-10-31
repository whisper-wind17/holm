import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiClient } from "./client";
import {
  DOWNLOAD_TASK_STATUSES,
  ModelAcquisitionContractError,
  parseDownloadStatusCounts,
  parseModelArtifactDetail,
} from "./model-acquisition-contracts";

const TASK_ID = "0198f58a-2586-7e37-bbf5-fefe165e8082";

afterEach(() => {
  vi.restoreAllMocks();
});

function detail(overrides: Record<string, unknown> = {}) {
  return {
    created_at: "2026-09-11T00:00:00Z",
    display_name: "Qwen3",
    id: "0198f58a-2586-7e37-bbf5-fefe165e8084",
    last_verified_at: null,
    model_id: "0198f58a-2586-7e37-bbf5-fefe165e8083",
    path: "/models/qwen3",
    referencing_instances: 0,
    repository_id: "Qwen/Qwen3",
    resolved_revision: "abc123",
    size_bytes: 1024,
    source: "Hugging Face",
    status: "Ready",
    storage_type: "Managed",
    updated_at: "2026-09-11T01:00:00Z",
    model: {
      architecture: null,
      modalities: null,
      precision: null,
      quantization: null,
      context_length: null,
      license: null,
    },
    files: [{ path: "config.json", size_bytes: 1, sha256: "a".repeat(64) }],
    download_tasks: [
      {
        id: TASK_ID,
        status: "Completed",
        created_at: "2026-09-11T00:00:00Z",
        completed_at: "2026-09-11T00:10:00Z",
      },
    ],
    instances: [{ id: TASK_ID, name: "chat", status: "Running" }],
    ...overrides,
  };
}

describe("UI-25 contracts", () => {
  it("accepts nine status counts and rejects unknown keys", () => {
    const counts = Object.fromEntries(
      DOWNLOAD_TASK_STATUSES.map((s) => [s, 0]),
    );
    expect(parseDownloadStatusCounts({ counts }).counts.Failed).toBe(0);
    expect(() => parseDownloadStatusCounts({ counts: { Bogus: 1 } })).toThrow(
      ModelAcquisitionContractError,
    );
    expect(() => parseDownloadStatusCounts({ counts, extra: 1 })).toThrow(
      ModelAcquisitionContractError,
    );
  });

  it("parses detail with files as a list or null and rejects an empty list", () => {
    expect(parseModelArtifactDetail(detail()).files).toHaveLength(1);
    expect(parseModelArtifactDetail(detail({ files: null })).files).toBeNull();
    expect(() => parseModelArtifactDetail(detail({ files: [] }))).toThrow(
      ModelAcquisitionContractError,
    );
    expect(() => parseModelArtifactDetail(detail({ extra: 1 }))).toThrow(
      ModelAcquisitionContractError,
    );
  });
});

describe("UI-25 client", () => {
  function mockFetch(response: Response) {
    vi.spyOn(document, "cookie", "get").mockReturnValue("__Host-pws_csrf=csrf");
    return vi.spyOn(globalThis, "fetch").mockResolvedValue(response);
  }

  it("sends repeated status filters", async () => {
    const fetchMock = mockFetch(
      new Response(JSON.stringify({ items: [], next_cursor: null })),
    );
    await new ApiClient().getDownloadTasks("c1", ["Failed", "Cancelled"]);
    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      "/api/v1/downloads?cursor=c1&status=Failed&status=Cancelled",
    );
  });

  it("deletes a task with the cleanup flag and CSRF", async () => {
    const fetchMock = mockFetch(new Response(null, { status: 204 }));
    await new ApiClient().deleteDownloadTask(TASK_ID, true);
    const [path, request] = fetchMock.mock.calls[0] ?? [];
    expect(path).toBe(`/api/v1/downloads/${TASK_ID}?cleanup_staging=true`);
    expect(request?.method).toBe("DELETE");
    expect(new Headers(request?.headers).get("X-CSRF-Token")).toBe("csrf");
  });

  it("passes cleanup_staging only on cancel", async () => {
    const body = JSON.stringify({
      attempt: 1,
      command: "cancel",
      id: TASK_ID,
      staging_cleanup_requested: false,
      status: "Cancelled",
    });
    const fetchMock = mockFetch(new Response(body));
    await new ApiClient().executeDownloadCommand(TASK_ID, "cancel", false);
    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      `/api/v1/downloads/${TASK_ID}/cancel?cleanup_staging=false`,
    );
  });

  it("reads status counts and artifact detail", async () => {
    const counts = Object.fromEntries(
      DOWNLOAD_TASK_STATUSES.map((s) => [s, 0]),
    );
    const fetchMock = mockFetch(new Response(JSON.stringify({ counts })));
    await new ApiClient().downloadStatusCounts();
    expect(fetchMock.mock.calls[0]?.[0]).toBe(
      "/api/v1/downloads/status-counts",
    );
    fetchMock.mockResolvedValue(new Response(JSON.stringify(detail())));
    const result = await new ApiClient().artifactDetail(detail().id);
    expect(fetchMock.mock.calls[1]?.[0]).toBe(
      `/api/v1/artifacts/${detail().id}`,
    );
    expect(result.instances[0]?.status).toBe("Running");
  });
});
