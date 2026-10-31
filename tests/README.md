# Test suites

测试按层组织，测试名带用例编号，便于和失败日志对应。`./dev fast-check` 运行单元层，`./dev ci` 再加上集成与故障测试。

| 目录 | 层级 |
|---|---|
| `engineering/` | 仓库结构、工具入口、构建与依赖方向检查 |
| `unit/` | 纯函数、状态机、权限、公式与错误映射 |
| `property/` | GPU 集合、事件序列与领域不变量 |
| `integration/` | PostgreSQL、SQLite、Lease、Migration 与 SSE |
| `contract/` | 模型源、推理引擎、协议与 Host Agent 适配 |
| `security/` | 权限、越权访问、CSRF、Secret、容器与零正文 |
| `fault/` | 进程被杀、断连、磁盘满、OOM 与状态漂移 |
| `performance/` | Gateway、控制面、WAL、采集与聚合基准 |
| `e2e/`、`accessibility/`、`certification/` | 端到端、无障碍与真机认证用例 |
| `fixtures/` | 录制或构造的测试数据，每份记录来源与净化方法 |
