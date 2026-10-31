import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { Controller, useForm } from "react-hook-form";
import { z } from "zod";
import { ApiError, type ApiClient } from "../api/client";
import {
  diagnosticRequestSchema,
  type DiagnosticManifest,
  type DiagnosticPreview,
  type DiagnosticRequest,
} from "../api/diagnostic-contracts";
import {
  Button,
  DataTable,
  InlineAlert,
  MissingValue,
  TextField,
  StatusBadge,
} from "../components";
import { formatBytes, formatObservedAt } from "./formatting";

const fieldsSchema = z.object({
  minutes: z
    .string()
    .refine(
      (value) =>
        /^\d+$/.test(value) && Number(value) >= 1 && Number(value) <= 1440,
      "请输入 1–1440 分钟",
    ),
  end: z
    .string()
    .refine(
      (value) => !value || Number.isFinite(new Date(value).getTime()),
      "请输入有效结束时间",
    ),
  requests: z
    .string()
    .refine(
      (value) => z.array(z.uuid()).max(20).safeParse(splitIds(value)).success,
      "请输入最多 20 个有效 Request ID，以逗号或空格分隔",
    ),
});
type Fields = z.infer<typeof fieldsSchema>;
function splitIds(value: string) {
  return value.trim() ? value.trim().split(/[\s,，]+/) : [];
}
const labels: Record<DiagnosticManifest["items"][number]["key"], string> = {
  component_versions: "组件版本",
  configuration: "配置脱敏副本",
  health: "组件健康状态",
  structured_events: "结构化事件",
  metrics: "指标摘要",
  request_records: "调用记录元数据",
  sanitized_logs: "相关日志行",
};
function reason(value: string | null) {
  switch (value) {
    case "sanitized_config_unavailable":
      return "配置脱敏副本暂不可用";
    case "sanitized_source_timeout":
      return "来源响应超时，请稍后重新预览";
    case "no_sanitized_log_source_for_requested_records":
      return "未找到这些调用的相关日志";
    case "sanitized_content_exceeds_limit":
      return "脱敏内容超过大小上限";
    case "byte_or_count_limit_reached":
      return "已达到条数或大小上限";
    case null:
      return "—";
    default:
      return "安全数据来源暂不可用，请稍后重新预览";
  }
}

export function DiagnosticsTab({ apiClient }: { apiClient: ApiClient }) {
  const form = useForm<Fields>({
    resolver: zodResolver(fieldsSchema),
    defaultValues: { minutes: "60", end: "", requests: "" },
  });
  const [preview, setPreview] = useState<DiagnosticPreview>();
  const [stale, setStale] = useState(false);
  const [error, setError] = useState<unknown>();
  const [notice, setNotice] = useState("");
  const epoch = useRef(0);
  const busy = useRef(false);
  const previewMutation = useMutation({
    mutationFn: (input: DiagnosticRequest) =>
      apiClient.previewDiagnostics(input),
    gcTime: 0,
  });
  const downloadMutation = useMutation({
    mutationFn: ({
      input,
      receipt,
    }: {
      input: DiagnosticRequest;
      receipt: string;
    }) => apiClient.downloadDiagnostics(input, receipt),
    gcTime: 0,
  });
  const pending = previewMutation.isPending || downloadMutation.isPending;
  useEffect(
    () => () => {
      epoch.current += 1;
    },
    [],
  );
  useEffect(() => {
    if (!preview) return;
    const timer = window.setTimeout(
      () => {
        setStale(true);
        setNotice("预览已过期，请重新预览后确认下载。");
      },
      Math.max(0, preview.expiresAt - Date.now()),
    );
    return () => {
      window.clearTimeout(timer);
    };
  }, [preview]);
  function invalidate() {
    epoch.current += 1;
    setStale(true);
    setError(undefined);
    setNotice(preview ? "条件已修改，请重新预览。" : "");
  }
  async function makePreview(fields: Fields) {
    if (busy.current) return;
    busy.current = true;
    const attempt = ++epoch.current;
    setPreview(undefined);
    setError(undefined);
    setNotice("");
    const input = diagnosticRequestSchema.parse({
      window_minutes: Number(fields.minutes),
      end_at: fields.end ? new Date(fields.end).toISOString() : undefined,
      request_ids: splitIds(fields.requests),
      instance_id: null,
    });
    try {
      const result = await previewMutation.mutateAsync(input);
      if (epoch.current === attempt) {
        setPreview(result);
        setStale(false);
      }
    } catch (failure) {
      if (epoch.current === attempt) setError(failure);
    } finally {
      busy.current = false;
      previewMutation.reset();
    }
  }
  async function download() {
    if (!preview || stale || busy.current) return;
    if (preview.expiresAt <= Date.now()) {
      setStale(true);
      setNotice("预览已过期，请重新预览后确认下载。");
      return;
    }
    busy.current = true;
    const attempt = epoch.current;
    setError(undefined);
    setNotice("");
    const manifest = preview.manifest;
    try {
      const blob = await downloadMutation.mutateAsync({
        receipt: preview.receipt,
        input: {
          window_minutes: Number(form.getValues("minutes")),
          end_at: manifest.window.to,
          request_ids: manifest.request_ids,
          instance_id: null,
        },
      });
      if (attempt !== epoch.current) return;
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      try {
        anchor.href = url;
        anchor.download = "holm-diagnostics.zip";
        document.body.append(anchor);
        anchor.click();
      } finally {
        anchor.remove();
        URL.revokeObjectURL(url);
      }
      setNotice("诊断包已交给浏览器下载。");
    } catch (failure) {
      if (attempt === epoch.current) {
        setError(failure);
        if (
          failure instanceof ApiError &&
          failure.code === "diagnostic_preview_expired"
        )
          setStale(true);
      }
    } finally {
      busy.current = false;
      downloadMutation.reset();
    }
  }
  return (
    <section className="pw-system-stack pw-diagnostics" aria-label="诊断包">
      <form
        className="pw-system-stack pw-system-panel"
        aria-label="诊断包条件"
        onSubmit={(event) => {
          void form.handleSubmit(makePreview)(event);
        }}
        onChange={invalidate}
      >
        <div className="pw-diagnostics-fields">
          {(
            [
              [
                "minutes",
                "时间范围（分钟）",
                "number",
                "最长 1440 分钟（24 小时）",
              ],
              [
                "end",
                "结束时间",
                "datetime-local",
                "留空表示预览时刻；按本地时区填写",
              ],
              [
                "requests",
                "Request ID（可选）",
                "text",
                "填写后才包含这些调用的记录与相关日志行",
              ],
            ] as const
          ).map(([name, label, type, helpText]) => (
            <Controller
              key={name}
              name={name}
              control={form.control}
              render={({ field, fieldState }) =>
                name === "requests" ? (
                  <div className="pw-field">
                    <label
                      className="pw-field__label"
                      htmlFor="diagnostic-requests"
                    >
                      {label}
                    </label>
                    <textarea
                      {...field}
                      id="diagnostic-requests"
                      className="pw-input"
                      aria-describedby="diagnostic-requests-help diagnostic-requests-error"
                      aria-invalid={fieldState.invalid}
                      maxLength={760}
                      placeholder="最多 20 个，以逗号或空格分隔"
                    />
                    <span
                      className="pw-field__help"
                      id="diagnostic-requests-help"
                    >
                      {helpText}
                    </span>
                    <span
                      id="diagnostic-requests-error"
                      className="pw-field__error"
                      role={fieldState.error ? "alert" : undefined}
                    >
                      {fieldState.error?.message}
                    </span>
                  </div>
                ) : (
                  <TextField
                    {...field}
                    label={label}
                    type={type}
                    helpText={helpText}
                    errorText={fieldState.error?.message}
                  />
                )
              }
            />
          ))}
        </div>
        <div>
          <Button
            type="submit"
            variant={preview ? "secondary" : "primary"}
            loading={previewMutation.isPending}
            loadingText="正在预览"
            disabled={pending}
          >
            {preview ? "重新预览" : "预览内容"}
          </Button>
        </div>
      </form>
      {error !== undefined ? (
        <InlineAlert
          title={
            error instanceof ApiError && error.status === 403
              ? "没有诊断包权限"
              : "诊断操作未完成"
          }
          tone="negative"
        >
          {error instanceof ApiError &&
          error.code === "diagnostic_preview_expired"
            ? "预览已失效，请重新预览后确认下载。"
            : error instanceof ApiError && error.status === 403
              ? "当前账号无法生成诊断包，请联系管理员。"
              : error instanceof ApiError && error.code === "diagnostics_busy"
                ? "诊断生成容量已满，请稍后重试。"
                : "无法完成本次操作，请检查连接后重试；输入已保留。"}
          {error instanceof ApiError && error.requestId ? (
            <span> Request ID：{error.requestId}</span>
          ) : null}
        </InlineAlert>
      ) : null}
      {notice ? <p role="status">{notice}</p> : null}
      {preview ? (
        <div className="pw-system-stack pw-system-group">
          <h2>诊断包内容清单</h2>
          <p>
            时间范围：{formatObservedAt(preview.manifest.window.from)} 至{" "}
            {formatObservedAt(preview.manifest.window.to)}
          </p>
          {!preview.manifest.items.some(
            (item) => item.status === "included" && item.count > 0,
          ) ? (
            <p role="status">当前没有可包含的数据，请调整条件后重新预览。</p>
          ) : null}
          <DataTable
            caption="诊断包内容清单"
            rows={preview.manifest.items}
            getRowKey={(item) => item.key}
            columns={[
              {
                id: "name",
                header: "内容",
                render: (item) => <strong>{labels[item.key]}</strong>,
              },
              {
                id: "status",
                header: "包含情况",
                render: (item) => (
                  <StatusBadge
                    icon={
                      item.status === "included"
                        ? item.truncated
                          ? "notice"
                          : "success"
                        : "pause"
                    }
                    tone={
                      item.status === "included"
                        ? item.truncated
                          ? "notice"
                          : "positive"
                        : "idle"
                    }
                    text={
                      item.status === "included"
                        ? item.truncated
                          ? "已包含（已截断）"
                          : "已包含"
                        : "未包含"
                    }
                  />
                ),
              },
              {
                id: "count",
                header: "条数",
                numeric: true,
                render: (item) =>
                  item.status === "included" ? (
                    item.count
                  ) : (
                    <MissingValue
                      kind="unavailable"
                      reason="未包含，条数未知"
                    />
                  ),
              },
              {
                id: "bytes",
                header: "大小",
                numeric: true,
                render: (item) =>
                  item.status === "included" ? (
                    formatBytes(item.bytes)
                  ) : (
                    <MissingValue
                      kind="unavailable"
                      reason="未包含，字节未知"
                    />
                  ),
              },
              {
                id: "reason",
                header: "说明",
                render: (item) =>
                  item.status === "not_included" &&
                  (item.key === "request_records" ||
                    item.key === "sanitized_logs") &&
                  preview.manifest.request_ids.length === 0
                    ? "未填写 Request ID"
                    : item.reason === null &&
                        (item.key === "health" || item.key === "metrics")
                      ? "生成时的快照"
                      : `${item.truncated ? "已截断；" : ""}${reason(item.reason)}`,
              },
            ]}
          />
          <p>预览 5 分钟内有效；过期或修改条件后需重新预览。</p>
          <div>
            <Button
              variant="primary"
              onClick={() => void download()}
              disabled={
                pending ||
                stale ||
                !preview.manifest.items.some(
                  (item) => item.status === "included" && item.count > 0,
                )
              }
              loading={downloadMutation.isPending}
              loadingText="正在生成"
            >
              下载诊断包
            </Button>
          </div>
        </div>
      ) : null}
    </section>
  );
}
