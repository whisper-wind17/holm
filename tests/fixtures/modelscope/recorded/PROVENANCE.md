# FX-MS-09 — ModelScope Hub 录制响应

按项目安全与测试约束「Fixture 使用 `FX-*` 标识并记录来源、版本和是否待真机复核」。
形状照 `FX-MS-08`（Hugging Face）办，便于 M2-06 与 M2-04 的判断面逐条对照。

## 来源

| 项 | 值 |
|---|---|
| 录制时间 | 2026-09-04 |
| 录制方 | 在 **WSL** 自动录制。M2-14D 于 2026-09-14 在开发容器从官方端点完成在线复核；录制文件本身仍是本机产物 |
| 录制方式 | `tests/fixtures/modelscope/record_fx_ms_09.py`（仅 stdlib + `curl`），每份保存响应头与正文 |
| 真机复核 | **已于 2026-09-14 完成无凭证在线复核**：仓库级 Commit 缺失、不存在 Revision 的 `Files:null`、匿名 `not_found` 及数字 `Code` 判别四项均与录制一致 |
| 凭证 | **全程未使用任何真实凭证。** 请求要么匿名，要么携带一枚**编造的**无效 Token（`ms-0000…0000`，从来不是任何真实凭证）。已按 `ms-`／`Bearer`／`Authorization`／`hf_` 四种形状扫描，零命中 |

## 净化

- 每份头文件第一行原本是本机 HTTP 代理的 `HTTP/1.1 200 Connection established`，已剥离，只留真实应答。
- `Set-Cookie` 的值（`acw_tc`、`csrf_session`、`csrf_token`、`source`）是每会话值，
  本项目不在其上做任何判定，已替换为 `<session-value-redacted>`，**只保留 Cookie 名**。
- LFS 302 的 `auth_key` 是带签名与有效期的 CDN 凭据，**头与正文两处**都替换为 `<signed-value-redacted>`。
  判定用得上的 `Location` 主机名、路径与其余查询参数逐字保留。
- 其余字节逐字未改。`RequestId` 每次请求都不同，**保留原值**——它正是「两份响应是否逐字相同」
  的唯一差异来源，抹掉它会让塌缩显得比实际更彻底。

## 内容与它证明的性质

| 文件 | 请求 | 状态 | 证明 |
|---|---|---:|---|
| `public-repo-metadata` | `GET /api/v1/models/Qwen/Qwen2.5-7B-Instruct` | 200 | `Architectures`、`License`、`StorageSize`（15242807272）有出处；**`Revision` 字段的值是 `"master"`，一个分支名，不是 Commit**；`ModelRevisions` 为 `null` |
| `public-repo-revisions` | `/revisions` | 200 | 分支两条（`master`、一条 `pr/…`），`Tags` 为空数组。**整份响应里没有任何 Commit sha** |
| `public-repo-files-master` | `/repo/files?Revision=master&Recursive=True` | 200 | 15 个文件全部带 `Size`、`Sha256`、`IsLFS`；**每个文件各带自己的 `Revision`（末次提交 sha），15 个文件共 7 个不同值** |
| `public-repo-files-by-partial-sha` | 同上，`Revision=3fe289ea2828843e2d`（**18 位截断 sha**） | 200 | 截断 sha 被接受，返回**另一个**文件集合（14 个，7→4 个不同 sha）——历史某一时刻的树 |
| `public-repo-files-unknown-sha` | 同上，`Revision=deadbeef…`（40 位、合法形状、不存在） | **200** | **`Code:200`、`Success:true`、`Message:"success"`、`Files:null`** |
| `public-repo-files-unknown-branch` | 同上，`Revision=nonexistent-branch-xyz` | **200** | 与上一行**逐字相同**（除 `RequestId`） |
| `public-repo-files-empty-revision` | 同上，`Revision=`（空） | 200 | **与 `master` 的响应正文除 `RequestId` 外逐字节相同**（实测差在第 6013 字节，就是 `RequestId`；与本表其余「逐字相同」的成对录制同一形状）——缺省被静默当成默认分支 |
| `public-repo-config` | `/repo?Revision=master&FilePath=config.json` | 200 | 架构／精度／Context 的正文来源 |
| `small-repo-files-master` | `iic/nlp_structbert_word-segmentation_chinese-base` | 200 | 文件较少（11 个、407 MB）的对照 |
| `missing-repo-metadata-anonymous` | 不存在的仓库，**无**凭证 | **404** | `Code:10010205001`，`Message` 是中文散文「获取模型信息失败，信息：record not found」 |
| `missing-repo-files-anonymous` | 同上的文件树 | **404** | 同一 `Code`，`Message` 换成「获取模型目录树失败，…」 |
| `invalid-credential-missing-repo` | 同一仓库，**带无效凭证** | **404** | 与匿名那份**逐字相同**（除 `RequestId`） |
| `invalid-credential-public-repo` | 无效凭证 → 公有仓库元数据 | **200** | **完整元数据照常返回**——公有读路径直接忽略无效 Token |
| `anonymous-users-info` | 无凭证 → `GET /api/v1/users/info` | **401** | `Code:10010101003`、`Message:"user not logged in"` |
| `invalid-credential-users-info` | 无效凭证 → 同一端点 | **401** | 与上一行**逐字相同**（除 `RequestId`） |
| `public-repo-lfs-redirect` | `GET /repo?…FilePath=model-00001-of-00004.safetensors`（`Range: 0-0`） | **302** | 权重经 `cdn-lfs-cn-1.modelscope.cn` 下发，查询串带签名 `auth_key`；**该端点不支持 `HEAD`（返回 404 纯文本）**，逐文件尺寸只能取自 `/repo/files` 的 `Size` |

## 一处跨来源交叉核对

`model-00001-of-00004.safetensors` 在本录制里的 `Size` 是 **3945441440**，
与 `FX-MS-08` 中 Hugging Face 同名文件的 `x-linked-size` **逐位相等**。
两个来源、两次独立录制、同一个数——这条不需要谁去信任谁。

## 四条录制结论

1. **ModelScope 上「不可变 Commit」不是一个仓库级事实。**
   `/revisions` 只有分支与标签；模型元数据的 `Revision` 字段值就是分支名 `"master"`；
   `/repo/files` 给的是**每个文件各自的末次提交 sha**（15 个文件 7 个不同值）。
   HF 的仓库级 `sha` 在这里没有对应物。因此 M2-04 的「Branch/Tag 经 Hub API 固定到不可变 Commit」
   **不能照搬**——挑一个 `Files[].Revision` 当仓库 Commit 是错的，取最大 `CommittedDate` 也只是启发式。
   M2-06 必须明确区分可变分支与仓库级 Commit，不得推测不可变标识。
2. **不存在的 Revision 返回 `200 / Success:true / Message:"success" / Files:null`。**
   这是本录制最危险的一条：`files = data["Data"]["Files"] or []` 这一句
   会把一个**打错的 Revision** 变成「解析成功、文件清单为空」，
   而所有正例照绿、没有任何东西会失败。空 `Revision` 更进一步——静默回落到默认分支。
   与 M2-03 的失败语义、M2-04A 的「未识别不推测填充」同形：**`Files: null` 必须是显式失败，
   不是空列表**；`null` 与「真的是一个空仓库」也必须可区分。
3. **`not_found` 在本来源上无凭证即可观测——与 Hugging Face 相反。**
   HF 对不存在的仓库匿名返回 **401**（防枚举），带有效凭证才 404；
   ModelScope 匿名直接 **404 + `Code:10010205001`**，且带无效凭证的那份逐字相同。
   因此 **M2-04B 的「矩阵按有无凭证分两半、`not_found` 只在有凭证的一半可达」
   是 Hugging Face 的性质，不是来源无关的性质**，M2-06 不得照抄。
4. **判别依据是响应体里的数字 `Code`，不是 `Message`。**
   `Message` 是中文散文（「获取模型信息失败，信息：record not found」），
   按项目安全与测试约束「不泄露原始来源响应」，**一个字都不得进入**响应体、结构化日志、
   审计详情与异常文本。已观测到的 `Code`：`200`（成功）、`10010205001`（record not found）、
   `10010101003`（user not logged in）、`10010102001`（参数错误）。
   注意 `Code` 与 HTTP 状态**不是同一层**：`Code:200` 出现在一个 `Files:null` 的响应里。

## M2-14D 在线校正（2026-09-14）

- 开发容器直连官方 ModelScope 端点复核了上述四条性质，结果与录制一致，无需改动录制正文。
- 生产 `snapshot_download` 已将 `BAAI/bge-small-zh-v1.5` 的 14 个文件（含 `1_Pooling/config.json`）从空 staging 下载并发布为 `Ready`；线上清单、投递计数与发布 Manifest 均为 **192250194 字节 / 14 文件**。
- TC-DL-27 用的 sentinel 是专用于泄漏扫描的编造值，**不是有效 ModelScope Token**；因此它不关闭下表三个需有效凭证的缺口。

## 已知缺口（不得为让用例变绿而在夹具里合成）

| 缺什么 | 为什么缺 | 谁来补 |
|---|---|---|
| **带有效凭证的 `GET /api/v1/users/info` 成功响应** | `BLOCKED_ENV`：录制方与 M2-14D 实施方均没有 ModelScope 账号与 Token | 人工在可出网侧补录。**「凭证有效」的唯一正证据就是它，不得用「解析一个公有仓库成功」冒充**（与 M2-04B 的凭证验证语义 同形） |
| **私有／门控仓库的匿名与授权两面** | `BLOCKED_ENV`：未找到确定处于门控状态且可指认的公开仓库；私有仓库需账号才能创建 | 在补录之前，「私有存在」与「不存在」在本来源上是否塌缩**未经实测，不得假定** |
| **带有效凭证访问不存在的仓库** | `BLOCKED_ENV`：同上 | HF 在这一格是 404，ModelScope 匿名已经是 404，两者是否仍有差别未知 |

## 重新录制

```
python3 tests/fixtures/modelscope/record_fx_ms_09.py   # 本机 WSL 执行，需可访问 modelscope.cn
```

脚本只用 stdlib 与 `curl`，不装任何依赖，不使用任何凭证，**必须在仓库根目录执行**。
`CommittedDate`、`RequestId`、`Set-Cookie` 会随时间变化——**重录产生 diff 是正常的**；
判定用得上的字段（`Code`、`Files`、`Size`、`Sha256`、HTTP 状态）不应变。
