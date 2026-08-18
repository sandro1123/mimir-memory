# Changelog · 变更日志

> All notable changes to Mímir are documented here.
> Mímir 的所有重要变更记录于此。
> English · 中文双语

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
