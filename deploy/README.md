# Deployment assets

生产部署所需的全部文件。安装器按发布清单校验它们的版本与 Hash，请通过构建与安装命令使用，不要在服务器上手工修改。

| 目录 | 内容 |
|---|---|
| `build/` | `holm-build`：从源码构建镜像与安装配置 |
| `installer/` | `holm`：安装与卸载 |
| `images/` | 构建器、应用、edge 与推理引擎镜像的 Dockerfile |
| `compose/` | Docker Compose 编排 |
| `caddy/` | 入口反向代理与 TLS 配置 |
| `systemd/`、`tmpfiles.d/` | 宿主机服务单元与运行目录 |
| `verify/` | 真机验证用的辅助脚本 |

使用方法见[安装指南](../docs/install.md)。
