import { useEffect, useRef } from "react";
import { Link } from "react-router";

import type { ApiClient } from "../api/client";
import { ModelSelectionStep } from "./selection-step";

export const MODEL_DOWNLOAD_PATH = "/models/download";

export function ModelDownloadPage({ apiClient }: { apiClient: ApiClient }) {
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => heading.current?.focus(), []);

  return (
    <div className="pw-model-download-page">
      <nav aria-label="面包屑" className="pw-model-download-page__breadcrumbs">
        <Link to="/models">模型</Link>
        <span aria-hidden="true">›</span>
        <Link to="/models">本地模型</Link>
        <span aria-hidden="true">›</span>
        <span aria-current="page">下载模型</span>
      </nav>
      <header className="pw-model-download-page__head">
        <span>
          <h1 ref={heading} tabIndex={-1}>
            下载模型
          </h1>
          <p>
            先读取模型信息确认版本与大小，再下载到服务器。下载在后台进行，不占用
            GPU，可以离开本页。
          </p>
        </span>
      </header>
      <ModelSelectionStep apiClient={apiClient} />
    </div>
  );
}
