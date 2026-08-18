# Changelog

## v12.0.1 — 2026-08-16 · 读侧打通 (Hermes MemoryProvider live)

### Added
- **Hermes MemoryProvider flat-layout plugin** — `hermes-plugin/mimir_memory_provider/` (__init__.py + tools.py + plugin.yaml). 实现 `agent.memory_provider.MemoryProvider` ABC: `prefetch()` 每轮召回注入, `get_tool_schemas()`/`handle_tool_call()` 暴露 4 个工具; loader 于 `$HERMES_HOME/plugins/<name>/` 发现. 写路径保持 mimir-v9.2-cdc 不变 (避免重复抽取).
- **`/v8/query` 审计** — `execute_query` 每次成功检索写 `audit_log(action='query')`, 使 `/v12/evolve/report` 的 `audit_query_count` 有真实数据; 检索失败不影响响应 (try/except fail-open).
- **install.sh 重写** — 安装目录布局 + 配置提示 (memory.provider=mimir_memory_provider + plugins.enabled), 单文件拷贝不再有效.

### Fixed
- **插件 search 请求体** — 全部三处 (flat plugin tools.py / legacy facade plugins/memory/.../tools.py / hermes-plugin legacy) `{"query": ...}` → `{"text": ...}`, 修复 `/v8/query` 422 request_validation_error 静默失败.
- **Hermes healthcheck** — `/usr/local/lib/hermes-ops/healthcheck.sh`: gateway 检查从 system scope 改为 user scope (`check_user_unit hermes-gateway.service`), 修复每 5 分钟 exit 1.
- **README** — schema 徽章 14 → 18; Roadmap 表更新 (v11/v12 shipped).

### Deployed
- `~/.hermes/config.yaml` + profiles/{mentor,jarvis}: `memory.provider: mimir_memory_provider`, `plugins.enabled` mnemosyne → mimir_memory_provider (原配置已备份 .bak.mimir-provider-*).
- 4 个 gateway (default/mentor/jarvis/quantmaster) + mimir.service 已重启; 查询流与首条 useful 反馈已验证入库.

## v12.0.0 (schema 18) — 2026-08-14 · 代号 Insight (借鉴 aiduMEI v18.3)

### Added
- **M1a Ebbinghaus 三轨遗忘** — `DECAY_TIER_MAP` 细化, 新增 `L5_ephemeral` (半衰期 7d), `decay.py` 按 `exp(-ln2*days/half_life)` 降权, 降权不删行.
- **M1b Chronos 双时间轴** — facts 增加 `valid_from/valid_to`; 过期查询降权 50%, 未生效排最后, 铁律类 valid_to=NULL; schema 15 迁移.
- **M1c EvolveMem** — `search_feedback` + `quality_metrics` 表; `/v12/evolve/feedback` + `/v12/evolve/report`; systemd `mimir-v12-evolve` 每 6h 聚合, 有用 +0.05 / 无用 -0.05, 调权走 opinions + 审计.
- **M1d Hermes MemoryProvider 插件** — `~/.hermes/plugins/memory/mimir_memory_provider/` (plugin.yaml+provider.py+tools.py), 钩子 on_turn_start/on_turn_end/before_context_compress/on_memory_update; 工具 mimir_search/remember/recent/reflect.
- **M2a 召回漏斗** — `QueryKernel.trace()` 五阶段 (RelevanceGate→CandidatePool→JaccardDedup→ChronosDecay→TopK); `POST /v12/search/trace`.
- **M2b Dashboard** — 新增「检索」tab: 漏斗可视化 + `/api/search/trace` 代理 + `/api/quality` 质量看板.
- **M3a 冲突消解** — `conflict_resolutions` (schema 16); `ConflictService` detect/resolve/dismiss, 失败方置 disputed 不删行; `/v12/conflicts/*`.
- **M3b 技能结晶** — `crystal_candidates` (schema 17); `CrystalService` scan/list/approve/dismiss, 7 天 topic 聚类 ≥3 次→候选, 人工 approve 才结晶为 pattern 事实; systemd `mimir-v12-crystallize` 每日 0:30; Dashboard「检索」tab 附带候选管理.
- **M3c MCP 扩展** — 27 个工具: Core CRUD + Facts + Reflect + Evolve(2) + Conflict(4) + Crystal(4) + Trace + CoreMemory + Multi-modal(2).
- **M4 包装** — PyPI `pyproject.toml` (wheel/sdist 已验证; 4 scripts: mimir-server/worker/migrate/cli); Dockerfile 修复为自包含 (ghcr tag); **Obsidian Wikilink 双链** (`wikilink.py`: stable note slugs, forward `[[links]]`, Backlinks section); **多模态** (`fact_assets` schema 18; `MultiModalService` attach/list + 政策校验 + Obsidian `![[embed]]`).

### Changed
- **Version**: 11.0.0 (schema 14) → 12.0.0 (schema 18)
- `store.create_fact` 增加可选 `connection` 参数以支持晶体物化在同事务.
- `migration._additive_chain(source)` 条件化 V11..V18 链条, 支持 9..17 单步升到 runtime schema.
- Tests: 113 → 198 passed + 23 subtests, 目标 150+ 达成.

### v11.0 Upgrade — 全量升级 (借鉴 TencentDB Agent Memory 设计理念)

### Added
- **Symbolic short-term memory** (`symbolic_memory.py`) — Mermaid canvas offload engine for tool logs, step-wise summaries, node_id drill-down recall. Inspired by TencentDB Agent Memory's symbolic short-term memory pattern.
- **CodeGraph** (`code_symbols`, `code_relations` tables) — code symbol index, call graph, impact analysis (callers/callees) via API.
- **v11 API endpoints**: `/v11/symbolic/offload`, `/v11/symbolic/canvas`, `/v11/symbolic/{node_id}` (recall), `/v11/code/search`, `/v11/code/impact/{symbol_id}`.
- **`/v10/reflect/{topic}`** — synthesized insight from related facts + opinions (replaces placeholder).
- **`/v10/federation/{peer_hierarchy:path}`** — cross-principal shared search with ACL enforcement (replaces placeholder).
- **`CanonicalStore.write_audit()`** — one-shot audit trail helper for reflect/federation.
- **V14 migration** (`migrate_schema_v14`) — schema 13 → 14, additive (symbolic_blocks, symbolic_canvases, code_symbols, code_relations).
- **Dashboard**: 2 new tabs (符号, 代码) + Claude-style redesign (warm cream/charcoal palette, serif headings, coral accent, dark/light mode, safe-area mobile nav).

### Changed
- **Version**: 10.1.0 → 11.0.0, Schema 13 → 14
- **Dashboard frontend**: full rewrite in Claude-style (CSS variables, dark/light toggle, proper mobile bottom nav, all 7 original tabs + 2 new v11 tabs).
- **Dashboard backend**: fixed `api_source_add` duplicate function (broken endpoint), added v11 proxy routes.
- **`coalesce.py`**: fixed duplicate `CoalesceBatch` class, `os._common_substring` → `os_common_substring`, deprecated query, removed dead code.
- **`migration.py`**: `migrate_schema` version gate updated for schema 14 compatibility.
- **Tests**: 113 tests pass (0 failed), updated stale version assertions, added pytest to requirements.

### Fixed
- Dashboard opinions tab: escaped HTML quotes (`\"` → `"`) — tab was silently broken.
- `/api/source/add` endpoint: duplicate def caused null return.
- `docs/*.md` bad file removed.
- All 7 systemd timers result=success (no 203/EXEC).

### Inspiration
- TencentDB Agent Memory (MIT) — symbolic short-term memory (Mermaid canvas + offload) and CodeGraph concepts.

## v10.0.0 (schema 13) — 2026-08-11

### Added
- **Governance pipeline** now in-package (mimir_v8/governance.py)
- **Opinion confidence evolution** — set_opinion endpoint + evolve action with ±0.1 adjustment per signal
- **Opinion consolidation** — consolidate_observations produces observations when ≥3 same-topic with high confidence
- **fast_track auto-commit** for reviewed provisional candidates via HTTP
- **Dashboard proxy endpoints**: /api/opinions, /api/observations, /api/governance/decisions
- **New systemd services:** Mimir v10 governance pipeline, v10 consolidation module

### Changed
- QueryKernel now includes `include_provisional` parameter (searches provisional facts)
- Provisional path no longer blocked by filter check
- Dashboard UI tags and auto-refresh fixes (cache key fix, front-end auth)
- Systemd systemd units all upgrade from v9.3.0 → v10.0.0-20260811_104554

### Fixed
- Dead key accumulation in cache with `_cache_key(params) + prefix` separation
- Dashboard write operations now use bearer token auth reject 401
- `opinions` native table sequences now support group by topic per topic

## v9.3.0 (schema 12) — 2026-08-07

- Full v9.0 release with search preview, facts query all statuses
- 已 standalone governance pipeline with dashboard audit flow
- 修改助理投票 sqlite3.ProgrammingError 语句 patch in知道自己 store.py

## v9.0.0 (schema 12) — 2026-08-05
- Embedding to canonical + projections + first compass audit
- v9 production رغم knowledge topology (memory/learning/wiki)

## v8.1.0 (schema 11) — 2026-08-03
- ... distributed from prior markdown from state HA negligibly a candidates source_category enum
- Candidate pipeline "filter-confidence" 路径 incomplete (custom detection for dedup Jaccard)

## v8.0.0 — 2026-08-02
- Initial Fedevent sourcing API structure