<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/assets/holm-mark-dark.svg">
  <img src="docs/assets/holm-mark-light.svg" alt="Holm" width="96" height="96">
</picture>

<h1>Holm</h1>

<p><strong>把一台 GPU 服务器变成团队自己的私有 AI 服务平台</strong></p>

<p>
  <a href="docs/install.md">安装指南</a> ·
  <a href="#调用示例">调用示例</a> ·
  <a href="#预览版限制">预览版限制</a> ·
  <a href="README.en.md">English</a>
</p>

<p>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-Apache--2.0-blue" alt="License: Apache-2.0"></a>
  <img src="https://img.shields.io/badge/version-v0.1.0--rc.1-orange" alt="Version: v0.1.0-rc.1">
  <img src="https://img.shields.io/badge/platform-Linux%20%2B%20NVIDIA%20GPU-76b900" alt="Platform: Linux + NVIDIA GPU">
</p>

</div>

管理员在网页上下载模型、部署到 GPU、发布成 AI 服务并给成员授权；成员拿自己的 API 密钥，用 OpenAI 或 Anthropic 兼容接口调用，Claude Code 等工具可以直接接入。

> [!NOTE]
> v0.1.0-rc.1 是预览版，适合在测试环境试用。

## 功能

- **模型**：从 Hugging Face、ModelScope 下载，支持暂停与续传；也可登记服务器上已有的模型目录，只读使用。
- **部署**：用 vLLM 或 SGLang 部署到指定 GPU，支持多卡张量并行。
- **AI 服务**：把模型发布为统一 Endpoint；更换后端实例，调用方无感知。
- **用户与密钥**：按服务授权；密钥只在创建时显示一次，可设到期、可撤销。
- **调用上限**：按「用户 × 服务」限制同时请求数和每分钟请求数。
- **兼容接口**：OpenAI（`/v1/chat/completions` 等）与 Anthropic（`/v1/messages`），支持流式、工具调用和思考。
- **用量与监控**：调用记录、Token 用量统计、GPU 与服务监控。
- **隐私**：请求与回答正文只在转发时存在于内存，不落盘；调用记录只存元数据和 Token 数。

## 截图

<p align="center"><img src="docs/images/overview.png" alt="总览页" width="860"></p>
<p align="center"><sub>总览：AI 服务、GPU、请求与今日用量</sub></p>

<p align="center"><img src="docs/images/deploy-gpu.png" alt="部署模型时选择 GPU" width="860"></p>
<p align="center"><sub>部署模型：选择 GPU，支持多卡</sub></p>

<p align="center"><img src="docs/images/ai-assistant.png" alt="WorkBuddy 接入 Holm 的模型配置与对话" width="860"></p>
<p align="center"><sub>AI 编程助手接入私有模型（WorkBuddy）</sub></p>

## 与 Ollama 的区别

| | Ollama | Holm |
|---|---|---|
| 面向 | 个人，在自己的电脑上 | 团队，共用一台 GPU 服务器 |
| 推理引擎 | llama.cpp，以 GGUF 量化模型为主 | vLLM／SGLang，原版权重，多人并发吞吐更高 |
| 多人使用 | 不区分调用者 | 每人一把密钥，授权、限流、统计用量 |
| 管理方式 | 命令行 | 网页 |
| 运行平台 | macOS、Windows、Linux，可纯 CPU | 仅 Linux + NVIDIA GPU |

两者可以同时使用：个人试模型用 Ollama，团队共享用 Holm。

## 快速开始

**运行要求**：Ubuntu Server 22.04／24.04、NVIDIA 驱动 ≥ `580.65.06`、Docker 与 NVIDIA Container Toolkit、Python 3.12，磁盘至少 80 GiB 可用。

安装器命令是 `holm`：安装前在仓库的 `deploy/installer/holm`，安装后位于 `/opt/holm/holm`。构建和安装都须用 Python 3.12 执行。

```bash
git clone https://github.com/whisper-wind17/holm.git && cd holm
sudo python3.12 deploy/build/holm-build --public-url https://<主机>
sudo python3.12 deploy/installer/holm install --config build/release/install.json
```

安装结束时，终端会显示一条初始化链接，**30 分钟内**打开它设置管理员账号。

**用 AI 编程助手安装**：在服务器上打开 Claude Code、Codex 等助手，发送下面这句话。助手会先检查环境、向你确认参数，再逐步安装；初始化口令由你自己查看，不经过助手。

```text
克隆 https://github.com/whisper-wind17/holm.git，完整阅读仓库里的 docs/install.md，严格遵守其中「给 AI 助手的规则」，帮我在这台服务器上安装 Holm。
```

> [!TIP]
> 默认使用内部证书，客户端须先信任根证书。证书模式、构建参数、客户端配置和卸载，见[安装指南](docs/install.md)。

## 调用示例

管理员发布服务并授权后，成员在自己的服务详情页查看 Endpoint 和模型名，并创建 API 密钥。

**OpenAI 兼容**（Base URL 填到 `/v1` 为止）

```bash
curl https://<主机>/v1/chat/completions \
  -H "Authorization: Bearer $HOLM_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "<模型名>", "messages": [{"role": "user", "content": "你好"}]}'
```

**Anthropic 兼容**（以 Claude Code 为例）

```bash
export ANTHROPIC_BASE_URL=https://<主机>
export ANTHROPIC_API_KEY=$HOLM_API_KEY
export ANTHROPIC_MODEL=<模型名>
claude
```

## 预览版限制

- 只支持单台服务器；GPU 按整卡分配，多个模型不能共用一张卡。
- 只提供 API，没有聊天界面；不支持多模态输入。
- 调用上限只有并发数和每分钟请求数，没有 Token 限额。
- 运维命令目前只有 `holm uninstall`，`status`、`logs`、`doctor` 留待后续版本。
- 尚未完成验收：ACME 实际签发与续期、internal 模式全量验证、离线安装、升级与回滚、SBOM 与签名、安全加固扫描、无障碍专项。
- 真机验证只在 NVIDIA L20 上做过，vLLM 和 SGLang 都已完成部署与对话验证。

## 文档

| 文档 | 内容 |
|---|---|
| [安装指南](docs/install.md) | 运行要求、构建参数、TLS 证书、客户端信任、卸载 |
| [更新日志](CHANGELOG.md) | 各版本的新功能、修复与已知限制 |

## 参与开发

Python（uv 工作区）与 TypeScript（`apps/web`）单仓库，统一入口为 `./dev`：

```bash
./dev bootstrap    # 安装依赖：uv ≥ 0.11、Node ^22.13 或 24、npm 10～11
./dev fast-check   # 格式、Lint、类型检查、单元测试
./dev ci           # 完整检查：再加集成测试和故障测试
```

主要目录：`apps/`（Control API、Gateway、Workers、Host Agent、Web）、`packages/`（领域、契约、持久化、协议与 Runtime Adapter、安全）、`migrations/`、`deploy/`、`tests/`。

**安全问题**请不要提交公开 Issue，请在仓库的 Security 页面通过「Report a vulnerability」私下报告。

## 许可证

[Apache License 2.0](LICENSE)。第三方依赖以及随仓库附带的字体、词表沿用各自的许可证，见 [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md)。
