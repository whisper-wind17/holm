# FX-MS-08 — Hugging Face Hub 录制响应

按项目安全与测试约束「Fixture 使用 `FX-*` 标识并记录来源、版本和是否待真机复核」。

## 来源

| 项 | 值 |
|---|---|
| 录制时间 | 2026-09-03 |
| 录制方 | 人工录制（开发容器**无法访问** `huggingface.co`：DNS 返回无效 IPv6，443 直连超时；`hf-mirror.com` 同样超时） |
| 录制方式 | `curl`，经人工侧的 HTTP 代理；每份保存响应头与正文 |
| 真机复核 | **不适用**——这些**就是**真实来源的响应；待复核的是「SDK 版本升级后行为是否变化」，归 M2-14 的认证套件 |
| 凭证 | 录制第 9 份时使用了一枚真实 Token。**Token 不在本目录任何文件内**，已按 `hf_`／`Bearer`／`Authorization`／`api_org_` 四种形状独立扫描，零命中 |

## 净化

`public-repo-file-head.headers` 的 `location` 是一条带签名与有效期的 CDN URL；查询串已替换为
`<signed-query-redacted>`。判定用得上的 `x-linked-size`、`x-linked-etag` 逐字保留。
每份头文件的第一行原本是代理的 `HTTP/1.1 200 Connection established`，已剥离，只留真实应答。

## 内容与它证明的性质

| 文件 | 请求 | 状态 | 证明 |
|---|---|---:|---|
| `public-repo-main` | `GET /api/models/Qwen/Qwen2.5-7B-Instruct/revision/main?blobs=true` | 200 | 分支名 → `sha`；14 个 siblings **全部带 `size`**；`config.architectures`、`safetensors.parameters.BF16`、`cardData.license` 均有出处 |
| `public-repo-by-sha` | 同上，但按解析出的 `sha` | 200 | Branch → Commit 固定是幂等的 |
| `public-repo-config` | `GET /Qwen/…/resolve/<sha>/config.json` | 200 | 架构／精度／Context 的正文来源 |
| `public-repo-file-head` | `HEAD /Qwen/…/resolve/<sha>/model-00001-of-00004.safetensors` | 302 | dry run 的逐文件尺寸：`x-linked-size: 3945441440`、`x-linked-etag` |
| `tiny-repo-main` | `hf-internal-testing/tiny-random-gpt2` | 200 | 文件极少的对照 |
| `gated-repo-metadata` | `meta-llama/Llama-3.1-8B-Instruct` 元数据 | 200 | **门控仓库的元数据是公开可读的**，`gated` 只是 JSON 里的一个字段（值 `"manual"`） |
| `gated-repo-file-denied` | 同一仓库的**文件**，无凭证 | **401** | 真正的拒绝发生在取文件时，且是 401 不是 403；判据是 `x-error-code: GatedRepo` 响应头 |
| `missing-repo-anonymous` | 不存在的仓库，**无**凭证 | **401** | 正文 `{"error":"Invalid username or password."}`——**请求根本没带凭证**，HF 的防枚举设计 |
| `missing-repo-authenticated` | 同一仓库，**带**有效凭证 | **404** | 正文 `{"error":"Repository not found"}` |

## 第二批：凭证无效的可观测性（2026-09-03 补录）

用一枚**编造的**无效 Token（`hf_ZZZZ…`，不是任何真实凭证；经 `curl -K` 配置文件传入，
不进 argv）打五个面：

| 文件 | 请求 | 状态 | 结果 |
|---|---|---:|---|
| `invalid-credential-public-repo` | 坏凭证 → 公有仓库元数据 | **200** | **完整元数据照常返回**——公有读路径直接忽略无效 Token |
| `invalid-credential-gated-file` | 坏凭证 → 门控仓库的文件 | 401 | `x-error-code: GatedRepo`，与**匿名**访问同一文件的响应无法区分 |
| `invalid-credential-missing-repo` | 坏凭证 → 不存在的仓库 | 401 | 正文与匿名访问逐字相同 |
| `invalid-credential-whoami` | 坏凭证 → `/api/whoami-v2` | 401 | `{"error":"Invalid username or password."}` |
| `anonymous-whoami` | 无凭证 → `/api/whoami-v2` | 401 | **与上一行逐字相同** |

## 三条录制结论



1. **`not_found` 只在配了凭证时可观测。** 无凭证时「不存在」与「私有/不可见」都塌缩成同一个 401，
   且正文字面写着「用户名或密码无效」——**而请求没有带任何凭证**。
   把该 401 映射为 `invalid_credential` 会在「管理员把仓库名打错且未配凭证」时把处置方向指向
   「去重配凭证」，完全错误，而所有正例照绿。
2. **判别依据是 `x-error-code` 响应头与 HTTP 状态，不是 `x-error-message`。**
   后者是英文散文，按项目安全与测试约束「不泄露原始来源响应」，一个字都不得进入
   响应体、结构化日志或审计详情。
3. **「凭证无效」在解析路径上根本不可观测，必须由一个要求鉴权的端点判定。**
   无效 Token 打公有仓库返回 **200 和完整元数据**；打门控文件、不存在的仓库、
   `whoami-v2` 全部返回 401，且**与完全不带凭证的响应逐字相同**。
   因此：（a）「凭证错误」「凭证过期」「未提供凭证」三者在本来源上**始终不可区分**；
   （b）**用「解析一个公有仓库成功」来判定凭证有效，是一个必然的假绿**——
   管理员配错 Token 会被告知「验证通过」，随后所有私有仓库下载全部失败。
   凭证验证必须打 `/api/whoami-v2` 一类要求鉴权的端点，且 200 才是「凭证有效」的唯一正证据。

## 已知缺口

`TASKS.md` 的凭证政策要求「挂起项必须**同时**出现在本表与 `TASKS.md` 的移交表，
否则最后一遍集成没有清单可查」。本表列出尚待补录的场景。

| 缺口 | 为什么没有 | 谁来关闭 |
|---|---|---|
| **「有效 Token → `whoami-v2` 返回 200」** | 录制第 9 份时用过一枚真实 Token，但成功响应含账号身份，未纳入本目录；且再录一次需要真实凭证 | **M2-14**。在具备凭证与出网条件时补录。**在此之前不得在假 Hub 里合成一个 200**——那会让「解析冒充验证」的阳性对照永远不会红 |
| **带有效凭证读取门控仓库的文件** | 同上，需要一枚对该仓库有权限的真实 Token | **M2-14**。授权半的真实往返在此之前只能挂起 |
| **429／限流响应** | 两批录制都没有触发限流；真实触发需要可控地打满配额 | **M2-14**。在此之前限流分支只由 `FX-MS-04` 承担稳定结论与「不得记为凭证无效」的断言，**不得报作已在真实来源上跑过** |

**不属于缺口的三件事**（已录全，阳性对照今天就能实测）：无效 Token 打 `whoami-v2` 的 401、
匿名打同一端点的 401（与前者逐字相同）、无效 Token 打公有仓库的 200。
携带安全性质的正是这三份。
