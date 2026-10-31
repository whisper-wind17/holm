# Shared packages

共享包只被 `apps/` 依赖。`domain/` 位于依赖图最内层，不依赖 Web、数据库、Docker／NVML 或具体 Adapter；`./dev fast-check` 会检查依赖方向。

| 目录 | 职责 |
|---|---|
| `domain/` | 与框架无关的实体、值对象、状态机、错误码与领域服务 |
| `common/` | 配置 Schema、环境覆盖、Secret 文件读取、UUIDv7、时间与结构化日志 |
| `contracts/` | API、分页、错误、事件与 Host Agent RPC 契约 |
| `protocols/` | OpenAI／Anthropic 协议适配与统一事件模型 |
| `persistence/` | Repository、Unit of Work 与 PostgreSQL 实现 |
| `ledger/` | 调用记录事件模型、本地 SQLite 暂存与序列守卫，不含请求或回答正文 |
| `runtimes/` | vLLM／SGLang 适配与兼容矩阵读取 |
| `telemetry/` | 指标解析、时间窗口与聚合 |
| `security/` | 认证、加密、脱敏与审计工具 |

`downloads/` 为预留目录，当前为空。
