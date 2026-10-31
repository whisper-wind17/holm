import { Component, type ReactNode } from "react";

import { Button, CopyControl, Icon } from "../components";
import { toAppError, type AppError } from "./errors";

interface ErrorBoundaryProps {
  children: ReactNode;
}

interface ErrorBoundaryState {
  error: AppError | undefined;
}

export class AppErrorBoundary extends Component<
  ErrorBoundaryProps,
  ErrorBoundaryState
> {
  override state: ErrorBoundaryState = { error: undefined };

  static getDerivedStateFromError(error: unknown): ErrorBoundaryState {
    return { error: toAppError(error) };
  }

  override componentDidCatch() {
    // The browser console is not a permitted error sink for request-derived data.
  }

  override render() {
    if (this.state.error === undefined) {
      return this.props.children;
    }
    return <SafeErrorPage error={this.state.error} />;
  }
}

export function SafeErrorPage({ error }: { error: unknown }) {
  const publicError = toAppError(error);

  return (
    <main className="pw-error-page" id="main-content">
      <section className="pw-error-panel" role="alert">
        <Icon name="error" size="xl" />
        <h1>页面暂时无法显示</h1>
        <p>本次操作未完成。请刷新页面；若问题持续，请联系平台管理员。</p>
        <dl className="pw-error-details">
          <div>
            <dt>错误码</dt>
            <dd>{publicError.code}</dd>
          </div>
          {publicError.requestId === undefined ? null : (
            <div>
              <dt>Request ID</dt>
              <dd>
                <code>{publicError.requestId}</code>
                <CopyControl
                  label="复制 Request ID"
                  value={publicError.requestId}
                />
              </dd>
            </div>
          )}
        </dl>
        <Button
          onClick={() => {
            window.location.reload();
          }}
          variant="primary"
        >
          刷新页面
        </Button>
      </section>
    </main>
  );
}
