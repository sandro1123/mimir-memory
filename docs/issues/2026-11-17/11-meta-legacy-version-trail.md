# 版本遗迹总账：模块名、路由前缀、环境变量、集合名、测试名（元问题）

**标签**：tech-debt, api, docs
**严重度**：Meta（本身不是漏洞，但正在制造漏洞——见 #09）
**影响版本**：全仓

## 为什么单开一条

这不是"改个名字更好看"。现状已经造成两类实际故障：

1. **#09**：Dockerfile 里的 `MIMIR_V8_COLLECTION=mimir_v12_prod` 过不了代码自己的白名单，默认镜像起不来。
2. **三套部署写三个不同的向量集合**（`mimir_v8_prod_facts` / `mimir_v12_prod` / `mimir_v10_prod_local`），表现为"记忆丢了"。

所以把它作为一条总账提出来，逐项收敛，并给 CI 加门，避免继续漂。

## 实测清单（2026-11-17，`docs/2.0-scope-plan` 分支）

**模块与包名**

- 包目录 `mimir_v8/`，PyPI 名 `mimir-v8`（pyproject），但 `MIMIR_VERSION = "1.7.0"`。
- README 安装命令写的是 `pip install mimir-brain`——与实际包名不符。
- Dashboard 的 `FastAPI(title="Mímir Dashboard", version="4.1.0")`，与 1.7.0 无对应关系。

**路由版本前缀：90 个带版本装饰器，横跨 v8–v16**

- v8 = 41、v9 = 5、v10 = 9、v11 = 5、v12 = 15、v13 = 7、v14 = 4、v15 = 1、v16 = 3。
- 另有 8 个**不带版本**的遗留路由：`/health`、`/ready`、`/bootstrap`、`/stats`、`/awareness`、`/write`、`/delete`、`/core_memory/{agent_id}/inject`。
- 其中三对是同一实现的重复暴露：`/health` ≡ `/v8/health`、`/ready` ≡ `/v8/ready`、`/bootstrap` ≡ `/v15/bootstrap`；而 `/core_memory/...` 是 v7 命名，`/v8/core-memory/...` 是新命名——两种拼写。
- 又有四对是同功能的双路径别名：`/v8/learning/candidates/pending` ≡ `/v8/candidates/pending`、`review` 与 `commit` 各两份。

**环境变量：22 个文件使用 `MIMIR_V8_*`，另有一个 `MIMIR_V9_KNOWLEDGE_LAYERS`**

- `MIMIR_V8_DATA_DIR` / `_TOKEN_FILE` / `_COLLECTION` / `_MODEL` / `_URL` / `_TOKEN` / `_TIMEOUT`，加 `MIMIR_V9_KNOWLEDGE_LAYERS`。
- 用户配置脚本、QUICKSTART、FEDERATION.md、skills/INSTALL.md、init.sh 全都在教这套名字。

**测试名按内部计划编号，而不是行为**

- `test_p0a_…`、`test_p14_…`、`test_p1421_…`、`test_r7_…`、`test_v12_insight.py` 等。
- 新贡献者无法从文件名判断覆盖了什么；`p1421` 里的 1421 是 14.2.1 的缩写，纪元早已结束。

**同一语义的多份实现（不止是名字）**

- ACL 判定了四次：`store.can_access`、`knowledge.can_read`、`core_memory._authorize`、`blackboard._authorize`（各自一套边界语义）。
- 谱系有两套表达：`fact_supersessions` 与 `relations` 的 `supersedes`/`superseded_by` 边（已在 2.0 裁决 G2 中列为归一目标）。
- 健康端点两套（`/health`、`/ready` 与各自 `/v8` 版本），且 Dockerfile 健康检查用 `/ready`、compose 用 `/health`。
- `docs/VERSIONING.md` 的纪元表让读者必须做 `v15 ≡ 1.0.0`、`v16 ≡ 1.1.0`、`v18 ≡ 1.3.0` 的心算。

## 建议的收敛路径（分三步，不要一次大改）

**第一步：加门，冻结现状**

- CI 新增 `scripts/naming_guard.py`：
  - 新路由必须是 `/v1/…` 或不带版本，禁止再新增 `v8…v16` 前缀；
  - 环境变量只允许 `MIMIR_*` 前缀（无版本号）；
  - 集合名默认值全仓唯一；
  - Dockerfile/compose 的每一条集合默认值都要通过 `validate_vector_collection_name`。
- 反向注入：故意写一条 `/v17/foo` 路由 → CI 红。

**第二步：非破坏性改名（1.x 内完成）**

- 环境变量加 `MIMIR_DATA_DIR` / `MIMIR_TOKEN_FILE` / … 作为首选，旧名保留为 alias 并在启动日志打一次性 deprecation 警告。
- 路由加无版本别名（`/facts`、`/query`），旧前缀保留；文档只写新名字。
- PyPI 名与 README 对齐（要么改 README 写 `mimir-v8`，要么 2.0 发行 `mimir`）。
- 测试文件按行为改名（`test_acl_shared_domain.py` 之类），保留旧文件名的 git 历史。

**第三步：2.0 破坏性清理（与 semver 大版本对齐）**

- 模块 `mimir_v8` → `mimir`，附 `import mimir_v8` 兼容 shim 一个版本。
- 删除 v7 兼容路由（`/write`、`/delete`、`/stats`、`/awareness`、`/core_memory/...`）——它们还是 issue #05 的越权入口。
- 删除重复别名路由，只留一套。
- 收敛 ACL 为单一实现（`store.can_access` + 批量版本，建议参考 #07）。

## 验收

- 第一、二步可分别在 1.x 内发布，不破坏任何现有客户端。
- 第三步写进 `docs/superpowers/specs/2026-11-16-mimir-2.0-scope-and-acceptance.md` 的 G4 判据里：`rg '"/v(8|9|1[0-6])/' mimir_v8/` 零命中。

## 说明

本条的清单是静态阅读所得，没有跑任何测试；数字用 `rg` 可复算，命令已写在上文。
