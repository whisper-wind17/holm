import { Link } from "react-router";
import { useEffect, useRef } from "react";

import type { ApiClient } from "../api/client";
import type { PersonalAiService } from "../api/user-contracts";
import { Button, EmptyState, InlineAlert, StatusBadge } from "../components";
import { useMyAiServices } from "../users/queries";

export function PersonalAiServicesPage({
  apiClient,
}: {
  apiClient: ApiClient;
}) {
  const heading = useRef<HTMLHeadingElement>(null);
  const services = useMyAiServices(apiClient);
  useEffect(() => heading.current?.focus(), []);

  return (
    <section className="pw-personal-services-page">
      <header className="pw-services-page__head">
        <span>
          <h1 ref={heading} tabIndex={-1}>
            AI 服务
          </h1>
        </span>
      </header>
      {services.data !== undefined && services.isError ? (
        <InlineAlert title="AI 服务数据可能过期" tone="notice">
          刷新失败，请重试以确认当前授权。
          <Button onClick={() => void services.refetch()} size="s">
            重试
          </Button>
        </InlineAlert>
      ) : null}
      {services.data === undefined && services.isError ? (
        <InlineAlert title="无法获取 AI 服务" tone="negative">
          已授权服务无法显示。请检查平台连接后重试。
          <Button onClick={() => void services.refetch()} size="s">
            重试
          </Button>
        </InlineAlert>
      ) : services.isPending ? (
        <div
          aria-busy="true"
          aria-label="AI 服务正在加载"
          className="pw-personal-service-grid"
          role="status"
        >
          <span className="sk pw-personal-service-skeleton" />
        </div>
      ) : services.data.items.length === 0 ? (
        <EmptyState
          description="获得授权后，服务会出现在这里。如需使用，请联系管理员。"
          icon="apiServices"
          title="管理员尚未为你分配 AI 服务"
        />
      ) : (
        <div className="pw-personal-service-grid">
          {services.data.items.map((service) => (
            <PersonalServiceCard key={service.id} service={service} />
          ))}
        </div>
      )}
    </section>
  );
}

function PersonalServiceCard({ service }: { service: PersonalAiService }) {
  return (
    <article className="pw-personal-service-card">
      <header>
        <h2 title={service.name}>{service.name}</h2>
        <StatusBadge
          icon={service.available ? "success" : "notice"}
          text={service.available ? "可用" : "不可用"}
          tone={service.available ? "positive" : "notice"}
        />
      </header>
      <p>{service.description}</p>
      <dl className="pw-personal-service-meta">
        <div>
          <dt>API 模型名</dt>
          <dd>
            <code>{service.public_model_name}</code>
          </dd>
        </div>
        <div>
          <dt>最近使用</dt>
          <dd>
            {service.last_used_at === null
              ? "尚未使用"
              : new Date(service.last_used_at).toLocaleString("zh-CN")}
          </dd>
        </div>
      </dl>
      <Link
        className="pw-button pw-button--secondary pw-button--m"
        to={`/services/${service.id}`}
      >
        查看接入方式
      </Link>
    </article>
  );
}
