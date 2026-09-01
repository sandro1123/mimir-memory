# Changelog · 变更日志

> All notable changes to Mímir are documented here.
> Mímir 的所有重要变更记录于此。
> English · 中文双语

---

## v12.1.2 — 2026-09-01 · 写路径注册表半通电补全 (Write-Path Registry Completion)

### Fixed · 修复
- **静态集消费者全迁动态注册表（部署实弹验收发现）** — v12.1.1 通电了 `validate_agent_id()` 半边，但 6 处写路径消费者仍查静态 `AGENT_IDS`/`DOMAINS` frozenset：quantstar 经 config federation 注册后仍被写路径拒收（生产活进程铁证：`POST /v8/facts` 422 `invalid owner_principal: quantstar`；DB 中 quantstar 存量 15 条系旧热修时代写入，新写入被堵）。修复：全部迁移到 `get_registered_agents()`/`get_registered_domains()`——
  - `schema.py` `CreateFact.validated()`（owner+domain 两查，POST /v8/facts 主写路径）
  - `learning.py` `remember()`（显式记忆摄入，agent+domain）
  - `core_memory.py` `promote()`（canonical 晋升闸门）
  - `api.py` `crystal_approve`（回落 mentor 前先查动态集——注册 agent 不再被静默改派）
  - `knowledge.py` `create_item`（domain 闸门，grep 复查新发现）
  - `evaluator.py` domain 白名单（`ALLOWED_DOMAINS = frozenset(DOMAINS)` 导入期冻结——改为 `_allowed_domains()` 每次评估现算，grep 复查新发现）
  负向守护：未注册 agent/domain 依旧拒收不变。11 项新测试（tests/test_p21_federation_write_paths.py），含「消费者模块不得 import 静态集」源级总闸。

### Changed · 变更
- **版本号 12.1.1 → 12.1.2**（`mimir_v8/schema.py` MIMIR_VERSION + `pyproject.toml` + test_r8_release 断言）。tag v12.1.1 已推公共远端不重写，热修以独立 tag v12.1.2 锚定部署树。

---

## v12.1.1 — 2026-09-01 · 动态注册表通电 (Federation Registry Wiring)

### Fixed · 修复
- **动态注册表通电（部署前审计发现）** — `register_agent()/register_domain()`（v12.1.0 任务1，d91b0fc）建成后全库零调用点（教科书式「建了没通电」），部署 v12.1.0 将使 quantstar（生产硬编码热修注册）写入直接校验失败。修复：`config.py` 新增 `load_federation_registry()`——读 `mimir_config.yaml` 可选 `federation.agents/domains` 段逐项注册；缺文件/缺段静默跳过（worker 无 config 可跑），结构错 ValueError 决不静默（铁律#12）。挂点双入口：`worker.main` 与 `runtime.build_runtime`，server/worker 进程全覆盖。8 项新测试（tests/test_p20_federation_bootstrap.py），注册表模块级集合有快照/还原隔离。

### Changed · 变更
- **版本号 12.1.0 → 12.1.1**（`mimir_v8/schema.py` MIMIR_VERSION + `pyproject.toml` + test_r8_release 断言）。tag v12.1.0 不重打（公共远端已发布），热修以独立 tag v12.1.1 锚定部署树。

---

## v12.1.0 — 2026-08-31 · Eval 安全网 (Eval Safety Net)

### Added · 新增
- **Mímir-Eval 评测套件** — `mimir_v8/eval_suite.py`。纯函数指标层 + 种子化合成基准。
  - 检索指标：`hit_rate@K`（查询级：任一 ground-truth 进 top-K 即 1.0）与 `mrr`（仅首个命中：1/rank，未命中 0.0）；空 ground_truth 拒绝计分（ValueError）。
  - 抽取指标：precision / recall / F1，集合语义（重复只计一次）。
  - ACL 泄漏率：检索行中未授权占比——生产地板值为 0.0，任何非零值即安全回归。
  - **诚实遥测**：合成基准在报告与摘要两级均盖 `provenance="synthetic"` 章，合成数字永远无法伪装成生产质量；真实地板值在在线金标集（tests/test_r9_eval.py），不在此处。
  - 双语（CJK+latin）12 主题合成语料 + 固定种子可复现；19 项新测试（tests/test_p18_mimir_eval.py）。
- **全源采集统一调度管道** — `worker.collect_all` 升级为配置驱动的源注册表（`collector.sources`：rss / web / vault），单一调度入口跑全部启用源。
  - **Vault (Obsidian) 采集器** — `collectors/vault.py`：扫描 markdown 笔记库转 CollectResult；排除隐藏目录（.obsidian/.git/.trash/.smart-env）与 template.md；幂等键 `vault:<relpath>:<mtime>`（改过的笔记以新版本再采，未动过的去重跳过）。
  - **配置驱动源注册表** — `load_source_registry`；无配置回落旧版 RSS-only 行为（存量部署零影响）；未知源类型抛 ValueError，绝不静默跳过；单源失败隔离进 `results["errors"]` 不中断其他源。
  - **幂等键统一** — RSS `rss:sha256(url|title)`、web `web:sha256(url)`、vault `vault:relpath:mtime`，逐条入库防重。
  - Web 采集错误从 `collect_url` 的静默吞没中上浮到 `results["errors"]`。
  - 8 项新测试（tests/test_p19_ingestion_pipeline.py）。

### Changed · 变更
- **版本号 12.0.2 → 12.1.0**（`mimir_v8/schema.py` MIMIR_VERSION + `pyproject.toml`）。
- （master 先行合入）动态 agent 注册表 d91b0fc——v12.1.0 任务1。

---

## v12.0.2 — 2026-08-18 · 安全与隔离修复 (Security Hardening)

### Security · 安全修复
- **ACL 联邦隔离 (High)**：修复 `store.can_access` 中对所有已认证主体无条件注入 `federated_agents` 角色导致 `shared` 可见性塌陷为 `all` 的缺陷。
- **特权分离 (High)**：移除 `learning.py` 中所有硬编码的 `actor_principal != "sandro"` 特权绕过分支，统一由系统鉴权层控制。
- **越权修复 (Med->High)**：修复 `GET /v10/opinions` 与 `GET /v10/observations` 列表端点未按调用主体做 IDOR 隔离的问题。
- **符号记忆租户隔离 (Med)**：在 `symbolic_blocks` 和 `symbolic_canvases` 表中加入 `owner_principal` 字段并建立索引，API 层强制租户鉴权。
- **Web 采集器 SSRF 防御 (Low)**：为 `WebCollector` 增加协议限制与私有/回环/保留 IP 阻断检查。
- **实体解耦 (Low)**：将 `relevance.py` 中硬编码的私有拓扑与 Agent 名单解耦为通用领域术语。

---

## v12.0.1 — 2026-08-16 · 读侧打通 (Hermes MemoryProvider live)

### Added · 新增
- **Hermes MemoryProvider flat-layout plugin** — `hermes-plugin/mimir_memory_provider/`。
  实现 `agent.memory_provider.MemoryProvider` ABC：`prefetch()` 每轮召回注入，
  `get_tool_schemas()`/`handle_tool_call()` 暴露 4 个工具。写路径保持
  mimir-v9.2-cdc 不变（避免重复抽取）。
- **`/v8/query` 审计** — `execute_query` 每次成功检索写 `audit_log(action='query')`，
  使 `/v12/evolve/report` 的 `audit_query_count` 有真实数据。
- **install.sh 重写** — 安装目录布局 + 配置提示。

### Fixed · 修复
- **插件 search 请求体** — 全部三处 `{"query": ...}` → `{"text": ...}`，修复
  `/v8/query` 422 静默失败。
- **Hermes healthcheck** — gateway 检查从 system scope 改为 user scope。
- **README** — schema 徽章 14 → 18；Roadmap 表更新。

### Deployed · 部署
- `memory.provider: mimir_memory_provider` + `plugins.enabled` 配置更新。
- 4 个 gateway + mimir.service 已重启，查询流与首条 useful 反馈已验证入库。

---

## v12.0.0 (schema 18) — 2026-08-14 · 代号 Insight (借鉴 aiduMEI v18.3)

### Added · 新增
- **M1a Ebbinghaus 三轨遗忘** — `DECAY_TIER_MAP` 细化，新增 `L5_ephemeral`
  （半衰期 7d），降权不删行。
- **M1b Chronos 双时间轴** — facts 增加 `valid_from/valid_to`；过期降权 50%。
- **M1c EvolveMem** — `search_feedback` + `quality_metrics` 表；systemd
  `mimir-v12-evolve` 每 6h 聚合，有用 +0.05 / 无用 -0.05。
- **M1d Hermes MemoryProvider 插件** — 钩子 on_turn_start/on_turn_end/
  before_context_compress/on_memory_update。
- **M2a 召回漏斗** — `QueryKernel.trace()` 五阶段；`POST /v12/search/trace`。
- **M2b Dashboard** — 新增「检索」tab：漏斗可视化 + 质量看板。
- **M3a 冲突消解** — `conflict_resolutions`（schema 16），败方置 disputed 不删行。
- **M3b 技能结晶** — `crystal_candidates`（schema 17），7 天 topic 聚类 ≥3 → 候选，
  人工 approve 才结晶。
- **M3c MCP 扩展** — 27 个工具。
- **M4 包装** — PyPI、Dockerfile 自包含、Obsidian Wikilink 双链、多模态（schema 18）。

### Changed · 变更
- **Version**: 11.0.0 (schema 14) → 12.0.0 (schema 18)
- Tests: 113 → 198 passed + 23 subtests。

---

## v11.0.0 (schema 14) — 2026-08 · 全量升级 (借鉴 TencentDB Agent Memory)

### Added · 新增
- **Symbolic short-term memory**（`symbolic_memory.py`）— Mermaid canvas 卸载引擎。
- **CodeGraph**（`code_symbols`, `code_relations`）— 代码符号索引、调用图、影响分析。
- **v11 API**：`/v11/symbolic/*`, `/v11/code/*`。
- **`/v10/reflect/{topic}`** — 从相关 facts + opinions 合成洞见。
- **`/v10/federation/{peer_hierarchy:path}`** — 跨主体的 ACL 共享搜索。
- **Dashboard**：2 个新 tab（符号、代码）+ Claude 风格重设计。

### Changed · 变更
- **Version**: 10.1.0 → 11.0.0, Schema 13 → 14
- Dashboard frontend 全量重写（CSS 变量、明暗切换、移动端底导航）。

---

## v10.0.0 (schema 13) — 2026-08-11

### Added · 新增
- **Governance pipeline** 进入包内（`mimir_v8/governance.py`）
- **Opinion confidence evolution** — set_opinion 端点 + evolve ±0.1 调整
- **Opinion consolidation** — ≥3 同主题高置信生成 observations
- **fast_track auto-commit** — 审核通过的 provisional 候选经 HTTP 提交
- **Dashboard 代理端点**：/api/opinions, /api/observations, /api/governance/decisions

### Changed · 变更
- QueryKernel 增加 `include_provisional` 参数
- systemd units 从 v9.3.0 → v10.0.0 升级

### Fixed · 修复
- 缓存 key 死键累积、Dashboard 写操作 Bearer token 鉴权、opinions 分组查询

---

## v9.x — 2026-08

- v9.3.0 (schema 12)：全量 v9.0 发布，搜索预览、事实全状态查询
- v9.0.0 (schema 12)：嵌入 → 规范化存储 + 投影 + 首次审计
- v8.1.0 (schema 11)：候选管线「filter-confidence」路径、Jaccard 去重
- v8.0.0：初始事件溯源 API 结构
