# Applications

可独立运行或打包的进程。应用可以依赖 `packages/`，共享业务逻辑不放回应用目录。

| 目录 | 职责 |
|---|---|
| `control_api/` | 管理 API、会话、权限、SSE 推送与系统状态查询 |
| `gateway/` | OpenAI／Anthropic 兼容接入、API 密钥鉴权、路由、流式代理与调用记录 |
| `workers/download/` | 模型下载、暂停续传与产物发布 |
| `workers/instance/` | 模型实例的启动、停止与状态收敛 |
| `workers/ledger/` | 调用记录写入 |
| `workers/maintenance/` | 数据保留与定期清理 |
| `host_agent/` | 宿主机上的受限代理，负责 Docker 与 GPU 操作 |
| `web/` | React 管理界面与成员界面 |

`workers/telemetry/` 为预留目录，当前为空。
