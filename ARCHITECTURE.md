# Mímir Architecture · 架构设计

> 同步版本 · Synced version：v12.0.0 · Schema 18 · 代号 Codename Insight · 2026-08-14
>
> English · 中文双语

---

## 1. Storage (SQLite canonical) · 存储（SQLite 规范化库）

**Core anchor · 核心锚点**：`facts`（25 列）带 `'active'|'tombstoned'|'disputed'|'archived'`
状态；事件溯源用 `memory_events` 追加式、触发器保护。

**Projection fanout · 投影扇出**：`outbox` → vector / FTS / graph / core_memory。

### Immutability and Audit · 不可变性与审计

```
memory_events:
  CREATE TRIGGER memory_events_no_update BEFORE UPDATE ON memory_events BEGIN RAISE(ABORT, 'memory_events are immutable'); END;
  CREATE TRIGGER memory_events_no_delete BEFORE DELETE ON memory_events BEGIN RAISE(ABORT, 'memory_events are immutable'); END;
fact_versions: 同样处理 same approach.
审计轨迹来自 audit_log，带 request_id/category 引用。
```

### New v10 tables (schema 13) · 新增 v10 表

```
opinions(opinion_id, fact_id, topic, stance('support'|'oppose'|'neutral'),
  confidence REAL, evidence_ids TEXT, owner_principal, created_at, updated_at)

observations(observation_id, summary(topic), supporting_opinion_ids TEXT,
  confidence REAL, stale 0/1, owner_principal, created_at, updated_at)
```

CLI/命令通过 `mimir_v8.worker consolidate` 读取这些表（单次运行：同主题 ≥3 条
opinions → 生成 observation）。`run_governance_once` 在权限范围内处理审核队列。

---

## 2. API Layer (FastAPI) · API 层

| Area 区域 | Endpoints 端点 | Scope 权限 |
|----|---|---|
| 查询 Query `/v8/query` | POST | `read` |
| 事实 CRUD `/v8/facts*` | POST/PATCH/DELETE | `write`/`delete`/`manage` |
| 学习管线 Learning `/v8/learning/*` | GET/POST | `read`/`write` |
| 候选工作流 Candidate `/v8/candidates*` | POST | `ingest`/`review` |
| 知识 Knowledge `/v9/knowledge/*` | GET/POST | `read`/`write` |
| 治理 Governance `/v10/opinions` | GET/POST | GET: read, POST: write |
| `/v10/observations` | GET | `read` |
| `/v10/opinions/consolidate` | POST | `manage` |
| `/v10/governance/run` | POST | `manage` |
| `/v10/candidates/{id}/fast_track` | POST | `write` |

Headers 请求头：`Authorization: Bearer ***`，scope 来自 `TokenStore`。
HTTP 401（缺失/无效 token）/ 403（权限不足）。

---

## 3. Query (Document Relevance Metering) · 查询（文档相关性计量）

```
QueryRequest → RelevanceGate#should_search
  (启发式跳过：空 / 寒暄 / 短对话 / 记忆关键词 / 实体 / 疑问句)
  → Vector SEARCH (Chroma, cosine nRank 50)
  → FTS SEARCH (SQLite FTS5)
  → Graph LOOKOUT (图边邻域扩展)
  → RRF merge (k=60) → ACL filter → 最终衰减与信任重加权
  → Top-K + score_explanation（可审计）
```

**v10 改进**：`include_provisional` 标志可包含 `status='provisional'` 的结果，
否则 fast_tracked 的结果会被丢弃。

---

## 4. Governance pipeline (v10, within package) · 治理管线

`worker governance`（systemd 定时器每 15 分钟）
```
:: review_required ordered created_at
→ LLM assessment (可配置模型, temperature 0.1, max_tokens 512)
→ make_decision:
    is_noise → auto_reject
    risk high/critical → human_review
    risk low & confidence≥0.7 → provisional
    else → human_review
→ review_candidate(action=approve|reject) → 更新统计 → commit_approved (原子, outbox 扇出)
```

**fast_track** 端点在人工确认（confidence 0.5）后手动提升待处理候选，否则系统将
其送回 human_review。

---

## 5. Opinion/Observation Layer (v10) · 意见/观察层

**Opinions 意见**：
- 写入/更新 Write/update：agent `set_opinion({"fact_id","topic","stance","confidence","owner"})`，`UNIQUE(fact_id, owner_principal)`
- 演化 Evolve：signal `confirm/useful` → confidence ±0.1
- 合并 Consolidate：`mimir_v8.worker consolidate` — 同主题 ≥3 条 opinions → `observations` (stale=0)

**Observations 观察**：基于较强 opinions（confidence ≥ 0.6）构建的摘要。

---

## 6. Dashboard · 看板

`~/mimir-dashboard`
- ASP.NET 风格后端 FastAPI（8800）反向代理 Mímir HTTP 端点
- 前端单页 index.html（Alpine.js + Chart.js），**9 个标签页**：overview / memory /
  review / sources / agents / opinions / system / symbolic / codegraph
- **v11 改进**：Claude 风格重设计（暖奶油/炭灰主题、衬线标题、珊瑚强调色、明暗切换、
  移动端安全区底导航）；修复 opinions 标签转义引号渲染 bug；修复 `/api/source/add`
  重复函数；新增 v11 代理路由（`/v11/symbolic/*`, `/v11/code/*`）。

---

## 7. v11 Symbolic Memory + CodeGraph · 符号记忆 + 代码图谱

### Symbolic short-term memory · 符号短时记忆（`mimir_v8/symbolic_memory.py`）
灵感来自 TencentDB Agent Memory：
```
冗长工具日志 → 卸载到 symbolic_blocks (node_id, summary, raw_text)
  → 上下文中的 Mermaid 画布 (symbolic_canvases)
  → Agent 在画布上推理 → 通过 node_id 下钻 → 完整原始文本
```
- `symbolic_blocks` / `symbolic_canvases` 表（schema 14）
- API：`/v11/symbolic/offload`, `/v11/symbolic/canvas`, `/v11/symbolic/{node_id}`

### CodeGraph（`code_symbols` + `code_relations`）
- 索引代码符号（name/kind/file/line/signature/doc）
- 记录调用者/被调用者边
- API：`/v11/code/search`, `/v11/code/impact/{symbol_id}`

### v10 reflect/federation（替换占位符）
- `/v10/reflect/{topic}` — 从相关 facts + opinions 合成洞见
- `/v10/federation/{peer_hierarchy:path}` — 跨主体的 ACL 共享搜索

---

## 8. Timer cron jobs (systemd) · 定时任务

```
mimir-v9.2-cdc 每 5min (via worker hermes-cdc)
mimir-v9.2-governance 每 15min
mimir-v9.2-review-reminder 每日 daily
mimir-v9.2-daily-report 每日 daily
mimir-v9.2-decay-scan 每 24h
mimir-v9.2-collect-all 每 30min
mimir-v9.2-trust-update 每 1h
```

---

## 9. v12 Insight (schema 15→18) · 洞察

```
schema 15  EvolveMem: search_feedback, quality_metrics; facts.valid_from/valid_to (Chronos)
schema 16  Conflict: conflict_resolutions (检测/解决 -> 败方 disputed, 永不删除)
schema 17  Crystal: crystal_candidates (7 天主题聚类 ≥3 -> 候选 -> 人工批准)
schema 18  Multi-modal: fact_assets (图片/音频/文档/文件引用, 发布策略守护)
```

- **Retrieval funnel 召回漏斗** — `QueryKernel.trace()`: RelevanceGate → CandidatePool →
  JaccardDedup → ChronosDecay → TopK（`POST /v12/search/trace`）。
- **EvolveMem** — `/v12/evolve/*`; systemd `mimir-v12-evolve` 每 6h 聚合 7 天反馈，
  微调置信度 ±0.05，仅审计事件。
- **Conflict 冲突** — `/v12/conflicts/*`; 败方翻转 `status='disputed'` +
  `fact.conflict_lost` 事件（历史保留）。
- **Crystallization 结晶** — `/v12/crystals/*`; systemd `mimir-v12-crystallize` 每日
  00:30；批准后通过 `create_fact(connection=...)` 物化为 pattern 事实。
- **Multi-modal 多模态** — `/v12/facts/{id}/assets`; 资产在 `wikilink.py` 笔记中渲染为
  Obsidian `![[embed]]`（确定性 slug + Backlinks 双向链接）。
- **MCP** — 27 工具（Core CRUD / Facts / Reflect / Evolve / Conflict / Crystal /
  Trace / Multi-modal），由 `client.MimirAPIClient` 在 v12 REST 接口上支撑。
- **Packaging 打包** — PyPI wheel+sdist 经 `pyproject.toml`（`mimir-server/worker/
  migrate/cli` 入口）；自包含 `Dockerfile`（ghcr 拉取），看板单独从
  `mimir-dashboard/` 发布。
