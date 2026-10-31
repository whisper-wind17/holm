import { useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router";

import { HELP_TOPICS } from "../help/content";
import { HelpButton } from "../help/help-center";
import { buildAccessExamples } from "../api/access-examples";
import type { ApiClient } from "../api/client";
import type { PersonalAiService } from "../api/user-contracts";
import { CreateApiKeyModal } from "../api-keys/api-key-page";
import { useApiKeys } from "../api-keys/queries";
import {
  Button,
  CopyControl,
  EmptyState,
  InlineAlert,
  StatusBadge,
  Tabs,
} from "../components";
import { useMyAiServices } from "../users/queries";

const FAQ = [
  [
    "Key 无效或已过期",
    "401 invalid_api_key／api_key_expired",
    "确认请求头是 Authorization: Bearer <密钥>，且密钥完整无多余空格。到「API 密钥」查看状态：已撤销、已过期的密钥不能再用（过期不能续期），请新建一把。",
  ],
  [
    "没有服务权限",
    "403 service_not_authorized",
    "你的密钥没有勾选这个服务，或你的账号已不再获授权。前者可以新建一把包含此服务的密钥；后者请联系管理员。",
  ],
  [
    "服务不可用",
    "503 service_disabled／backend_unavailable",
    "服务被管理员停用或暂时不可用，与你的配置无关，无需修改。稍后重试；长时间不可用请联系管理员。",
  ],
  [
    "Endpoint 或模型名配置错误",
    "404 model_not_found／连接失败",
    "OpenAI SDK、Cursor 等使用 OpenAI 兼容 Endpoint（不要再加 /chat/completions）；Claude Code 使用 Anthropic 兼容 Endpoint。model 填 API 模型名，区分大小写。",
  ],
] as const;

export function PersonalAiServiceDetailPage({
  apiClient,
}: {
  apiClient: ApiClient;
}) {
  const { serviceId } = useParams();
  const services = useMyAiServices(apiClient);
  const service = services.data?.items.find(({ id }) => id === serviceId);
  return (
    <section className="pw-personal-service-detail">
      <nav aria-label="面包屑">
        <Link to="/services">AI 服务</Link>
        {service === undefined ? null : <span> › {service.name}</span>}
      </nav>
      {services.data === undefined && services.isError ? (
        <InlineAlert title="无法获取 AI 服务" tone="negative">
          已授权服务无法显示。请检查平台连接后重试。
          <Button onClick={() => void services.refetch()}>重试</Button>
        </InlineAlert>
      ) : services.isPending ? (
        <div role="status" aria-busy="true" aria-label="AI 服务正在加载">
          <span className="sk pw-personal-service-skeleton" />
        </div>
      ) : service === undefined ? (
        <EmptyState
          icon="apiServices"
          title="无权限"
          description="服务不存在或你已不再获授权，请返回 AI 服务列表。"
        />
      ) : (
        <>
          {services.isError ? (
            <InlineAlert title="AI 服务数据可能过期" tone="notice">
              刷新失败，请重试以确认当前授权。
              <Button onClick={() => void services.refetch()}>重试</Button>
            </InlineAlert>
          ) : null}
          <PersonalServiceGuide
            apiClient={apiClient}
            key={service.id}
            service={service}
            services={services.data.items}
            servicesUnavailable={services.isError}
          />
        </>
      )}
    </section>
  );
}

function PersonalServiceGuide({
  apiClient,
  service,
  services,
  servicesUnavailable,
}: {
  apiClient: ApiClient;
  service: PersonalAiService;
  services: readonly PersonalAiService[];
  servicesUnavailable: boolean;
}) {
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => heading.current?.focus(), []);
  const keys = useApiKeys(apiClient);
  const [selectedKeyId, setSelectedKeyId] = useState("");
  const [testSucceeded, setTestSucceeded] = useState(false);
  const applicable =
    keys.data?.items.filter(
      (key) =>
        key.status === "Active" &&
        (key.expires_at === null || Date.parse(key.expires_at) > Date.now()) &&
        (key.service_ids.length === 0 || key.service_ids.includes(service.id)),
    ) ?? [];
  const selectedKey =
    applicable.find(({ id }) => id === selectedKeyId) ?? applicable[0];
  const examples = buildAccessExamples(
    window.location.origin,
    service.public_model_name,
    service.name,
  );
  const tabs = [
    {
      value: "node",
      label: "OpenAI SDK",
      note: "Node.js，使用官方 openai 包（npm install openai）。",
      code: examples.node,
    },
    {
      value: "python",
      label: "Python",
      note: "Python，使用官方 openai 包（pip install openai）。",
      code: examples.python,
    },
    {
      value: "curl",
      label: "curl",
      note: "命令行直接调用，便于排查网络与权限问题。",
      code: examples.curl,
    },
    {
      value: "claudeCode",
      label: "Claude Code",
      note: "走 Anthropic 兼容接口，ANTHROPIC_BASE_URL 不带 /v1。",
      code: examples.claudeCode,
      foot: "配置方式可能随 Claude Code 版本变化，以工具当前版本为准。",
    },
    {
      value: "cursor",
      label: "Cursor",
      note: "在 Cursor 的模型设置里添加 OpenAI 兼容模型。菜单名称以 Cursor 当前版本为准。",
      code: examples.cursor,
      foot: "字段名称与位置可能随 Cursor 版本变化，以工具当前版本为准。",
    },
    {
      value: "cline",
      label: "Cline",
      note: "API Provider 选「OpenAI Compatible」。菜单名称以 Cline 当前版本为准。",
      code: examples.cline,
      foot: "字段名称与位置可能随 Cline 版本变化，以工具当前版本为准。",
    },
    {
      value: "continue",
      label: "Continue",
      note: "写入 Continue 的配置文件（config.yaml）。字段以 Continue 当前版本为准。",
      code: examples.continue,
      foot: "字段名称可能随 Continue 版本变化，以工具当前版本为准。",
    },
  ];
  return (
    <>
      <header>
        <h1 ref={heading} tabIndex={-1}>
          {service.name}
        </h1>
        <p>{service.description}</p>
      </header>
      {!service.enabled || !service.available ? (
        <div className="pw-personal-service-warning" role="status">
          {!service.enabled
            ? "该服务已被管理员停用，暂时无法调用。你的授权和密钥都还在，恢复后无需修改配置。"
            : "该服务暂时不可用。配置无需修改，恢复后即可正常调用；长时间不可用请联系管理员。"}
        </div>
      ) : null}
      {keys.isError ? (
        <InlineAlert
          title={
            keys.data === undefined
              ? "无法获取 API 密钥"
              : "API 密钥数据可能过期"
          }
          tone="notice"
        >
          请重试以确认适用密钥。
          <Button onClick={() => void keys.refetch()}>重试</Button>
        </InlineAlert>
      ) : null}
      <dl className="pw-personal-access-facts">
        <div>
          <dt>Endpoint</dt>
          <dd className="pw-endpoint-rows">
            <div className="pw-endpoint-row">
              <span>OpenAI 兼容</span>
              <code>{examples.baseUrl}</code>
              <CopyControl
                aria-label="复制 OpenAI 兼容 Endpoint"
                value={examples.baseUrl}
              />
            </div>
            <div className="pw-endpoint-row">
              <span>Anthropic 兼容</span>
              <code>{examples.anthropicBaseUrl}</code>
              <CopyControl
                aria-label="复制 Anthropic 兼容 Endpoint"
                value={examples.anthropicBaseUrl}
              />
            </div>
          </dd>
        </div>
        <div>
          <dt>Model</dt>
          <dd>
            <code>{service.public_model_name}</code>
            <CopyControl
              aria-label="复制 Model"
              value={service.public_model_name}
            />
          </dd>
        </div>
        <div>
          <dt>API Key</dt>
          <dd>
            <span>
              {keys.isPending
                ? "正在加载"
                : keys.data === undefined
                  ? "暂不可用"
                  : selectedKey === undefined
                    ? "尚未创建"
                    : `${selectedKey.name} · holm_sk_…${selectedKey.last4}`}
            </span>
            {applicable.length >= 2 ? (
              <select
                aria-label="切换 API 密钥"
                value={selectedKey?.id}
                onChange={(event) => {
                  setSelectedKeyId(event.target.value);
                }}
              >
                {applicable.map((key) => (
                  <option key={key.id} value={key.id}>
                    切换：{key.name} · …{key.last4}
                  </option>
                ))}
              </select>
            ) : null}
            <CreateApiKeyModal
              apiClient={apiClient}
              personalScope
              services={services}
              servicesUnavailable={servicesUnavailable}
              initialServiceId={service.id}
              onTestSucceeded={() => {
                setTestSucceeded(true);
              }}
              triggerHidden={
                keys.data === undefined || selectedKey !== undefined
              }
            />
          </dd>
        </div>
        <div>
          <dt>Status</dt>
          <dd>
            <StatusBadge
              icon={service.available ? "success" : "notice"}
              text={service.available ? "可用" : "不可用"}
              tone={service.available ? "positive" : "notice"}
            />
          </dd>
        </div>
        <div>
          <dt>最近调用成功</dt>
          <dd>
            {testSucceeded
              ? "刚刚（测试调用）"
              : service.last_succeeded_at === null
                ? "还没有成功的调用"
                : new Date(service.last_succeeded_at).toLocaleString("zh-CN")}
          </dd>
        </div>
      </dl>
      <section className="pw-personal-access-examples">
        <Tabs
          aria-label="接入示例"
          defaultValue="node"
          items={tabs.map((tab) => ({
            value: tab.value,
            label: tab.label,
            content: (
              <div className="pw-personal-access-example">
                <p>{tab.note}</p>
                <pre>
                  <code>{tab.code}</code>
                </pre>
                <footer>
                  <span>
                    {tab.foot ??
                      "先在终端设置环境变量：export HOLM_API_KEY=<你保存的密钥>"}
                  </span>
                  <CopyControl label="复制代码" value={tab.code} />
                </footer>
              </div>
            ),
          }))}
        />
      </section>
      <section className="pw-personal-access-faq">
        <HelpButton topic={HELP_TOPICS.access.id}>查看 API 接入帮助</HelpButton>
        <h2>常见问题</h2>
        {FAQ.map(([title, codes, body]) => (
          <details key={title}>
            <summary>
              {title}
              <code>{codes}</code>
            </summary>
            <p>{body}</p>
          </details>
        ))}
        <p>
          每次调用的响应头都带 <code>x-request-id</code>
          ；联系管理员时附上它，便于定位。
        </p>
      </section>
    </>
  );
}
