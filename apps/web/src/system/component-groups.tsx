import { useState } from "react";
import type {
  HostEnvironmentItem,
  SystemComponent,
  SystemComponentsResponse,
} from "../api/system-contracts";
import { SystemContractError } from "../api/system-contracts";
import {
  Button,
  DataTable,
  InlineAlert,
  MissingValue,
  StatusBadge,
} from "../components";
import {
  componentPresentation,
  componentStatusPresentation,
  driverCompatibilityPresentation,
  driverCompatibilityReason,
  hostEnvironmentName,
  hostEnvironmentReason,
  hostEnvironmentStatusPresentation,
  inferenceEngineName,
} from "./presentation";
import { formatObservedAt } from "./formatting";

// service names and impacts follow the approved prototype's SUBS.
const MODULES = {
  Edge: ["edge", "管理页面与 API 入口不可用。"],
  "Control Plane": ["control-api", "管理页面与配置操作可能不可用。"],
  "Telemetry Collector": [
    "control-api（进程内）",
    "监控指标停止更新，AI 服务可用性判断不受影响。",
  ],
  Gateway: ["gateway", "新的 API 调用可能失败。"],
  Scheduler: ["instance-controller", "模型实例的部署、停止与恢复可能延迟。"],
  "Download Manager": ["download-worker", "模型下载任务可能暂停。"],
  "Request Ledger Writer": [
    "ledger-writer",
    "用量入账可能延迟，调用是否继续取决于可靠队列状态。",
  ],
  Maintenance: ["maintenance-worker", "备份与过期数据清理可能延迟。"],
  Database: ["postgres", "平台配置与授权数据可能无法读取。"],
  "Host Agent": [
    "Host Agent（systemd）",
    "GPU 信息不可用，模型实例无法启动或停止；已在运行的实例不受影响。",
  ],
} as const satisfies Record<SystemComponent["id"], readonly [string, string]>;

export function hostRows(response: SystemComponentsResponse) {
  const get = (id: HostEnvironmentItem["id"]) => {
    const item = response.host_environment.items.find(
      (entry) => entry.id === id,
    );
    if (!item) throw new SystemContractError();
    return item;
  };
  const current = (item: HostEnvironmentItem) =>
    item.current.state === "known" ? item.current.value : "无法检测";
  const minimum = (item: HostEnvironmentItem) =>
    item.minimum === null ? "未声明" : `≥ ${item.minimum}`;
  const docker = get("docker"),
    compose = get("compose");
  const severity = { satisfied: 0, unknown: 1, unsatisfied: 2 } as const;
  const status =
    severity[docker.status] >= severity[compose.status]
      ? docker.status
      : compose.status;
  return [get("nvidia_driver"), docker, get("nvidia_container_toolkit")].map(
    (item) => ({
      id: item.id,
      label:
        item.id === "docker" ? "Docker／Compose" : hostEnvironmentName(item.id),
      current:
        item.id === "docker"
          ? `Docker ${current(docker)} · Compose ${current(compose)}`
          : current(item),
      minimum:
        item.id === "docker"
          ? `Docker ${minimum(docker)} · Compose ${minimum(compose)}`
          : minimum(item),
      status: item.id === "docker" ? status : item.status,
      note:
        item.id === "docker"
          ? [docker, compose]
              .filter((part) => part.status !== "satisfied")
              .map(
                (part) =>
                  `${hostEnvironmentName(part.id)}：${hostEnvironmentReason(part.reason)}`,
              )
              .join("；")
          : hostEnvironmentReason(item.reason),
      sub:
        item.id === "nvidia_driver" &&
        response.host_environment.cuda_driver_version.state === "known"
          ? `驱动支持 CUDA ${response.host_environment.cuda_driver_version.value}`
          : "",
    }),
  );
}
export function componentAttention(response: SystemComponentsResponse) {
  return [
    ...hostRows(response)
      .filter((item) => item.status === "unsatisfied")
      .map((item) => `${item.label}：低于最低要求`),
    ...response.components
      .filter(isAbnormal)
      .map(
        (item) =>
          `${componentPresentation(item.id).label}：${componentStatusPresentation(item.status).label}`,
      ),
    ...response.inference_engines
      .filter((item) => item.driver_compatibility === "incompatible")
      .map(
        (item) =>
          `${inferenceEngineName(item.name)} ${item.version}：与当前驱动不兼容`,
      ),
  ];
}
function isAbnormal(component: SystemComponent) {
  return component.status === "degraded" || component.status === "unavailable";
}
function Observation({
  value,
  timestamp = false,
}: {
  value: SystemComponent["version"] | { state: "known"; value: number };
  timestamp?: boolean;
}) {
  return value.state === "known" ? (
    <>{timestamp ? formatObservedAt(String(value.value)) : value.value}</>
  ) : (
    <MissingValue kind="unavailable" reason="未采集" />
  );
}
function ModuleDetail({ component }: { component: SystemComponent }) {
  const details = component.details;
  return (
    <span className="pw-system-component-detail">
      <span>
        {component.status === "not_deployed"
          ? "该子模块尚未上报状态，不计入异常。"
          : isAbnormal(component)
            ? MODULES[component.id][1]
            : component.id === "Database"
              ? null
              : "运行正常。"}
      </span>
      {component.id === "Database" ? (
        <span>
          PostgreSQL <Observation value={component.version} /> · Schema{" "}
          {details.kind === "database" ? (
            <Observation value={details.schema_revision} />
          ) : (
            "—"
          )}
        </span>
      ) : null}
      {details.kind === "request_ledger_writer" ? (
        <span>
          待写事件 <Observation value={details.pending_event_count} /> ·
          最旧事件 <Observation value={details.oldest_event_at} timestamp />
        </span>
      ) : null}
    </span>
  );
}
export function ComponentsContent({
  response,
}: {
  response: SystemComponentsResponse;
}) {
  const attention = componentAttention(response);
  const abnormal = response.components.filter(isAbnormal);
  const quiet = response.components.filter(
    (item) => item.status === "not_deployed",
  );
  const [expanded, setExpanded] = useState<boolean | null>(null);
  const open = expanded ?? abnormal.length > 0;
  const order = (item: SystemComponent) =>
    isAbnormal(item) ? 0 : item.status === "not_deployed" ? 2 : 1;
  const modules = Object.keys(MODULES)
    .flatMap((id) => response.components.filter((item) => item.id === id))
    .sort((a, b) => order(a) - order(b));
  const driver = response.host_environment.items.find(
    (item) => item.id === "nvidia_driver",
  )?.current;
  return (
    <div className="pw-system-stack">
      {attention.length ? (
        <InlineAlert
          title={`${String(attention.length)} 项需要关注`}
          tone="negative"
        >
          {attention.join("；")}。详情见下方各组。
        </InlineAlert>
      ) : (
        <div className="pw-system-summary">
          <StatusBadge
            icon="success"
            tone="positive"
            text="宿主环境、Holm 与推理引擎均无异常"
          />
          {quiet.length ? (
            <span>{quiet.length} 个子模块尚未上报状态，不计入异常</span>
          ) : null}
        </div>
      )}
      <section className="pw-system-group" aria-label="宿主环境">
        <h2>宿主环境</h2>
        <DataTable
          caption="宿主环境"
          rows={hostRows(response)}
          getRowKey={(item) => item.id}
          columns={[
            {
              id: "item",
              header: "项目",
              render: (item) => (
                <>
                  <strong>{item.label}</strong>
                  {item.sub ? (
                    <span className="pw-system-sub">{item.sub}</span>
                  ) : null}
                </>
              ),
            },
            {
              id: "current",
              header: "当前版本",
              render: (item) => (
                <span className="pw-system-technical">{item.current}</span>
              ),
            },
            {
              id: "minimum",
              header: "最低要求",
              render: (item) => (
                <span className="pw-system-technical">{item.minimum}</span>
              ),
            },
            {
              id: "status",
              header: "状态",
              render: (item) => {
                const status = hostEnvironmentStatusPresentation(item.status);
                return (
                  <>
                    <StatusBadge
                      {...{
                        icon: status.icon,
                        tone: status.tone,
                        text: status.label,
                      }}
                    />
                    {item.note ? (
                      <span className="pw-system-sub">{item.note}</span>
                    ) : null}
                  </>
                );
              },
            },
          ]}
        />
      </section>
      <section className="pw-system-group" aria-label="Holm">
        <h2>Holm</h2>
        <DataTable
          caption="Holm"
          rows={[response.platform]}
          getRowKey={() => "holm"}
          columns={[
            {
              id: "name",
              header: "名称",
              render: () => (
                <>
                  <strong>Holm 平台</strong>
                  <span className="pw-system-sub">
                    Compose 9 个服务 + 宿主机服务
                  </span>
                </>
              ),
            },
            {
              id: "version",
              header: "版本",
              render: (item) => (
                <span className="pw-system-technical">
                  {item.build_version}
                </span>
              ),
            },
            {
              id: "status",
              header: "状态",
              render: () => (
                <StatusBadge
                  icon={abnormal.length ? "alert" : "success"}
                  tone={
                    abnormal.some((item) => item.status === "unavailable")
                      ? "negative"
                      : abnormal.length
                        ? "notice"
                        : "positive"
                  }
                  text={
                    abnormal.length
                      ? `${String(abnormal.length)} 个子模块异常`
                      : quiet.length
                        ? `就绪（${String(quiet.length)} 个未上报）`
                        : "全部就绪"
                  }
                />
              ),
            },
            {
              id: "action",
              header: "说明",
              render: () => (
                <Button
                  variant="ghost"
                  aria-expanded={open}
                  onClick={() => {
                    setExpanded(!open);
                  }}
                >
                  {open ? "收起" : "展开"}子模块（{modules.length}）
                </Button>
              ),
            },
          ]}
        />
        {open ? (
          <DataTable
            caption="Holm 子模块"
            rows={modules}
            getRowKey={(item) => item.id}
            columns={[
              {
                id: "module",
                header: "子模块",
                render: (item) => (
                  <strong
                    className={
                      item.status === "not_deployed"
                        ? "pw-system-muted"
                        : undefined
                    }
                  >
                    {componentPresentation(item.id).label}
                  </strong>
                ),
              },
              {
                id: "service",
                header: "服务",
                render: (item) => (
                  <span className="pw-system-technical">
                    {MODULES[item.id][0]}
                  </span>
                ),
              },
              {
                id: "status",
                header: "状态",
                render: (item) => {
                  const status = componentStatusPresentation(item.status);
                  return (
                    <StatusBadge
                      icon={status.icon}
                      tone={status.tone}
                      text={
                        item.status === "not_deployed"
                          ? "未上报状态"
                          : status.label
                      }
                    />
                  );
                },
              },
              {
                id: "impact",
                header: "影响与补充",
                render: (item) => <ModuleDetail component={item} />,
              },
            ]}
          />
        ) : null}
      </section>
      <section className="pw-system-group" aria-label="推理引擎">
        <h2>推理引擎</h2>
        <DataTable
          caption="推理引擎"
          rows={response.inference_engines}
          getRowKey={(item) => `${item.name}-${item.version}`}
          emptyContent="没有推理引擎信息"
          columns={[
            {
              id: "engine",
              header: "引擎",
              render: (item) => (
                <>
                  <strong>{inferenceEngineName(item.name)}</strong>
                  <span className="pw-system-sub">版本 {item.version}</span>
                </>
              ),
            },
            {
              id: "image",
              header: "镜像",
              render: (item) => (
                <span className="pw-system-technical">
                  {item.image_digest === null
                    ? "按进程启动，无镜像"
                    : `sha256:…${item.image_digest.slice(-6)}`}
                </span>
              ),
            },
            {
              id: "instances",
              header: "在用实例",
              render: (item) =>
                item.active_instance_count === null
                  ? "—"
                  : `${String(item.active_instance_count)} 个`,
            },
            {
              id: "compatibility",
              header: "与当前驱动",
              render: (item) => {
                const status = driverCompatibilityPresentation(
                  item.driver_compatibility,
                );
                return (
                  <>
                    <StatusBadge
                      icon={status.icon}
                      tone={status.tone}
                      text={status.label}
                    />
                    <span className="pw-system-sub">
                      {driverCompatibilityReason(
                        item.reason,
                        item.minimum_driver_version,
                        driver?.state === "known" ? driver.value : null,
                      )}
                    </span>
                  </>
                );
              },
            },
          ]}
        />
      </section>
    </div>
  );
}
