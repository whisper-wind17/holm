# Scripts

开发、验证与发行用的辅助脚本：可重复执行、非交互优先、退出码明确。脚本不得绕过应用的安全边界、内嵌 Secret，或以静默删除作为清理方式。

常用脚本：

- `check_dependencies.py`：检查 `packages → apps` 反向依赖与 Domain 禁止依赖。
- `build_artifacts.py`：在固定构建环境中生成 Python wheel、Web bundle 与 SHA-256 清单。
- `cache_build_dependencies.py`：按 `uv.lock` 下载并校验构建后端 wheel，供离线构建复用。
- `verify_clean_bootstrap.py`、`verify_reproducible_build.py`：在全新工作区验证依赖安装与构建产物可复现。
- `third_party_licenses.py`：生成 `THIRD_PARTY_LICENSES.md`。
- `generate_*_schemas.py`、`generate_agent_proto.py`：生成或校验 API JSON Schema 快照与 Host Agent protobuf 模块。
