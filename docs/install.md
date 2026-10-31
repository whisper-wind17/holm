# 安装指南

本文介绍如何在一台 GPU 服务器上从源码构建并安装 Holm v0.1.0-rc.1，以及证书配置、客户端信任和卸载。

- [运行要求](#运行要求)
- [安装步骤](#安装步骤)
- [用 AI 编程助手安装](#用-ai-编程助手安装)
- [构建参数](#构建参数)
- [TLS 证书](#tls-证书)
- [信任内部根证书](#信任内部根证书)
- [卸载](#卸载)

## 运行要求

| 项 | 要求 |
|---|---|
| 操作系统 | Ubuntu Server 22.04 LTS 或 24.04 LTS，x86_64，systemd |
| GPU | NVIDIA GPU，驱动不低于 `580.65.06` |
| 容器 | Docker Engine、Docker Compose Plugin、NVIDIA Container Toolkit |
| 构建 | Python 3.12；能访问 Docker Hub、nodejs.org、PyPI 和 npm；不需要安装 uv 或 Node |
| 磁盘 | 构建输出目录和 Docker 存储各需至少 80 GiB 可用空间 |
| 时钟 | 建议开启时钟同步，未同步时安装器会警告 |

> [!NOTE]
> 安装器只检查、不安装：缺少上述软件时会停止并提示，不会自动安装软件包，也不会替换 NVIDIA 驱动。

## 安装步骤

以下命令都在仓库根目录执行。构建和安装只支持 Python 3.12，所以命令里写明 `python3.12`：Ubuntu 24.04 自带；22.04 默认是 3.10，需另装 3.12。

安装器命令是 `holm`：安装前在仓库的 `deploy/installer/holm`，安装后会复制到 `/opt/holm/holm`，卸载时用后者。

**1. 获取源码**

```bash
git clone https://github.com/whisper-wind17/holm.git
cd holm
```

**2. 构建**

```bash
sudo python3.12 deploy/build/holm-build --public-url https://<主机>
```

`--public-url` 是用户访问 Holm 的地址。其他参数见[构建参数](#构建参数)，证书模式见 [TLS 证书](#tls-证书)。

**3. 安装**

```bash
sudo python3.12 deploy/installer/holm install --config build/release/install.json
```

**4. 设置管理员**

安装结束时，终端会显示一条初始化链接，**30 分钟内**打开它设置管理员账号。

- 链接含一次性口令，只显示在终端上，不写入安装日志。请勿转发或保存。
- 没有交互终端时，口令保存在 `/etc/holm/secrets/bootstrap-token`（仅 root 可读），在浏览器打开 `https://<主机>/setup#token=<口令>` 即可。
- 重新执行安装不会生成新口令。

> [!IMPORTANT]
> 默认的 `internal` 证书只适合试用和隔离内网。客户端须先[信任内部根证书](#信任内部根证书)才能访问；正式使用请选 `provided` 或 `acme`。

源码安装生成的 Manifest 未签名，也不提供离线安装包。

## 用 AI 编程助手安装

可以让 Claude Code、Codex 等 AI 编程助手代为执行上面的步骤。在服务器上打开助手，发送：

```text
克隆 https://github.com/whisper-wind17/holm.git，完整阅读仓库里的 docs/install.md，严格遵守其中「给 AI 助手的规则」，帮我在这台服务器上安装 Holm。
```

助手需要能执行 `sudo`：可以给当前用户配置免密 sudo，也可以在助手请求时由你在终端手动执行命令。

### 给 AI 助手的规则

如果你是正在帮用户安装 Holm 的 AI 助手，请遵守以下规则：

1. **先读全文**：在克隆下来的仓库里完整阅读本文（不要只读网页摘要），按「安装步骤」顺序执行，不要跳步或改用其他安装方式。
2. **先查环境**：逐项检查[运行要求](#运行要求)，包括 `python3.12 --version`，把结果列给用户。缺少软件时，说明需要安装什么、怎么装，经用户同意后再装。**不要升级、降级或替换 NVIDIA 驱动。**
3. **先问参数**：构建前向用户确认访问地址 `--public-url`、证书模式（推荐顺序见 [TLS 证书](#tls-证书)），以及是否需要[镜像源](#镜像源)。不要自行猜测。
4. **说明 sudo 命令**：每条 `sudo` 命令执行前，先告诉用户这条命令要做什么。
5. **不接触初始化口令**：安装器只把初始化链接写到终端设备，你的输出里通常看不到，没有终端时只会显示 `bootstrap=valid delivery=root_only_file`。不要读取、输出或记录 `/etc/holm/secrets/bootstrap-token` 的内容，也不要拼出含口令的链接。请用户在自己的终端执行 `sudo cat /etc/holm/secrets/bootstrap-token`，再在浏览器打开 `https://<主机>/setup#token=<口令>`，30 分钟内完成管理员设置。
6. **出错就停**：任何一步失败都要停下，把完整报错交给用户。不要删除数据目录，不要执行 `uninstall --purge-data` 或 `--purge-images`，不要关闭证书校验。
7. **收尾**：告诉用户访问地址、所选证书模式；如果是 `internal` 模式，告诉用户安装输出中的根证书路径和 SHA-256 指纹，并指引用户阅读[信任内部根证书](#信任内部根证书)。

## 构建参数

| 参数 | 说明 | 默认值 |
|---|---|---|
| `--public-url <URL>` | 用户访问地址（必填） | — |
| `--tls internal\|acme\|provided` | 证书模式，见 [TLS 证书](#tls-证书) | `internal` |
| `--base-registry <镜像源>` | 基础镜像源，例如 `docker.1ms.run` | `docker.io` |
| `--output <目录>` | 构建输出目录；目录已存在时拒绝，加 `--force-output` 覆盖配置与 Manifest | `build/release/` |
| `--edge-address <IPv4>` | 监听地址 | `0.0.0.0` |
| `--http-port`、`--https-port` | 监听端口；`--public-url` 中的端口须与 HTTPS 端口一致 | `80`、`443` |
| `--external-model-root <绝对路径>` | 服务器上已有模型的目录，平台只读挂载 | `/var/lib/holm/external-models` |
| `--no-systemd` | 不设开机启动 | 设开机启动 |

### 镜像源

访问 Docker Hub、PyPI、npm 或 Debian 软件源较慢时，可以改用镜像源。基础镜像用 `--base-registry` 指定；其他四项用环境变量传给构建容器：

| 环境变量 | 用途 | 示例 |
|---|---|---|
| `UV_DEFAULT_INDEX` | PyPI 镜像 | `https://pypi.tuna.tsinghua.edu.cn/simple` |
| `npm_config_registry` | npm 镜像 | `https://registry.npmmirror.com` |
| `APT_MIRROR` | Debian 软件源 | `http://mirrors.tuna.tsinghua.edu.cn/debian` |
| `APT_SECURITY_MIRROR` | Debian 安全更新源 | `http://mirrors.tuna.tsinghua.edu.cn/debian-security` |

`sudo` 默认不传递环境变量，所以要把变量写在 `sudo env` 后面：

```bash
sudo env UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple \
  npm_config_registry=https://registry.npmmirror.com \
  APT_MIRROR=http://mirrors.tuna.tsinghua.edu.cn/debian \
  APT_SECURITY_MIRROR=http://mirrors.tuna.tsinghua.edu.cn/debian-security \
  python3.12 deploy/build/holm-build --base-registry docker.1ms.run --public-url https://<主机>
```

镜像源地址只能是 http 或 https，不能带用户名和密码；锁文件里记录的下载地址仍需可访问。

## TLS 证书

证书模式在构建时选定，写入 `install.json` 的 `edge` 段。组织内部使用时，建议按以下顺序选择：

| 推荐顺序 | 模式 | 适用场景 | 构建参数 |
|---|---|---|---|
| 1 | `provided`：组织 CA 签发的证书 | 公司电脑已通过组策略或 MDM 信任组织 CA，客户端无需任何操作 | `--tls provided --tls-certificate <证书路径> --tls-private-key <私钥路径>` |
| 2 | `acme`：公网受信证书 | 服务器有可解析的域名，且公网能访问到本机 | `--tls acme --acme-email admin@example.com`（邮箱可省略） |
| 3 | `internal`：内部 CA（默认） | 试用、隔离内网 | `--tls internal` |

### provided

- 证书与私钥须是安装宿主上的普通文件，使用绝对路径，不能经过符号链接。
- 证书为 PEM 格式，叶子证书在第一位，SAN 须匹配访问地址，且在有效期内。
- 私钥为未加密的 PEM，与证书匹配，属主为 root，组和其他用户无任何权限。
- 构建只记录路径，不读取私钥。安装时校验后复制到 `<数据目录>/caddy/tls/`，只有 Edge 以只读方式挂载；距到期不足 30 天时会警告。
- 客户端仍须信任签发证书的 CA。

### acme

由 Caddy 自动签发和续期。要求公网能访问本机的 80 或 443 端口之一，并允许出站访问。内网服务器多半不满足这个条件。

### internal

开箱即用，但每台客户端都要信任安装器生成的根证书，可由 IT 通过组策略或 MDM 统一下发。部分 Electron 客户端即使信任了根证书也无法使用，这时须改用受信证书。

### 更换证书

- **替换 provided 证书**：在原路径替换证书和私钥文件，执行默认卸载，再用原发行目录和**同一配置**重新安装。默认卸载保留数据，新证书校验通过后才替换副本。安装器从不修改或删除你提供的源文件。
- **切换证书模式**：v0.1 不支持保留数据切换，只能 `uninstall --purge-data` 后全新安装。

> [!CAUTION]
> `--purge-data` 会清除全部数据。

## 信任内部根证书

使用 `internal` 模式时，只信任根证书 `root.crt`，不要信任中间证书（中间证书约每 7 天轮换）。安装器不会修改客户端的信任库。

### 1. 在服务器上导出根证书并核对指纹

安装输出会给出根证书路径（`ca_certificate`）和 SHA-256 指纹。以默认数据目录为例：

```bash
sudo cat /var/lib/holm/caddy/data/caddy/pki/authorities/local/root.crt > holm-root.crt
openssl x509 -in holm-root.crt -noout -fingerprint -sha256
```

等号右侧的指纹须与安装输出的 `sha256` 完全一致。通过可信渠道把 `holm-root.crt` 和指纹一起分发；客户端导入前再核对一次，不一致就停止导入。只拷贝根证书，不要拷贝中间证书或任何私钥。

### 2. 在客户端信任根证书

把下表中的路径换成实际的绝对路径，完成后重启应用。

| 客户端 | 操作 |
|---|---|
| Windows | 运行 `certmgr.msc`，在「受信任的根证书颁发机构 → 证书」上选择「所有任务 → 导入」。全机信任须由管理员在本地计算机证书管理器中导入 |
| macOS | 在「钥匙串访问」中把证书导入「系统」钥匙串，打开证书的「信任」，将 SSL 设为「始终信任」 |
| Ubuntu | `sudo cp holm-root.crt /usr/local/share/ca-certificates/ && sudo update-ca-certificates` |
| Node | 启动前设置 `NODE_EXTRA_CA_CERTS=/path/to/holm-root.crt`（PowerShell：`$env:NODE_EXTRA_CA_CERTS = 'C:\path\to\holm-root.crt'`） |
| Python | 标准库 SSL 用 `SSL_CERT_FILE`，Requests 用 `REQUESTS_CA_BUNDLE`，指向证书文件 |
| curl | 每次请求加 `--cacert /path/to/holm-root.crt` |

- Python 的这两个变量会替换默认的 CA 集合。如果还要访问公网，请指向一个同时包含系统根证书和 Holm 根证书的 bundle；SDK 自带 TLS 配置时，以 SDK 的设置为准。
- 部分 Node／Electron 客户端既不读系统证书库，也不读 `NODE_EXTRA_CA_CERTS`，无法使用 `internal` 证书，须改用受信证书。
- 执行 `--purge-data` 后根证书会重新生成，须重新核对指纹，并在客户端信任新的根证书。

> [!WARNING]
> 不要关闭客户端的证书校验来绕过上述限制。

## 卸载

```bash
sudo /opt/holm/holm uninstall
```

默认卸载只移除程序、容器和 systemd 服务，**保留**数据、证书、密钥、系统用户和镜像；用原发行目录和同一配置重新安装即可恢复。

卸载后 `/opt/holm/holm` 已不存在，之后的命令改用仓库里的 `deploy/installer/holm`：

| 命令 | 作用 |
|---|---|
| `sudo python3.12 deploy/installer/holm uninstall --purge-data` | 同时删除数据，须在终端输入 `purge` 确认 |
| `sudo python3.12 deploy/installer/holm uninstall --purge-images` | 同时删除未被容器引用的发行镜像 |

`--purge-data` 会删除 `/var/lib/holm/models` 中下载的模型，确认前会提示总大小和外部模型目录的位置。需要保留的模型，请先移到外部模型目录，重装后按「本地目录」重新登记。外部模型目录在任何情况下都不会被删除。
